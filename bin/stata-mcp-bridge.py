#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stdio <-> Streamable-HTTP bridge for a local Stata MCP server.

Why this exists
---------------
Most MCP clients speak JSON-RPC over stdin/stdout, while the Stata server speaks
Streamable HTTP on a local port. This bridge forwards newline-delimited JSON-RPC
messages between the two.

Design notes
------------
* **Standard library only**, Python 3.8+. No third-party package is required, so
  the bridge can run with any interpreter on the machine and does not pin the
  user to the server's virtual environment.
* **Proxy-immune**: it uses ``http.client`` directly, which never consults
  ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY``. Loopback traffic must not go
  through a proxy; that is the usual cause of ``502 Bad Gateway`` with agent
  hosts running behind a local proxy.
* **Self-contained**: defaults come from ``<state>/state.json`` written by the
  installer, then from environment variables, then from CLI flags. It never
  reads any other application's configuration.
* **stdout is protocol-only.** Diagnostics go to stderr and only when
  ``STATA_MCP_DEBUG`` is set.

Configuration
-------------
CLI:  ``--home DIR --host H --port N --no-autostart --autostart-timeout S``
Env:  ``STATA_MCP_HOME``, ``STATA_MCP_HOST``, ``STATA_MCP_PORT``,
      ``STATA_MCP_AUTOSTART`` (0/1), ``STATA_MCP_START_CMD``,
      ``STATA_MCP_TIMEOUT`` (seconds), ``STATA_MCP_DEBUG`` (0/1)
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4001
MCP_PATH = "/mcp-streamable"
HEALTH_PATH = "/health"
# Jobs are polled with short calls, so a long per-request timeout is only a
# safety net for synchronous runs (default job timeout is 6 hours).
DEFAULT_TIMEOUT = 21600.0
DEFAULT_AUTOSTART_TIMEOUT = 120.0


def debug(message):
    if os.environ.get("STATA_MCP_DEBUG", "").strip() in ("1", "true", "yes"):
        sys.stderr.write("stata-mcp-bridge: %s\n" % message)
        sys.stderr.flush()


def fail(message, code=1):
    sys.stderr.write("stata-mcp-bridge: %s\n" % message)
    sys.stderr.flush()
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
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


class Settings(object):
    def __init__(self, args):
        self.home = resolve_home(args.home)
        state = load_state(self.home)

        self.host = (
            args.host
            or os.environ.get("STATA_MCP_HOST")
            or state.get("host")
            or DEFAULT_HOST
        )
        port = (
            args.port
            or os.environ.get("STATA_MCP_PORT")
            or state.get("port")
            or DEFAULT_PORT
        )
        try:
            self.port = int(port)
        except (TypeError, ValueError):
            self.port = DEFAULT_PORT

        autostart_env = os.environ.get("STATA_MCP_AUTOSTART", "").strip().lower()
        autostart_default = autostart_env not in ("0", "false", "no", "off")
        self.autostart = autostart_default if args.autostart is None else args.autostart

        try:
            self.autostart_timeout = float(
                args.autostart_timeout or os.environ.get("STATA_MCP_AUTOSTART_TIMEOUT") or DEFAULT_AUTOSTART_TIMEOUT
            )
        except (TypeError, ValueError):
            self.autostart_timeout = DEFAULT_AUTOSTART_TIMEOUT

        try:
            self.timeout = float(args.timeout or os.environ.get("STATA_MCP_TIMEOUT") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT

        explicit_cmd = args.start_cmd or os.environ.get("STATA_MCP_START_CMD")
        self.start_argv = None if explicit_cmd else self._default_start_argv(state)
        self.start_cmd = explicit_cmd or (
            " ".join(_quote(part) for part in self.start_argv) if self.start_argv else ""
        )

    def _default_start_argv(self, state):
        launcher = state.get("launcher") or os.path.join(self.home, "bin", "stata-mcp-start.py")
        if os.path.isfile(launcher):
            return [sys.executable, launcher]
        return None

    def _default_start_cmd(self, state):
        argv = self._default_start_argv(state)
        if argv:
            return "%s %s" % (_quote(argv[0]), _quote(argv[1]))
        return ""

    def describe(self):
        return "home=%s endpoint=http://%s:%d%s autostart=%s" % (
            self.home,
            self.host,
            self.port,
            MCP_PATH,
            "on" if self.autostart else "off",
        )


def _quote(value):
    if os.name == "nt" and " " in value and not value.startswith('"'):
        return '"%s"' % value
    return value


# ---------------------------------------------------------------------------
# HTTP (never proxied)
# ---------------------------------------------------------------------------
def is_healthy(settings, timeout=2.0):
    try:
        connection = http.client.HTTPConnection(settings.host, settings.port, timeout=timeout)
        try:
            connection.request("GET", HEALTH_PATH, headers={"Accept": "application/json"})
            response = connection.getresponse()
            response.read()
            return response.status == 200
        finally:
            connection.close()
    except (OSError, http.client.HTTPException):
        return False


def _win_background_flags(breakaway=True):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    if breakaway:
        flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    return flags


def spawn_autostart(settings):
    argv = getattr(settings, "start_argv", None)
    command = argv if argv else settings.start_cmd
    if not command:
        debug("no start command available; cannot autostart")
        return None
    use_shell = not isinstance(command, (list, tuple))
    debug("autostart: %s" % (command if isinstance(command, str) else " ".join(command)))
    try:
        if os.name == "nt":
            try:
                return subprocess.Popen(
                    command,
                    shell=use_shell,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_win_background_flags(breakaway=True),
                    close_fds=True,
                )
            except OSError as exc:
                if getattr(exc, "winerror", None) != 5:
                    raise
                return subprocess.Popen(
                    command,
                    shell=use_shell,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_win_background_flags(breakaway=False),
                    close_fds=True,
                )
        return subprocess.Popen(
            command,
            shell=use_shell,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        debug("autostart failed: %s" % exc)
        return None


def ensure_server(settings):
    if is_healthy(settings, timeout=1.5):
        return True
    if not settings.autostart:
        return False
    proc = spawn_autostart(settings)
    if not proc:
        return False

    deadline = time.time() + settings.autostart_timeout
    exited_at = None
    while time.time() < deadline:
        if is_healthy(settings, timeout=1.5):
            debug("server became healthy")
            return True
        rc = proc.poll()
        if rc is not None:
            if exited_at is None:
                exited_at = time.time()
            elif time.time() - exited_at >= 1.5:
                debug("autostart launcher exited early with code %s" % rc)
                return False
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# JSON-RPC forwarding
# ---------------------------------------------------------------------------
def emit(payload):
    sys.stdout.write(payload.rstrip("\n") + "\n")
    sys.stdout.flush()


def is_final_response(payload, request_id):
    if request_id is None:
        return False
    try:
        data = json.loads(payload)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("id") != request_id:
        return False
    if "result" in data or "error" in data:
        # Guard against a partial object that merely contains the key name.
        return True
    return False


def forward(settings, line, session):
    """Send one JSON-RPC line to the server and emit whatever comes back."""
    try:
        request = json.loads(line)
    except ValueError as exc:
        fail("invalid JSON on stdin: %s" % exc)

    request_id = request.get("id")
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session.get("id"):
        headers["mcp-session-id"] = session["id"]
    if session.get("protocol"):
        headers["mcp-protocol-version"] = session["protocol"]

    connection = http.client.HTTPConnection(settings.host, settings.port, timeout=settings.timeout)
    try:
        connection.request("POST", MCP_PATH, body=line.encode("utf-8"), headers=headers)
        response = connection.getresponse()

        new_session = response.getheader("mcp-session-id")
        if new_session:
            session["id"] = new_session

        content_type = (response.getheader("content-type") or "").lower()
        if response.status >= 400:
            body = response.read().decode("utf-8", "replace").strip()
            raise RuntimeError("HTTP %d from server: %s" % (response.status, body[:400]))

        if "text/event-stream" in content_type:
            finished = False
            while True:
                raw = response.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if text.startswith("data:"):
                    payload = text[5:].strip()
                    if not payload:
                        continue
                    emit(payload)
                    if is_final_response(payload, request_id):
                        finished = True
                    elif request_id is None:
                        finished = True
                elif text == "":
                    if finished:
                        break
            return

        body = response.read().decode("utf-8", "replace").strip()
        if body:
            if body.startswith("data:"):
                for chunk in body.splitlines():
                    if chunk.startswith("data:"):
                        payload = chunk[5:].strip()
                        if payload:
                            emit(payload)
            else:
                emit(body)
    except (OSError, http.client.HTTPException, RuntimeError) as exc:
        debug("forwarding error: %s" % exc)
        if request_id is not None:
            emit(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": "stata-mcp bridge: %s" % exc},
                    }
                )
            )
    finally:
        try:
            connection.close()
        except Exception:
            pass


def run(settings):
    if not ensure_server(settings):
        fail(
            "no Stata MCP server is answering on http://%s:%d%s and autostart %s.\n"
            "  Start it with: python3 %s/bin/stata-mcp-start.py\n"
            "  Or disable autostart with --no-autostart / STATA_MCP_AUTOSTART=0."
            % (
                settings.host,
                settings.port,
                HEALTH_PATH,
                "was disabled" if not settings.autostart else "failed",
                settings.home,
            )
        )

    debug("connected (%s)" % settings.describe())
    session = {}
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        forward(settings, line, session)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="stata-mcp-bridge",
        description="stdio <-> Streamable HTTP bridge for a local Stata MCP server.",
    )
    parser.add_argument("--home", help="state directory (default: ~/.stata-mcp or STATA_MCP_HOME)")
    parser.add_argument("--host", help="server host (default from state.json, else 127.0.0.1)")
    parser.add_argument("--port", help="server port (default from state.json, else 4001)")
    parser.add_argument(
        "--autostart",
        dest="autostart",
        action="store_true",
        default=None,
        help="start the server when it is not running (default: on)",
    )
    parser.add_argument(
        "--no-autostart",
        dest="autostart",
        action="store_false",
        help="never start the server from the bridge",
    )
    parser.add_argument("--autostart-timeout", help="seconds to wait for autostart (default: 120)")
    parser.add_argument("--start-cmd", help="command used to start the server during autostart")
    parser.add_argument("--timeout", help="per-request timeout in seconds (default: 21600)")
    return parser.parse_args(argv)


def _configure_stdio():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv=None):
    _configure_stdio()
    args = parse_args(argv)
    settings = Settings(args)
    debug("starting (%s)" % settings.describe())
    run(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
