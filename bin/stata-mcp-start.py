#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Start / stop / inspect the local Stata MCP server.

Cross-platform replacement for per-shell hook scripts: one standard-library
Python file works on macOS, Linux and Windows, so nothing here depends on bash,
PowerShell, an editor extension, or a particular MCP host.

Behaviour
---------
``python3 stata-mcp-start.py``              start detached, wait for health, exit 0
``python3 stata-mcp-start.py --foreground`` run in the foreground (logs to stderr)
``python3 stata-mcp-start.py --status``     exit 0 when healthy, 1 otherwise
``python3 stata-mcp-start.py --stop``       stop the process recorded in the state

Reads ``<state>/state.json`` (written by ``install.py``); no other file is read
except the server source it launches. Only the loopback port is contacted.
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


def health(host, port, timeout=2.0):
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
        info = health(host, port, timeout=2.0)
        if info:
            return info
        time.sleep(0.4)
    return None


def is_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid(home):
    try:
        with open(os.path.join(home, "run", "server.pid"), "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return 0


def ensure_private_dir(path):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def write_pid(home, pid):
    directory = os.path.join(home, "run")
    ensure_private_dir(directory)
    path = os.path.join(directory, "server.pid")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("%d\n" % pid)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def server_argv(state, host, port, log_file):
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
        str(port),
        "--log-file",
        log_file,
    ]
    edition = state.get("stata_edition")
    if edition in ("mp", "se", "be"):
        argv += ["--stata-edition", edition]
    return argv


def start(home, state, host, port, wait, foreground):
    info = health(host, port)
    if info:
        return 0
    log_file = state.get("log_file") or os.path.join(home, "logs", "server.log")
    argv = server_argv(state, host, port, log_file)

    if foreground:
        os.execv(argv[0], argv)

    console = os.path.join(home, "logs", "server.console.log")
    ensure_private_dir(os.path.dirname(console))
    handle = open(console, "ab")
    try:
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=handle, creationflags=flags
            )
        else:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
    finally:
        handle.close()
    write_pid(home, process.pid)
    return 0 if wait_healthy(host, port, wait) else 1


def stop(home, host, port):
    pid = read_pid(home)
    if not is_alive(pid):
        return 0 if not health(host, port) else 1
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + 10
    while time.time() < deadline and is_alive(pid):
        time.sleep(0.2)
    if is_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.unlink(os.path.join(home, "run", "server.pid"))
    except OSError:
        pass
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="stata-mcp-start",
        description="Start, stop or check the local Stata MCP server.",
    )
    parser.add_argument("--home", help="state directory (default: ~/.stata-mcp or STATA_MCP_HOME)")
    parser.add_argument("--host", help="host to bind/probe (default from state.json, else 127.0.0.1)")
    parser.add_argument("--port", help="port to bind/probe (default from state.json, else 4001)")
    parser.add_argument("--wait", type=float, default=120.0, help="seconds to wait for health (default: 120)")
    parser.add_argument("--foreground", action="store_true", help="run the server in this process")
    parser.add_argument("--status", action="store_true", help="report health and exit")
    parser.add_argument("--stop", action="store_true", help="stop the recorded server process")
    args = parser.parse_args(argv)

    home = resolve_home(args.home)
    state = load_state(home)
    host = args.host or os.environ.get("STATA_MCP_HOST") or state.get("host") or "127.0.0.1"
    try:
        port = int(args.port or os.environ.get("STATA_MCP_PORT") or state.get("port") or 4001)
    except (TypeError, ValueError):
        port = 4001

    if args.status:
        info = health(host, port)
        if info:
            sys.stdout.write(json.dumps(info, ensure_ascii=False) + "\n")
            return 0
        return 1
    if args.stop:
        return stop(home, host, port)
    return start(home, state, host, port, args.wait, args.foreground)


if __name__ == "__main__":
    sys.exit(main())
