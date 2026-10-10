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
            
            return result
        except Exception:
            return False, None, None
    
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
