# Copyright (C) 2026 coming-nbk
# SPDX-License-Identifier: AGPL-3.0-or-later

"""安全体检：把「现在这套配置有多容易被突破」变成一份能读的报告。

两块：
1. 本地硬检查 —— 不联网，永远能跑。看的是配置和文件系统状态。
2. 外部漏洞源 —— 可选，管理员填一个 JSON 地址，按内核版本和实际后端匹配。

注意：这里只做「诊断 + 建议」。真正的自动修复只有插件自己能改的东西
（清 setuid、收紧配置），绝不去动宿主内核。
"""

from __future__ import annotations

import json
import os
import platform
import re
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


@dataclass(frozen=True)
class Finding:
    id: str
    title: str
    severity: str
    detail: str
    fix: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "detail": self.detail,
            "fix": self.fix,
        }


def kernel_release() -> str:
    try:
        return Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").strip()
    except OSError:
        return platform.release() or "未知"


def count_setuid(rootfs: Path | None, *, limit: int = 20000) -> tuple[int, list[str]]:
    """数一下 rootfs 里还有多少 setuid/setgid 文件。"""
    if not rootfs or not Path(rootfs).is_dir():
        return 0, []
    found: list[str] = []
    seen = 0
    for current, dirs, files in os.walk(str(rootfs)):
        for name in dirs + files:
            seen += 1
            if seen > limit:
                return len(found), found[:5]
            path = os.path.join(current, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
                found.append(os.path.relpath(path, str(rootfs)))
    return len(found), found[:5]


def local_findings(settings, *, backend: str, rootfs: Path | None) -> list[Finding]:
    """不联网的那部分检查。"""
    out: list[Finding] = []

    if backend == "local":
        out.append(
            Finding(
                id="backend-local",
                title="当前没有文件系统隔离",
                severity="high",
                detail="后端是 local，沙盒里的命令能直接读写宿主文件系统。",
                fix="装 bubblewrap，或配置 rootfs 用 chroot/bwrap；"
                "实在不行就把 allow_local_backend 关掉。",
            )
        )

    uid = int(getattr(settings, "chroot_uid", 65534))
    if backend == "chroot" and uid == 0:
        out.append(
            Finding(
                id="chroot-root",
                title="沙盒内以 root 运行",
                severity="high",
                detail="chroot_uid=0 时沙盒进程就是 root，一次 chroot 逃逸等于接管宿主。",
                fix="把 chroot_uid / chroot_gid 改回 65534。",
            )
        )

    total, sample = count_setuid(rootfs)
    if total:
        out.append(
            Finding(
                id="setuid-binaries",
                title=f"rootfs 里有 {total} 个 setuid/setgid 文件",
                severity="high",
                detail="例如：" + "、".join(sample) + "。降权后的进程执行它们就能拿回 euid 0。",
                fix="跑一次 /sbx harden 清掉这些位；装包后也要再清一次。",
            )
        )

    roots = tuple(getattr(settings, "import_roots", ()) or ())
    if any(str(root).rstrip("/") in ("", "/", "/*") for root in roots):
        out.append(
            Finding(
                id="import-root-all",
                title="import_roots 配成了整个根目录",
                severity="high",
                detail="这等于允许把宿主上任意文件拷进沙盒，包括密钥和数据库。",
                fix="改成具体目录，例如 /opt/astrbot/data。",
            )
        )

    if getattr(settings, "web_enabled", True) and getattr(settings, "web_auth", "") == "none":
        out.append(
            Finding(
                id="web-no-auth",
                title="WebUI 没有鉴权",
                severity="medium",
                detail="能访问到这个端口的人都能执行命令、读所有沙盒。",
                fix="把 web_auth 改成 dashboard 或 token。",
            )
        )

    if backend == "chroot" and getattr(settings, "allow_network", False):
        out.append(
            Finding(
                id="chroot-network",
                title="chroot 沙盒开了外网",
                severity="medium",
                detail="chroot 不隔离网络，沙盒里的进程能连内网服务和云元数据地址。",
                fix="关掉 allow_network，或改用 bwrap 并加 --unshare-net。",
            )
        )

    quota = int(getattr(settings, "quota_bytes", 0))
    if quota <= 0:
        out.append(
            Finding(
                id="no-quota",
                title="没有空间配额",
                severity="medium",
                detail="单个沙盒可以把磁盘写满，连带 AstrBot 一起挂。",
                fix="给 quota_bytes 设一个上限，比如 256MB。",
            )
        )

    if int(getattr(settings, "max_concurrent_runs", 0)) > 8:
        out.append(
            Finding(
                id="many-concurrent",
                title="并发上限偏高",
                severity="low",
                detail="并发太高时 fork 炸弹更容易把机器拖死。",
                fix="把 max_concurrent_runs 收到 4 以内。",
            )
        )

    return sorted(out, key=lambda item: SEVERITY_ORDER.get(item.severity, 9))


def parse_nvd(payload: dict) -> list[dict]:
    """NVD 2.0 的响应 → 我们的条目格式。

    形如：{"vulnerabilities": [{"cve": {"id", "descriptions", "metrics", ...}}]}
    """
    out: list[dict] = []
    for wrapper in payload.get("vulnerabilities") or []:
        cve = wrapper.get("cve") or {}
        cve_id = str(cve.get("id") or "")
        if not cve_id:
            continue

        desc = ""
        for item in cve.get("descriptions") or []:
            if str(item.get("lang")) == "en":
                desc = str(item.get("value") or "")
                break

        severity = "medium"
        metrics = cve.get("metrics") or {}
        for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
            for metric in metrics.get(key) or []:
                base = str((metric.get("cvssData") or {}).get("baseSeverity") or "").lower()
                if base in ("critical", "high"):
                    severity = "high"
                elif base == "medium":
                    severity = "medium"
                elif base == "low":
                    severity = "low"
                break
            if severity != "medium":
                break

        out.append(
            {
                "id": cve_id,
                "title": (desc[:80] + "…") if len(desc) > 80 else (desc or cve_id),
                "severity": severity,
                "affects": "",  # NVD 的版本约束是 CPE，太细，这里交给关键词过滤
                "detail": desc[:300],
                "fix": "升级到厂商修复版本；具体版本看 NVD 条目里的 configurations。",
            }
        )
    return out


def parse_kev(payload: dict, *, limit: int = 20) -> list[dict]:
    """CISA 已知被利用漏洞目录 → 条目。

    目录里有上千条、覆盖所有厂商，直接全丢进报告会把真正相关的淹掉。
    所以先挑 Linux / 内核相关的；一条都没有时，退回最近披露的几条。
    """
    rows: list[dict] = []
    for item in payload.get("vulnerabilities") or []:
        cve_id = str(item.get("cveID") or "")
        if not cve_id:
            continue
        product = str(item.get("product") or "")
        name = str(item.get("vulnerabilityName") or cve_id)
        desc = str(item.get("shortDescription") or "")
        rows.append(
            {
                "id": cve_id,
                "title": name[:80],
                "severity": "high",
                "affects": "",
                "detail": (desc + f"（产品：{product}）")[:300],
                "fix": str(item.get("requiredAction") or "按厂商公告升级。")[:200],
                "added": str(item.get("dateAdded") or ""),
                "haystack": f"{product} {name} {desc}".lower(),
            }
        )

    related = [row for row in rows if "linux" in row["haystack"] or "kernel" in row["haystack"]]
    picked = related or sorted(rows, key=lambda row: row["added"], reverse=True)
    for row in picked:
        row.pop("haystack", None)
        row.pop("added", None)
    return picked[:limit]


PARSERS = {
    "custom": None,
    "nvd": parse_nvd,
    "kev": parse_kev,
}


def fetch_feed(
    url: str,
    *,
    fmt: str = "custom",
    cache_path: Path | None = None,
    ttl: int = 86400,
    timeout: float = 8.0,
) -> list[dict]:
    """拉外部漏洞源。格式：JSON 数组，每条含 id/title/severity/affects/fix。

    拉不到就用缓存；都没有就返回空 —— 安全体检不能因为网络挂了就整体失败。
    """
    if not url:
        return []

    if cache_path and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            if time.time() - float(cached.get("at", 0)) < ttl:
                return list(cached.get("items") or [])
        except (OSError, ValueError, TypeError):
            pass

    try:
        request = urllib.request.Request(url, headers={"User-Agent": "astrbot-sandbox-audit"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return []

    parser = PARSERS.get((fmt or "custom").lower())
    if parser is not None:
        if not isinstance(payload, dict):
            return []
        return parser(payload)[:50]

    items = payload if isinstance(payload, list) else payload.get("items", [])
    clean: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        clean.append(
            {
                "id": str(item.get("id")),
                "title": str(item.get("title") or item.get("id")),
                "severity": str(item.get("severity") or "medium").lower(),
                "affects": str(item.get("affects") or ""),
                "detail": str(item.get("detail") or ""),
                "fix": str(item.get("fix") or ""),
            }
        )

    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"at": time.time(), "items": clean}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
    return clean


def match_feed(items: list[dict], *, kernel: str, backend: str) -> list[Finding]:
    """把外部条目按内核版本和后端匹配到本机。"""
    haystack = f"{kernel} {backend}"
    out: list[Finding] = []
    for item in items:
        pattern = item.get("affects") or ""
        if pattern:
            try:
                if not re.search(pattern, haystack):
                    continue
            except re.error:
                continue
        out.append(
            Finding(
                id="feed-" + item["id"],
                title=item.get("title") or item["id"],
                severity=str(item.get("severity") or "medium").lower(),
                detail=item.get("detail") or "",
                fix=item.get("fix") or "升级内核或收紧配置。",
            )
        )
    return out


BOUNDARY = (
    "沙盒能管：路径越界、越权执行、资源耗尽、setuid 提权、同 uid 互相 ptrace。\n"
    "沙盒管不了：内核漏洞（要宿主升级）、固件/硬件级持久化、侧信道、"
    "宿主本身被攻破。这几类不是配置能解决的。"
)


def render(
    findings: list[Finding], *, kernel: str, backend: str, lang: str = "zh"
) -> str:
    from .i18n import t

    host = t("audit.host", lang, kernel=kernel, backend=backend)
    boundary = t("audit.boundary_can", lang) + "\n" + t("audit.boundary_cannot", lang)

    if not findings:
        return (
            t("audit.clean", lang)
            + "\n" + host
            + "\n\n" + t("audit.clean_note", lang)
            + "\n\n" + boundary
        )

    lines = [t("audit.title", lang, n=len(findings)), host, ""]
    for item in findings:
        mark = t(f"audit.sev.{item.severity}", lang)
        lines.append(f"[{mark}] {item.title}")
        if item.detail:
            lines.append(f"    {item.detail}")
        if item.fix:
            lines.append("    " + t("audit.fix", lang, text=item.fix))
        lines.append("")
    lines.append(boundary)
    return "\n".join(lines).rstrip()
