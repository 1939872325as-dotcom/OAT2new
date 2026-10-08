# -*- coding: utf-8 -*-
"""打包热更新版 zip（不做 Nuitka 编译）。

用途：把「已部署的热更新目录」打成可直接分发 / 解压即用的 zip。
第三方依赖（PyQt6 / cv2 / rapidocr ...）和 `OAT/` 业务源码都在包里，
解压后运行 `OAT2.exe` 即可；改 `OAT/**.py` 重启即生效，无需重新编译。

用法：
    python build_zip.py                       # 默认源=..\\OAT2new，输出到源目录旁边
    python build_zip.py --source D:\\somewhere\\OAT2new
    python build_zip.py --output D:\\out\\OAT2new.zip
    python build_zip.py --dry-run             # 只列清单，不写 zip

排除项：运行日志、Python 字节码缓存、临时目录、更新器构建产物、
        多开启动器的本机 ini（含本机游戏路径）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOURCE = os.path.join(os.path.dirname(ROOT), "OAT2new")

# 目录名（任意层级命中即整个跳过）
EXCLUDE_DIRS = {
    "__pycache__",
    ".git",
    ".idea",
    ".vscode",
    "temp",
    "OAT_Updater_Build",
    "main.dist",
    "main.build",
    "logs",  # 运行日志，含本机使用痕迹
}

# 文件名后缀（大小写不敏感）
EXCLUDE_EXTS = {
    ".pyc",
    ".pyo",
    ".log",
    ".bak",
    ".tmp",
}

# 相对路径精确排除（本机特定状态，不应分发给别人）
EXCLUDE_RELPATHS = {
    os.path.join("yyx", "yyx-launcher.ini"),  # 存的是本机游戏 exe 路径
    os.path.join("config", "update_cache.json"),  # 更新检查缓存
}


def _norm(rel: str) -> str:
    return rel.replace("/", os.sep).replace("\\", os.sep)


def should_skip(rel_dir: str, name: str) -> bool:
    """该条目是否应排除。rel_dir 为相对源根目录的目录（根为 ""）。"""
    rel = os.path.join(rel_dir, name) if rel_dir else name
    if _norm(rel) in {_norm(p) for p in EXCLUDE_RELPATHS}:
        return True
    if os.path.splitext(name)[1].lower() in EXCLUDE_EXTS:
        return True
    return False


def collect(source: str) -> list[tuple[str, str]]:
    """遍历源目录，返回 [(绝对路径, zip 内相对路径)]。"""
    items: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(source):
        rel_dir = os.path.relpath(dirpath, source)
        if rel_dir == ".":
            rel_dir = ""

        # 原地过滤，os.walk 就不会进入被排除的目录
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]

        for name in sorted(filenames):
            if should_skip(rel_dir, name):
                continue
            abs_path = os.path.join(dirpath, name)
            rel_path = os.path.join(rel_dir, name) if rel_dir else name
            items.append((abs_path, rel_path))
    return items


def build(source: str, output: str, compress: bool = True, dry_run: bool = False) -> int:
    if not os.path.isdir(source):
        raise SystemExit(f"[zip] 源目录不存在: {source}")
    if not os.path.isfile(os.path.join(source, "OAT2.exe")):
        raise SystemExit(f"[zip] 源目录里没有 OAT2.exe，不像是热更新部署目录: {source}")

    items = collect(source)
    total = sum(os.path.getsize(p) for p, _ in items)
    print(f"[zip] 源目录 : {source}")
    print(f"[zip] 待打包 : {len(items)} 个文件，{total / 1024 / 1024:.1f} MB（未压缩）")
    print(f"[zip] 输出   : {output}")

    if dry_run:
        print("[zip] --dry-run：仅列清单（最多 40 条）")
        for _, rel in items[:40]:
            print("   ", rel)
        if len(items) > 40:
            print(f"    ... 其余 {len(items) - 40} 条略")
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(output)) or ".", exist_ok=True)
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    t0 = time.time()

    with zipfile.ZipFile(output, "w", compression=method, compresslevel=6) as zf:
        for abs_path, rel_path in items:
            # 中文文件名依赖 zipfile 自动写入 UTF-8 标志位（flag bit 11），
            # 否则 Windows 解压会得到乱码文件名。
            zf.write(abs_path, arcname=rel_path)

    out_size = os.path.getsize(output)
    ratio = (1 - out_size / total) * 100 if total else 0
    print(f"[zip] 完成   : {out_size / 1024 / 1024:.1f} MB"
          f"（压缩掉 {ratio:.1f}%，耗时 {time.time() - t0:.1f}s）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包热更新版 zip（不编译）")
    ap.add_argument("--source", default=DEFAULT_SOURCE, help=f"热更新部署目录（默认 {DEFAULT_SOURCE}）")
    ap.add_argument("--output", default="", help="输出 zip 路径；默认放在源目录旁边")
    ap.add_argument("--store", action="store_true", help="不压缩（更快，体积更大）")
    ap.add_argument("--dry-run", action="store_true", help="只列清单，不写 zip")
    args = ap.parse_args(argv)

    source = os.path.abspath(args.source)
    output = args.output or os.path.join(
        os.path.dirname(source),
        f"OAT2new-hotfix-{time.strftime('%Y%m%d')}.zip",
    )
    return build(source, output, compress=not args.store, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
