# 客户端配置

工具**不假设**你用的是哪个 MCP 客户端。它只做两件事：生成标准 MCP 配置片段；或者在你明确指定的
文件里合并一个条目。客户端叫什么、配置放哪里，由你决定。

## 生成片段

```bash
python3 install.py config                        # stdio，打印到终端
python3 install.py config --transport http       # Streamable HTTP 片段
python3 install.py config --emit ~/stata-mcp.json  # 写成文件（0600），不动任何客户端配置
```

stdio 片段：

```json
{
  "mcpServers": {
    "stata-mcp": {
      "command": "<venv-python>",
      "args": ["<state>/bin/stata-mcp-bridge.py"]
    }
  }
}
```

HTTP 片段：

```json
{
  "mcpServers": {
    "stata-mcp": {
      "type": "http",
      "url": "http://127.0.0.1:4001/mcp-streamable"
    }
  }
}
```

### 客户端工具超时

有些客户端会取消超过固定时长（例如 30 秒）的 MCP 工具调用。网关已经把长 do 文件改成后台任务，
同步调用最多 20 秒就会返回 `job_id`，所以**不配置也能跑长任务**。若仍想给同步调用留余量，
可以让片段带上客户端专用字段（目前仅在你显式要求时生成）：

```bash
python3 install.py config --transport http --timeout-ms 120000
```

```json
{
  "mcpServers": {
    "stata-mcp": {
      "type": "http",
      "timeoutMs": 120000,
      "url": "http://127.0.0.1:4001/mcp-streamable"
    }
  }
}
```

何时用哪个：

- **stdio**：客户端以子进程方式启动 MCP 服务器（最常见）。桥接会自动拉起后台服务、
  直连回环端口、绕开 HTTP 代理，适合开代理的机器。
- **http**：客户端自己支持远程/Streamable HTTP。此时必须保证服务常驻（`start` 命令或系统服务管理），
  且客户端不要走代理访问 `127.0.0.1`。

两者可以同时存在：同一台机器上 stdio 与 http 指向同一个后台服务。

## 顶层键不是 `mcpServers` 的客户端

`--root` 支持点号路径，`--key` 支持改名：

```bash
python3 install.py config --root mcp.servers          # {"mcp": {"servers": {...}}}
python3 install.py config --key stata                 # 条目名改为 stata
```

自定义环境变量也可覆盖：`STATA_MCP_HOME` / `STATA_MCP_HOST` / `STATA_MCP_PORT` /
`STATA_MCP_AUTOSTART` / `STATA_MCP_START_CMD` / `STATA_MCP_TIMEOUT`。

## 写入已有配置文件（可选，显式）

```bash
python3 install.py config --write-config /path/to/client-config.json           # 合并 stata-mcp 一个条目
python3 install.py config --write-config /path/to/client-config.json --dry-run # 只看结果，不写
```

写入语义（`config_emit.write_config`）：

1. 目标文件不存在 → 新建（0600），只含该条目。
2. 目标文件存在 → `json.loads` 成功后，**只**设置 `root.key` 这一项；其余键值（含其他 MCP 服务器、
   API Key、主题设置）原样保留。
3. 写之前把原文件复制为 `NAME.bak-YYYYmmdd-HHMMSS`（复制失败会明确报告）。
4. 文件不是合法 JSON、或根本身不是对象、或 `root` 路径上已有非对象值 → **拒绝修改**并提示手动处理。
5. 条目已与预期完全一致 → 报告 `unchanged`，不写、不产生备份。

原子写：先写同目录临时文件再 `os.replace`，崩溃不会留下半个文件；保留原文件权限位。

## 手工核对

片段里出现的路径只有三类：解释器、桥接脚本、状态目录。可直接验证：

```bash
python3 install.py status       # 安装摘要 + 健康
python3 install.py verify       # 经桥接跑 display 1+1
```
