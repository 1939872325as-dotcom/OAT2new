import os
import random
import time
from typing import Dict, List, Optional

from OAT.utils.do_after_challenge import do_after_challenge
from OAT.utils.logging import logger
from OAT.utils.pause_state import is_stale, pause_aware_sleep, wait_if_paused
from .recognition import RecognitionEngine
from .task_definition import Task, parse_pipeline


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
        self.consecutive_count: Dict[str, int] = {}
        self.retry_count: int = 0
        self.next_start_time: Optional[float] = None
        self.current_task: Optional[Task] = None
        self._last_matched_task: Optional[Task] = None
        self._last_matched_task_name: Optional[str] = None

    def run(self, times: int) -> bool:
        """执行挑战主循环"""
        # 各图在「编辑图像配置」里可单独设置点击后随机等待；这里只提示默认区间
        _lo, _hi = self._click_wait_range()
        _custom = [t.name for t in self.tasks
                   if getattr(t, "click_wait_min", None) is not None]
        logger.info(f"点击后随机等待：默认 {_lo:g}~{_hi:g} 秒"
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
                self._check_global_tasks()

                if self.current_next and self.current_next in self._task_map:
                    if self._process_next_task():
                        # 注意：_match_and_execute 已经把 current_next 改成它自己的 next 了，
                        # 所以这里必须取「刚刚匹配到的任务」。原实现写的是
                        # self._task_map[self.current_next] —— 拿到的是下一张任务（is_start 判错），
                        # 而且当 next 为空时 self._task_map[None] 会 KeyError，直接崩掉整个挑战。
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
                else:
                    if self._process_non_global_tasks():
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
                # 两次识别之间的随机休眠（暂停不计入休眠，停止则干净退出）
                try:
                    _sleep_target = random.uniform(1.0, 2.0)
                except Exception:
                    _sleep_target = 1.0
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

    def _check_global_tasks(self) -> None:
        """优先检测所有全局任务"""
        for task in self._global_tasks:
            if self._match_and_execute(task):
                self.consecutive_count.clear()
                self.retry_count = 0
                self.next_start_time = None

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
            return True
        return False

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

    def _match_and_execute(self, task: Task) -> bool:
        """对单个任务执行识别 → 点击 → 状态更新"""
        self.current_task = task
        result = self._recognize(task)
        if not result.found:
            return False

        via = f"OCR('{result.text}')" if result.text else "模板匹配"
        logger.info(f"匹配到: {task.name}（方式: {via}，置信度: {result.confidence:.3f}，"
                    f"识别区域: {result.region}，"
                    f"设定: 阈值={task.threshold} ocr阈值={task.ocr_confidence} "
                    f"下一张={task.next or '无'}{'，挑战起点' if task.is_start else ''}）")

        # 上一轮等它的 next 等超时了、现在它又匹配到：说明上一步的点击没生效
        if getattr(self, "_timed_out_next", None) == task.name:
            logger.warning(f"任务 {task.name} 再次匹配到，但它设定的下一张「{task.next}」之前已超时未出现："
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

        self._execute_action(task, result)
        self._last_matched_task = task

        # 同步器通知
        if self.synchronizer and hasattr(self.synchronizer, "on_pipeline_task"):
            self.synchronizer.on_pipeline_task(task.name)

        # 更新 next 状态
        if task.next:
            self.current_next = task.next
        else:
            self.current_next = None

        self.next_start_time = None
        return True

    def _recognize(self, task: Task):
        """根据任务配置选择识别方式"""
        full_path = os.path.join(self.script_dir, task.template)

        if task.recognition == "ocr":
            if task.ocr_region:
                logger.info(f"任务 {task.name}：OCR 限定搜索区域 {list(task.ocr_region)}")
            return self.engine.find_text(task.ocr_text or "", task.ocr_confidence,
                                         region=task.ocr_region)
        elif task.recognition == "template+ocr":
            result = self.engine.find_template(full_path, task.threshold)
            if not result.found and task.ocr_text:
                # 模板失败会静默退到 OCR —— 必须显式说明，否则用户只看到“匹配到”，
                # 完全不知道走的不是模板（模板点击位置精确，OCR 命中的可能只是同名文字）
                logger.warning(f"模板未匹配，改用 OCR 兜底：任务 {task.name}，模板 {task.template}，"
                               f"OCR目标文字 '{task.ocr_text}'")
                if task.ocr_region:
                    logger.info(f"任务 {task.name}：OCR 限定搜索区域 {list(task.ocr_region)}")
                result = self.engine.find_text(task.ocr_text, task.ocr_confidence,
                                               region=task.ocr_region)
            return result
        else:
            return self.engine.find_template(full_path, task.threshold)

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
        没配则回落到默认 0.3~5 秒。
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
            lo, hi = 0.3, 5.0
        if lo < 0:
            lo = 0.0
        if hi < 0:
            hi = 0.0
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi

    def _sleep_after_click(self, task: Optional[Task] = None) -> None:
        """点击后的随机等待。

        区间来自该图的「点击后随机等待」配置（默认 0.3~5 秒），
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

    if threshold is None:
        threshold = settings.FIND_THRESHOLD
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
