"""点击后随机等待接管主循环节奏（替换掉写死的 1.0~2.0 秒）。

背景：主循环末尾原来各写死一份 random.uniform(1.0, 2.0)（pipeline/runner.py
与 source/common_challenge.py），节奏对每张图都一样、也改不了。现在移除，
改由每张图的「点击后随机等待」(click_wait_min/max，默认 0.3~5) 主管；
只有「一张图都没匹配到」时才用 NO_MATCH_WAIT_RANGE 做轻量节流兜底 ——
否则等界面时就会全速空转截图。

覆盖三件事：
  1) 两条路径对同一份配置解析出的区间一致（含只填一个、写反、负数、非法）；
  2) 匹配到图时：只等该图自己的区间，不再叠加 1.0~2.0，也不走未匹配节流；
  3) 一张都没匹配到时：走轻量节流。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from OAT.pipeline import runner as runner_mod
from OAT.pipeline.recognition import RecognitionResult
from OAT.pipeline.runner import NO_MATCH_WAIT_RANGE, PipelineRunner
from OAT.pipeline.task_definition import parse_pipeline
from OAT.tools import OnmyojiAuto as auto_mod
from OAT.tools.OnmyojiAuto import DEFAULT_CLICK_WAIT_RANGE, resolve_click_wait_range


# ---------- 测试替身 ----------

class _FakeEngine:
    """只回答「识别到 / 没识别到」，并记录点击；不碰真实窗口。"""

    def __init__(self, found_after=0, region=(100, 100, 30, 30)):
        self.found_after = found_after  # 前 N 次识别返回「没找到」
        self.calls = 0
        self.clicks = []
        self.swipes = []
        self._region = region

    def find_template(self, path, threshold=None, **kw):
        self.calls += 1
        found = self.calls > self.found_after
        return RecognitionResult(
            found=found,
            region=self._region if found else None,
            confidence=0.99 if found else 0.0,
        )

    def find_text(self, *a, **kw):
        return self.find_template(None)

    def click(self, x, y, sync_mode=False):
        self.clicks.append((x, y))

    def swipe(self, x1, y1, x2, y2, duration=0.5, sync_mode=False):
        self.swipes.append((x1, y1, x2, y2))


def _record_runner_sleeps():
    """把真实休眠/暂停探测换成记录器，让 run() 瞬间跑完。返回 (记录, 还原函数)。"""
    sleeps = []
    saved = (
        runner_mod.pause_aware_sleep,
        runner_mod.wait_if_paused,
        runner_mod.is_stale,
        runner_mod.do_after_challenge,
    )
    runner_mod.pause_aware_sleep = lambda seconds, interval=0.1: (sleeps.append(seconds), True)[1]
    runner_mod.wait_if_paused = lambda: 0
    runner_mod.is_stale = lambda: False
    runner_mod.do_after_challenge = lambda *a, **kw: None

    def restore():
        (runner_mod.pause_aware_sleep,
         runner_mod.wait_if_paused,
         runner_mod.is_stale,
         runner_mod.do_after_challenge) = saved

    return sleeps, restore


def _run_pipeline(engine, click_wait=(3.0, 3.0), times=1):
    """跑挑战：单个 is_start 任务，命中即计数 +1，返回等待秒数序列。"""
    tasks = parse_pipeline({"tasks": [
        {"name": "t1", "template": "t1.png", "is_start": True,
         "click_wait_min": click_wait[0], "click_wait_max": click_wait[1]},
    ]})
    sleeps, restore = _record_runner_sleeps()
    try:
        PipelineRunner(tasks=tasks, engine=engine, script_dir=".").run(times=times)
    finally:
        restore()
    return sleeps


def _record_auto_sleeps(fn):
    """记录通用挑战路径（OnmyojiAuto）实际等待的秒数。"""
    seen = []
    saved = auto_mod.pause_aware_sleep
    auto_mod.pause_aware_sleep = lambda seconds, interval=0.1: (seen.append(seconds), True)[1]
    try:
        fn()
    finally:
        auto_mod.pause_aware_sleep = saved
    return seen


# ---------- 1) 区间解析：两条路径必须一致 ----------

def test_range_resolution_is_identical_across_both_paths():
    runner = PipelineRunner(tasks=[], engine=object(), script_dir=".")

    # 覆盖：都没填、只填一个、正常、写反、负数、非数字、全零
    cases = [
        (None, None), (2, None), (None, 3), (1, 3), (8, 1),
        (-1, 2), (-5, -1), ("x", "y"), (0, 0), ("", ""),
    ]
    for lo_raw, hi_raw in cases:
        task = parse_pipeline({"tasks": [{
            "name": "t", "template": "t.png",
            "click_wait_min": lo_raw, "click_wait_max": hi_raw,
        }]})[0]
        via_pipeline = runner._click_wait_range(task)
        via_legacy = resolve_click_wait_range(lo_raw, hi_raw)
        assert via_pipeline == via_legacy, (lo_raw, hi_raw, via_pipeline, via_legacy)


def test_partial_config_fills_the_other_side_with_default():
    # 文档口径：只填下限 2 → 2~5（不是整体回落到 0.3~5）
    assert resolve_click_wait_range(2, None) == (2.0, 5.0)
    assert resolve_click_wait_range(None, 3) == (0.3, 3.0)


def test_defaults_when_nothing_configured():
    runner = PipelineRunner(tasks=[], engine=object(), script_dir=".")
    assert runner._click_wait_range() == DEFAULT_CLICK_WAIT_RANGE
    assert resolve_click_wait_range(None, None) == DEFAULT_CLICK_WAIT_RANGE


def test_reversed_and_invalid_are_normalized():
    assert resolve_click_wait_range(8, 1) == (1.0, 8.0)
    assert resolve_click_wait_range("x", "y") == DEFAULT_CLICK_WAIT_RANGE
    assert resolve_click_wait_range(-5, -1) == DEFAULT_CLICK_WAIT_RANGE


def test_pipeline_task_carries_click_wait():
    t = parse_pipeline({"tasks": [{"name": "a", "template": "a.png",
                                   "click_wait_min": 1, "click_wait_max": 3}]})[0]
    assert (t.click_wait_min, t.click_wait_max) == (1.0, 3.0)


# ---------- 2) 命中一张图：只等该图区间 ----------

def test_matched_image_waits_its_own_range_only():
    """命中时只等该图区间：既没有 1.0~2.0，也没有未匹配节流。"""
    sleeps = _run_pipeline(_FakeEngine(found_after=0), click_wait=(3.0, 3.0))
    assert sleeps == [3.0], sleeps


def test_per_image_range_is_honored():
    sleeps = _run_pipeline(_FakeEngine(found_after=0), click_wait=(1.0, 2.0))
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 2.0, sleeps


def test_swipe_action_also_gets_the_wait():
    """滑动后同样要有节奏：主循环 1.0~2.0 移除后，否则滑动完立刻又去识别。"""
    tasks = parse_pipeline({"tasks": [
        {"name": "s1", "template": "s1.png", "is_start": True,
         "click_wait_min": 3.0, "click_wait_max": 3.0,
         "action": {"type": "swipe", "duration": 0.2}},
    ]})
    engine = _FakeEngine(found_after=0)
    sleeps, restore = _record_runner_sleeps()
    try:
        PipelineRunner(tasks=tasks, engine=engine, script_dir=".").run(times=1)
    finally:
        restore()
    assert engine.swipes, "应当发生了滑动"
    assert sleeps == [3.0], sleeps


# ---------- 3) 一张都没匹配到：轻量节流 ----------

def test_no_match_uses_throttle_then_click_takes_over():
    """前两轮没匹配到 → 节流；命中后 → 换成该图区间。"""
    engine = _FakeEngine(found_after=2)  # 第 1、2 次识别不到，第 3 次命中
    sleeps = _run_pipeline(engine, click_wait=(3.0, 3.0))

    assert len(sleeps) == 3, sleeps
    # 未匹配的两轮落在节流区间内（注意：该区间可能与某个按图配置重叠，这里不做
    # 「不在某区间」的全局断言，那种写法会随区间取值变化而误报）
    for s in sleeps[:-1]:
        assert NO_MATCH_WAIT_RANGE[0] <= s <= NO_MATCH_WAIT_RANGE[1], sleeps
    # 命中那一轮只等该图自己的区间（3.0 高于节流区间上界，因此这就证明没叠加节流）
    assert sleeps[-1] == 3.0, sleeps


def test_no_extra_sleep_between_consecutive_matches():
    """连续命中多轮：每轮只有一次等待，中间没有额外的固定休眠。

    这是本次改动的核心回归点。改之前每轮末尾还会再睡 random.uniform(1.0, 2.0)，
    配置成 3~3 时日志会是 3.0 / 1.x / 3.0 / 1.x …；现在应当只有 3.0。
    用 3.0 这个高于节流区间上界的值，能和节流明确区分开。
    """
    sleeps = _run_pipeline(_FakeEngine(found_after=0), click_wait=(3.0, 3.0), times=3)
    assert sleeps == [3.0, 3.0, 3.0], sleeps


# ---------- 4) 通用挑战路径（非管道模式）同样按图配置 ----------

def test_legacy_path_uses_configured_range():
    obj = auto_mod.OnmyojiAutomation.__new__(auto_mod.OnmyojiAutomation)
    assert _record_auto_sleeps(
        lambda: obj._sleep_after_click(4.0, 4.0, label="tiaozhan")) == [4.0]


def test_legacy_path_falls_back_to_default_range():
    obj = auto_mod.OnmyojiAutomation.__new__(auto_mod.OnmyojiAutomation)
    seen = _record_auto_sleeps(lambda: obj._sleep_after_click(None, None))
    assert len(seen) == 1
    assert DEFAULT_CLICK_WAIT_RANGE[0] <= seen[0] <= DEFAULT_CLICK_WAIT_RANGE[1], seen
