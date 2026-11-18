import os

import cv2

from OAT.utils.OCREnginePool import OCREnginePool
from OAT.utils.logging import logger


class OCRManager:
    """
    OCR管理器类：提供离线文字识别功能
    用于识别图片中的文字并返回文字在窗口内的区域坐标
    """
    
    def __init__(self):
        """初始化OCR管理器"""
        self.reader = None
        # 最近一次 find_text_offline 的匹配置信度。单独用属性暴露，
        # 避免为了日志去改 (found, area, text) 这个三元返回签名。
        self.last_match_confidence = 0.0
    
    def _init_reader(self):
        """延迟初始化OCR reader

        reader 现在是 OCREnginePool（CPU+GPU 双引擎动态调度，见 OAT/utils/OCREnginePool.py）。
        infer(img) 的返回值与原 RapidOCR(img) 一致，调用方无感。
        OAT_OCR_MODE=cpu 可退回纯单引擎行为。
        """
        if self.reader is None:
            try:
                logger.info('正在初始化OCR引擎')
                # det/rec 强制指到 PP-OCRv6 模型：exe 内编译的 rapidocr 是老版本，
                # 默认用 PP-OCRv4 det，对暗色底艺术字小图（如『准备』）检测不出框
                # （2026-11-16 实测复现，v6 同图 conf=1.0）。模型文件放在
                # 根目录 rapidocr/models/（数据目录，随源码热更新生效，无需重打包）。
                # 注意 OCRManager 在 OAT/tools/ 下，到根目录要 dirname 三次。
                model_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__)))),
                    'rapidocr', 'models')
                det_v6 = os.path.join(model_dir, 'PP-OCRv6_det_small.onnx')
                rec_v6 = os.path.join(model_dir, 'PP-OCRv6_rec_small.onnx')
                extra = {}
                if os.path.exists(det_v6):
                    extra["Det.model_path"] = det_v6
                if os.path.exists(rec_v6):
                    extra["Rec.model_path"] = rec_v6
                if extra:
                    logger.info(f'OCR模型升级: 使用 PP-OCRv6 det/rec（{len(extra)}个）'
                                f' @ {model_dir}')
                else:
                    logger.warning(f'OCR模型升级: v6 模型未找到于 {model_dir}，'
                                   f'回落编译版默认（v4）')
                # 初始化OCR引擎池（内部按环境决定单CPU还是CPU+GPU双引擎）
                self.reader = OCREnginePool(base_params={
                    "Det.box_thresh": 0.8,  # 文本检测阈值，取值：0.1~0.9 | 越大=越严格（只框清晰文字）| 越小=越宽松（容易框背景）
                    "Det.unclip_ratio": 1.8,  # 增大文本框大小，取值：1.0~2.5 | 越大=框越大 | 越小=框紧贴文字
                    "Global.text_score": 0.7,  # 文字识别置信度阈值， 取值：0.1~0.9 | 低于该值的识别结果会被丢弃
                    "Det.max_side_len": 960,  # 设置最大边长，避免图像过大
                    **extra
                })
                logger.info('OCR引擎初始化完成')
            except Exception as e:
                logger.error(f"OCR初始化失败: {str(e)}")
                self.reader = None
    
    def find_text_offline(self, image_input, target_text: str, debug: bool = False,
                          confidence_threshold: float = 0.5, region=None):
        """
        OCR：查找图片中的指定文字
        
        参数:
            image_input: 图片文件路径（字符串）或图像对象（numpy数组）
            target_text: 要查找的目标文字
            debug: 是否启用调试输出，默认为False
            confidence_threshold: 置信度阈值，默认为0.5
            region: 可选搜索区域，语义与 click_area 一致：[x1, x2, y1, y2]，
                    坐标基于传入图像本身。用于同一画面出现多个相同文字时排除干扰
            
        返回:
            tuple: (是否找到, 文字区域坐标, 文字内容)
            - 是否找到: bool类型，True表示找到，False表示未找到
            - 文字区域坐标: list类型，格式为 [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]，未找到则为None
            - 文字内容: str类型，识别到的实际文字内容，未找到则为None
        """
        # 处理输入：如果是字符串则从文件读取，否则直接使用图像对象
        if isinstance(image_input, str):
            # 检查图片文件是否存在
            if not os.path.exists(image_input):
                logger.error(f"图片不存在：{image_input}")
                return False, None, None
            
            # 读取图片
            from OAT.utils.image_io import imread as _imread_safe
            img = _imread_safe(image_input)
            if img is None:
                logger.error(f"无法读取图片：{image_input}")
                return False, None, None
        else:
            # 直接使用图像对象
            img = image_input
            # 空图守卫：识别对象路径与 recognize_all_text 对齐，空图不进引擎
            # （否则浪费一次 GPU 推理，rapidocr 内部还会打 det 空告警）
            if img is None or getattr(img, "size", 0) == 0:
                return False, None, None

        # 可选：只在 region 指定的范围内做 OCR（语义与 click_area 一致：[x1, x2, y1, y2]）
        offset_x = 0
        offset_y = 0
        if region and len(region) == 4:
            try:
                rx1, rx2, ry1, ry2 = [int(v) for v in region]
                h, w = img.shape[:2]
                cx1, cx2 = max(0, min(rx1, w)), max(0, min(rx2, w))
                cy1, cy2 = max(0, min(ry1, h)), max(0, min(ry2, h))
                if cx2 - cx1 >= 2 and cy2 - cy1 >= 2:
                    img = img[cy1:cy2, cx1:cx2]
                    offset_x, offset_y = cx1, cy1
                    if debug:
                        logger.info(f"[OCR Debug] 限定搜索区域 {region} → 裁剪为 x:{cx1}-{cx2} y:{cy1}-{cy2}")
                else:
                    logger.warning(f"OCR 搜索区域无效（宽或高不足），已忽略: {region}")
            except Exception as e:
                logger.warning(f"OCR 搜索区域解析失败，已忽略: {region} ({e})")

        # 预处理：增强对比度（有助于垂直文字识别）
        img = cv2.convertScaleAbs(img, alpha=1.5, beta=0)  # 增强对比度

        # 延迟初始化OCR reader
        self._init_reader()
        
        # 检查reader是否初始化成功
        if self.reader is None:
            return False, None, None
        
        # 使用OCR引擎池进行识别（CPU+GPU 动态分配）
        results = self.reader.infer(img)
        if debug and hasattr(self.reader, 'stats_line'):
            logger.info(f"[OCR Debug] 引擎池负载: {self.reader.stats_line()}")

        found = False
        text_area = None
        real_text = None
        self.last_match_confidence = 0.0

        # 获取识别结果数据（RapidOCR返回的是RapidOCROutput对象）
        # 注意：整图一个字都没检出时 txts 为 None（不是空列表），直接判空返回
        if not getattr(results, 'txts', None):
            if debug:
                logger.info("[OCR Debug] 未识别到文字")
            return found, text_area, real_text

        candidates = []
        if hasattr(results, 'txts'):
            if debug:
                logger.info(f"[OCR Debug] 识别到的文字数量: {len(results.txts)}")

            # 收集全部候选：不能“命中第一个就返回”——同一画面可能有两个相同文字，
            # 先命中的那个未必是要点的那一个（这正是「点错位置」的根因）。
            for i in range(len(results.txts)):
                text = results.txts[i]
                if target_text not in text:
                    continue
                conf = float(results.scores[i])
                box = results.boxes[i]
                area = box.tolist() if hasattr(box, 'tolist') else box
                if offset_x or offset_y:
                    # 裁剪过 region：坐标还原到原图坐标系，调用方无需感知裁剪
                    area = [[p[0] + offset_x, p[1] + offset_y] for p in area]
                candidates.append({'text': text, 'confidence': conf, 'area': area})
                if debug:
                    logger.info(f"[OCR Debug] 候选: '{text}'，置信度: {conf:.4f}，区域: {area}")

            if candidates:
                # 选择顺序：完全相等优先 → 达到置信度阈值者优先 → 置信度最高者；
                # 并列时保留 OCR 原始顺序（自上而下、自左而右），保证结果确定可复现。
                exact = [c for c in candidates if c['text'].strip() == target_text.strip()]
                if not exact:
                    # 防误点（与整帧路径同一规则）：目标太短时子串会命中一整句
                    # 无关文字（实测『挑战』命中顶部横幅系统消息 → 点错位置）。
                    # 命中文本比目标长 3 字以上即视为不可信，直接判未命中。
                    _tl = len(target_text.strip())
                    near = [c for c in candidates
                            if len(c['text'].strip()) <= _tl + 3]
                    if not near:
                        logger.log_to_file(f"OCR: '{target_text}' 只命中过长文本"
                                           f"（如 '{candidates[0]['text'][:20]}…'），判定不可信")
                        return found, text_area, real_text
                    candidates = near
                pool = exact if exact else candidates
                passed = [c for c in pool if c['confidence'] >= confidence_threshold]
                chosen = max(passed if passed else pool, key=lambda c: c['confidence'])
                found = True
                text_area = chosen['area']
                real_text = chosen['text']
                self.last_match_confidence = float(chosen['confidence'])

                if len(candidates) > 1:
                    # 命中多处时把各自坐标写进日志，据此判断该收窄搜索区域还是该改目标文字
                    logger.info(f"OCR: '{target_text}' 命中 {len(candidates)} 处，"
                                f"选用置信度最高的一处（{chosen['confidence']:.4f}）")
                    for idx, c in enumerate(candidates, 1):
                        xs = [p[0] for p in c['area']]
                        ys = [p[1] for p in c['area']]
                        mark = '   <== 选用' if c is chosen else ''
                        logger.info(f"    候选{idx}: '{c['text']}' conf={c['confidence']:.4f} "
                                    f"x: {min(xs):.0f}-{max(xs):.0f}  y: {min(ys):.0f}-{max(ys):.0f}{mark}")
                elif debug:
                    logger.info(f"[OCR Debug] 找到目标文字: '{target_text}'，"
                                f"匹配文字: '{real_text}'，置信度: {chosen['confidence']:.4f}")
        else:
            if debug:
                logger.info("[OCR Debug] RapidOCR未返回txts属性")

        # 调试输出：统计信息
        if debug:
            logger.info(f"[OCR Debug] 总识别到 {len(results.txts)} 个文字")
            passed_n = len([c for c in candidates if c['confidence'] >= confidence_threshold])
            logger.info(f"[OCR Debug] 达到置信度阈值({confidence_threshold})的候选数: {passed_n}")

        return found, text_area, real_text


    def recognize_all_text(self, image_input, min_confidence: float = 0.5):
        """识别图片里的【全部】文字（不限定目标词），按阅读顺序返回。

        用途：替换模板图时自动认出图上的文字，帮用户把「OCR识别文本」填好。
        早前没有这个方法，只能给定目标词去查找，因此没法做「自动识别图片文字」。

        Args:
            image_input: 图片路径（str）或图像对象（numpy 数组）
            min_confidence: 置信度下限，低于此值的结果丢弃

        Returns:
            list[dict]: 每项 {text, confidence, area}，按从上到下、从左到右排序；
                        识别不到或失败时返回空列表。永不抛异常。
        """
        try:
            if isinstance(image_input, str):
                if not os.path.exists(image_input):
                    logger.error(f"图片不存在：{image_input}")
                    return []
                from OAT.utils.image_io import imread as _imread_safe
                img = _imread_safe(image_input)
                if img is None:
                    logger.error(f"无法读取图片：{image_input}")
                    return []
            else:
                img = image_input

            if img is None or getattr(img, "size", 0) == 0:
                return []

            self._init_reader()
            if self.reader is None:
                return []

            # 多变体识别（自动填文字场景专用，不追求运行时口径一致）：
            # 候选依次跑，取「识别条数最多、总置信最高」的那一版：
            # 1) 原始图 + box_thresh=0.5 —— 暗字亮底/亮字暗底两种极性都能检出；
            # 2) 放大 2x（仅小图）—— 大字艺术字排版（如『召唤鬼王』2x2 排布）
            #    原始尺寸下 det 常只框到上排，放大后两排都能框出（实测 92x108：
            #    原始仅『召唤』，2x 得『召唤』+『鬼王』）；
            # 3) 提对比度兜底（低对比界面）——注意不能当默认，alpha=1.5 会让
            #    暗字亮底的图 det 全空（2026-11-16 实测）。
            variants = [("原始", img)]
            h, w = img.shape[:2]
            if max(h, w) < 260:
                variants.append(("2x放大", cv2.resize(
                    img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)))
            variants.append(("提对比度", cv2.convertScaleAbs(img, alpha=1.5, beta=0)))

            # 择优标准：先看「最长单条文本 × 该条置信度」，再看总置信。
            # 不能用「条数多」当第一优先——拆行会让条数虚高：实测『伤害排名』
            # 原始图拆成 ['伤','方害排名']（2 条，含错字）而提对比度是
            # ['伤害排名']（1 条，conf 1.0），按条数会选到错的拆行版本，
            # 自动填文字就只剩一个字。也不能"≥2 条就提前退出"（同理）。
            best = None          # ((最长*置信, 置信和), results)
            for _name, v in variants:
                try:
                    res = self.reader.infer(v, box_thresh=0.5)
                except Exception:
                    continue
                txts = getattr(res, 'txts', None)
                if txts is None or len(txts) == 0:
                    continue
                scores = getattr(res, 'scores', None)
                try:
                    pairs = [(str(t or '').strip(), float(scores[i]))
                             for i, t in enumerate(txts)]
                except Exception:
                    pairs = [(str(t or '').strip(), 0.0) for t in txts]
                pairs = [(t, c) for t, c in pairs if t]
                if not pairs:
                    continue
                best_item = max((len(t) * c, c) for t, c in pairs)
                score = (round(best_item[0], 6),
                         round(float(sum(c for _t, c in pairs)), 6),
                         len(pairs))
                if best is None or score > best[0]:
                    best = (score, res)
            if best is None:
                return []
            results = best[1]

            try:
                lo = float(min_confidence)
            except (ValueError, TypeError):
                lo = 0.5

            items = []
            for i in range(len(results.txts)):
                text = results.txts[i]
                if text is None:
                    continue
                text = str(text).strip()
                if not text:
                    continue
                try:
                    conf = float(results.scores[i])
                except (ValueError, TypeError, IndexError):
                    conf = 0.0
                if conf < lo:
                    continue
                try:
                    box = results.boxes[i]
                    area = box.tolist() if hasattr(box, 'tolist') else box
                except (IndexError, AttributeError):
                    area = None
                # 排序键：文本框上边缘 → 左边缘，还原成阅读顺序
                y0 = min(p[1] for p in area) if area else 0.0
                x0 = min(p[0] for p in area) if area else 0.0
                items.append({'text': text, 'confidence': conf,
                              'area': area, '_y': y0, '_x': x0})

            items.sort(key=lambda d: (round(d['_y'] / 10.0), d['_x']))
            for d in items:
                d.pop('_y', None)
                d.pop('_x', None)
            return items
        except Exception as e:
            logger.warning(f"识别图片文字失败: {e}")
            return []


# ===================== 测试（只改这里） =====================
if __name__ == "__main__":
    # 创建OCR管理器实例
    ocr_manager = OCRManager()
    
    # 测试图片路径（使用OAT/tools/image.png）
    import os
    IMG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "image.png")
    # 测试目标文字
    TARGET = "进攻"
    
    # 执行离线查找
    found, text_area, real_text = ocr_manager.find_text_offline(IMG_PATH, TARGET, debug=True)
    print(f"测试结果：找到={found}，文字={real_text}，区域={text_area}")
    print(f"text_area类型: {type(text_area)}")
