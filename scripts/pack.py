#!/usr/bin/env python3
# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""打发布包。

发布包只放「跑起来要用的东西」：测试和开发脚本留在仓库里，不进 zip。
理由很简单 —— 自检是写代码的人的事，装插件的人不需要跑 600 个单测。

用法：
    python3 scripts/pack.py                 # 输出到上一级目录
    python3 scripts/pack.py -o dist/        # 指定输出目录
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "astrbot_plugin_sandbox"

# 顶层要打进去的文件（目录另算）
FILES = [
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "README.md",
    "README.en.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "LICENSE",
    ".gitignore",
    "install.py",
    "install.sh",
    "logo.png",
    "icon.png",
]

# 要打进去的目录
DIRS = ["pages", "sandboxkit"]

# 永远不进包的东西
SKIP_DIRS = {"__pycache__", "tests", "scripts", ".git", ".pytest_cache", ".mypy_cache"}
SKIP_SUFFIX = {".pyc", ".pyo", ".bak", ".orig", ".rej"}


def collect() -> list[Path]:
    picked: list[Path] = []
    for name in FILES:
        path = ROOT / name
        if not path.is_file():
            raise SystemExit(f"缺文件：{name}")
        picked.append(path)
    for name in DIRS:
        base = ROOT / name
        if not base.is_dir():
            raise SystemExit(f"缺目录：{name}")
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
                continue
            if path.suffix in SKIP_SUFFIX:
                continue
            picked.append(path)
    return picked


def version_of() -> str:
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8-sig")
    match = re.search(r"^version:\s*v?([\d.]+)", text, re.M)
    if not match:
        raise SystemExit("metadata.yaml 里找不到 version")
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="打 astrbot_plugin_sandbox 发布包")
    parser.add_argument("-o", "--out", default=str(ROOT.parent), help="输出目录")
    args = parser.parse_args()

    version = version_of()
    target = Path(args.out) / f"{PLUGIN_NAME}-v{version}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    files = collect()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            zf.write(path, (Path(PLUGIN_NAME) / path.relative_to(ROOT)).as_posix())

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"{target}")
    print(f"  version : {version}")
    print(f"  files   : {len(files)}")
    print(f"  size    : {target.stat().st_size / 1024:.1f} KB")
    print(f"  sha256  : {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
