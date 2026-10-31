<div align="center">

<img src="https://raw.githubusercontent.com/coming-nbk/astrbot_plugin_sandbox/main/logo.png" width="140" alt="logo">

# Sandbox Console

**A disposable Linux sandbox for AstrBot — run commands, move files in and out.**

<img alt="version" src="https://img.shields.io/badge/version-1.0.1-blue?style=flat-square">
<img alt="platform" src="https://img.shields.io/badge/platform-aiocqhttp%20%7C%20telegram%20%7C%20discord%20%7C%20kook-2ea44f?style=flat-square">
<img alt="license" src="https://img.shields.io/badge/license-AGPL--3.0-red?style=flat-square">
<img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3776ab?style=flat-square">

[中文](README.md) · [English](README.en.md)

</div>

> **Released under the GNU Affero General Public License v3.0 (AGPL-3.0).**
> You may use, modify and redistribute it freely; if you run a modified version as a
> network service, you must publish the modified source under the same license.
> Full terms in [LICENSE](LICENSE). Copyright (C) 2026 maid.

---

## What it does

- One directory per user: `sessions/<user>-<hash>/`, with `imports/`, `tmp/` and `out/` inside
- Commands run in an isolated child process: `bwrap` / `chroot` / `proot` / `docker`, falling back to `local`
- Root filesystem is downloaded and unpacked on first run into `rootfs/`
- Host files are **copied**, never bind-mounted — editing them inside the sandbox leaves the original alone
- Every command has a timeout, a memory cap and an output limit; everything is written to `audit.jsonl`

---

## Upgrade notes

Uninstalling from the WebUI also deletes `plugin_data` — `rootfs/`, `permissions.json`,
trust rules and logs all go with it. Back up first:

```bash
cp -r data/plugin_data/astrbot_plugin_sandbox /tmp/sandbox-backup
```

After changing code you must **fully restart** AstrBot. Reloading the plugin is not enough:
Python keeps the old `sandboxkit` module cached and you get
`Settings object has no attribute ...`.

Keep exactly one copy of the plugin directory. `sandboxkit` is a top-level package name,
so it is global in `sys.modules`: if an old copy is still on disk, the stale one gets
imported first and the new `main.py` ends up paired with the old `sandboxkit`.

---

## Install

```bash
# unzip into data/plugins/ and restart AstrBot
unzip astrbot_plugin_sandbox-v1.0.1.zip -d data/plugins/

# or let the script find AstrBot itself
python3 install.py --astrbot /opt/AstrBot
```

Then open the plugin page and go to the **Setup** tab to check the environment and
download the rootfs. No SSH needed.

---

## Commands

| Command | What it does |
| --- | --- |
| `/sbx run <cmd>` | run a shell command |
| `/sbx py <code>` | run a snippet with `python3` |
| `/sbx ls [path]` | list files |
| `/sbx cat <path>` | print a file |
| `/sbx usage` | quota and limits |
| `/sbx import <host path>` | copy a host file into the sandbox |
| `/sbx export <path>` | copy a sandbox file back to the host |
| `/sbx reset` | wipe your own session |
| `/sbx guide` | usage help |
| `/sbx audit` | recent audit entries |
| `/sbx harden` | show the hardening checklist |
| `/sbx install <pkg>` | install packages into the rootfs |
| `/sbx bootstrap` | re-check / re-download the rootfs |
| `/sbx cve search\|add\|rm\|match` | manage the local CVE feed |

---

## Who can use it

Access is a whitelist, checked in this order:

1. `allow_all` — everyone, when you really mean it
2. `admin_users` — full rights, including `install`
3. `allowed_users` — normal use, no package installs
4. `allowed_platforms` — empty means every adapter; otherwise only the listed ones

User IDs are per-platform: QQ is numeric, Telegram is numeric, Discord is a snowflake,
Lark is a string starting with `ou_`. Wrong format means "authorized but it doesn't work".

---

## Backends

| Where AstrBot runs | Usable backends |
| --- | --- |
| bare metal / systemd | `bwrap`, `chroot`, `local` |
| Docker container as root | `chroot`, `proot`, `local` |
| Docker + `/var/run/docker.sock` + docker CLI | `docker` |
| Docker without the socket | `chroot` / `proot` / `local` |

`auto` tries them in order: `bwrap` → `chroot` → `proot` → `local`.

---

## Security boundary — read this

The sandbox is **not** a security boundary you can hand to strangers.

- `bwrap`: `--cap-drop ALL`, separate PID/UTS/IPC/net namespaces, read-only rootfs, only `sessions/<key>` mounted at `/sandbox`
- `docker`: `--read-only`, `--cap-drop ALL`, `no-new-privileges`, `--tmpfs /tmp`, `--network none`
- `chroot`: drops to `nobody` (`chroot_uid`) immediately, but chroot alone is not isolation
- `local` / `chroot` / `proot` cannot stop a sandboxed process from reaching the network

Every child process gets `PR_SET_NO_NEW_PRIVS` and `PR_SET_DUMPABLE=0`, a minimal
environment (no API keys), and a per-user session directory.

---

## WebUI

- Routes live under AstrBot's `/api` and inherit the console login; `web_auth=token` adds a second layer
- Write requests must be `application/json` with a matching `Origin`
- Responses carry CSP, `nosniff`, `Referrer-Policy` and `X-Frame-Options: SAMEORIGIN`
- Tabs: Overview, Settings, Setup, Access, Files, Run, Rules, Logs, Security

The page is a plain `pages/sandbox/` directory — no build step. `index.html` uses relative
paths for `app.css` and `app.js`.

---

## Limits

| Setting | Default | Meaning |
| --- | --- | --- |
| `quota_bytes` | 256 MB | per-user disk quota |
| `timeout_seconds` | 30 | per-command timeout |
| `memory_mb` | 1536 | memory cap |
| `max_runs_per_minute` | 20 | rate limit, 0 = off |
| `max_command_chars` | 8192 | longer commands are rejected |
| `max_output_bytes` | 64 KB | output truncation |

---

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

606 tests. They are for development only — the release zip does not ship them.

---

## License

AGPL-3.0. See [LICENSE](LICENSE).
