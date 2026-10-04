# -*- coding: utf-8 -*-
"""缓存占用统计。

只做一件事：算出「清理缓存」按钮实际会清掉的那些东西有多大。
路径必须与 main_window.clean_cache 完全一致，否则显示的体积和实际清掉的
对不上（这种不一致比不显示更误导）。

清理目标：
  1. <root>/temp               残留安装包
  2. <root>/logs/screen_shot   窗口预览缓存
  3. <root>/logs/<日志文件>     日志文件（清理时是清空，不是删除）
"""

import os


def _project_root() -> str:
    """与 main_window.clean_cache 保持同一套推导（上溯三层到项目根）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _log_file_name() -> str:
    """日志文件名。惰性导入，避免与 utils 内部模块产生导入环。"""
    try:
        from OAT.utils.error_handler import LOG_FILE
        return os.path.basename(LOG_FILE)
    except Exception:
        return "log.txt"


def get_cache_targets():
    """返回 [(名称, 路径, 类型)]，类型为 'tree'（目录）或 'file'（单文件）。"""
    root = _project_root()
    logs_dir = os.path.join(root, "logs")
    return [
        ("残留安装包", os.path.join(root, "temp"), "tree"),
        ("窗口预览缓存", os.path.join(logs_dir, "screen_shot"), "tree"),
        ("日志文件", os.path.join(logs_dir, _log_file_name()), "file"),
    ]


def _dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                # 单个文件读不到（被占用/权限）就跳过，不影响整体统计
                continue
    return total


def format_size(num_bytes: float) -> str:
    """人类可读体积，保留 1 位小数。"""
    try:
        n = float(num_bytes)
    except (ValueError, TypeError):
        return "0 B"
    if n < 0:
        n = 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def compute_cache_size():
    """返回 (总字节数, 分项文本)。

    分项文本只列出有内容的部分，例如 "窗口预览缓存 12.3 MB、日志文件 1.1 MB"；
    全为空时返回 "无缓存"。
    """
    total = 0
    parts = []
    for name, path, kind in get_cache_targets():
        if not path or not os.path.exists(path):
            continue
        try:
            size = _dir_size(path) if kind == "tree" else os.path.getsize(path)
        except OSError:
            size = 0
        total += size
        if size > 0:
            parts.append(f"{name} {format_size(size)}")
    detail = "、".join(parts) if parts else "无缓存"
    return total, detail
