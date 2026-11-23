# 排查

先跑：

```bash
python3 install.py doctor
```

它按「平台 → 工具链 → Stata 候选 → 状态目录 → 服务健康 → 本项目环境变量」顺序输出，并且
**不会**打印其他环境变量的值。

---

## 安装阶段

### `no Python >= 3.9 found`

服务端需要 Python 3.9+。安装器会依次尝试：`--python`、`STATA_MCP_PYTHON`、当前解释器、
`PATH` 中的 `python3.13` … `python3.9`。

```bash
python3 install.py install --python /path/to/python3.12
```

### `git is required to fetch the server source` / `git clone failed`

三个选择：

1. 装 git；
2. 用 `--server-dir` 指向已有源码副本（完全离线）：

   ```bash
   python3 install.py install --server-dir ~/src/stata-mcp
   ```

3. 网络受限时给 clone 加镜像前缀（也支持 `--repo` 换源/内网镜像）：

   ```bash
   python3 install.py install --mirror https://your-mirror.example/
   ```

### `dependency installation failed`

依赖安装沿用本机 pip/uv 配置。常见原因与处理：

- 包索引不可达 → 配好 pip/uv 镜像后重试（工具本身不注入索引，也不改你的配置）。
- 想先跳过：`python3 install.py install --skip-deps`，之后再在你自己的环境里装。
- 已经手工准备好环境：`--venv /path/to/venv`。

### `--server-dir ... does not contain any of src/stata_mcp_server.py`

`--server-dir` 必须是上游源码树（含 `src/stata_mcp_server.py` 与 `src/requirements.txt`）。

---

## 服务端启动阶段

### `gateway did not become healthy on 127.0.0.1:4001`

现在运行时是两个进程：客户端端口（默认 4001）是网关，内部端口（默认 4002）是上游引擎。
安装器会打印对应日志尾部。也可直接看：

```bash
tail -50 ~/.stata-mcp/logs/gateway.log
tail -50 ~/.stata-mcp/logs/gateway.console.log
tail -50 ~/.stata-mcp/logs/server.log          # 引擎
tail -50 ~/.stata-mcp/logs/server.console.log  # 引擎 stdout/stderr
```

高概率原因：

| 现象 | 处理 |
|---|---|
| 日志提示 Stata 未找到 | `--stata-path` 指向安装根（含 `StataMP.app` 或 `Stata*-64.exe` 的那一层） |
| 网关端口被占用 | 换端口：`--port 4011`，引擎会自动用 4012 |
| 引擎端口被占用 | 正常安装会自动顺延；`install.py doctor` 会显示实际 `engine port` |
| 许可证/授权问题 | 先在终端里直接运行 Stata 确认可启动 |
| 依赖缺失 | 重跑安装，或 `--venv` 指定已备好依赖的环境 |

### `another MCP service already answers on 127.0.0.1:4001 and it was not started by this installation`

端口上已经有一个**不是本安装启动**的服务（例如旧部署或手工启动的实例）。工具不会去杀它，也不会
假装成功：

```bash
python3 install.py refresh --port 4011        # 换一个空闲端口（引擎自动顺延）
# 或先停掉那个实例，再用原端口重装
```

判定依据是 `<state>/run/jobs.pid`：健康检查能通、但没有本安装的 pid 文件时即视为外来实例。

### 端口被别的程序占用

工具不会替你杀进程。先确认占用者：

```bash
# macOS / Linux
lsof -nP -iTCP:4001 -sTCP:LISTEN
# Windows
netstat -ano | findstr :4001
```

然后换端口，或停掉那个程序。

---

## 客户端接入阶段

### 客户端能启动但工具列表为空 / 调用超时

1. `python3 install.py status` 确认网关与引擎都健康。
2. `python3 install.py verify` —— 它绕过客户端，直接经桥接跑 `display 1+1` 并调一次 `stata_job_list`：
   - 通过 → 问题在客户端配置（路径、传输类型、有没有走代理）。
   - 不通过 → 问题在服务端（回到上一节）。
3. 用 `STATA_MCP_DEBUG=1` 重试可看到桥接的诊断信息（只写 stderr）：

   ```bash
   STATA_MCP_DEBUG=1 /path/to/python ~/.stata-mcp/bin/stata-mcp-bridge.py < request.jsonl
   ```

### 大型 do 文件跑很久 / 客户端报 “tool timed out”

客户端可能在 30 秒左右就取消 MCP 工具调用（与服务端超时无关）。处理方式：

1. **推荐**：用 `stata_run_file`。网关最多等 20 秒，长任务会立刻返回 `job_id`，之后
   `stata_job_tail(job_id, offset)` 取新增步骤、`stata_job_status(job_id, wait_seconds=20)` 等结果。
   这样无论跑 6 小时还是客户端超时 30 秒都不影响。
2. 如果客户端支持工具超时配置（例如 ZCode 的 `timeoutMs`），可以把它调大，给同步调用留余量；
   但不要让同步调用承担小时级任务。
3. 任务超时默认 6 小时，可用 `install.py refresh --default-timeout 43200` 调整；
   单次调用也可以传 `timeout`。

怎么看单步进度：

```bash
tail -f ~/.stata-mcp/jobs/<job_id>/journal.log
```

日志按 Stata 回显的命令切步：`[00:12.4] STEP 7  . regress y x, robust`。

### 任务一直是 `running` 但没有任何新记录

- `sleep`、导入超大文件等命令在完成前不会产生新的日志行，属于正常。
- 超过任务 timeout 后引擎会返回 `ERROR: Execution timed out after ...`，状态变 `timed_out`。
- 上一次网关进程被强杀留下的任务会显示 `interrupted`，记录仍可读；重新跑即可。

### 并行任务互相等待

上游按 `session_id` 串行化同一会话。需要真并行时，给每个 `stata_run_file` / `stata_run_selection`
传不同的 `session_id`；`stata_session(action="list")` 可以看当前会话。

### 开着代理就一定 502？

桥接本身已免疫（`http.client` 直连，不读 `*_PROXY`）。若仍 502：

- 检查客户端配置是不是写成了 `--transport http`（那样流量由客户端发起，客户端可能走代理）。
  开代理的机器建议统一用 stdio 桥接。
- 若客户端有独立的代理/网络设置，为 `127.0.0.1` 加直连例外。

### 片段里的路径不对

片段来自 `<state>/state.json`。用 `--home` 或 `STATA_MCP_HOME` 指到实际安装目录重新生成：

```bash
python3 install.py config --home /actual/state/dir
```

### 改完 `--write-config` 后客户端报 JSON 解析错误

工具只在目标文件已是合法 JSON、且 `root` 路径上无冲突类型时才写，并在写前备份
`NAME.bak-<时间戳>`。若客户端仍报错，回滚：

```bash
cp /path/to/config.json.bak-20260101-120000 /path/to/config.json
```

---

## 通用兜底

```bash
python3 install.py privacy        # 确认工具到底碰了什么
python3 install.py status         # 安装摘要 + 健康
python3 install.py uninstall      # 干净移除运行时
python3 install.py install --print-plan   # 只读预览
```

如果 `uninstall` 提示「a server is answering ... but was not started by this installation」，
说明端口上有别的实例（可能是手工启动的或另一个客户端的）。用你的进程管理方式停掉它，
本工具不会去杀不属于自己的进程。
