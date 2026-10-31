# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""把聊天消息拆成子命令和参数。

单独成模块，是因为它不依赖 astrbot，可以单独测；
main.py 只做一层薄薄的转接。
"""

from __future__ import annotations

COMMAND_ALIASES = ("sandbox", "sbx", "沙盒")


def split_command(message: str, aliases: tuple[str, ...] = COMMAND_ALIASES) -> tuple[str, str]:
    """`/sbx run ls -l` -> ("run", "ls -l")。

    - 去掉开头的 /
    - 去掉 /sbx@bot 这种带 @ 的命令名
    - 参数部分原样保留（含空格和换行）
    """
    text = (message or "").strip()
    if text.startswith("/"):
        text = text[1:]
    head, _, rest = text.partition(" ")
    head = head.split("@", 1)[0].strip()
    rest = rest.strip()
    if head not in aliases:
        # 消息不是以命令开头（例如被别的插件拼接），仍然按第一个词当子命令
        return head, rest
    sub, _, arg = rest.partition(" ")
    return sub.strip(), arg.strip()
