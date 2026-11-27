"""模板匹配的统一入口：灰度 / 彩色由设置开关决定（默认灰度）。

为什么要有这一层：
- 灰度把三通道合成单通道，`matchTemplate` 的耗时约为彩色的 1/6
  （1416x798 全帧实测 142ms → 21ms，10 张模板一轮 1.3s → 0.22s）。
  现有 117 张模板的分数与彩色一致（正样本最低分、负样本最高分都在同一档）。
- 代价是丢掉色度：**「形状/亮度相同、只有颜色不同」的两个状态会变得不可区分**
  （御魂稀有度边框、元素球、技能可用/冷却、选中金边等）。这类场景要关掉灰度。

开关在「设置 → 识别阈值」下方，运行中切换立即生效（每次匹配现读 settings）。

性能细节：同一帧会被一轮内的多个任务复用，`to_match_image` 按线程缓存最近一帧的
灰度图（线程池里每个线程各留一份），避免每个任务都重做一次 1416x798 的 cvtColor。
"""
import threading

import cv2

from OAT.tools import settings

_local = threading.local()

# 只缓存「大图」（整帧截图）。模板图每次都在换，缓存它们只会把帧缓存挤掉，
# 而且单张小图的 cvtColor 只要几十微秒，不值得缓存。
_CACHE_MIN_PIXELS = 200_000


def is_grayscale() -> bool:
    """当前是否走灰度（供日志/自检显示）。"""
    return bool(getattr(settings, 'USE_GRAYSCALE', True))


def to_match_image(img):
    """按设置把图像转成匹配用的通道数；None 原样返回。"""
    if img is None:
        return None
    if not is_grayscale() or getattr(img, 'ndim', 0) != 3:
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


def match_template(search, target, method=cv2.TM_CCOEFF_NORMED):
    """灰度/彩色自适应的一次匹配，返回 (max_val, max_loc)。

    调用方负责保证 search 不小于 target（OpenCV 尺寸不足会抛错）。
    """
    result = cv2.matchTemplate(to_match_image(search), to_match_image(target), method)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    return float(max_val), max_loc
