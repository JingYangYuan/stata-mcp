#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Start / stop / inspect the local Stata MCP runtime.

The runtime is two processes:

* the **engine** (upstream ``stata_mcp_server.py``) on the internal port, which
  owns the Stata session and executes code;
* the **gateway** (``stata-mcp-jobs.py``) on the client-facing port, which
  exposes the MCP tools, runs long ``.do`` files as background jobs and keeps a
  per-step journal.

Cross-platform replacement for per-shell hook scripts: one standard-library
Python file works on macOS, Linux and Windows, so nothing here depends on bash,
PowerShell, an editor extension, or a particular MCP host.

Behaviour
---------
``python3 stata-mcp-start.py``              start both, wait for health, exit 0
``python3 stata-mcp-start.py --foreground`` run the gateway in this process
``python3 stata-mcp-start.py --status``     exit 0 when healthy, 1 otherwise
``python3 stata-mcp-start.py --stop``       stop the processes recorded in state

Reads ``<state>/state.json`` (written by ``install.py``); no other file is read
except the server source it launches. Only the loopback ports are contacted.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import subprocess
import sys
import time

HEALTH_PATH = "/health"


def resolve_home(explicit=None):
    for value in (explicit, os.environ.get("STATA_MCP_HOME")):
        if value:
            return os.path.abspath(os.path.expanduser(str(value)))
    return os.path.abspath(os.path.expanduser(os.path.join("~", ".stata-mcp")))


def load_state(home):
    path = os.path.join(home, "state.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def health(host, port, timeout=1.5):
    try:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request("GET", HEALTH_PATH, headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                return None
            try:
                return json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                return {"status": "unknown"}
        finally:
            connection.close()
    except (OSError, http.client.HTTPException):
        return None


def wait_healthy(host, port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = health(host, port, timeout=1.5)
        if info:
            return info
        time.sleep(0.4)
    return None


def _win_no_window_kwargs():
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def _win_background_flags(breakaway=True):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    if breakaway:
        flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    return flags


def _is_alive_win32(pid):
    try:
        import ctypes
        import ctypes.wintypes as wt

        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            err = k32.GetLastError()
            if err == 5:
                return True
            if err == 87:
                return False
            return None
        try:
            code = wt.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def is_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        direct = _is_alive_win32(pid)
        if direct is not None:
            return direct
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                **_win_no_window_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        target = '"%d"' % pid
        return any(target in line for line in (result.stdout or "").splitlines())
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return 0


def ensure_private_dir(path):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def write_pid(path, pid):
    ensure_private_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("%d\n" % pid)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def ports(state, host, port_override=None):
    port = int(port_override or os.environ.get("STATA_MCP_PORT") or state.get("port") or 4001)
    engine = int(state.get("engine_port") or port + 1)
    if engine == port:
        engine = port + 1
    return port, engine


def engine_argv(state, host, engine_port, log_file):
    entry = state.get("server_entry")
    python = state.get("python") or sys.executable
    stata_path = state.get("stata_path")
    if not entry or not os.path.isfile(entry):
        raise SystemExit("stata-mcp-start: server entry not found (%s); re-run install" % entry)
    if not stata_path:
        raise SystemExit("stata-mcp-start: no Stata installation recorded; re-run install")
    ensure_private_dir(os.path.dirname(log_file) or ".")
    argv = [
        python,
        entry,
        "--stata-path",
        stata_path,
        "--host",
        host,
        "--port",
        str(engine_port),
        "--log-file",
        log_file,
    ]
    edition = state.get("stata_edition")
    if edition in ("mp", "se", "be"):
        argv += ["--stata-edition", edition]
    return argv


def gateway_argv(state, home, host, port):
    script = state.get("jobs_file") or os.path.join(home, "bin", "stata-mcp-jobs.py")
    python = state.get("python") or sys.executable
    if not os.path.isfile(script):
        raise SystemExit("stata-mcp-start: gateway not found (%s); re-run install" % script)
    return [
        python,
        script,
        "--home",
        home,
        "--host",
        host,
        "--port",
        str(port),
        "--log-file",
        os.path.join(home, "logs", "gateway.log"),
    ]


def spawn(argv, console_log):
    ensure_private_dir(os.path.dirname(console_log) or ".")
    handle = open(console_log, "ab")
    try:
        if os.name == "nt":
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=handle,
                    creationflags=_win_background_flags(breakaway=True),
                    close_fds=True,
                )
            except OSError as exc:
                if getattr(exc, "winerror", None) != 5:
                    raise
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=handle,
                    creationflags=_win_background_flags(breakaway=False),
                    close_fds=True,
                )
        else:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
                close_fds=True,
            )
    finally:
        handle.close()
    return process


def start(home, state, host, port_override, wait, foreground):
    port, engine_port = ports(state, host, port_override)
    log_file = state.get("log_file") or os.path.join(home, "logs", "server.log")
    engine_pid_file = os.path.join(home, "run", "server.pid")
    gateway_pid_file = os.path.join(home, "run", "jobs.pid")

    engine_ok = bool(health(host, engine_port))
    gateway_ok = bool(health(host, port))
    if engine_ok and gateway_ok:
        return 0

    if not engine_ok:
        process = spawn(engine_argv(state, host, engine_port, log_file), os.path.join(home, "logs", "server.console.log"))
        write_pid(engine_pid_file, process.pid)
        if not wait_healthy(host, engine_port, wait):
            raise SystemExit("stata-mcp-start: engine did not become healthy on %s:%d" % (host, engine_port))

    if gateway_ok:
        return 0

    if foreground:
        argv = gateway_argv(state, home, host, port)
        os.execv(argv[0], argv)

    process = spawn(gateway_argv(state, home, host, port), os.path.join(home, "logs", "gateway.console.log"))
    write_pid(gateway_pid_file, process.pid)
    if not wait_healthy(host, port, wait):
        return 1
    return 0


def stop_one(pid, label):
    if pid <= 0 or not is_alive(pid):
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                **_win_no_window_kwargs(),
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass
    deadline = time.time() + 10
    while time.time() < deadline and is_alive(pid):
        time.sleep(0.2)
    if is_alive(pid):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    **_win_no_window_kwargs(),
                )
            else:
                sigkill = getattr(signal, "SIGKILL", None)
                if sigkill is not None:
                    os.kill(pid, sigkill)
        except (OSError, subprocess.SubprocessError):
            pass
    return True


def _health_gateway_pid(info):
    if not isinstance(info, dict):
        return 0
    gw = info.get("gateway")
    if isinstance(gw, dict):
        try:
            val = int(gw.get("pid") or 0)
            return val if val > 0 else 0
        except (TypeError, ValueError):
            return 0
    return 0


def stop(home, state, host, port_override):
    port, engine_port = ports(state, host, port_override)
    gateway_pid_file = os.path.join(home, "run", "jobs.pid")
    engine_pid_file = os.path.join(home, "run", "server.pid")

    stopped = False
    stopped = stop_one(read_pid(gateway_pid_file), "gateway") or stopped
    if not stopped:
        gw_pid = _health_gateway_pid(health(host, port, timeout=1.0))
        if gw_pid:
            stopped = stop_one(gw_pid, "gateway") or stopped
    stopped = stop_one(read_pid(engine_pid_file), "engine") or stopped
    for path in (gateway_pid_file, engine_pid_file):
        try:
            os.unlink(path)
        except OSError:
            pass

    if not stopped:
        if health(host, port, timeout=1.0) or health(host, engine_port, timeout=1.0):
            return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="stata-mcp-start",
        description="Start, stop or check the local Stata MCP runtime (engine + gateway).",
    )
    parser.add_argument("--home", help="state directory (default: ~/.stata-mcp or STATA_MCP_HOME)")
    parser.add_argument("--host", help="host to bind/probe (default from state.json, else 127.0.0.1)")
    parser.add_argument("--port", help="client-facing port (default from state.json, else 4001)")
    parser.add_argument("--wait", type=float, default=120.0, help="seconds to wait for health (default: 120)")
    parser.add_argument("--foreground", action="store_true", help="run the gateway in this process")
    parser.add_argument("--status", action="store_true", help="report health and exit")
    parser.add_argument("--stop", action="store_true", help="stop the recorded processes")
    args = parser.parse_args(argv)

    home = resolve_home(args.home)
    state = load_state(home)
    host = args.host or os.environ.get("STATA_MCP_HOST") or state.get("host") or "127.0.0.1"
    port, engine_port = ports(state, host, args.port)

    if args.status:
        info = health(host, port)
        if info:
            info = dict(info)
            info.setdefault("engine_port", engine_port)
            sys.stdout.write(json.dumps(info, ensure_ascii=False) + "\n")
            return 0
        return 1
    if args.stop:
        return stop(home, state, host, args.port)
    return start(home, state, host, args.port, args.wait, args.foreground)


if __name__ == "__main__":
    sys.exit(main())
