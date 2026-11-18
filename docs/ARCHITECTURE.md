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
    ├── runtime.py             venv 构建、依赖安装、进程启停、健康探测
    ├── config_emit.py         MCP 片段生成 + 非破坏性 JSON 合并
    └── cli.py                 子命令与编排
bin/
├── stata-mcp-bridge.py        stdio ↔ Streamable HTTP 桥（独立、零依赖）
└── stata-mcp-start.py         启停器（独立、零依赖、跨平台）
```

`bin/` 下两个脚本**不 import 上面的包**，只读 `<state>/state.json`，因为安装后被复制到状态目录里，
必须能在没有仓库的情况下独立运行。少量重复是有意的取舍。

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
  6 写 state.json
  7 生成执行壳（stata-mcp / stata-mcp.cmd）
  8 启动并等待健康
  9 打印 stdio 配置片段（不写任何文件）
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
Stata 路径启动，写 `run/server.pid`，轮询 `/health` 直到就绪或超时。

## 关键决策

| 决策 | 原因 |
|---|---|
| 配置文件只打印、不自动写 | 客户端配置里通常有 API Key 和其他 MCP 条目；隐式修改风险远大于收益 |
| 写入必须显式 `--write-config`，且只动一个 key | 把破坏面收敛到可审计的一处；写前备份、拒绝非法 JSON |
| 只支持克隆上游源码，不 vendor | 避免许可证与版本漂移问题，也避免本仓库变成上游的分叉 |
| 端口默认 4001 | 与上游扩展常用默认 4000 错开，避免抢占 |
| 不杀端口占用者 | `--force-port` 一类行为会误杀用户进程，这里不提供 |
| 状态集中在单一目录 | 卸载/审计/备份只需处理一个目录树 |
| `bin/` 脚本零 import | 状态目录自包含，仓库删掉也不影响已装实例 |
