# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""多语言：文案集中在字典里，代码只引用 key。

设计取舍：
- 只支持 zh / en 两种，够覆盖绝大多数场景；加语种只需在 _STRINGS 里加一段
- 缺 key 时回落到中文，再回落到 key 本身 —— 宁可显示 key，也不抛异常
- 参数用 str.format，占位符统一 {name}
"""

from __future__ import annotations

DEFAULT = "zh"
SUPPORTED = ("zh", "en")

_STRINGS: dict[str, dict[str, str]] = {
    "zh": {
        # ---- 通用 ----
        "common.yes": "允许",
        "common.no": "禁止",
        "common.unknown": "未知",
        "common.none": "无",
        # ---- 使用说明 ----
        "guide.title": "沙盒用法（{user}）",
        "guide.can_use": "你可以用这些：",
        "guide.limits": "你的限制（当前生效值）：",
        "guide.quota": "空间上限：{value}",
        "guide.timeout": "单条命令最长：{seconds} 秒，超时会被杀掉",
        "guide.memory": "内存上限：{mb} MB",
        "guide.rate": "频率：每分钟最多 {n} 次",
        "guide.rate_off": "频率：不限",
        "guide.network": "联网：{value}",
        "guide.import": "从宿主导入文件：{value}",
        "guide.whats_inside": "沙盒里现在有什么：",
        "guide.system": "系统：{preset}（{backend} 后端）",
        "guide.python_ok": "Python：可用，直接 /sbx py",
        "guide.python_denied": "Python：装了，但管理员没给你 /sbx py 权限",
        "guide.python_missing": "Python：没装，需要管理员装 python3",
        "guide.cwd": "工作目录：/sandbox —— 你写的文件都在这里，插件重启不会丢",
        "guide.notes": "几条要注意：",
        "guide.note_vm": "1. 这不是虚拟机。chroot 只是换了根目录，不是强隔离，别跑来路不明的程序。",
        "guide.note_net_off": "2. 沙盒里没有外网。要装东西让管理员用 /sbx install，不要自己 apt。",
        "guide.note_net_on": "2. 沙盒能联网，但装系统包仍然建议走 /sbx install。",
        "guide.note_output": "3. 输出太长会被截断，写文件再看比直接打印稳。",
        "guide.note_quota": "4. 超过空间配额会被拒绝执行，先 /sbx usage 看看用了多少。",
        "guide.note_local": "5. 当前是 local 后端，沙盒里的命令能直接读写宿主文件系统，风险自负。",
        "guide.cmd_run": "在沙盒里跑一条 shell 命令",
        "guide.cmd_py": "直接跑 Python，不用先写文件",
        "guide.cmd_ls": "看目录里有什么",
        "guide.cmd_cat": "看一个文本文件",
        "guide.cmd_usage": "看自己用了多少空间",
        "guide.cmd_import": "把宿主机上的文件拷进来",
        "guide.cmd_export": "把沙盒里的文件拿走",
        "guide.cmd_reset": "清空自己的沙盒",
        "guide.cmd_guide": "再看一遍这份说明",
        "guide.cmd_install": "以 root 装软件包（会影响所有用户）",
        "guide.cmd_bootstrap": "重新下载并解压 rootfs",
        # ---- 安全体检 ----
        "audit.title": "安全体检：{n} 项",
        "audit.clean": "安全体检：没发现明显问题",
        "audit.host": "内核 {kernel} · 后端 {backend}",
        "audit.clean_note": "配置层面的检查都过了。内核本身的 CVE 需要外部漏洞源。",
        "audit.fix": "修法：{text}",
        "audit.sev.high": "高",
        "audit.sev.medium": "中",
        "audit.sev.low": "低",
        "audit.sev.info": "提示",
        "audit.boundary_can": "沙盒能管：路径越界、越权执行、资源耗尽、setuid 提权、同 uid 互相 ptrace。",
        "audit.boundary_cannot": "沙盒管不了：内核漏洞（要宿主升级）、固件/硬件级持久化、侧信道、宿主本身被攻破。这几类不是配置能解决的。",
        # ---- 常见拒绝 ----
        "err.too_long": "命令太长（超过 {limit} 字符），请先写进文件再执行。",
        "err.nul": "命令里有非法字符（NUL），已拒绝。",
        "err.denied": "这条命令命中了禁用规则「{pattern}」，已拒绝。",
        "err.admin_only": "只有管理员可以执行这个操作。",
        "err.no_isolation": "当前没有可用的隔离后端，命令已被拒绝。",
        "err.root_gate": "沙盒配置成了以 root 运行（chroot_uid=0），但没打开 allow_root_sandbox。",
        "err.platform_blocked": "沙盒没有对「{platform}」开放。管理员可用的平台：{allowed}。",
        "err.platform_unknown": "读不出当前平台，沙盒拒绝了这次调用。",
    },
    "en": {
        "common.yes": "allowed",
        "common.no": "blocked",
        "common.unknown": "unknown",
        "common.none": "none",
        "guide.title": "Sandbox guide ({user})",
        "guide.can_use": "What you can run:",
        "guide.limits": "Your limits (currently in effect):",
        "guide.quota": "Disk quota: {value}",
        "guide.timeout": "Max command time: {seconds}s, killed on timeout",
        "guide.memory": "Memory cap: {mb} MB",
        "guide.rate": "Rate: up to {n} runs per minute",
        "guide.rate_off": "Rate: unlimited",
        "guide.network": "Network: {value}",
        "guide.import": "Import from host: {value}",
        "guide.whats_inside": "What's inside:",
        "guide.system": "System: {preset} ({backend} backend)",
        "guide.python_ok": "Python: available, use /sbx py",
        "guide.python_denied": "Python: installed, but you have no /sbx py permission",
        "guide.python_missing": "Python: not installed, ask the admin to install python3",
        "guide.cwd": "Working dir: /sandbox — files you write here survive restarts",
        "guide.notes": "Things to know:",
        "guide.note_vm": "1. This is not a VM. chroot only swaps the root dir; do not run untrusted binaries.",
        "guide.note_net_off": "2. No outbound network. Ask the admin to run /sbx install instead of apt.",
        "guide.note_net_on": "2. Network is on, but system packages should still go through /sbx install.",
        "guide.note_output": "3. Long output gets truncated; write to a file and read it back.",
        "guide.note_quota": "4. Exceeding the quota blocks execution; check /sbx usage first.",
        "guide.note_local": "5. Backend is local: commands can read and write the host filesystem.",
        "guide.cmd_run": "Run a shell command inside the sandbox",
        "guide.cmd_py": "Run Python without writing a file first",
        "guide.cmd_ls": "List a directory",
        "guide.cmd_cat": "Print a text file",
        "guide.cmd_usage": "Show how much space you use",
        "guide.cmd_import": "Copy a file from the host",
        "guide.cmd_export": "Take a file out of the sandbox",
        "guide.cmd_reset": "Wipe your own sandbox",
        "guide.cmd_guide": "Show this guide again",
        "guide.cmd_install": "Install packages as root (affects every user)",
        "guide.cmd_bootstrap": "Re-download and unpack the rootfs",
        "audit.title": "Security check: {n} findings",
        "audit.clean": "Security check: nothing obvious",
        "audit.host": "kernel {kernel} · backend {backend}",
        "audit.clean_note": "Configuration checks all passed. Kernel CVEs need an external feed.",
        "audit.fix": "Fix: {text}",
        "audit.sev.high": "HIGH",
        "audit.sev.medium": "MED",
        "audit.sev.low": "LOW",
        "audit.sev.info": "INFO",
        "audit.boundary_can": "Sandbox covers: path escapes, privilege abuse, resource exhaustion, setuid escalation, cross-process ptrace.",
        "audit.boundary_cannot": "Sandbox does not cover: kernel bugs (patch the host), firmware/hardware persistence, side channels, a compromised host.",
        "err.too_long": "Command too long (over {limit} chars); write it to a file first.",
        "err.nul": "Command contains a NUL byte; rejected.",
        "err.denied": "Command matched deny rule '{pattern}'; rejected.",
        "err.admin_only": "Admins only.",
        "err.no_isolation": "No isolation backend available; execution refused.",
        "err.root_gate": "Sandbox is configured to run as root (chroot_uid=0) without allow_root_sandbox.",
        "err.platform_blocked": "The sandbox is not enabled for '{platform}'. Allowed: {allowed}.",
        "err.platform_unknown": "Could not determine the platform; the sandbox refused this call.",
    },
}


def normalize(lang: str | None) -> str:
    text = (lang or "").strip().lower().replace("_", "-")
    if text.startswith("zh"):
        return "zh"
    if text.startswith("en"):
        return "en"
    return DEFAULT


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """取文案。缺 key 回落到中文，再回落到 key 本身。"""
    locale = normalize(lang)
    table = _STRINGS.get(locale) or _STRINGS[DEFAULT]
    text = table.get(key) or _STRINGS[DEFAULT].get(key) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def available() -> tuple[str, ...]:
    return SUPPORTED
