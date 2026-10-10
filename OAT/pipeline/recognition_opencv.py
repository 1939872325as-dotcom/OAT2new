import os
import random
import time
from typing import Optional, Tuple

import cv2
import numpy as np
import pyautogui
import win32gui
import win32con

from OAT.tools.GetDC import WindowCapture, effective_client_dy, warn_minimized_capture
from OAT.tools import settings
from OAT.tools.human_click import foreground_click, human_drag
from OAT.utils.OCRService import ocr_service
from OAT.utils.logging import logger
from .recognition import RecognitionEngine, RecognitionResult


def _normalize_threshold(threshold) -> float:
    """统一阈值到 0-1：兼容 0-100 百分比写法（与 GetDC.find_image_precise 一致）"""
    try:
        v = float(threshold)
    except (ValueError, TypeError):
        return settings.FIND_THRESHOLD / 100.0
    if v > 1.0:
        v = v / 100.0
    return min(1.0, max(0.0, v))


class OpenCVRecognitionEngine(RecognitionEngine):
    """基于 OpenCV 模板匹配 + RapidOCR 的识别引擎"""

    # 模板缓存：{路径: (mtime, 图片)}。缓存带文件修改时间校验，
    # 因为这是【类属性】—— 所有引擎实例共享，且进程存活期间一直存在。
    # 早前只按路径缓存、从不校验 mtime，导致「在程序运行中替换模板图后，
    # 仍然拿旧图去匹配」（表现为：换了图还是按旧模板的尺寸/内容识别失败）。
    _template_cache: dict = {}

    def __init__(
        self,
        hwnd: int,
        threshold: float = None,
        find_mode: str = None,
        synchronizer=None,
        hidden_window: bool = True,
        backend=None,
    ):
        self.hwnd = hwnd
        self.hidden_window = hidden_window
        self.threshold = _normalize_threshold(
            threshold if threshold is not None else settings.FIND_THRESHOLD
        )
        self.find_mode = find_mode if find_mode else settings.FIND_MODE
        self.synchronizer = synchronizer
        self.backend = backend
        if self.backend is None:
            try:
                if settings.EMULATOR_TYPE == "mumu12":
                    from OAT.tools.emulator.backend import create_backend
                    self.backend = create_backend(
                        "mumu12",
                        handle_spec=settings.HANDLE_SPEC,
                        mumu_folder=settings.resolve_mumu_folder(),
                    )
            except Exception:
                self.backend = None
        if self.backend is None:
            # hwnd 是 MuMu 句柄树根时自动挂模拟器后台（子窗口消息 + IPC），
            # PC 桌面版窗口不满足句柄树校验，自然回落旧链路
            self.backend = self._create_mumu_backend_for_hwnd(hwnd)

        # 窗口捕获器（隐藏窗口模式下使用）
        self.window_capture = None
        if hwnd:
            try:
                self.window_capture = WindowCapture(hwnd=hwnd)
            except Exception:
                pass

    def get_window_rect(self) -> Tuple[int, int, int, int]:
        rect = win32gui.GetClientRect(self.hwnd)
        return (rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1])

    @staticmethod
    def _create_mumu_backend_for_hwnd(hwnd):
        """hwnd 通过 MuMu 句柄树校验时创建 MumuBackend；普通窗口返回 None。"""
        if not hwnd:
            return None
        try:
            from OAT.tools.emulator.mumu_handle import build_handle
            from OAT.tools.emulator.backend import create_backend
            build_handle(int(hwnd), wait_tries=1)  # 非 MuMu 树会抛异常
            return create_backend(
                "mumu12",
                handle_spec=int(hwnd),
                mumu_folder=settings.resolve_mumu_folder(),
            )
        except Exception:
            return None

    def _get_title_bar_height(self) -> int:
        """计算标题栏高度（窗口高度 - 客户区高度）"""
        try:
            window_rect = win32gui.GetWindowRect(self.hwnd)
            client_rect = win32gui.GetClientRect(self.hwnd)
            window_h = window_rect[3] - window_rect[1]
            client_h = client_rect[3] - client_rect[1]
            return max(0, window_h - client_h)
        except Exception:
            return 0

    def _load_template(self, template_path: str) -> Optional[np.ndarray]:
        # 取文件修改时间；取不到（文件不存在等）则退化为不缓存
        try:
            mtime = os.path.getmtime(template_path)
        except OSError:
            mtime = None

        if mtime is not None:
            cached = self._template_cache.get(template_path)
            if cached is not None and cached[0] == mtime:
                return cached[1]

        target = cv2.imread(template_path)
        if target is not None and mtime is not None:
            self._template_cache[template_path] = (mtime, target)
            logger.info(f"已加载模板 {os.path.basename(template_path)}"
                        f"（{target.shape[1]}x{target.shape[0]}）")
        elif target is not None:
            # 拿不到 mtime：仍然返回，但不写入缓存，避免用到过期图
            logger.warning(f"无法读取模板修改时间，本次不缓存: {template_path}")
        return target

    def capture_screenshot(self) -> Optional[np.ndarray]:
        if self.hidden_window:
            if self.backend is not None:
                try:
                    img = self.backend.screenshot()
                except Exception:
                    img = None
                if img is not None:
                    return img
            if self.window_capture:
                return self.window_capture.capture_window()
            return None
        # 前台模式：截取窗口矩形区域
        try:
            if not self.hwnd or not win32gui.IsWindow(self.hwnd):
                return None
            # 桌面版客户端最小化后系统不再出图，截到的是无效区域；统一弹窗提示
            if win32gui.IsIconic(self.hwnd):
                warn_minimized_capture()
                return None
            left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
            w, h = right - left, bottom - top
            if w <= 0 or h <= 0:
                return None
            img = pyautogui.screenshot(region=(left, top, w, h))
            arr = np.array(img)
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        except Exception:
            return None

    def find_template(self, template_path: str, threshold: float = None, region: Tuple = None) -> RecognitionResult:
        if threshold is None:
            threshold = self.threshold
        else:
            threshold = _normalize_threshold(threshold)

        return self._find_template_hidden(template_path, threshold, region)

    def _find_template_hidden(self, template_path: str, threshold: float, region: Tuple = None) -> RecognitionResult:
        screenshot = self.capture_screenshot()
        if screenshot is None:
            return RecognitionResult(found=False)

        try:
            target = self._load_template(template_path)
            if target is None:
                return RecognitionResult(found=False)

            h, w = target.shape[:2]
            sh, sw = screenshot.shape[:2]
            _, _, cw, ch = self.get_window_rect()
            title_bar = effective_client_dy(sh, ch, self._get_title_bar_height())

            # region 为客户区坐标；截图含标题栏时顶部需要先偏移（纯客户区截图偏移为0）
            search = screenshot
            offset_x, offset_y = 0, 0
            if region:
                rx, ry, rw, rh = region
                sh, sw = screenshot.shape[:2]
                x1, y1 = int(rx), int(ry) + title_bar
                x2, y2 = min(int(rx + rw), sw), min(int(ry + rh) + title_bar, sh)
                if x2 <= x1 or y2 <= y1:
                    return RecognitionResult(found=False)
                search = screenshot[y1:y2, x1:x2]
                offset_x, offset_y = x1, y1

            result = cv2.matchTemplate(search, target, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

            if max_val >= threshold:
                x1, y1 = max_loc[0] + offset_x, max_loc[1] + offset_y
                y1 = max(0, y1 - title_bar)
                return RecognitionResult(
                    found=True,
                    region=(x1, y1, w, h),
                    confidence=float(max_val),
                )
            # 未达阈值：把「最高分 / 阈值 / 最佳位置」打出来。
            # 以前这里是无声失败，只能看到后面莫名走了 OCR。分数差一点 → 多半是
            # 窗口尺寸变了或阈值偏严；分数很低 → 模板图和当前界面不是同一处，要重截。
            logger.warning(
                f"模板未达阈值: {template_path} 最高分 {max_val:.3f} < 阈值 {threshold:.3f}"
                f"（最佳位置 x={max_loc[0] + offset_x} y={max(0, max_loc[1] + offset_y - title_bar)}，"
                f"模板 {w}x{h}，截图 {sh}x{sw}）"
            )
        except Exception as e:
            logger.error(f"模板匹配出错: {e}")

        return RecognitionResult(found=False)

    def find_text(self, target_text: str, confidence: float = 0.8,
                  region: Tuple = None) -> RecognitionResult:
        screenshot = self.capture_screenshot()
        if screenshot is None:
            return RecognitionResult(found=False)

        # region 是客户区坐标 [x1, x2, y1, y2]；截图可能含标题栏，需先换算成截图坐标
        shot_region = None
        if region and len(region) == 4:
            try:
                sh = screenshot.shape[0]
                _, _, _, ch = self.get_window_rect()
                title_bar = effective_client_dy(sh, ch, self._get_title_bar_height())
                rx1, rx2, ry1, ry2 = (int(v) for v in region)
                shot_region = [rx1, rx2, ry1 + title_bar, ry2 + title_bar]
                logger.info(f"OCR 限定搜索区域（客户区）[{rx1}, {rx2}, {ry1}, {ry2}]"
                            f" → 截图坐标 {shot_region}")
            except Exception as e:
                logger.warning(f"OCR 搜索区域换算失败，已忽略: {region} ({e})")
                shot_region = None

        # confidence 是本任务配置的 OCR 阈值，必须真的传下去。
        # 以前这里没传，ocr_confidence 形同虚设（一直用 OCR 侧默认值）。
        found, text_area, real_text = ocr_service.find_text(
            screenshot, target_text, confidence_threshold=confidence, region=shot_region)
        # OCR 侧算出的真实置信度（0~1）：不填的话日志里永远是“置信度: 0.00”
        ocr_conf = float(getattr(ocr_service, "last_match_confidence", 0.0))
        if found and text_area:
            sh = screenshot.shape[0]
            _, _, _, ch = self.get_window_rect()
            title_bar = effective_client_dy(sh, ch, self._get_title_bar_height())
            xs = [p[0] for p in text_area]
            ys = [p[1] - title_bar for p in text_area]
            # OCR 返回的是 float 坐标，统一转 int，避免下游 randint/位运算报错
            # ('float' object cannot be interpreted as an integer)
            x, y = int(min(xs)), int(max(0, min(ys)))
            w, h = int(max(xs) - min(xs)), int(max(ys) - min(ys))
            w, h = max(0, w), max(0, h)
            if not w or not h:
                # 文字框退化成一条线时 region 无效，点了也没意义：判未匹配并说明原因
                logger.warning(f"OCR 命中 '{real_text}' 但文字框无效"
                               f"（x={x} y={y} w={w} h={h}），按未匹配处理")
                return RecognitionResult(found=False)
            return RecognitionResult(
                found=True,
                region=(x, y, w, h),
                confidence=ocr_conf,
                text=real_text,
            )
        return RecognitionResult(found=False)

    def _click_postmessage(self, x: int, y: int) -> None:
        # 与 _swipe_postmessage 保持一致：句柄无效时 PostMessage 会静默失败，
        # 表现就是“日志说成功但实际没点”。这里必须检查并报错。
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            logger.error(f"点击失败：窗口句柄无效（hwnd={self.hwnd}），消息未发送。"
                         f"窗口可能已关闭，请重新窗口检测")
            return
        try:
            l_param = x | (y << 16)
            win32gui.PostMessage(self.hwnd, win32con.WM_MOUSEMOVE, 0, l_param)
            time.sleep(0.05)
            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, l_param)
            time.sleep(0.2)
            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, l_param)
        except Exception as e:
            logger.error(f"点击 ({x},{y}) 发送失败: {e}")

    def _swipe_postmessage(self, x1: int, y1: int, x2: int, y2: int, duration: float) -> None:
        try:
            if not self.hwnd or not win32gui.IsWindow(self.hwnd):
                return
            l_param1 = x1 | (y1 << 16)
            l_param2 = x2 | (y2 << 16)
            win32gui.PostMessage(self.hwnd, win32con.WM_MOUSEMOVE, 0, l_param1)
            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, l_param1)
            time.sleep(0.05)
            win32gui.PostMessage(self.hwnd, win32con.WM_MOUSEMOVE, 0, l_param2)
            time.sleep(min(duration, 5.0))
            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, l_param2)
        except Exception:
            return

    def click(self, x: int, y: int, sync_mode: bool = False) -> None:
        """在客户区坐标 (x, y) 处执行点击"""
        # 防御：上游可能传入 float（OCR 坐标），位运算要求 int
        x, y = int(x), int(y)
        if self.hidden_window:
            if sync_mode and self.synchronizer and self.synchronizer.sync_enabled:
                # 同步模式：向主窗口和所有副窗口发送点击消息
                main_hwnd = self.synchronizer.main_window[0] if self.synchronizer.main_window else self.hwnd
                self.synchronizer.send_click_message(hwnd=main_hwnd, relative_x=x, relative_y=y)
                for sub_hwnd, _ in self.synchronizer.get_sub_windows():
                    self.synchronizer.send_click_message(hwnd=sub_hwnd, relative_x=x, relative_y=y)
            else:
                if self.backend is not None:
                    try:
                        self.backend.click(x, y)
                    except Exception:
                        self._click_postmessage(x, y)
                else:
                    self._click_postmessage(x, y)
        else:
            # 前台模式：客户区坐标 → 屏幕坐标后移动鼠标真实点击
            if sync_mode:
                logger.warn("前台模式不支持多窗口同步点击，仅执行本窗口点击（同步需后台模式）")
            try:
                if not self.hwnd or not win32gui.IsWindow(self.hwnd):
                    return
                win_left, win_top, _, _ = win32gui.GetWindowRect(self.hwnd)
                title_bar = self._get_title_bar_height()
                screen_x = win_left + x
                screen_y = win_top + title_bar + y
                foreground_click(screen_x, screen_y)
            except Exception:
                return

        time.sleep(random.uniform(1.5, 3.0))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5, sync_mode: bool = False) -> None:
        """在客户区坐标 (x1, y1) → (x2, y2) 处执行滑动"""
        try:
            x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        except (ValueError, TypeError):
            return
        try:
            duration = float(duration)
        except (ValueError, TypeError):
            duration = 0.5
        duration = max(0.0, duration)
        if self.hidden_window:
            if sync_mode and self.synchronizer and self.synchronizer.sync_enabled:
                # 同步模式：向主窗口和所有副窗口发送滑动消息
                main_hwnd = self.synchronizer.main_window[0] if self.synchronizer.main_window else self.hwnd
                targets = [main_hwnd] + [h for h, _ in self.synchronizer.get_sub_windows()]
                for hwnd in targets:
                    try:
                        if not hwnd or not win32gui.IsWindow(hwnd):
                            continue
                        self.synchronizer.send_mouse_move(hwnd, x1, y1)
                        self.synchronizer.send_mouse_down(hwnd, x1, y1)
                        time.sleep(0.05)
                        self.synchronizer.send_mouse_move(hwnd, x2, y2)
                        time.sleep(min(duration, 5.0))
                        self.synchronizer.send_mouse_up(hwnd, x2, y2)
                    except Exception:
                        continue
            else:
                if self.backend is not None:
                    try:
                        self.backend.swipe(x1, y1, x2, y2, duration)
                    except Exception:
                        self._swipe_postmessage(x1, y1, x2, y2, duration)
                else:
                    self._swipe_postmessage(x1, y1, x2, y2, duration)
        else:
            # 前台模式：客户区坐标 → 屏幕坐标后真实拖拽
            if sync_mode:
                logger.warn("前台模式不支持多窗口同步滑动，仅执行本窗口滑动（同步需后台模式）")
            try:
                if not self.hwnd or not win32gui.IsWindow(self.hwnd):
                    return
                win_left, win_top, _, _ = win32gui.GetWindowRect(self.hwnd)
                title_bar = self._get_title_bar_height()
                human_drag(win_left + x1, win_top + title_bar + y1,
                           win_left + x2, win_top + title_bar + y2,
                           duration=duration)
            except Exception:
                return

        time.sleep(random.uniform(1.5, 3.0))
