from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TaskAction:
    """任务匹配后执行的动作"""

    type: str = "click"  # click, swipe, wait
    target: str = "matched"  # matched, fixed_coord
    x_range: Optional[tuple] = None  # (min_x, max_x)
    y_range: Optional[tuple] = None  # (min_y, max_y)
    ex_range: Optional[tuple] = None  # 终点x范围 (min_x, max_x)，swipe 用
    ey_range: Optional[tuple] = None  # 终点y范围 (min_y, max_y)，swipe 用
    duration: Optional[float] = None  # 滑动时长 / 等待秒数


@dataclass
class Task:
    """单个管道任务，类比 MAA TaskModel"""

    name: str
    template: str  # 模板图片路径（相对 mode 目录）
    recognition: str = "template"  # template, ocr, template+ocr

    # 识别参数
    threshold: Optional[float] = None
    ocr_text: Optional[str] = None
    ocr_confidence: float = 0.8
    # OCR 搜索区域 (x1, x2, y1, y2)，客户区坐标；None = 全窗口。
    # 用于排除同页出现的相同文字（否则可能点到另一处同名文字）。
    ocr_region: Optional[tuple] = None
    # OCR 命中后要点击的区域 (x1, x2, y1, y2)，客户区坐标；None = 点文字所在区域。
    # 用于「文字在一处、真正要点的地方在另一处」的场景。
    ocr_click_area: Optional[tuple] = None

    # 点击后随机等待区间（秒），每张图可单独配置；None = 用运行时默认 0.8~5
    click_wait_min: Optional[float] = None
    click_wait_max: Optional[float] = None

    # 管道控制
    next: Optional[str] = None  # 匹配后跳转的任务名
    is_start: bool = False  # 匹配此任务时，挑战计数 +1
    is_global: bool = False  # 全局任务（始终检测，不受 next 限制）

    # 动作
    action: TaskAction = field(default_factory=TaskAction)

    # 超时和重试
    timeout: int = 15
    max_retries: int = 5

    # 同一任务连续匹配预算（达到即判停；结算等长驻界面可调大，如 zuihou=20）
    max_consecutive: int = 5


def _parse_max_consecutive(raw) -> int:
    """连续匹配预算解析：非法/<=0 → 5"""
    try:
        v = int(raw)
    except (ValueError, TypeError):
        return 5
    return v if v > 0 else 5


def _parse_duration(raw) -> Optional[float]:
    """时长解析：非法 → None（调用方回退默认值）"""
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _parse_wait_range(lo_raw, hi_raw):
    """点击后随机等待区间解析：非法/缺省 -> (None, None)，由运行时回退默认。

    只填了一个时，另一个用默认补全；写反了自动交换；负数按非法处理。
    """
    lo = _parse_duration(lo_raw)
    hi = _parse_duration(hi_raw)
    if lo is not None and lo < 0:
        lo = None
    if hi is not None and hi < 0:
        hi = None
    if lo is None and hi is None:
        return None, None
    if lo is None:
        lo = 0.8
    if hi is None:
        hi = 5.0
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def parse_pipeline(pipeline_data: dict) -> List[Task]:
    """从 dict（来自 config.json 的 pipeline key）解析为 Task 列表"""
    tasks = []
    for item in pipeline_data.get("tasks", []):
        action_data = item.get("action", {})
        action = TaskAction(
            type=action_data.get("type", "click"),
            target=action_data.get("target", "matched"),
            x_range=tuple(action_data["x_range"]) if "x_range" in action_data else None,
            y_range=tuple(action_data["y_range"]) if "y_range" in action_data else None,
            ex_range=tuple(action_data["ex_range"]) if "ex_range" in action_data else None,
            ey_range=tuple(action_data["ey_range"]) if "ey_range" in action_data else None,
            duration=_parse_duration(action_data.get("duration")),
        )
        task = Task(
            name=item["name"],
            template=item["template"],
            recognition=item.get("recognition", "template"),
            threshold=item.get("threshold"),
            ocr_text=item.get("ocr_text"),
            ocr_confidence=item.get("ocr_confidence", 0.8),
            ocr_region=tuple(item["ocr_region"]) if item.get("ocr_region") else None,
            ocr_click_area=(tuple(item["ocr_click_area"])
                            if item.get("ocr_click_area") else None),
            next=item.get("next"),
            is_start=item.get("is_start", False),
            is_global=item.get("is_global", False),
            action=action,
            timeout=item.get("timeout", 15),
            max_retries=item.get("max_retries", 5),
            max_consecutive=_parse_max_consecutive(item.get("max_consecutive", 5)),
        )
        task.click_wait_min, task.click_wait_max = _parse_wait_range(
            item.get("click_wait_min"), item.get("click_wait_max"))
        tasks.append(task)
    return tasks
