"""
窗口操作工具集
包含功能：
1. 窗口尺寸获取 (get_window_size)
2. 窗口尺寸调整 (resize_window)
3. 窗口连接检测 (connect_all)
4. 构建算法，获取窗口位置和尺寸并检查是否为标准尺寸，传递参数给Auto的region
"""

import time

import win32gui
import win32con
import win32process
import pywintypes
from typing import Optional, Tuple

from OAT.utils.logging import logger
from OAT.utils.warning_box import warning_box


class WindowChecker:
    """窗口状态检查与操作类"""

    def __init__(self):
        self.window_title = None
        self.window_handle = None
        # 最近一次 resize_window 的失败原因：None / 'access_denied' / 'exception'
        self.last_resize_error = None

    @staticmethod
    def get_process_integrity_level(hwnd: int) -> Optional[str]:
        """取目标窗口所属进程的完整性级别（'Low'/'Medium'/'High'/'System'/...）

        用于诊断 UIPI：低完整性进程无法 SetWindowPos 高完整性进程的窗口
        （SetWindowPos 返回 winerror 5）。失败返回 None。
        """
        try:
            import ctypes
            import ctypes.wintypes as wt

            adv = ctypes.windll.advapi32
            k32 = ctypes.windll.kernel32
            adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(ctypes.c_wchar_p)]
            adv.ConvertSidToStringSidW.restype = ctypes.c_bool

            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            if not pid:
                return None
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                tok = wt.HANDLE()
                if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
                    return None
                try:
                    buf = ctypes.create_string_buffer(64)
                    rl = wt.DWORD()
                    # TokenIntegrityLevel = 25
                    if not adv.GetTokenInformation(tok, 25, buf, 64, ctypes.byref(rl)):
                        return None
                    sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
                    s = ctypes.c_wchar_p()
                    if not adv.ConvertSidToStringSidW(sid, ctypes.byref(s)):
                        return None
                    il = s.value
                    k32.LocalFree(s)
                    return il
                finally:
                    k32.CloseHandle(tok)
            finally:
                k32.CloseHandle(h)
        except Exception as e:
            logger.warning(f'查询目标进程完整性级别失败: {e}')
            return None

    def set_window_title(self, window_title: str) -> None:
        """设置窗口标题"""
        self.window_title = window_title

    def set_window_handle(self, hwnd: int):
        # 添加设置窗口句柄的方法
        self.window_handle = hwnd

    def _resolve_hwnd(self) -> int:
        """解析出当前可用的窗口句柄

        缓存的句柄可能已失效（游戏/模拟器窗口被关闭或重建后会重新分配句柄），
        这里用 IsWindow 校验；失效则丢弃缓存并按标题重新查找。
        """
        if self.window_handle:
            try:
                if win32gui.IsWindow(self.window_handle):
                    return self.window_handle
            except Exception:
                pass
            logger.warning('缓存的窗口句柄已失效，按窗口标题重新获取')
            self.window_handle = None
        if self.window_title:
            return win32gui.FindWindow(None, self.window_title) or 0
        return 0

    def get_window_info(self) -> Optional[Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int]]]:
        """
        获取窗口的位置和尺寸信息
        返回值格式：((左上角 x, 左上角 y), (右下角 x, 右下角 y), (宽度, 高度))
        """
        if not self.window_handle and not self.window_title:
            logger.error('窗口标题未设置且未提供窗口句柄')
            return None

        hwnd = self._resolve_hwnd()

        if hwnd:
            try:
                rect = win32gui.GetWindowRect(hwnd)
            except pywintypes.error as e:
                # 句柄可能在取值前的一瞬间被销毁（窗口关闭/模拟器重启）。
                # 这里必须返回 None 而不是抛异常：本方法会被 main_window 的定时器
                # 回调调用，抛出去会变成每轮一次的未捕获异常（曾刷屏 (1400, ...)）。
                logger.warning(f'获取窗口位置失败，窗口可能已关闭: {e}')
                return None
            left, top, right, bottom = rect
            width = right - left
            height = bottom - top
            return (left, top), (right, bottom), (width, height)
        logger.error('未找到指定客户端窗口')
        return None
        
    def get_client_info(self) -> Optional[Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int]]]:
        """
        获取窗口客户区域的位置和尺寸信息
        返回值格式：((客户区域左上角 x, 客户区域左上角 y), (客户区域右下角 x, 客户区域右下角 y), (客户区域宽度, 客户区域高度))
        """
        if not self.window_handle and not self.window_title:
            logger.error('窗口标题未设置且未提供窗口句柄')
            return None

        hwnd = self._resolve_hwnd()

        if hwnd:
            try:
                # 获取客户区域矩形（相对于窗口内部）
                client_rect = win32gui.GetClientRect(hwnd)
                client_left, client_top, client_right, client_bottom = client_rect
                client_width = client_right - client_left
                client_height = client_bottom - client_top

                # 将客户区域左上角转换为屏幕坐标
                client_screen_left, client_screen_top = win32gui.ClientToScreen(hwnd, (client_left, client_top))
                # 将客户区域右下角转换为屏幕坐标
                client_screen_right, client_screen_bottom = win32gui.ClientToScreen(hwnd, (client_right, client_bottom))
            except pywintypes.error as e:
                # 与 get_window_info 同理：窗口消失时返回 None，不把异常抛给调用方
                logger.warning(f'获取客户区信息失败，窗口可能已关闭: {e}')
                return None

            return (client_screen_left, client_screen_top), (client_screen_right, client_screen_bottom), (client_width, client_height)
        logger.error('未找到指定客户端窗口')
        return None

    def _get_client_size(self, hwnd: int) -> Tuple[int, int]:
        """客户区（游戏画面）尺寸；失败返回 (0, 0)"""
        try:
            r = win32gui.GetClientRect(hwnd)
            return (r[2] - r[0], r[3] - r[1])
        except Exception:
            return (0, 0)

    def _get_process_dpi_awareness(self, hwnd: int) -> int:
        """取目标窗口所属进程的 DPI 感知级别

        0=UNAWARE  1=SYSTEM-AWARE  2=PER-MONITOR-AWARE
        取不到时返回 -1（按"无需换算"处理）。
        """
        try:
            import ctypes
            import ctypes.wintypes as wt
            shcore = ctypes.windll.shcore
            pid = wt.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return -1
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
            if not h:
                return -1
            try:
                val = ctypes.c_int(-1)
                hr = shcore.GetProcessDpiAwareness(h, ctypes.byref(val))
                if hr != 0:
                    return -1
                return int(val.value)
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            return -1

    def _get_system_dpi_scale(self) -> float:
        """当前进程视角下的系统 DPI 缩放系数（96dpi = 1.0，125% = 1.25）"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hdc = user32.GetDC(0)
            try:
                dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            finally:
                user32.ReleaseDC(0, hdc)
            if dpi and dpi > 0:
                return dpi / 96.0
        except Exception:
            pass
        return 1.0

    def _dpi_scale_for_window(self, hwnd: int) -> float:
        """目标窗口「逻辑尺寸 → 物理尺寸」的换算系数。

        背景（实测踩坑）：屏幕 1920x1080 @125% 时，游戏客户端是 DPI-UNAWARE，
        它以为屏幕只有 1536x864，按 1/1.25 的尺寸绘制界面；而本程序是
        SYSTEM-AWARE，看到的是完整 1920x1080。于是把窗口设成"逻辑 1417x798"
        时，物理客户区确实是 1417x798，但游戏只画了其中 1132x638 的内容，
        其余是黑边 —— 模板按 1417x798 基准截取，尺度差 20%，全部匹配失败，
        挑战循环静默空转（表现为"启动后一直不动"）。

        规则：目标进程 UNAWARE 时，物理尺寸 = 逻辑尺寸 × 系统缩放系数。
        """
        awareness = self._get_process_dpi_awareness(hwnd)
        if awareness == 0:  # DPI_UNAWARE：被系统虚拟化，需按缩放系数放大
            return self._get_system_dpi_scale()
        return 1.0

    def _physical_target_size(self, hwnd: int, target_w: int, target_h: int):
        """把「逻辑目标尺寸」换算成「该窗口实际应达到的物理客户区尺寸」

        _get_client_size 读的是物理像素，设置里存的是逻辑（模板基准）尺寸。
        DPI-UNAWARE 的目标窗口需要按系统缩放系数放大后再比较，否则会陷入
        「永远差 20%、每轮都重新 resize」的死循环。
        """
        scale = self._dpi_scale_for_window(hwnd)
        if scale == 1.0:
            return (int(target_w), int(target_h))
        return (int(round(target_w * scale)), int(round(target_h * scale)))

    def _outer_size_for_client(self, hwnd: int, client_w: int, client_h: int):
        """把「客户区尺寸」换算成「窗口外框尺寸」（含标题栏/边框）；失败返回 None

        client_w/client_h 是【逻辑尺寸】（模板基准尺寸）。若目标进程 DPI-UNAWARE
        且屏幕有缩放，会先换算成物理尺寸再算外框 —— 否则游戏只画出一部分内容。
        """
        try:
            import ctypes
            from ctypes import wintypes
            gwl_style = getattr(win32con, 'GWL_STYLE', -16)
            gwl_exstyle = getattr(win32con, 'GWL_EXSTYLE', -20)
            style = win32gui.GetWindowLong(hwnd, gwl_style)
            exstyle = win32gui.GetWindowLong(hwnd, gwl_exstyle)

            scale = self._dpi_scale_for_window(hwnd)
            phys_w = int(round(client_w * scale))
            phys_h = int(round(client_h * scale))
            if scale != 1.0:
                logger.info(f'DPI 换算：目标进程 DPI-UNAWARE，系统缩放 {scale:.2f}；'
                            f'逻辑 {client_w}x{client_h} → 物理 {phys_w}x{phys_h}')

            rect = wintypes.RECT(0, 0, phys_w, phys_h)
            if not ctypes.windll.user32.AdjustWindowRectEx(
                    ctypes.byref(rect), style, False, exstyle):
                return None
            return (rect.right - rect.left, rect.bottom - rect.top)
        except Exception as e:
            logger.error(f'换算窗口外框尺寸失败: {e}')
            return None

    def log_size_pair(self, hwnd: int, target_width: int, target_height: int) -> None:
        """把「外框」和「客户区」两个尺寸都记下来

        以前只记一个数、还标成“客户端大小”，实际是 GetWindowRect 的外框尺寸，
        于是同一件事出现两个数字、模板该按哪个尺寸截也说不清。
        """
        try:
            wr = win32gui.GetWindowRect(hwnd)
            win_w, win_h = wr[2] - wr[0], wr[3] - wr[1]
            cw, ch = self._get_client_size(hwnd)
            scale = self._dpi_scale_for_window(hwnd)
            extra = f'，DPI 系数 {scale:.2f}' if scale != 1.0 else ''
            logger.info(f'窗口尺寸：外框 {win_w}x{win_h}，客户区 {cw}x{ch}'
                        f'（目标客户区 {target_width}x{target_height}{extra}）'
                        f'—— 模板图与截图都以【客户区】为准')
        except Exception:
            pass

    def resize_window(self, width: int, height: int, hwnd: Optional[int] = None):
        """把「客户区」调整为 width x height（内部换算成窗口外框尺寸再设置）

        注意：参数是客户区（游戏画面）尺寸，不是窗口外框尺寸。
        原实现直接把它当外框尺寸传给 SetWindowPos，导致客户区永远比目标小一截
        （标题栏 + 边框），而且与「客户区目标」比较时永远不相等、反复调整。

        返回值：True=调整成功；False=失败。
        失败原因记录在 self.last_resize_error：
          'access_denied' —— winerror 5（UIPI 权限不足，见 _is_uipi_denied）
          'exception'     —— 其它异常
        """
        self.last_resize_error = None
        try:
            if not hwnd:
                if self.window_handle:
                    hwnd = self.window_handle
                elif self.window_title:
                    hwnd = win32gui.FindWindow(None, self.window_title)
                else:
                    logger.error('窗口标题未设置且未提供窗口句柄')
                    return False

            if hwnd:
                # 添加异常处理，处理可能的访问拒绝错误
                try:
                    outer = self._outer_size_for_client(hwnd, width, height)
                    if outer:
                        logger.info(f'调整窗口：目标客户区 {width}x{height}'
                                    f' → 窗口外框 {outer[0]}x{outer[1]}')
                        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0,
                                              outer[0], outer[1], win32con.SWP_NOMOVE)
                    else:
                        # 换算失败时退回旧行为（按外框尺寸设置），至少不会完全不动作
                        logger.warning('无法换算外框尺寸，按窗口外框尺寸直接设置（客户区会偏小）')
                        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, width, height, win32con.SWP_NOMOVE)
                    return True
                except pywintypes.error as e:
                    if e.winerror == 5:  # 错误代码5表示访问被拒绝
                        self.last_resize_error = 'access_denied'
                        logger.error(f'无法调整窗口大小（访问被拒绝）。请确保程序以管理员身份运行，或手动调整窗口大小为{width}x{height}。')
                    else:
                        self.last_resize_error = 'exception'
                        logger.error(f'调整窗口大小时发生错误：{e}')
                    return False
            else:
                logger.error('未找到指定客户端窗口')
                return False
        except Exception as e:
            self.last_resize_error = 'exception'
            logger.error(f'调整窗口大小时发生未知错误：{e}')
            return False

    @staticmethod
    def _size_close(a, b, tol: int = 2) -> bool:
        """尺寸是否"够接近"（模拟器常返回 ±1~2px 抖动，精确相等会误判为失败）"""
        try:
            return abs(int(a[0]) - int(b[0])) <= tol and abs(int(a[1]) - int(b[1])) <= tol
        except Exception:
            return False

    def _nearest_achievable_size(self, hwnd: int, target_w: int, target_h: int,
                                 actual_w: int, actual_h: int):
        """当目标客户区尺寸被窗口/模拟器拒绝时，推算一个"最接近且可达"的尺寸。

        典型场景：MuMu 模拟器锁定 16:9 宽高比，而目标尺寸比例不是 16:9 时
        永远达不到 —— 例如旧的默认 1404x834 是 1.684，会出现「高度对、宽度多 76」
        和「宽度对、高度少 43」来回横跳。这里按实测到的真实宽高比，取面积最接近
        目标的可达尺寸。（当前默认 1441x812 ≈ 1.775，接近 16:9，一般可直接达到。）
        """
        if actual_w <= 0 or actual_h <= 0:
            return (actual_w, actual_h)
        aspect = actual_w / actual_h          # 实测比例（如 16:9 ≈ 1.7778）
        target_area = target_w * target_h
        # 以目标面积反推该比例下的尺寸，再对齐到偶数（避免半像素抖动）
        h = int(round((target_area / aspect) ** 0.5))
        w = int(round(h * aspect))
        w -= w % 2
        h -= h % 2
        if w <= 0 or h <= 0:
            return (actual_w, actual_h)
        return (w, h)

    def connect_all(self, target_width: int = None, target_height: int = None) -> None:
        """执行完整窗口连接流程

        target_width/height 为 None 时读取用户在设置里的「自定义分辨率」
        （custom_res_width / custom_res_height）。旧实现是硬编码 1404x834 无参调用，
        用户改了设置也不生效。（该硬编码值现已改为 1441x812 并统一走设置读取。）
        """
        if target_width is None or target_height is None:
            try:
                from OAT.tools import settings as _s
                sd = getattr(_s, 'settings_data', {}) or {}
                target_width = int(sd.get('custom_res_width', 1441))
                target_height = int(sd.get('custom_res_height', 812))
                logger.info(f'窗口目标尺寸取自设置：{target_width}x{target_height}')
            except Exception:
                target_width, target_height = 1441, 812
        if not self.window_title:
            logger.error('窗口标题未设置')
            return

        # 获取窗口句柄
        if not self.window_handle:
            hwnd = win32gui.FindWindow(None, self.window_title)
        else:
            hwnd = self.window_handle

        # 检查窗口是否最小化：检测流程必须真实调整窗口尺寸（对齐模板尺度），
        # iconic 窗口上 resize 可能不生效，先自动恢复再继续；检测完仍可最小化跑后台
        if hwnd and win32gui.IsIconic(hwnd):
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            except Exception as e:
                logger.error(f'恢复窗口失败: {e}')
            # 等待窗口真正脱离 iconic 状态（最长 1s）
            deadline = time.time() + 1.0
            while time.time() < deadline and win32gui.IsIconic(hwnd):
                time.sleep(0.1)
            if win32gui.IsIconic(hwnd):
                try:
                    warning_box("窗口处于最小化状态且自动恢复失败，请手动恢复窗口后再执行窗口检测。")
                except Exception as e:
                    logger.error(f"显示弹窗失败: {e}")
                return
            logger.info('窗口处于最小化状态，已自动恢复窗口后继续检测')

        # 比较与调整一律以【客户区】为准：模板图、截图、点击坐标全是客户区坐标系。
        # 旧版用 get_window_info()（GetWindowRect 外框尺寸）既比较又报告，
        # 却标成“客户端大小”，于是同一件事出现两个数字、且永远与客户区目标不等。
        self.log_size_pair(hwnd, target_width, target_height)

        # 比较基准：逻辑目标 → 该窗口实际应有的物理客户区尺寸
        # （DPI-UNAWARE 的游戏在 125% 屏幕上必须按 1.25 放大，否则只画 80% 内容）
        phys_w, phys_h = self._physical_target_size(hwnd, target_width, target_height)

        # 容差随 DPI 系数放大：物理尺寸经过 1.25 倍换算 + 窗口边框取整，
        # 实测只能到 1768x996（目标 1771x998），差 3px。若仍用 ±2px 判定，
        # 会误判"达不到"→ 吸附 → 写回设置，把逻辑目标也带偏（实测踩坑）。
        _scale = self._dpi_scale_for_window(hwnd) or 1.0
        tol = max(2, int(round(2 * _scale)) + 1)

        client_w, client_h = self._get_client_size(hwnd)
        # 与后面的判定保持一致用容差比较：±1~2px 抖动（模拟器常见）不值得
        # 触发一次 resize —— 否则权限受限场景下每次检测都会弹一次拒绝提示
        if hwnd and not self._size_close((client_w, client_h), (phys_w, phys_h), tol):
            # 尝试调整窗口大小，但不强制要求成功（结果以后续实际尺寸为准）
            self.resize_window(target_width, target_height, hwnd=hwnd)

            # 窗口尺寸变化后系统需要一点时间生效：立即复测会读到旧值，
            # 旧版因此每次点都拿到不同的数、反复提示“调整失败”。
            deadline = time.time() + 2.0
            while time.time() < deadline:
                time.sleep(0.15)
                if self._size_close(self._get_client_size(hwnd), (phys_w, phys_h), tol):
                    break

            client_w, client_h = self._get_client_size(hwnd)
            self.log_size_pair(hwnd, target_width, target_height)

            if not self._size_close((client_w, client_h), (phys_w, phys_h), tol):
                # 【权限拒绝】优先分支：SetWindowPos 报 winerror 5 是 UIPI 拦截
                # （OAT2 完整性级别低于游戏客户端，典型于游戏以管理员运行而本工具没有）。
                # 这不是尺寸协商失败：绝不能走下面的"吸附/写回设置"逻辑 ——
                # 曾把没调成的尺寸误写回设置并提示用户重截模板，带偏模板制作。
                if self.last_resize_error == 'access_denied':
                    il = self.get_process_integrity_level(hwnd) or '未知'
                    logger.warning(
                        f'窗口调整被系统拒绝（UIPI 权限不足）：本程序完整性级别低于游戏窗口所属进程'
                        f'（目标进程 IL={il}）。不写回设置、不改目标尺寸。')
                    try:
                        warning_box(
                            f"窗口尺寸自动调整被 Windows 拒绝（权限不足，UIPI）。\n\n"
                            f"原因：游戏客户端以管理员权限运行，而本工具没有。\n"
                            f"当前目标进程完整性级别：{il}\n\n"
                            f"任选其一修复：\n"
                            f"  1) 以管理员身份重新运行本工具（推荐）；\n"
                            f"  2) 让游戏客户端不以管理员身份运行；\n"
                            f"  3) 手动把游戏窗口客户区调到 {target_width}x{target_height}。\n\n"
                            f"未修改任何设置，自定义分辨率保持 {target_width}x{target_height}。"
                        )
                    except Exception as e:
                        logger.error(f"显示弹窗失败: {e}")
                else:
                    # 目标不可达：判断是不是被强制锁定宽高比（模拟器常见）。
                    # 若是，直接吸附到最接近的可达尺寸并【接受】，不再反复调整 ——
                    # 否则每次点「窗口检测」都会在两个尺寸间来回横跳，模板永远对不上。
                    # 注意：这里的比较基准是【物理尺寸】。
                    near_w, near_h = self._nearest_achievable_size(
                        hwnd, phys_w, phys_h, client_w, client_h)
                    if not self._size_close((near_w, near_h), (client_w, client_h)):
                        logger.info(f'目标客户区 {target_width}x{target_height}'
                                    f'（物理 {phys_w}x{phys_h}）被拒绝，'
                                    f'按实测比例吸附到最接近的可达尺寸 {near_w}x{near_h}')
                        # resize_window 收的是逻辑尺寸，把物理吸附值折回逻辑
                        scale = self._dpi_scale_for_window(hwnd) or 1.0
                        self.resize_window(int(round(near_w / scale)),
                                           int(round(near_h / scale)), hwnd=hwnd)
                        deadline = time.time() + 2.0
                        while time.time() < deadline:
                            time.sleep(0.15)
                            if self._size_close(self._get_client_size(hwnd), (near_w, near_h)):
                                break
                        client_w, client_h = self._get_client_size(hwnd)
                        self.log_size_pair(hwnd, target_width, target_height)

                    # 仍然达不到目标：把「实际可达尺寸」写回设置，让它成为新的目标。
                    # 这样下一次检测 target == 实际，不再反复弹窗，用户也不必手动改设置。
                    # 这正是「自动调整到它需要的分辨率」——只是把结果固化下来。
                    # 【关键】写回的必须是逻辑尺寸（÷DPI 系数），否则物理值被当成
                    # 模板基准存进设置，下次比较又差 25%，形成死循环。
                    if not self._size_close((client_w, client_h), (phys_w, phys_h)):
                        scale = self._dpi_scale_for_window(hwnd) or 1.0
                        logical_w = int(round(client_w / scale))
                        logical_h = int(round(client_h / scale))
                        aspect_now = round(client_w / client_h, 4) if client_h else 0
                        logger.warning(
                            f'窗口被锁定宽高比（实测 {aspect_now}，16:9 = 1.7778），'
                            f'{target_width}x{target_height} 达不到；'
                            f'已自动采用可达尺寸 逻辑 {logical_w}x{logical_h}'
                            f'（物理 {client_w}x{client_h}）并写回设置')
                        try:
                            from OAT.tools import settings as _s
                            _s.update_settings('custom_res_width', logical_w)
                            _s.update_settings('custom_res_height', logical_h)
                            logger.info(f'已将自定义分辨率更新为 {logical_w}x{logical_h}，'
                                        f'后续检测不会再提示')
                        except Exception as e:
                            logger.error(f'写回自定义分辨率失败: {e}')

                        # 只在“吸附后仍不稳定”时提示一次；写回设置后下次不会再来
                        try:
                            warning_box(
                                f"窗口客户区达不到 {target_width}x{target_height}"
                                f"（该窗口锁定宽高比 {aspect_now}，16:9 = 1.7778）。\n\n"
                                f"已自动改用可达尺寸 {logical_w}x{logical_h} 并记入设置，"
                                f"以后不会再提示。\n\n"
                                f"请注意：模板图必须在这个尺寸（{logical_w}x{logical_h}）下重新截图，"
                                f"否则识别会失败。"
                            )
                        except Exception as e:
                            logger.error(f"显示弹窗失败: {e}")
                # 已提供窗口句柄时继续运行（按当前实际尺寸跑）
                if not self.window_handle:
                    return



