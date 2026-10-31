"""模板+OCR 复用同一帧截图的回归测试。

背景：一个任务在 template+ocr 模式下原本会截两次图（模板一次、OCR 一次），
PrintWindow 每次约 0.7s；两个任务一轮就多花 2~3 秒。现在同一任务复用一帧。
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from OAT.pipeline.recognition import RecognitionResult


class TestEngineAcceptsScreenshot(unittest.TestCase):
    def _bare_engine(self):
        from OAT.pipeline.recognition_opencv import OpenCVRecognitionEngine
        eng = object.__new__(OpenCVRecognitionEngine)
        eng.hwnd = 0
        eng.threshold = 0.8
        eng.hidden_window = False
        eng.backend = None
        eng.window_capture = None
        eng.get_window_rect = lambda: (0, 0, 400, 300)
        eng._get_title_bar_height = lambda: 0
        eng.capture_screenshot = lambda: (_ for _ in ()).throw(
            AssertionError("传入了 screenshot 就不该再抓屏"))
        return eng

    def test_find_template_uses_provided_screenshot(self):
        rng = np.random.RandomState(0)
        tpl = rng.randint(0, 255, (40, 60, 3), dtype=np.uint8)
        shot = np.zeros((300, 400, 3), dtype=np.uint8)
        shot[100:140, 150:210] = tpl

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.png")
            cv2.imwrite(path, tpl)
            eng = self._bare_engine()
            res = eng.find_template(path, 0.9, screenshot=shot)
        self.assertTrue(res.found)
        self.assertEqual(res.region, (150, 100, 60, 40))

    def test_find_text_uses_provided_screenshot(self):
        shot = np.zeros((300, 400, 3), dtype=np.uint8)
        eng = self._bare_engine()
        with patch("OAT.pipeline.recognition_opencv.ocr_service.find_text",
                   return_value=(True, [(10, 10), (60, 10), (60, 30), (10, 30)],
                                 "挑战")) as m:
            res = eng.find_text("挑战", 0.8, region=[10, 60, 10, 30], screenshot=shot)
        self.assertTrue(res.found)
        # 第一参数必须是传入的那张图（不是重新抓的）
        self.assertIs(m.call_args[0][0], shot)


class TestRunnerReusesFrame(unittest.TestCase):
    def test_template_plus_ocr_captures_once(self):
        from OAT.pipeline.runner import PipelineRunner

        class FakeEngine:
            def __init__(self):
                self.captures = 0
                self.template_shot = None
                self.text_shot = None

            def capture_screenshot(self):
                self.captures += 1
                return np.zeros((20, 20, 3), dtype=np.uint8)

            def find_template(self, path, threshold=None, region=None, screenshot=None):
                if screenshot is None:      # 与真实引擎一致：没传才自己抓
                    self.capture_screenshot()
                self.template_shot = screenshot
                return RecognitionResult(found=False)

            def find_text(self, text, confidence=0.8, region=None, screenshot=None):
                if screenshot is None:
                    self.capture_screenshot()
                self.text_shot = screenshot
                return RecognitionResult(found=True, region=(1, 2, 3, 4),
                                         confidence=0.99, text=text)

        fake = FakeEngine()
        runner = object.__new__(PipelineRunner)
        runner.engine = fake
        runner.script_dir = "."
        task = SimpleNamespace(
            name="t", template="t.png", recognition="template+ocr",
            ocr_text="挑战", ocr_confidence=0.8, ocr_region=None, threshold=None,
        )
        res = runner._recognize(task)

        self.assertTrue(res.found)
        self.assertEqual(fake.captures, 1)          # 只抓一帧
        self.assertIs(fake.template_shot, fake.text_shot)  # 两次识别共用同一帧

    def test_template_only_still_captures_once(self):
        from OAT.pipeline.runner import PipelineRunner

        class FakeEngine:
            def __init__(self):
                self.captures = 0

            def capture_screenshot(self):
                self.captures += 1
                return np.zeros((20, 20, 3), dtype=np.uint8)

            def find_template(self, path, threshold=None, region=None, screenshot=None):
                if screenshot is None:
                    self.capture_screenshot()
                return RecognitionResult(found=False)

        fake = FakeEngine()
        runner = object.__new__(PipelineRunner)
        runner.engine = fake
        runner.script_dir = "."
        task = SimpleNamespace(name="t", template="t.png", recognition="template",
                               threshold=None)
        runner._recognize(task)
        self.assertEqual(fake.captures, 1)


if __name__ == "__main__":
    unittest.main()
