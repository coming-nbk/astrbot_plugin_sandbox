#!/usr/bin/env python3
# Copyright (C) 2026 maid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""一键安装 / 升级 astrbot_plugin_sandbox。

为什么用 Python 而不是 shell：AstrBot 一定有 Python，Windows 上也能直接用，
不用再维护一份 PowerShell。

做的事：
  1. 找到 AstrBot 根目录（或 --astrbot 指定）
  2. 备份旧版本
  3. 复制插件文件（默认不带测试）
  4. 探测这台机器能用哪个后端（bwrap / chroot / proot / docker）
  5. 可选：预先下载 rootfs，省得第一次启动卡在下载
  6. 编译检查 + 打印上线清单

用法：
    python3 install.py --astrbot /opt/AstrBot
    python3 install.py --astrbot /opt/AstrBot --with-tests
    python3 install.py --astrbot /opt/AstrBot --rootfs
    python3 install.py --astrbot /opt/AstrBot --dry-run
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_sandbox"
SOURCE = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".venv"}


def say(message: str) -> None:
    print(message, flush=True)


def find_astrbot(explicit: str) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if (path / "data").is_dir() or (path / "astrbot").is_dir():
            return path
        say(f"[x] {path} 不像 AstrBot 根目录（没有 data/ 或 astrbot/）")
        return None

    candidates = []
    env_root = os.environ.get("ASTRBOT_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    cwd = Path.cwd()
    candidates.extend(
        [
            cwd,
            cwd / "AstrBot",
            cwd.parent / "AstrBot",
            Path.home() / "AstrBot",
            Path.home() / "astrbot",
            Path("/opt/AstrBot"),
            Path("/root/AstrBot"),
        ]
    )
    for candidate in candidates:
        try:
            if (candidate / "data" / "plugins").is_dir():
                return candidate.resolve()
        except OSError:
            continue
    return None


def copy_plugin(target: Path, with_tests: bool, dry_run: bool) -> list[str]:
    copied: list[str] = []
    for path in sorted(SOURCE.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(SOURCE).parts):
            continue
        relative = path.relative_to(SOURCE)
        if not with_tests and relative.parts and relative.parts[0] == "tests":
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        destination = target / relative
        copied.append(str(relative))
        if dry_run:
            continue
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    return copied


def probe_runtime() -> list[str]:
    """用插件自己的探测逻辑，保证建议和运行期一致。"""
    sys.path.insert(0, str(SOURCE))
    try:
        from sandboxkit.env import advice, detect

        runtime = detect()
        lines = [
            f"容器内：{'是（' + runtime.container_kind + '）' if runtime.in_container else '否'}",
            f"root：{'是' if runtime.is_root else '否'}",
            f"bwrap：{'有' if runtime.has_bwrap else '无'}   "
            f"proot：{'有' if runtime.has_proot else '无'}   "
            f"docker CLI：{'有' if runtime.has_docker_cli else '无'}",
            f"docker socket：{runtime.docker_socket or '无'}",
        ]
        tips = advice(runtime)
        if tips:
            lines.append("建议：")
            lines.extend(f"  - {tip}" for tip in tips)
        return lines
    except Exception as exc:  # noqa: BLE001 - 探测失败不该挡住安装
        return [f"探测失败（{exc}），不影响安装"]
    finally:
        sys.path.pop(0)


def fetch_rootfs(astrbot: Path, timeout: float) -> bool:
    sys.path.insert(0, str(SOURCE))
    try:
        from sandboxkit.bootstrap import BootstrapError, bootstrap, detect_arch

        target = (
            astrbot / "data" / "plugin_data" / PLUGIN_NAME / "rootfs"
        )
        say(f"下载 rootfs 到 {target}（Debian slim，约 30MB）")
        info = bootstrap(
            target,
            preset="debian",
            arch=detect_arch(),
            packages=("python3",),
            timeout=timeout,
            log=lambda message: say(f"  {message}"),
        )
        say(f"[ok] rootfs {info.preset} {info.version} {info.arch} 就绪")
        return True
    except BootstrapError as exc:
        say(f"[!] rootfs 下载失败：{exc}")
        say("    插件启动时会再试一次；内网环境请在配置里填 rootfs_url")
        return False
    finally:
        sys.path.pop(0)


def compile_check(target: Path) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        say("[x] 编译检查失败：")
        say((result.stderr or result.stdout)[-800:])
        return False
    return True


def run_smoke(target: Path) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_plugin_smoke", "-q"],
        cwd=str(target),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        say("[!] 冒烟测试未通过（不影响启动，但建议贴出来看看）：")
        say((result.stderr or result.stdout)[-1200:])
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="一键安装 astrbot_plugin_sandbox")
    parser.add_argument("--astrbot", default="", help="AstrBot 根目录（含 data/）")
    parser.add_argument("--with-tests", action="store_true", help="连测试一起装（发布包不带 tests，仅在仓库里有效）")
    parser.add_argument("--rootfs", action="store_true", help="顺便下载 rootfs")
    parser.add_argument("--no-backup", action="store_true", help="不备份旧版本")
    parser.add_argument("--dry-run", action="store_true", help="只看会做什么")
    parser.add_argument("--timeout", type=float, default=300.0, help="下载超时秒数")
    args = parser.parse_args()

    say(f"源目录：{SOURCE}")
    astrbot = find_astrbot(args.astrbot)
    if astrbot is None:
        say("[x] 没找到 AstrBot 根目录，请用 --astrbot /path/to/AstrBot 指定")
        return 2
    say(f"AstrBot：{astrbot}")

    target = astrbot / "data" / "plugins" / PLUGIN_NAME
    say(f"安装到：{target}")

    if target.exists() and not args.no_backup and not args.dry_run:
        backup = target.with_name(
            f"{PLUGIN_NAME}.bak-{time.strftime('%Y%m%d%H%M%S')}"
        )
        shutil.move(str(target), str(backup))
        say(f"已备份旧版本 -> {backup.name}")

    files = copy_plugin(target, args.with_tests, args.dry_run)
    say(f"复制 {len(files)} 个文件" + ("（dry-run，未落盘）" if args.dry_run else ""))

    say("\n运行环境：")
    for line in probe_runtime():
        say(f"  {line}")

    if args.dry_run:
        say("\ndry-run 结束，没有改动磁盘。")
        return 0

    if not compile_check(target):
        return 3
    say("[ok] 编译检查通过")

    if args.with_tests and run_smoke(target):
        say("[ok] 冒烟测试通过")

    if args.rootfs:
        say("")
        fetch_rootfs(astrbot, args.timeout)

    say(
        f"""
下一步（都能在网页里点完）：
  1. 重启 AstrBot
  2. 打开控制台 -> 插件 -> 沙盒 -> 页面（或直接访问 /astrbot_plugin_sandbox/api/page）
  3. 进「部署」页：环境自检 -> 快速自检 ->（可选）下载 rootfs
  4. 「一键授权」里填你的用户 ID（QQ / Telegram / 飞书都行），保存后立刻生效
  5. 聊天里发：/sbx run echo ok
  6. 想全停：在 {astrbot / 'data' / 'plugin_data' / PLUGIN_NAME} 放一个 DISABLED 文件
"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
