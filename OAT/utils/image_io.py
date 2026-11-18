# 图片读写（Unicode 路径安全）
#
# 为什么需要：OpenCV 在 Windows 上用窄字符 API 打开文件，**中文/非 ASCII 路径
# 一律读不到**（日志表现为 `imread_(...): can't open/read file`，返回 None）。
# 而本项目的图片名现在按识别到的 OCR 文字自动命名（准备.png / 召唤.png），
# 必然出现中文文件名 —— 直接 cv2.imread/imwrite 会让模板匹配、OCR、截图保存
# 全部静默失效。
#
# 规避办法（OpenCV 官方推荐做法）：用 Python 自己打开文件（os/python 层支持
# Unicode），读成字节流后交给 cv2.imdecode / cv2.imencode 编解码。
import os

import cv2
import numpy as np


def imread(path: str, flags: int = cv2.IMREAD_COLOR):
    """等价 cv2.imread，但支持中文路径。读不到返回 None。"""
    if not path or not isinstance(path, str):
        return None
    try:
        img = cv2.imread(path, flags)
        if img is not None:
            return img
    except Exception:
        pass
    # 中文路径兜底：走字节流解码
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        if buf.size == 0:
            return None
        return cv2.imdecode(buf, flags)
    except Exception:
        return None


def imwrite(path: str, img, ext: str = None) -> bool:
    """等价 cv2.imwrite，但支持中文路径。成功返回 True。"""
    if img is None:
        return False
    try:
        suffix = ext or os.path.splitext(path)[1] or ".png"
        ok, buf = cv2.imencode(suffix, img)
        if not ok:
            return False
        buf.tofile(path)
        return True
    except Exception:
        return False