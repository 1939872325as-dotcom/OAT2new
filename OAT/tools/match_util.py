"""模板匹配的灰度层：一处安装，所有特征匹配默认走灰度。

为什么挂在 `cv2.matchTemplate` 这一层：
- 非 OCR 的识别最终都落到 `cv2.matchTemplate`（pyscreeze / pyautogui 内部也是它），
  在这里统一转换，**新增调用点、第三方库调用都自动继承**，不需要每个调用点单独配。
- OCR 走模型推理，不经过 matchTemplate，完全不受影响。

两层机制（都在这里，全局生效）：
1. **灰度搜索**：`USE_GRAYSCALE` 打开时，3/4 通道的 image/templ/mask 先转单通道再匹配。
   速度约为彩色的 1/6；同一帧按线程缓存，一轮内多任务复用只转一次。
2. **彩色复核**：灰度分 ≥0.65 的最高峰，回原彩色图上取同位置小窗再算一次彩色分。
   彩色分比灰度分低 0.15 以上 → 判为「同形异色」（灰按钮撞彩色模板、稀有度边框、
   元素球等），压制该峰继续找次优。真命中时彩色分 ≈ 灰度分（差 <0.05），不受影响；
   光度类退化（模糊/遮挡）两者同步下降（实测差 <0.03），也不会误伤。
   等待态（灰度分 <0.65）零复核开销。

开关：设置 → 灰度匹配（`settings.USE_GRAYSCALE`，默认 True），运行中切换立即生效。
需要完全绕过两层（含复核）的特例：`with match_util.color_mode(): cv2.matchTemplate(...)`。

已知边界：模板本身低饱和（米白/灰底按钮）时，灰度分和彩色分都会很高（实测挑战模板
去色后 0.979 / 0.961）——彩色复核救不了这种「灰状态」，这类场景用「停止点击」
把灰状态截成条件图解决（那是它设计出来的用途）。
"""
import threading

import cv2
import numpy as np

from OAT.tools import settings
from OAT.utils.logging import logger

_local = threading.local()

# 只缓存「大图」（整帧截图）。模板图每次都在换，缓存它们只会把帧缓存挤掉，
# 而且单张小图的 cvtColor 只要几十微秒，不值得缓存。
_CACHE_MIN_PIXELS = 200_000

# 彩色复核参数：
# _VERIFY_FLOOR  灰度最高分低于它时不复核（项目阈值滑条范围 70~100%，低于 0.65 任何调用者都不会接受）
# _VERIFY_GAP    彩色分比灰度分低超过它才判「同形异色」（真命中/光度退化时两者差 <0.05）
# _VERIFY_MAX    最多压制多少个峰（防异常画面把循环打满）
_VERIFY_FLOOR = 0.65
_VERIFY_GAP = 0.15
_VERIFY_MAX = 5

# 越大越好的方法才做峰值复核；SQDIFF 系越小越好，峰提取逻辑相反，直接跳过
_POSITIVE_METHODS = frozenset({
    cv2.TM_CCOEFF_NORMED, cv2.TM_CCOEFF,
    cv2.TM_CCORR_NORMED, cv2.TM_CCORR,
})

_INSTALLED = False
_original_matchTemplate = None


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
    """临时绕过灰度层（含彩色复核）。

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


def _color_verify_score(image, templ, loc, method):
    """在灰度命中的位置上取原彩色小窗，返回彩色分（算不了返回 None）。"""
    y, x = loc[1], loc[0]
    h, w = templ.shape[:2]
    patch = image[y:y + h, x:x + w]
    if patch.shape[:2] != (h, w):
        return None
    r = _original_matchTemplate(patch, templ, method)
    return float(r.max())


def _suppress_color_mismatch(image, templ, res, method):
    """对结果矩阵里「灰度过阈但彩色对不上」的峰做压制，返回过滤后的矩阵。

    压制 = 把该峰周围一块区域置 -1（覆盖相关峰簇 + 模板足迹），然后在剩余矩阵上
    继续找最高峰，直到最高峰可信或低于 _VERIFY_FLOOR。未发生压制时不拷贝矩阵。
    每次压制都写一条后台日志（前端不显示）——「偶发性不识别」排查全靠它。
    """
    h, w = templ.shape[:2]
    H, W = res.shape[:2]
    pad = max(4, min(h, w) // 4)   # 峰簇比模板脚印略宽，向外扩一圈才算压干净
    original_res = res             # 异常时退回未过滤的结果，绝不能返回半截矩阵
    modified = False
    try:
        for _ in range(_VERIFY_MAX):
            _, mx, _, mxl = cv2.minMaxLoc(res)
            if mx < _VERIFY_FLOOR:
                break
            cs = _color_verify_score(image, templ, mxl, method)
            if cs is None or cs >= mx - _VERIFY_GAP:
                break
            if not modified:
                res = res.copy()
                modified = True
            x1, y1 = max(0, mxl[0] - pad), max(0, mxl[1] - pad)
            x2, y2 = min(W, mxl[0] + w + pad), min(H, mxl[1] + h + pad)
            res[y1:y2, x1:x2] = -1.0
            logger.log_to_file({
                'level': 'WARNING',
                'module': 'OAT.tools.match_util',
                'message': (f"彩色复核否决：灰度分 {mx:.3f} @({mxl[0]},{mxl[1]}) "
                            f"彩色分 {cs:.3f}（差 {mx - cs:.3f} ≥ {_VERIFY_GAP}），"
                            f"判同形异色，压制后继续找次优"),
            })
        return res
    except Exception:
        return original_res


def install_global_patch() -> bool:
    """把灰度层挂到 cv2.matchTemplate 上（幂等，返回是否已安装）。"""
    global _INSTALLED, _original_matchTemplate
    if _INSTALLED:
        return True

    original = getattr(cv2, 'matchTemplate', None)
    if original is None:
        return False
    _original_matchTemplate = original

    def _call(image, templ, method, result=None, mask=None):
        if result is None:
            if mask is None:
                return original(image, templ, method)
            return original(image, templ, method, None, mask)
        return original(image, templ, method, result, mask)

    def _match_template(image, templ, method, result=None, mask=None):
        bypass = getattr(_local, 'bypass', False)
        # 彩色复核要用「转换前」的原图：先留底，再做灰度转换
        color_image, color_templ = image, templ
        if not bypass and is_grayscale():
            image = to_match_image(image)
            templ = to_match_image(templ)
        res = _call(image, templ, method, result, mask)
        # 彩色复核：要求调用方传的就是彩色图（自己传灰度图时无法复核，跳过）
        if (not bypass
                and is_grayscale()
                and method in _POSITIVE_METHODS
                and mask is None
                and isinstance(res, np.ndarray) and res.ndim == 2
                and isinstance(color_image, np.ndarray) and color_image.ndim == 3
                and isinstance(color_templ, np.ndarray) and color_templ.ndim == 3):
            try:
                res = _suppress_color_mismatch(color_image, color_templ, res, method)
            except Exception:
                pass  # 复核任何异常都不能影响匹配主链路
        return res

    _match_template.__name__ = 'matchTemplate'
    _match_template.__doc__ = ((original.__doc__ or '')
                               + '\n[OAT] 灰度层：默认灰度搜索 + 彩色复核（同形异色否决）')
    cv2.matchTemplate = _match_template
    _INSTALLED = True
    return True
