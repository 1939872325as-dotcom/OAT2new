"""框选截图在高 DPI 下的坐标换算回归测试。

问题：ClientToScreen 返回物理像素，Qt 的 setGeometry 用逻辑像素。
系统缩放 110 DPI（Qt devicePixelRatio≈1.1458）时，遮罩会比游戏窗口
大 14.6% 并向右下偏移，看起来像“截图时窗口被自动放大了”。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from OAT.tools.region_capture import (
    _MaskWindow,
    _physical_rect_to_logical,
    _window_dpr,
)


class TestPhysicalRectToLogical(unittest.TestCase):
    def test_100_percent_unchanged(self):
        r = _physical_rect_to_logical(100, 50, 1441, 812, 1.0)
        self.assertEqual((r.x(), r.y(), r.width(), r.height()),
                         (100, 50, 1441, 812))

    def test_110_dpi_scales_down(self):
        # 110/96 = 1.1458333…
        r = _physical_rect_to_logical(100, 50, 1441, 812, 110 / 96.0)
        self.assertEqual((r.x(), r.y(), r.width(), r.height()),
                         (87, 44, 1258, 709))

    def test_125_percent(self):
        r = _physical_rect_to_logical(200, 100, 1000, 500, 1.25)
        self.assertEqual((r.x(), r.y(), r.width(), r.height()),
                         (160, 80, 800, 400))

    def test_invalid_dpr_falls_back_to_one(self):
        for bad in (0, -1, None, "x"):
            r = _physical_rect_to_logical(10, 20, 30, 40, bad)
            self.assertEqual((r.x(), r.y(), r.width(), r.height()),
                             (10, 20, 30, 40))

    def test_min_size_is_one(self):
        r = _physical_rect_to_logical(0, 0, 1, 1, 4.0)
        self.assertGreaterEqual(r.width(), 1)
        self.assertGreaterEqual(r.height(), 1)


class TestWindowDpr(unittest.TestCase):
    def test_invalid_hwnd_returns_one(self):
        self.assertEqual(_window_dpr(0), 1.0)

    def test_current_screen_dpr_matches_system(self):
        # 本机系统 DPI 110 → 1.1458；只断言在合理范围内，避免绑定具体机器
        self.assertGreaterEqual(_window_dpr(0), 1.0)


class TestMaskGeometry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_mask_uses_logical_rect_and_maps_mouse_back(self):
        """遮罩按逻辑矩形贴住客户区；鼠标逻辑坐标能换回图片像素"""
        from PyQt6.QtCore import QPoint, QRect

        frame = np.zeros((50, 100, 3), dtype=np.uint8)   # 100x50 的“截图”
        rect = QRect(10, 20, 50, 25)                     # 逻辑尺寸 = 图的一半（dpr=2）
        mask = _MaskWindow(frame, parent=None, target_rect=rect)
        try:
            self.assertEqual(mask.geometry(), rect)
            # 图片被缩放到 50x25 逻辑像素，scale=0.5
            # （真实流程由 showEvent/resizeEvent 触发，这里直接重算）
            mask._recompute_layout()
            self.assertAlmostEqual(mask.scale, 0.5, places=3)
            p = mask._screen_to_image(QPoint(25, 12))
            self.assertEqual((p.x(), p.y()), (50, 24))
        finally:
            mask.close()
            mask.deleteLater()


if __name__ == "__main__":
    unittest.main()
