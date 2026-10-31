# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""astrbot.core.utils.astrbot_path 的桩：数据目录用环境变量指定。"""

from __future__ import annotations

import os
from pathlib import Path


def get_astrbot_data_path() -> str:
    return os.environ.get("SANDBOX_TEST_DATA", str(Path.cwd() / "data"))
