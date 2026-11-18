# OCR服务类：提供统一的OCR调用接口
import hashlib
import threading
import time
from typing import Optional, Tuple, List

import numpy as np

# 导入OCR管理器
from OAT.tools.OCRManager import OCRManager


class OCRService:
    """
    OCR服务类：提供统一的OCR调用接口
    封装OCRManager，提供简洁的API和异步调用支持
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """单例模式，确保全局只有一个OCR服务实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(OCRService, cls).__new__(cls)
        return cls._instance
    
    def __init__(self):
        """初始化OCR服务"""
        if not hasattr(self, '_initialized'):
            self._initialized = True
            self.ocr_manager = OCRManager()
            self._cache = {}
            self._cache_timeout = 1.0  # 缓存超时时间（秒）
            # 缓存条目上限：键是每帧降采样指纹，几乎每帧都不同，原来只增不清，
            # 长跑数小时会累积数万条目（缓慢内存泄漏）。1s TTL 内真正可能命中的
            # 键也就几十个，256 上限绰绰有余。
            self._cache_max = 256
            # 整帧识别缓存：帧指纹 → (时间戳, items)。一轮内多个图像配置共用
            # 同一次 OCR 结果，帧换了指纹就变，天然失效。
            self._frame_cache = {}
            self._frame_cache_max = 8
            # 单飞锁：并行扫描时多个线程可能同时要求同一帧的 OCR 结果，
            # 这里串行化计算并二次检查缓存，保证同一帧只推理一次。
            self._frame_compute_lock = threading.Lock()
            # 最近一次 find_text 的匹配置信度，供上层写日志用
            self.last_match_confidence = 0.0
    
    def find_text(self, image_input, target_text: str, debug: bool = False,
                  confidence_threshold: float = 0.5, region=None) -> Tuple[bool, Optional[List[List[float]]], Optional[str]]:
        """
        同步查找文字
        
        参数:
            image_input: 图片文件路径（字符串）或图像对象（numpy数组）
            target_text: 要查找的目标文字
            debug: 是否启用调试输出，默认为False
            confidence_threshold: 置信度阈值，默认为0.5
            region: 可选搜索区域，语义同 click_area：[x1, x2, y1, y2]（基于传入图像本身）
            
        返回:
            tuple: (是否找到, 文字区域坐标, 文字内容)
            - 是否找到: bool类型，True表示找到，False表示未找到
            - 文字区域坐标: list类型，格式为 [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]，未找到则为None
            - 文字内容: str类型，识别到的实际文字内容，未找到则为None
        """
        # 生成缓存键
        # 注意：图像对象必须把「内容」也算进键。原来固定用 f"image:{target_text}"，
        # 1 秒内即使换了一帧也命中旧结果，会返回上一帧的坐标 —— 表现为点错位置。
        cache_key = None
        if isinstance(image_input, str):
            cache_key = f"file:{image_input}:{target_text}:{confidence_threshold}:{region}"
        else:
            try:
                # 降采样指纹：逐像素哈希太慢，64x64 采样足以区分不同帧
                step_y = max(1, image_input.shape[0] // 64)
                step_x = max(1, image_input.shape[1] // 64)
                sig = hashlib.md5(
                    np.ascontiguousarray(image_input[::step_y, ::step_x]).tobytes()
                ).hexdigest()
                cache_key = f"image:{target_text}:{sig}:{confidence_threshold}:{region}"
            except Exception:
                cache_key = None   # 拿不到指纹就不缓存：宁可慢，不能用错缓存
        
        # 检查缓存
        current_time = time.time()
        if cache_key and cache_key in self._cache:
            cached = self._cache[cache_key]
            cached_result, cache_time = cached[0], cached[1]
            if current_time - cache_time < self._cache_timeout:
                self.last_match_confidence = cached[2] if len(cached) > 2 else 0.0
                return cached_result
        
        # 执行OCR识别
        try:
            result = self.ocr_manager.find_text_offline(
                image_input, target_text, debug=debug,
                confidence_threshold=confidence_threshold, region=region)
            self.last_match_confidence = self.ocr_manager.last_match_confidence
            
            # 更新缓存（一并缓存置信度，这样缓存命中时 last_match_confidence 才准确）
            if cache_key:
                self._cache[cache_key] = (result, current_time, self.last_match_confidence)
                # 上限淘汰：dict 保序（最旧在前）。先顺手清掉已过期条目，
                # 再按 FIFO 裁剪到上限，防止长跑内存只增不减。
                if len(self._cache) > self._cache_max:
                    try:
                        stale = [k for k, v in self._cache.items()
                                 if current_time - v[1] >= self._cache_timeout]
                        for k in stale:
                            self._cache.pop(k, None)
                    except Exception:
                        pass
                    while len(self._cache) > self._cache_max:
                        try:
                            self._cache.pop(next(iter(self._cache)))
                        except StopIteration:
                            break
                        except Exception:
                            break
            
            return result
        except Exception:
            return False, None, None
    
    # ---- 整帧一次性识别（一轮内所有图像配置共用）----

    @staticmethod
    def _frame_signature(image) -> Optional[str]:
        """帧指纹：64x64 降采样哈希。取不到返回 None（调用方不做缓存）。"""
        try:
            step_y = max(1, image.shape[0] // 64)
            step_x = max(1, image.shape[1] // 64)
            return hashlib.md5(
                np.ascontiguousarray(image[::step_y, ::step_x]).tobytes()).hexdigest()
        except Exception:
            return None

    def recognize_frame_items(self, image_input, ttl: float = 2.0) -> List[dict]:
        """对整帧做【一次】OCR，返回图上全部文字行（供一轮内多个配置复用）。

        与 find_text 的区别：不限定目标词、不做区域裁剪 —— 谁需要就按自己的
        ocr_region 在结果里过滤。这样一轮 N 个图像的 OCR 兜底只花 1 次推理
        （原来每图各跑一次 OCR，1416x798 每轮 3~10 次）。

        预处理与 find_text 保持一致（alpha=1.5），命中口径不变。
        同一帧（指纹相同）在 ttl 内重复调用直接命中缓存。

        Returns:
            list[dict]: 每项 {text, confidence, area}；area 为整帧坐标四点多边形。
        """
        try:
            if isinstance(image_input, str):
                from OAT.utils.image_io import imread as _imread_safe
                img = _imread_safe(image_input) if os.path.exists(image_input) else None
            else:
                img = image_input
            if img is None or getattr(img, "size", 0) == 0:
                return []

            sig = (id(img), self._frame_signature(img))
            now = time.time()
            if sig:
                hit = self._frame_cache.get(sig)
                if hit and now - hit[0] < ttl:
                    return hit[1]

            # 单飞：并行扫描时可能有多个线程同时要这一帧的结果，串行化计算 + 二次检查
            with self._frame_compute_lock:
                if sig:
                    hit = self._frame_cache.get(sig)
                    if hit and time.time() - hit[0] < ttl:
                        return hit[1]

                try:
                    import cv2 as _cv2
                    proc = _cv2.convertScaleAbs(img, alpha=1.5, beta=0)
                except Exception:
                    proc = img

                self.ocr_manager._init_reader()
                reader = self.ocr_manager.reader
                if reader is None:
                    return []

                # 整帧用 box_thresh=0.5：整帧 det 会缩到 max_side=960，小字漏检概率比
                # 区域裁剪高；放宽框阈值多召回，再由「子串 + ocr_region + 置信阈值」
                # 三重筛选保证不误点（与自动填文字的取词口径一致）。
                res = reader.infer(proc, box_thresh=0.5)
                items = []
                # 注意：RapidOCR 3.10 起 txts/scores/boxes 是 numpy 数组，
                # `if txts:` / `x or []` 会抛 "truth value of an array is ambiguous"，
                # 必须显式判 None/len。
                txts = getattr(res, "txts", None)
                if txts is not None and len(txts) > 0:
                    scores = getattr(res, "scores", None)
                    boxes = getattr(res, "boxes", None)
                    for i in range(len(txts)):
                        t = txts[i]
                        if t is None:
                            continue
                        t = str(t).strip()
                        if not t:
                            continue
                        try:
                            conf = float(scores[i]) if scores is not None else 0.0
                        except Exception:
                            conf = 0.0
                        try:
                            b = boxes[i]
                            area = b.tolist() if hasattr(b, "tolist") else b
                        except Exception:
                            area = None
                        items.append({"text": t, "confidence": conf, "area": area})

                if sig:
                    if len(self._frame_cache) > self._frame_cache_max:
                        try:
                            oldest = min(self._frame_cache.items(),
                                         key=lambda kv: kv[1][0])[0]
                            self._frame_cache.pop(oldest, None)
                        except Exception:
                            self._frame_cache.clear()
                    self._frame_cache[sig] = (time.time(), items)
                return items
        except Exception as e:
            try:
                from OAT.utils.logging import logger
                logger.warning(f"整帧OCR失败: {e}")
            except Exception:
                pass
            return []

    def recognize_all_text(self, image_input, min_confidence: float = 0.5) -> List[dict]:
        """识别图片里的全部文字，供「替换模板图时自动填 OCR 文本」使用。

        与 find_text 的区别：不限定目标词，返回图上所有文字。
        识别不到或失败时返回空列表（调用方据此提示用户手动填写）。
        """
        try:
            return self.ocr_manager.recognize_all_text(
                image_input, min_confidence=min_confidence)
        except Exception as e:
            try:
                from OAT.utils.logging import logger
                logger.warning(f"识别图片文字失败: {e}")
            except Exception:
                pass
            return []

    def get_text_area_center(self, text_area: List[List[float]]) -> Tuple[float, float]:
        """
        获取文字区域的中心点坐标
        
        参数:
            text_area: 文字区域坐标，格式为 [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
            
        返回:
            tuple: (center_x, center_y) 中心点坐标
        """
        if not text_area or len(text_area) != 4:
            return (0.0, 0.0)
        
        # 计算四个顶点的平均值
        x_coords = [point[0] for point in text_area]
        y_coords = [point[1] for point in text_area]
        
        center_x = sum(x_coords) / len(x_coords)
        center_y = sum(y_coords) / len(y_coords)
        
        return (center_x, center_y)
    
    def get_random_point_in_area(self, text_area: List[List[float]], offset: int = 5) -> Tuple[int, int]:
        """
        在文字区域内随机生成一个点击点
        
        参数:
            text_area: 文字区域坐标，格式为 [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
            offset: 边缘偏移量，避免点击到区域边缘
            
        返回:
            tuple: (x, y) 随机点击坐标
        """
        if not text_area or len(text_area) != 4:
            return (0, 0)
        
        # 获取区域的边界
        x_coords = [point[0] for point in text_area]
        y_coords = [point[1] for point in text_area]
        
        min_x = min(x_coords) + offset
        max_x = max(x_coords) - offset
        min_y = min(y_coords) + offset
        max_y = max(y_coords) - offset
        
        # 确保边界有效
        if min_x >= max_x or min_y >= max_y:
            return self.get_text_area_center(text_area)
        
        # 生成随机点
        import random
        x = random.randint(int(min_x), int(max_x))
        y = random.randint(int(min_y), int(max_y))
        
        return (x, y)


# 全局OCR服务实例
ocr_service = OCRService()
