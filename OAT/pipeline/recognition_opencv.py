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
from OAT.tools.match_util import match_template
from OAT.tools.human_click import foreground_click, human_drag
from OAT.utils.OCRService import ocr_service
from OAT.utils.logging import logger
from .recognition import RecognitionEngine, RecognitionResult

# 点击按下时长（秒）：随机区间。固定值容易被客户端侧按“机械点击”特征识别，
# 随机化更接近真人；下限 0.20 是实测能稳定触发按钮的保守值，上限 0.25。
CLICK_HOLD_RANGE = (0.20, 0.25)

# 指针移动到按下（以及滑动"按下→开始移动"）之间的间隔（秒）：随机区间。
# 同样是为了不留固定微秒级节奏特征；50ms 是保证消息处理顺序的保守下限。
MOVE_DELAY_RANGE = (0.05, 0.06)


# 「被判不可信」的命中次数（过长文本），供 runner 汇总到「等待命中」状态行。
# _match_joined_lines 是 staticmethod，用模块级计数最省事。
_REJECT_STATS = {'count': 0}


def take_rejected_count() -> int:
    """取走并清零「被判不可信」次数。"""
    n = int(_REJECT_STATS.get('count', 0))
    _REJECT_STATS['count'] = 0
    return n


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
        # 阈值来源决定是否「实时」：显式传入（如测试/特殊流程）就用传进来的；
        # 没传则每轮现读 settings.FIND_THRESHOLD —— 设置页拖滑块立刻作用于
        # 正在跑的任务，而不是等下次创建引擎（以前这里是构造时复制一份，
        # 运行中改阈值完全不生效）。
        self._threshold_explicit = threshold is not None
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

    def _load_template(self, template_path: str, display: str = None) -> Optional[np.ndarray]:
        # 取文件修改时间；取不到（文件不存在等）则退化为不缓存
        try:
            mtime = os.path.getmtime(template_path)
        except OSError:
            mtime = None

        if mtime is not None:
            cached = self._template_cache.get(template_path)
            if cached is not None and cached[0] == mtime:
                return cached[1]

        from OAT.utils.image_io import imread as _imread_safe
        target = _imread_safe(template_path)
        if target is not None and mtime is not None:
            self._template_cache[template_path] = (mtime, target)
            _shown = (display or "").strip() or os.path.basename(template_path)
            logger.info(f"已加载模板 {_shown}"
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

    def find_template(self, template_path: str, threshold: float = None, region: Tuple = None,
                      screenshot: np.ndarray = None, display: str = None) -> RecognitionResult:
        # display：日志显示名（通常是 OCR 文字 label）；不传则用文件名。
        # 日志里不再打印完整路径，改为「名称（文件名）」或只名称。
        if threshold is None:
            # 实时读设置：任务未单独配阈值时，滑块拖动对正在跑的流程立刻生效
            threshold = (self.threshold if self._threshold_explicit
                         else _normalize_threshold(settings.FIND_THRESHOLD))
        else:
            threshold = _normalize_threshold(threshold)

        return self._find_template_hidden(template_path, threshold, region,
                                          screenshot=screenshot, display=display)

    def _find_template_hidden(self, template_path: str, threshold: float, region: Tuple = None,
                              screenshot: np.ndarray = None, display: str = None) -> RecognitionResult:
        # 调用方可在同一任务里复用一帧（模板失败后 OCR 用同一张图），
        # 省掉一次抓屏（PrintWindow 每次约 0.7s）。
        if screenshot is None:
            screenshot = self.capture_screenshot()
        if screenshot is None:
            return RecognitionResult(found=False)

        try:
            target = self._load_template(template_path, display)
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

            # 灰度/彩色由设置开关决定（match_util 内部处理；同一帧多任务复用灰度图）
            max_val, max_loc = match_template(search, target)

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
            _shown = (display or "").strip() or os.path.basename(template_path)
            # 详情只进后台日志文件：前端改成「等待命中」聚合提示（每轮一条），
            # 否则每轮每张模板各刷一行，真正要看的信息被淹没。
            logger.log_to_file({
                'level': 'WARNING',
                'module': 'OAT.pipeline.recognition_opencv',
                'message': (f"模板未达阈值: {_shown} 最高分 {max_val:.3f} < 阈值 {threshold:.3f}"
                            f"（最佳位置 x={max_loc[0] + offset_x} "
                            f"y={max(0, max_loc[1] + offset_y - title_bar)}，"
                            f"模板 {w}x{h}，截图 {sh}x{sw}）"),
            })
        except Exception as e:
            logger.error(f"模板匹配出错: {e}")

        return RecognitionResult(found=False)

    def find_text(self, target_text: str, confidence: float = 0.8,
                  region: Tuple = None, screenshot: np.ndarray = None) -> RecognitionResult:
        # 同 find_template：允许复用同一帧，避免模板+OCR 一个任务截两次屏
        if screenshot is None:
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
                logger.log_to_file(f"OCR 限定搜索区域（客户区）[{rx1}, {rx2}, {ry1}, {ry2}]"
                                   f" → 截图坐标 {shot_region}")
            except Exception as e:
                logger.warning(f"OCR 搜索区域换算失败，已忽略: {region} ({e})")
                shot_region = None

        # —— 先用「整帧一次 OCR」的结果匹配（一轮内所有配置共用同一次推理）——
        # 原来每个任务各跑一次 OCR：一轮 3~10 个目标就是 3~10 次 1416x798 推理。
        # 现在整帧只 OCR 一次，各配置按自己的 ocr_region 在结果里过滤。
        hit = self._match_from_frame_items(
            screenshot, target_text, confidence, shot_region)
        if hit is not None:
            kind, payload = hit
            if kind == "hit":
                return self._result_from_text_area(screenshot, *payload)
            # kind == "settled"：区域内已有文字但没匹配上目标 → 信任整帧结果，
            # 不再为它单独跑一次裁剪 OCR（否则等待页面时会退化成"每任务一次 OCR"，
            # 比改造前还慢）。只有区域内一条文字都没有（整帧 det 可能漏检小字）才
            # 继续走下面的精确路径兜底。
            return RecognitionResult(found=False)
        # 整帧没认到目标：回落到「按区域裁剪后再 OCR」的精确路径。整帧 det 会缩放到
        # max_side=960，小字可能漏检；裁剪后 OCR 能补上，所以必须保留这条兜底。

        # confidence 是本任务配置的 OCR 阈值，必须真的传下去。
        # 以前这里没传，ocr_confidence 形同虚设（一直用 OCR 侧默认值）。
        found, text_area, real_text = ocr_service.find_text(
            screenshot, target_text, confidence_threshold=confidence, region=shot_region)
        # OCR 侧算出的真实置信度（0~1）：不填的话日志里永远是“置信度: 0.00”
        ocr_conf = float(getattr(ocr_service, "last_match_confidence", 0.0))
        if found and text_area:
            return self._result_from_text_area(screenshot, text_area, real_text, ocr_conf)
        return RecognitionResult(found=False)

    def _match_from_frame_items(self, screenshot, target_text: str, confidence: float,
                                shot_region=None):
        """在「整帧一次 OCR」的结果里匹配目标词。

        返回 (kind, payload)：
          ("hit", (area, real_text, conf))  —— 命中，直接构造结果；
          ("settled", None)               —— 该区域已有文字但无目标词，判定无命中，
                                             调用方不必再跑裁剪 OCR；
          None                            —— 整帧无可用结果/区域内无任何文字，
                                             调用方走精确裁剪 OCR 兜底。

        选择规则与 OCRManager.find_text_offline 完全一致：
        完全相等优先 → 达阈值者优先 → 置信最高者；并列保留 OCR 原始顺序。
        shot_region（截图坐标 [x1,x2,y1,y2]）用于按配置排除干扰文字：
        只保留文字框中心落在该区域内的候选。
        """
        try:
            items = ocr_service.recognize_frame_items(screenshot)
        except Exception as e:
            logger.debug(f"整帧OCR不可用，走精确路径: {e}")
            return None
        if not items:
            return None

        def _in_region(area):
            if not (shot_region and len(shot_region) == 4 and area):
                return True
            try:
                xs = [p[0] for p in area]
                ys = [p[1] for p in area]
                cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
                x1, x2, y1, y2 = shot_region
                return x1 <= cx <= x2 and y1 <= cy <= y2
            except Exception:
                return True

        region_items = [it for it in items if _in_region(it.get("area"))]
        cands = [it for it in region_items if target_text in (it.get("text") or "")]
        if not cands:
            # 多字按钮常被 det 拆成多行（『伤害排名』→『伤』+『害排名』），
            # 而配置里的目标文字是整词。整词在单行里找不到时，把相邻行拼起来再判。
            joined = self._match_joined_lines(region_items, target_text, confidence)
            if joined is not None:
                return ("hit", joined)
            # 区域里有文字（说明整帧 det 没漏这一块）只是没目标词 → 判定无命中
            return ("settled", None) if region_items else None

        exact = [c for c in cands if c["text"].strip() == target_text.strip()]
        if not exact:
            # 防误点：目标很短（如『召唤』）时，子串匹配很容易命中一整句
            # 无关文本（实测命中世界频道『星鬼王召唤券捏手里，只』→ 点错位置）。
            # 规则：命中文本比目标长出 4 字以上就视为不可信，只保留长度接近的。
            _tl = len(target_text.strip())
            near = [c for c in cands
                    if len((c.get("text") or "").strip()) <= _tl + 3]
            if near:
                cands = near
            else:
                dropped = [c.get("text") for c in cands][:3]
                _REJECT_STATS['count'] = _REJECT_STATS.get('count', 0) + 1
                # 详情进后台文件；前端只保留「等待命中」一行（不刷屏）
                logger.log_to_file(f"[整帧OCR] '{target_text}' 只命中过长文本 {dropped}，"
                                   f"判定为不可信（可给该图配更完整的 OCR 目标文字）")
                # 交给拼接匹配/兜底路径，不要按"子串命中"点下去
                joined_only = self._match_joined_lines(region_items, target_text, confidence)
                if joined_only is not None:
                    return ("hit", joined_only)
                return ("settled", None) if region_items else None
        pool = exact if exact else cands
        passed = [c for c in pool if c.get("confidence", 0.0) >= confidence]
        chosen = max(passed if passed else pool, key=lambda c: c.get("confidence", 0.0))
        if len(cands) > 1:
            logger.info(f"[整帧OCR] '{target_text}' 命中 {len(cands)} 处，选用置信最高一处"
                        f"（{chosen.get('confidence', 0.0):.4f}）")
        return ("hit", (chosen.get("area"), chosen.get("text"),
                        float(chosen.get("confidence", 0.0))))

    @staticmethod
    def _match_joined_lines(items, target, confidence):
        """把相邻文字行拼起来再判子串，命中返回 (area, text, conf)，否则 None。

        为什么需要：det 对小字号/艺术字的切分不可控（『伤害排名』常被切成
        『伤』+『害排名』）。配置里填的是整词，逐行匹配就永远落空——
        这里按阅读顺序把 2~4 个相邻行拼起来试一次，命中就用这些行的并集框
        当点击区域（比只点一个字更稳）。
        """
        target = (target or "").strip()
        if len(items) < 2 or len(target) < 2:
            return None

        def _box(it):
            a = it.get("area") or []
            xs = [float(p[0]) for p in a]
            ys = [float(p[1]) for p in a]
            if not xs or not ys:
                return None
            return min(xs), min(ys), max(xs), max(ys)

        seq = []
        for it in items:
            b = _box(it)
            if b is not None and (it.get("text") or "").strip():
                seq.append((it, b, (it.get("text") or "").strip()))
        if len(seq) < 2:
            return None
        # 阅读顺序：按「行聚类」（竖直重叠算同一行，行内按 x 排）——
        # 2x2 排版（召 唤 / 鬼 王）用 y/常数分桶会把『鬼』『王』排反
        _rows = []
        for entry in sorted(seq, key=lambda p: p[1][1]):
            for row in _rows:
                ry1 = min(x[1][1] for x in row)
                ry2 = max(x[1][3] for x in row)
                overlap = min(entry[1][3], ry2) - max(entry[1][1], ry1)
                if overlap > 0.5 * min(entry[1][3] - entry[1][1], ry2 - ry1):
                    row.append(entry)
                    break
            else:
                _rows.append([entry])
        _rows.sort(key=lambda r: min(x[1][1] for x in r))
        seq = []
        for row in _rows:
            row.sort(key=lambda p: p[1][0])
            seq.extend(row)

        for span in (2, 3, 4):
            for i in range(len(seq) - span + 1):
                grp = seq[i:i + span]
                joined = "".join(t[2] for t in grp)
                if target not in joined:
                    continue
                # 长度防护（和单行候选同一套规则）：拼接结果比目标长出 3 字以上
                # 说明是「一整句里恰好含目标词」（实测：顶部系统横幅
                # 「5550正要挑战集结·BOSS稀有·蜃气楼，」含『挑战』→ 点到横幅）。
                # 多行按钮的拼接结果基本等于目标本身，不会被这条卡掉。
                if len(joined) > len(target) + 3:
                    _REJECT_STATS['count'] = _REJECT_STATS.get('count', 0) + 1
                    logger.log_to_file(f"[整帧OCR] 目标 '{target}' 拼接命中过长文本"
                                       f"「{joined[:24]}…」（{len(joined)}字），判定不可信")
                    continue
                x1 = min(t[1][0] for t in grp)
                y1 = min(t[1][1] for t in grp)
                x2 = max(t[1][2] for t in grp)
                y2 = max(t[1][3] for t in grp)
                try:
                    conf = min(float(t[0].get("confidence", 0.0)) for t in grp)
                except Exception:
                    conf = 0.0
                logger.info(f"[整帧OCR] 目标 '{target}' 由 {span} 行拼接命中"
                            f"「{joined}」（最低置信 {conf:.2f}）")
                area = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
                return area, joined, conf
        return None

    def _result_from_text_area(self, screenshot, text_area, real_text, ocr_conf):
        """把整帧/裁剪两种来源的文字框统一换算成客户区 region 并构造 RecognitionResult。"""
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
            time.sleep(random.uniform(*MOVE_DELAY_RANGE))   # 移动→按下 50~60ms 随机
            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, l_param)
            time.sleep(random.uniform(*CLICK_HOLD_RANGE))   # 按下时长 200~250ms 随机
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
            time.sleep(random.uniform(*MOVE_DELAY_RANGE))   # 按下→开始移动 50~60ms 随机
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
