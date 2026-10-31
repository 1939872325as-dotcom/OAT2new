# -*- coding: utf-8 -*-
"""
飞书式框选截图工具（后台抓游戏窗口）

设计要点
--------
1. **图源是后台抓取的游戏窗口**（PrintWindow），不是屏幕截屏。
   好处：不打扰游戏、可被遮挡，而且**截出来的尺寸就是识别时用的客户区尺寸**，
   避免「模板在 A 尺寸下截、运行时是 B 尺寸」导致匹配失败（这是最常见的坑）。

2. 抓图失败（个别硬件渲染窗口 PrintWindow 会全黑）时自动回落到
   `WindowCapture` 的另一条链路；仍失败则提示用户。

3. 遮罩显示的是**按屏幕缩放后的图**，拖拽坐标必须按比例还原到原图坐标，
   否则裁出来的位置会偏 —— 这是框选工具最容易写错的地方。

4. 松开鼠标后弹出小工具条：保存 / 重截 / 取消；支持 Esc 取消、右键取消。

对外接口
--------
    RegionCaptureDialog(parent, hwnd, window_title) -> QDialog
        .cropped_image   : np.ndarray | None   裁好的图（BGR）
        .crop_rect       : (x, y, w, h) | None  相对客户区的裁剪矩形
        .capture_size    : (w, h) | None        抓到的原图尺寸（=客户区尺寸）

    capture_region_from_window(parent, hwnd, window_title) -> (np.ndarray, rect, size) | (None, None, None)
        便捷函数：弹出工具，返回结果。
"""

import os
import time

import cv2
import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets

from OAT.utils.logging import logger


# ---------------------------------------------------------------- 抓图

def _window_dpr(hwnd: int) -> float:
    """窗口所在显示器的 DPI 缩放（1.0 = 100%）。

    Qt6 的窗口几何用【逻辑像素】，而 Win32 的 ClientToScreen/GetWindowRect
    返回【物理像素】。系统缩放不是 100% 时两者差一个系数；不换算会让遮罩
    比游戏窗口大一圈、位置偏到右下，看起来就像“截图时窗口被自动放大了”。
    取 GetDpiForWindow（Win10+），失败按 1.0 处理。
    """
    try:
        import ctypes
        dpi = int(ctypes.windll.user32.GetDpiForWindow(int(hwnd)))
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def _physical_rect_to_logical(x, y, w, h, dpr):
    """物理像素的屏幕矩形 → Qt 逻辑矩形（四舍五入，宽高至少 1）。"""
    try:
        dpr = float(dpr)
    except (TypeError, ValueError):
        dpr = 1.0
    if dpr <= 0:
        dpr = 1.0
    return QtCore.QRect(
        int(round(float(x) / dpr)), int(round(float(y) / dpr)),
        max(1, int(round(float(w) / dpr))), max(1, int(round(float(h) / dpr))))


def grab_window_frame(hwnd: int):
    """后台抓取窗口客户区一帧，返回 BGR ndarray；失败返回 None。

    顺序与 main_window._capture_preview_frame 保持一致：
    先试 MuMu 后台通道，再回落 GetDC 的窗口截图（内部自带 PrintWindow→BitBlt 回退）。
    """
    if not hwnd:
        return None

    # 1) MuMu 后台通道（最小化也能抓，且是 1280x720 参考系，会按客户区缩放）
    try:
        from OAT.tools.emulator.backend import create_backend
        from OAT.tools.emulator.mumu_handle import build_handle
        from OAT.tools import settings as _s
        build_handle(int(hwnd), wait_tries=1)      # 非 MuMu 句柄树会抛异常
        backend = create_backend(
            "mumu12",
            handle_spec=int(hwnd),
            mumu_folder=_s.resolve_mumu_folder(),
        )
        try:
            img = backend.screenshot()
            if img is not None and getattr(img, "size", 0) and float(np.mean(img)) > 1.0:
                return img
        finally:
            try:
                backend.close()
            except Exception:
                pass
    except Exception as e:
        logger.info(f"截图工具：窗口 {hwnd} 不走 MuMu 后台通道（{e}）")

    # 2) GetDC 窗口截图（PrintWindow → BitBlt 内部回退）
    try:
        from OAT.tools.GetDC import WindowCapture
        return WindowCapture(hwnd=int(hwnd)).capture_window()
    except Exception as e:
        logger.error(f"截图工具：窗口抓取失败 {e}")
        return None


# ---------------------------------------------------------------- 遮罩层

def _screen_union() -> QtCore.QRect:
    """所有屏幕的合并矩形。取不到屏幕信息时回落到主屏尺寸，避免崩溃。"""
    try:
        union = QtCore.QRect()
        for s in QtGui.QGuiApplication.screens():
            union = union.united(s.geometry())
        if union.width() > 0 and union.height() > 0:
            return union
    except Exception:
        pass
    try:
        app = QtWidgets.QApplication.instance()
        if app is not None:
            g = app.primaryScreen().geometry()
            return QtCore.QRect(g)
    except Exception:
        pass
    return QtCore.QRect(0, 0, 1920, 1080)


class _MaskWindow(QtWidgets.QDialog):
    """贴住游戏窗口的框选遮罩（1:1 原始像素，不缩放）

    为什么要贴住窗口而不是铺满全屏：
      铺满全屏时必须把 1441x812 放大到 1920x1080（约 1.33 倍），
      一来画面发虚、二来看起来像"只显示了部分游戏画面"。
      直接覆盖在游戏窗口上、按原始像素显示，所见即所得。
    """

    def __init__(self, frame_bgr: np.ndarray, hint: str = "", parent=None,
                 target_rect: QtCore.QRect = None, owner=None):
        super().__init__(parent)
        self.frame = frame_bgr
        self.h = frame_bgr.shape[0]
        self.w = frame_bgr.shape[1]
        # 结果回传给宿主对话框。遮罩开了 WA_DeleteOnClose，关闭后 C++ 对象
        # 可能已被销毁，再去读 mask.result_rect 会抛 RuntimeError 把选好的
        # 框丢掉；所以主动写一份到宿主上。
        # owner 与 parent 分开：parent 用可见窗口（保证遮罩能正常显示），
        # owner 是真正需要拿结果的那个对象。
        self._owner = owner if owner is not None else parent

        # 转成 QPixmap（BGR -> RGB）
        # 用 tobytes() 拷贝一份连续内存：直接用 ndarray.data 会让 QImage 引用
        # 可能被回收的缓冲区，表现为图时有时无。
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        self._rgb_bytes = rgb.tobytes()
        self.qimg = QtGui.QImage(self._rgb_bytes, self.w, self.h, 3 * self.w,
                                 QtGui.QImage.Format.Format_RGB888)
        self.pixmap = QtGui.QPixmap.fromImage(self.qimg)

        self.hint = hint or "拖拽框选要作为模板的区域   ·   Esc / 右键取消"

        # 选区（窗口内坐标）
        self.sel = QtCore.QRect()
        self.dragging = False
        self.origin = QtCore.QPoint()

        # 显示区域：默认贴住整块图（1:1）；调用方给了 target_rect 就用它
        self.scale = 1.0
        self.off_x = 0
        self.off_y = 0
        self._layout_ready = True

        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        # 显式关掉"不显示"属性：宿主 RegionCaptureDialog 为了防误用设了
        # WA_DontShowOnScreen，遮罩是它弹出的独立窗口，必须确保能真正显示。
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, False)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

        if target_rect is not None and target_rect.width() > 0 and target_rect.height() > 0:
            # 贴住游戏窗口客户区：位置与尺寸都由调用方换算成 Qt 逻辑像素，
            # 这里直接采用（高 DPI 下若仍用图片原始像素当逻辑尺寸，遮罩会放大一圈）
            self.setGeometry(target_rect)
        else:
            # 兜底：图多大窗口就多大，先放左上角，调用方负责摆位
            self.setGeometry(0, 0, self.w, self.h)

        # 安全网：显式取消按钮。Esc / 右键也能取消，但万一键盘焦点没进来，
        # 用户至少有一个看得见、点得到的出口，不会被困在遮罩里。
        self.cancel_btn = QtWidgets.QPushButton("✕ 取消 (Esc)", self)
        self.cancel_btn.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self.cancel_btn.setStyleSheet(
            "QPushButton{background:rgba(30,30,30,200);color:#fff;border:1px solid #666;"
            "border-radius:4px;padding:2px 10px;font-size:12px;}"
            "QPushButton:hover{background:rgba(200,60,60,230);border-color:#c33;}"
        )
        self.cancel_btn.clicked.connect(self._on_cancel)
        self._place_cancel_btn()

        self.result_rect = None      # 图内坐标 (x, y, w, h)

    def _finish(self, rect):
        """统一出口：记录结果 -> 回传宿主 -> 关闭。

        先回传再关闭，宿主不必依赖已被销毁的遮罩对象。
        """
        self.result_rect = rect
        try:
            if self._owner is not None:
                self._owner._mask_result = rect
        except Exception:
            pass
        self.close()

    def _place_cancel_btn(self):
        """把取消按钮放在遮罩右上角（随窗口尺寸变化重摆）"""
        try:
            w = max(80, 96)
            self.cancel_btn.resize(w, 26)
            self.cancel_btn.move(max(0, self.width() - w - 6), 4)
            self.cancel_btn.raise_()
        except Exception:
            pass

    def _on_cancel(self):
        self._finish(None)

    def showEvent(self, ev):
        super().showEvent(ev)
        self._recompute_layout()
        self.update()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._recompute_layout()
        self._place_cancel_btn()
        self.update()

    def _recompute_layout(self):
        """1:1 显示：整张图铺满窗口，不缩放。

        窗口尺寸 == 图尺寸时 scale=1、偏移 0；若被系统限制成别的尺寸，
        则等比缩放居中，保证仍能看全。
        """
        sw, sh = self.width(), self.height()
        if sw <= 0 or sh <= 0:
            return
        if sw == self.w and sh == self.h:
            self.scale = 1.0
            self.off_x = self.off_y = 0
        else:
            self.scale = min(sw / self.w, sh / self.h)
            self.off_x = (sw - int(self.w * self.scale)) // 2
            self.off_y = (sh - int(self.h * self.scale)) // 2
        self._layout_ready = True

    # ---------------- 坐标换算 ----------------

    def _screen_to_image(self, pt: QtCore.QPoint):
        """屏幕坐标 -> 原图像素坐标（夹在图像范围内）"""
        if self.scale <= 0:
            return QtCore.QPoint(0, 0)
        x = int((pt.x() - self.off_x) / self.scale)
        y = int((pt.y() - self.off_y) / self.scale)
        x = max(0, min(self.w, x))
        y = max(0, min(self.h, y))
        return QtCore.QPoint(x, y)

    # ---------------- 绘制 ----------------

    def paintEvent(self, ev):
        # 惰性重算布局：paintEvent 时几何尺寸一定已就绪
        if not self._layout_ready:
            self._recompute_layout()

        p = QtGui.QPainter(self)
        scr = self.rect()

        draw_w = int(self.w * self.scale)
        draw_h = int(self.h * self.scale)
        target = QtCore.QRect(self.off_x, self.off_y, draw_w, draw_h)
        if target.width() <= 0 or target.height() <= 0:
            return

        r = self.sel.normalized() if not self.sel.isNull() else None
        has_sel = r is not None and r.width() > 0 and r.height() > 0

        if not has_sel:
            # 还没框选：整幅画面压暗，提示"这里可以拖"
            p.drawPixmap(target, self.pixmap)
            p.fillRect(scr, QtGui.QColor(0, 0, 0, 110))
        else:
            # 已框选：只压暗选区【外】的部分，选区内保持原亮度（所见即所得）
            p.drawPixmap(target, self.pixmap)
            p.save()
            # 用 even-odd 规则把"整块减去选区"填成半透明黑
            path = QtGui.QPainterPath()
            path.addRect(QtCore.QRectF(scr))
            path.addRect(QtCore.QRectF(r))
            path.setFillRule(QtCore.Qt.FillRule.OddEvenFill)
            p.fillPath(path, QtGui.QColor(0, 0, 0, 130))
            p.restore()

            # 选区边框 + 四角把手
            p.setPen(QtGui.QPen(QtGui.QColor(0, 160, 255), 2))
            p.drawRect(r)
            p.setBrush(QtGui.QColor(0, 160, 255))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            hs = 4
            for (hx, hy) in [(r.left(), r.top()), (r.right(), r.top()),
                             (r.left(), r.bottom()), (r.right(), r.bottom())]:
                p.drawRect(QtCore.QRect(hx - hs, hy - hs, hs * 2, hs * 2))
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)

            # 尺寸提示（图内坐标 = 模板真实尺寸）
            tl = self._screen_to_image(r.topLeft())
            br = self._screen_to_image(r.bottomRight())
            w_px = abs(br.x() - tl.x())
            h_px = abs(br.y() - tl.y())
            text = f"{w_px} x {h_px}"
            f = p.font(); f.setPointSize(11); f.setBold(True); p.setFont(f)
            lw, lh = 100, 24
            lx = r.left()
            ly = r.top() - lh - 4
            if ly < 34:                      # 避开顶部提示条
                ly = r.top() + 4
            if lx + lw > scr.width():
                lx = max(0, scr.width() - lw)
            p.fillRect(QtCore.QRect(lx, ly, lw, lh), QtGui.QColor(0, 0, 0, 210))
            p.setPen(QtGui.QColor(0, 200, 255))
            p.drawText(QtCore.QRect(lx + 6, ly, lw, lh),
                       QtCore.Qt.AlignmentFlag.AlignVCenter, text)

        # 顶部提示条（压在画面顶端）
        p.setPen(QtGui.QColor(255, 255, 255))
        f = p.font(); f.setPointSize(10); f.setBold(False); p.setFont(f)
        band = QtCore.QRect(0, 0, scr.width(), 30)
        p.fillRect(band, QtGui.QColor(0, 0, 0, 200))
        p.drawText(band, QtCore.Qt.AlignmentFlag.AlignCenter, self.hint)

    # ---------------- 交互 ----------------

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.RightButton:
            self._finish(None)
            return
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self.dragging = True
            self.origin = ev.position().toPoint()
            self.sel = QtCore.QRect(self.origin, self.origin)
            self.update()

    def mouseMoveEvent(self, ev):
        if self.dragging:
            cur = ev.position().toPoint()
            self.sel = QtCore.QRect(self.origin, cur)
            self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() != QtCore.Qt.MouseButton.LeftButton or not self.dragging:
            return
        self.dragging = False
        r = self.sel.normalized()
        tl = self._screen_to_image(r.topLeft())
        br = self._screen_to_image(r.bottomRight())
        x, y = min(tl.x(), br.x()), min(tl.y(), br.y())
        w, h = abs(br.x() - tl.x()), abs(br.y() - tl.y())

        if w < 4 or h < 4:
            # 误点：当作取消
            self._finish(None)
            return

        self._finish((x, y, w, h))

    def keyPressEvent(self, ev):
        if ev.key() == QtCore.Qt.Key.Key_Escape:
            self._finish(None)


class RegionCaptureDialog(QtWidgets.QDialog):
    """框选截图（对外入口）

    注意：**本对象只负责干活，自身永远不显示**。
    真正的交互界面是它弹出的全屏遮罩 _MaskWindow。
    构造完成时结果已就绪，调用方直接读 .cropped_image 即可，
    绝对不要对它会 dlg.exec() —— 它没有自己的界面，exec() 只会
    把一个空白对话框显示出来并卡住（历史上就是这么卡住的）。
    """

    def __init__(self, parent=None, hwnd: int = 0, window_title: str = ""):
        super().__init__(parent)
        # 双保险：即使调用方误用了 exec()/show()，也不会出现空白窗
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.setWindowTitle("框选截图")
        self.hwnd = int(hwnd or 0)
        self.window_title = window_title
        self.cropped_image = None
        self.crop_rect = None
        self.capture_size = None
        self._mask_result = None      # 遮罩回传的选区（避免读已销毁对象）

        self._run()

    def _run(self):
        import win32gui

        # 1) 解析窗口句柄
        hwnd = self.hwnd
        if not hwnd and self.window_title:
            try:
                hwnd = win32gui.FindWindow(None, self.window_title)
            except Exception:
                hwnd = 0
        if not hwnd:
            # 最后兜底：找标题含关键字的活动窗口
            try:
                for kw in ("阴阳师", "MuMu", "Onmyoji"):
                    h = win32gui.FindWindow(None, None)
                    # 枚举式兜底
                    found = []
                    def _cb(hh, _):
                        if win32gui.IsWindowVisible(hh) and kw in win32gui.GetWindowText(hh):
                            found.append(hh)
                    win32gui.EnumWindows(_cb, None)
                    if found:
                        hwnd = found[0]
                        break
            except Exception:
                hwnd = 0
        self.hwnd = hwnd

        if not hwnd or not win32gui.IsWindow(hwnd):
            QtWidgets.QMessageBox.warning(
                self, "截图失败",
                "没找到游戏窗口。\n\n"
                "请确认：\n"
                "  · 游戏已打开\n"
                "  · 已在上方选择正确的客户端，并点过一次「窗口检测」"
            )
            self.reject()
            return

        # 2) 把游戏窗口提到前台，否则遮罩贴上去可能盖不住它
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, 9)      # SW_RESTORE
                time.sleep(0.35)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.25)
        except Exception as e:
            logger.info(f"框选截图：前置窗口失败（不致命）: {e}")

        # 3) 抓一帧
        frame = grab_window_frame(hwnd)
        if frame is None:
            QtWidgets.QMessageBox.warning(
                self, "截图失败",
                "无法抓取游戏窗口画面。\n\n"
                "请确认：\n"
                "  · 游戏窗口未被最小化\n"
                "  · 若已最小化，请先恢复窗口再试"
            )
            self.reject()
            return

        self.capture_size = (frame.shape[1], frame.shape[0])

        # 4) 算出客户区在屏幕上的位置 —— 遮罩就贴在这里，1:1 显示
        #    物理像素 → Qt 逻辑像素：高 DPI（如 125%）下不换算，遮罩会比窗口
        #    大一圈并向右下偏移，看起来就像“截图时窗口被自动放大了”。
        dpr = _window_dpr(hwnd)
        target = None
        try:
            cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
            target = _physical_rect_to_logical(
                cx, cy, self.capture_size[0], self.capture_size[1], dpr)
            logger.info(f"框选截图：抓到 {self.capture_size[0]}x{self.capture_size[1]}，"
                        f"客户区屏幕位置 ({cx},{cy})，DPI 缩放 {dpr:.3f}")
        except Exception as e:
            logger.warning(f"框选截图：取客户区屏幕位置失败，回落到窗口矩形: {e}")
            try:
                wr = win32gui.GetWindowRect(hwnd)
                target = _physical_rect_to_logical(
                    wr[0], wr[1], self.capture_size[0], self.capture_size[1], dpr)
            except Exception:
                target = None

        hint = (f"拖拽框选要作为模板的区域   ·   {self.capture_size[0]}x{self.capture_size[1]}"
                f"   ·   Esc / 右键取消")

        # 5) 弹遮罩框选（贴着游戏窗口，模态）
        # parent 用真正的可见父窗口（编辑器和图像对话框），保证遮罩能显示；
        # owner 用 self，用来接收框选结果。
        vis_parent = self.parentWidget()
        mask = _MaskWindow(frame, hint, parent=vis_parent, target_rect=target, owner=self)
        self._mask = mask
        mask.show()
        mask.raise_()
        mask.activateWindow()
        mask.setFocus()
        mask.exec()

        rect = self._mask_result
        if not rect:
            logger.info("框选截图：已取消")
            self.reject()
            return

        x, y, w, h = rect
        # 6) 裁剪（frame 是原图，rect 已是图内坐标）
        crop = frame[y:y + h, x:x + w].copy()
        if crop.size == 0:
            QtWidgets.QMessageBox.warning(self, "截图失败", "选区无效，请重新框选。")
            self.reject()
            return

        self.crop_rect = (x, y, w, h)
        self.cropped_image = crop
        logger.info(f"框选截图：裁剪 {w}x{h} @ ({x},{y})，原图 {self.capture_size[0]}x{self.capture_size[1]}")
        self.accept()


def capture_region_from_window(parent=None, hwnd: int = 0, window_title: str = ""):
    """便捷函数：弹出框选截图工具。

    Returns:
        (np.ndarray | None, (x,y,w,h) | None, (w,h) | None)
    """
    dlg = RegionCaptureDialog(parent, hwnd=hwnd, window_title=window_title)
    return dlg.cropped_image, dlg.crop_rect, dlg.capture_size


def save_crop(crop_bgr: np.ndarray, dest_path: str) -> bool:
    """把裁好的图存到 dest_path。

    编码格式跟随目标文件扩展名：.jpg/.jpeg 用 JPEG(质量95)，其余用 PNG。
    这点对「截图替换」很重要 —— 若把 PNG 字节写进 .jpg 文件，虽然不报错，
    但文件内容与扩展名不符，后续按扩展名解码的地方会出问题。
    中文路径用 imencode + 文件写入规避 cv2.imwrite 的编码问题。
    """
    try:
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        ext = os.path.splitext(dest_path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            params = [int(cv2.IMWRITE_JPEG_QUALITY), 95]
            ok, buf = cv2.imencode(".jpg", crop_bgr, params)
        else:
            ok, buf = cv2.imencode(".png", crop_bgr)
        if not ok:
            return False
        with open(dest_path, "wb") as f:
            f.write(buf.tobytes())
        return True
    except Exception as e:
        logger.error(f"保存截图失败 {dest_path}: {e}")
        return False
