import os
import random
import threading
import time
import traceback

import cv2
import numpy as np
import pyautogui
import win32con
import win32gui
from PIL import Image

from .WindowSynchronizer import WindowSynchronizer
from .GetDC import WindowCapture, effective_client_dy, warn_minimized_capture
from . import human_click
# 导入整个settings模块，而不是单个变量
from . import settings
from OAT.utils.warning_box import warning_box
from OAT.utils.error_handler import log_error
from OAT.utils.OCRService import ocr_service
from ..utils.logging import logger
from OAT.utils.pause_state import pause_aware_sleep


# 点击后随机等待的默认区间（秒）。每张图可在「编辑图像配置 → 点击区域设置 →
# 点击后随机等待(秒)」里单独覆盖；这里是不单独配置时的回落值，与界面默认一致。
DEFAULT_CLICK_WAIT_RANGE = (0.8, 5.0)


def resolve_click_wait_range(click_wait_min, click_wait_max):
    """把配置里的等待区间解析成 (lo, hi)，规则与管道模式的解析保持完全一致：

    · 两个都没填（或都非法 / 为负）→ 默认 0.8~5；
    · 只填一个 → 另一个按默认补（只填下限 2 → 2~5）；
    · 写反了自动交换（8, 1 → 1~8）；
    · 负数 / 非数字按「没填」处理。

    两条路径对同一份配置必须得到同一个区间，否则「按图配置」在不同模式下
    表现会不一致（管道模式 2~5、通用挑战模式却是 0.8~5）。
    """
    def _norm(raw):
        try:
            v = float(raw)
        except (ValueError, TypeError):
            return None
        return v if v >= 0 else None

    lo = _norm(click_wait_min)
    hi = _norm(click_wait_max)
    if lo is None and hi is None:
        lo, hi = DEFAULT_CLICK_WAIT_RANGE
    else:
        if lo is None:
            lo = DEFAULT_CLICK_WAIT_RANGE[0]
        if hi is None:
            hi = DEFAULT_CLICK_WAIT_RANGE[1]
    if hi < lo:
        lo, hi = hi, lo
    return float(lo), float(hi)


class OnmyojiAutomation:
    def __init__(self, window_title: str, synchronizer=None, sync_mode: str = "exactly_sync", find_mode=None, find_threshold=None, hwnd: int = None):
        self.window_title = window_title
        # 窗口信息获取与初始化（显式句柄优先：改名/多开场景下比标题可靠）
        self.hwnd = 0
        if hwnd:
            try:
                if win32gui.IsWindow(int(hwnd)):
                    self.hwnd = int(hwnd)
            except Exception:
                self.hwnd = 0
        if not self.hwnd:
            self.hwnd = win32gui.FindWindow(None, window_title)
        if not self.hwnd:
            logger.error(f"无法找到窗口 {window_title}")
            # 设置默认窗口信息
            self.area = (0, 0, 1920, 1080)  # 默认屏幕尺寸
            self.x1, self.y1, self.width, self.height = self.area
            self.x2, self.y2 = self.x1 + self.width, self.y1 + self.height
            # 设置同步器为None
            if synchronizer is None:
                self.synchronizer = None
            else:
                self.synchronizer = synchronizer
            # 跳过后续窗口初始化
            self._init_common_params(find_mode, find_threshold)
            return

        # 窗口同步器 - 如果没有提供则创建新实例
        if synchronizer is None:
            self.synchronizer = WindowSynchronizer(sync_mode=sync_mode)
        else:
            self.synchronizer = synchronizer
            # 如果提供了同步器，设置其同步模式
            if hasattr(self.synchronizer, 'set_sync_mode'):
                self.synchronizer.set_sync_mode(sync_mode)

        self.area = self.get_window_rect()
        self.x1, self.y1, self.width, self.height = self.area
        self.x2, self.y2 = self.x1 + self.width, self.y1 + self.height

        # 初始化公共参数
        self._init_common_params(find_mode, find_threshold)
    
    def _init_common_params(self, find_mode=None, find_threshold=None):
        """初始化公共参数"""
        # 线程与控制变量
        self.lock = threading.Lock()
        self.shutdown_flag = False

        # 图像识别缓存和参数
        self.recognition_cache = {}
        self.cache_timeout = 1.0
        # 设置识别模式和阈值
        # 优先使用传入的参数，其次使用settings.py中的配置
        self.find_mode = find_mode if find_mode else settings.FIND_MODE
        # 获取阈值并转换为0-1之间的值
        threshold_value = find_threshold if find_threshold is not None else settings.FIND_THRESHOLD
        self.default_confidence = threshold_value / 100.0  # 转换为0-1之间的值
        self.image_templates = {}

        # 创建WindowCapture实例，用于隐藏窗口模式
        self.window_capture = None
        if hasattr(self, 'hwnd') and self.hwnd:
            try:
                self.window_capture = WindowCapture(hwnd=self.hwnd)
            except Exception:
                pass

        # MuMu 后台 backend（单实例；截图沿用 WindowCapture，输入经 backend 路由）
        self.backend = None
        try:
            if settings.EMULATOR_TYPE == "mumu12" and getattr(self, "hwnd", 0):
                from OAT.tools.emulator.backend import create_backend
                self.backend = create_backend(
                    "mumu12",
                    handle_spec=settings.HANDLE_SPEC,
                    mumu_folder=settings.resolve_mumu_folder(),
                )
        except Exception:
            self.backend = None
        if self.backend is None and getattr(self, "hwnd", 0):
            # hwnd 是 MuMu 句柄树根时自动挂模拟器后台（子窗口消息 + IPC），
            # PC 桌面版窗口不满足句柄树校验，自然回落旧链路
            try:
                from OAT.tools.emulator.mumu_handle import build_handle
                from OAT.tools.emulator.backend import create_backend
                build_handle(int(self.hwnd), wait_tries=1)
                self.backend = create_backend(
                    "mumu12",
                    handle_spec=int(self.hwnd),
                    mumu_folder=settings.resolve_mumu_folder(),
                )
            except Exception:
                self.backend = None
        if self.backend is not None and self.synchronizer is not None:
            try:
                self.synchronizer.backend = self.backend
            except Exception:
                pass

        # 窗口矩形缓存
        self._window_rect_cache = None
        self._window_rect_cache_ts = 0.0

    def _get_cached_window_rect(self):
        """获取窗口矩形（带1秒TTL缓存）"""
        now = time.time()
        if self._window_rect_cache is not None and now - self._window_rect_cache_ts < 1.0:
            return self._window_rect_cache
        rect = win32gui.GetWindowRect(self.hwnd)
        client = win32gui.GetClientRect(self.hwnd)
        self._window_rect_cache = (rect, client)
        self._window_rect_cache_ts = now
        return self._window_rect_cache

    def is_window_present(self) -> bool:
        """检查窗口是否存在且有效"""
        if self.hwnd == 0:
            return False
        
        # 尝试获取窗口矩形，进一步验证窗口是否有效
        try:
            rect = win32gui.GetWindowRect(self.hwnd)
            # 检查窗口是否有有效的尺寸
            x1, y1, x2, y2 = rect
            if x1 == 0 and y1 == 0 and x2 == 0 and y2 == 0:
                return False
            if x2 <= x1 or y2 <= y1:
                return False
            return True
        except Exception:
            # 如果获取窗口矩形失败，说明窗口无效
            return False

    def print_window_info(self) -> None:
        """输出窗口信息"""
        logger.info('已获取到游戏窗口信息\n'
              f'窗口左上角的位置是({self.x1},{self.y1})\n'
              f'窗口右下角的位置是({self.x2},{self.y2})\n')

    def get_window_rect(self) -> tuple:
        """获取窗口矩形区域"""
        rect = win32gui.GetWindowRect(self.hwnd)
        x1, y1, x2, y2 = rect
        return x1, y1, x2 - x1, y2 - y1

    def preload_image(self, logo_path: str) -> bool:
        """预加载并缓存图像模板"""
        if logo_path not in self.image_templates:
            if not os.path.exists(logo_path):
                logger.warn(f"图像文件不存在，跳过预加载: {logo_path}")
                return False
            try:
                # 读取图像并进行预处理
                image = Image.open(logo_path)
                # 转换为RGB模式（如果不是）
                if image.mode != 'RGB':
                    image = image.convert('RGB')
                self.image_templates[logo_path] = image
            except Exception as e:
                logger.warn(f"警告：预加载图像 {logo_path} 失败：{str(e)}")
                return False
        return True

    def find_img(self, logo: str, use_cache=True) -> bool:
        """图像识别 + 缓存机制"""
        current_time = time.time()

        # 检查缓存
        if use_cache and logo in self.recognition_cache:
            cached_result, cache_time = self.recognition_cache[logo]
            if current_time - cache_time < self.cache_timeout:
                if cached_result:
                    x, y, width, height = cached_result
                    self.target_x = random.randint(x, x + width)
                    self.target_y = random.randint(y, y + height)
                return cached_result is not None

        # 预加载图像
        self.preload_image(logo)

        # 执行图像识别
        target = None
        try:
            # 使用预加载的图像模板而不是文件路径
            if logo in self.image_templates:
                target = pyautogui.locateOnScreen(
                    self.image_templates[logo],
                    confidence=self.default_confidence,
                    region=self.area
                )
            else:
                # 降级到文件路径方式
                target = pyautogui.locateOnScreen(
                    logo,
                    confidence=self.default_confidence,
                    region=self.area
                )
        except pyautogui.ImageNotFoundException:
            # 未找到图像时设置target为None
            target = None
        except OSError:
            # 处理文件读取错误
            pass
        except Exception:
            # 处理其他未预期的错误
            traceback.print_exc()

        # 更新缓存
        self.recognition_cache[logo] = (target, current_time)

        if target:
            x, y, width, height = target
            self.target_x = random.randint(x, x + width)
            self.target_y = random.randint(y, y + height)
            return True
        
        return False
    
    def find_text_ocr(self, target_text: str, region: list = None,
                      confidence_threshold: float = None) -> tuple:
        """
        使用OCR识别文字（异步执行）
        
        参数:
            target_text: 要识别的目标文字
            region: 可选搜索区域，客户区坐标 [x1, x2, y1, y2]（与 click_area 同语义）。
                    同一画面出现多个相同文字时，用它排除不需要的那一个
            confidence_threshold: 可选，OCR 置信度阈值；None 时用 OCR 侧默认值
            
        返回:
            tuple: (是否找到, 文字区域坐标, 文字内容)
        """
        if confidence_threshold is None:
            confidence_threshold = 0.5
        import queue
        
        # 创建队列用于接收OCR结果
        result_queue = queue.Queue()
        
        def ocr_task():
            """OCR识别任务，在单独线程中执行"""
            start_time = time.time()
            
            try:
                # 获取窗口截图（backend 优先：与 backend.click 同一坐标系）
                screenshot = None
                if self.backend is not None:
                    try:
                        screenshot = self.backend.screenshot()
                    except Exception:
                        screenshot = None
                if screenshot is None and self.window_capture:
                    # 使用隐藏窗口捕获
                    screenshot = self.window_capture.capture_window()
                elif screenshot is None:
                    # 客户端最小化时屏幕截图同样截不到内容，统一提示后按未识别处理
                    if self.hwnd and win32gui.IsIconic(self.hwnd):
                        warn_minimized_capture()
                        result_queue.put((False, None, None))
                        return
                    # 使用pyautogui截图
                    screenshot = pyautogui.screenshot(region=self.area)
                    # 转换为OpenCV格式
                    screenshot = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
                
                if screenshot is None:
                    result_queue.put((False, None, None))
                    return

                # 截图坐标 → 客户区坐标的偏移量。必须在 OCR 之前算好：
                # ocr_region 用客户区坐标描述，要先换算成截图坐标才能裁剪。
                # （截图含标题栏时偏移为标题栏高度；纯客户区截图偏移为 0）
                dy = 0
                if self.hwnd:
                    try:
                        window_rect, client_rect = self._get_cached_window_rect()
                        window_height = window_rect[3] - window_rect[1]
                        client_height = client_rect[3] - client_rect[1]
                        dy = effective_client_dy(screenshot.shape[0], client_height,
                                                 window_height - client_height)
                    except Exception:
                        dy = 0

                # ocr_region 是客户区坐标，换算到截图坐标后交给 OCR 裁剪
                shot_region = None
                if region and len(region) == 4:
                    try:
                        rx1, rx2, ry1, ry2 = [int(v) for v in region]
                        shot_region = [rx1, rx2, ry1 + dy, ry2 + dy]
                    except Exception:
                        shot_region = None

                # 使用OCR服务识别文字
                found, text_area, real_text = ocr_service.find_text(
                    screenshot, target_text,
                    confidence_threshold=confidence_threshold, region=shot_region)
                
                # 记录识别时间（仅在找到文字时打印）
                elapsed_time = time.time() - start_time
                if found:
                    logger.info(f"OCR识别成功: '{target_text}'，耗时: {elapsed_time:.3f}秒")
                    
                    # 将截图坐标转换为客户区坐标（偏移量上面已算好，复用同一个 dy）
                    if self.hwnd and text_area:
                        try:
                            text_area = [[x, y - dy] for x, y in text_area]
                        except Exception:
                            # 如果转换失败，使用原始坐标
                            pass
                
                result_queue.put((found, text_area, real_text))
            except pyautogui.FailSafeException:
                logger.error("OCR识别失败: 触发了PyAutoGUI安全模式")
                result_queue.put((False, None, None))
            except Exception as e:
                logger.error(f"OCR识别发生错误: {str(e)}")
                result_queue.put((False, None, None))
        
        # 创建并启动线程
        ocr_thread = threading.Thread(target=ocr_task, daemon=True)
        ocr_thread.start()
        
        # 等待结果，设置超时时间避免无限等待
        try:
            return result_queue.get(timeout=15.0)  # 15秒超时，适应OCR识别耗时
        except queue.Empty:
            logger.error("OCR识别超时")
            return False, None, None

    def _win32_double_click(self) -> None:
        """优化的双击操作，减少延迟（委托共享实现）"""
        human_click.win32_double_click()

    def send_click_message(self, relative_x: int, relative_y: int) -> None:
        """
        向指定窗口发送点击消息
        :param relative_x: 窗口内相对X坐标
        :param relative_y: 窗口内相对Y坐标
        """
        # 将相对坐标转换为LPARAM格式
        l_param = relative_x | (relative_y << 16)
        
        # 发送鼠标移动过去的信息
        win32gui.PostMessage(self.hwnd, win32con.WM_MOUSEMOVE, 0, l_param)
        
        # 添加短暂延时确保鼠标移动到位
        time.sleep(0.05)
        
        # 发送鼠标左键按下消息
        win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, l_param)
        
        # 增加鼠标按下时长，确保点击被正确识别
        time.sleep(0.2)
        
        # 发送鼠标左键释放消息
        win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, l_param)


    @staticmethod
    def _should_skip_ocr_for_custom_area(ocr_enabled, ocr_text, ocr_action, click_type, click_area) -> bool:
        """ocr_action 为自定义点击区域时跳过 OCR 直达坐标分支的门控。"""
        if not (ocr_enabled and (ocr_text or "").strip()):
            return False
        if ocr_action != "点击点击区域设置的区域":
            return False
        if click_type != "coordinate":
            return False
        if not isinstance(click_area, (list, tuple)) or len(click_area) != 4:
            return False
        try:
            x1, x2, y1, y2 = (int(v) for v in click_area)
        except (ValueError, TypeError):
            return False
        return x1 <= x2 and y1 <= y2

    def perform_action(self,
                       logo: str,
                       hidden_window: bool = False,
                       threshold: float = None,
                       sync_mode: bool = False,
                       click_type: str = "image",
                       click_area: list = None,
                       ocr_enabled: bool = False,
                       ocr_target_text: str = "",
                       ocr_action: str = "",
                       ocr_region: list = None,
                       ocr_confidence_threshold: float = None,
                       click_wait_min: float = None,
                       click_wait_max: float = None,
                       # sync_type: str = "完全同步"
                       ) -> bool:
        """
        执行操作：根据是否隐藏窗口选择不同的执行模式
        :param logo: 要识别的图像路径
        :param hidden_window: 是否使用隐藏窗口模式
        :param threshold: 识别阈值，默认使用设置中的值
        :param sync_mode: 是否同步执行
        :param click_type: 点击方式：image（图片区域）、coordinate（指定坐标）
        :param click_area: 当click_type为coordinate时的点击区域 [x1, x2, y1, y2]
        :param ocr_enabled: 是否启用OCR识别
        :param ocr_target_text: OCR目标文字
        :param ocr_region: OCR搜索区域，客户区坐标 [x1, x2, y1, y2]；用于排除同名的其他文字
        :param ocr_confidence_threshold: OCR置信度阈值
        :param click_wait_min: 点击后随机等待区间下限（秒），来自该图的配置；None 表示用默认 0.8
        :param click_wait_max: 点击后随机等待区间上限（秒），来自该图的配置；None 表示用默认 5.0
        :param sync_type: 同步类型，可选值："完全同步"、"点击同步"
        :return: 是否成功执行操作
        """
        try:
            # 使用设置中的阈值作为默认值
            if threshold is None:
                threshold = self.default_confidence
                
            # 如果启用了OCR，先尝试OCR识别（自定义点击区域时跳过，直达坐标分支）
            if ocr_enabled and ocr_target_text and not self._should_skip_ocr_for_custom_area(
                    ocr_enabled, ocr_target_text, ocr_action, click_type, click_area):
                found, text_area, real_text = self.find_text_ocr(
                    ocr_target_text, region=ocr_region,
                    confidence_threshold=ocr_confidence_threshold)
                if found and text_area:
                    # 在文字区域内随机点击
                    x, y = ocr_service.get_random_point_in_area(text_area)
                    if hidden_window:
                        # 隐藏窗口模式下使用相对坐标
                        self._send_click_messages(int(x), int(y), sync_mode,
                                                  click_wait_min, click_wait_max,
                                                  label=os.path.basename(logo))
                    else:
                        # 普通模式下转换为绝对坐标
                        absolute_x = self.x1 + int(x)
                        absolute_y = self.y1 + int(y)
                        with self.lock:
                            self._complex_move(target_x=absolute_x, target_y=absolute_y)
                            self._win32_double_click()
                            if sync_mode and self.synchronizer.sync_enabled:
                                self.synchronizer.sync_controller()
                            self._sleep_after_click(click_wait_min, click_wait_max,
                                                    label=os.path.basename(logo))
                    return True
                # 如果OCR未识别到文字，不输出错误信息，直接尝试图像识别
            
            # 如果OCR未启用或识别失败，使用常规图像识别
            if hidden_window:
                return self._perform_action_hidden_window(logo, threshold, sync_mode, click_type, click_area,
                                                          click_wait_min, click_wait_max)
            else:
                return self._perform_action_normal(logo, threshold, sync_mode, click_type, click_area,
                                                   click_wait_min, click_wait_max)
        except pyautogui.FailSafeException:
            error_msg = "警告：触发了PyAutoGUI的安全模式，操作已停止"
            # 使用warning_box显示错误信息
            warning_box(error_msg)
            # 写入日志文件
            log_error(error_msg)
            return False
        except Exception as e:
            error_msg = f"警告：执行操作时发生错误：{str(e)}"
            # 使用warning_box显示错误信息
            warning_box(error_msg)
            # 写入日志文件
            log_error(error_msg)
            return False

    def _perform_action_hidden_window(self, logo: str, threshold: float, sync_mode: bool, click_type: str = "image", click_area: list = None,
                                      click_wait_min: float = None, click_wait_max: float = None) -> bool:
        """使用隐藏窗口捕获模式执行操作"""
        try:
            # 使用初始化时创建的WindowCapture实例
            if not self.window_capture:
                self.window_capture = WindowCapture(hwnd=self.hwnd)
            
            wc = self.window_capture
            
            # 准备目标图像：优先使用预加载的图像
            target_image = logo
            if logo in self.image_templates:
                # 将PIL图像转换为OpenCV格式（NumPy数组）
                pil_image = self.image_templates[logo]
                # 转换为RGB模式
                if pil_image.mode != 'RGB':
                    pil_image = pil_image.convert('RGB')
                # 转换为NumPy数组并调整通道顺序（PIL是RGB，OpenCV是BGR）
                target_image = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
            
            # 使用设置的识别模式和阈值
            # backend 可用时用同一后端截图（与 backend.click 同一坐标系，IPC 更快）
            using_backend = self.backend is not None
            if using_backend:
                backend_img = self.backend.screenshot()
                if backend_img is None:
                    return False
                position = wc.find_image_in(backend_img, target_image, threshold=threshold, method=self.find_mode)
            else:
                position = wc.find_image_precise(target_image, threshold=threshold, method=self.find_mode)
            if position:
                # 确定点击坐标
                if click_type == "coordinate" and click_area:
                    # 使用指定区域生成随机坐标
                    x1, x2, y1, y2 = click_area
                    relative_x = random.randint(x1, x2)
                    relative_y = random.randint(y1, y2)
                else:
                    # 从区域范围中计算中心点坐标
                    (x1, x2), (y1, y2) = position
                    
                    # 计算区域中心
                    center_x = (x1 + x2) // 2
                    center_y = (y1 + y2) // 2

                    # 截图坐标 → 客户区坐标（含标题栏截图才减标题栏，纯客户区截图偏移为0）
                    dy = 0
                    if not using_backend:
                        try:
                            # 获取窗口信息（使用缓存）
                            window_rect, client_rect = self._get_cached_window_rect()

                            # 计算标题栏高度（窗口高度 - 客户区高度）
                            window_height = window_rect[3] - window_rect[1]
                            client_height = client_rect[3] - client_rect[1]
                            title_bar_height = window_height - client_height
                            shot_h = wc.last_shot_shape[0] if wc.last_shot_shape else client_height
                            dy = effective_client_dy(shot_h, client_height, title_bar_height)
                        except Exception:
                            # 如果转换失败，使用原始坐标
                            dy = 0
                    center_y = max(0, center_y - dy)
                    y1 = max(0, y1 - dy)
                    y2 = max(0, y2 - dy)
                    
                    # 计算区域的1/3大小作为随机范围，使点击更靠近中心
                    range_x = (x2 - x1) // 3
                    range_y = (y2 - y1) // 3
                    # 在中心附近生成随机坐标
                    relative_x = random.randint(max(x1, center_x - range_x), min(x2, center_x + range_x))
                    relative_y = random.randint(max(y1, center_y - range_y), min(y2, center_y + range_y))

                self._send_click_messages(relative_x, relative_y, sync_mode,
                                          click_wait_min, click_wait_max,
                                          label=os.path.basename(logo))
                return True
            else:
                return False
        except Exception as e:
            logger.error(f"隐藏窗口捕获发生错误: {str(e)}")
            # 降级到常规模式
            return self._perform_action_normal(logo, threshold, sync_mode, click_type, click_area,
                                               click_wait_min, click_wait_max)

    def _perform_action_normal(self, logo: str, threshold: float, sync_mode: bool, click_type: str = "image", click_area: list = None,
                               click_wait_min: float = None, click_wait_max: float = None) -> bool:
        """使用常规模式执行操作"""
        # 桌面版客户端最小化后系统不再出图，前台识别必然落空；统一弹窗提示（30s 节流）
        if self.hwnd and win32gui.IsIconic(self.hwnd):
            warn_minimized_capture()
            return False
        found = self.find_img(logo)
        if not found:
            return False

        with self.lock:  # 只在执行关键操作时持有锁
            # 确定点击坐标
            if click_type == "coordinate" and click_area:
                # 使用指定区域生成随机坐标
                x1, x2, y1, y2 = click_area
                # 转换为屏幕绝对坐标
                target_x = self.x1 + random.randint(x1, x2)
                target_y = self.y1 + random.randint(y1, y2)
            else:
                # 重新计算更靠近中心的随机坐标
                # 从find_img方法中，target_x和target_y是在区域内随机生成的
                if logo in self.recognition_cache:
                    cached_result, _ = self.recognition_cache[logo]
                    if cached_result:
                        x, y, width, height = cached_result
                        # 计算区域中心
                        center_x = x + width // 2
                        center_y = y + height // 2
                        # 计算区域的1/3大小作为随机范围，使点击更靠近中心
                        range_x = width // 3
                        range_y = height // 3
                        # 在中心附近生成随机坐标
                        target_x = random.randint(max(x, center_x - range_x), min(x + width, center_x + range_x))
                        target_y = random.randint(max(y, center_y - range_y), min(y + height, center_y + range_y))
                    else:
                        # 如果缓存中没有结果，使用原来的坐标
                        target_x, target_y = self.target_x, self.target_y
                else:
                    # 如果缓存中没有结果，使用原来的坐标
                    target_x, target_y = self.target_x, self.target_y

            self._complex_move(target_x=target_x, target_y=target_y)
            self._win32_double_click()
            
            # 如果启用同步模式，根据同步类型执行相应操作
            if sync_mode and self.synchronizer.sync_enabled:
                self.synchronizer.sync_controller()
            
            self._sleep_after_click(click_wait_min, click_wait_max, label=os.path.basename(logo))
            return True

    def _send_click_messages(self, relative_x: int, relative_y: int, sync_mode: bool,
                             click_wait_min: float = None, click_wait_max: float = None,
                             label: str = None) -> None:
        """发送点击消息，根据同步模式决定是否同步到多个窗口
        :param relative_x, relative_y: 相对坐标
        :param sync_mode: 是否启用同步
        :param click_wait_min: 点击后随机等待区间下限（秒），来自该图的配置
        :param click_wait_max: 点击后随机等待区间上限（秒），来自该图的配置
        :param label: 日志里显示的图名，便于核对是哪张图的配置生效
        :param sync_type: 同步类型，可选值："完全同步"、"点击同步"
        """
        if sync_mode and self.synchronizer.sync_enabled:
            # 确保使用主窗口句柄
            if self.synchronizer.main_window and self.hwnd != self.synchronizer.main_window[0]:
                self.hwnd = self.synchronizer.main_window[0]

            # 给主窗口发送点击消息
            self.synchronizer.send_click_message(hwnd=self.hwnd, relative_x=relative_x, relative_y=relative_y)

            # 给所有副窗口发送点击消息
            for sub_hwnd, _ in self.synchronizer.get_sub_windows():
                self.synchronizer.send_click_message(hwnd=sub_hwnd, relative_x=relative_x, relative_y=relative_y)

        elif sync_mode and not self.synchronizer.sync_enabled:
            # 同步模式已被禁用，只给当前窗口发送点击消息
            self.synchronizer.send_click_message(hwnd=self.hwnd, relative_x=relative_x, relative_y=relative_y)
        else:
            # 非同步模式，使用普通点击方法
            if getattr(self, "backend", None) is not None:
                try:
                    self.backend.click(relative_x, relative_y)
                except Exception:
                    self.send_click_message(relative_x, relative_y)
            else:
                self.send_click_message(relative_x, relative_y)

        # 等待点击操作完成（区间来自该图的「点击后随机等待」配置，默认 0.8~5 秒）
        self._sleep_after_click(click_wait_min, click_wait_max, label=label)

    def _sleep_after_click(self, click_wait_min=None, click_wait_max=None, label: str = None) -> None:
        """点击后的随机等待 —— 通用挑战路径的节奏控制点。

        区间来自该图的「点击后随机等待」配置（默认 0.8~5 秒）。这一等替代了过去
        写死在主循环里的 1.0~2.0 秒固定休眠：匹配到图时由这里主管节奏，主循环只在
        「什么都没匹配到」时做一次轻量节流（见 common_challenge.py 的 NO_MATCH_WAIT）。
        暂停不计入等待，停止则立即返回；每次都把实际秒数写进日志便于核对。
        """
        lo, hi = resolve_click_wait_range(click_wait_min, click_wait_max)
        try:
            seconds = random.uniform(lo, hi)
        except Exception:
            seconds = lo
        _who = f"图 {label}" if label else "默认"
        logger.info(f"点击后随机等待 {seconds:.2f} 秒（{_who} 区间 {lo:g}~{hi:g}）")
        if seconds <= 0:
            return
        try:
            pause_aware_sleep(seconds, 0.1)
        except Exception:
            time.sleep(seconds)

    def _complex_move(self, target_x: int, target_y: int) -> None:
        """
        （委托共享实现，回退路径沿用 self.lock）
        :param target_x: 目标X坐标
        :param target_y: 目标Y坐标
        """
        human_click.complex_move(target_x, target_y, self.lock)