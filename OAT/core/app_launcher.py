import sys
import time

from PyQt6 import QtWidgets

from OAT.utils.error_handler import handle_global_exception
from OAT.app.main_window import MainWindow
from OAT.utils.logging import logger
from OAT.utils.autostart_elevation import maybe_elevate

if not hasattr(sys, 'setcheckinterval'):
    def setcheckinterval(interval):
        sys.setswitchinterval(interval / 1000.0)
    sys.setcheckinterval = setcheckinterval


def launch_app():
    # 启动即检测权限：游戏常以管理员运行，低权限下窗口自动调整被 UIPI 拒绝。
    # maybe_elevate 返回 True 表示已发起提权重启 —— 当前进程立刻退出，
    # 新进程（UAC 确认后）以管理员身份接手；用户点「否」则原样继续。
    try:
        if maybe_elevate():
            return 0
    except Exception as e:
        # 提权检测绝不能阻断正常启动
        logger.warning(f"自动提权检测异常（忽略，继续启动）: {e}")

    app = QtWidgets.QApplication([])
    window = None
    try:
        window = MainWindow()
        window.show()
        logger.info("程序启动成功")
        time.sleep(1)
        return app.exec()
    except Exception as e:
        handle_global_exception(e, window)
        return 1
