import os
import random
import time
from typing import Dict, List, Optional

from OAT.utils.OCRService import ocr_service
from OAT.utils.do_after_challenge import do_after_challenge
from OAT.utils.logging import logger
from OAT.utils.pause_state import is_stale, pause_aware_sleep, wait_if_paused
from .recognition import RecognitionEngine
from .task_definition import Task, parse_pipeline


# 「一张图都没匹配到」时的节流（秒）。匹配到图时，节奏由该图的
# 「点击后随机等待」(click_wait_min/max) 接管，主循环不再额外等待。
#
# 这里替代的是过去写死在主循环末尾的 random.uniform(1.0, 2.0)：
# 那段原本想解决的是「两次识别之间别太密」，但那件事现在交给每张图各自的配置来做；
# 而「一张都没匹配到」这个场景没有图可以负责，若也一并去掉就会全速空转截图，
# 所以保留一段固定节流兜底（值按需调整，与按图配置无关）。
NO_MATCH_WAIT_RANGE = (0.7, 2.0)


class PipelineRunner:
    """MAA 风格管道运行器，替换 CommonChallenge"""

    def __init__(
        self,
        tasks: List[Task],
        engine: RecognitionEngine,
        script_dir: str,
        synchronizer=None,
        hidden_window: bool = False,
        sync_mode: bool = False,
    ):
        self.tasks = tasks
        self.engine = engine
        self.script_dir = script_dir
        self.synchronizer = synchronizer
        self.hidden_window = hidden_window
        self.sync_mode = sync_mode

        self._task_map: Dict[str, Task] = {t.name: t for t in tasks}
        self._global_tasks = [t for t in tasks if t.is_global]
        self._non_global_tasks = [t for t in tasks if not t.is_global]

        # 运行时状态
        self.current_next: Optional[str] = None
        # 等待 next 期间的「起点图复检」是否已做过（按 next 名字记，防重复点击）
        self._start_rechecked_for: Optional[str] = None
        self.consecutive_count: Dict[str, int] = {}
        self.retry_count: int = 0
        self.next_start_time: Optional[float] = None
        self.current_task: Optional[Task] = None
        self._last_matched_task: Optional[Task] = None
        self._last_matched_task_name: Optional[str] = None

        # 共享识别帧：一轮内所有任务的识别阶段复用同一帧截图（原来每任务各抓一帧，
        # PrintWindow ~0.7s / BitBlt ~0.1s，任务多时空转一轮光截图就占掉大头）。
        # 安全前提：一轮内只有首个命中任务会执行动作。动作执行 / 停闸等待后画面已变，
        # 必须置脏重新抓帧；新一轮循环开始也强制置脏（帧不跨轮复用）。
        self._shared_frame = None
        self._frame_dirty = True
        # 「等待命中」是否已在当前等待期内提示过：一段连续未命中只提示一次，
        # 命中后重置（避免前端反复出现同样的提示）
        self._wait_logged = False
        self._wait_phase = 0

    def _capture_frame(self):
        """取共享识别帧：脏则重新抓屏，否则复用。抓屏失败返回 None（各识别函数自行兜底）。"""
        if self._frame_dirty:
            try:
                self._shared_frame = self.engine.capture_screenshot()
            except Exception:
                self._shared_frame = None
            self._frame_dirty = False
        return self._shared_frame

    def _invalidate_frame(self) -> None:
        """画面已变（动作/停闸等待/新一轮），下一任务重新抓帧。"""
        self._frame_dirty = True
        self._shared_frame = None

    def run(self, times: int) -> bool:
        """执行挑战主循环"""
        # 各图在「编辑图像配置」里可单独设置点击后随机等待；这里只提示默认区间
        _lo, _hi = self._click_wait_range()
        _custom = [t.name for t in self.tasks
                   if getattr(t, "click_wait_min", None) is not None]
        logger.info(f"点击后随机等待：默认 {_lo:g}~{_hi:g} 秒（节奏由它接管；"
                    f"未匹配到任何图时另有 {NO_MATCH_WAIT_RANGE[0]:g}~{NO_MATCH_WAIT_RANGE[1]:g} 秒节流）"
                    + (f"；已单独配置的图：{', '.join(_custom)}" if _custom else "（可在 编辑图像配置 里按图修改）"))
        try:
            i = 0
            while i < times:
                # 全局协同暂停：暂停时阻塞等待；收到停止请求则干净退出
                try:
                    if wait_if_paused() < 0:
                        try:
                            logger.info("挑战已停止")
                        except Exception:
                            pass
                        return False
                except Exception:
                    pass
                # 代际过期：双起旧线程即使错过 join 也必须退出（不替代上方停止检查）
                try:
                    try:
                        _stale = is_stale()
                    except Exception:
                        _stale = False
                    if _stale:
                        try:
                            logger.info("挑战已停止")
                        except Exception:
                            pass
                        return False
                except Exception:
                    pass
                # 本轮是否真的点到过图：点到过则由该图的「点击后随机等待」主管节奏，
                # 一张都没点到才走末尾那段轻量节流（替代了原来写死的 1.0~2.0 秒）
                # 新一轮开始：共享帧不跨轮复用（上一轮的节流等待/动作后画面可能已变）
                self._invalidate_frame()

                if self.SCAN_MODE == "legacy":
                    acted = self._check_global_tasks()
                    if self.current_next and self.current_next in self._task_map:
                        if self._process_next_task():
                            acted = True
                    else:
                        if self._process_non_global_tasks():
                            acted = True
                else:
                    # 并行模式：一帧内并发识别全部配置（整帧 OCR 一次 + 模板池并行），
                    # 按「全局 > 流程链下一张 > 其余」择优点击
                    acted = self._process_round()

                if acted:
                    active = self._get_last_matched()
                    if active and active.is_start:
                        i += 1
                        logger.info(f"还剩{times - i}次挑战")
                        try:
                            logger.progress(i, times)
                        except Exception:
                            pass
                        self.consecutive_count.clear()
                        self.retry_count = 0
                # 只有「一张图都没匹配到」时才在这里节流（暂停不计入，停止则干净退出）。
                # 匹配到图时节奏已由该图的「点击后随机等待」负责，这里不再叠加 ——
                # 过去写死的 random.uniform(1.0, 2.0) 就是被这一段取代的。
                if not acted:
                    self._log_waiting()
                    try:
                        _sleep_target = random.uniform(*NO_MATCH_WAIT_RANGE)
                    except Exception:
                        _sleep_target = NO_MATCH_WAIT_RANGE[0]
                    try:
                        _sleep_done = pause_aware_sleep(_sleep_target, 0.1)
                    except Exception:
                        _sleep_done = True
                    if not _sleep_done:
                        try:
                            logger.info("挑战已停止")
                        except Exception:
                            pass
                        return False

            logger.info(f"挑战完成！共执行{times}次挑战")
            do_after_challenge(
                getattr(self.engine, "hwnd", None),
                self.synchronizer,
                self.sync_mode,
            )
            return True
        except Exception as e:
            logger.error(f"挑战过程出现致命错误：{e}")
            return False

    def _check_global_tasks(self) -> bool:
        """优先检测所有全局任务；返回本轮是否点到过图。

        返回值供主循环判断「本轮节奏是否已由按图配置的随机等待接管」：
        全局图被点过也算点过，此时不应再叠加末尾的未匹配节流。
        """
        acted = False
        for task in self._global_tasks:
            if self._match_and_execute(task):
                acted = True
                self.consecutive_count.clear()
                self.retry_count = 0
                self.next_start_time = None
        return acted

    # ---- 一帧并行扫描：整帧 OCR + 所有模板同时识别，择优点击 ----

    # 并行扫描开关：parallel=一帧内并发识别全部配置（默认）；legacy=旧的顺序扫描。
    # 出问题时可用环境变量 OAT_SCAN_MODE=legacy 一键退回，不必改代码。
    SCAN_MODE = (os.environ.get("OAT_SCAN_MODE") or "parallel").strip().lower()

    def _scan_frame_parallel(self, shot):
        """在同一帧上并发评估所有任务，返回 [(task, result)]（仅命中的）。

        为什么这么改：原来顺序扫描时，流程链等待期间只测一个任务（19:19 实测：
        画面还有「挑战」却死等「点击屏幕继续」12 秒）；顺序扫 N 个模板也是线性
        叠加。现在一帧内：整帧 OCR 起一次（与模板并行），模板匹配丢线程池并行，
        模板失败的再复用那份 OCR 结果——一次扫描拿到全部配置的结论。
        """
        tasks = list(self.tasks)
        if not tasks or shot is None:
            return []

        results = {}
        # 先起 OCR（整帧一次、与模板并行；模板+OCR 任务后续复用这份结果）
        ocr_future = None
        try:
            from concurrent.futures import ThreadPoolExecutor
            workers = max(2, min(8, (os.cpu_count() or 4) - 1))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                if any((t.recognition or "") in ("ocr", "template+ocr")
                       and (t.ocr_text or "") for t in tasks):
                    ocr_future = ex.submit(
                        ocr_service.recognize_frame_items, shot)

                fut_map = {}
                for t in tasks:
                    rec = (t.recognition or "").lower()
                    if rec == "ocr":
                        continue                      # 走 OCR，单独提交
                    path = os.path.join(self.script_dir, t.template)
                    fut_map[t.name] = ex.submit(
                        self.engine.find_template, path, t.threshold, None, shot,
                        getattr(t, "display", None) or t.name)

                # 纯 OCR 任务：与模板并行提交
                for t in tasks:
                    if (t.recognition or "").lower() == "ocr":
                        fut_map[t.name] = ex.submit(
                            self.engine.find_text, t.ocr_text or "",
                            t.ocr_confidence, t.ocr_region, shot)

                for t in tasks:
                    rec = (t.recognition or "").lower()
                    fut = fut_map.get(t.name)
                    try:
                        result = fut.result() if fut else None
                    except Exception as e:
                        logger.warning(f"并行识别失败（任务 {t.name}）: {e}")
                        result = None
                    if result is not None and result.found:
                        results[t.name] = result
                        continue
                    # 模板没命中 → 用同一帧的整帧 OCR 结果兜底（0 次额外推理）
                    if rec == "template+ocr" and (t.ocr_text or ""):
                        if ocr_future is not None:
                            try:
                                ocr_future.result()   # 确保整帧 OCR 已就绪（缓存）
                            except Exception:
                                pass
                        try:
                            r2 = self.engine.find_text(
                                t.ocr_text, t.ocr_confidence, t.ocr_region, shot)
                        except Exception as e:
                            logger.warning(f"OCR 兜底失败（任务 {t.name}）: {e}")
                            r2 = None
                        if r2 is not None and r2.found:
                            logger.log_to_file(f"模板未匹配，改用 OCR 兜底：任务 "
                                               f"{getattr(t, 'display', None) or t.name}，"
                                               f"OCR目标文字 '{t.ocr_text}'")
                            results[t.name] = r2
        except Exception as e:
            logger.warning(f"并行扫描不可用，退回顺序识别: {e}")
            for t in tasks:
                try:
                    r = self._recognize(t, shot)
                except Exception:
                    r = None
                if r is not None and r.found:
                    results[t.name] = r
        return [(t, results[t.name]) for t in tasks if t.name in results]

    def _log_waiting(self, extra: str = "") -> None:
        """一轮没命中时，更新「等待命中」状态行（单行原地刷新，不刷屏）。

        前端只有一行，每轮带新的识别时间（动态感）；每张模板的分数、被判
        「不可信」的过长文本等明细都只写入后台日志文件。命中后清除这一行。
        """
        wait_name = ""
        if self.current_next:
            t = self._task_map.get(self.current_next)
            if t is not None:
                wait_name = f" · 正在等「{getattr(t, 'display', None) or t.name}」"
        try:
            total = len(self.tasks)
            ts = time.strftime('%H:%M:%S')
            tail = f" · {extra}" if extra else ""
            logger.status(f"等待命中：{total} 张图像均未匹配{wait_name}"
                          f" · 最近识别 {ts}{tail}")
        except Exception:
            pass

    def _clear_waiting(self) -> None:
        """命中/流程变化后清除「等待命中」状态行。"""
        try:
            logger.status_clear()
        except Exception:
            pass

    def _pick_best(self, hits):
        """按「全局 > 流程链下一张 > 其余」分层，同层取置信度最高。"""
        if not hits:
            return None
        def tier(task):
            if getattr(task, "is_global", False):
                return 0
            if self.current_next and task.name == self.current_next:
                return 1
            return 2
        hits.sort(key=lambda p: (tier(p[0]), -float(p[1].confidence or 0.0)))
        return hits[0]

    def _process_round(self) -> bool:
        """一轮 = 抓一帧 → 并行识别全部配置 → 择优点击。返回本轮是否点到。

        流程链（current_next）仍然生效：它排在第 1 层优先级，命中就优先点它；
        若它没出现而别的图命中，按实际画面点击并记录「流程断链」告警，
        避免像旧逻辑那样为一个不出现的下一张空等超时。
        """
        task_next = self._task_map.get(self.current_next) if self.current_next else None
        if task_next is not None:
            if self.next_start_time is None:
                self.next_start_time = time.time()
            if time.time() - self.next_start_time > task_next.timeout:
                logger.warning(f"识别超时：{getattr(task_next, 'display', None) or task_next.name}"
                               f"（{task_next.timeout} 秒内未出现），回到默认识别模式")
                self._timed_out_next = task_next.name
                self.current_next = None
                self.next_start_time = None
                self._start_rechecked_for = None
                self.consecutive_count.clear()
                self.retry_count = 0

        shot = self._capture_frame()
        if shot is None:
            return False

        hits = self._scan_frame_parallel(shot)
        chosen = self._pick_best(hits)
        if chosen is None:
            # 被判「不可信」的过长文本条数（如命中聊天长句）只体现在状态行，
            # 明细已进后台文件，避免前端刷屏
            try:
                from .recognition_opencv import take_rejected_count
                rejected = take_rejected_count()
            except Exception:
                rejected = 0
            extra = (f"{rejected} 处文字因过长被排除（详见后台日志）"
                     if rejected else "")
            self._log_waiting(extra)
            return False

        task, result = chosen
        if task_next is not None and task.name != task_next.name:
            logger.warning(f"流程断链：本该等「{getattr(task_next, 'display', None) or task_next.name}」，"
                           f"实际命中「{getattr(task, 'display', None) or task.name}」"
                           f"（置信度 {result.confidence:.3f}）——按画面实际内容优先处理")
            self.current_next = None
            self.next_start_time = None

        if not self._match_and_execute(task, pre_result=result):
            return False
        if task.name != self._last_matched_task_name:
            self.retry_count = 0
        self._update_consecutive(task.name)
        if self._should_abort(task.name):
            return False
        self._last_matched_task_name = task.name
        self.next_start_time = None
        self._start_rechecked_for = None
        return True

    def _process_next_task(self) -> bool:
        """处理 current_next 指定的任务，返回是否匹配成功"""
        task = self._task_map[self.current_next]
        if self.next_start_time is None:
            self.next_start_time = time.time()

        if time.time() - self.next_start_time > task.timeout:
            logger.warning(f"识别超时：{task.name}（{task.timeout} 秒内未出现），回到默认识别模式")
            self._timed_out_next = task.name
            self.current_next = None
            self.next_start_time = None
            self.consecutive_count.clear()
            self.retry_count = 0
            return False

        if self._match_and_execute(task):
            if task.name != self._last_matched_task_name:
                self.retry_count = 0
            self._update_consecutive(task.name)
            if self._should_abort(task.name):
                return False
            self._last_matched_task_name = task.name
            self.next_start_time = None
            self._start_rechecked_for = None
            return True

        # 等待 next 期间不能纯盲等：若画面仍停在「起点图」（如「挑战」），说明
        # 上一步点击根本没生效，继续干等只会在超时前空转（实测 19:19 就是等了
        # 12 秒、每轮只测任务2，画面上的挑战一直被忽略）。这里在宽限期后复检
        # 一次起点任务，命中就重置流程，交给下一轮正常扫描重新点击它。
        self._recheck_start_while_waiting()
        return False

    # 等待 next 多少秒后才做起点图复检：太短会把正常转场动画误判成"没点动"
    WAIT_START_RECHECK_DELAY = 3.0

    def _recheck_start_while_waiting(self) -> None:
        """等待 next 期间复检起点任务（每个 next 只复检一次，避免反复点击）。"""
        if self.next_start_time is None:
            return
        if self._start_rechecked_for == self.current_next:
            return                                  # 这个 next 已经复检过
        if time.time() - self.next_start_time < self.WAIT_START_RECHECK_DELAY:
            return
        self._start_rechecked_for = self.current_next

        for t in self.tasks:
            if not getattr(t, "is_start", False) or t.name == self.current_next:
                continue
            shot = self._capture_frame()
            if shot is None:
                continue
            try:
                result = self._recognize(t, shot)
            except Exception as e:
                logger.debug(f"等待期起点复检失败（{t.name}）: {e}")
                continue
            if not result.found:
                continue
            logger.warning(
                f"等待「{self.current_next}」期间画面仍显示「{t.name}」"
                f"（{result.confidence:.3f}）：上一步点击很可能没有生效，"
                f"重置流程并重新处理 {t.name}")
            self.current_next = None
            self.next_start_time = None
            self._start_rechecked_for = None
            self.consecutive_count.clear()
            self.retry_count = 0
            return

    def _process_non_global_tasks(self) -> bool:
        """遍历所有非全局任务，返回是否匹配到任一任务"""
        for task in self._non_global_tasks:
            if self._match_and_execute(task):
                if task.name != self._last_matched_task_name:
                    self.retry_count = 0
                self._update_consecutive(task.name)
                if self._should_abort(task.name):
                    return False
                self._last_matched_task_name = task.name
                return True
        return False

    def _get_last_matched(self) -> Optional[Task]:
        return getattr(self, "_last_matched_task", None)

    def _match_and_execute(self, task: Task, pre_result=None) -> bool:
        """对单个任务执行识别 → 点击 → 状态更新。

        pre_result：并行扫描已经算好的识别结果；传入时跳过重复识别
        （否则并行扫描 + 这里再扫一遍等于双倍开销）。
        """
        self.current_task = task
        shot = self._capture_frame()
        result = pre_result if pre_result is not None else self._recognize(task, shot)
        if result is None or not result.found:
            return False

        self._clear_waiting()
        via = f"OCR('{result.text}')" if result.text else "模板匹配"
        logger.info(f"匹配到: {getattr(task, 'display', None) or task.name}（方式: {via}，置信度: {result.confidence:.3f}，"
                    f"识别区域: {result.region}，"
                    f"设定: 阈值={task.threshold} ocr阈值={task.ocr_confidence} "
                    f"下一张={task.next or '无'}{'，挑战起点' if task.is_start else ''}）")

        # 上一轮等它的 next 等超时了、现在它又匹配到：说明上一步的点击没生效
        if getattr(self, "_timed_out_next", None) == task.name:
            logger.warning(f"任务 {getattr(task, 'display', None) or task.name} 再次匹配到，"
                           f"上一步的点击很可能没有生效（或该步本就不需要点击）")
            self._timed_out_next = None

        # OCR 是子串匹配：目标是短词时很容易命中“更长但不同位置”的另一处文字
        # （典型：目标“退治”，却命中了页面标题“妖怪退治”）。这种情况必须点名，
        # 否则日志只显示“匹配到”，看不出点错了地方。
        if result.text and task.ocr_text and result.text.strip() != task.ocr_text.strip():
            logger.warning(f"注意：OCR 目标是 '{task.ocr_text}'，实际命中的是更长的文字块 "
                           f"'{result.text}'（子串匹配），命中区域 {result.region}。"
                           f"若这不是你要点的位置，请把 OCR 目标文字改成完整文字，"
                           f"或用「OCR搜索区域」限定范围")

        # 停止点击闸（优先级高于动作执行）：任一停止条件图出现在画面上 →
        # 本轮不执行该任务的动作，按「停止时间」等待后按「未匹配」处理
        # （不更新 next / 不通知同步器 / 不计入连续命中）。
        if task.stop_click_enabled and task.stop_images:
            _hit = self._find_stop_image(task, shot)
            if _hit:
                try:
                    _seconds = max(0.0, float(task.stop_time))
                except (ValueError, TypeError):
                    _seconds = 5.0
                logger.info(f"停止条件命中（图 {os.path.basename(_hit)} 出现），"
                            f"本轮不执行任务 {task.name} 的动作，停止 {_seconds:g} 秒")
                if _seconds > 0:
                    try:
                        if not pause_aware_sleep(_seconds, 0.1):
                            return False
                    except Exception:
                        time.sleep(_seconds)
                # 停闸等待期间画面可能已变（暂停可能停了很久）：共享帧作废
                self._invalidate_frame()
                return False

        # 暂停检查点：识别完成后、执行点击前再确认一次。循环顶部的检查点只拦
        # 下一轮，拦不住本轮收尾——暂停请求落在识别阶段（模板×N + OCR 兜底，
        # 可长达数秒）时，若不在这里等一下，机器人会多点击一次才停。
        # 收到停止请求则返回 False，由外层按「未匹配」路径干净退出。
        try:
            if wait_if_paused() < 0:
                return False
        except Exception:
            pass

        self._execute_action(task, result)
        # 动作已执行（含点击后随机等待），画面已变：共享帧作废
        self._invalidate_frame()
        self._last_matched_task = task

        # 同步器通知
        if self.synchronizer and hasattr(self.synchronizer, "on_pipeline_task"):
            self.synchronizer.on_pipeline_task(task.name)

        # 更新 next 状态
        if task.next:
            self.current_next = task.next
        else:
            self.current_next = None
        self._start_rechecked_for = None      # 新一段等待，允许重新复检起点图

        self.next_start_time = None
        return True

    def _find_stop_image(self, task: Task, shot=None) -> Optional[str]:
        """按配置顺序检查停止条件图，返回第一个命中的相对路径；都没命中返回 None。

        所有停止图与模板/OCR 识别共用同一帧（shot 为共享识别帧），逐张各抓一帧太贵；
        识别失败按未命中处理不阻断流程。
        """
        if not task.stop_images:
            return None
        if shot is None:
            try:
                shot = self.engine.capture_screenshot()
            except Exception:
                shot = None
        for rel in task.stop_images:
            full_path = os.path.join(self.script_dir, rel)
            try:
                if self.engine.find_template(full_path, task.threshold, screenshot=shot).found:
                    return rel
            except Exception as e:
                logger.warning(f"停止图识别失败（按未命中处理）: {rel}: {e}")
        return None

    def _recognize(self, task: Task, shot=None):
        """根据任务配置选择识别方式（shot 为共享识别帧，None 时各识别函数自行抓屏）"""
        full_path = os.path.join(self.script_dir, task.template)

        if task.recognition == "ocr":
            if task.ocr_region:
                logger.debug(f"任务 {task.name}：OCR 限定搜索区域 {list(task.ocr_region)}")
            return self.engine.find_text(task.ocr_text or "", task.ocr_confidence,
                                         region=task.ocr_region, screenshot=shot)
        elif task.recognition == "template+ocr":
            # 同一任务只抓一帧：模板没匹配上时，OCR 兜底复用这张图。
            # 共享帧由 _capture_frame 提供；共享帧缺失时此处自行抓一张并兜底复用。
            if shot is None:
                try:
                    shot = self.engine.capture_screenshot()
                except Exception:
                    shot = None
            result = self.engine.find_template(full_path, task.threshold, screenshot=shot,
                                               display=getattr(task, "display", None))
            if not result.found and task.ocr_text:
                # 模板失败会静默退到 OCR —— 必须显式说明，否则用户只看到“匹配到”，
                # 完全不知道走的不是模板（模板点击位置精确，OCR 命中的可能只是同名文字）
                logger.log_to_file(f"模板未匹配，改用 OCR 兜底：任务 "
                                   f"{getattr(task, 'display', None) or task.name}，"
                                   f"OCR目标文字 '{task.ocr_text}'")
                if task.ocr_region:
                    logger.debug(f"任务 {task.name}：OCR 限定搜索区域 {list(task.ocr_region)}")
                result = self.engine.find_text(task.ocr_text, task.ocr_confidence,
                                               region=task.ocr_region, screenshot=shot)
            return result
        else:
            return self.engine.find_template(full_path, task.threshold, screenshot=shot,
                                             display=getattr(task, "display", None))

    def _execute_action(self, task: Task, result) -> None:
        """执行任务匹配后的动作（click / swipe / wait，pipeline 专属）"""
        action_type = (task.action.type or "click")
        logger.info(f"执行动作: {action_type}（点击目标类型: {task.action.target}）")
        if action_type == "swipe":
            coords = self._resolve_click_coords(task, result)
            if coords is None:
                return
            x1, y1 = coords
            x2, y2 = x1, y1
            if task.action.ex_range and task.action.ey_range:
                try:
                    x2 = random.randint(int(task.action.ex_range[0]), int(task.action.ex_range[1]))
                except (ValueError, TypeError):
                    x2 = x1
                try:
                    y2 = random.randint(int(task.action.ey_range[0]), int(task.action.ey_range[1]))
                except (ValueError, TypeError):
                    y2 = y1
            duration = task.action.duration if task.action.duration is not None else 0.5
            logger.info(f"后台滑动 ({x1},{y1}) → ({x2},{y2})，时长 {duration}s")
            self.engine.swipe(int(x1), int(y1), int(x2), int(y2),
                              duration=duration, sync_mode=self.sync_mode)
            # 滑动也要有节奏：主循环的 1.0~2.0 秒移除后，否则滑动完立刻又去识别
            self._sleep_after_click(task)
        elif action_type == "wait":
            raw = task.action.duration if task.action.duration is not None else 1.0
            try:
                seconds = float(raw)
            except (ValueError, TypeError):
                seconds = 1.0
            seconds = max(0.0, seconds)
            logger.info(f"等待 {seconds} 秒")
            time.sleep(seconds)
        else:
            coords = self._resolve_click_coords(task, result)
            if coords is None:
                return
            x, y = coords
            logger.info(f"后台点击 ({x}, {y})")
            self.engine.click(x, y, sync_mode=self.sync_mode)
            self._sleep_after_click(task)

    def _click_wait_range(self, task: Optional[Task] = None) -> tuple:
        """点击后随机等待区间（秒）。

        优先用该任务自己的配置（在「编辑图像配置」里按图设置），
        没配则回落到默认 0.8~5 秒。
        """
        lo = hi = None
        if task is not None:
            lo = getattr(task, "click_wait_min", None)
            hi = getattr(task, "click_wait_max", None)
        try:
            lo = float(lo) if lo is not None else None
            hi = float(hi) if hi is not None else None
        except (ValueError, TypeError):
            lo = hi = None
        if lo is None or hi is None:
            lo, hi = 0.8, 5.0
        if lo < 0:
            lo = 0.0
        if hi < 0:
            hi = 0.0
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi

    def _sleep_after_click(self, task: Optional[Task] = None) -> None:
        """点击后的随机等待。

        区间来自该图的「点击后随机等待」配置（默认 0.8~5 秒），
        作用：模拟人工节奏，避免固定间隔点击被识别为脚本。
        暂停不计入休眠、停止则立即返回；每次都把实际秒数写进日志便于核对。
        """
        lo, hi = self._click_wait_range(task)
        try:
            seconds = random.uniform(lo, hi)
        except Exception:
            seconds = lo
        _who = f"任务 {task.name}" if task is not None else "默认"
        logger.info(f"点击后随机等待 {seconds:.2f} 秒（{_who} 区间 {lo:g}~{hi:g}）")
        if seconds <= 0:
            return
        _start = time.time()
        try:
            # 与主循环一致：用 pause_aware_sleep，暂停时不计入等待时间
            done = pause_aware_sleep(seconds, 0.1)
        except Exception:
            time.sleep(seconds)
            done = True
        _actual = time.time() - _start
        if not done:
            logger.info(f"等待被中断（已等待 {_actual:.2f} 秒，挑战已停止）")
        elif _actual > seconds + 0.5:
            # 实际远大于目标：多半是中途被暂停过，记一下便于排查
            logger.info(f"实际等待 {_actual:.2f} 秒（含暂停时间），目标 {seconds:.2f} 秒")

    def _resolve_click_coords(self, task: Task, result):
        """确定点击坐标（客户区逻辑坐标）；无法确定时返回 None 并记录原因"""
        # OCR 点击区域优先：文字只是「判断到了某个界面」，真正要点的地方可能在别处
        _occ = getattr(task, "ocr_click_area", None)
        if _occ and len(_occ) == 4:
            try:
                x1, x2, y1, y2 = (int(v) for v in _occ)
                lo_x, hi_x = min(x1, x2), max(x1, x2)
                lo_y, hi_y = min(y1, y2), max(y1, y2)
                return (random.randint(lo_x, hi_x), random.randint(lo_y, hi_y))
            except (ValueError, TypeError) as e:
                logger.error(f"任务 {task.name} 的 OCR 点击区域无效"
                             f"（ocr_click_area={_occ}）: {e}，回退到识别区域")

        if task.action.target == "fixed_coord" and task.action.x_range and task.action.y_range:
            try:
                return (
                    random.randint(int(task.action.x_range[0]), int(task.action.x_range[1])),
                    random.randint(int(task.action.y_range[0]), int(task.action.y_range[1])),
                )
            except (ValueError, TypeError, IndexError) as e:
                logger.error(f"任务 {task.name} 的固定点击区域无效"
                             f"（x_range={task.action.x_range}, y_range={task.action.y_range}）: {e}")
                return None
        # matched / ocr_area：用识别到的区域
        if result.region:
            # OCR 返回的 region 可能是 float，randint/位运算要求 int，这里统一转换
            rx, ry, rw, rh = (int(v) for v in result.region)
            rw, rh = max(0, rw), max(0, rh)
            if rw <= 0 or rh <= 0:
                logger.error(f"任务 {task.name} 的识别区域无效: {result.region}，已跳过点击")
                return None
            cx = rx + rw // 2
            cy = ry + rh // 2
            range_x = rw // 3
            range_y = rh // 3
            lo_x, hi_x = max(rx, cx - range_x), min(rx + rw, cx + range_x)
            lo_y, hi_y = max(ry, cy - range_y), min(ry + rh, cy + range_y)
            # 防御：int 截断后理论上 lo<=hi，若异常则回退到中心点
            if lo_x > hi_x:
                lo_x, hi_x = cx, cx
            if lo_y > hi_y:
                lo_y, hi_y = cy, cy
            return (
                random.randint(lo_x, hi_x),
                random.randint(lo_y, hi_y),
            )
        # 既没有可用的固定区域、也没有识别区域。原来这里静默返回 (0, 0)，
        # 会点到窗口左上角 —— 表现就是“日志说成功、实际点了个没用的地方”。
        logger.error(f"任务 {task.name}: 无法确定点击坐标"
                     f"（target={task.action.target}，固定区域={task.action.x_range}/{task.action.y_range}，"
                     f"识别区域={result.region}），已跳过点击；"
                     f"请在图片配置里设置「点击区域」，或确认该识别方式能返回区域")
        return None

    def _update_consecutive(self, task_name: str) -> None:
        self.consecutive_count[task_name] = self.consecutive_count.get(task_name, 0) + 1

    def _should_abort(self, task_name: str) -> bool:
        count = self.consecutive_count.get(task_name, 0)
        try:
            budget = int(getattr(self._task_map.get(task_name), "max_consecutive", 5))
        except (ValueError, TypeError, AttributeError):
            budget = 5
        if budget <= 0:
            budget = 5
        if count >= budget:
            logger.error(f"图片 {task_name} 已连续出现{budget}次，强制停止")
            return True
        if count >= 2:
            self.retry_count += 1
            if self.retry_count >= budget:
                logger.error(f"重试{budget}次后图片 {task_name} 仍然存在，结束挑战")
                return True
        return False


def create_and_run_pipeline(
    config: dict,
    script_dir: str,
    window_title: str,
    hidden_window: bool = False,
    sync_mode: bool = False,
    synchronizer=None,
    sync_mode_value: str = "exactly_sync",
    threshold: int = None,
    find_mode: str = None,
    times: int = 1,
    window_hwnd: int = None,
) -> bool:
    """便捷函数：解析配置 → 创建引擎 → 创建运行器 → 执行

    供 source/__init__.py 中的 mode_choice() 调用。
    """
    from OAT.tools import settings

    # 不在这里把 settings.FIND_THRESHOLD 取成固定值：传 None 下去，让识别引擎
    # 每轮现读设置，这样设置页拖「识别阈值」能对正在跑的挑战实时生效。
    if find_mode is None:
        find_mode = settings.FIND_MODE

    # 获取窗口句柄（显式句柄优先：改名/多开场景下比标题可靠）
    hwnd = None
    try:
        import win32gui

        if window_hwnd and win32gui.IsWindow(int(window_hwnd)):
            hwnd = int(window_hwnd)
        if not hwnd:
            hwnd = win32gui.FindWindow(None, window_title)
    except Exception:
        pass

    engine = OpenCVRecognitionEngine(
        hwnd=hwnd,
        threshold=threshold,
        find_mode=find_mode,
        synchronizer=synchronizer,
        hidden_window=hidden_window,
    )
    logger.info(f"管道识别模式: {'后台' if hidden_window else '前台'}")

    pipeline_data = config.get("pipeline", config)
    tasks = parse_pipeline(pipeline_data)

    runner = PipelineRunner(
        tasks=tasks,
        engine=engine,
        script_dir=script_dir,
        synchronizer=synchronizer,
        hidden_window=hidden_window,
        sync_mode=sync_mode,
    )
    return runner.run(times)


# 延迟导入避免循环引用
from .recognition_opencv import OpenCVRecognitionEngine  # noqa: E402
