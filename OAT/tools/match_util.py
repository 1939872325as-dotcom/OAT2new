"""模板匹配的灰度层：一处安装，所有特征匹配默认走灰度。

为什么挂在 `cv2.matchTemplate` 这一层：
- 非 OCR 的识别最终都落到 `cv2.matchTemplate`（pyscreeze / pyautogui 内部也是它），
  在这里统一转换，**新增调用点、第三方库调用都自动继承**，不需要每个调用点单独配。
- OCR 走模型推理，不经过 matchTemplate，完全不受影响。

开关：设置 → 灰度匹配（`settings.USE_GRAYSCALE`，默认 True），运行中切换立即生效。
需要彩色匹配的特例：`with match_util.color_mode(): cv2.matchTemplate(...)`。

性能：同一帧会被一轮内多个任务复用，灰度图按线程缓存最近一份（只缓存大图）。
"""
import threading

import cv2
import numpy as np

from OAT.tools import settings

_local = threading.local()

# 只缓存「大图」（整帧截图）。模板图每次都在换，缓存它们只会把帧缓存挤掉，
# 而且单张小图的 cvtColor 只要几十微秒，不值得缓存。
_CACHE_MIN_PIXELS = 200_000

_INSTALLED = False


def is_grayscale() -> bool:
    """当前是否走灰度（供日志/自检显示）。"""
    return bool(getattr(settings, 'USE_GRAYSCALE', True))


def to_match_image(img):
    """按设置把图像转成匹配用的通道数；None / 非 ndarray 原样返回。"""
    if img is None or not isinstance(img, np.ndarray):
        return img
    if not is_grayscale() or img.ndim != 3:
        return img

    channels = img.shape[2]
    if channels == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    if channels != 3:
        return img

    if img.size >= _CACHE_MIN_PIXELS:
        cached = getattr(_local, 'frame', None)
        if cached is not None and cached[0] is img:
            return cached[1]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # 同一帧会被一轮内多个任务复用，按线程留最近一份
        _local.frame = (img, gray)
        return gray
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


class color_mode:
    """临时绕过灰度（需要彩色匹配的特例用）。

    用法：
        with match_util.color_mode():
            cv2.matchTemplate(color_img, color_tpl, cv2.TM_CCOEFF_NORMED)
    """

    def __enter__(self):
        _local.bypass = True
        return self

    def __exit__(self, *exc):
        _local.bypass = False
        return False


def install_global_patch() -> bool:
    """把灰度转换挂到 cv2.matchTemplate 上（幂等，返回是否已安装）。"""
    global _INSTALLED
    if _INSTALLED:
        return True

    original = getattr(cv2, 'matchTemplate', None)
    if original is None:
        return False

    def _match_template(image, templ, method, result=None, mask=None):
        if not getattr(_local, 'bypass', False):
            image = to_match_image(image)
            templ = to_match_image(templ)
            if isinstance(mask, np.ndarray) and mask.ndim == 3:
                mask = to_match_image(mask)
        if result is None:
            if mask is None:
                return original(image, templ, method)
            return original(image, templ, method, None, mask)
        return original(image, templ, method, result, mask)

    _match_template.__name__ = 'matchTemplate'
    _match_template.__doc__ = ((original.__doc__ or '')
                               + '\n[OAT] 灰度层：默认把彩色图转灰度后匹配')
    cv2.matchTemplate = _match_template
    _INSTALLED = True
    return True
