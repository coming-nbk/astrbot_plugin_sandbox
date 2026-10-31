<div align="center">

<img src="https://raw.githubusercontent.com/coming-nbk/astrbot_plugin_sandbox/main/logo.png" width="140" alt="logo">

# 沙盒终端

**给 AstrBot 一个能跑命令的独立沙盒，宿主上的文件可以拖进去操作。**

<img alt="version" src="https://img.shields.io/badge/version-1.0.1-blue?style=flat-square">
<img alt="platform" src="https://img.shields.io/badge/platform-aiocqhttp%20%7C%20telegram%20%7C%20discord%20%7C%20kook-2ea44f?style=flat-square">
<img alt="license" src="https://img.shields.io/badge/license-AGPL--3.0-red?style=flat-square">
<img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3776ab?style=flat-square">
<img alt="astrbot" src="https://img.shields.io/badge/AstrBot-4.0%2B-ff6b6b?style=flat-square">

[中文](README.md) · [English](README.en.md)

</div>

> **本插件以 GNU Affero General Public License v3.0（AGPL-3.0）发布。**
> 你可以自由使用、修改、分发；如果你修改后把它作为网络服务对外提供，
> 必须同样以 AGPL-3.0 公开你修改后的源码。
> 完整条款见 [LICENSE](LICENSE)，版权归 Copyright (C) 2026 maid。

---

## 它到底做了什么

- 每个用户一个独立目录 `sessions/<用户>-<hash>/`，里面 `imports/`、`tmp/`、`out/` 三个子目录
- 命令跑在隔离后端里：bwrap / chroot / proot / docker，都不行时退回 `local`
- 根文件系统可选 Debian 12 slim / Ubuntu base / Alpine minirootfs，首次启动自动下载解压到 `rootfs/`
- 宿主文件不会被「挂载」，是**复制**进沙盒的，复制完跟宿主就没关系了
- 每条命令都有超时、内存、输出上限；所有操作写 `audit.jsonl`

---

## 升级时注意

**在 WebUI 里卸载插件会连 `plugin_data` 一起删掉** —— rootfs、`permissions.json`、信任规则、日志全没了。升级前先备份：

```bash
cp -r data/plugin_data/astrbot_plugin_sandbox /tmp/sandbox-backup
```

**改完插件必须完全重启 AstrBot。** 只点「重载插件」不够：Python 的模块缓存还留着旧的
`sandboxkit`，会出现 `Settings object has no attribute ...` 这类错。插件加载时会主动清掉
旧模块并自检，但进程级重启最干净。

**只保留一份插件目录。** `sandboxkit` 是顶层包名，同名模块在 `sys.modules` 里是全局的：
如果机器上还留着旧副本（升级 AstrBot 时最容易发生），旧的那份先被 import，
新 `main.py` 就会配上旧 `sandboxkit`，报 `unexpected keyword argument` 之类的错。

插件启动时会检测并自动切回自己目录的副本，同时打一条 warning。看到这条就去删旧目录：

```bash
ls data/plugins | grep astrbot_plugin_sandbox
# 只留 astrbot_plugin_sandbox，其它 .bak- 或旧版本目录删掉
```

rootfs 下载失败也不会把插件打死：会按 `配置的源 -> ubuntu -> debian -> alpine` 依次重试，
全失败才报错，并在错误里列出每个源的原因。

---

## 部署前检查清单

1. 插件目录放到 `data/plugins/astrbot_plugin_sandbox/`，重启 AstrBot
2. 看日志有没有 `沙盒插件就绪：backend=...`；没有就往上翻 error
3. **先配 `admin_users`**，否则装完谁都用不了（默认全拒）
4. 确认后端：
   - 裸机 → `bwrap`（装 `bubblewrap`）
   - 容器内是 root → `chroot`
   - 挂了 docker socket 且镜像里有 docker CLI → `docker`（开 `docker_reuse`）
   - 不确定就留 `auto`，概览页会显示实际用了哪个
5. `allow_local_backend` 保持 `false`，除非你确实要在无隔离情况下跑
6. 首次启动会下载 Debian rootfs（约 30MB），需要联网；内网就填 `rootfs_url`
7. `import_roots` 设成允许导入的目录，默认只有 AstrBot 数据目录
8. 打开概览页，确认 `health.ok`、`worker.alive`、`runtime` 三项正常
9. 发一条 `/sbx run echo ok` 验证链路
10. 出问题想立刻全停：在数据目录放一个 `DISABLED` 文件，删掉即恢复

---

## 安装

**一键装**（推荐，Linux / Windows / macOS 都行，只要有 Python）：

```bash
# 解压后进入目录
python3 install.py --astrbot /path/to/AstrBot
# 或让脚本自己找
python3 install.py
# 想连测试一起装、顺便预下载 rootfs
python3 install.py --astrbot /path/to/AstrBot --with-tests --rootfs
```

脚本会备份旧版本、复制文件、探测可用后端、编译检查、跑冒烟测试，最后打印上线清单。`--dry-run` 只看不改。

**手动装**：

```bash
cd /path/to/astrbot/data/plugins
unzip astrbot_plugin_sandbox-vX.Y.Z.zip
```

无第三方依赖，只用标准库。重启后看日志里有没有：

```
沙盒插件就绪：backend=bwrap rootfs=/.../plugin_data/astrbot_plugin_sandbox/rootfs
```

---

## 命令

命令前缀 `/sandbox`，可简写 `/sbx` 或 `/沙盒`。

| 命令 | 作用 |
| --- | --- |
| `/sbx run <命令>` | 在沙盒里执行 shell 命令 |
| `/sbx py <代码>` | 执行一段 python（rootfs 里要有 python3） |
| `/sbx ls [路径]` | 列目录 |
| `/sbx cat <路径>` | 看文本文件 |
| `/sbx rm <路径>` | 删文件或目录 |
| `/sbx import <宿主绝对路径> [as 名字]` | 把宿主文件复制进 `imports/` |
| `/sbx export <路径>` | 把沙盒文件发回聊天 |
| `/sbx usage` | 配额占用 |
| `/sbx backend` | 当前后端、限制、是否联网 |
| `/sbx install <包名…>` | 以 root 在 rootfs 里装包（仅管理员，影响所有用户） |
| `/sbx reset` | 清空自己的沙盒 |
| `/sbx bootstrap` | 重建 rootfs（仅管理员） |

LLM 工具（bot 自己会调）：`sandbox_exec`、`sandbox_python`、`sandbox_import`、`sandbox_export`、`sandbox_list`。每个工具都走同一套权限判断，没权限的用户让 bot 调也没用。

---

## 谁能在哪用

四层，从具体到宽泛：

| 层 | 存在哪 | 例子 |
| --- | --- | --- |
| 管理员 | 插件配置 `admin_users` | 永远能用，能执行 bootstrap |
| 单用户 | `permissions.json` | 某人配额 1GB、允许联网、禁止导出 |
| 场景规则 | `scopes.json` | 这个群所有人能用；那个群禁止；私聊只放行两个人 |
| 全局 | 插件配置 `allowed_users` / `allowed_groups` / `allow_all` | 兜底白名单 |

命中就停，不再往下看。所以「群规则允许、但某人被单独停用」的结果是停用。

场景规则优先级：指定群号 > `group:*` > 私聊 > `all`。每条规则可以带 `users` 白名单，留空表示该场景所有人。

**单用户还能单独开关能力**：执行命令、运行 Python、导出文件、重置沙盒。比如给某人只读权限，就关掉前两个。

默认 `allow_all: false` 且白名单为空 —— 装完谁都用不了，先把自己在那个平台上的用户 ID 填进 `admin_users`。

---

## 死循环与信任命令

两条看门狗：

- **内存**：进程组 RSS 超过 `memory_mb` 整组 SIGKILL
- **CPU**：进程组 CPU 占用连续 `cpu_burst_seconds` 秒超过 `cpu_percent_limit`%，判定死循环，整组 SIGKILL

结果里会分别标「内存超限已终止」和「疑似死循环已终止」，不会和普通超时混在一起。

**信任命令**（WebUI「规则」页）可以豁免：

```json
{"pattern": "make *", "bypass_memory": true, "bypass_cpu": true, "timeout_seconds": 600}
```

按整条命令做通配匹配，命中后跳过对应看门狗，`timeout_seconds` 可单独放宽。信任等于主动放弃这层保护 —— 只加自己确认过的命令，别写 `*`。

---

## 页面走的是 AstrBot 的 bridge

页面在 AstrBot 的**受限 iframe**（`allow-scripts allow-forms allow-downloads`，没有 `allow-same-origin`）里，
所以 `fetch`、`localStorage`、`document.cookie` 全部不可用。所有后端调用都通过
`window.AstrBotPluginView`（`bridge.apiGet / apiPost`）由 Dashboard 转发，鉴权也由它带。

后端接口按官方要求注册成 `/{插件名}/...`，页面里写相对路径（`overview`、`files/save`）。
后端用 `astrbot.api.web` 的 `request` / `json_response` / `error_response`，老版本自动回退到 quart。

---

## 页面打不开或按钮没反应

顶栏有一行小字，它是判断「脚本到底跑没跑」的最快方式：

| 显示 | 含义 |
| --- | --- |
| 脚本未运行 | JS 没执行 —— 多半是页面被嵌在禁止脚本的 iframe 里，直接访问 `/astrbot_plugin_sandbox/api/page`（插件自己提供的路由，和接口同源） |
| 脚本已启动，正在连接… | JS 在跑，正在请求接口 |
| 已连接 · 自动刷新 10s | 正常，概览/部署/日志页每 10 秒自动刷新 |
| 脚本出错：… / 请求失败：… | 具体原因会写在后面，不用翻控制台 |
| 提示「AstrBot 拒绝了这个请求（未授权）」 | AstrBot 4.29 给接口加了鉴权：在已登录控制台的同一浏览器里新标签页打开 `/astrbot_plugin_sandbox/api/page`；页面会自动从 localStorage 取 token，取不到就在部署页「控制台 token」里粘一次 |
| 页面顶部出现黄色提示 | 页面在 sandbox iframe 里（origin=null），接口会被浏览器拦掉；按提示在新标签页打开 `/astrbot_plugin_sandbox/api/page`，或加 `?api=` 指定接口地址 |

接口前缀会**自动探测**三个候选（`/astrbot_plugin_sandbox/api`、`/api/astrbot_plugin_sandbox/api`、`/api/plugin/astrbot_plugin_sandbox/api`），404 才换下一个。部署页的「诊断连接」会列出每个候选的 HTTP 状态或失败原因。

`localStorage` 在沙盒 iframe 里会抛 `SecurityError`，所以页面里所有存储访问都包了 try/catch —— 以前一次异常就会让整页脚本停摆，看起来「完全是静态的」。

---

## 设置页

「设置」标签可以直接改插件配置，写回 AstrBot 自己的 `data/config/astrbot_plugin_sandbox_config.json`：

- 保存前自动备份成 `.bak-时间戳`
- 只允许改白名单字段，未知键忽略
- 保存后**立即生效**（内存里的 settings 同步替换）
- 标「需重启」的项（`backend`、`chroot_uid`、`worker_isolation`）要 `docker restart`

最常改的一项单列出来了：**沙盒内用户**

| 值 | 效果 |
| --- | --- |
| `nobody`（65534，默认） | 安全；`apt` 用不了，装包走 `/sbx install` |
| `root`（0） | `apt` 直接可用；同时自动开启下面的硬防护 |

### 选 root 时自动加的硬防护

`chroot_uid: 0` 时，子进程在 exec 前会：

1. `prctl(PR_SET_NO_NEW_PRIVS, 1)` —— 禁止 setuid/setcap 提权
2. 丢掉危险 capability 的 bounding set：`SYS_MODULE`、`SYS_ADMIN`、`NET_ADMIN`、`SYS_PTRACE`、`SYS_RAWIO`、`SYS_BOOT`、`SYS_CHROOT`

也就是说沙盒内虽然是 root，但**不能 mount、不能 chroot 逃逸、不能加载内核模块、不能 ptrace 宿主**。
配置里 `root_hardening: false` 可以关掉（不建议）。

注意：这层靠 libc 的 `prctl`，非 Linux 或极老内核上会静默跳过。

---

## 部署向导（页面里点，不用 SSH）

「部署」页把装、测、配三件事都放进了 WebUI：

| 按钮 | 做什么 | 输出 |
| --- | --- | --- |
| 环境自检 | 容器类型、可用后端、rootfs、worker、健康状态、目录 | 直接显示在页面 |
| 快速自检 | 目录可写、越界拒绝、执行链路、超时终止、内存看门狗、后端隔离、rootfs | 逐项 `[通过]/[失败] + 耗时` |
| 深度自检 | 再加 CPU 死循环、docker 可用性 | 同上 |
| 下载 rootfs | 后台线程下载 Debian rootfs，页面每秒轮询进度 | 实时日志 |
| 授权并使用 | 给指定用户写 `permissions.json`，立即生效 | 返回用户记录 |

自检跑在 `_selftest/` 临时目录里，跑完自动清掉；不改任何配置，也不执行你输入的命令（用例是写死的）。

长任务用任务 id 轮询（`/setup/rootfs` 返回 id，`/setup/task?task_id=` 查进度），HTTP 请求不会挂住。

---

## WebUI

页面按 AstrBot 的约定放：`pages/sandbox/` 一个目录，`index.html` 用**相对路径**引用同目录的 `app.css` / `app.js`。`pages/index.html` 只是跳转桩。没有构建步骤。仓库里改完可以跑 `python3 scripts/sync_pages.py --check` 自检。

### 界面长什么样

八个标签页叠成**一摞卡片**：当前页在最上面，底下露出两张卡的边缘。切页时旧卡往下沉进叠里，新卡按翻页方向从侧面抽出来盖上去 —— 往后翻从右边进，往回翻从左边进。

| 标签 | 能做什么 |
| --- | --- |
| 概览 | 后端状态、rootfs 版本、沙盒数、配额、宿主内存/磁盘/负载、运行信息 |
| 设置 | 直接改插件配置，写回 `data/config/astrbot_plugin_sandbox_config.json`，保存即生效 |
| 部署 | 环境自检、选套餐下 rootfs、一键授权、装软件包 |
| 权限 | 增删改单用户权限（配额、超时、联网、导入、启停、沙盒内权限） |
| 文件 | 用户下拉选择、目录树、预览/编辑/保存、图片预览、下载、复制宿主路径、删除。预览会按类型渲染：代码带行号和语法高亮，Markdown 渲染成排版，CSV/TSV 变表格，JSON 美化，二进制给十六进制头 |
| 执行 | 指定用户沙盒里跑命令，输出是终端样式 |
| 规则 | 信任命令（跳过看门狗）、场景规则（哪些群/私聊谁能用） |
| 日志 | 最近一个日志文件、立即清理 |

### 视觉与交互

- Material 3 规范：颜色角色、8dp 间距阶梯、4/8/12/16/28 圆角、tonal elevation
- 深色模式跟随系统（`prefers-color-scheme`）
- 动效：卡片进出、标签指示条滑动、按钮水波纹、数字滚动、进度条从 0 长、提示条退场
- 无障碍：`:focus-visible` 描边、`prefers-reduced-motion` 下关闭全部动画
- 手机：600 / 420 / 380 三个断点，tab 换行、表格横向滚、底部安全区

### 危险操作的确认

不用浏览器自带的 `confirm`，全部是页面内的正式弹窗，写清后果：

| 操作 | 确认方式 |
| --- | --- |
| 下载 rootfs（已有可用环境时） | 红色弹窗列出后果，**必须手动输入「替换」** 才能点确认 |
| 下载 rootfs（首次） | 普通弹窗，说明体积和耗时 |
| 安装软件包 | 弹窗提示「会影响所有用户的沙盒」 |
| 删除用户 / 文件 / 信任规则 / 场景规则 | 红色弹窗，写明删了之后会发生什么 |

另外，插件启动时如果发现 rootfs 没就绪，会自动下载一次，并在概览顶部留一条黄色提示（时间、发行版、原因），重启后仍然可见 —— 免得你更新插件重启之后发现环境被换了却不知道是谁干的。

### 鉴权

| `web_auth` | 行为 |
| --- | --- |
| `dashboard`（默认） | 不额外检查，接口跟着 AstrBot 控制台的登录态走 |
| `token` | 再要求 `X-Sandbox-Token` 头等于 `web_token` |
| `none` | 不检查，只适合本机调试 |

`web_enabled: false` 不注册任何接口，配置表单照常可用。

### 接口

都在 `/astrbot_plugin_sandbox/` 下：

`overview`、`users`、`users/all`、`users/save`、`users/delete`、`trust`、`trust/save`、`trust/delete`、`scope`、`scope/save`、`scope/delete`、`tree`、`file`、`file/save`、`file/delete`、`file/raw`、`exec`、`packages/install`、`settings`、`settings/save`、`logs`、`logs/cleanup`、`setup/*`。

> 端点叫 `settings` 不叫 `config`：`config` 会和宿主自己的 `/api/config` 撞车，返回 500。旧路径 `/config` 仍保留兼容。

**插件页面等价于管理员面板**：能看所有人的沙盒、能执行任意命令。它跟着控制台登录态走，所以别把 6185 直接暴露到公网。

---

## 宿主导入的白名单

`import_roots` 是**唯一**能导入的范围，留空则只允许 AstrBot 数据目录。

```yaml
import_roots:
  - /opt/astrbot/data
  - /srv/files
```

路径会先 `realpath` 再判断，符号链接指到外面一样被拦。命中 `import_deny_globs` 的文件（`.env`、`*.pem`、`id_rsa*` 等）直接拒绝。

---

## 后端怎么选

| 后端 | 隔离强度 | 需要 | 能禁网 |
| --- | --- | --- | --- |
| `bwrap` | 文件系统 + PID + 网络命名空间 | `apt install bubblewrap`，非特权即可 | 能 |
| `chroot` | 文件系统（chroot 不是强隔离） | root | 不能 |
| `proot` | 文件系统（用户态模拟） | `apt install proot` | 不能 |
| `docker` | 容器 | docker 守护进程 | 能 |
| `local` | 无 | 无 | 不能 |

`auto` 按 bwrap → chroot → proot → docker → local 挑第一个能用的。配置里写死某个后端但它不可用时，会回落到 `local` 并在日志里说明。

**`local` 后端等于没有沙盒**：命令以 AstrBot 的身份跑，能读宿主上 AstrBot 能读的一切。只适合本机自己玩。

---

## 根文件系统：默认 Ubuntu 24.04

**四种来源，按可靠性排序，前一个失败自动试下一个：**

1. `rootfs_image`：用 `docker export` 从本地已有镜像里导出（`alpine:3.20` / `debian:bookworm-slim`）。**完全离线可用，最可靠**
2. 清华镜像的 Alpine minirootfs（国内快）
3. 官方 CDN 的 Alpine
4. Ubuntu base / Debian slim（GitHub 上的第三方产物，说没就没）

`rootfs_url` 也支持直接填本地 tar 包路径，内网部署用这个。

| 预置 | 内容 | 体积 | 包管理器 |
| --- | --- | --- | --- |
| `ubuntu`（默认） | Ubuntu base 24.04：bash、coreutils、apt | ~30MB | apt |
| `debian` | 第三方产物，GitHub 上那个路径已 404，仅作兜底 | — | apt |
| `alpine` | 只有 busybox，连 `bash` 都没有 | ~3MB | apk |
| `custom` | 完全按 `rootfs_url` 下载 | 取决于你 | 自动识别 |

首次启动会下载对应 rootfs，然后用 `bootstrap_packages`（默认 `["python3"]`）装包，所以 `/sbx py` 开箱可用。装包要宿主机能联网，跟沙盒的 `allow_network` 无关 —— 装包走的是宿主网络。

换预置：

```yaml
rootfs_preset: debian
rootfs_version: bookworm   # 留空用预置默认
bootstrap_packages:
  - python3
  - curl
  - git
```

改完 `/sbx bootstrap`（管理员）重建。内网或想固定版本就填 `rootfs_url` + `rootfs_sha256`。

Alpine 仍保留，给只要 3MB 的场景；但它真的只有 busybox，`/sbx py` 需要先装 python3。

---

## 资源限制

| 配置 | 作用 |
| --- | --- |
| `timeout_seconds` | 墙钟超时，到点 `killpg` 整组进程 |
| `memory_mb` | `RLIMIT_AS` |
| `cpu_seconds` | `RLIMIT_CPU` |
| `max_output_bytes` | stdout / stderr 各自上限，超了截断并标注 |
| `quota_bytes` | 每个用户沙盒的总占用 |
| `quota_check_seconds` | 配额检查间隔，默认 30 秒；扫描目录有成本，间隔内复用上次结果 |
| `import_max_bytes` | 单个文件导入/导出上限 |
| `max_procs` | `RLIMIT_NPROC`，默认 0（关）。它按 uid 统计，共享主机上开了会把宿主已有进程算进去 |
| `max_cores` | CPU 核数，用亲和性把命令限制在前 N 个核上；0 = 不限 |
| `memory_watchdog` | 按进程组统计 RSS，超过 `memory_mb` 就整组 SIGKILL |
| `cpu_percent_limit` | 进程组 CPU 占用持续超过这个百分比就判定死循环，默认 95 |
| `cpu_burst_seconds` | 连续超限多少秒才动手，默认 5（编译、解压会短时跑满） |
| `rootfs_preset` | 用哪套根文件系统，见上一节 |

---

## 安全边界（请读完）

**这个插件的本质是远程命令执行。** 拿到权限的用户能在沙盒里跑任意命令，能做的事的上限由后端决定，不由代码决定。下面每一条都是「默认拒绝」，需要你明确打开。

### 没有隔离就拒绝执行

`backend=local` 时命令以 AstrBot 的身份跑，能读宿主上 AstrBot 能读的一切。所以默认 `allow_local_backend: false`：没有 bwrap / chroot / proot / docker 可用时，`run`、`py` 和 WebUI 的执行接口一律拒绝，只保留看文件、导入导出。要冒险就把这个开关打开，日志和 `/sbx backend` 都会写明。

### 三道闸

| 闸 | 默认 | 作用 |
| --- | --- | --- |
| `max_runs_per_minute` | 20 | 每人每分钟执行次数，滑动窗口，超了直接拒绝并回还要等几秒 |
| `max_concurrent_runs` | 4 | 全局同时在跑的命令数，满了拒绝，不排队 |
| `quota_bytes` | 256MB | 执行前检查，已超配额就不跑；执行后超了会在结果里警告 |

配额是「软上限」：靠执行前后统计目录大小来卡，不是文件系统级 quota。单个文件另有 `RLIMIT_FSIZE` 兜底。

### 文件权限

- 沙盒目录和 `imports/`、`tmp/`、`out/` 一律 `0700`
- 导入的文件、生成的脚本、保存的文件 `0600`
- `audit.jsonl` `0600`，`permissions.json` 走 `mkstemp`（本来就是 0600）
- 解压 rootfs 时剥掉 setuid / setgid 位，沙盒里提权没有意义

### 导入

- 只允许 `import_roots` 里的路径，先 `realpath` 再判断，符号链接指到外面一样拦
- `import_deny_globs` 默认拦 `.env`、`*.pem`、`*.key`、`id_rsa*`、`*.kdbx` 等
- 单文件上限 `import_max_bytes`，边拷边数，源文件中途变大也突破不了
- 目录导入按文件计数，超过 `import_max_files` 整个拒绝，目录内部的 `.env` 也会被跳过

### 后端隔离

- `bwrap`：`--cap-drop ALL`、PID / UTS / IPC / 网络命名空间分离、rootfs 只读挂载、只把 `sessions/<key>` 挂成 `/sandbox`
- `docker`：`--read-only`、`--cap-drop ALL`、`--security-opt no-new-privileges`、`--tmpfs /tmp`、`--network none`
- `chroot`：进入后立刻 `setuid` 到 `nobody`（`chroot_uid`），但 chroot 本身不是安全边界，能上 bwrap 就上
- `local` / `chroot` / `proot` 无法阻止沙盒内进程访问网络

### 页面走的是 AstrBot 的 bridge

页面在 AstrBot 的**受限 iframe**（`allow-scripts allow-forms allow-downloads`，没有 `allow-same-origin`）里，
所以 `fetch`、`localStorage`、`document.cookie` 全部不可用。所有后端调用都通过
`window.AstrBotPluginView`（`bridge.apiGet / apiPost`）由 Dashboard 转发，鉴权也由它带。

后端接口按官方要求注册成 `/{插件名}/...`，页面里写相对路径（`overview`、`files/save`）。
后端用 `astrbot.api.web` 的 `request` / `json_response` / `error_response`，老版本自动回退到 quart。

---

## 部署形态与性能

插件跑在 AstrBot 进程里，所以「能不能用某个后端」取决于 AstrBot 所在的容器给了什么。

| AstrBot 跑在哪 | 可用后端 | 每条命令的额外开销（量级） |
| --- | --- | --- |
| 裸机 / systemd | bwrap、chroot、local | bwrap 5–15ms、chroot 3–8ms、local 2–5ms |
| Docker 容器，普通权限 | chroot（容器内是 root 时）、proot、local | chroot 3–8ms |
| Docker 容器 + `-v /var/run/docker.sock` + 镜像内有 docker CLI | docker | 首次 300ms–1s，之后 30–80ms（复用容器） |
| Docker 容器，没有 socket | 只能 chroot / proot / local | 同上 |

数字是量级，不是承诺。**在自己机器上跑一次才准**：

这两个脚本只在仓库里，发布包不带：

```bash
python3 scripts/bench.py --all --runs 20 --quota
python3 scripts/bench.py --backend local --runs 20 --worker   # 对比进程间往返
```

实测参考（proot 环境，绝对值虚高）：同进程 `sh -c true` 56.9ms，经 worker 58.1ms —— **IPC 往返约 1ms**，真正的成本是 worker 首次启动（Python 解释器），所以插件启动时会预热。

### 为什么 docker 要复用容器

`docker run --rm` 每条命令都要「创建容器 → 起进程 → 删容器」，这部分和命令本身无关，通常是 300ms 起。`docker_reuse: true`（默认）改成：每个沙盒留一个常驻容器（`sleep infinity`），命令走 `docker exec`，只有第一次付建容器的钱。闲置 `docker_idle_minutes` 分钟后由清理任务 `docker rm -f`。

容器名是 `astrbot-sbx-<沙盒 key>`，带 `astrbot.sandbox=1` 标签，进程崩溃留下的孤儿可以用 `cleanup_orphans()` 按标签扫掉。

### 插件自身开销

除了后端，每条命令还有这些固定成本：

| 环节 | 成本 | 优化 |
| --- | --- | --- |
| fork + exec | 2–5ms | 无法避免 |
| 配额统计 | 文件越多越慢 | 按 `quota_check_seconds` 缓存，执行后只失效不重扫 |
| 沙盒目录检查 | 4 次 mkdir/chmod | `ensure()` 每实例只做一次 |
| 审计 + 日志 | 2 次追加写 | 0600 打开、失败吞掉 |

配额是软上限：执行前用缓存值判断，执行后标记失效，下一次执行或定时巡检（`cleanup_interval_hours`）才会发现超了。单个文件另有 `RLIMIT_FSIZE` 兜底。

---

## 执行进程隔离

默认 `worker_isolation: true`：命令不是由插件进程直接 fork 的，而是交给一个独立的 worker 进程。

```
AstrBot 进程 ──(JSON 行协议 / stdin+stdout)──> worker 进程 ──> 沙盒里的命令
```

- worker 是会话首进程，父进程一个 `killpg` 就能把它和它派生的命令一起收掉，不留孤儿
- worker 崩了、卡住、返回乱码，父进程只拿到一个带 `error` 的结果，AstrBot 不受影响
- 超时或崩溃后 worker 自动重开，下次调用照常
- 启动时预热一次，第一条用户命令不用等 Python 启动
- `worker_fallback: true` 时，worker 起不来会回落到同进程执行（日志里记一条），关掉则直接不可用
- 每次崩溃都会记进熔断器：60 秒内连崩 5 次就暂停沙盒，不会无限重开
- 崩溃次数、连续崩溃数、存活状态在概览页的 `worker` 字段里

代价是每条命令多一次进程间往返，正常机器上是 1–3ms；worker 首次启动约 50ms，之后常驻。

---

## 别把 AstrBot 拖死

插件和宿主同进程，所以「插件状态差」= 「宿主状态差」。四道防护：

| 防护 | 行为 |
| --- | --- |
| 独立线程池 | 沙盒的活不走 AstrBot 的默认执行器，占满也只堵自己 |
| 熔断 | 60 秒内 5 次内部异常就暂停 300 秒，期间所有入口直接回「暂停使用」 |
| 自检 | `plugin_max_rss_mb` / `plugin_max_threads` 超限时拒绝新任务（默认关） |
| 拉闸 | 数据目录放一个 `DISABLED` 文件，立刻全停，删掉即恢复，不用改配置 |

熔断和拉闸的状态会写进概览页和日志。熔断不靠「成功一次就洗白」—— 只有超出时间窗的失败才被遗忘。

另外两处细节：

- `killpg` 前先比对自己的进程组，相等就拒绝，避免杀错组把宿主带走
- `resource` 在模块加载时导入，`preexec_fn` 里不再 import —— 多线程 fork 后抢 import 锁会死锁

---

## 为什么沙盒里 apt 装不了

沙盒内的进程降权到 `nobody`（`chroot_uid: 65534`），写不了 `/var/lib/dpkg`，所以
`apt-get install` 会失败。这是有意的 —— 给沙盒 root，chroot 就能反过来逃逸。

装包走独立通道，由插件以 root 执行，只暴露「装哪些包」：

```
/sbx install ffmpeg sqlite3 tree        # 管理员，影响所有用户
```

或者部署页的「安装软件包」卡片。包名有白名单校验（`^[a-z0-9][a-z0-9+.-]{0,63}$`），
一次最多 12 个。

真要给沙盒 root，把 `chroot_uid` 改成 `0`，风险自负。

---

## 文件怎么拿出来

AstrBot 4.29 有个自带的沙盒运行时开关，关着的时候「把文件发到聊天」会被它拦掉，报
`Sandbox runtime is disabled by configuration` —— 这是 AstrBot 的配置，不是本插件。

所以本插件给两条不依赖它的出口：

1. 文件页选中文件 → **下载**：后端读成 base64 回给页面，浏览器直接存盘，全程走 bridge
2. 文件页 → **复制宿主路径**：拿去宝塔/`docker cp` 直接捞

聊天里的 `/sbx export` 仍然走 AstrBot 的文件通道，那个开关关着就会失败。

---

## 日志

两套，分工不同，都放在 `plugin_data/astrbot_plugin_sandbox/` 下：

| 文件 | 内容 | 轮转 | 清理 |
| --- | --- | --- | --- |
| `logs/sandbox-YYYY-MM-DD.log` | 给人看的运行记录：后端选择、rootfs 准备、每次执行、异常 | 按天切；单文件超过 `log_max_bytes` 再切一个带时间戳的 | 超过 `log_retention_days` 或数量超过 `log_max_files` 就删 |
| `audit.jsonl` | 结构化证据：谁、什么命令、exit code、耗时 | 超过 `audit_max_bytes` 轮转到 `.1` | 超过 `audit_retention_days` 删 |

清理有两个触发点：插件启动时跑一次，之后每 `cleanup_interval_hours` 小时一次（后台任务，卸载插件时取消）。WebUI 的「日志」页也能看最近 200 行、手动点一次「立即清理」。

日志权限 `0600`，目录 `0700`。写失败只吞掉，不影响命令执行。

内存是两层：`RLIMIT_AS` 卡单进程，看门狗卡整个进程组 —— 只设前者的话，fork 十个孩子每个吃一半就绕过去了。

---

## 目录结构

```
astrbot_plugin_sandbox/
├── main.py              AstrBot 入口：命令 + LLM 工具 + 生命周期
├── metadata.yaml
├── _conf_schema.json    WebUI 配置项
├── sandboxkit/          纯逻辑，不 import astrbot
│   ├── config.py        配置收敛 + 访问控制
│   ├── paths.py         宿主/沙盒两条路径边界
│   ├── limits.py        受限执行、输出截断
│   ├── backends.py      bwrap / chroot / proot / docker / local
│   ├── bootstrap.py     下载、校验、安全解压 minirootfs
│   ├── session.py       每用户目录、配额、导入导出
│   ├── service.py       子命令实现
│   ├── store.py         permissions.json
│   ├── webfs.py         目录树、预览、保存、删除
│   ├── webapi.py        WebUI 控制器（不依赖 quart）
│   ├── audit.py         JSONL 审计
│   ├── logbook.py       按天切分、定时清理的运行日志
│   ├── guard.py         限流与并发闸门
│   ├── env.py           运行环境探测（容器 / socket / CLI）
│   └── parse.py         消息拆分
├── pages/
│   ├── index.html       跳转桩（旧约定兜底，<1KB）
│   └── sandbox/         AstrBot 实际加载的页面
│       ├── index.html   结构
│       ├── app.css
│       └── app.js
├── install.sh / install.py   一键安装 / 升级
├── scripts/           开发脚本（不进发布包）
└── tests/               python3 -m unittest discover -s tests -t .
```

运行期产生的文件：

```
plugin_data/astrbot_plugin_sandbox/
├── rootfs/              解压出来的根文件系统
├── sessions/<用户>-<hash>/   每个用户的沙盒
├── permissions.json     单用户权限与能力位
├── trust.json           信任命令（豁免看门狗）
├── scopes.json          场景规则（哪个群/私聊谁能用）
├── audit.jsonl          审计日志（轮转保留 .1）
└── logs/                按天的运行日志，过期自动清
```

---

## 跑测试

```bash
python3 -m unittest discover -s tests -t .
```

---

## 许可证

**AGPL-3.0**，见 `LICENSE`。

简单说：可以改、可以自己用、可以分发，但**如果你把它改成一个对外提供的网络服务，必须公开你改过的源码**。这是 AGPL 和 GPL 的唯一区别，也是选它的原因 —— 免得有人拿这套东西做个闭源沙盒平台卖钱。

Copyright (C) 2026 maid

---

## 支持哪些平台

插件只用四个通用能力：发送者 ID、群 ID、统一会话标识（`unified_msg_origin`）、纯文本回复。
所以命令本身在任何适配器上都能跑。

**元数据里只声明了四个**，因为导出文件要用 `File` 组件，而有些平台发不了任意文件：

| 适配器 | 命令 | 导出文件 | 备注 |
| --- | --- | --- | --- |
| aiocqhttp（OneBot v11） | 支持 | 支持 | 作者自用环境，实测过 |
| telegram | 支持 | 支持 | 代码层面可用，未实测 |
| discord | 支持 | 支持 | 代码层面可用，未实测 |
| kook | 支持 | 支持（有大小限制） | 代码层面可用，未实测 |

其它适配器（qq_official、lark、dingtalk、slack、wecom…）：命令能用，但**发文件不保证**，所以没写进 `support_platforms`。

发不了文件的平台，把 `export_as_file` 关掉 —— `/sbx export` 会改成回你宿主路径，你自己去服务器上取。

**只开放部分平台**：设置里 `allowed_platforms` 填适配器名（一行一个），留空表示全部开放。不在名单里的平台发 `/sbx` 会被拒绝并提示可用的平台。

**用户 ID 不是 QQ 号**。`admin_users` / `allowed_users` 填的是各平台自己的 ID 格式：QQ 是数字，Telegram 是数字，Discord 是雪花 ID，飞书是 `ou_` 开头的串。填错就是「授权了但用不了」。

---

## 从 GitHub 安装

仓库根目录就是插件根目录（`main.py`、`metadata.yaml` 在根上），所以两种装法都行：

**装法一：填仓库地址（推荐，能一键更新）**

AstrBot 控制台 → 插件管理 → 从链接安装 → 填：

```
https://github.com/coming-nbk/astrbot_plugin_sandbox
```

**装法二：上传 zip**

在仓库页面点 `Code → Download ZIP`，把得到的 zip 直接拖进「插件管理 → 安装插件」的上传框。

注意：GitHub 下载的 zip 里顶层目录叫 `astrbot_plugin_sandbox-main/`，AstrBot 认这个结构；如果你自己打包，顶层目录名要是插件名。

**目录结构**（仓库根 = 插件根）：

```
main.py              # 入口
metadata.yaml        # 插件元信息
_conf_schema.json    # 配置表单
sandboxkit/          # 真正的逻辑，不依赖 astrbot，可单测
pages/sandbox/       # WebUI（index.html + app.css + app.js）
tests/               # 578 个测试
LICENSE
SECURITY.md
README.md
```

---

## 第三方

这个插件**不打包**别人的代码，只按需按路径加载：

| 组件 | 关系 | 说明 |
| --- | --- | --- |
| `astrbot_plugin_search_engines` | 运行时调用，不打包 | `/sbx cve search` 会按路径加载它的 `crawler.py`，读它的代理配置；它有自己的许可证，分发本插件不包含它的代码 |
| CISA KEV / NVD | 公开数据 | 通过 HTTP 读取公开 JSON，不做缓存分发 |

沙盒 rootfs 用的是发行版官方或第三方镜像（Ubuntu base、Alpine minirootfs），下载和使用遵守各自发行版的许可。
