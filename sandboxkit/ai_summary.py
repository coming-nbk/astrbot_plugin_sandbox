# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""用 OpenAI 兼容接口把漏洞原文翻成中文并总结。

约束：
- 只读：把文本发出去，拿回一段文字，不执行模型说的任何东西
- 可选：默认关闭，必须管理员在配置里打开并填 key
- 有缓存：同一条只花一次钱
- 失败不影响主流程：拿不到摘要就只显示原文
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

PROMPT = (
    "你是服务器安全助手。把下面这段漏洞情报翻译成中文，然后给出三点："
    "一、影响什么；二、有多严重；三、该怎么处理。"
    "要求：不超过 150 字，不要编造原文里没有的信息，不要输出 Markdown 标题。"
)


class SummaryError(Exception):
    pass


def _cache_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


class SummaryCache:
    def __init__(self, path: Path | str, *, ttl: int = 7 * 86400):
        self.path = Path(path)
        self.ttl = ttl

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, text: str) -> str:
        entry = self._read().get(_cache_key(text))
        if not isinstance(entry, dict):
            return ""
        try:
            fresh = time.time() - float(entry.get("at", 0)) <= self.ttl
        except (TypeError, ValueError):
            return ""
        return str(entry.get("summary") or "") if fresh else ""

    def put(self, text: str, summary: str) -> None:
        data = self._read()
        data[_cache_key(text)] = {"at": int(time.time()), "summary": summary[:2000]}
        if len(data) > 300:
            keep = sorted(data.items(), key=lambda kv: kv[1].get("at", 0))[-300:]
            data = dict(keep)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass


def summarize(
    text: str,
    *,
    base_url: str,
    api_key: str,
    model: str = "gpt-4o-mini",
    timeout: float = 30.0,
) -> str:
    """调一次 chat/completions，返回中文摘要。失败抛 SummaryError。"""
    text = (text or "").strip()
    if not text:
        raise SummaryError("没有可总结的内容")
    if not base_url:
        raise SummaryError("没有配置 AI 接口地址")

    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    payload = {
        "model": model or "gpt-4o-mini",
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": text[:4000]},
        ],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}" if api_key else "",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise SummaryError(f"接口返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SummaryError(f"连不上接口：{exc}") from exc
    except ValueError as exc:
        raise SummaryError(f"接口返回的不是 JSON：{exc}") from exc

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SummaryError("接口返回里没有 choices") from exc
    content = str(content or "").strip()
    if not content:
        raise SummaryError("接口返回了空内容")
    return content[:1000]
