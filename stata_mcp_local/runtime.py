"""Runtime management: Python environment, process control, health checks.

Everything here is standard library only, and network probes use
``http.client`` directly. That deliberately bypasses the ambient
``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY`` environment variables: localhost
traffic must never be routed through a user's proxy, which is the classic cause
of ``502 Bad Gateway`` loops when an agent host runs behind a local proxy.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths

MIN_PYTHON = (3, 9)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4001
DEFAULT_ENGINE_PORT = 4001
DEFAULT_JOB_TIMEOUT = 21600.0  # 6 hours
DEFAULT_SYNC_WAIT = 20.0
MCP_PATH = "/mcp-streamable"
HEALTH_PATH = "/health"


class RuntimeError_(RuntimeError):
    """Raised for recoverable runtime problems with actionable messages."""


# ---------------------------------------------------------------------------
# HTTP helpers (proxy-free by construction)
# ---------------------------------------------------------------------------
def http_json(
    host: str,
    port: int,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 3.0,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request_headers.setdefault("Accept", "application/json")
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET" if body is None else "POST", path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        if response.status != 200:
            return None
        return json.loads(raw.decode("utf-8", "replace"))
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        try:
            connection.close()
        except Exception:
            pass


def health(host: str, port: int, timeout: float = 1.5) -> Optional[Dict[str, Any]]:
    return http_json(host, port, HEALTH_PATH, timeout=timeout)


def wait_healthy(host: str, port: int, timeout: float = 60.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if health(host, port, timeout=min(1.5, max(0.5, deadline - time.time()))):
            return True
        time.sleep(interval)
    return False


def _win_no_window_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------
@dataclass
class PythonChoice:
    executable: str
    version: Tuple[int, int]
    builder: str  # "uv" | "venv"
    uv: Optional[str] = None  # path to the ``uv`` binary when available
    uv_version: str = ""

    def describe(self) -> str:
        text = "%s (Python %d.%d via %s)" % (self.executable, self.version[0], self.version[1], self.builder)
        if self.uv:
            text += " with %s" % (self.uv_version or "uv")
        return text


def _python_version(executable: str) -> Optional[Tuple[int, int]]:
    try:
        result = subprocess.run(
            [
                executable,
                "-c",
                "import sys;print('%d.%d' % sys.version_info[:2])",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            **_win_no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        major, minor = result.stdout.strip().split(".")
        return int(major), int(minor)
    except ValueError:
        return None


def _find_uv() -> Tuple[Optional[str], str]:
    """Return ``(path, version string)`` for uv, or ``(None, "")``."""
    exe = shutil.which("uv")
    if not exe:
        return None, ""
    try:
        result = subprocess.run(
            [exe, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            **_win_no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return exe, ""
    version = result.stdout.strip() if result.returncode == 0 else ""
    return exe, version


def detect_python(explicit: Optional[str] = None, prefer_uv: bool = True) -> PythonChoice:
    """Pick an interpreter that can host the server (>=3.9)."""
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    env_python = os.environ.get("STATA_MCP_PYTHON", "").strip()
    if env_python:
        candidates.append(env_python)
    candidates.append(sys.executable)
    for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3.9", "python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    seen = set()
    for exe in candidates:
        if not exe or exe in seen:
            continue
        seen.add(exe)
        version = _python_version(exe)
        if version and version >= MIN_PYTHON:
            uv, uv_version = _find_uv() if prefer_uv else (None, "")
            return PythonChoice(exe, version, "uv" if uv else "venv", uv, uv_version)

    raise RuntimeError_(
        "no Python >= %d.%d found. Install one (for example via your package "
        "manager or https://www.python.org/downloads/) and re-run with --python <path>."
        % MIN_PYTHON
    )


def venv_python_path(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def create_venv(venv_dir: Path, choice: PythonChoice) -> Path:
    """Create the virtual environment; returns the interpreter path."""
    target = venv_python_path(venv_dir)
    if target.is_file():
        return target

    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    if choice.uv:
        cmd = [choice.uv, "venv", str(venv_dir), "--python", choice.executable]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_win_no_window_kwargs(),
        )
        if result.returncode != 0 or not target.is_file():
            # fall back to the stdlib builder below
            pass
    if not target.is_file():
        cmd = [choice.executable, "-m", "venv", str(venv_dir)]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **_win_no_window_kwargs(),
            )
        except OSError as exc:
            raise RuntimeError_("could not create virtual environment: %s" % exc)
        if result.returncode != 0:
            raise RuntimeError_(
                "could not create virtual environment with %s:\n%s"
                % (choice.executable, (result.stdout or "").strip()[-800:])
            )
    if not target.is_file():
        raise RuntimeError_("virtual environment created but interpreter missing: %s" % target)
    return target


def install_dependencies(
    venv_python: Path,
    requirements: Path,
    choice: PythonChoice,
    index_url: Optional[str] = None,
) -> str:
    """Install the server's requirements into the venv.

    Notes on privacy: only the packages listed by the upstream project are
    installed, into this project's own venv. When `index_url` is omitted, the
    user's normal package index settings apply with no override or credentials.
    """
    mirror = (index_url or "").strip()
    extra_index = ["--index-url", mirror] if (mirror and choice.uv) else (["-i", mirror] if mirror else [])
    if choice.uv:
        cmd = [choice.uv, "pip", "install", "--python", str(venv_python), *extra_index, "-r", str(requirements)]
    else:
        # Make sure pip itself exists in the fresh environment, then install.
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--upgrade", *extra_index, "pip"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_win_no_window_kwargs(),
        )
        cmd = [str(venv_python), "-m", "pip", "install", *extra_index, "-r", str(requirements)]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_win_no_window_kwargs(),
    )
    if result.returncode != 0:
        tail = "\n".join((result.stdout or "").strip().splitlines()[-12:])
        raise RuntimeError_("dependency installation failed:\n%s" % tail)
    return (result.stdout or "").strip()


# ---------------------------------------------------------------------------
# Process control
# ---------------------------------------------------------------------------
def server_argv(
    python: str,
    entry: Path,
    stata_path: str,
    stata_edition: str,
    host: str,
    port: int,
    log_file: Path,
    home: Optional[Path] = None,
) -> List[str]:
    """Argv for the Stata MCP server (unified single-port when ``home`` is given)."""
    argv = [
        python,
        str(entry),
        "--stata-path",
        stata_path,
        "--host",
        host,
        "--port",
        str(port),
        "--log-file",
        str(log_file),
    ]
    if stata_edition in ("mp", "se", "be"):
        argv += ["--stata-edition", stata_edition]
    if home is not None:
        argv += ["--gateway-home", str(home)]
    return argv


def gateway_argv(
    layout: paths.Layout,
    python: str,
    host: str,
    port: int,
) -> List[str]:
    """Argv for the local gateway (client-facing port)."""
    return [
        python,
        str(layout.gateway_file),
        "--home",
        str(layout.home),
        "--host",
        host,
        "--port",
        str(port),
        "--log-file",
        str(layout.gateway_log_file),
    ]


def port_free(host: str, port: int) -> bool:
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # On Windows SO_REUSEADDR allows binding a port already held by another listener.
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except OSError:
            pass


def pick_engine_port(host: str, gateway_port: int, preferred: Optional[int] = None) -> int:
    candidate = int(preferred or (gateway_port + 1))
    for offset in range(0, 40):
        port = candidate + offset
        if port == gateway_port:
            continue
        if port_free(host, port):
            return port
    raise RuntimeError_(
        "no free port for the Stata engine near %d; pass a different --port" % candidate
    )


def _is_alive_win32(pid: int) -> Optional[bool]:
    """Query Win32 process exit code directly without spawning tasklist."""
    try:
        import ctypes
        import ctypes.wintypes as wt

        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            err = k32.GetLastError()
            if err == 5:  # ERROR_ACCESS_DENIED: process exists
                return True
            if err == 87:  # ERROR_INVALID_PARAMETER: no such process
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


def is_alive(pid: int) -> bool:
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


def read_pid(layout: paths.Layout) -> Optional[int]:
    try:
        return int(layout.pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def read_gateway_pid(layout: paths.Layout) -> Optional[int]:
    try:
        return int(layout.gateway_pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _win_background_flags(breakaway: bool = True) -> int:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    if breakaway:
        flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    return flags


def _spawn(argv: List[str], console_log: Path) -> int:
    paths.ensure_private_dir(console_log.parent)
    handle = open(str(console_log), "ab")
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
    return process.pid


def _is_unified_state(state: Dict[str, Any]) -> bool:
    mode = (state.get("mode") or "").strip().lower()
    if mode == "unified":
        return True
    try:
        p = int(state.get("port", DEFAULT_PORT))
        ep = int(state.get("engine_port", p))
        return p == ep
    except (TypeError, ValueError):
        return False


def _engine_port(state: Dict[str, Any]) -> int:
    if _is_unified_state(state):
        try:
            return int(state.get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            return DEFAULT_PORT
    try:
        return int(state.get("engine_port") or (int(state.get("port", DEFAULT_PORT)) + 1))
    except (TypeError, ValueError):
        return DEFAULT_ENGINE_PORT


def _validate_engine_inputs(layout: paths.Layout, state: Dict[str, Any]) -> Tuple[str, str, str]:
    entry = state.get("server_entry")
    python = state.get("python")
    stata_path = state.get("stata_path")
    if not entry or not Path(entry).is_file():
        raise RuntimeError_("server entry missing (%s); re-run install" % entry)
    if not python or not Path(python).is_file():
        raise RuntimeError_("venv interpreter missing (%s); re-run install" % python)
    if not stata_path:
        raise RuntimeError_("no Stata installation recorded; re-run install with --stata-path")
    if not layout.gateway_file.is_file():
        raise RuntimeError_("gateway script missing (%s); re-run install" % layout.gateway_file)
    return entry, python, stata_path


def _start_engine(
    layout: paths.Layout,
    state: Dict[str, Any],
    host: str,
    engine_port: int,
    wait: float,
    entry: str,
    python: str,
    stata_path: str,
    unified: bool = False,
) -> int:
    if health(host, engine_port):
        return read_pid(layout) or 0
    log_file = Path(state.get("log_file") or layout.log_file)
    argv = server_argv(
        python,
        Path(entry),
        stata_path,
        state.get("stata_edition", "mp"),
        host,
        engine_port,
        log_file,
        home=layout.home if unified else None,
    )
    engine_pid = _spawn(argv, layout.log_dir / "server.console.log")
    paths.write_text_atomic(layout.pid_file, "%d\n" % engine_pid, mode=0o600)
    if not wait_healthy(host, engine_port, timeout=wait):
        detail = _log_tail(log_file)
        raise RuntimeError_(
            "server did not become healthy on %s:%d within %.0fs%s"
            % (host, engine_port, wait, ("\n" + detail) if detail else "")
        )
    return engine_pid


def start(layout: paths.Layout, state: Dict[str, Any], wait: float = 90.0) -> Tuple[bool, str]:
    layout.ensure()
    host = state.get("host", DEFAULT_HOST)
    port = int(state.get("port", DEFAULT_PORT))
    unified = _is_unified_state(state)
    engine_port = port if unified else _engine_port(state)

    if health(host, port):
        pid = read_pid(layout) if unified else read_gateway_pid(layout)
        if not pid and not unified:
            pid = read_pid(layout)
        if pid and is_alive(pid):
            if unified or health(host, engine_port):
                return True, "already running on %s:%d (pid %d)" % (host, port, pid)
            entry, python, stata_path = _validate_engine_inputs(layout, state)
            engine_pid = _start_engine(layout, state, host, engine_port, wait, entry, python, stata_path)
            return True, "gateway still on %s:%d (pid %d); engine restarted on :%d (pid %s)" % (
                host,
                port,
                pid,
                engine_port,
                engine_pid,
            )
        raise RuntimeError_(
            "another MCP service already answers on %s:%d and it was not started by "
            "this installation (no pid file at %s).\n"
            "      Use a free port (`install --port <N>`) or stop that service first."
            % (host, port, layout.pid_file if unified else layout.gateway_pid_file)
        )

    entry, python, stata_path = _validate_engine_inputs(layout, state)
    log_file = Path(state.get("log_file") or layout.log_file)

    if unified:
        # Clean up any stale legacy gateway pid file from pre-1.4.0 installs.
        if layout.gateway_pid_file.is_file():
            try:
                layout.gateway_pid_file.unlink()
            except OSError:
                pass
        pid = _start_engine(layout, state, host, port, wait, entry, python, stata_path, unified=True)
        for path in (log_file, layout.log_dir / "server.console.log"):
            try:
                os.chmod(str(path), 0o600)
            except OSError:
                pass
        return True, "unified server on %s:%d (pid %s)" % (host, port, pid)

    # Legacy dual-process fallback
    engine_pid = _start_engine(layout, state, host, engine_port, wait, entry, python, stata_path, unified=False)
    try:
        gateway_pid = _spawn(gateway_argv(layout, python, host, port), layout.log_dir / "gateway.console.log")
    except OSError as exc:
        raise RuntimeError_("could not start the gateway: %s" % exc)
    paths.write_text_atomic(layout.gateway_pid_file, "%d\n" % gateway_pid, mode=0o600)

    if wait_healthy(host, port, timeout=wait):
        for path in (log_file, layout.log_dir / "server.console.log", layout.gateway_log_file):
            try:
                os.chmod(str(path), 0o600)
            except OSError:
                pass
        return True, "gateway on %s:%d (pid %s), engine on :%d (pid %s)" % (
            host,
            port,
            gateway_pid,
            engine_port,
            engine_pid,
        )

    detail = _log_tail(layout.gateway_log_file)
    if not detail:
        detail = _log_tail(layout.log_dir / "gateway.console.log")
    raise RuntimeError_(
        "gateway did not become healthy on %s:%d within %.0fs%s"
        % (host, port, wait, ("\n" + detail) if detail else "")
    )


def _log_tail(log_file: Path, lines: int = 12) -> str:
    candidates = (log_file, log_file.parent / "server.console.log", log_file.parent / "gateway.console.log")
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks = text.strip().splitlines()[-lines:]
        if chunks:
            return "\n".join("      " + line for line in chunks)
    return ""


def _stop_pid(pid: Optional[int], pid_file: Optional[Path], timeout: float) -> bool:
    if pid is None or not is_alive(pid):
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

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_alive(pid):
            break
        time.sleep(0.2)
    else:
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

    if pid_file is not None:
        try:
            pid_file.unlink()
        except OSError:
            pass
    return True


def _health_gateway_pid(info: Optional[Dict[str, Any]]) -> Optional[int]:
    if not isinstance(info, dict):
        return None
    gw = info.get("gateway")
    if isinstance(gw, dict):
        try:
            val = int(gw.get("pid") or 0)
            return val if val > 0 else None
        except (TypeError, ValueError):
            return None
    return None


def stop(layout: paths.Layout, state: Dict[str, Any], timeout: float = 10.0) -> str:
    host = state.get("host", DEFAULT_HOST)
    port = int(state.get("port", DEFAULT_PORT))
    engine_port = _engine_port(state)

    stopped_gateway = _stop_pid(read_gateway_pid(layout), layout.gateway_pid_file, timeout)
    if not stopped_gateway:
        gw_pid = _health_gateway_pid(health(host, port, timeout=1.0))
        if gw_pid:
            stopped_gateway = _stop_pid(gw_pid, layout.gateway_pid_file, timeout)

    stopped_engine = _stop_pid(read_pid(layout), layout.pid_file, timeout)

    if not stopped_gateway and not stopped_engine:
        if health(host, port, timeout=1.0) or health(host, engine_port, timeout=1.0):
            return (
                "a server is answering on %s:%d but was not started by this "
                "installation (no pid file); stop it with your process manager"
                % (host, port)
            )
        return "not running"
    parts = []
    if stopped_gateway:
        parts.append("gateway")
    if stopped_engine:
        parts.append("engine")
    return "stopped (%s)" % ", ".join(parts)


def status(layout: paths.Layout, state: Dict[str, Any]) -> Dict[str, Any]:
    host = state.get("host", DEFAULT_HOST)
    port = int(state.get("port", DEFAULT_PORT))
    unified = _is_unified_state(state)
    engine_port = port if unified else _engine_port(state)
    info = health(host, port, timeout=1.0) or {}
    is_unified_live = unified or info.get("mode") == "unified"
    engine = info if is_unified_live else (health(host, engine_port, timeout=1.0) or {})

    server_pid = read_pid(layout)
    gw_pid = read_gateway_pid(layout)
    pid = server_pid if is_unified_live else gw_pid
    if not (pid and is_alive(pid)):
        pid = _health_gateway_pid(info)
    engine_pid = server_pid if (server_pid and is_alive(server_pid)) else pid

    return {
        "healthy": bool(info),
        "mode": "unified" if is_unified_live else "dual",
        "health": info,
        "pid": pid if (pid and is_alive(pid)) else None,
        "host": host,
        "port": port,
        "engine": {
            "healthy": bool(engine),
            "port": port if is_unified_live else engine_port,
            "pid": engine_pid if (engine_pid and is_alive(engine_pid)) else None,
            "unified": bool(is_unified_live),
            "health": engine,
        },
    }


def write_helper_scripts(layout: paths.Layout, repo_root: Path) -> List[Path]:
    """Copy the helper scripts into the state directory's bin/."""
    written: List[Path] = []
    for name in ("stata-mcp-bridge.py", "stata-mcp-start.py", "stata-mcp-jobs.py", "stata_mcp_analysis.py"):
        src = repo_root / "bin" / name
        if not src.is_file():
            raise RuntimeError_("helper script missing from package: %s" % src)
        dest = layout.bin_dir / name
        text = src.read_text(encoding="utf-8")
        paths.write_text_atomic(dest, text, mode=0o700)
        written.append(dest)
    return written


def write_launchers(layout: paths.Layout, python: str) -> List[Path]:
    """Create thin exec shims so MCP clients can use one stable command path."""
    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    bridge = layout.bridge_file

    if os.name == "nt":
        cmd = layout.bin_dir / "stata-mcp.cmd"
        paths.write_text_atomic(
            cmd,
            '@echo off\r\n"%s" "%s" %%*\r\n' % (python, bridge),
            mode=0o700,
        )
        written.append(cmd)
    posix = layout.bin_dir / "stata-mcp"
    paths.write_text_atomic(
        posix,
        '#!/bin/sh\n# Generated shim: stdio <-> Streamable HTTP bridge for a local Stata MCP server.\nexec "%s" "%s" "$@"\n'
        % (python, bridge),
        mode=0o755,
    )
    try:
        os.chmod(str(posix), 0o755)
    except OSError:
        pass
    written.append(posix)
    return written
