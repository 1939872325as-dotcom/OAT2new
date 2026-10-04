import os

import cv2
from rapidocr import RapidOCR

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
        """延迟初始化OCR reader"""
        if self.reader is None:
            try:
                logger.info('正在初始化OCR引擎')
                # 初始化RapidOCR（支持中英文识别）
                self.reader = RapidOCR(
                    params={
                        "Det.box_thresh": 0.8,  # 文本检测阈值，取值：0.1~0.9 | 越大=越严格（只框清晰文字）| 越小=越宽松（容易框背景）
                        "Det.unclip_ratio": 1.8,  # 增大文本框大小，取值：1.0~2.5 | 越大=框越大 | 越小=框紧贴文字
                        "Global.text_score": 0.7,  # 文字识别置信度阈值， 取值：0.1~0.9 | 低于该值的识别结果会被丢弃
                        "Det.max_side_len": 960  # 设置最大边长，避免图像过大
                    }
                )
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
            img = cv2.imread(image_input)
            if img is None:
                logger.error(f"无法读取图片：{image_input}")
                return False, None, None
        else:
            # 直接使用图像对象
            img = image_input

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
        
        # 使用RapidOCR进行识别
        results = self.reader(img)

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
