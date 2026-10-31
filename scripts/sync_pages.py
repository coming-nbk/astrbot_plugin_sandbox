#!/usr/bin/env python3
# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""校验页面结构。

AstrBot 的插件页面约定是「一个页面一个目录，资源放在 index.html 旁边」：
`pages/sandbox/index.html` + `app.css` + `app.js`，用相对路径引用。
`pages/index.html` 只是给旧约定留的跳转桩，不是副本。

    python3 scripts/sync_pages.py          # 检查并修复跳转桩
    python3 scripts/sync_pages.py --check  # 只检查
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "pages"
STUB = PAGES / "index.html"
PAGE_DIR = PAGES / "sandbox"
INDEX = PAGE_DIR / "index.html"
ASSETS = (PAGE_DIR / "app.css", PAGE_DIR / "app.js")

STUB_BODY = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>沙盒控制台</title>
<meta http-equiv="refresh" content="0; url=./sandbox/index.html">
</head>
<body>
<p>正在跳转到 <a href="./sandbox/index.html">sandbox/index.html</a></p>
</body>
</html>
"""


def check() -> list[str]:
    problems: list[str] = []
    if not INDEX.is_file():
        problems.append("pages/sandbox/index.html 缺失")
        return problems
    for asset in ASSETS:
        if not asset.is_file():
            problems.append(f"{asset.relative_to(ROOT)} 缺失")
    html = INDEX.read_text("utf-8")
    if 'href="./app.css"' not in html:
        problems.append("页面没有用相对路径引用 ./app.css")
    if 'src="./app.js"' not in html:
        problems.append("页面没有用相对路径引用 ./app.js")
    if "<style>" in html or "<script>" in html:
        problems.append("页面里还有内联样式或脚本")
    if STUB.is_file() and "sandbox/index.html" not in STUB.read_text("utf-8"):
        problems.append("pages/index.html 不是跳转桩")
    return problems


def main(argv: list[str]) -> int:
    check_only = "--check" in argv

    problems = check()
    if problems:
        print("发现问题：")
        for item in problems:
            print(f"  - {item}")
        if not check_only:
            STUB.write_text(STUB_BODY, "utf-8")
            print("已重写 pages/index.html 跳转桩")
            problems = check()
    if problems:
        return 1
    print("页面结构正常：pages/sandbox/{index.html,app.css,app.js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
