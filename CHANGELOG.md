# 更新日志

## 1.0.0

第一个正式版。之前 1.x 的编号是内部迭代，全部按 beta 处理，不单独列。

### 功能

- 沙盒执行：`/sbx run`、`/sbx py`、`/sbx ls`、`/sbx cat`、`/sbx usage`、`/sbx import`、`/sbx export`、`/sbx reset`
- 三种后端：chroot（默认）、bwrap、docker；没有隔离后端时默认拒绝执行
- 权限：单用户配额/超时/联网/导入/启停，场景规则（群/私聊/全部），全局白名单
- WebUI 九个页面：概览 / 设置 / 部署 / 权限 / 文件 / 执行 / 规则 / 日志 / 安全
- 文件页：目录树、语法高亮、Markdown 渲染、CSV 表格、JSON 美化、二进制十六进制
- 安全体检：本地检查 + CISA KEV / NVD + 本地 CVE 库，支持包版本比对
- 定时巡检：发现新的高风险时私聊管理员
- 使用说明：按当前配置动态生成，首次使用自动发送

### 安全

- 子进程无条件 `PR_SET_NO_NEW_PRIVS`，setuid 提权失效
- `PR_SET_DUMPABLE=0`，同 uid 之间不能 ptrace
- 子进程只给最小环境变量，不继承宿主的 API Key
- rootfs 解压与装包后各清一次 setuid/setgid 位
- 命令长度上限、NUL 字符拦截、禁用片段黑名单
- `chroot_uid=0` 默认拒绝执行，要显式打开 `allow_root_sandbox`
- 明文 HTTP 的 rootfs 源必须配 sha256
- 全仓禁止 `eval` / `exec` / `os.system` / `shell=True`，有测试守着

### 修复

- 设置改完不生效：只写文件没回写 AstrBot 的配置对象
- 设置页空白：配置文件带 UTF-8 BOM 时读取失败
- 点按钮没反应：请求没有超时，promise 永远不 settle
- 安全页整片空白：切换标签时漏掉了新加的面板
- 升级沙盒包报 500：审计参数名和函数参数冲突
- 升级完风险不消失：从不比对包版本
- 沙盒目录属主是 root：降权后连 `cd` 都进不去
