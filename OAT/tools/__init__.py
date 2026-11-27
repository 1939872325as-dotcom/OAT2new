"""
将util的所有功能都打包到一起
"""
from .WindowChecker import WindowChecker
from .OnmyojiAuto import OnmyojiAutomation

# 灰度层：把「彩色 → 灰度」挂到 cv2.matchTemplate 上。
# 非 OCR 的识别最终都走 matchTemplate（pyscreeze / pyautogui 内部也是它），
# 在这里装一次，新增调用点和第三方调用都自动继承，不需要逐个调用点配置。
# 开关在「设置 → 灰度匹配」（settings.USE_GRAYSCALE，默认开）。
# 装不上也不能影响启动：各调用点仍有显式转换兜底。
try:
    from . import match_util
    match_util.install_global_patch()
except Exception as _e:  # pragma: no cover
    try:
        from OAT.utils.logging import logger
        logger.warning(f"灰度层安装失败（退回逐调用点转换）: {_e}")
    except Exception:
        pass

__all__ = [
    'WindowChecker',
    'OnmyojiAutomation',
]