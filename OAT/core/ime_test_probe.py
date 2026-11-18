# -*- coding: utf-8 -*-
"""临时验证：调用 ensure_chinese_ime() 后，输入框能否打出中文。"""
import ctypes
import os
import time

from PyQt6 import QtCore, QtWidgets

LOG_PATH = os.environ.get("OAT_IME_PROBE", "")
VK = {'n': 0x4E, 'i': 0x49, 'h': 0x48, 'a': 0x41, 'o': 0x4F, 'space': 0x20}


def _log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _key(vk, up=False):
    ctypes.windll.user32.keybd_event(vk, 0, 2 if up else 0, 0)


class _P(QtCore.QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.dlg = QtWidgets.QDialog(parent)
        self.dlg.setWindowTitle("IME-FIX-CHECK")
        lay = QtWidgets.QVBoxLayout(self.dlg)
        self.edit = QtWidgets.QLineEdit(self.dlg)
        lay.addWidget(self.edit)
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()

    def go(self):
        self.edit.clear()
        self.edit.setFocus()
        self.send(0)

    def send(self, i):
        word = "nihao"
        if i >= len(word):
            QtCore.QTimer.singleShot(400, self.commit)
            return
        vk = VK[word[i]]
        _key(vk)
        QtCore.QTimer.singleShot(45, lambda: self.rel(i, vk))

    def rel(self, i, vk):
        _key(vk, up=True)
        QtCore.QTimer.singleShot(90, lambda: self.send(i + 1))

    def commit(self):
        _log(f"before commit text={self.edit.text()!r}")
        _key(VK['space'])
        QtCore.QTimer.singleShot(45, self.rel_space)

    def rel_space(self):
        _key(VK['space'], up=True)
        QtCore.QTimer.singleShot(450, self.final)

    def final(self):
        _log(f"FINAL text={self.edit.text()!r}")
        self.dlg.reject()
        QtCore.QTimer.singleShot(300, QtWidgets.QApplication.instance().quit)


_INST = None


def run(parent):
    global _INST
    if not LOG_PATH:
        return
    from OAT.utils.ime import ensure_chinese_ime
    ok = ensure_chinese_ime()
    _log(f"ensure_chinese_ime -> {ok}")
    _INST = _P(parent)
    QtCore.QTimer.singleShot(1500, _INST.go)
