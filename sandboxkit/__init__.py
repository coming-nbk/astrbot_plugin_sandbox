# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""沙盒核心逻辑。

本包不 import astrbot，因此可以脱离 AstrBot 单独单元测试。
main.py 只负责把 AstrBot 的事件、配置接到这里的函数上。
"""

__all__ = ["config", "paths", "limits", "backends", "session", "audit", "bootstrap"]
