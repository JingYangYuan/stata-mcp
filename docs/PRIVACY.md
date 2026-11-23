# 隐私与数据流声明

`python3 install.py privacy` 会打印同一份声明的当前版本（含实际状态目录路径）。本文说明设计依据，
方便审计与复核。

## 一句话

只写一个自己的私有目录；只在你明确点名时才会碰一个已有的 JSON 文件；不联网上报任何东西。

## 写入清单

| 路径 | 内容 | 何时 |
|---|---|---|
| `<state>/state.json` | 安装记录：源码路径、入口、解释器、Stata 路径、端口、commit | `install` |
| `<state>/server/` | 上游源码检出 | 仅在不给 `--server-dir` 时 `git clone` |
| `<state>/venv/` | 服务端专用 Python 环境 | `install`（`--venv` 时跳过） |
| `<state>/bin/` | 网关、桥接、启停器、执行壳 | `install` / `refresh` |
| `<state>/jobs/<job_id>/` | 任务日志：`records.jsonl`、`journal.log`、`meta.json`（**含 Stata 命令与输出**） | 每次运行 do 文件 |
| `<state>/cache/selection/` | `stata_run_selection` 的临时 do 文件（保留 2 天） | 运行期 |
| `<state>/logs/server.log` | 引擎日志（**可能含 Stata 命令输出、数据集路径**） | 运行期 |
| `<state>/logs/gateway.log` | 网关日志（任务启动、引擎错误） | 运行期 |
| `<state>/logs/*.console.log` | 两个进程的 stdout/stderr | 运行期 |
| `<state>/run/server.pid` | 引擎进程号 | 运行期 |
| `<state>/run/jobs.pid` | 网关进程号 | 运行期 |
| `<用户显式指定的 JSON>` + `*.bak-<时间戳>` | 仅 `config --write-config PATH` | 用户主动触发 |

目录 0700、文件 0600（`state.json`、pid、日志、片段文件、执行壳），在支持的平台上尽力设置。
任务目录只保留最近 200 个 / 7 天，启动时自动清理。

目录 0700、文件 0600（`state.json`、pid、日志、片段文件、执行壳），在支持的平台上尽力设置。
日志里可能出现你的数据路径与 Stata 输出——这是本地日志的正常内容，也是为什么 `.gitignore`
默认排除 `*.log`、`*.dta`、`*.smcl` 等文件。

## 读取清单

| 读取对象 | 目的 | 说明 |
|---|---|---|
| 目录列表（`/Applications`、`Program Files`、`PATH`、系统程序目录） | 发现 Stata 安装 | 只列目录，不读文件内容 |
| Windows 卸载注册表（`...\Uninstall`） | 发现 Stata 安装 | 只读 `DisplayName` / `InstallLocation` 元数据 |
| `isstata.*` 标记文件名 | 推断 Stata 版本号 | 只取文件名 |
| `--server-dir` 指向的源码树 | 找入口与依赖清单 | 只读 `src/stata_mcp_server.py`、`requirements.txt` |
| `<state>/state.json` | 启停与配置片段 | 自家文件 |
| 你 `--write-config` 指定的 JSON | 合并一个条目 | 仅在显式指定时；先备份 |

## 网络

| 目的地 | 何时 | 备注 |
|---|---|---|
| `git clone <repo>` | `install` 且未给 `--server-dir` | URL 由 `--repo` / `--mirror` 决定；`GIT_TERMINAL_PROMPT=0`，不会弹凭据输入 |
| 包索引（pip/uv 默认源） | `install` 安装 `src/requirements.txt` | 沿用你本机的 pip/uv 配置（含私有镜像）；不注入凭据、不改索引 |
| `127.0.0.1:<port>` | 运行期 | 健康检查与 MCP 端点，仅本机回环 |

没有遥测、统计、崩溃上报、远程日志、版本检查。`verify` / `status` / `doctor` 只访问回环端口。

## 不信任代理

桥接（`bin/stata-mcp-bridge.py`）、启停器、安装器都用 `http.client` 直连回环地址，
**不读取** `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`。因此开启本地代理
（Clash / Surge / Mihomo / V2Ray 等）不会把本地健康检查与 MCP 请求送出本机或被代理拒绝。

## 明确不做的事

- 不读取、不修改任何其他 MCP 客户端的配置（无论它叫什么、放在哪里）。
- 不读取 API Key、令牌、凭据、shell 配置、`.env`。
- 不做环境变量转储：`doctor` 只打印本项目的 `STATA_MCP_*` 变量。
- 不把密钥写进片段、日志、状态或仓库。
- 不杀端口占用进程、不静默改端口、不请求 sudo/管理员权限。
- 不在仓库里存放任何个人信息（域名、邮箱、用户名、盘符、机器路径）。

## 审计方法

```bash
# 1. 只读预览：不下载、不建目录、不启动
python3 install.py install --print-plan

# 2. 直接读代码：全部标准库，无第三方依赖
wc -l install.py stata_mcp_local/*.py bin/*.py

# 3. 看所有写入点
grep -rn "write_text_atomic\|save_state\|open(.*[, ]\"[wa]" stata_mcp_local bin

# 4. 确认没有外联
grep -rn "http://\|https://\|socket\|urlopen\|requests" stata_mcp_local bin

# 5. 用完即走
python3 install.py uninstall --purge
```

## 卸载

```bash
python3 install.py uninstall           # 停服务，删 venv/server/bin/logs/run/cache 与 state.json
python3 install.py uninstall --purge   # 连状态目录一起删
```

卸载不会触及任何其他产品的文件；你通过 `--write-config` 写入的条目及其备份需要你自己清理
（工具不会替你改动别人的配置）。
