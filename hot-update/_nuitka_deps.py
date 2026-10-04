# -*- coding: utf-8 -*-
"""
仅为 Nuitka 静态分析服务的依赖清单（hot-update 构建专用）。

为什么这样写
------------
热更新构建用 `--nofollow-import-to=OAT` 让 Nuitka 不编译 OAT 业务代码，
副作用是 Nuitka 不再扫描 OAT 内部的 import，于是 OAT 依赖的第三方库
（PyQt6 / cv2 / rapidocr / ...）就不会被打进 exe。

所以要在 main.py 能看到的地方把它们的 import 写出来，让 Nuitka 顺着收进去。

**关键：这些 import 必须放在函数体内、且永不调用。**
若写在模块顶层，程序一启动就会抢先 import PyQt6 / qfluentwidgets / rapidocr /
pythoncom —— 这会打乱 Qt 与 COM 的初始化顺序，实测直接导致
「Nuitka: A segmentation fault has occurred」段错误崩溃。
放在函数里：Nuitka 的静态分析仍会跟随这些 import（函数内 import 同样会被收集），
但运行时模块体是空的，零副作用。
"""


def _collect_deps_for_nuitka():
    """仅用于让 Nuitka 收集依赖，运行时不调用。"""
    # --- GUI ---
    import PyQt6            # noqa: F401
    import PyQt6.QtCore     # noqa: F401
    import PyQt6.QtGui      # noqa: F401
    import PyQt6.QtWidgets  # noqa: F401
    import PyQt6.QtSvg      # noqa: F401
    import PyQt6.QtNetwork  # noqa: F401
    import qfluentwidgets   # noqa: F401

    # --- 图像 / 数值 ---
    import cv2              # noqa: F401
    import numpy            # noqa: F401
    import PIL              # noqa: F401
    import PIL.Image        # noqa: F401

    # --- OCR ---
    import rapidocr         # noqa: F401
    import onnxruntime      # noqa: F401

    # --- Windows 交互 ---
    import win32api         # noqa: F401
    import win32con         # noqa: F401
    import win32gui         # noqa: F401
    import win32ui          # noqa: F401
    import win32print       # noqa: F401
    import win32process     # noqa: F401
    import pywintypes       # noqa: F401
    import pythoncom        # noqa: F401
    import psutil           # noqa: F401

    # --- 输入模拟 / 截图 ---
    import pyautogui        # noqa: F401
    import pyscreeze        # noqa: F401
    import pynput           # noqa: F401

    # --- 杂项 ---
    import yaml             # noqa: F401
    import loguru           # noqa: F401
    import requests         # noqa: F401
    import tqdm             # noqa: F401
