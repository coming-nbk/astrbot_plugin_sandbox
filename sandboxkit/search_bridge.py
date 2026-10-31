# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""桥接 astrbot_plugin_search_engines：借它的抓链能力查 CVE。

那个插件导出了 `crawl(keyword, engine, pages, gateway=...) -> [{title,url,snippet}]`，
带代理轮换，每次请求换出口 IP。这里按文件路径把它加载进来直接调，
不复制它的代码，也不改它。

找不到插件、或它依赖缺失时，返回明确的错误字符串，不影响体检其它部分。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

PLUGIN_NAME = "astrbot_plugin_search_engines"
CRAWLER_FILE = "crawler.py"


def find_crawler(explicit: str = "", *, near: Path | None = None) -> Path | None:
    """按优先级找那个插件的 crawler.py。"""
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path
        if (path / CRAWLER_FILE).is_file():
            return path / CRAWLER_FILE
        return None

    candidates: list[Path] = []
    if near is not None:
        candidates.append(Path(near).parent / PLUGIN_NAME / CRAWLER_FILE)
        candidates.append(Path(near) / PLUGIN_NAME / CRAWLER_FILE)
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_gateway(config_path: Path | str | None) -> dict | None:
    """读搜索插件的配置，拼出它那套「网关代理」参数。

    它支持 nexip 这类带认证、按会话轮换出口 IP 的网关：把 gateway/username/
    password 拼成一个 URL，rotate 打开时每次请求换一个 session。
    这里只读配置，不改对方的文件。
    """
    if not config_path:
        return None
    path = Path(config_path)
    if not path.is_file():
        return None
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None

    gateway = str(cfg.get("proxy_gateway") or "").strip()
    if not gateway:
        return None
    return {
        "gateway": gateway,
        "username": str(cfg.get("proxy_username") or ""),
        "password": str(cfg.get("proxy_password") or ""),
        "country": str(cfg.get("proxy_country") or ""),
        "scheme": str(cfg.get("proxy_scheme") or "http"),
        "rotate": bool(cfg.get("proxy_rotate_per_request", True)),
    }


def find_config(data_dir: Path | str | None, *, plugin_name: str = PLUGIN_NAME) -> Path | None:
    """AstrBot 把插件配置放在 <data>/config/<插件名>_config.json。"""
    if not data_dir:
        return None
    path = Path(data_dir) / "config" / f"{plugin_name}_config.json"
    return path if path.is_file() else None


def load_crawler(path: Path):
    """按文件路径加载模块。模块名带上路径指纹，换目录不会拿到旧模块。"""
    digest = hashlib.md5(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:10]
    module_name = f"sandbox_search_crawler_{digest}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    # 那个插件的 crawler.py 里是 `from fingerprint import ...`（绝对导入），
    # 按文件路径加载时插件目录不在 sys.path 上，会报 No module named 'fingerprint'。
    # 加载期间临时把它加进去，加载完再撤掉，不污染全局。
    plugin_dir = str(Path(path).resolve().parent)
    added = plugin_dir not in sys.path
    if added:
        sys.path.insert(0, plugin_dir)
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        if added:
            try:
                sys.path.remove(plugin_dir)
            except ValueError:
                pass
    return module


async def search(
    keyword: str,
    *,
    crawler_path: Path,
    engine: str = "bing",
    pages: int = 1,
    gateway: dict | None = None,
    timeout: int = 15,
    limit: int = 8,
) -> tuple[list[dict], str]:
    """跑一次搜索，返回 (结果, 错误信息)。错误信息为空表示成功。"""
    try:
        module = load_crawler(crawler_path)
    except Exception as exc:  # noqa: BLE001 - 对方插件可能缺依赖
        return [], f"加载搜索插件失败：{exc}"

    crawl = getattr(module, "crawl", None)
    if crawl is None:
        return [], f"{crawler_path.name} 里没有 crawl()"

    try:
        results: Any = await crawl(
            keyword,
            engine=engine,
            pages=max(1, min(int(pages), 3)),
            gateway=gateway or None,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 网络/代理问题都归到这里
        return [], f"搜索失败：{exc}"

    if not isinstance(results, list):
        return [], "搜索插件返回了意外的格式"

    cleaned: list[dict] = []
    for item in results[: max(1, limit)]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        cleaned.append(
            {
                "title": str(item.get("title") or url)[:200],
                "url": url[:500],
                "snippet": str(item.get("snippet") or "")[:300],
            }
        )
    if not cleaned:
        return [], "没有抓到有效结果（可能是代理不可用或被限流）"
    return cleaned, ""


def render(results: list[dict], keyword: str) -> str:
    lines = [f"搜索：{keyword}", f"命中 {len(results)} 条：", ""]
    for index, item in enumerate(results, 1):
        lines.append(f"{index}. {item['title']}")
        lines.append(f"   {item['url']}")
        if item.get("snippet"):
            lines.append(f"   {item['snippet']}")
        lines.append("")
    return "\n".join(lines).rstrip()
