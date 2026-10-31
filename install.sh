#!/usr/bin/env bash
# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later
# 一键安装包装：真正的逻辑在 install.py，这里只负责找到 python3。
# 用法：./install.sh /opt/AstrBot         或   ASTRBOT_ROOT=/opt/AstrBot ./install.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${PYTHON:-python3}"

if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "找不到 $python_bin，请先装 Python 3.10+ 或用 PYTHON=/path/to/python 指定" >&2
  exit 1
fi

args=()
if [ "${1:-}" != "" ] && [ "${1:0:2}" != "--" ]; then
  args+=(--astrbot "$1")
  shift
fi

exec "$python_bin" "$here/install.py" "${args[@]}" "$@"
