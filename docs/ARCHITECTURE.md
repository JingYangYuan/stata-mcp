# 架构与生命周期

## 目标

1. **通用**：任何 MCP 客户端、任何操作系统、任何 Stata 版本与发行方式，都不需要改代码。
2. **隐私**：只碰自己的状态目录；绝不读写别人的配置；不做环境转储、不联网上报。
3. **可审计**：全部逻辑是几百行标准库 Python，读一遍即可确认行为。

## 分层

```text
install.py                     单入口（默认子命令 install）
└── stata_mcp_local/
    ├── paths.py               状态目录布局 + 原子写（0700/0600）
    ├── ui.py                  tty 感知的终端输出（NO_COLOR 友好）
    ├── stata.py               Stata 安装发现（跨平台、无硬编码）
    ├── source.py              上游源码：本地目录 / git clone（可加镜像前缀）
    ├── runtime.py             venv 构建、依赖安装、双进程启停、健康探测
    ├── config_emit.py         MCP 片段生成 + 非破坏性 JSON 合并
    └── cli.py                 子命令与编排
bin/
├── stata-mcp-bridge.py        stdio ↔ Streamable HTTP 桥（独立、零依赖）
├── stata-mcp-start.py         启停器（独立、零依赖、跨平台，管两个进程）
├── stata_mcp_analysis.py      纯标准库分析引擎（Help SMCL、报错行定位、反循环 Linter、向量化指南）
└── stata-mcp-jobs.py          本地网关（17 个 MCP 工具）+ 异步任务 + 单步日志（用服务端 venv）
desktop/
└── Stata MCP Studio           Electron 跨平台桌面端（内嵌 core，Win/Mac Actions 自动打包）
```

`bin/` 下前两个脚本**不 import 上面的包**，只读 `<state>/state.json`，因为安装后被复制到状态目录里，
必须能在没有仓库的情况下独立运行。`stata-mcp-jobs.py` 需要服务端 venv 里的 `mcp` 包（上游依赖之一），
由状态记录的解释器启动。

## 单进程单端口结构与长任务

```text
MCP 客户端 ──> :4001 单进程融合服务（server/src/stata_mcp_server.py --gateway-home <state>）
                    ├─ /mcp-streamable（挂载 bin/stata-mcp-jobs.py 的 17 工具异步网关）
                    ├─ /run_file/stream、/stop_execution、/view_data、/v1/tools（同进程 FastAPI 路由）
                    └─ jobs/<job_id>/records.jsonl + journal.log（每一步一条记录）
```

为什么将网关与引擎融合到单端口 `:4001`：

- 上游 `stata_run_file` 是一次阻塞调用，默认超时 600s；任何超过客户端工具超时（常见 30s）的 do 文件都会直接失败。网关将异步作业、17 个扩展 MCP 工具、精确报错行号定位与反循环分析全部挂载到同一 uvicorn 进程的 `/mcp-streamable`，同时内部 HTTP 调用均在工作线程中执行，既避免事件循环阻塞又省去第二个内部端口 `:4002`。
- `stata-mcp-start.py` 幂等：`:4001/health` 返回健康且模式正常即退出 0；否则启动统一入口并写 `run/server.pid`，轮询 `/health` 直到就绪或超时。
- 若存在旧版双端口残留的 `run/jobs.pid`，`refresh` 与 `stop` 会自动停止并清理旧网关进程，无缝迁移到单端口模式。

## 为什么桥接用标准库

上游服务端是 FastAPI/uvicorn，暴露 Streamable HTTP（`/mcp-streamable`）与 `/health`。
多数 MCP 客户端只支持 stdio 子进程，所以需要一层转发。

用 `http.client` 而不是第三方 HTTP 库，换来三件事：

1. **零安装**：桥接不依赖服务端 venv，任意 Python 3.8+ 都能跑。
2. **代理免疫**：`http.client` 从不读取 `*_PROXY` 环境变量。回环流量走代理是
   `502 Bad Gateway` 的经典成因；这里从机制上排除。
3. **可审计**：一个文件、不到 400 行，行为完全可读。

## 生命周期

```text
install
  1 发现 Stata（显式 → 环境变量 → PATH → 平台目录/注册表）
  2 选解释器（显式 → STATA_MCP_PYTHON → 当前解释器 → PATH 中 3.13…3.9）
  3 复制 bin/ 到 <state>/bin
  4 取得源码（--server-dir，或复用 <state>/server，或 git clone）
  5 建 venv（uv 优先，否则 venv）+ 安装 src/requirements.txt
  6 写 state.json（含 engine_port / default_timeout / sync_wait）
  7 生成执行壳（stata-mcp / stata-mcp.cmd）
  8 启动引擎与网关并等待两者健康
  9 打印 stdio 配置片段（不写任何文件）
```

已装实例升级脚本、不改源码/依赖：

```text
refresh
  1 停掉网关与引擎（按 pid 文件）
  2 重新复制 bin/ 三个脚本
  3 更新 state.json（端口、任务超时、日志路径）
  4 重建执行壳
  5 启动引擎 + 网关
```

客户端调用时：

```text
MCP 客户端 ── JSON-RPC over stdio ──> stata-mcp-bridge.py
                                        │ 1. GET /health（直连，2s）
                                        │ 2. 不健康且允许自动启动 → 调 stata-mcp-start.py
                                        │ 3. POST /mcp-streamable，转发 JSON-RPC
                                        │ 4. 回传 SSE 的 data: 行或 JSON 响应
                                        └─ 会话 id 由 mcp-session-id 头维护
```

`stata-mcp-start.py` 幂等：健康即退出 0；否则用 `<state>/state.json` 记录的入口、解释器、
Stata 路径启动引擎与网关，写 `run/server.pid` 与 `run/jobs.pid`，轮询 `/health` 直到就绪或超时。

## 任务与单步记录

```
<state>/jobs/<job_id>/
├── records.jsonl    每行一条：{"seq","step","t","kind","text"}，kind ∈ command/output/error/graph/marker
├── journal.log      人可读：`[00:12.4] STEP 7  . regress y x, robust`
└── meta.json        状态、耗时、超时、当前命令、日志路径
```

- `stata_job_tail(job_id, offset, mode="steps")` 只返回 `offset` 之后的新记录，默认每条命令配 4 行输出预览；
  `mode="full"` 返回原始日志行。
- 任务状态：`running / finished / failed / timed_out / cancelled / interrupted`。网关重启会把遗留的
  `running` 标成 `interrupted`，记录仍然可读。
- 保留最近 200 个任务 / 7 天，启动时清理；`cache/selection/` 里的临时 selection 文件保留 2 天。
- 取消走上游 `/stop_execution`；并行任务需要不同的 `session_id`（上游会话模型）。

## 关键决策

| 决策 | 原因 |
|---|---|
| 配置文件只打印、不自动写 | 客户端配置里通常有 API Key 和其他 MCP 条目；隐式修改风险远大于收益 |
| 写入必须显式 `--write-config`，且只动一个 key | 把破坏面收敛到可审计的一处；写前备份、拒绝非法 JSON |
| 只支持克隆上游源码，不 vendor | 避免许可证与版本漂移问题，也避免本仓库变成上游的分叉 |
| 端口默认 4001 | 与上游扩展常用默认 4000 错开，避免抢占 |
| 客户端端口永远留给网关，引擎用 port+1 | 已接入的客户端 URL 不需要改；上游升级也不影响客户端 |
| 长任务一律异步 + 落盘日志 | 客户端工具超时（常见 30s）与服务端 600s 默认值都无法靠“调大一点”解决 |
| 不杀端口占用者 | `--force-port` 一类行为会误杀用户进程，这里不提供 |
| 状态集中在单一目录 | 卸载/审计/备份只需处理一个目录树 |
| `bin/` 脚本零 import | 状态目录自包含，仓库删掉也不影响已装实例 |
