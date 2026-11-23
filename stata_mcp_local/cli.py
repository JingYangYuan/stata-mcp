"""Command line interface.

Sub-commands
------------
``install``   fetch source (optional), build a private venv, record state, start
``config``    print / emit / merge an MCP client snippet
``start``     start the server in the background (idempotent)
``stop``      stop the server started by this installation
``status``    health + installation summary
``doctor``    environment diagnosis (no secret values are printed)
``verify``    end-to-end smoke test through the stdio bridge
``privacy``   print the data-flow / file-touch disclosure
``uninstall`` remove the runtime (optionally purge the state directory)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, config_emit, paths, runtime, source, stata, ui

PROG = "install.py"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _layout(args: argparse.Namespace) -> paths.Layout:
    return paths.Layout(getattr(args, "home", None))


def _state_summary(state: Dict[str, Any]) -> List[str]:
    if not state:
        return ["no installation recorded in this directory"]
    order = [
        ("home", "state directory"),
        ("server_dir", "server source"),
        ("python", "venv interpreter"),
        ("stata_path", "Stata installation"),
        ("stata_edition", "Stata edition"),
        ("host", "bind host"),
        ("port", "gateway port"),
        ("engine_port", "engine port"),
        ("default_timeout", "job timeout (s)"),
        ("sync_wait", "sync wait (s)"),
        ("commit", "source commit"),
        ("installed_at", "installed at"),
    ]
    lines = []
    for key, label in order:
        if state.get(key) not in (None, ""):
            lines.append("%-18s %s" % (label + ":", state[key]))
    return lines


def _record_install(
    layout: paths.Layout,
    build: "source.Source",
    python: str,
    choice: runtime.PythonChoice,
    cand: stata.Candidate,
    host: str,
    port: int,
    repo: str,
    engine_port: int,
    default_timeout: float,
    sync_wait: float,
) -> Dict[str, Any]:
    state = {
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "installer_version": __version__,
        "server_dir": str(build.root),
        "server_entry": str(build.entry),
        "requirements": str(build.requirements) if build.requirements else "",
        "source_origin": build.origin,
        "commit": build.commit,
        "repo": repo,
        "python": str(python),
        "python_choice": choice.describe(),
        "venv": str(layout.venv_dir),
        "stata_path": str(cand.path),
        "stata_edition": cand.edition,
        "stata_version": cand.version,
        "host": host,
        "port": port,
        "engine_port": engine_port,
        "default_timeout": default_timeout,
        "sync_wait": sync_wait,
        "log_file": str(layout.log_file),
        "gateway_log_file": str(layout.gateway_log_file),
        "bridge": str(layout.bridge_file),
        "launcher": str(layout.launcher_file),
        "jobs_file": str(layout.gateway_file),
        "jobs_dir": str(layout.jobs_dir),
        "jobs_pid_file": str(layout.gateway_pid_file),
    }
    layout.save_state(state)
    return state


def _load_state_or_fail(layout: paths.Layout) -> Dict[str, Any]:
    state = layout.load_state()
    if not state:
        raise runtime.RuntimeError_(
            "no installation found under %s. Run `python3 %s install` first "
            "(or pass --home if you installed elsewhere)." % (layout.home, PROG)
        )
    return state


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_install(args: argparse.Namespace) -> int:
    layout = _layout(args)
    host = args.host
    port = int(args.port)
    plan_only = bool(args.print_plan)

    ui.step("Pre-flight")
    target = stata.best(args.stata_path)
    if args.stata_path and not target:
        ui.error("--stata-path %s does not look like a Stata installation" % args.stata_path)
        ui.hint("expected e.g. a directory containing StataMP.app, stata-mp, or Stata*-64.exe")
        return 2
    if target:
        ui.ok("Stata: %s" % stata.describe(target))
    else:
        ui.warn("no Stata installation detected")
        ui.hint("install Stata 17+ (or newer) and re-run, or pass --stata-path <dir>")
        if not plan_only:
            return 2

    choice = runtime.detect_python(args.python, prefer_uv=True)
    ui.ok("Python: %s" % choice.describe())
    if not source.git_available() and not args.server_dir:
        ui.warn("git is not on PATH; a source checkout must be supplied with --server-dir")

    ui.step("Plan")
    ui.info("state directory : %s" % layout.home)
    ui.info("server source   : %s" % (args.server_dir or source.mirror_url(args.repo, args.mirror)))
    ui.info("venv            : %s" % (args.venv or layout.venv_dir))
    ui.info("endpoint        : http://%s:%d%s (gateway)" % (host, port, runtime.MCP_PATH))
    ui.info("engine endpoint : http://%s:%d (internal, upstream server)" % (host, port + 1))
    ui.info("job timeout     : %.0fs (%.1fh), sync wait %.0fs" % (args.default_timeout, args.default_timeout / 3600.0, args.sync_wait))
    ui.info("config files    : none modified (snippet is printed; use `config --write-config PATH`)")
    if plan_only:
        ui.info("")
        ui.info("Dry run: nothing was created, downloaded, started or written.")
        return 0

    if not args.yes and not _confirm("Proceed? [Y/n] "):
        ui.warn("aborted")
        return 1

    layout.ensure()

    previous = layout.load_state()
    if runtime.read_pid(layout) or runtime.read_gateway_pid(layout):
        ui.step("Stopping the previous runtime")
        ui.ok(runtime.stop(layout, previous))

    ui.step("Installing helper scripts")
    written = runtime.write_helper_scripts(layout, Path(__file__).resolve().parent.parent)
    for path in written:
        ui.ok(str(path))

    ui.step("Locating server source")
    if args.server_dir:
        build = source.require_local(args.server_dir)
        ui.ok("using local checkout: %s" % build.describe())
    else:
        build = source.acquire(
            server_dir=None,
            dest=layout.server_dir,
            repo=args.repo,
            mirror=args.mirror,
            ref=args.ref,
            refresh=args.refresh,
        )
        ui.ok("source: %s" % build.describe())
    ui.info("entry: %s" % build.entry)

    ui.step("Preparing Python environment")
    if args.venv:
        python = runtime.venv_python_path(Path(args.venv).expanduser())
        if not python.is_file():
            python = Path(args.venv).expanduser()
        if not Path(str(python)).is_file():
            ui.error("--venv %s does not contain a usable interpreter" % args.venv)
            return 2
        ui.ok("reusing existing environment: %s" % python)
        if not args.skip_deps:
            ui.warn("dependencies are not installed automatically when reusing --venv")
    else:
        python = runtime.create_venv(layout.venv_dir, choice)
        ui.ok("venv interpreter: %s" % python)
        if build.requirements and not args.skip_deps:
            ui.info("installing %s" % build.requirements)
            output = runtime.install_dependencies(python, build.requirements, choice)
            tail = output.strip().splitlines()[-1:] or ["dependencies installed"]
            ui.ok(tail[0])
        elif not build.requirements:
            ui.warn("no requirements file in source tree; skipping dependency install")

    ui.step("Recording installation")
    engine_port = runtime.pick_engine_port(host, port)
    state = _record_install(
        layout,
        build,
        str(python),
        choice,
        target,
        host,
        port,
        args.repo,
        engine_port,
        float(args.default_timeout),
        float(args.sync_wait),
    )
    ui.ok("state written to %s" % layout.state_file)
    ui.info("client-facing port %d, engine port %d" % (port, engine_port))

    ui.step("Creating launchers")
    for path in runtime.write_launchers(layout, str(python)):
        ui.ok(str(path))

    if args.no_start:
        ui.warn("server not started (--no-start)")
    else:
        ui.step("Starting server")
        try:
            started, message = runtime.start(layout, state, wait=float(args.wait))
        except runtime.RuntimeError_ as exc:
            ui.error(str(exc))
            ui.hint("see %s" % (layout.log_dir / "server.console.log"))
            return 2
        ui.ok(message)
        info = runtime.health(host, port) or {}
        if info:
            ui.ok("health: %s" % json.dumps(info, ensure_ascii=False))

    entry = config_emit.server_entry(
        "stdio", str(python), layout.bridge_file, host, port, layout.home
    )
    print()
    print("MCP client snippet (stdio transport, no configuration file was modified):")
    print()
    sys.stdout.write(config_emit.render(config_emit.snippet(entry)))
    print()
    ui.hint("write it yourself, or run: python3 %s config --write-config <path> --transport stdio" % PROG)
    ui.hint("run `python3 %s verify` for an end-to-end smoke test" % PROG)
    ui.done("Installation complete")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    layout = _layout(args)
    state = layout.load_state()
    python = (state.get("python") or args.python)
    if not python:
        python = sys.executable
    transport = args.transport
    host = state.get("host", args.host)
    port = int(state.get("port", args.port))
    entry = config_emit.server_entry(
        transport,
        str(python),
        layout.bridge_file,
        host,
        port,
        layout.home,
        timeout_ms=args.timeout_ms,
    )

    shown = config_emit.render(config_emit.snippet(entry, root=args.root, key=args.key))

    if args.emit:
        target = Path(args.emit).expanduser()
        paths.write_text_atomic(target, shown, mode=0o600)
        ui.ok("snippet written to %s" % target)
    elif not args.write_config:
        sys.stdout.write(shown)

    if not layout.bridge_file.is_file():
        ui.warn("bridge script not found at %s" % layout.bridge_file)
        ui.hint("run `python3 %s install` (or pass --home) so the snippet points at a real file" % PROG)

    if args.write_config:
        target = Path(args.write_config).expanduser()
        try:
            status, backup = config_emit.write_config(
                target, args.root, args.key, entry, dry_run=args.dry_run
            )
        except ValueError as exc:
            ui.error(str(exc))
            return 2
        if status == "unchanged":
            ui.info("%s already contains an identical %r entry; nothing written" % (target, args.key))
        elif args.dry_run:
            ui.info("dry run: would %s %s (no file touched)" % (status, target))
        else:
            ui.ok("%s %s" % (status, target))
            if backup:
                ui.info("backup: %s" % backup)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    layout = _layout(args)
    state = _load_state_or_fail(layout)
    if args.port:
        state["port"] = int(args.port)
        if not state.get("engine_port") or int(state["engine_port"]) == int(args.port):
            state["engine_port"] = runtime.pick_engine_port(state.get("host", runtime.DEFAULT_HOST), int(args.port))
    try:
        _started, message = runtime.start(layout, state, wait=float(args.wait))
    except runtime.RuntimeError_ as exc:
        ui.error(str(exc))
        return 2
    layout.save_state(state)
    ui.ok(message)
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    """Re-stage the gateway/bridge/launcher and restart, without cloning or pip."""
    layout = _layout(args)
    state = _load_state_or_fail(layout)
    host = state.get("host", runtime.DEFAULT_HOST)
    port = int(args.port or state.get("port") or runtime.DEFAULT_PORT)

    ui.step("Stopping the running runtime")
    ui.ok(runtime.stop(layout, state))

    ui.step("Re-staging helper scripts")
    for path in runtime.write_helper_scripts(layout, Path(__file__).resolve().parent.parent):
        ui.ok(str(path))

    ui.step("Updating the installation record")
    engine_port = runtime.pick_engine_port(host, port, state.get("engine_port"))
    state.update(
        {
            "installer_version": __version__,
            "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "port": port,
            "engine_port": engine_port,
            "log_file": str(layout.log_file),
            "gateway_log_file": str(layout.gateway_log_file),
            "bridge": str(layout.bridge_file),
            "launcher": str(layout.launcher_file),
            "jobs_file": str(layout.gateway_file),
            "jobs_dir": str(layout.jobs_dir),
            "jobs_pid_file": str(layout.gateway_pid_file),
            "default_timeout": float(args.default_timeout or state.get("default_timeout") or runtime.DEFAULT_JOB_TIMEOUT),
            "sync_wait": float(args.sync_wait or state.get("sync_wait") or runtime.DEFAULT_SYNC_WAIT),
        }
    )
    layout.save_state(state)
    ui.ok("client-facing port %d, engine port %d" % (port, engine_port))

    ui.step("Creating launchers")
    python = state.get("python")
    if not python:
        ui.error("no interpreter recorded; run `install` instead")
        return 2
    for path in runtime.write_launchers(layout, str(python)):
        ui.ok(str(path))

    ui.step("Starting the runtime")
    try:
        _started, message = runtime.start(layout, state, wait=float(args.wait))
    except runtime.RuntimeError_ as exc:
        ui.error(str(exc))
        ui.hint("see %s" % layout.gateway_log_file)
        return 2
    ui.ok(message)
    ui.info("job timeout %.0fs, sync wait %.0fs" % (state["default_timeout"], state["sync_wait"]))
    ui.hint("run `python3 %s verify` for an end-to-end smoke test" % PROG)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    layout = _layout(args)
    state = layout.load_state()
    ui.ok(runtime.stop(layout, state))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    layout = _layout(args)
    state = layout.load_state()
    ui.step("Installation")
    for line in _state_summary(state):
        ui.info(line)
    info = runtime.status(layout, state)
    ui.step("Gateway (client-facing)")
    if info["healthy"]:
        ui.ok(
            "healthy on %s:%d%s"
            % (info["host"], info["port"], " (pid %s)" % info["pid"] if info["pid"] else "")
        )
        ui.info(json.dumps(info["health"], ensure_ascii=False))
    else:
        ui.warn("not healthy on %s:%d" % (info["host"], info["port"]))
        if info["pid"]:
            ui.info("process %s is alive but not answering yet" % info["pid"])
    engine = info.get("engine", {})
    ui.step("Engine (internal)")
    if engine.get("healthy"):
        ui.ok("healthy on %s:%d%s" % (info["host"], engine["port"], " (pid %s)" % engine["pid"] if engine.get("pid") else ""))
    else:
        ui.warn("not healthy on %s:%d" % (info["host"], engine.get("port")))
    if not info["healthy"]:
        ui.hint("start it with `python3 %s start`" % PROG)
        return 1
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    layout = _layout(args)
    state = layout.load_state()
    problems = 0

    ui.step("Platform")
    ui.info("os          : %s" % sys.platform)
    ui.info("architecture: %s" % (os.uname().machine if hasattr(os, "uname") else os.environ.get("PROCESSOR_ARCHITECTURE", "unknown")))
    ui.info("python      : %s (%d.%d.%d)" % (sys.executable, *sys.version_info[:3]))

    ui.step("Tools")
    for tool in ("git", "uv"):
        found = shutil.which(tool)
        if found:
            ui.ok("%s: %s" % (tool, found))
        else:
            ui.info("%s: not found (optional)" % tool)

    ui.step("Stata candidates (read-only directory inspection)")
    cands = stata.discover(args.stata_path)
    if not cands:
        problems += 1
        ui.error("no Stata installation found")
        ui.hint("pass --stata-path <installation root>")
    for cand in cands[:8]:
        mark = "OK" if stata.looks_like_stata(cand.path) else "??"
        ui.info("[%s] %s" % (mark, stata.describe(cand)))

    ui.step("State directory")
    ui.info(str(layout.home))
    if layout.exists():
        ui.ok("state.json present")
        for line in _state_summary(state):
            ui.info("  " + line)
    else:
        problems += 1
        ui.warn("not installed in this directory")

    ui.step("Runtime")
    info = runtime.status(layout, state)
    if info["healthy"]:
        ui.ok("gateway healthy on %s:%d" % (info["host"], info["port"]))
        ui.info(json.dumps(info["health"], ensure_ascii=False))
    else:
        problems += 1
        ui.warn("no healthy gateway on %s:%d" % (info["host"], info["port"]))
        tail = runtime._log_tail(Path(state.get("gateway_log_file") or layout.gateway_log_file))
        if tail:
            ui.info("last log lines:")
            ui.info(tail)

    engine = info.get("engine", {})
    if engine.get("healthy"):
        ui.ok("engine healthy on %s:%d" % (info["host"], engine["port"]))
    else:
        problems += 1
        ui.warn("no healthy engine on %s:%d" % (info["host"], engine.get("port")))
        tail = runtime._log_tail(Path(state.get("log_file") or layout.log_file))
        if tail:
            ui.info("last engine log lines:")
            ui.info(tail)

    ui.info("job timeout: %ss; sync wait: %ss" % (state.get("default_timeout", runtime.DEFAULT_JOB_TIMEOUT), state.get("sync_wait", runtime.DEFAULT_SYNC_WAIT)))
    jobs = sorted(layout.jobs_dir.glob("*")) if layout.jobs_dir.is_dir() else []
    if jobs:
        size = 0
        for entry in jobs:
            for path in entry.rglob("*"):
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
        ui.info("jobs: %d recorded, %.1f MiB under %s" % (len(jobs), size / 1048576.0, layout.jobs_dir))
    else:
        ui.info("jobs: none yet (%s)" % layout.jobs_dir)

    ui.step("Related environment variables (names only unless set by this project)")
    for key in ("STATA_MCP_HOME", "STATA_MCP_HOST", "STATA_MCP_PORT", "STATA_MCP_PYTHON", "STATA_MCP_AUTOSTART"):
        value = os.environ.get(key)
        ui.info("%s=%s" % (key, value if value else "<unset>"))

    print()
    if problems:
        ui.warn("%d item(s) need attention" % problems)
        return 1
    ui.done("No problems detected")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    layout = _layout(args)
    state = _load_state_or_fail(layout)
    bridge = Path(state.get("bridge") or layout.bridge_file)
    python = state.get("python") or sys.executable
    if not bridge.is_file():
        ui.error("bridge script missing: %s (re-run install)" % bridge)
        return 2

    ui.step("End-to-end smoke test via stdio bridge")
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "stata-mcp-installer", "version": __version__},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "stata_run_selection", "arguments": {"selection": "display 1+1"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "stata_job_list", "arguments": {}},
        },
    ]
    payload = "\n".join(json.dumps(item) for item in requests) + "\n"
    env = dict(os.environ)
    env["STATA_MCP_HOME"] = str(layout.home)
    try:
        result = subprocess.run(
            [str(python), str(bridge)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=float(args.timeout),
            env=env,
        )
    except subprocess.TimeoutExpired:
        ui.error("bridge did not finish within %.0fs" % args.timeout)
        return 2

    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    errors = [line for line in lines if '"error"' in line]
    if result.returncode != 0:
        ui.error("bridge exited with code %s" % result.returncode)
        if result.stderr:
            ui.info(result.stderr.strip()[-800:])
        return 2
    if not lines:
        ui.error("bridge produced no output")
        if result.stderr:
            ui.info(result.stderr.strip()[-800:])
        return 2

    ui.ok("received %d JSON-RPC message(s) over stdio" % len(lines))
    if not errors and "2" in result.stdout:
        ui.ok("Stata computed 1+1 through the bridge")
        if "job_id" in result.stdout or '"jobs"' in result.stdout:
            ui.ok("job tools answered through the gateway")
        else:
            ui.warn("job tools did not answer; check `status` and the gateway log")
            return 1
        return 0
    ui.warn("responses received but the expected Stata result was not found")
    ui.info((result.stdout or "").strip()[:800])
    return 1


def cmd_privacy(args: argparse.Namespace) -> int:
    layout = _layout(args)
    print(PRIVACY_TEXT.format(home=layout.home))
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    layout = _layout(args)
    state = layout.load_state()
    if not layout.home.exists():
        ui.warn("nothing installed at %s" % layout.home)
        return 0

    ui.step("Stopping server")
    if runtime.read_pid(layout) is not None or runtime.read_gateway_pid(layout) is not None:
        ui.ok(runtime.stop(layout, state))
    else:
        ui.info("no server process recorded; nothing to stop")
        # A pid file may be missing while the process is still up (manual start).
        if state.get("port") and runtime.health(state.get("host", runtime.DEFAULT_HOST), int(state["port"])):
            ui.warn(
                "something still answers on %s:%s; stop it with your process manager"
                % (state.get("host", runtime.DEFAULT_HOST), state["port"])
            )

    ui.step("Removing runtime files")
    targets: List[Path] = [
        layout.venv_dir,
        layout.server_dir,
        layout.bin_dir,
        layout.log_dir,
        layout.run_dir,
        layout.cache_dir,
        layout.jobs_dir,
    ]
    if args.purge:
        targets.append(layout.home)
    for target in targets:
        if target.exists():
            shutil.rmtree(str(target), ignore_errors=True)
            ui.ok("removed %s" % target)
    layout.remove_state()
    if not args.purge:
        ui.info("state.json removed; %s kept (use --purge to delete it)" % layout.home)
    ui.done("Uninstalled. No other application's files were touched.")
    return 0


PRIVACY_TEXT = """
Data-flow and file-touch disclosure
===================================

State directory
  Everything this project creates lives under:
      {home}
  Permissions are 0700 for directories and 0600 for files where supported.
  Nothing is written outside it, except the single configuration file you name
  explicitly with `config --write-config PATH` (and its timestamped backup).

Files written
  state.json              installation record (paths, ports, interpreter)
  server/                 upstream source checkout (only when cloning)
  venv/                   private Python environment for the server
  bin/                    gateway, stdio bridge, launcher, exec shims
  logs/server.log         engine log (may contain Stata command output/paths)
  logs/gateway.log        gateway log (job starts, engine errors)
  logs/*.console.log      captured stdout/stderr of the two processes
  jobs/<job_id>/          per-job journal: records.jsonl, journal.log, meta.json
                          (kept 7 days / 200 jobs, then removed automatically)
  run/server.pid          pid of the engine process
  run/jobs.pid            pid of the gateway process

Network
  git clone <repo>        only on `install` without --server-dir, and only the
                          URL you configure via --repo/--mirror
  package index           only when installing the server's requirements,
                          through your existing pip/uv configuration
  http://host:port        local loopback only (health + MCP endpoints)
  No telemetry, no analytics, no remote logging, no update checks.

Files read
  Directory listings for Stata discovery (application/program directories,
  PATH, and on Windows the uninstall registry keys).
  The upstream source tree you point at, and its requirements file.
  A configuration file only when you pass it explicitly with --write-config.

Never done
  No reading or rewriting of any other MCP client's configuration, API keys,
  credentials, shell profiles, datasets, or Stata do-files.
  No environment dump: doctor prints only the STATA_MCP_* variables it uses.
  No proxy environment variables are trusted for loopback traffic, and no
  credentials are ever placed in generated snippets or committed files.

Removal
  python3 install.py uninstall          # remove runtime, keep the directory
  python3 install.py uninstall --purge  # remove the directory entirely
"""


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return True
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return True
    return answer in ("", "y", "yes")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", metavar="DIR", help="state directory (default: ~/.stata-mcp or STATA_MCP_HOME)")
    parser.add_argument("--host", default=runtime.DEFAULT_HOST, help="bind/probe host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=runtime.DEFAULT_PORT, help="bind/probe port (default: 4001)")
    parser.add_argument("--stata-path", metavar="DIR", help="Stata installation root (auto-detected when omitted)")
    parser.add_argument("--python", metavar="EXE", help="interpreter to build the venv with")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Install and run a local Stata MCP server for any MCP client.",
        epilog="See docs/PRIVACY.md, docs/COMPATIBILITY.md and docs/CONFIG.md.",
    )
    parser.add_argument("--version", action="version", version="stata-mcp local installer %s" % __version__)
    sub = parser.add_subparsers(dest="command")

    install = sub.add_parser("install", help="fetch/build/install and start the server")
    _add_common(install)
    install.add_argument("--server-dir", metavar="DIR", help="use an existing upstream source checkout")
    install.add_argument("--repo", default=source.DEFAULT_REPO, help="repository URL or local path to clone")
    install.add_argument("--mirror", metavar="PREFIX", help="prefix https URLs with a mirror, e.g. https://mirror.example/")
    install.add_argument("--ref", metavar="REF", help="branch/tag to clone")
    install.add_argument("--refresh", action="store_true", help="re-clone even if a checkout already exists")
    install.add_argument("--venv", metavar="DIR", help="reuse an existing virtual environment")
    install.add_argument("--skip-deps", action="store_true", help="do not install the requirements")
    install.add_argument("--no-start", action="store_true", help="install without starting the server")
    install.add_argument("--wait", type=float, default=120.0, help="seconds to wait for health (default: 120)")
    install.add_argument(
        "--default-timeout",
        type=float,
        default=runtime.DEFAULT_JOB_TIMEOUT,
        help="default timeout for a Stata job in seconds (default: 21600 = 6h)",
    )
    install.add_argument(
        "--sync-wait",
        type=float,
        default=runtime.DEFAULT_SYNC_WAIT,
        help="seconds a synchronous tool call waits before returning a job id (default: 20)",
    )
    install.add_argument("--print-plan", "--dry-run", dest="print_plan", action="store_true", help="show what would happen and exit")
    install.add_argument("-y", "--yes", action="store_true", help="do not prompt")
    install.set_defaults(func=cmd_install)

    config = sub.add_parser("config", help="print, emit or merge an MCP client snippet")
    config.add_argument("--home", metavar="DIR")
    config.add_argument("--transport", choices=config_emit.TRANSPORTS, default="stdio")
    config.add_argument("--root", default=config_emit.DEFAULT_ROOT, help="dotted config root (default: mcpServers)")
    config.add_argument("--key", default=config_emit.DEFAULT_SERVER_KEY, help="server entry name (default: stata-mcp)")
    config.add_argument("--emit", metavar="FILE", help="write the snippet to FILE instead of stdout")
    config.add_argument("--write-config", metavar="FILE", help="merge only this server entry into an existing JSON file")
    config.add_argument("--dry-run", action="store_true", help="with --write-config: show the result without writing")
    config.add_argument("--host", default=runtime.DEFAULT_HOST)
    config.add_argument("--port", type=int, default=runtime.DEFAULT_PORT)
    config.add_argument("--python", metavar="EXE")
    config.add_argument(
        "--timeout-ms",
        type=int,
        metavar="MS",
        help="add a client-side tool-call timeout (e.g. 120000) to HTTP entries; ignored otherwise",
    )
    config.set_defaults(func=cmd_config)

    refresh = sub.add_parser("refresh", help="re-stage scripts and restart without cloning or pip")
    refresh.add_argument("--home", metavar="DIR")
    refresh.add_argument("--port", type=int, help="new client-facing port (engine moves to port+1)")
    refresh.add_argument("--wait", type=float, default=120.0)
    refresh.add_argument("--default-timeout", type=float, default=None)
    refresh.add_argument("--sync-wait", type=float, default=None)
    refresh.set_defaults(func=cmd_refresh)

    start = sub.add_parser("start", help="start the server in the background")
    start.add_argument("--home", metavar="DIR")
    start.add_argument("--port", type=int)
    start.add_argument("--wait", type=float, default=120.0)
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop the server started by this installation")
    stop.add_argument("--home", metavar="DIR")
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="show installation and health status")
    status.add_argument("--home", metavar="DIR")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="diagnose the environment")
    doctor.add_argument("--home", metavar="DIR")
    doctor.add_argument("--stata-path", metavar="DIR")
    doctor.set_defaults(func=cmd_doctor)

    verify = sub.add_parser("verify", help="end-to-end smoke test through the stdio bridge")
    verify.add_argument("--home", metavar="DIR")
    verify.add_argument("--timeout", type=float, default=300.0)
    verify.set_defaults(func=cmd_verify)

    privacy = sub.add_parser("privacy", help="print the data-flow / file-touch disclosure")
    privacy.add_argument("--home", metavar="DIR")
    privacy.set_defaults(func=cmd_privacy)

    uninstall = sub.add_parser("uninstall", help="remove the runtime")
    uninstall.add_argument("--home", metavar="DIR")
    uninstall.add_argument("--purge", action="store_true", help="delete the whole state directory")
    uninstall.set_defaults(func=cmd_uninstall)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # Default to install, matching "clone and run one command" ergonomics.
        args = parser.parse_args(["install"] + (argv or sys.argv[1:]))
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        ui.warn("interrupted")
        return 130
    except runtime.RuntimeError_ as exc:
        ui.error(str(exc))
        return 2
    except source.SourceError as exc:
        ui.error(str(exc))
        return 2
