# 兼容性

## 平台

| 平台 | 支持 | 说明 |
|---|---|---|
| macOS 12+（Apple Silicon / Intel） | ✅ | 扫描 `/Applications`、`~/Applications` 下的 `Stata*`；自动穿透 `*.app` 找到安装根 |
| Linux | ✅ | 扫描 `/usr/local`、`/opt`、`/usr/lib`、`/usr/share` 下的 `stata*`，以及 `PATH` |
| Windows 10/11 | ✅ | 扫描 `Program Files` / `Program Files (x86)` / 其他盘符一层目录 + 卸载注册表项 |
| 其他 POSIX | ⚠️ | 逻辑可用，路径发现未做专门适配，建议显式 `--stata-path` |

## Stata

- 版本：17 及以上；19 / StataNow 亦可。版本号从安装根下的 `isstata.<NNN>` 标记文件名推断
  （`180` → 18.0，`195` → 19.5）。
- 版本类型：`mp` / `se` / `be` 自动识别（优先 mp），并作为 `--stata-edition` 传给服务端。
- 判定「像 Stata 安装根」的依据：存在 `StataMP.app`/`StataSE.app`/`StataBE.app`/`Stata.app`、
  或 `stata[-mp|-se|-be][.exe]`、或 `utilities/pystata`、或 `stata.lic` / `auto.dta`。
- 传错也不会炸：服务端启动失败时 `install` 会打印日志尾部；用 `--stata-path` 纠正即可。

## Python

| 角色 | 版本 |
|---|---|
| 运行安装器 / 桥接 / 启停器 | 3.8+（纯标准库） |
| 服务端宿主 | 3.9+（上游要求）；安装器自动挑选可用解释器 |

- 有 `uv` 时用 `uv venv` + `uv pip install`（快、跨平台一致）。
- 无 `uv` 时回退 `python -m venv` + `pip`。
- `--venv DIR` 可复用已有虚拟环境（此时不自动装依赖，除非你自行确认）。

## 传输与客户端

| 传输 | 端点 | 适用 |
|---|---|---|
| stdio | `bin/stata-mcp-bridge.py` | 客户端以子进程方式启动 MCP 服务器；支持开代理的机器 |
| Streamable HTTP | `http://<host>:<port>/mcp-streamable` | 客户端原生支持 HTTP 传输，且服务常驻 |

客户端侧唯一要求：接受标准 MCP 配置结构（默认 `mcpServers` 对象，可 `--root` 改成点号路径）。
工具不针对任何具体客户端写代码，也不感知客户端的名字、安装位置或配置文件格式之外的东西。

## 端口

- 默认 `4001`（与上游扩展默认的 `4000` 错开）。
- `--port` 修改；同时改了端口的安装会在片段里带上 `STATA_MCP_PORT` 环境变量。
- 不提供「强杀占用端口的进程」：请自行选择端口或先停掉相关进程（`--force-port` 类行为被有意移除，
  因为误杀用户进程的代价高于便利）。

## 与上游的边界

- 本仓库不包含、不修改上游代码；`install` 只做 clone（或使用 `--server-dir`）。
- 上游版本随之演进：状态里记录 `commit`，`status` 可见；`install --refresh` 可重新拉取。
- 若上游目录结构变化（入口不在 `src/stata_mcp_server.py`），`source.py` 会给出明确报错，
  可用 `--server-dir` 指向任何符合约定的副本。
