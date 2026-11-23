# stata-mcp（本地通用部署）

在**任意 MCP 客户端**里使用本地 Stata 的部署工具：一键把 [hanlulong/stata-mcp](https://github.com/hanlulong/stata-mcp)
服务端装到本机私有目录、独立后台常驻，并生成标准 MCP 配置片段。

- **不依赖任何特定软件**：不需要任何编辑器或其扩展，不需要任何特定 agent 宿主 / MCP 网关，
  也不需要 bash-only 或 PowerShell-only 环境。安装器、桥接、启动器全部是**单文件 Python 标准库**，
  macOS / Linux / Windows 同一套代码。
- **不碰别人的配置**：默认只在终端打印配置片段，**不读取、不改写任何其他产品的配置文件**
  （那些文件通常含 API Key 和其他 MCP 服务器条目）。只有显式 `--write-config PATH` 时才会写入，
  且只动一个 server 条目、写前自动备份、遇到非法 JSON 直接拒绝。
- **不硬编码机器信息**：仓库里没有用户名、邮箱、盘符、编辑器目录、端口以外的机器路径；
  Stata 安装位置、端口、状态目录、上游仓库地址全部是参数或自动发现。

> 上游项目（MIT，Han Lulong 等）只作为**服务端代码来源**：本仓库不 vendor、不 fork、不打补丁，
> 安装时按需 clone 到本机私有目录，或使用你已有的源码副本（`--server-dir`）。

---

## 支持范围

| 项目 | 说明 |
|---|---|
| 操作系统 | macOS（Apple Silicon / Intel）、Linux、Windows 10/11 |
| Stata | 17 及以上（mp / se / be 均可；StataNow 亦支持），路径自动发现或 `--stata-path` 指定 |
| Python | 3.8+ 运行安装器；服务端宿主解释器 3.9+（有 `uv` 时用 `uv`，否则用 `venv`+`pip`） |
| 传输 | stdio（推荐，经代理免疫桥接）或 Streamable HTTP |
| MCP 客户端 | 任何支持 JSON `mcpServers` 配置的客户端 |

不需要的东西：任何编辑器、任何 agent 宿主、任何 MCP 网关、任何第三方 Python 包（桥接零依赖）。

---

## 快速开始

macOS / Linux：

```bash
git clone <this-repo> stata-mcp && cd stata-mcp
python3 install.py                       # 等价于 install：拉源码 → 建私有 venv → 启动 → 打印配置片段
```

Windows（PowerShell 或 cmd）：

```powershell
python install.py
```

常用变体：

```bash
python3 install.py install --print-plan              # 只打印计划，不下载、不写盘、不启动
python3 install.py install --stata-path /path/to/Stata --port 4001
python3 install.py install --server-dir ~/src/stata-mcp   # 用已有源码，完全离线
python3 install.py install --mirror https://your-mirror.example/   # 网络受限时给 git 加镜像前缀
python3 install.py install --venv /path/to/existing/venv           # 复用已有虚拟环境
python3 install.py status | verify | doctor | privacy | uninstall
```

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `install` | 获取源码（可跳过）、建私有 venv、记录状态、写启动器、启动引擎+网关、打印配置片段 |
| `refresh` | 已装实例原地升级：停进程、重拷 `bin/`、更新记录、重启（不 clone、不 pip） |
| `config` | 打印 / 落盘（`--emit`）/ 合并（`--write-config`）MCP 配置片段；支持 `--transport stdio\|http`、`--root mcpServers\|mcp.servers`、`--key`、`--timeout-ms` |
| `start` / `stop` | 后台启动 / 停止引擎与网关（幂等） |
| `status` | 安装摘要 + 网关与引擎健康检查 |
| `verify` | 端到端自检：经 stdio 桥接跑 `display 1+1` 并调一次 `stata_job_list` |
| `doctor` | 环境诊断（Stata 候选、工具链、状态、双进程健康、任务目录、仅本项目使用的环境变量） |
| `privacy` | 打印完整的数据流与文件触碰声明 |
| `uninstall` | 停止服务并删除运行时（`--purge` 连状态目录一起删） |

所有命令都接受 `--home DIR`（默认 `~/.stata-mcp`，或环境变量 `STATA_MCP_HOME`）。

---

## 接入任意 MCP 客户端

`python3 install.py config` 会输出类似（stdio，推荐）：

```json
{
  "mcpServers": {
    "stata-mcp": {
      "command": "/path/to/python-or-venv-python",
      "args": ["/path/to/.stata-mcp/bin/stata-mcp-bridge.py"]
    }
  }
}
```

或 HTTP（客户端需支持 Streamable HTTP 传输）：

```bash
python3 install.py config --transport http --port 4001
```

把它粘到你所用客户端的配置里即可，客户端名字无关紧要——每个客户端都只是读同一个 JSON 结构。
细节（dotted root、写盘、备份、非破坏性合并）见 [docs/CONFIG.md](docs/CONFIG.md)。

部分客户端（例如 ZCode）支持给每个 MCP 服务器配置工具调用超时，可显式加进片段：

```bash
python3 install.py config --transport http --timeout-ms 120000
```

---

## 长 do 文件与单步记录

上游 `stata_run_file` 是一次阻塞调用，默认 600s；很多客户端还会在 30s 左右直接取消工具调用。
本部署在客户端与服务端之间加了一层**本地网关**，把长任务变成后台作业：

```text
客户端 :4001（网关，URL 不变）──> :4002（上游引擎，源码不改）
```

| 工具 | 用途 |
|---|---|
| `stata_run_file` / `stata_run_selection` | 同名兼容。最多等 20s：短脚本直接给结果，长脚本返回 `job_id` |
| `stata_job_status` | 状态、耗时、当前第几步；`wait_seconds` 最多阻塞 25s |
| `stata_job_tail` | 取 `offset` 之后的新记录；`mode="steps"` 每条命令 + 输出预览，`mode="full"` 原始日志 |
| `stata_job_list` / `stata_job_cancel` | 列任务 / 走上游 `/stop_execution` 取消 |
| `stata_session` | 上游会话管理透传；并行任务需不同 `session_id` |

每一步都会落盘，随时可看：

```bash
tail -f ~/.stata-mcp/jobs/<job_id>/journal.log
# [00:00:01.8] STEP 2  . import delimited using data.csv, clear
# [00:00:12.4] STEP 3  . regress y x1 x2, robust
```

默认任务超时 6 小时（`install --default-timeout 43200` 可改），删除策略是保留最近 200 个任务 / 7 天。

---

## 目录与产物

所有产物集中在一个**私有状态目录**（默认 `~/.stata-mcp`，目录 0700、文件 0600）：

```text
~/.stata-mcp/
├── state.json           安装记录（路径、端口、解释器；不含任何密钥）
├── server/              上游源码检出（仅 clone 时）
├── venv/                服务端专用 Python 环境
├── bin/                 stata-mcp-jobs.py（网关）、stata-mcp-bridge.py（stdio↔HTTP）、
│                        stata-mcp-start.py（启停）、stata-mcp 执行壳
├── jobs/<job_id>/       任务日志：records.jsonl、journal.log、meta.json
├── logs/server.log      引擎日志（可能含 Stata 命令输出与路径）
├── logs/gateway.log     网关日志（任务启动、引擎错误）
├── logs/*.console.log   两个进程的 stdout/stderr
├── run/server.pid       引擎进程号
└── run/jobs.pid         网关进程号
```

四类角色：

```text
MCP 客户端 ──stdio──> bin/stata-mcp-bridge.py ──HTTP(127.0.0.1:4001)──> 网关 stata-mcp-jobs.py
                                                                              │
                                                                              └──HTTP(127.0.0.1:4002)──> 上游引擎 ──> 本地 Stata
MCP 客户端 ──HTTP(127.0.0.1:4001)──────────────────────────────────────────────┘
                                 │
                                 └─ 未运行时自动调用 bin/stata-mcp-start.py 拉起（可选，默认开启）
```

桥接用 `http.client` **直连** 回环端口，永不读取 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`，
因此本地代理（Clash / Surge / Mihomo 等）不会把本地流量劫持成 `502`。

---

## 隐私

- 运行时不联网（除了你显式触发的源码 clone 与依赖安装），无遥测、无远程日志、无更新检查。
- 只读：Stata 发现所需的目录列表、`PATH`、Windows 卸载注册表项、上游源码树。
- 只写：上面列出的状态目录；**外加**你显式 `--write-config` 指定的那一个 JSON 文件及其备份。
- `doctor` 只打印本项目自己的 `STATA_MCP_*` 变量，不做环境变量转储。
- 生成的片段里不含任何密钥；`.gitignore` 默认挡住日志、数据集、`.env`、备份文件。

完整声明：`python3 install.py privacy` 与 [docs/PRIVACY.md](docs/PRIVACY.md)。

---

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 组件、生命周期、为什么这样分层
- [docs/CONFIG.md](docs/CONFIG.md) — 配置片段、传输选择、安全写入语义
- [docs/PRIVACY.md](docs/PRIVACY.md) — 数据流与文件触碰声明、审计方法
- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) — 平台 / Stata 版本 / 客户端兼容性说明
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — 常见故障与排查

## 许可与致谢

本仓库（部署工具）采用 [MIT License](LICENSE)，Copyright (c) 2026 JingYangYuan。

服务端代码**不在本仓库内**：安装时从 [hanlulong/stata-mcp](https://github.com/hanlulong/stata-mcp)
（MIT，版权归其作者 Han Lulong 等所有）clone 到本机私有目录，或使用你已有的源码副本。
使用、再分发该部分代码请遵循其许可证。
