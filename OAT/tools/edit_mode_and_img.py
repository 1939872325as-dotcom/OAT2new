import os
import re
import json
import ast
import shutil
import time
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem, QPushButton, QLineEdit, QLabel, QFileDialog, QListWidget, QListWidgetItem, QMessageBox, QSplitter, QMenu
from PyQt6.QtGui import QPixmap, QShortcut, QKeySequence

from OAT.utils.logging import logger


def _normalize_dest_filename(new_name: str, pending_source=None, old_image_name="") -> str:
    """规范化目标文件名：有后缀保留，无后缀继承 pending 源后缀/老图后缀，默认 .png"""
    s = (new_name or "").strip()
    if not s:
        return ""
    _root, ext = os.path.splitext(s)
    if ext:
        return s
    fallback = ""
    if pending_source:
        _, fallback = os.path.splitext(pending_source)
    if not fallback and old_image_name:
        _, fallback = os.path.splitext(old_image_name)
    if not fallback:
        fallback = ".png"
    return s + fallback


# mode.json 里「没选子模式时」用的兜底键。它出现在模式树里以便改配置，
# 但删除/改名会被拦住：删掉它，该模式在没有子模式时就无法启动。
DEFAULT_SUBMODE_KEY = 'default'


def _normalize_confidence(raw, default=0.8) -> float:
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return default
    if v > 100.0:
        return 1.0
    if v > 1.0:
        if v < 2.0:
            return 1.0
        v = v / 100.0
    return min(1.0, max(0.0, v))

def _parse_click_area(raw):
    import ast
    try:
        area = ast.literal_eval(raw)
    except (ValueError, SyntaxError, TypeError):
        return None
    if not isinstance(area, list) or len(area) != 4:
        return None
    try:
        area = [int(x) for x in area]
    except (ValueError, TypeError):
        return None
    if area[0] > area[1] or area[2] > area[3]:
        return None
    return area


def _normalize_area(raw):
    """把区域值规整成 [x1, x2, y1, y2]；非法返回 None。

    config.json 里存的是 list（如 [300, 438, 500, 561]），而 GUI 输入框里是
    str（如 "[300, 438, 500, 561]"）。两种来源都要能处理 —— 只认 str 会导致
    从配置读出来的 list 被当成非法而静默丢弃。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return _parse_click_area(raw)
    try:
        area = [int(x) for x in raw]
    except (ValueError, TypeError):
        return None
    if len(area) != 4 or area[0] > area[1] or area[2] > area[3]:
        return None
    return area


def _normalize_wait(raw):
    """把等待秒数规整成非负 float；非法/缺省返回 None（调用方按默认处理）。"""
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return None
    if v < 0:
        return None
    return v


def _plan_image_file_replace(mode_dir, old_image_name, dest_filename, pending_source):
    """纯逻辑：计算替换/改名计划，不碰磁盘（便于单测）"""
    mode_dir = mode_dir or ""
    dest_path = os.path.join(mode_dir, dest_filename or "")
    old_path = os.path.join(mode_dir, old_image_name or "")

    def _norm(p):
        return os.path.normcase(os.path.abspath(p)) if p else ""

    same_dest_old = bool(dest_filename) and bool(old_image_name) and _norm(dest_path) == _norm(old_path)
    if pending_source:
        same_file = _norm(pending_source) == _norm(dest_path)
        need_copy = not same_file
        remove_old = (not same_dest_old) and bool(old_image_name) and bool(dest_filename)
        return {
            "dest_path": dest_path,
            "old_path": old_path,
            "dest_filename": dest_filename or "",
            "pending_source": pending_source,
            "need_copy": need_copy,
            "remove_old": remove_old,
            "same_file": same_file,
        }
    need_copy = False
    remove_old = (not same_dest_old) and bool(old_image_name) and bool(dest_filename)
    return {
        "dest_path": dest_path,
        "old_path": old_path,
        "dest_filename": dest_filename or "",
        "pending_source": None,
        "need_copy": need_copy,
        "remove_old": remove_old,
        "same_file": same_dest_old,
    }


def _apply_image_file_replace(plan: dict) -> None:
    """执行替换/改名计划：复制新图、删老图/纯改名"""
    dest = plan.get("dest_path", "")
    pending = plan.get("pending_source")
    old = plan.get("old_path", "")
    if not dest:
        return
    dest_dir = os.path.dirname(dest)
    if dest_dir and not os.path.exists(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)

    def _same(a, b):
        return a and b and os.path.abspath(a) == os.path.abspath(b)

    if pending and plan.get("need_copy"):
        if not _same(pending, dest):
            shutil.copy(pending, dest)
        if plan.get("remove_old") and not _same(old, dest):
            if old and os.path.exists(old):
                os.remove(old)
        return
    if not pending and plan.get("remove_old"):
        if old and not _same(old, dest) and os.path.exists(old):
            if os.path.exists(dest):
                os.replace(old, dest)
            else:
                os.rename(old, dest)


def _image_paths_to_pipeline_task(name: str, data: dict) -> dict:
    """将 image_paths 格式的配置项转换为 pipeline.tasks 格式"""
    task = {"name": name, "template": data.get("path", name + ".png")}
    # 显示名（label）：纯展示用，往返必须保留，否则编辑器里「图像名称」会退回文件名
    _label = str(data.get("label") or "").strip()
    if _label:
        task["label"] = _label

    # 点击后随机等待（每张图独立，留空则运行时按默认 0.8~5）
    _cw_min = _normalize_wait(data.get("click_wait_min"))
    _cw_max = _normalize_wait(data.get("click_wait_max"))
    if _cw_min is not None or _cw_max is not None:
        if _cw_min is None:
            _cw_min = 0.8
        if _cw_max is None:
            _cw_max = 5.0
        if _cw_max < _cw_min:
            _cw_min, _cw_max = _cw_max, _cw_min
        task["click_wait_min"] = _cw_min
        task["click_wait_max"] = _cw_max

    if data.get("is_challenge_start"):
        task["is_start"] = True
    if data.get("is_global"):
        task["is_global"] = True
    if data.get("next_image"):
        task["next"] = data["next_image"]

    if data.get("threshold") is not None:
        try:
            task["threshold"] = float(data["threshold"])
        except (ValueError, TypeError):
            pass

    ocr_enabled = data.get("ocr_enabled", False)
    ocr_text = (data.get("ocr_target_text") or "").strip()
    # OCR 搜索区域（可选）：限定 OCR 只在该区域内找文字，用于排除同页出现的
    # 相同文字。语义与 click_area 一致 [x1, x2, y1, y2]（客户区坐标）。
    ocr_region = _normalize_area(data.get("ocr_region"))
    # OCR 点击区域（可选）：OCR 命中后，点在【这个框内】的随机位置。
    # 用于「文字在一处、真正要点的地方在另一处」的场景。
    ocr_click_area = _normalize_area(data.get("ocr_click_area"))
    _ocr_action_raw = (data.get("ocr_action") or "").strip()
    _ocr_wants_click_area = (_ocr_action_raw == "点击框选区域内的随机位置")
    # 逆向转换会把原始 recognition 存下来（审批：接受冗余字段），优先还原纯 ocr 模式
    if data.get("recognition") == "ocr" and ocr_text:
        task["recognition"] = "ocr"
        task["ocr_text"] = ocr_text
        try:
            task["ocr_confidence"] = float(data.get("ocr_confidence_threshold", 0.8))
        except (ValueError, TypeError):
            task["ocr_confidence"] = 0.8
        if ocr_region:
            task["ocr_region"] = ocr_region
        if _ocr_wants_click_area and ocr_click_area:
            task["ocr_click_area"] = ocr_click_area
    elif ocr_enabled and ocr_text:
        task["recognition"] = "template+ocr"
        task["ocr_text"] = ocr_text
        try:
            task["ocr_confidence"] = float(data.get("ocr_confidence_threshold", 0.8))
        except (ValueError, TypeError):
            task["ocr_confidence"] = 0.8
        if ocr_region:
            task["ocr_region"] = ocr_region
        if _ocr_wants_click_area and ocr_click_area:
            task["ocr_click_area"] = ocr_click_area

    # 落点规则（按使用习惯设计）：
    #   勾「点击自定义区域」 → 用这块坐标，【覆盖】识别位置（截图记的位置也一并覆盖）
    #   没勾                 → 用识别位置（模板匹配到哪点哪）
    # 「点击框选区域内的随机位置」是 OCR 专属动作，落点由 ocr_click_area 决定。
    click_area = _normalize_area(data.get("click_area"))
    _click_type = (data.get("click_type") or "").strip()
    _use_custom_area = (_click_type == "coordinate")
    action_type = (data.get("action_type") or "click")
    if action_type == "wait":
        try:
            task["action"] = {"type": "wait", "duration": float(data.get("duration", 1.0))}
        except (ValueError, TypeError):
            task["action"] = {"type": "wait", "duration": 1.0}
    elif action_type == "swipe":
        # 滑动起点复用 click_type/click_area（与 click 同规则）；终点来自 end_area；缺失则运行时 end=start
        _click_type = data.get("click_type") or ("coordinate" if click_area else "image")
        action = {"type": "swipe"}
        if _click_type == "coordinate" and click_area and len(click_area) == 4:
            try:
                action["target"] = "fixed_coord"
                action["x_range"] = [int(click_area[0]), int(click_area[1])]
                action["y_range"] = [int(click_area[2]), int(click_area[3])]
            except (ValueError, TypeError):
                action["target"] = "matched"
        else:
            action["target"] = "matched"
        end_area = data.get("end_area")
        if end_area and len(end_area) == 4:
            try:
                action["ex_range"] = [int(end_area[0]), int(end_area[1])]
                action["ey_range"] = [int(end_area[2]), int(end_area[3])]
            except (ValueError, TypeError):
                pass
        try:
            action["duration"] = float(data.get("duration", 0.5))
        except (ValueError, TypeError):
            action["duration"] = 0.5
        task["action"] = action
    elif click_area and len(click_area) == 4:
        # 只有显式勾了「点击自定义区域」才生成 fixed_coord（覆盖识别位置）；
        # 没勾时 click_area 只是截图记下的参考值，落点交给识别结果。
        # 例外：「点击框选区域内的随机位置」由 ocr_click_area 决定，不在这里生成。
        _ocr_action = (data.get("ocr_action") or "").strip()
        if not _ocr_action:
            _ocr_action = "点击文字所在区域"
        _has_ocr = task.get("recognition") in ("ocr", "template+ocr")
        _ocr_click_owns = _has_ocr and _ocr_action == "点击框选区域内的随机位置"
        if _use_custom_area and not _ocr_click_owns:
            try:
                task["action"] = {
                    "type": "click",
                    "target": "fixed_coord",
                    "x_range": [int(click_area[0]), int(click_area[1])],
                    "y_range": [int(click_area[2]), int(click_area[3])],
                }
            except (ValueError, TypeError):
                pass

    if data.get("timeout") is not None:
        try:
            task["timeout"] = int(data["timeout"])
        except (ValueError, TypeError):
            pass
    if data.get("max_retries") is not None:
        try:
            task["max_retries"] = int(data["max_retries"])
        except (ValueError, TypeError):
            pass
    if data.get("max_consecutive") is not None:
        try:
            task["max_consecutive"] = int(data["max_consecutive"])
        except (ValueError, TypeError):
            pass

    # 停止点击（出现图片停止）：往返保留，字段名与 image_paths 侧一致
    if data.get("stop_click_enabled"):
        task["stop_click_enabled"] = True
        _si = data.get("stop_images")
        if isinstance(_si, list) and _si:
            task["stop_images"] = list(_si)
        if data.get("stop_time") is not None:
            try:
                task["stop_time"] = float(data["stop_time"])
            except (ValueError, TypeError):
                pass

    return task


def _pipeline_task_to_image_paths(task: dict) -> dict:
    """将 pipeline.tasks 格式转换为 image_paths 格式（用于旧版编辑器）"""
    data = {
        "path": task.get("template", task.get("name", "") + ".png"),
        # 显示名（label）：往返保留，编辑器「图像名称」直接显示它
        "label": task.get("label", ""),
        "is_challenge_start": bool(task.get("is_start", False)),
        "message": task.get("message", ""),
        "is_global": bool(task.get("is_global", False)),
        "next_image": task.get("next", ""),
        # 冗余存原始值，保证往返无损（已审批）
        "recognition": task.get("recognition", "template"),
        "threshold": task.get("threshold"),
        "timeout": task.get("timeout"),
        "max_retries": task.get("max_retries"),
        "max_consecutive": task.get("max_consecutive"),
    }

    recognition = task.get("recognition", "template")
    if recognition in ("template+ocr", "ocr"):
        data["ocr_enabled"] = True
        data["ocr_target_text"] = task.get("ocr_text", "")
        try:
            data["ocr_confidence_threshold"] = float(task.get("ocr_confidence", 0.8))
        except (ValueError, TypeError):
            data["ocr_confidence_threshold"] = 0.8
    else:
        data["ocr_enabled"] = False
        data["ocr_target_text"] = ""
        data["ocr_confidence_threshold"] = 0.8
    # OCR 搜索区域：往返必须保留，否则「截图添加」记录的位置在保存/重开后丢失
    _rr = task.get("ocr_region")
    if _rr and len(_rr) == 4:
        try:
            data["ocr_region"] = [int(v) for v in _rr]
        except (ValueError, TypeError):
            pass
    # 点击后随机等待：往返保留
    _cw_min = _normalize_wait(task.get("click_wait_min"))
    _cw_max = _normalize_wait(task.get("click_wait_max"))
    if _cw_min is not None:
        data["click_wait_min"] = _cw_min
    if _cw_max is not None:
        data["click_wait_max"] = _cw_max

    # OCR 点击区域：往返保留（「点击框选区域内的随机位置」专用）
    _occ = task.get("ocr_click_area")
    if _occ and len(_occ) == 4:
        try:
            data["ocr_click_area"] = [int(v) for v in _occ]
        except (ValueError, TypeError):
            pass

    # ocr_action 往返保留：早前这里硬编码成「点击文字所在区域」，
    # 导致「点击点击区域设置的区域」和「点击框选区域内的随机位置」保存后丢失。
    action = task.get("action", {}) or {}
    if _occ and len(_occ) == 4:
        data["ocr_action"] = "点击框选区域内的随机位置"
    elif action.get("target") == "fixed_coord":
        data["ocr_action"] = "点击点击区域设置的区域"
    else:
        data["ocr_action"] = "点击文字所在区域"

    action_type = (action.get("type") or "click")
    data["action_type"] = action_type
    if action_type == "wait":
        try:
            data["duration"] = float(action.get("duration", 1.0))
        except (ValueError, TypeError):
            data["duration"] = 1.0
    elif action_type == "swipe":
        if action.get("target") == "fixed_coord":
            xr = list(action.get("x_range", []))
            yr = list(action.get("y_range", []))
            if len(xr) == 2 and len(yr) == 2:
                try:
                    data["click_area"] = [int(xr[0]), int(xr[1]), int(yr[0]), int(yr[1])]
                    data["click_type"] = "coordinate"
                except (ValueError, TypeError):
                    pass
        if "click_type" not in data:
            data["click_type"] = "image"
        exr = list(action.get("ex_range", []) or [])
        eyr = list(action.get("ey_range", []) or [])
        if len(exr) == 2 and len(eyr) == 2:
            try:
                data["end_area"] = [int(exr[0]), int(exr[1]), int(eyr[0]), int(eyr[1])]
            except (ValueError, TypeError):
                pass
        try:
            data["duration"] = float(action.get("duration", 0.5))
        except (ValueError, TypeError):
            data["duration"] = 0.5
    else:
        if action.get("target") == "fixed_coord":
            xr = list(action.get("x_range", []))
            yr = list(action.get("y_range", []))
            if len(xr) == 2 and len(yr) == 2:
                try:
                    data["click_area"] = [int(xr[0]), int(xr[1]), int(yr[0]), int(yr[1])]
                    data["click_type"] = "coordinate"
                except (ValueError, TypeError):
                    pass
        if "click_type" not in data:
            data["click_type"] = "image"

    # 停止点击（出现图片停止）：往返保留
    if task.get("stop_click_enabled"):
        data["stop_click_enabled"] = True
        _si = task.get("stop_images")
        if isinstance(_si, list) and _si:
            data["stop_images"] = list(_si)
        try:
            data["stop_time"] = float(task.get("stop_time", 5.0))
        except (ValueError, TypeError):
            data["stop_time"] = 5.0

    return data


# 「图片 + 配置」复制粘贴（Ctrl+C / Ctrl+V）的进程内剪贴板。
# 存图片字节和配置快照，而不是源文件路径：复制后源图被改名/删除，粘贴照样能落地。
# 不走系统剪贴板，避免覆盖用户自己复制的文字；同一进程内跨模式、跨对话框都有效。
_IMAGE_CLIPBOARD = {
    "image_name": "",
    "image_bytes": b"",
    "config": {},
    "source_mode_dir": "",
    # 停止条件图字节快照：{相对路径: bytes}，粘贴时随配置一起落地
    "stop_files": {},
}


def _unique_paste_filename(mode_dir: str, image_name: str, existing_keys) -> str:
    """为粘贴生成不重名的文件名：foo.png → foo_copy.png → foo_copy2.png …

    同时避开目录里已有的文件和配置里已有的键（键是去后缀的文件名），
    否则会出现「文件写进去但配置键撞车」或反之。
    """
    stem, ext = os.path.splitext(image_name or "")
    stem = stem or "image"
    ext = ext or ".png"
    keys = set(existing_keys or ())
    n = 1
    while True:
        suffix = "_copy" if n == 1 else f"_copy{n}"
        candidate = f"{stem}{suffix}{ext}"
        key = os.path.splitext(candidate)[0]
        if key not in keys and not os.path.exists(os.path.join(mode_dir, candidate)):
            return candidate
        n += 1


def _prepare_pasted_config(config, old_key: str, new_key: str, new_filename: str) -> dict:
    """把复制来的配置改到目标模式：path 换新文件名，自引用的 next 指向新键。

    其余字段（OCR 文本/阈值、点击区域、动作、等待区间…）原样保留 —— 复制粘贴
    的语义就是「配置一起带过去」。next_image 指向别的图时保持不动：目标模式里
    有没有那张图由用户自己决定，编辑器不擅自改写别人的引用。
    """
    try:
        cfg = json.loads(json.dumps(config or {}))
    except (TypeError, ValueError):
        cfg = dict(config or {})
    cfg["path"] = new_filename
    if cfg.get("next_image") == old_key:
        cfg["next_image"] = new_key
    return cfg


def _insert_pasted_image(config: dict, new_key: str, cfg: dict) -> None:
    """把粘贴的图片配置写进目标 config（image_paths + 已有 pipeline 时同步 tasks）

    配置结构异常（image_paths/pipeline 不是预期类型）时抛 ValueError，
    由调用方在落盘前中止 —— 宁可不粘贴，也不静默覆盖用户已有的配置。
    """
    image_paths = config.get("image_paths")
    if image_paths is None:
        image_paths = {}
        config["image_paths"] = image_paths
    if not isinstance(image_paths, dict):
        raise ValueError("目标配置的 image_paths 不是对象")
    image_paths[new_key] = cfg

    # 只有目标配置本身就有 pipeline 才同步：老格式模式走 common_challenge，
    # 凭空加一个 pipeline 会改变它的运行路径。
    if config.get("pipeline") is not None:
        pipeline = config["pipeline"]
        if not isinstance(pipeline, dict):
            raise ValueError("目标配置的 pipeline 不是对象")
        tasks = pipeline.get("tasks")
        if tasks is None:
            tasks = []
            pipeline["tasks"] = tasks
        if not isinstance(tasks, list):
            raise ValueError("目标配置的 pipeline.tasks 不是数组")
        task = _image_paths_to_pipeline_task(new_key, cfg)
        for i, t in enumerate(tasks):
            if isinstance(t, dict) and t.get("name") == new_key:
                tasks[i] = task
                break
        else:
            tasks.append(task)


class ImageConfigDialog(QDialog):
    def __init__(self, parent=None, image_name="", config_data={}, config_path="", image_list=[],
                 display_name=""):
        """display_name：图像「名称」= 列表里显示的那行文字（config 的 label 字段）。

        名称与后端解耦：改它只写 label（纯显示），配置键/template/path/文件名
        一律由磁盘文件名派生，不会跟着变；路径（文件名）也保持 ASCII 不被 OCR 命名。
        """
        super().__init__(parent)
        self.setWindowTitle("图像配置")
        self.resize(500, 460)

        self.image_name = image_name
        self.config_data = config_data
        # 名称默认取配置里的 label（= 列表里显示的同一份文字），没有才退回文件名
        self.display_name = (display_name
                             or str(self.config_data.get("label") or "").strip()
                             or image_name)
        self.config_path = config_path
        self.image_list = image_list
        # 待替换的新图绝对路径（浏览选择后设置，保存时复制到模式目录）
        self.pending_image_source = None
        # OCR点击区域是否被用户单独框选过（决定它是否跟随「自定义区域」）
        self._ocr_click_area_user_set = False
        self._ocr_click_area_auto_value = None

        # 初始化UI
        self.setup_ui()
        self._refresh_preview()
        # 新增图片配置时：自动识别图片上的文字，填进「OCR识别文本」。
        # 只在配置里还没带 ocr_target_text 时识别（编辑旧配置、截图添加已预填的不重复跑）；
        # 文本框非空时 _autofill_ocr_from_image 也不会覆盖手填内容。
        if "ocr_target_text" not in self.config_data and self.config_path:
            image_path = os.path.join(os.path.dirname(self.config_path), self.image_name)
            self._autofill_ocr_from_image(image_path)
    
    def setup_ui(self):
        """
        设置UI界面
        """
        main_layout = QVBoxLayout(self)
        
        # 图像名称
        name_layout = QHBoxLayout()
        name_label = QLabel("图像名称:")
        self.name_line_edit = QLineEdit(self.display_name)
        name_layout.addWidget(name_label)
        name_layout.addWidget(self.name_line_edit)
        main_layout.addLayout(name_layout)
        
        # 路径（只读显示目标文件名，替换走浏览按钮）
        path_layout = QHBoxLayout()
        path_label = QLabel("路径:")
        self.path_line_edit = QLineEdit(self.config_data.get("path", self.image_name))
        self.path_line_edit.setReadOnly(True)
        self.path_line_edit.setToolTip("目标文件名，点击右侧浏览按钮替换图片")
        self.browse_img_btn = QPushButton("浏览…/替换图片")
        self.browse_img_btn.setToolTip("从资源管理器选择一张图，保存时复制到当前模式目录")
        self.browse_img_btn.clicked.connect(self.browse_image)
        path_layout.addWidget(path_label)
        path_layout.addWidget(self.path_line_edit)
        path_layout.addWidget(self.browse_img_btn)
        main_layout.addLayout(path_layout)

        # 图片预览（当前图 / 待替换新图）
        self.preview_label = QLabel()
        self.preview_label.setFixedHeight(110)
        self.preview_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("border: 1px solid #d0d0d0; background: #fafafa;")
        main_layout.addWidget(self.preview_label)
        
        # 是否是挑战开始
        self.is_challenge_start_checkbox = QtWidgets.QCheckBox("是否是挑战开始")
        self.is_challenge_start_checkbox.setChecked(self.config_data.get("is_challenge_start", False))
        main_layout.addWidget(self.is_challenge_start_checkbox)
        
        # 消息
        message_layout = QHBoxLayout()
        message_label = QLabel("消息:")
        self.message_line_edit = QLineEdit(self.config_data.get("message", ""))
        message_layout.addWidget(message_label)
        message_layout.addWidget(self.message_line_edit)
        main_layout.addLayout(message_layout)
        
        # is_global配置
        self.is_global_checkbox = QtWidgets.QCheckBox("是否全局")
        self.is_global_checkbox.setChecked(self.config_data.get("is_global", False))
        main_layout.addWidget(self.is_global_checkbox)
        
        # next_image配置
        next_img_layout = QHBoxLayout()
        next_img_label = QLabel("下一张图片:")
        self.next_img_combo = QtWidgets.QComboBox()
        # 添加空选项
        self.next_img_combo.addItem("")
        # 添加当前模式的所有图片
        for img in self.image_list:
            self.next_img_combo.addItem(img)
        # 设置当前值
        current_next_img = self.config_data.get("next_image", "")
        # 尝试直接查找
        index = self.next_img_combo.findText(current_next_img)
        if index == -1 and current_next_img:
            # 如果找不到，尝试查找带后缀的版本
            for img in self.image_list:
                if os.path.splitext(img)[0] == current_next_img:
                    index = self.next_img_combo.findText(img)
                    break
        if index != -1:
            self.next_img_combo.setCurrentIndex(index)
        next_img_layout.addWidget(next_img_label)
        next_img_layout.addWidget(self.next_img_combo)
        main_layout.addLayout(next_img_layout)
        
        # 点击区域设置
        click_area_group = QtWidgets.QGroupBox("点击区域设置")
        click_area_layout = QVBoxLayout(click_area_group)

        # 动作类型：点击 / 滑动 / 等待（pipeline 专属；legacy common_challenge 保持 click-only）
        self.action_type_group = QtWidgets.QButtonGroup()
        self.action_click_radio = QtWidgets.QRadioButton("点击")
        self.action_swipe_radio = QtWidgets.QRadioButton("滑动")
        self.action_wait_radio = QtWidgets.QRadioButton("等待")
        _saved_action = (self.config_data.get("action_type") or "click")
        self.action_click_radio.setChecked(_saved_action == "click")
        self.action_swipe_radio.setChecked(_saved_action == "swipe")
        self.action_wait_radio.setChecked(_saved_action == "wait")
        self.action_type_group.addButton(self.action_click_radio)
        self.action_type_group.addButton(self.action_swipe_radio)
        self.action_type_group.addButton(self.action_wait_radio)

        action_type_layout = QHBoxLayout()
        action_type_layout.addWidget(QLabel("动作类型:"))
        action_type_layout.addWidget(self.action_click_radio)
        action_type_layout.addWidget(self.action_swipe_radio)
        action_type_layout.addWidget(self.action_wait_radio)
        click_area_layout.addLayout(action_type_layout)

        # 点击方式
        self.click_method_group = QtWidgets.QButtonGroup()
        self.click_image_radio = QtWidgets.QRadioButton("点击图片区域")
        self.click_custom_radio = QtWidgets.QRadioButton("点击自定义区域")
        
        # 默认选择点击图片区域
        _is_custom = self.config_data.get("click_type") == "coordinate" or ("click_type" not in self.config_data and "click_area" in self.config_data)
        self.click_image_radio.setChecked(not _is_custom)
        self.click_custom_radio.setChecked(_is_custom)
        
        self.click_method_group.addButton(self.click_image_radio)
        self.click_method_group.addButton(self.click_custom_radio)
        
        click_area_layout.addWidget(self.click_image_radio)
        click_area_layout.addWidget(self.click_custom_radio)
        
        # 自定义区域
        custom_area_layout = QHBoxLayout()
        custom_area_label = QLabel("自定义区域:")
        self.custom_area_line_edit = QLineEdit(str(self.config_data.get("click_area", [100, 200, 200, 400])))
        self.custom_area_pick_btn = QPushButton("框选")
        self.custom_area_pick_btn.setToolTip("到游戏画面上框选一块区域，自动填入坐标（[x1, x2, y1, y2]）")
        self.custom_area_pick_btn.clicked.connect(self.pick_custom_area)
        custom_area_layout.addWidget(custom_area_label)
        custom_area_layout.addWidget(self.custom_area_line_edit)
        custom_area_layout.addWidget(self.custom_area_pick_btn)
        click_area_layout.addLayout(custom_area_layout)

        # 终点区域（滑动终点坐标范围，格式同自定义区域）
        end_area_layout = QHBoxLayout()
        end_area_label = QLabel("终点区域:")
        self.end_area_line_edit = QLineEdit(str(self.config_data.get("end_area", [100, 200, 200, 400])))
        self.end_area_pick_btn = QPushButton("框选")
        self.end_area_pick_btn.setToolTip("到游戏画面上框选一块区域，自动填入坐标（[x1, x2, y1, y2]）")
        self.end_area_pick_btn.clicked.connect(self.pick_end_area)
        end_area_layout.addWidget(end_area_label)
        end_area_layout.addWidget(self.end_area_line_edit)
        end_area_layout.addWidget(self.end_area_pick_btn)
        click_area_layout.addLayout(end_area_layout)

        # 时长（滑动时长 / 等待秒数，单位秒）
        duration_layout = QHBoxLayout()
        duration_label = QLabel("时长(秒):")
        self.duration_line_edit = QLineEdit(str(self.config_data.get("duration", 0.5)))
        duration_layout.addWidget(duration_label)
        duration_layout.addWidget(self.duration_line_edit)
        click_area_layout.addLayout(duration_layout)

        # 点击后随机等待（秒）：每张图可单独设置，点击后在此区间随机等待。
        # 用于模拟人工节奏，避免固定间隔被判定为脚本。
        click_wait_layout = QHBoxLayout()
        click_wait_label = QLabel("点击后随机等待(秒):")
        self.click_wait_min_edit = QLineEdit(str(self.config_data.get("click_wait_min", 0.8)))
        self.click_wait_min_edit.setFixedWidth(60)
        self.click_wait_max_edit = QLineEdit(str(self.config_data.get("click_wait_max", 5.0)))
        self.click_wait_max_edit.setFixedWidth(60)
        click_wait_layout.addWidget(click_wait_label)
        click_wait_layout.addWidget(self.click_wait_min_edit)
        click_wait_layout.addWidget(QLabel("~"))
        click_wait_layout.addWidget(self.click_wait_max_edit)
        click_wait_layout.addWidget(QLabel("（默认 0.8~5，留空按默认）"))
        click_wait_layout.addStretch()
        click_area_layout.addLayout(click_wait_layout)

        main_layout.addWidget(click_area_group)

        # 停止点击设置：勾选后，点击该图之前先识别停止条件图；
        # 任一条件图出现在画面上就本轮不点击，并按「停止时间」等待后进入下一轮。
        stop_group = QtWidgets.QGroupBox("停止点击设置")
        stop_layout = QtWidgets.QVBoxLayout(stop_group)

        self.stop_click_checkbox = QtWidgets.QCheckBox("出现图片停止点击")
        self.stop_click_checkbox.setChecked(bool(self.config_data.get("stop_click_enabled", False)))
        self.stop_click_checkbox.setToolTip("勾选后，点击该图之前先识别下方条件图；命中任一张则本轮不点击")
        stop_layout.addWidget(self.stop_click_checkbox)

        # 条件图片槽位（默认 3 个）：点 + 号进入截图添加模式，截图后自动随机命名，
        # 槽位里只显示缩略图不显示文件名；已添加时点击=重新截图，右键=清除
        self.stop_slots = [p for p in (self.config_data.get("stop_images") or []) if p][:3]
        while len(self.stop_slots) < 3:
            self.stop_slots.append(None)
        self.stop_slot_buttons = []
        stop_slots_layout = QtWidgets.QHBoxLayout()
        for _i in range(3):
            _btn = QtWidgets.QPushButton()
            _btn.setFixedSize(84, 84)
            _btn.setIconSize(QtCore.QSize(72, 72))
            _btn.setText("+")
            _btn.setToolTip("点击 + 截图添加停止条件图；已添加时点击重新截图，右键清除")
            _btn.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            _btn.clicked.connect(lambda _checked=False, idx=_i: self._capture_stop_image(idx))
            _btn.customContextMenuRequested.connect(lambda _pos, idx=_i: self._stop_slot_menu(idx))
            self.stop_slot_buttons.append(_btn)
            stop_slots_layout.addWidget(_btn)
        stop_slots_layout.addStretch()
        stop_layout.addLayout(stop_slots_layout)

        # 停止时间（秒）：命中停止条件后本轮不点击并等待此时长，默认 5
        stop_time_layout = QtWidgets.QHBoxLayout()
        stop_time_layout.addWidget(QLabel("停止时间(秒):"))
        _saved_stop_time = self.config_data.get("stop_time")
        self.stop_time_edit = QtWidgets.QLineEdit(
            str(_saved_stop_time if _saved_stop_time is not None else 5))
        self.stop_time_edit.setFixedWidth(60)
        stop_time_layout.addWidget(self.stop_time_edit)
        stop_time_layout.addWidget(QLabel("命中停止条件后等待的秒数（默认 5）"))
        stop_time_layout.addStretch()
        stop_layout.addLayout(stop_time_layout)

        main_layout.addWidget(stop_group)

        # 勾选状态驱动槽位/时间框可用性；打开对话框时按已有配置刷新缩略图
        self.stop_click_checkbox.toggled.connect(self._on_stop_toggle)
        self._on_stop_toggle(self.stop_click_checkbox.isChecked())
        
        # OCR配置
        ocr_group = QtWidgets.QGroupBox("OCR配置")
        ocr_layout = QVBoxLayout(ocr_group)
        
        # 是否启用OCR（默认勾选；旧配置里存过 False 的仍按存的值显示）
        self.is_ocr_enabled_checkbox = QtWidgets.QCheckBox("是否启用OCR")
        self.is_ocr_enabled_checkbox.setChecked(self.config_data.get("ocr_enabled", True))
        ocr_layout.addWidget(self.is_ocr_enabled_checkbox)
        
        # OCR识别文本
        ocr_text_layout = QHBoxLayout()
        ocr_text_label = QLabel("OCR识别文本:")
        self.ocr_text_edit = QLineEdit(self.config_data.get("ocr_target_text", ""))
        ocr_text_layout.addWidget(ocr_text_label)
        ocr_text_layout.addWidget(self.ocr_text_edit)
        ocr_layout.addLayout(ocr_text_layout)
        
        # OCR识别阈值
        ocr_threshold_layout = QtWidgets.QHBoxLayout()
        ocr_threshold_label = QLabel("OCR识别阈值:")
        self.ocr_threshold_edit = QLineEdit(str(self.config_data.get("ocr_confidence_threshold", 0.8)))
        ocr_threshold_layout.addWidget(ocr_threshold_label)
        ocr_threshold_layout.addWidget(self.ocr_threshold_edit)
        ocr_layout.addLayout(ocr_threshold_layout)
        
        # OCR搜索区域（可选）：同一画面出现多个相同文字时，用区域把干扰的那一个排除掉
        ocr_region_layout = QtWidgets.QHBoxLayout()
        ocr_region_label = QLabel("OCR搜索区域:")
        _ocr_region_val = self.config_data.get("ocr_region")
        self.ocr_region_edit = QLineEdit("" if not _ocr_region_val else str(list(_ocr_region_val)))
        self.ocr_region_edit.setPlaceholderText("可选，[x1, x2, y1, y2]，留空 = 全窗口")
        self.ocr_region_pick_btn = QPushButton("框选")
        self.ocr_region_pick_btn.setToolTip("到游戏画面上框选一块区域作为 OCR 搜索范围")
        self.ocr_region_pick_btn.clicked.connect(self.pick_ocr_region)
        ocr_region_layout.addWidget(ocr_region_label)
        ocr_region_layout.addWidget(self.ocr_region_edit)
        ocr_region_layout.addWidget(self.ocr_region_pick_btn)
        ocr_layout.addLayout(ocr_region_layout)
        
        # OCR识别后的操作
        ocr_action_layout = QHBoxLayout()
        ocr_action_label = QLabel("OCR识别后的操作:")
        self.ocr_action_combo = QtWidgets.QComboBox()
        self.ocr_action_combo.addItem("点击文字所在区域")
        self.ocr_action_combo.addItem("点击点击区域设置的区域")
        self.ocr_action_combo.addItem("点击框选区域内的随机位置")
        current_action = self.config_data.get("ocr_action", "点击文字所在区域")
        index = self.ocr_action_combo.findText(current_action)
        if index != -1:
            self.ocr_action_combo.setCurrentIndex(index)
        ocr_action_layout.addWidget(ocr_action_label)
        ocr_action_layout.addWidget(self.ocr_action_combo)
        ocr_layout.addLayout(ocr_action_layout)

        # OCR 点击区域（选「点击框选区域内的随机位置」时用；与上面点击区域设置里的
        # 自定义区域相互独立，方便「文字在一处、要点在另一处」的场景）
        #
        # 默认值：没单独配过就沿用「自定义区域」的坐标 —— 大多数情况下
        # 想点的地方就是框选出来的那块，不必再框一遍；真要不同再单独框选覆盖。
        ocr_click_layout = QHBoxLayout()
        ocr_click_label = QLabel("OCR点击区域:")
        _occ = self.config_data.get("ocr_click_area")
        if not _occ:
            _occ = self.config_data.get("click_area")
        self.ocr_click_area_edit = QLineEdit("" if not _occ else str(list(_occ)))
        self.ocr_click_area_edit.setPlaceholderText(
            "可选，[x1, x2, y1, y2]；动作为「点击框选区域内的随机位置」时必填")
        self.ocr_click_pick_btn = QPushButton("框选")
        self.ocr_click_pick_btn.setToolTip("到游戏画面上框选 OCR 识别成功后要点击的范围")
        self.ocr_click_pick_btn.clicked.connect(self.pick_ocr_click_area)
        ocr_click_layout.addWidget(ocr_click_label)
        ocr_click_layout.addWidget(self.ocr_click_area_edit)
        ocr_click_layout.addWidget(self.ocr_click_pick_btn)
        ocr_layout.addLayout(ocr_click_layout)
        
        main_layout.addWidget(ocr_group)
        
        # 按钮
        button_layout = QHBoxLayout()
        self.save_btn = QPushButton("保存")
        self.cancel_btn = QPushButton("取消")
        
        button_layout.addStretch()
        button_layout.addWidget(self.save_btn)
        button_layout.addWidget(self.cancel_btn)
        
        main_layout.addLayout(button_layout)
        
        # 连接信号
        self.save_btn.clicked.connect(self.save)
        self.cancel_btn.clicked.connect(self.reject)

    def browse_image(self):
        """弹出资源管理器选择替换图片，仅记录源路径，保存时再复制改名"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择替换图片", "", "图像文件 (*.png *.jpg *.jpeg *.bmp)"
        )
        if not file_path:
            return
        self.pending_image_source = file_path
        self._refresh_preview(preview_path=file_path)
        # 选了新图就顺手认一下图上的文字，帮用户把 OCR 文本框填好
        self._autofill_ocr_from_image(file_path)

    def _autofill_ocr_from_image(self, image_path: str) -> None:
        """识别图片上的文字并填进「OCR识别文本」（只在文本框为空时填，不覆盖手填内容）。

        为什么需要它：替换模板图后，OCR 兜底用的目标文字还是旧的（例如图上已
        换成「挑战」，配置里还写着「退治」），一旦模板匹配失败就会去点错地方。
        这里用新图自身的文字把配置对齐，避免「换了图忘了改字」。
        """
        if not image_path or not os.path.exists(image_path):
            return
        # 已经手填过就不动，尊重用户输入
        try:
            if self.ocr_text_edit.text().strip():
                return
        except AttributeError:
            return

        try:
            from OAT.utils.OCRService import ocr_service
        except Exception as e:
            logger.warning(f"自动识别文字：OCR 服务不可用（{e}），请手动填写")
            return

        try:
            items = ocr_service.recognize_all_text(image_path)
        except Exception as e:
            logger.warning(f"自动识别文字失败: {e}")
            return

        if not items:
            logger.info(f"自动识别文字：{os.path.basename(image_path)} 上没认到文字，请手动填写")
            return

        # 主文字选法：能把「同一按钮的多行文字」拼成完整文案就拼（『召唤』+『鬼王』
# →『召唤鬼王』），框太散/太长才退回第一行（见 _compose_autofill_text）。
        text = ModeEditorDialog._compose_autofill_text(items)
        best = next((d for d in items if (d.get('text') or '').strip() == text), None) \
            or max(items, key=lambda d: d.get('confidence', 0.0))
        if not text:
            return
        self.ocr_text_edit.setText(text)
        logger.info(f"自动识别文字：{os.path.basename(image_path)} → '{text}'"
                    f"（置信度 {best.get('confidence', 0.0):.3f}）")
        if len(items) > 1:
            others = '、'.join(f"{d['text']}({d.get('confidence', 0):.2f})"
                              for d in items if d is not best)
            logger.info(f"  图上其余文字（未采用）：{others}")
            self.ocr_text_edit.setToolTip(
                f"图上识别到 {len(items)} 处文字，已自动填入：{text}\n"
                f"其余候选：{others}\n如填错了请手动改。")

    def _preview_candidate_paths(self):
        """预览优先级：待替换新图 > 模式目录下老图"""
        if self.pending_image_source and os.path.exists(self.pending_image_source):
            return self.pending_image_source
        if self.config_path:
            mode_dir = os.path.dirname(self.config_path)
            old_path = os.path.join(mode_dir, self.image_name)
            if self.image_name and os.path.exists(old_path):
                return old_path
        return ""

    def _refresh_preview(self, preview_path=""):
        if not hasattr(self, "preview_label"):
            return
        target = preview_path or self._preview_candidate_paths()
        if target and os.path.exists(target):
            pixmap = QPixmap(target)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(
                    200, 100,
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                )
                self.preview_label.setPixmap(pixmap)
                tip = "待替换新图预览" if self.pending_image_source else "当前图片预览"
                self.preview_label.setToolTip(f"{tip}：{target}")
                return
        if self.pending_image_source:
            self.preview_label.setText(f"已选择：{os.path.basename(self.pending_image_source)}（无预览）")
        else:
            self.preview_label.setText("暂无预览")

    # ---------- 框选坐标（复用 region_capture 的遮罩） ----------

    def _current_game_window(self):
        """尽力取到当前游戏窗口的 (hwnd, title)。

        ImageConfigDialog 是从 ModeEditorDialog 弹出的，往上找父链拿主界面
        选中的句柄；拿不到再回落到 client.json 里的标题。
        （与 ModeEditorDialog 的同名方法同源，两边各自独立。）
        """
        # 1) 沿父链找主界面窗口上的选中句柄
        try:
            w = self.parent()
            for _ in range(6):
                if w is None:
                    break
                hwnd = getattr(w, 'selected_hwnd', None)
                title = getattr(w, 'window_title', None)
                if hwnd or title:
                    return int(hwnd or 0), (title or "")
                w = w.parent()
        except Exception:
            pass

        # 2) client.json 的第一个标题
        try:
            cj = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'client.json')
            if os.path.exists(cj):
                with open(cj, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                titles = data.get('title', {})
                if isinstance(titles, dict) and titles:
                    first = titles.get('0') or next(iter(titles.values()))
                    return 0, str(first)
        except Exception:
            pass

        return 0, ""

    def _pick_area_rect(self, hint: str):
        """到游戏画面上框选一块区域，返回 [x1, x2, y1, y2]；取消返回 None。

        框选用的是「后台抓取的游戏窗口客户区」图，与识别/点击同一套坐标系，
        所以框出来的坐标可以直接写进配置，不需要任何换算。
        """
        try:
            from OAT.tools.region_capture import capture_region_from_window
        except Exception as e:
            QMessageBox.warning(self, "警告", f"框选工具不可用: {e}")
            return None

        hwnd, title = self._current_game_window()
        _crop, crop_rect, _size = capture_region_from_window(
            self, hwnd=hwnd, window_title=title)
        if crop_rect is None:
            return None
        rx, ry, rw, rh = crop_rect
        if rw <= 0 or rh <= 0:
            return None
        box = [int(rx), int(rx + rw), int(ry), int(ry + rh)]
        logger.info(f"{hint}：框选区域 [x1,x2,y1,y2]={box}")
        return box

    def pick_custom_area(self):
        """框选「自定义区域」（点击方式为『点击自定义区域』时用）。"""
        box = self._pick_area_rect("框选自定义区域")
        if not box:
            return
        self.custom_area_line_edit.setText(str(box))
        # 框选完自动切到「点击自定义区域」，否则填了坐标也不会生效
        self.click_custom_radio.setChecked(True)
        # OCR 点击区域若还没单独框选过，就跟着走 —— 省掉「同一块地方框两遍」
        self._sync_ocr_click_area_from_custom(box)

    def pick_end_area(self):
        """框选「终点区域」（动作类型为『滑动』时用）。"""
        box = self._pick_area_rect("框选终点区域")
        if not box:
            return
        self.end_area_line_edit.setText(str(box))
        self.action_swipe_radio.setChecked(True)

    def pick_ocr_region(self):
        """框选「OCR搜索区域」：限定只在框内找文字，用于排除同页相同文字。"""
        box = self._pick_area_rect("框选OCR搜索区域")
        if not box:
            return
        self.ocr_region_edit.setText(str(box))

    def pick_ocr_click_area(self):
        """框选「OCR识别后点击的区域」：文字识别成功、但点在框内的随机位置。

        适用场景：文字只是「判断到了某个界面」，真正要点的地方不在文字上
        （例如文字在标题、按钮在别处）。
        """
        box = self._pick_area_rect("框选OCR点击区域")
        if not box:
            return
        self.ocr_click_area_edit.setText(str(box))
        # 标记「用户单独框选过」：之后改自定义区域不再覆盖这里
        self._ocr_click_area_user_set = True
        idx = self.ocr_action_combo.findText("点击框选区域内的随机位置")
        if idx != -1:
            self.ocr_action_combo.setCurrentIndex(idx)

    def _sync_ocr_click_area_from_custom(self, box):
        """把「自定义区域」的坐标同步给「OCR点击区域」。

        规则（按使用习惯设计）：
          · OCR点击区域为空       → 直接跟随，省掉重复框选；
          · 用户单独框选过        → 不动它（尊重用户的选择）；
          · 之前是跟着自动填的    → 跟着更新（用户改了自定义区域，OCR 也应更新）。
        判据是「当前值是否等于上一次自动同步的值」，而不是简单看空不空。
        """
        try:
            if getattr(self, "_ocr_click_area_user_set", False):
                return
            cur = self.ocr_click_area_edit.text().strip()
            last_auto = getattr(self, "_ocr_click_area_auto_value", None)
            # 空 或 与上次自动同步的值相同 → 视为「跟随中」，可以覆盖
            if cur and last_auto is not None and cur != last_auto:
                return
            self.ocr_click_area_edit.setText(str(box))
            self._ocr_click_area_auto_value = str(box)
        except Exception:
            pass

    # ---------- 停止点击设置（出现图片停止） ----------

    def _stop_dir(self) -> str:
        """停止条件图的存放目录：模式目录下的 stop/ 子目录。

        放子目录而不是模式根目录，是因为图片列表是扫根目录建的，
        放根目录会让停止图混进普通模板图列表里。
        """
        return os.path.join(os.path.dirname(self.config_path), 'stop') if self.config_path else ""

    def _on_stop_toggle(self, checked: bool):
        """勾选状态驱动槽位与停止时间框的可用性，并刷新缩略图"""
        for _btn in self.stop_slot_buttons:
            _btn.setEnabled(checked)
        self.stop_time_edit.setEnabled(checked)
        if checked:
            for _i in range(3):
                self._refresh_stop_slot(_i)

    def _refresh_stop_slot(self, idx: int):
        """刷新槽位显示：有图显示缩略图，无图显示 + 号（不显示文件名）"""
        btn = self.stop_slot_buttons[idx]
        rel = self.stop_slots[idx]
        if rel:
            p = os.path.join(os.path.dirname(self.config_path), rel)
            pix = QtGui.QPixmap(p)
            if not pix.isNull():
                btn.setIcon(QtGui.QIcon(pix))
                btn.setText("")
                btn.setToolTip(f"点击重新截图替换；右键清除\n{rel}")
                return
            # 配置里有引用但文件丢了：按空槽处理，保存时这条引用会被剔掉
            self.stop_slots[idx] = None
        btn.setIcon(QtGui.QIcon())
        btn.setText("+")
        btn.setToolTip("点击 + 截图添加停止条件图；已添加时点击重新截图，右键清除")

    def _capture_stop_image(self, idx: int):
        """点击槽位：进入截图添加模式，框选后自动随机命名存入 stop/ 目录"""
        if not self.stop_click_checkbox.isChecked():
            self.stop_click_checkbox.setChecked(True)
            return
        try:
            from OAT.tools.region_capture import capture_region_from_window, save_crop
        except Exception as e:
            QMessageBox.warning(self, "警告", f"截图工具不可用: {e}")
            return
        hwnd, title = self._current_game_window()
        crop, _crop_rect, _size = capture_region_from_window(
            self, hwnd=hwnd, window_title=title)
        if crop is None:
            return
        stop_dir = self._stop_dir()
        if not stop_dir:
            QMessageBox.warning(self, "警告", "无法确定模式目录，无法保存停止条件图")
            return
        try:
            os.makedirs(stop_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "警告", f"创建停止图目录失败: {e}")
            return

        # 自动随机命名（时间戳），重名自动加序号
        base = "stop_" + time.strftime("%Y%m%d-%H%M%S")
        filename = base + ".png"
        n = 1
        while os.path.exists(os.path.join(stop_dir, filename)):
            filename = f"{base}_{n}.png"
            n += 1
        dest = os.path.join(stop_dir, filename)
        if not save_crop(crop, dest):
            QMessageBox.warning(self, "警告", f"保存截图失败: {dest}")
            return

        # 覆盖语义：该槽位已有图就删旧文件，避免孤儿文件堆积
        old_rel = self.stop_slots[idx]
        if old_rel:
            try:
                old_abs = os.path.join(os.path.dirname(self.config_path), old_rel)
                if os.path.exists(old_abs):
                    os.remove(old_abs)
            except Exception as e:
                logger.warning(f"删除旧停止图失败（忽略）: {e}")
        self.stop_slots[idx] = f"stop/{filename}"
        self._refresh_stop_slot(idx)
        logger.info(f"停止条件图已添加: stop/{filename}（槽位 {idx + 1}/3）")

    def _stop_slot_menu(self, idx: int):
        """槽位右键菜单：重新截图 / 清除"""
        if not self.stop_slots[idx]:
            return
        menu = QtWidgets.QMenu(self)
        recapture_action = menu.addAction("重新截图")
        clear_action = menu.addAction("清除")
        btn = self.stop_slot_buttons[idx]
        chosen = menu.exec(btn.mapToGlobal(QtCore.QPoint(0, btn.height())))
        if chosen == recapture_action:
            self._capture_stop_image(idx)
        elif chosen == clear_action:
            self._clear_stop_slot(idx)

    def _clear_stop_slot(self, idx: int):
        """清除槽位：删文件 + 清引用，不留孤儿"""
        rel = self.stop_slots[idx]
        if rel:
            try:
                p = os.path.join(os.path.dirname(self.config_path), rel)
                if os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                logger.warning(f"删除停止图失败（忽略）: {e}")
        self.stop_slots[idx] = None
        self._refresh_stop_slot(idx)

    def _discard_stop_files(self):
        """取消勾选后保存成功时调用：删除全部槽位引用的停止图文件"""
        mode_dir = os.path.dirname(self.config_path) if self.config_path else ""
        for rel in self.stop_slots:
            if not rel or not mode_dir:
                continue
            try:
                p = os.path.join(mode_dir, rel)
                if os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                logger.warning(f"删除停止图失败（忽略）: {e}")
        self.stop_slots = [None, None, None]
        for _i in range(3):
            self._refresh_stop_slot(_i)

    def save(self):
        """
        保存配置（含图片替换：复制到模式目录并按改后名字落地）
        """
        # 更新配置数据
        # 名称只是「显示名」(label)：改了它不会动配置键、文件名、template；
        # 后端身份始终由路径（文件名）派生，避免界面上改个字就把配置结构改了。
        raw_new_name = self.name_line_edit.text().strip()
        if not raw_new_name:
            QMessageBox.warning(self, "警告", "图像名称不能为空")
            return

        # 目标文件名：以路径框为准（待替换新图时按新图后缀规范化）
        path_name = _normalize_dest_filename(
            self.path_line_edit.text().strip() or self.image_name,
            self.pending_image_source, self.image_name)
        new_name = path_name                       # 文件操作仍用文件名
        self.path_line_edit.setText(path_name)
        self.config_data["path"] = path_name
        self.config_data["label"] = raw_new_name   # 仅显示用
        self.config_data["is_challenge_start"] = self.is_challenge_start_checkbox.isChecked()
        self.config_data["message"] = self.message_line_edit.text()
        self.config_data["is_global"] = self.is_global_checkbox.isChecked()
        # 存储不带后缀的文件名
        next_img_value = self.next_img_combo.currentText()
        if next_img_value:
            next_img_value = os.path.splitext(next_img_value)[0]
        self.config_data["next_image"] = next_img_value
        
        # 处理动作类型（pipeline 专属；legacy common_challenge 保持 click-only，不在此动）
        if self.action_swipe_radio.isChecked():
            action_type = "swipe"
        elif self.action_wait_radio.isChecked():
            action_type = "wait"
        else:
            action_type = "click"
        self.config_data["action_type"] = action_type

        # 点击后随机等待（每张图独立配置）。两个框都留空 => 移除该字段，运行时按默认 0.8~5。
        # 只填一个时，另一个按默认补；写反了自动交换。
        _cw_min_raw = self.click_wait_min_edit.text().strip()
        _cw_max_raw = self.click_wait_max_edit.text().strip()
        if not _cw_min_raw and not _cw_max_raw:
            self.config_data.pop("click_wait_min", None)
            self.config_data.pop("click_wait_max", None)
        else:
            try:
                _cw_min = float(_cw_min_raw) if _cw_min_raw else 0.8
                _cw_max = float(_cw_max_raw) if _cw_max_raw else 5.0
            except (ValueError, TypeError):
                QMessageBox.warning(self, "警告", "点击后随机等待格式非法，应为数字（单位秒）")
                return
            if _cw_min < 0 or _cw_max < 0:
                QMessageBox.warning(self, "警告", "点击后随机等待不能为负数")
                return
            if _cw_max < _cw_min:
                _cw_min, _cw_max = _cw_max, _cw_min
            self.config_data["click_wait_min"] = _cw_min
            self.config_data["click_wait_max"] = _cw_max

        # 停止点击设置（出现图片停止）：勾选时写入条件图与停止时间；
        # 未勾选时只清配置字段（引用的文件等保存成功后再物理删除，见 save 尾部）
        if self.stop_click_checkbox.isChecked():
            _stop_imgs = [p for p in self.stop_slots if p]
            if not _stop_imgs:
                QMessageBox.warning(self, "警告", "已勾选停止点击，请至少为 1 个方框截图添加条件图片")
                return
            _stop_raw = self.stop_time_edit.text().strip()
            try:
                _stop_time = float(_stop_raw) if _stop_raw else 5.0
            except (ValueError, TypeError):
                QMessageBox.warning(self, "警告", "停止时间格式非法，应为数字（单位秒）")
                return
            if _stop_time < 0:
                QMessageBox.warning(self, "警告", "停止时间不能为负数")
                return
            self.config_data["stop_click_enabled"] = True
            self.config_data["stop_images"] = _stop_imgs
            self.config_data["stop_time"] = _stop_time
        else:
            self.config_data.pop("stop_click_enabled", None)
            self.config_data.pop("stop_images", None)
            self.config_data.pop("stop_time", None)

        if action_type == "wait":
            try:
                duration = float(self.duration_line_edit.text())
            except (ValueError, TypeError):
                QMessageBox.warning(self, "警告", "时长格式非法，应为数字（单位秒）")
                return
            if duration < 0:
                QMessageBox.warning(self, "警告", "时长不能为负数")
                return
            self.config_data["duration"] = duration
            self.config_data.pop("click_area", None)
            self.config_data.pop("click_type", None)
            self.config_data.pop("end_area", None)
        elif action_type == "swipe":
            # 滑动起点复用目标（点击图片区域 / 点击自定义区域）
            if self.click_image_radio.isChecked():
                if "click_area" in self.config_data:
                    del self.config_data["click_area"]
                self.config_data["click_type"] = "image"
            else:
                area = _parse_click_area(self.custom_area_line_edit.text())
                if area is None:
                    QMessageBox.warning(self, "警告", "点击区域格式非法，应为 [x1, x2, y1, y2] 且满足 x1<=x2, y1<=y2")
                    return
                self.config_data["click_area"] = area
                self.config_data["click_type"] = "coordinate"
            end_area = _parse_click_area(self.end_area_line_edit.text())
            if end_area is None:
                QMessageBox.warning(self, "警告", "终点区域格式非法，应为 [x1, x2, y1, y2] 且满足 x1<=x2, y1<=y2")
                return
            self.config_data["end_area"] = end_area
            try:
                duration = float(self.duration_line_edit.text())
            except (ValueError, TypeError):
                QMessageBox.warning(self, "警告", "时长格式非法，应为数字（单位秒）")
                return
            if duration < 0:
                QMessageBox.warning(self, "警告", "时长不能为负数")
                return
            self.config_data["duration"] = duration
        elif self.click_image_radio.isChecked():
            # 移除click_area字段
            if "click_area" in self.config_data:
                del self.config_data["click_area"]
            self.config_data["click_type"] = "image"
            self.config_data.pop("end_area", None)
            self.config_data.pop("duration", None)
        else:
            # 设置自定义区域：非法输入 warn-and-return，绝不静默写默认区域
            area = _parse_click_area(self.custom_area_line_edit.text())
            if area is None:
                QMessageBox.warning(self, "警告", "点击区域格式非法，应为 [x1, x2, y1, y2] 且满足 x1<=x2, y1<=y2")
                return
            self.config_data["click_area"] = area
            self.config_data["click_type"] = "coordinate"
            self.config_data.pop("end_area", None)
            self.config_data.pop("duration", None)
        
        # 保存OCR配置
        self.config_data["ocr_enabled"] = self.is_ocr_enabled_checkbox.isChecked()
        if not self.config_data["ocr_enabled"]:
            # 未勾选纯 OCR/OCR：清除残留文字与 recognition，避免前向转换复活 pure-ocr
            self.config_data["ocr_target_text"] = ""
            self.config_data.pop("recognition", None)
        else:
            # OCR识别文本
            self.config_data["ocr_target_text"] = self.ocr_text_edit.text()
        
        # OCR识别阈值
        self.config_data["ocr_confidence_threshold"] = _normalize_confidence(self.ocr_threshold_edit.text())
        
        # OCR搜索区域（可选，与 click_area 同语义：[x1, x2, y1, y2]；留空则移除该字段）
        _ocr_region_raw = self.ocr_region_edit.text().strip()
        _ocr_region = _parse_click_area(_ocr_region_raw) if _ocr_region_raw else None
        if _ocr_region_raw and not _ocr_region:
            QMessageBox.warning(self, "警告", "OCR搜索区域格式不对，应为 [x1, x2, y1, y2]")
            return
        if _ocr_region:
            self.config_data["ocr_region"] = _ocr_region
        else:
            self.config_data.pop("ocr_region", None)
        
        # OCR识别后的操作（仅 click 类型需要点击区域校验；swipe/wait 自带动作语义）
        self.config_data["ocr_action"] = self.ocr_action_combo.currentText()
        if action_type == "click" and self.config_data["ocr_action"] == "点击点击区域设置的区域" and "click_area" not in self.config_data:
            QMessageBox.warning(self, "警告", "OCR操作需要点击区域，但当前未设置点击区域")
            return

        # OCR 点击区域（「点击框选区域内的随机位置」专用）
        # 留空时回退到「自定义区域」的坐标 —— 大多数情况下要点的就是同一块地方，
        # 不必重复框选；只有用户单独框了别的位置才用自己的值。
        if self.config_data["ocr_action"] == "点击框选区域内的随机位置":
            _occ_raw = self.ocr_click_area_edit.text().strip()
            _occ = _parse_click_area(_occ_raw) if _occ_raw else None
            if _occ is None:
                # 回退：用点击区域设置里的「自定义区域」
                _occ = _parse_click_area(self.custom_area_line_edit.text().strip())
            if _occ is None:
                QMessageBox.warning(
                    self, "警告",
                    "OCR 动作为「点击框选区域内的随机位置」时，需要指定点击区域：\n"
                    "可直接用上面的「自定义区域」，或用这里的「框选」单独指定。")
                return
            self.config_data["ocr_click_area"] = _occ
        else:
            self.config_data.pop("ocr_click_area", None)

        # 图片文件落地：复制到模式目录并按改后名字命名（含纯改名）
        if self.config_path:
            mode_dir = os.path.dirname(self.config_path)
            plan = _plan_image_file_replace(mode_dir, self.image_name, new_name, self.pending_image_source)
            needs_file_op = plan["need_copy"] or plan["remove_old"]
            if needs_file_op:
                dest_path = plan["dest_path"]
                old_path = plan["old_path"]
                # 目标已存在且不是老文件本身 → 弹窗确认覆盖
                if os.path.exists(dest_path) and os.path.abspath(dest_path) != os.path.abspath(old_path):
                    # 同文件（待选源就是目标）已在 need_copy=False 排除，此处是真覆盖
                    if not (plan["pending_source"] and os.path.abspath(plan["pending_source"]) == os.path.abspath(dest_path)):
                        reply = QMessageBox.question(
                            self, "确认覆盖",
                            f"目标文件已存在：\n{dest_path}\n确定要覆盖吗？",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.No,
                        )
                        if reply != QMessageBox.StandardButton.Yes:
                            return
                try:
                    _apply_image_file_replace(plan)
                except Exception as e:
                    QMessageBox.warning(self, "警告", f"替换图片文件失败: {e}")
                    return
        
        # 保存到文件
        if self.config_path:
            try:
                # 确保目录存在
                config_dir = os.path.dirname(self.config_path)
                if not os.path.exists(config_dir):
                    os.makedirs(config_dir)
                
                # 读取现有配置或创建新配置
                if os.path.exists(self.config_path):
                    with open(self.config_path, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                else:
                    config = {"image_paths": {}, "pipeline": {"tasks": []}}
                
                # 更新image_paths（兼容旧格式）
                if "image_paths" not in config:
                    config["image_paths"] = {}

                # 获取旧的图像键名（去除扩展名）
                old_image_key = os.path.splitext(self.image_name)[0]

                # 获取新的图像键名（去除扩展名）
                new_image_key = os.path.splitext(new_name)[0]   # 键 = 文件名主干（后端身份只看文件）

                # 如果名称改变，需要删除旧的配置并添加新的
                if new_image_key != old_image_key:
                    if old_image_key in config["image_paths"]:
                        del config["image_paths"][old_image_key]
                    # 重命名引用改写：其余 image_paths 条目的 next_image 指向旧名则更新
                    for k, v in config["image_paths"].items():
                        if isinstance(v, dict) and v.get("next_image") == old_image_key:
                            v["next_image"] = new_image_key
                    # 同时在 pipeline.tasks 中删除旧条目
                    if "pipeline" in config and "tasks" in config["pipeline"]:
                        config["pipeline"]["tasks"] = [
                            t for t in config["pipeline"]["tasks"]
                            if t.get("name") != old_image_key
                        ]
                        # 重命名引用改写：其余 task 的 next 指向旧名则更新（排除改名后的任务自身）
                        for t in config["pipeline"]["tasks"]:
                            if t.get("name") != new_image_key and t.get("next") == old_image_key:
                                t["next"] = new_image_key

                # 去掉值为 None 的冗余键，避免 JSON 里出现 null 污染旧格式
                for _k in ("threshold", "timeout", "max_retries", "max_consecutive"):
                    if self.config_data.get(_k) is None:
                        self.config_data.pop(_k, None)

                config["image_paths"][new_image_key] = self.config_data.copy()

                # 同步写入 pipeline.tasks 格式（如果 config 已有 pipeline）
                if "pipeline" in config:
                    if "tasks" not in config["pipeline"]:
                        config["pipeline"]["tasks"] = []
                    # 找到或创建匹配的 task
                    existing = None
                    for t in config["pipeline"]["tasks"]:
                        if t.get("name") == new_image_key:
                            existing = t
                            break
                    pipeline_task = _image_paths_to_pipeline_task(new_image_key, self.config_data)
                    if existing:
                        idx = config["pipeline"]["tasks"].index(existing)
                        config["pipeline"]["tasks"][idx] = pipeline_task
                    else:
                        config["pipeline"]["tasks"].append(pipeline_task)
                
                # 保存配置
                with open(self.config_path, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)

                if self.pending_image_source:
                    QMessageBox.information(self, "成功", f"图片已替换为 {new_name}，配置保存成功")
                else:
                    QMessageBox.information(self, "成功", "配置保存成功")
                # 保存成功后再物理删除停止图文件：写盘失败时保留，用户可重试不丢图
                if not self.stop_click_checkbox.isChecked():
                    self._discard_stop_files()
                self.accept()
            except Exception as e:
                QMessageBox.warning(self, "警告", f"保存配置失败: {e}")

class ModeEditorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("模式和图像编辑器")
        self.resize(800, 600)
        
        # 获取source目录的绝对路径
        self.source_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'source')
        # 构建mode.json的绝对路径
        self.mode_json_path = os.path.join(self.source_dir, 'mode.json')
        # 加载模式配置
        self.mode_config_data = self.load_mode_config()
        # 临时模式配置数据（用于存储未保存的模式）
        self.temp_mode_config = self.mode_config_data.copy()
        
        # 初始化UI
        self.setup_ui()
        
    def load_mode_config(self):
        """
        加载模式配置文件
        
        Returns:
            dict: 模式配置数据
        """
        try:
            with open(self.mode_json_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载模式配置失败: {e}")
            return {}
    
    def save_mode_config(self):
        """
        保存模式配置到文件
        """
        try:
            with open(self.mode_json_path, 'w', encoding='utf-8') as f:
                json.dump(self.mode_config_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"保存模式配置失败: {e}")
            return False
    
    def setup_ui(self):
        """
        设置UI界面
        """
        main_layout = QVBoxLayout(self)
        
        # 创建分割器
        splitter = QSplitter(QtCore.Qt.Orientation.Horizontal)
        
        # 左侧：模式管理区域
        mode_widget = QtWidgets.QWidget()
        mode_layout = QVBoxLayout(mode_widget)
        
        # 模式树
        self.mode_tree = QTreeWidget()
        self.mode_tree.setHeaderLabel("模式列表")
        self.mode_tree.setMinimumWidth(200)
        self.load_mode_tree()
        mode_layout.addWidget(self.mode_tree)
        
        # 模式操作按钮
        mode_buttons_layout = QHBoxLayout()
        self.add_mode_btn = QPushButton("添加模式")
        self.add_submode_btn = QPushButton("添加子模式")
        self.delete_mode_btn = QPushButton("删除模式")
        self.rename_mode_btn = QPushButton("重命名模式")
        
        mode_buttons_layout.addWidget(self.add_mode_btn)
        mode_buttons_layout.addWidget(self.add_submode_btn)
        mode_buttons_layout.addWidget(self.delete_mode_btn)
        mode_buttons_layout.addWidget(self.rename_mode_btn)
        
        mode_layout.addLayout(mode_buttons_layout)
        
        # 右侧：图像管理区域
        img_widget = QtWidgets.QWidget()
        img_layout = QVBoxLayout(img_widget)
        
        # 模式信息
        self.mode_info_label = QLabel("请选择一个模式")
        img_layout.addWidget(self.mode_info_label)
        
        # 图像列表
        self.img_list = QListWidget()
        self.img_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.img_list.setIconSize(QtCore.QSize(100, 100))
        self.img_list.setMinimumWidth(300)
        self.img_list.setToolTip("选中一张图按 Ctrl+C 复制（含配置），切到别的模式按 Ctrl+V 粘贴")
        self.img_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        img_layout.addWidget(self.img_list)

        # 剪贴板状态：告诉用户「复制了什么、可以粘到哪」
        self.clipboard_label = QLabel("")
        self.clipboard_label.setWordWrap(True)
        self.clipboard_label.setStyleSheet("color: #888;")
        img_layout.addWidget(self.clipboard_label)

        # 图像操作按钮（两行：一行放不下 9 个按钮）
        img_buttons_layout = QHBoxLayout()
        img_buttons_layout2 = QHBoxLayout()
        self.add_img_btn = QPushButton("添加图像")
        self.capture_img_btn = QPushButton("截图添加")
        self.capture_img_btn.setToolTip(
            "后台抓取游戏窗口画面并框选，截图尺寸与识别时一致，最适合做模板")
        self.delete_img_btn = QPushButton("删除图像")
        self.replace_img_btn = QPushButton("替换图片")
        self.replace_img_btn.setToolTip("选中一张图，从资源管理器选新图直接覆盖，文件名和配置不变")
        self.capture_replace_img_btn = QPushButton("截图替换")
        self.capture_replace_img_btn.setToolTip(
            "选中一张图，直接框选游戏画面覆盖它，文件名和配置不变（不用先存文件再选）")
        self.edit_img_config_btn = QPushButton("编辑图像配置")
        self.copy_img_btn = QPushButton("复制图片配置")
        self.copy_img_btn.setToolTip("复制选中的图片和它的全部配置（快捷键 Ctrl+C）")
        self.paste_img_btn = QPushButton("粘贴图片配置")
        self.paste_img_btn.setToolTip("把复制的图片和配置粘贴到当前选中的模式（快捷键 Ctrl+V）")
        self.usage_instructions_btn = QPushButton("查看使用说明")

        img_buttons_layout.addWidget(self.add_img_btn)
        img_buttons_layout.addWidget(self.capture_img_btn)
        img_buttons_layout.addWidget(self.delete_img_btn)
        img_buttons_layout.addWidget(self.replace_img_btn)
        img_buttons_layout.addWidget(self.capture_replace_img_btn)

        img_buttons_layout2.addWidget(self.edit_img_config_btn)
        img_buttons_layout2.addWidget(self.copy_img_btn)
        img_buttons_layout2.addWidget(self.paste_img_btn)
        img_buttons_layout2.addWidget(self.usage_instructions_btn)

        img_layout.addLayout(img_buttons_layout)
        img_layout.addLayout(img_buttons_layout2)
        
        # 将两个区域添加到分割器
        splitter.addWidget(mode_widget)
        splitter.addWidget(img_widget)
        splitter.setSizes([200, 600])
        
        # 底部按钮
        bottom_layout = QHBoxLayout()
        self.save_btn = QPushButton("保存")
        self.cancel_btn = QPushButton("取消")
        
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.save_btn)
        bottom_layout.addWidget(self.cancel_btn)
        
        main_layout.addWidget(splitter)
        main_layout.addLayout(bottom_layout)
        
        # 连接信号
        self.connect_signals()
    
    def load_mode_tree(self):
        """
        加载模式树

        `default` 是「没选子模式时」用的兜底目录，不是可选子模式，所以不在树里
        单独显示 —— 选中模式本身（顶级节点）编辑的就是它，见 on_mode_selected。
        早先把它当普通子模式显示，用户会以为多了一个叫 default 的子模式；
        彻底过滤又会连它的目录名都改不了，因此保留在数据里、只在树上隐藏。
        """
        self.mode_tree.clear()

        for mode_name, mode_data in self.temp_mode_config.items():
            root_item = QTreeWidgetItem([mode_name])

            if isinstance(mode_data, dict):
                for submode_name in mode_data:
                    if submode_name == DEFAULT_SUBMODE_KEY:
                        continue
                    sub_item = QTreeWidgetItem([submode_name])
                    root_item.addChild(sub_item)

            self.mode_tree.addTopLevelItem(root_item)
    
    def connect_signals(self):
        """
        连接信号和槽
        """
        # 模式树选择变化
        self.mode_tree.currentItemChanged.connect(self.on_mode_selected)
        
        # 模式操作按钮
        self.add_mode_btn.clicked.connect(self.add_mode)
        self.add_submode_btn.clicked.connect(self.add_submode)
        self.delete_mode_btn.clicked.connect(self.delete_mode)
        self.rename_mode_btn.clicked.connect(self.rename_mode)
        
        # 图像操作按钮
        self.add_img_btn.clicked.connect(self.add_image)
        self.capture_img_btn.clicked.connect(self.capture_and_add_image)
        self.delete_img_btn.clicked.connect(self.delete_image)
        self.replace_img_btn.clicked.connect(self.replace_image)
        self.capture_replace_img_btn.clicked.connect(self.capture_replace_image)
        self.edit_img_config_btn.clicked.connect(self.edit_image_config)
        self.copy_img_btn.clicked.connect(self.copy_selected_image)
        self.paste_img_btn.clicked.connect(self.paste_image)
        self.usage_instructions_btn.clicked.connect(self.show_usage_instructions)

        # 右键菜单：复制/粘贴放在最顺手的位置
        self.img_list.customContextMenuRequested.connect(self._show_img_context_menu)

        # Ctrl+C / Ctrl+V 复制粘贴图片+配置。
        # 本对话框里没有文本输入框，窗口级快捷键不会抢走文本框的复制粘贴。
        self.copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self)
        self.copy_shortcut.activated.connect(self.copy_selected_image)
        self.paste_shortcut = QShortcut(QKeySequence.StandardKey.Paste, self)
        self.paste_shortcut.activated.connect(self.paste_image)
        
        # 底部按钮
        self.save_btn.clicked.connect(self.save)
        self.cancel_btn.clicked.connect(self.reject)
    
    def on_mode_selected(self, current, previous):
        """
        处理模式选择事件
        
        Args:
            current: 当前选中的项
            previous: 之前选中的项
        """
        if not current:
            self.mode_info_label.setText("请选择一个模式")
            self.img_list.clear()
            return
        
        # 获取模式路径
        mode_path = []
        item = current
        while item:
            mode_path.insert(0, item.text(0))
            item = item.parent()
        
        if len(mode_path) == 1:
            # 顶级模式
            mode_name = mode_path[0]
            self.mode_info_label.setText(f"当前模式: {mode_name}")
        else:
            # 子模式
            mode_name = mode_path[0]
            submode_name = mode_path[1]
            if submode_name == DEFAULT_SUBMODE_KEY:
                # 说明它不是普通子模式，而是「没选子模式时」用的兜底目录
                self.mode_info_label.setText(
                    f"当前模式: {mode_name} -> 默认目录（未选择子模式时使用）")
            else:
                self.mode_info_label.setText(f"当前模式: {mode_name} -> {submode_name}")
        
        # 加载该模式的图像
        self.load_images(mode_path)
    
    def load_images(self, mode_path):
        """
        加载模式的图像
        
        Args:
            mode_path: 模式路径列表
        """
        self.img_list.clear()
        
        # 构建模式目录
        if len(mode_path) == 1:
            # 顶级模式
            mode_name = mode_path[0]
            # 从临时模式配置中获取模式对应的目录名
            mode_data = self.temp_mode_config.get(mode_name)
            if isinstance(mode_data, dict):
                dir_name = mode_data.get('default', '')
            else:
                dir_name = mode_data
        else:
            # 子模式
            mode_name = mode_path[0]
            submode_name = mode_path[1]
            mode_data = self.temp_mode_config.get(mode_name)
            if isinstance(mode_data, dict):
                dir_name = mode_data.get(submode_name, '')
            else:
                dir_name = mode_data
        
        # 构建图像目录路径
        if dir_name:
            mode_dir = os.path.join(self.source_dir, dir_name)
            if os.path.exists(mode_dir):
                # 加载配置文件
                config_path = os.path.join(mode_dir, 'config.json')
                config_data = {}
                if os.path.exists(config_path):
                    try:
                        with open(config_path, 'r', encoding='utf-8') as f:
                            config_data = json.load(f)
                    except Exception as e:
                        logger.error(f"加载配置文件失败: {e}")
                
                # 加载图像
                # 列表里显示「名称」(label，如 OCR 文字)，但条目数据里存真实文件名——
                # 显示与后端解耦：改显示名不会动配置键/文件名，所有后续操作都用文件名。
                label_map = {}
                for _key, _entry in (config_data.get("image_paths") or {}).items():
                    if isinstance(_entry, dict):
                        _p = _entry.get("path")
                        if isinstance(_p, str) and _p:
                            _lab = str(_entry.get("label") or "").strip()
                            if _lab:
                                label_map[_p.lower()] = _lab
                for t in ((config_data.get("pipeline") or {}).get("tasks") or []):
                    if isinstance(t, dict):
                        _p = t.get("template")
                        if isinstance(_p, str) and _p and _p.lower() not in label_map:
                            _lab = str(t.get("label") or "").strip()
                            if _lab:
                                label_map[_p.lower()] = _lab

                for file in os.listdir(mode_dir):
                    if file.endswith(('.png', '.jpg', '.jpeg', '.bmp')):
                        item = QListWidgetItem()
                        item.setText(label_map.get(file.lower(), file))
                        item.setData(QtCore.Qt.ItemDataRole.UserRole, file)

                        # 加载图像预览
                        img_path = os.path.join(mode_dir, file)
                        pixmap = QPixmap(img_path)
                        if not pixmap.isNull():
                            pixmap = pixmap.scaled(100, 100, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                            item.setIcon(QtGui.QIcon(pixmap))

                        self.img_list.addItem(item)

    @staticmethod
    def _item_file(item) -> str:
        """列表条目的真实文件名（显示文本可能是 label，不能当文件名用）。"""
        if item is None:
            return ""
        try:
            data = item.data(QtCore.Qt.ItemDataRole.UserRole)
        except Exception:
            data = None
        return data or (item.text() if item is not None else "")
    
    def _auto_dir_name(self, hint: str, fallback: str = "mode") -> str:
        """自动生成目录名：优先用 hint 里可用的英文字符，重名自动加序号。

        为什么不再让用户填：目录名只影响 source/ 下的落盘位置，用户看不到也用不到；
        手动输入既要校验英文、又要防重名，纯属负担。这里统一自动生成：
          - 名称本身是英文/数字/下划线 → 直接用（如 "PVE"、"pve2"）；
          - 含中文等非法字符 → 退化成 fallback / 父目录名 + 序号（如 "PVE_2"）；
          - 与已有目录或已配置目录重名 → 追加 _2/_3… 直到唯一。
        """
        used = set()
        for v in self.temp_mode_config.values():
            if isinstance(v, dict):
                used.update(str(x) for x in v.values())
            else:
                used.add(str(v))
        try:
            used.update(os.listdir(self.source_dir))
        except Exception:
            pass
        # Windows 目录名大小写不敏感：PVE 与 pve 是同一个目录，必须按小写比较，
        # 否则会"生成"一个与现有目录同名（不同大小写）的"新"目录。
        used = {s.lower() for s in used}

        base = str(hint or '')
        base = re.sub(r'\s+', '_', base)                  # 空格转下划线
        base = re.sub(r'[^A-Za-z0-9_]', '', base)         # 去掉中文等非法字符
        base = re.sub(r'_+', '_', base).strip('_')
        if not base or base.isdigit():
            base = fallback                               # 纯数字/中文名 → 通用名 + 序号
        base = base.lower()                               # 与现有 pve/dz/sf 风格统一

        if base not in used:
            return base
        n = 2
        while f"{base}_{n}" in used:
            n += 1
        return f"{base}_{n}"

    def _sanitize_image_stem(self, text: str, max_len: int = 24) -> str:
        """把 OCR 文字变成可用的文件名主干：留中英文/数字/_-，其余丢掉。

        用于「图片配置命名自动用识别到的 OCR 文字」——按钮文字常见中文，
        所以这里保留 CJK；长度截断避免一长串句子当文件名。
        """
        s = str(text or '').strip()
        if not s:
            return ''
        s = re.sub(r'\s+', '_', s)
        s = re.sub(r'[^0-9A-Za-z_\u4e00-\u9fff\-]', '', s)
        s = re.sub(r'_+', '_', s).strip('_.-')
        if len(s) > max_len:
            s = s[:max_len].strip('_.-')
        return s

    def _unique_image_name(self, stem: str, mode_dir: str, ext: str = ".png",
                           fallback: str = "") -> str:
        """在目标目录内取唯一文件名：<stem><ext>，重名依次加 _2/_3。

        stem 为空（没认到文字）时用 fallback（如 shot_时间戳），
        fallback 也为空才用 img_时间戳——保证永远能落盘。
        """
        existing = set()
        try:
            existing = {n.lower() for n in os.listdir(mode_dir)}
        except Exception:
            pass
        stem = (stem or fallback or ("img_" + time.strftime("%Y%m%d-%H%M%S"))).strip()
        ext = ext if ext.startswith('.') else f".{ext}"
        name = f"{stem}{ext}"
        n = 2
        while name.lower() in existing:
            name = f"{stem}_{n}{ext}"
            n += 1
        return name

    def _rename_image_key_in_config(self, mode_dir: str, old_key: str, new_key: str,
                                    update_filenames: bool = True) -> bool:
        """把 config.json 里的图片键 old_key 改成 new_key，并重写所有引用。

        update_filenames=True（真改名：文件也跟着改）时才同步 path/template；
        名称与文件名解耦的场景（名称用 OCR 文字、文件仍是 shot_xxx.png）传 False，
        这样只改配置键，不碰磁盘文件名。

        需要同步的东西：
          - image_paths: 键改名（+ 可选 path 文件名同步）
          - 其它条目的 next_image 指向旧键 → 改新键
          - pipeline.tasks: name 改名（+ 可选 template 文件名同步）；
            其余任务 next 指向旧键 → 改新键
        """
        cfg_path = os.path.join(mode_dir, 'config.json')
        if not os.path.exists(cfg_path) or old_key == new_key:
            return old_key == new_key
        old_ext = old_key  # 兼容下面用旧键推文件名
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            logger.warning(f"图片改名：读取配置失败 {cfg_path}: {e}")
            return False

        ip = config.get('image_paths')
        if isinstance(ip, dict):
            entry = ip.pop(old_key, None)
            if entry is not None:
                ip[new_key] = entry
            for k, v in ip.items():
                if not isinstance(v, dict):
                    continue
                # 指向旧键的引用
                if v.get('next_image') == old_key:
                    v['next_image'] = new_key
                # 文件名字段：同名条目里 path 仍是旧文件名时按新键改
                p = v.get('path')
                if (update_filenames and isinstance(p, str)
                        and os.path.splitext(p)[0] == old_ext):
                    v['path'] = new_key + (os.path.splitext(p)[1] or ".png")

        pipe = config.get('pipeline')
        if isinstance(pipe, dict) and isinstance(pipe.get('tasks'), list):
            for t in pipe['tasks']:
                if not isinstance(t, dict):
                    continue
                if t.get('name') == old_key:
                    t['name'] = new_key
                elif t.get('next') == old_key:
                    t['next'] = new_key
                tpl = t.get('template')
                if (update_filenames and isinstance(tpl, str)
                        and os.path.splitext(tpl)[0] == old_ext):
                    t['template'] = new_key + (os.path.splitext(tpl)[1] or ".png")

        try:
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"图片改名：写配置失败 {cfg_path}: {e}")
            return False
        return True

    def add_mode(self):
        """
        添加新模式
        """
        # 弹出输入对话框获取模式名称（只有显示名需要手填，目录名自动生成）
        mode_name, ok = QtWidgets.QInputDialog.getText(self, "添加模式", "请输入模式名称:")
        if not (ok and mode_name):
            return
        mode_name = mode_name.strip()
        if not mode_name:
            return

        dir_name = self._auto_dir_name(mode_name, fallback="mode")
        logger.info(f"添加模式：{mode_name} → 目录自动命名为 {dir_name}")

        if mode_name not in self.temp_mode_config:
            self.temp_mode_config[mode_name] = dir_name
            self.load_mode_tree()
        else:
            QMessageBox.warning(self, "警告", "模式名称已存在")
    
    def add_submode(self):
        """
        添加子模式
        """
        current_item = self.mode_tree.currentItem()
        if not current_item or current_item.parent():
            QMessageBox.warning(self, "警告", "请先选择一个顶级模式")
            return
        
        # 弹出输入对话框获取子模式名称（只有显示名需要手填，目录名自动生成）
        submode_name, ok = QtWidgets.QInputDialog.getText(self, "添加子模式", "请输入子模式名称:")
        if not (ok and submode_name):
            return
        submode_name = submode_name.strip()
        if not submode_name:
            return

        mode_name = current_item.text(0)

        # 自动目录名：提示词 = 父目录名 + 子模式名（中文名会被过滤，退化成父目录 + 序号，
        # 例如 PVE 下新增中文子模式 → pve_2），重名自动加后缀，全程不需要用户输入。
        _parent = self.temp_mode_config.get(mode_name)
        if isinstance(_parent, dict):
            _parent = next(iter(_parent.values()), "")
        submode_dir = self._auto_dir_name(f"{_parent}_{submode_name}", fallback="sub")
        logger.info(f"添加子模式：{mode_name} / {submode_name} → 目录自动命名为 {submode_dir}")
        
        # 如果模式数据不是字典，转换为字典
        if not isinstance(self.temp_mode_config[mode_name], dict):
            self.temp_mode_config[mode_name] = {"default": self.temp_mode_config[mode_name]}
        
        if submode_name not in self.temp_mode_config[mode_name]:
            self.temp_mode_config[mode_name][submode_name] = submode_dir
            self.load_mode_tree()
        else:
            QMessageBox.warning(self, "警告", "子模式名称已存在")
    
    def delete_mode(self):
        """
        删除模式
        """
        current_item = self.mode_tree.currentItem()
        if not current_item:
            QMessageBox.warning(self, "警告", "请选择一个模式")
            return
        
        # 获取模式路径
        mode_path = []
        item = current_item
        while item:
            mode_path.insert(0, item.text(0))
            item = item.parent()
        
        # 确认删除
        if QMessageBox.question(self, "确认删除", f"确定要删除{' -> '.join(mode_path)}吗？", 
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            if len(mode_path) == 1:
                # 删除顶级模式
                del self.temp_mode_config[mode_path[0]]
            else:
                # 删除子模式
                mode_name = mode_path[0]
                submode_name = mode_path[1]
                if submode_name == DEFAULT_SUBMODE_KEY:
                    # default 是兜底目录名，删掉后该模式在「没选子模式」时无法启动。
                    # 想清空它请把目录名改成空以外的有效值，而不是删除这一项。
                    QMessageBox.warning(
                        self, "无法删除",
                        f"「{DEFAULT_SUBMODE_KEY}」是这个模式的默认目录项，不能删除。\n"
                        f"它决定「没选子模式时」用哪个目录；如需更换，请直接修改它的目录名。")
                    return
                del self.temp_mode_config[mode_name][submode_name]
            
            self.load_mode_tree()
            self.img_list.clear()
            self.mode_info_label.setText("请选择一个模式")
    
    def rename_mode(self):
        """
        重命名模式
        """
        current_item = self.mode_tree.currentItem()
        if not current_item:
            QMessageBox.warning(self, "警告", "请选择一个模式")
            return
        
        # 获取模式路径
        mode_path = []
        item = current_item
        while item:
            mode_path.insert(0, item.text(0))
            item = item.parent()
        
        # 弹出输入对话框
        new_name, ok = QtWidgets.QInputDialog.getText(self, "重命名模式", "请输入新的模式名称:", 
                                                     text=mode_path[-1])
        if ok and new_name:
            if len(mode_path) == 1:
                # 重命名顶级模式
                if new_name != mode_path[0]:
                    if new_name not in self.temp_mode_config:
                        self.temp_mode_config[new_name] = self.temp_mode_config[mode_path[0]]
                        del self.temp_mode_config[mode_path[0]]
                        self.load_mode_tree()
                    else:
                        QMessageBox.warning(self, "警告", "模式名称已存在")
            else:
                # 重命名子模式
                mode_name = mode_path[0]
                submode_name = mode_path[1]
                if submode_name == DEFAULT_SUBMODE_KEY:
                    # 改名会让兜底键消失（变成普通子模式），该模式就没默认目录了。
                    QMessageBox.warning(
                        self, "无法重命名",
                        f"「{DEFAULT_SUBMODE_KEY}」是保留的默认目录项，不能改名。\n"
                        f"如需调整默认目录，请修改它对应的目录名。")
                    return
                if new_name != submode_name:
                    if new_name not in self.temp_mode_config[mode_name]:
                        self.temp_mode_config[mode_name][new_name] = self.temp_mode_config[mode_name][submode_name]
                        del self.temp_mode_config[mode_name][submode_name]
                        self.load_mode_tree()
                    else:
                        QMessageBox.warning(self, "警告", "子模式名称已存在")
    
    def add_image(self):
        """
        添加图像
        """
        current_item = self.mode_tree.currentItem()
        if not current_item:
            QMessageBox.warning(self, "警告", "请选择一个模式")
            return
        
        # 获取模式路径
        mode_path = []
        item = current_item
        while item:
            mode_path.insert(0, item.text(0))
            item = item.parent()
        
        # 构建模式目录
        if len(mode_path) == 1:
            # 顶级模式
            mode_name = mode_path[0]
            # 从临时模式配置中获取模式对应的目录名
            mode_data = self.temp_mode_config.get(mode_name)
            if isinstance(mode_data, dict):
                dir_name = mode_data.get('default', '')
            else:
                dir_name = mode_data
        else:
            # 子模式
            mode_name = mode_path[0]
            submode_name = mode_path[1]
            mode_data = self.temp_mode_config.get(mode_name)
            if isinstance(mode_data, dict):
                dir_name = mode_data.get(submode_name, '')
            else:
                dir_name = mode_data
        
        # 打开文件选择对话框
        files, _ = QFileDialog.getOpenFileNames(self, "选择图像", "", "图像文件 (*.png *.jpg *.jpeg *.bmp)")
        if files:
            # 构建图像目录路径
            if dir_name:
                mode_dir = os.path.join(self.source_dir, dir_name)
                if not os.path.exists(mode_dir):
                    os.makedirs(mode_dir)
                
                # 先规划：磁盘文件名保持 ASCII（原文件名/时间戳），OCR 文字只作为「显示名称」。
                # 中文文件名会在 OpenCV 等环节带来编码坑，而名称只是配置键/显示名，
                # 二者解耦后既好认又不踩雷。
                plan = []          # [(源文件, 目标文件名, 显示名)]
                for file in files:
                    ext = os.path.splitext(file)[1].lower() or ".png"
                    try:
                        ocr_text, _ = self._detect_text_on_crop_path(file)
                    except Exception:
                        ocr_text = ""
                    base = os.path.splitext(os.path.basename(file))[0]
                    if not base.isascii():
                        base = "shot_" + time.strftime("%Y%m%d-%H%M%S")
                    name_on_disk = self._unique_image_name("", mode_dir, ext, fallback=base)
                    plan.append((file, name_on_disk, ocr_text or ""))

                # 复制文件（目标名已保证不重名）
                for src, name, _disp in plan:
                    dest_path = os.path.join(mode_dir, name)
                    try:
                        shutil.copy(src, dest_path)
                    except Exception as e:
                        QMessageBox.warning(self, "警告", f"复制文件失败: {e}")

                # 构建配置文件路径
                config_path = os.path.join(mode_dir, 'config.json')

                # 重新加载图像
                self.load_images(mode_path)

                # 弹出图像配置对话框
                for src, filename, disp in plan:
                    # 查找新添加的图像项
                    for i in range(self.img_list.count()):
                        item = self.img_list.item(i)
                        if self._item_file(item) == filename:
                            # 选择该图像
                            self.img_list.setCurrentItem(item)
                            # 获取当前模式的所有图像名称
                            image_list = []
                            for i in range(self.img_list.count()):
                                item = self.img_list.item(i)
                                image_list.append(self._item_file(item))

                            # 打开配置对话框：名称预填 OCR 文字（显示名），路径仍是文件名
                            dialog = ImageConfigDialog(
                                self, filename, {}, config_path, image_list,
                                display_name=disp)
                            if dialog.exec() == QDialog.DialogCode.Accepted:
                                # 名称只影响配置键，不改磁盘文件名
                                self.load_images(mode_path)
                            break
            else:
                QMessageBox.warning(self, "警告", "模式对应的目录名未设置")
    
    def capture_and_add_image(self):
        """截图添加：后台抓游戏窗口 → 框选 → 存成模板并弹出配置对话框

        与「添加图像」的区别：不用先去别处截图再选文件，直接在 OAT 里框选。
        因为抓的是游戏窗口客户区，**截出来的尺寸就是识别时用的尺寸**，
        不会出现「模板在 A 尺寸下截、运行时是 B 尺寸」导致的匹配失败。
        """
        mode_path = self._get_current_mode_path()
        if not mode_path:
            QMessageBox.warning(self, "警告", "请选择一个模式")
            return
        dir_name = self._resolve_dir_name(mode_path)
        if not dir_name:
            QMessageBox.warning(self, "警告", "模式对应的目录名未设置")
            return

        mode_dir = os.path.join(self.source_dir, dir_name)
        if not os.path.exists(mode_dir):
            os.makedirs(mode_dir)

        # 取当前选中的客户端窗口（与主界面一致）
        hwnd, title = self._current_game_window()

        from OAT.tools.region_capture import capture_region_from_window, save_crop

        # 注意：这个工具自己弹遮罩完成交互，构造返回时结果就已就绪，
        # 不能对它 exec()（会弹出一个空白对话框并卡住）。
        crop, crop_rect, capture_size = capture_region_from_window(
            self, hwnd=hwnd, window_title=title)
        if crop is None:
            return

        w, h = crop.shape[1], crop.shape[0]
        cap = capture_size or (0, 0)

        # 顺手认一下框选出来的文字，填进对话框的「OCR识别文本」
        # initial_cfg 必须先建好再写：原先先写后建，OCR 一旦认出文字就抛
        # UnboundLocalError，表现为「截图添加」点了没反应、图也没加上。
        initial_cfg = {}
        ocr_text, ocr_candidates = self._detect_text_on_crop(crop)
        if ocr_text:
            initial_cfg["ocr_target_text"] = ocr_text
            initial_cfg["ocr_enabled"] = True

        # 文件名固定用 ASCII 时间戳（路径不按 OCR 命名）：中文文件名在 OpenCV 等
        # 环节有编码坑；识别到的文字只作为「图像名称」（配置键/显示名）。
        filename = self._unique_image_name(
            "", mode_dir, ".png",
            fallback="shot_" + time.strftime("%Y%m%d-%H%M%S"))
        dest_path = os.path.join(mode_dir, filename)

        if not save_crop(crop, dest_path):
            QMessageBox.warning(self, "警告", f"保存截图失败: {dest_path}")
            return

        # 框选位置 (x, y, w, h) 就是客户区坐标，和识别/点击用的是同一套坐标系，
        # 所以可以直接换算成配置里的区域格式 [x1, x2, y1, y2] 预填进去，
        # 省得用户再去猜坐标。这是「截图时记录位置」的核心。
        if crop_rect:
            rx, ry, rw, rh = crop_rect
            box = [int(rx), int(rx + rw), int(ry), int(ry + rh)]
            # ocr_region 是纯约束（留空=全窗口），预填不会改变默认行为，
            # 正好用来解决"同页两处相同文字点错位置"的问题。
            initial_cfg["ocr_region"] = box
            # click_area 只作为参考值展示（用户可点「框选」改成显式自定义区域）；
            # 显式写 click_type=image，避免默认点击方式被悄悄切成「自定义区域」。
            initial_cfg["click_area"] = box
            initial_cfg["click_type"] = "image"
            logger.info(f"截图添加：{filename}  {w}x{h}  @({rx},{ry})  "
                        f"来自窗口画面 {cap[0]}x{cap[1]}  区域[x1,x2,y1,y2]={box}")
        else:
            logger.info(f"截图添加：{filename}  {w}x{h}  来自窗口画面 {cap[0]}x{cap[1]}")

        if ocr_text:
            logger.info(f"截图添加：自动识别到文字 '{ocr_text}'，已预填到 OCR 识别文本")
        elif ocr_candidates is not None:
            logger.info(f"截图添加：框选区域没认到文字（共 {len(ocr_candidates)} 条候选）")

        # 尺寸提醒：模板必须和识别时的窗口尺寸一致
        if cap[0] and (w > cap[0] or h > cap[1]):
            QMessageBox.warning(self, "警告", "截图尺寸异常，请重新截图")

        self.load_images(mode_path)

        # 选中新图并弹出配置对话框（与 add_image 行为一致）
        config_path = os.path.join(mode_dir, 'config.json')
        for i in range(self.img_list.count()):
            item = self.img_list.item(i)
            if self._item_file(item) == filename:
                self.img_list.setCurrentItem(item)
                break

        image_list = [self.img_list.item(i).text() for i in range(self.img_list.count())]
        # 把记录下来的位置作为初始配置带进对话框；名称预填 OCR 文字（仅显示名）
        cfg = ImageConfigDialog(self, filename, initial_cfg, config_path, image_list,
                                display_name=self._sanitize_image_stem(ocr_text) if ocr_text else "")
        if cfg.exec() == QDialog.DialogCode.Accepted:
            # 名称写入配置键，磁盘文件名保持不变（路径不按 OCR 命名）
            self.load_images(mode_path)

    @staticmethod
    def _detect_text_on_crop(crop):
        """对刚框选出来的图做 OCR，返回 (主文字, 全部候选)。

        用途：截图添加 / 截图替换时自动把图上的文字填进 OCR 配置，
        避免「图换了、OCR 目标文字还是旧的」导致点错位置。
        永不抛异常；OCR 不可用时返回 ("", None) 让调用方静默跳过。
        """
        try:
            from OAT.utils.OCRService import ocr_service
        except Exception as e:
            logger.warning(f"自动识别文字：OCR 服务不可用（{e}），请手动填写")
            return "", None

        try:
            # 传原始 crop：极性处理统一在 recognize_all_text 内部做（原始图 +
            # box_thresh=0.5 为主、提对比度兜底）。这里不能再自己加 alpha=1.5——
            # 暗字亮底的图（如『召唤鬼王』）加对比度后 det 反而全空。
            items = ocr_service.recognize_all_text(crop)
        except Exception as e:
            logger.warning(f"自动识别文字失败: {e}")
            return "", None

        if not items:
            return "", []

        # 主文字选法（与 _autofill_ocr_from_image 共用）：优先把「同一按钮内的
# 多行文字」按阅读顺序拼成完整文案（『召唤』+『鬼王』→『召唤鬼王』），
# 框太散/太长则退回置信 ≥0.8 的阅读顺序第一个。运行端已支持相邻行拼接
# 匹配（_match_joined_lines），所以填整词是安全的。
        text = ModeEditorDialog._compose_autofill_text(items)
        best = next((d for d in items if (d.get('text') or '').strip() == text), None)
        if len(items) > 1:
            others = '、'.join(f"{d['text']}({d.get('confidence', 0):.2f})"
                              for d in items if d is not best)
            logger.info(f"  图上其余文字（未采用）：{others}")
        return text, items

    @staticmethod
    def _compose_autofill_text(items, max_len: int = 12) -> str:
        """把识别到的多行文字合成「按钮完整文案」；不可信时退回单行。

        规则：
          - 单条 → 直接用；
          - 多条且总长 ≤ max_len、且各文字框聚成一团（并集面积 ≤ 各框面积和的
            3 倍，说明是同一个按钮的 2x2/多行排版）→ 按阅读顺序拼接；
          - 否则（框太散，可能是画面里两处无关文字）→ 只取第一个高置信行。
        """
        try:
            entries = []
            for d in items or []:
                t = str(d.get('text') or '').strip()
                if t:
                    entries.append((t, d.get('area')))
            if not entries:
                return ''
            if len(entries) == 1:
                return entries[0][0]

            joined = ''.join(t for t, _a in entries)
            if len(joined) > max_len:
                return entries[0][0]

            boxes = []
            entry_boxes = []
            for _t, a in entries:
                try:
                    xs = [float(p[0]) for p in a]
                    ys = [float(p[1]) for p in a]
                    if xs and ys:
                        b = (min(xs), min(ys), max(xs), max(ys))
                        boxes.append(b)
                        entry_boxes.append(b)
                except Exception:
                    pass
            if len(boxes) < 2:
                return entries[0][0]

            uw = max(b[2] for b in boxes) - min(b[0] for b in boxes)
            uh = max(b[3] for b in boxes) - min(b[1] for b in boxes)
            union_area = max(1.0, uw * uh)
            sum_area = sum(max(1.0, (b[2] - b[0]) * (b[3] - b[1])) for b in boxes)
            if union_area > sum_area * 3.0:
                return entries[0][0]          # 分散在不同位置，不像同一按钮

            # 按「行聚类」还原阅读顺序：竖直方向有重叠的算同一行，行内按 x 排。
            # 不能简单按 y/常数 分桶——2x2 排版的『鬼』『王』常落在相邻桶里被排反。
            pairs = [(t, b) for (t, _a), b in zip(entries, entry_boxes)] \
                if len(entry_boxes) == len(entries) else None
            if pairs is None:
                return joined
            rows = []
            for t, b in sorted(pairs, key=lambda p: p[1][1]):
                for row in rows:
                    ry1 = min(x[1][1] for x in row)
                    ry2 = max(x[1][3] for x in row)
                    overlap = min(b[3], ry2) - max(b[1], ry1)
                    if overlap > 0.5 * min(b[3] - b[1], ry2 - ry1):
                        row.append((t, b))
                        break
                else:
                    rows.append([(t, b)])
            rows.sort(key=lambda r: min(x[1][1] for x in r))
            ordered = []
            for row in rows:
                row.sort(key=lambda p: p[1][0])
                ordered.extend(t for t, _b in row)
            return ''.join(ordered)
        except Exception:
            try:
                return str((items or [{}])[0].get('text') or '').strip()
            except Exception:
                return ''

    @classmethod
    def _detect_text_on_crop_path(cls, image_path: str):
        """同 _detect_text_on_crop，但入参是图片文件路径（供「替换图片」用）。"""
        try:
            import cv2
            from OAT.utils.image_io import imread as _imread_safe
            img = _imread_safe(image_path)
        except Exception as e:
            logger.warning(f"自动识别文字：无法读取图片 {image_path}（{e}）")
            return "", None
        if img is None:
            logger.warning(f"自动识别文字：无法读取图片 {image_path}")
            return "", None
        return cls._detect_text_on_crop(img)

    @staticmethod
    def _read_ocr_text_of(mode_dir: str, key: str) -> str:
        """读取某张图当前的 OCR 目标文字（pipeline 为准，回退 image_paths）。"""
        try:
            cfg_path = os.path.join(mode_dir, 'config.json')
            if not os.path.exists(cfg_path):
                return ""
            with open(cfg_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            for task in (config.get('pipeline', {}) or {}).get('tasks', []) or []:
                if task.get('name') == key:
                    return (task.get('ocr_text') or '').strip()
            entry = (config.get('image_paths', {}) or {}).get(key, {})
            if isinstance(entry, dict):
                return (entry.get('ocr_target_text') or '').strip()
        except Exception as e:
            logger.warning(f"读取 OCR 文字失败（{key}）: {e}")
        return ""

    @staticmethod
    def _write_label_of(mode_dir: str, key: str, label: str) -> bool:
        """写「显示名」(label) 到 config.json（image_paths 与 pipeline 同步）。

        label 纯显示用：不影响配置键、文件名、template 与任何识别行为——
        改显示名不该造成后端配置变动（刻意解耦）。
        """
        cfg_path = os.path.join(mode_dir, 'config.json')
        if not os.path.exists(cfg_path):
            return False
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            label = str(label or '').strip()
            changed = False
            ent = (config.get('image_paths') or {}).get(key)
            if isinstance(ent, dict) and (ent.get('label') or '') != label:
                ent['label'] = label
                changed = True
            for t in ((config.get('pipeline') or {}).get('tasks') or []):
                if isinstance(t, dict) and t.get('name') == key \
                        and (t.get('label') or '') != label:
                    t['label'] = label
                    changed = True
            if not changed:
                return True
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning(f"写入显示名失败（{key}）: {e}")
            return False

    @staticmethod
    def _write_ocr_text_of(mode_dir: str, key: str, new_text: str) -> bool:
        """把某张图的 OCR 目标文字写进 config.json（image_paths 与 pipeline 同步）。

        运行时的真相是 pipeline.tasks（见 runner），但旧编辑器读的是
        image_paths，两处必须一起改，否则界面显示和实际行为会不一致。
        写失败返回 False，调用方提示用户手动改。
        """
        try:
            cfg_path = os.path.join(mode_dir, 'config.json')
            if not os.path.exists(cfg_path):
                return False
            with open(cfg_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            changed = False
            entry = (config.get('image_paths', {}) or {}).get(key)
            if isinstance(entry, dict):
                entry['ocr_target_text'] = new_text
                # 图上有字，OCR 兜底才有意义；顺带打开开关
                entry['ocr_enabled'] = True
                changed = True

            for task in (config.get('pipeline', {}) or {}).get('tasks', []) or []:
                if task.get('name') == key:
                    task['ocr_text'] = new_text
                    # 模板+OCR：模板命中优先，失败时用 OCR 兜底
                    if task.get('recognition') != 'ocr':
                        task['recognition'] = 'template+ocr'
                    changed = True
                    break

            if not changed:
                return False

            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning(f"写入 OCR 文字失败（{key}）: {e}")
            return False

    def _current_game_window(self):
        """尽力取到当前游戏窗口的 (hwnd, title)。

        编辑器是独立对话框，拿不到主界面的选中项时回落到 client.json 里的标题。
        """
        # 1) 主界面窗口上的选中句柄
        try:
            mw = self.parent()
            for _ in range(4):
                if mw is None:
                    break
                hwnd = getattr(mw, 'selected_hwnd', None)
                title = getattr(mw, 'window_title', None)
                if hwnd or title:
                    return int(hwnd or 0), (title or "")
                mw = mw.parent()
        except Exception:
            pass

        # 2) client.json 的第一个标题
        try:
            import json
            cj = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'client.json')
            if os.path.exists(cj):
                with open(cj, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                titles = data.get('title', {})
                if isinstance(titles, dict) and titles:
                    first = titles.get('0') or next(iter(titles.values()))
                    return 0, str(first)
        except Exception:
            pass

        return 0, ""

    def delete_image(self):
        """
        删除图像
        """
        current_item = self.img_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "警告", "请选择一个图像")
            return
        
        # 获取模式路径
        mode_tree_item = self.mode_tree.currentItem()
        mode_path = []
        item = mode_tree_item
        while item:
            mode_path.insert(0, item.text(0))
            item = item.parent()
        
        # 构建模式目录
        if len(mode_path) == 1:
            # 顶级模式
            mode_name = mode_path[0]
            # 从临时模式配置中获取模式对应的目录名
            mode_data = self.temp_mode_config.get(mode_name)
            if isinstance(mode_data, dict):
                dir_name = mode_data.get('default', '')
            else:
                dir_name = mode_data
        else:
            # 子模式
            mode_name = mode_path[0]
            submode_name = mode_path[1]
            mode_data = self.temp_mode_config.get(mode_name)
            if isinstance(mode_data, dict):
                dir_name = mode_data.get(submode_name, '')
            else:
                dir_name = mode_data
        
        # 构建图像路径
        if dir_name:
            img_name = self._item_file(current_item)
            img_path = os.path.join(self.source_dir, dir_name, img_name)
            
            # 确认删除
            if QMessageBox.question(self, "确认删除", f"确定要删除 {img_name} 吗？", 
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                if os.path.exists(img_path):
                    try:
                        os.remove(img_path)
                        # 图片列表是扫目录建出来的，删文件不会动 config.json；
                        # 必须同时清掉配置条目，否则下次开始挑战仍会加载这条死记录。
                        self._purge_image_config(os.path.join(self.source_dir, dir_name),
                                                 os.path.splitext(img_name)[0])
                        self.img_list.takeItem(self.img_list.row(current_item))
                    except Exception as e:
                        QMessageBox.warning(self, "警告", f"删除文件失败: {e}")

    @staticmethod
    def _purge_image_config(mode_dir: str, image_key: str) -> bool:
        """从模式 config.json 里移除某张图的条目（image_paths + pipeline.tasks 双写）。

        运行时真相是 pipeline.tasks，旧编辑器读 image_paths，两处都要删。
        同时清理其它条目里指向它的 next / next_image 引用，避免留下指向空条目的
        死链（死链会让识别流程卡在「等一个永远不会命中的图」直到超时）。
        返回是否真的写盘了。
        """
        cfg_path = os.path.join(mode_dir, 'config.json')
        if not os.path.exists(cfg_path):
            return False
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            if not isinstance(config, dict):
                return False

            changed = False

            image_paths = config.get('image_paths')
            # 先收该条目引用的停止图（image_paths 侧 + pipeline 侧），删条目时一并清文件
            stop_rels = []
            entry = image_paths.get(image_key) if isinstance(image_paths, dict) else None
            if isinstance(entry, dict):
                stop_rels.extend(entry.get('stop_images') or [])
            pipeline = config.get('pipeline')
            tasks = pipeline.get('tasks') if isinstance(pipeline, dict) else None
            if isinstance(tasks, list):
                for t in tasks:
                    if isinstance(t, dict) and t.get('name') == image_key:
                        stop_rels.extend(t.get('stop_images') or [])
                        break
            for rel in stop_rels:
                try:
                    p = os.path.join(mode_dir, rel)
                    if os.path.exists(p):
                        os.remove(p)
                except Exception as e:
                    logger.warning(f"删除停止图失败（忽略）: {rel}: {e}")

            if isinstance(image_paths, dict) and image_key in image_paths:
                del image_paths[image_key]
                changed = True

            if isinstance(tasks, list):
                kept = [t for t in tasks
                        if not (isinstance(t, dict) and t.get('name') == image_key)]
                if len(kept) != len(tasks):
                    pipeline['tasks'] = kept
                    changed = True

            # 悬空引用清理：谁还指着这张图，就把那个引用清空
            if isinstance(image_paths, dict):
                for entry in image_paths.values():
                    if isinstance(entry, dict) and entry.get('next_image') == image_key:
                        entry['next_image'] = ''
                        changed = True
            if isinstance(tasks, list):
                for task in tasks:
                    if isinstance(task, dict) and task.get('next') == image_key:
                        task['next'] = ''
                        changed = True

            if not changed:
                return False

            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            logger.info(f"已从配置中移除图像条目: {os.path.basename(mode_dir)}/{image_key}")
            return True
        except Exception as e:
            logger.warning(f"移除图像配置失败（{image_key}）: {e}")
            return False
     
    def _get_current_mode_path(self):
        """从模式树获取当前选中的模式路径，如 ['魂王'] 或 ['魂土', '队长']，未选中返回 []"""
        mode_tree_item = self.mode_tree.currentItem()
        if not mode_tree_item:
            return []
        mode_path = []
        item = mode_tree_item
        while item:
            mode_path.insert(0, item.text(0))
            item = item.parent()
        return mode_path

    def _resolve_dir_name(self, mode_path):
        """由模式路径解析到 source 下的目录名，解析不出返回 ''"""
        if not mode_path:
            return ""
        if len(mode_path) == 1:
            mode_data = self.temp_mode_config.get(mode_path[0])
            if isinstance(mode_data, dict):
                return mode_data.get('default', '') or ""
            return mode_data or ""
        mode_data = self.temp_mode_config.get(mode_path[0])
        if isinstance(mode_data, dict):
            return mode_data.get(mode_path[1], '') or ""
        return mode_data or ""

    def replace_image(self):
        """
        替换图片（主界面快捷入口）：选中一张图后，从资源管理器选新图直接覆盖。
        文件名和 config.json 配置不变，与编辑配置对话框内的替换逻辑同源。
        """
        current_item = self.img_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "警告", "请选择一个图像")
            return

        mode_path = self._get_current_mode_path()
        if not mode_path:
            QMessageBox.warning(self, "警告", "请先选择一个模式")
            return
        dir_name = self._resolve_dir_name(mode_path)
        if not dir_name:
            QMessageBox.warning(self, "警告", "模式对应的目录名未设置")
            return

        img_name = self._item_file(current_item)
        mode_dir = os.path.join(self.source_dir, dir_name)
        dest_path = os.path.join(mode_dir, img_name)
        if not os.path.exists(dest_path):
            QMessageBox.warning(self, "警告", f"原图片文件不存在，无法替换：{img_name}")
            return

        src_path, _ = QFileDialog.getOpenFileName(
            self, "选择替换图片", "", "图像文件 (*.png *.jpg *.jpeg *.bmp)"
        )
        if not src_path:
            return
        if os.path.abspath(src_path) == os.path.abspath(dest_path):
            QMessageBox.information(self, "提示", "选择的就是当前图片，无需替换")
            return

        # 先认新图上的文字：替换后若 OCR 目标文字还是旧词，模板一失败就会点错
        key = os.path.splitext(img_name)[0]
        ocr_text, ocr_candidates = self._detect_text_on_crop_path(src_path)
        old_ocr_text = self._read_ocr_text_of(mode_dir, key)
        apply_ocr_change = False
        if ocr_text:
            if old_ocr_text and old_ocr_text != ocr_text:
                reply = QMessageBox.question(
                    self, "同步 OCR 文字",
                    f"新图上的文字是「{ocr_text}」，\n"
                    f"而当前配置里的 OCR 识别文本是「{old_ocr_text}」。\n\n"
                    f"要把 OCR 识别文本改成「{ocr_text}」吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                apply_ocr_change = (reply == QMessageBox.StandardButton.Yes)
            elif not old_ocr_text:
                apply_ocr_change = True

        if QMessageBox.question(
            self, "确认替换",
            f"确定要用\n{os.path.basename(src_path)}\n替换\n{img_name}\n吗？原图将被覆盖，文件名不变。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        try:
            plan = _plan_image_file_replace(mode_dir, img_name, img_name, src_path)
            _apply_image_file_replace(plan)
        except Exception as e:
            QMessageBox.warning(self, "警告", f"替换图片失败: {e}")
            return

        ocr_note = ""
        if apply_ocr_change and ocr_text:
            if self._write_ocr_text_of(mode_dir, key, ocr_text):
                ocr_note = f"\nOCR 识别文本已同步为「{ocr_text}」"
                logger.info(f"替换图片：OCR 识别文本已更新 '{old_ocr_text}' → '{ocr_text}'")
            else:
                ocr_note = f"\n（OCR 识别文本同步失败，请手动改成「{ocr_text}」）"
        elif ocr_text and old_ocr_text and old_ocr_text != ocr_text:
            ocr_note = f"\n（保留了原 OCR 识别文本「{old_ocr_text}」）"

        QMessageBox.information(self, "成功", f"图片已替换：{img_name}{ocr_note}")
        self.load_images(mode_path)

    def capture_replace_image(self):
        """截图替换：选中一张图，直接框选游戏画面覆盖它。

        与「替换图片」的区别：不用先截好图存成文件再去资源管理器挑，
        框选完直接覆盖。抓到文字时会按识别文字给图片改名（并同步改配置键与
        next 引用）；没认到文字则保持原文件名。
        抓的是游戏窗口客户区，尺寸与识别时一致，能避免模板尺寸不匹配。
        """
        current_item = self.img_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "警告", "请选择一个图像")
            return

        mode_path = self._get_current_mode_path()
        if not mode_path:
            QMessageBox.warning(self, "警告", "请先选择一个模式")
            return
        dir_name = self._resolve_dir_name(mode_path)
        if not dir_name:
            QMessageBox.warning(self, "警告", "模式对应的目录名未设置")
            return

        img_name = self._item_file(current_item)
        mode_dir = os.path.join(self.source_dir, dir_name)
        dest_path = os.path.join(mode_dir, img_name)
        if not os.path.exists(dest_path):
            QMessageBox.warning(self, "警告", f"原图片文件不存在，无法替换：{img_name}")
            return

        from OAT.tools.region_capture import capture_region_from_window, save_crop

        # 与「截图添加」同一套：工具自己弹遮罩，构造返回时结果已就绪，不能 exec()
        hwnd, title = self._current_game_window()
        crop, crop_rect, capture_size = capture_region_from_window(
            self, hwnd=hwnd, window_title=title)
        if crop is None:
            return

        w, h = crop.shape[1], crop.shape[0]
        cap = capture_size or (0, 0)

        # 认一下新框选的文字：替换后配置里的 OCR 目标文字若还是旧图上的词，
        # 模板匹配一失败就会按旧词去 OCR，可能点到别处。
        ocr_text, ocr_candidates = self._detect_text_on_crop(crop)
        key = os.path.splitext(img_name)[0]
        old_ocr_text = self._read_ocr_text_of(mode_dir, key)
        apply_ocr_change = False
        if ocr_text:
            if old_ocr_text and old_ocr_text != ocr_text:
                reply = QMessageBox.question(
                    self, "同步 OCR 文字",
                    f"新图上的文字是「{ocr_text}」，\n"
                    f"而当前配置里的 OCR 识别文本是「{old_ocr_text}」。\n\n"
                    f"要把 OCR 识别文本改成「{ocr_text}」吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                apply_ocr_change = (reply == QMessageBox.StandardButton.Yes)
            elif not old_ocr_text:
                apply_ocr_change = True

        if QMessageBox.question(
            self, "确认替换",
            f"确定用刚框选的 {w}x{h} 画面替换\n{img_name}\n吗？\n"
            f"原图将被覆盖"
            + (f"，并按识别文字改名为「{self._sanitize_image_stem(ocr_text)}」。" if ocr_text
               else "，文件名不变（没认到文字）。"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        # 先备份原图，写失败时能回滚（避免"替换失败但原图已没了"）
        backup_path = dest_path + ".bak"
        try:
            shutil.copy(dest_path, backup_path)
        except Exception as e:
            logger.warning(f"截图替换：备份原图失败（继续尝试替换）: {e}")
            backup_path = None

        if not save_crop(crop, dest_path):
            QMessageBox.warning(self, "警告", f"替换失败：无法写入 {img_name}")
            return

        logger.info(f"截图替换：{img_name}  {w}x{h}  来自窗口画面 {cap[0]}x{cap[1]}")

        # 尺寸提醒：模板尺寸应小于等于识别时的窗口画面
        if cap[0] and (w > cap[0] or h > cap[1]):
            QMessageBox.warning(self, "警告", "截图尺寸比窗口画面还大，可能不对，请重新截图")

        if backup_path and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except Exception:
                pass

        # 按识别文字更新「显示名」(label)，**不动磁盘文件名、不动配置键/template**：
        # 显示与后端解耦，改名不会引起任何后端配置变动。
        rename_note = ""
        new_key = key
        if ocr_text:
            label = self._sanitize_image_stem(ocr_text) or ocr_text.strip()
            if label and self._write_label_of(mode_dir, key, label):
                rename_note = f"\n显示名已按识别文字设为「{label}」"
                logger.info(f"截图替换：显示名 {key} → {label}（配置键/文件名不变）")
            else:
                logger.warning(f"截图替换：显示名写入失败（保持 {key}）")

        # 按用户确认的结果同步 OCR 文字（图片已落地，这一步失败不影响替换本身）
        ocr_note = ""
        if apply_ocr_change and ocr_text:
            if self._write_ocr_text_of(mode_dir, new_key, ocr_text):
                ocr_note = f"\nOCR 识别文本已同步为「{ocr_text}」"
                logger.info(f"截图替换：OCR 识别文本已更新 '{old_ocr_text}' → '{ocr_text}'")
            else:
                ocr_note = f"\n（OCR 识别文本同步失败，请手动改成「{ocr_text}」）"
        elif ocr_text and old_ocr_text and old_ocr_text != ocr_text:
            ocr_note = f"\n（保留了原 OCR 识别文本「{old_ocr_text}」）"

        final_name = img_name
        QMessageBox.information(self, "成功", f"图片已替换：{final_name}{rename_note}{ocr_note}")
        self.load_images(mode_path)

    def edit_image_config(self):
        """
        编辑图像配置
        """
        current_item = self.img_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "警告", "请选择一个图像")
            return
        
        # 获取模式路径
        mode_tree_item = self.mode_tree.currentItem()
        mode_path = []
        item = mode_tree_item
        while item:
            mode_path.insert(0, item.text(0))
            item = item.parent()
        
        # 构建模式目录
        if len(mode_path) == 1:
            # 顶级模式
            mode_name = mode_path[0]
            # 从临时模式配置中获取模式对应的目录名
            mode_data = self.temp_mode_config.get(mode_name)
            if isinstance(mode_data, dict):
                dir_name = mode_data.get('default', '')
            else:
                dir_name = mode_data
        else:
            # 子模式
            mode_name = mode_path[0]
            submode_name = mode_path[1]
            mode_data = self.temp_mode_config.get(mode_name)
            if isinstance(mode_data, dict):
                dir_name = mode_data.get(submode_name, '')
            else:
                dir_name = mode_data
        
        # 构建配置文件路径
        if dir_name:
            config_path = os.path.join(self.source_dir, dir_name, 'config.json')
            image_name = self._item_file(current_item)

            # 获取图像配置：pipeline 是运行时真相，优先 pipeline.tasks 格式
            config_data = self._load_image_config(os.path.dirname(config_path),
                                                  os.path.splitext(image_name)[0])
            
            # 获取当前模式的所有图像名称
            image_list = []
            for i in range(self.img_list.count()):
                item = self.img_list.item(i)
                image_list.append(self._item_file(item))
            
            # 打开图像配置对话框
            dialog = ImageConfigDialog(self, image_name, config_data, config_path, image_list)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                # 重新加载图像
                self.load_images(mode_path)

    @staticmethod
    def _load_image_config(mode_dir, image_key):
        """读取一张图的配置（pipeline 优先，回退 image_paths），读不到返回 {}。

        编辑器里「编辑配置」「复制配置」用的是同一份数据，避免两边各写一套
        读取逻辑后显示不一致。
        """
        config_path = os.path.join(mode_dir, 'config.json')
        if not os.path.exists(config_path):
            return {}
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return {}

        pipeline_task = None
        pipeline = config.get("pipeline")
        if isinstance(pipeline, dict):
            tasks = pipeline.get("tasks")
            if isinstance(tasks, list):
                for task in tasks:
                    if isinstance(task, dict) and task.get("name") == image_key:
                        pipeline_task = task
                        break
        if pipeline_task is not None:
            data = _pipeline_task_to_image_paths(pipeline_task)
            # pipeline 无等价字段的 legacy 覆盖：仅 message / ocr_action / ocr_region
            image_paths = config.get("image_paths")
            legacy_entry = image_paths.get(image_key, {}) if isinstance(image_paths, dict) else {}
            if isinstance(legacy_entry, dict):
                for k in ("message", "ocr_action", "ocr_region"):
                    if k in legacy_entry and legacy_entry[k]:
                        data[k] = legacy_entry[k]
            return data

        image_paths = config.get("image_paths")
        entry = image_paths.get(image_key, {}) if isinstance(image_paths, dict) else {}
        return entry if isinstance(entry, dict) else {}

    def _show_img_context_menu(self, pos):
        """图像列表右键菜单：复制/粘贴/编辑/删除"""
        # 右键点在哪张图上就先选中它，否则菜单操作的还是上一次选中的图
        item = self.img_list.itemAt(pos)
        if item is not None:
            self.img_list.setCurrentItem(item)

        menu = QMenu(self)
        copy_action = menu.addAction("复制图片配置\tCtrl+C")
        paste_action = menu.addAction("粘贴图片配置\tCtrl+V")
        menu.addSeparator()
        edit_action = menu.addAction("编辑图像配置")
        delete_action = menu.addAction("删除图像")

        has_selection = self.img_list.currentItem() is not None
        copy_action.setEnabled(has_selection)
        edit_action.setEnabled(has_selection)
        delete_action.setEnabled(has_selection)
        paste_action.setEnabled(bool(_IMAGE_CLIPBOARD.get("image_name")))

        chosen = menu.exec(self.img_list.mapToGlobal(pos))
        if chosen == copy_action:
            self.copy_selected_image()
        elif chosen == paste_action:
            self.paste_image()
        elif chosen == edit_action:
            self.edit_image_config()
        elif chosen == delete_action:
            self.delete_image()

    def copy_selected_image(self):
        """复制选中的图片和它的配置（Ctrl+C），供粘贴到其他模式"""
        current_item = self.img_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "警告", "请先选择一张图像")
            return

        mode_path = self._get_current_mode_path()
        if not mode_path:
            QMessageBox.warning(self, "警告", "请先选择一个模式")
            return
        dir_name = self._resolve_dir_name(mode_path)
        if not dir_name:
            QMessageBox.warning(self, "警告", "模式对应的目录名未设置")
            return

        mode_dir = os.path.join(self.source_dir, dir_name)
        image_name = self._item_file(current_item)
        img_path = os.path.join(mode_dir, image_name)
        try:
            with open(img_path, "rb") as f:
                image_bytes = f.read()
        except Exception as e:
            QMessageBox.warning(self, "警告", f"读取图片失败: {e}")
            return

        image_key = os.path.splitext(image_name)[0]
        config_data = self._load_image_config(mode_dir, image_key)

        # 停止条件图一并带走：粘贴到别的模式时这些文件也要落地
        stop_files = {}
        for _rel in (config_data.get("stop_images") or []):
            try:
                _sp = os.path.join(mode_dir, _rel)
                if os.path.exists(_sp):
                    with open(_sp, "rb") as f:
                        stop_files[_rel] = f.read()
            except Exception as e:
                logger.warning(f"读取停止图失败（忽略）: {_rel}: {e}")

        _IMAGE_CLIPBOARD.update({
            "image_name": image_name,
            "image_bytes": image_bytes,
            "config": config_data,
            "source_mode_dir": mode_dir,
            "stop_files": stop_files,
        })

        mode_label = " -> ".join(mode_path)
        self.clipboard_label.setText(
            f"已复制「{image_name}」（含配置）→ 切换到目标模式后按 Ctrl+V 粘贴")
        logger.info(f"复制图片配置：{mode_label}/{image_name}（{len(image_bytes)} 字节）")

    def paste_image(self):
        """把复制的图片和配置粘贴到当前选中的模式（Ctrl+V）

        重名时自动改名（foo.png → foo_copy.png → foo_copy2.png …），
        图片文件、image_paths 和（若目标有）pipeline.tasks 三处一起写。
        """
        if not _IMAGE_CLIPBOARD.get("image_name"):
            QMessageBox.information(
                self, "提示", "剪贴板里没有图片。\n请先选中一张图片按 Ctrl+C 复制。")
            return

        mode_path = self._get_current_mode_path()
        if not mode_path:
            QMessageBox.warning(self, "警告", "请先选择要粘贴到的模式")
            return
        dir_name = self._resolve_dir_name(mode_path)
        if not dir_name:
            QMessageBox.warning(self, "警告", "模式对应的目录名未设置")
            return

        mode_dir = os.path.join(self.source_dir, dir_name)
        try:
            os.makedirs(mode_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "警告", f"创建模式目录失败: {e}")
            return

        config_path = os.path.join(mode_dir, 'config.json')
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                if not isinstance(config, dict):
                    raise ValueError("配置文件根节点不是对象")
            except Exception as e:
                QMessageBox.warning(self, "警告", f"读取目标模式配置失败，已取消粘贴: {e}")
                return
        else:
            config = {"image_paths": {}, "pipeline": {"tasks": []}}

        existing_keys = set()
        image_paths = config.get("image_paths")
        if isinstance(image_paths, dict):
            existing_keys.update(image_paths.keys())
        pipeline = config.get("pipeline")
        tasks = pipeline.get("tasks") if isinstance(pipeline, dict) else None
        if isinstance(tasks, list):
            for t in tasks:
                if isinstance(t, dict) and t.get("name"):
                    existing_keys.add(t["name"])

        source_name = _IMAGE_CLIPBOARD["image_name"]
        new_filename = _unique_paste_filename(mode_dir, source_name, existing_keys)
        new_key = os.path.splitext(new_filename)[0]
        old_key = os.path.splitext(source_name)[0]

        cfg = _prepare_pasted_config(_IMAGE_CLIPBOARD.get("config"), old_key,
                                     new_key, new_filename)
        # 先在内存里改好配置（格式异常时直接中止，磁盘上不留任何痕迹）
        try:
            _insert_pasted_image(config, new_key, cfg)
        except ValueError as e:
            QMessageBox.warning(self, "警告", f"目标模式配置格式异常，已取消粘贴：{e}")
            return

        dest_path = os.path.join(mode_dir, new_filename)
        try:
            with open(dest_path, "wb") as f:
                f.write(_IMAGE_CLIPBOARD.get("image_bytes") or b"")
        except Exception as e:
            QMessageBox.warning(self, "警告", f"写入图片失败: {e}")
            return

        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            # 配置写失败则把刚写进去的图删掉，避免留下「有图无配置」的孤儿文件
            try:
                os.remove(dest_path)
            except Exception:
                pass
            QMessageBox.warning(self, "警告", f"保存配置失败: {e}")
            return

        # 停止条件图落地到目标模式的 stop/ 目录（配置里的相对路径保持不变）
        for _rel, _bytes in (_IMAGE_CLIPBOARD.get("stop_files") or {}).items():
            try:
                _sp = os.path.join(mode_dir, _rel)
                os.makedirs(os.path.dirname(_sp), exist_ok=True)
                with open(_sp, "wb") as f:
                    f.write(_bytes)
            except Exception as e:
                logger.warning(f"写入停止图失败（忽略）: {_rel}: {e}")

        mode_label = " -> ".join(mode_path)
        self.clipboard_label.setText(
            f"已粘贴「{new_filename}」到 {mode_label}（图片 + 配置）")
        logger.info(f"粘贴图片配置：{source_name} → {mode_label}/{new_filename}")

        # 刷新列表并选中新图，方便立刻继续编辑
        self.load_images(mode_path)
        for i in range(self.img_list.count()):
            item = self.img_list.item(i)
            if self._item_file(item) == new_filename:
                self.img_list.setCurrentItem(item)
                break
    
    def show_usage_instructions(self):
        """
        显示使用说明
        """
        # 创建自定义对话框
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("使用说明")
        dialog.resize(600, 400)  # 减小弹窗大小
        
        # 创建布局
        main_layout = QVBoxLayout(dialog)
        
        # 创建富文本编辑器
        text_edit = QtWidgets.QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.WidgetWidth)
        
        # 富文本内容
        rich_text = """
<h2 style="color: #333; margin-bottom: 10px;">使用说明</h2>

<h3 style="color: #555; margin-top: 15px; margin-bottom: 10px;">一、模式管理</h3>
<ol style="margin-left: 20px;">
<li><strong>添加模式</strong>：点击"添加模式"按钮，输入模式名称和目录名称（建议使用英文）</li>
<li><strong>添加子模式</strong>：选择一个顶级模式后，点击"添加子模式"按钮</li>
<li><strong>删除模式</strong>：选择要删除的模式，点击"删除模式"按钮</li>
<li><strong>重命名模式</strong>：选择要重命名的模式，点击"重命名模式"按钮</li>
</ol>

<h3 style="color: #555; margin-top: 15px; margin-bottom: 10px;">二、图像管理</h3>
<ol style="margin-left: 20px;">
<li><strong>添加图像</strong>：选择一个模式后，点击"添加图像"按钮，选择本地图像文件</li>
<li><strong>删除图像</strong>：选择要删除的图像，点击"删除图像"按钮</li>
<li><strong>编辑图像配置</strong>：选择图像后，点击"编辑图像配置"按钮</li>
<li><strong>复制/粘贴图片配置</strong>：选中一张图按 <strong>Ctrl+C</strong>（或点"复制图片配置"），切到另一个模式后按 <strong>Ctrl+V</strong>（或点"粘贴图片配置"），图片文件和它的全部配置（OCR、点击区域、动作、等待时间等）会一起复制过去；重名时自动改名为 xxx_copy.png</li>
<li><strong><span style="color: #d9534f;">注意：添加的本地的图片必须在执行窗口检测之后的游戏窗口内截图，否则会导致识别失败</span></strong></li>
</ol>

<h3 style="color: #555; margin-top: 15px; margin-bottom: 10px;">三、图像配置说明</h3>
<ol style="margin-left: 20px;">
<li><strong>图像名称</strong>：图像文件的名称（可修改）</li>
<li><strong>路径</strong>：图像在配置中的路径（通常与文件名相同）</li>
<li><strong>是否是挑战开始</strong>：标记该图像是否为挑战开始的标识</li>
<li><strong>消息</strong>：识别到该图像时显示的消息</li>
<li><strong>是否全局</strong>：是否在全局范围内搜索该图像</li>
<li><strong>下一张图片</strong>：识别到该图像后，下一个要识别的图像</li>
<li><strong>点击区域设置</strong>：
<ul style="margin-left: 20px;">
<li>点击图片区域：点击识别到的图像区域</li>
<li>点击自定义区域：点击指定的坐标区域，格式为[left, top, width, height]</li>
</ul>
</li>
<li><strong>OCR配置</strong>：
<ul style="margin-left: 20px;">
<br>
<strong><span style="color: #d9534f;">关于OCR：OCR是文字识别技术，在这里主要是识别游戏窗口内出现的文字内容，用于提高识别的精准度，但是会增加识别时间，建议在图像识别无法识别到时使用OCR</span></strong>
<li>是否启用OCR：启用后会进行文字识别，默认勾选；旧配置按配置文件中的ocr_enabled字段显示</li>
<li>OCR识别文本：需要识别的单个文本内容；添加图片配置时会自动识别图片上的文字填入（识别不到时需手动填写）</li>
<li>OCR识别阈值：OCR识别的置信度阈值，默认值为0.8</li>
<li>OCR识别后的操作：识别到文本后执行的操作（点击文字所在区域、点击点击区域设置的区域）</li>
</ul>
</li>
</ol>

<h3 style="color: #555; margin-top: 15px; margin-bottom: 10px;">四、配置文件结构</h3>
<ul style="margin-left: 20px;">
<li><strong>mode.json</strong>：存储模式和子模式的配置</li>
<li><strong>每个模式目录下的config.json</strong>：存储该模式的图像配置</li>
</ul>

<h3 style="color: #555; margin-top: 15px; margin-bottom: 10px;">五、注意事项</h3>
<ol style="margin-left: 20px;">
<li>图像文件建议使用PNG格式，确保图像清晰</li>
<li>目录名称必须使用英文、数字或下划线</li>
<li>配置修改后需要点击"保存"按钮才能生效</li>
<li>新添加的模式需要手动添加图像并配置</li>
</ol>
        """
        
        text_edit.setHtml(rich_text)
        main_layout.addWidget(text_edit)
        
        # 添加关闭按钮
        button_layout = QHBoxLayout()
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.close)
        
        button_layout.addStretch()
        button_layout.addWidget(close_button)
        
        main_layout.addLayout(button_layout)
        
        # 显示对话框
        dialog.exec()

    def save(self):
        """
        保存配置
        """
        # 将临时模式配置数据保存到正式的模式配置数据中
        self.mode_config_data = self.temp_mode_config.copy()
        
        # 自动创建模式目录
        for mode_name, mode_data in self.mode_config_data.items():
            if isinstance(mode_data, str) and mode_data:
                # 顶级模式，直接使用字符串作为目录名
                dir_path = os.path.join(self.source_dir, mode_data)
                if not os.path.exists(dir_path):
                    try:
                        os.makedirs(dir_path)
                        # 创建默认的config.json文件
                        config_path = os.path.join(dir_path, 'config.json')
                        if not os.path.exists(config_path):
                            with open(config_path, 'w', encoding='utf-8') as f:
                                json.dump({"image_paths": {}, "pipeline": {"tasks": []}}, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        QMessageBox.warning(self, "警告", f"创建目录 {mode_data} 失败: {e}")
            elif isinstance(mode_data, dict):
                # 有子模式的情况，为每个子模式创建目录
                for submode_name, submode_dir in mode_data.items():
                    if submode_dir:
                        dir_path = os.path.join(self.source_dir, submode_dir)
                        if not os.path.exists(dir_path):
                            try:
                                os.makedirs(dir_path)
                                # 创建默认的config.json文件
                                config_path = os.path.join(dir_path, 'config.json')
                                if not os.path.exists(config_path):
                                    with open(config_path, 'w', encoding='utf-8') as f:
                                        json.dump({"image_paths": {}, "pipeline": {"tasks": []}}, f, ensure_ascii=False, indent=2)
                            except Exception as e:
                                QMessageBox.warning(self, "警告", f"创建目录 {submode_dir} 失败: {e}")
        
        if self.save_mode_config():
            QMessageBox.information(self, "成功", "配置保存成功")
            self.accept()
        else:
            QMessageBox.warning(self, "警告", "配置保存失败")
