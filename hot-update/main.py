# -*- coding: utf-8 -*-
"""
@Author: RMA-MUN
@email: n3032747608@163.com
@Date: 2026/06/02
@Version: 2.3.0
@Location: https://github.com/RMA-MUN/OnmyojiAuto

热更新启动器（hot-update launcher）
=================================

让打包后的 OAT.exe **优先从 exe 同目录下的 `OAT/` 源码文件夹加载程序代码**。
因此修改 `OAT/**.py` 之后，只要重启程序就会生效，无需再用 Nuitka 重新编译。

打包方式（关键参数，缺一不可）
------------------------------
    python -m nuitka --standalone \
        --nofollow-import-to=OAT \
        --enable-plugin=pyqt6 \
        --include-data-dir=OAT/tools/uiResources=OAT/tools/uiResources \
        --output-filename=OAT main.py

其中 `--nofollow-import-to=OAT` 是让「磁盘源码优先」真正生效的关键：
Nuitka 的冻结导入器（meta_path）优先级高于 sys.path，只要 OAT 被编译进 exe，
磁盘上的 .py 就会被完全忽略 —— 这正是旧版「改了源码不生效」的根因。

部署目录结构
------------
    OAT.exe                    <- 启动器（第三方依赖已编入）
    *.dll / *.pyd / PyQt6/ ... <- 第三方依赖
    OAT/                       <- 业务源码（热更新目标，改这里）
        app/ core/ pipeline/ source/ tools/ utils/ config/
    logs/                      <- 运行日志

若 `OAT/` 文件夹不存在，会自然回退到 exe 内置代码，行为与旧版一致，不会崩。
"""

import os
import sys


def _prefer_disk_source() -> str:
    """把 exe（或当前脚本）所在目录插到 sys.path 最前，使 OAT 从磁盘源码加载"""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        # Nuitka / PyInstaller 打包后：以 exe 所在目录为准
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        # 直接 python main.py：以本文件所在目录为准
        base = os.path.dirname(os.path.abspath(__file__))

    if os.path.isdir(os.path.join(base, "OAT")):
        # 插到最前，确保磁盘源码优先于任何内置副本
        while base in sys.path:
            sys.path.remove(base)
        sys.path.insert(0, base)
    return base


SOURCE_DIR = _prefer_disk_source()

# 仅为让 Nuitka 收集第三方依赖而 import（该模块体内所有 import 都在函数里，
# 运行时不执行任何 import，因此不会干扰 Qt/COM 的初始化顺序）
try:
    import _nuitka_deps  # noqa: F401
except Exception:
    pass

from OAT.core.app_launcher import launch_app

if __name__ == "__main__":
    sys.exit(launch_app())
