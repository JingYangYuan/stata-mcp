"""Filesystem layout for the runtime.

Every artifact this project creates lives below one state directory (default
``~/.stata-mcp``, override with ``--home`` or ``STATA_MCP_HOME``) so that:

* users can audit or delete everything in a single step;
* no other application's directories are ever written to;
* the stdlib-only helper scripts in ``bin/`` can locate the runtime without
  importing this package (they read ``state.json``).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

ENV_HOME = "STATA_MCP_HOME"
DEFAULT_DIR_NAME = ".stata-mcp"
STATE_SCHEMA = 1


def default_home() -> Path:
    """Return the default state directory, honouring ``STATA_MCP_HOME``."""
    env = os.environ.get(ENV_HOME, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / DEFAULT_DIR_NAME


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (parents included) with 0700 permissions, best effort."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path), 0o700)
    except OSError:
        pass
    return path


def _private(path: Path, is_dir: bool = False) -> None:
    """Best-effort 0700/0600 permissions; silently skipped where unsupported."""
    try:
        mode = 0o700 if is_dir else 0o600
        os.chmod(str(path), mode)
    except OSError:
        pass


class Layout:
    """Paths for one runtime instance."""

    def __init__(self, home: Optional[str] = None) -> None:
        raw = Path(home).expanduser() if home else default_home()
        self.home = Path(os.path.abspath(str(raw)))

    # -- directories ------------------------------------------------------
    @property
    def server_dir(self) -> Path:
        return self.home / "server"

    @property
    def venv_dir(self) -> Path:
        return self.home / "venv"

    @property
    def bin_dir(self) -> Path:
        return self.home / "bin"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def jobs_dir(self) -> Path:
        return self.home / "jobs"

    # -- files ------------------------------------------------------------
    @property
    def state_file(self) -> Path:
        return self.home / "state.json"

    @property
    def bridge_file(self) -> Path:
        return self.bin_dir / "stata-mcp-bridge.py"

    @property
    def launcher_file(self) -> Path:
        return self.bin_dir / "stata-mcp-start.py"

    @property
    def gateway_file(self) -> Path:
        return self.bin_dir / "stata-mcp-jobs.py"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "server.log"

    @property
    def gateway_log_file(self) -> Path:
        return self.log_dir / "gateway.log"

    @property
    def pid_file(self) -> Path:
        return self.run_dir / "server.pid"

    @property
    def gateway_pid_file(self) -> Path:
        return self.run_dir / "jobs.pid"

    def venv_python(self) -> Path:
        if os.name == "nt":
            return self.venv_dir / "Scripts" / "python.exe"
        return self.venv_dir / "bin" / "python"

    # -- operations -------------------------------------------------------
    def directories(self):
        return [
            self.home,
            self.bin_dir,
            self.log_dir,
            self.run_dir,
            self.cache_dir,
            self.jobs_dir,
        ]

    def ensure(self) -> "Layout":
        """Create the state directories with private permissions."""
        for path in self.directories():
            ensure_private_dir(path)
        return self

    def exists(self) -> bool:
        return self.state_file.is_file()

    def load_state(self) -> Dict[str, Any]:
        try:
            with open(str(self.state_file), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_state(self, state: Dict[str, Any]) -> None:
        payload = dict(state)
        payload.setdefault("schema", STATE_SCHEMA)
        payload["home"] = str(self.home)
        _atomic_write_json(self.state_file, payload)
        _private(self.state_file)

    def remove_state(self) -> None:
        try:
            self.state_file.unlink()
        except OSError:
            pass


def rmtree_force(path: Path, retries: int = 3) -> bool:
    """Remove a file or directory tree even when Windows marks objects read-only.

    Git pack/index files under ``.git/objects`` carry the read-only attribute
    on Windows, causing plain ``shutil.rmtree`` to leave behind a broken partial
    directory. Clears read-only bits on error and retries briefly for transient
    file-handle locks.
    """
    target = Path(path)
    if not target.exists() and not target.is_symlink():
        return True
    if target.is_file() or target.is_symlink():
        for attempt in range(max(1, retries)):
            try:
                os.chmod(str(target), stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            try:
                target.unlink()
                return True
            except OSError:
                if attempt + 1 < retries:
                    time.sleep(0.15 * (attempt + 1))
        return not target.exists()

    def _on_error(func, failed_path, _exc):
        try:
            os.chmod(failed_path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        try:
            func(failed_path)
        except OSError:
            pass

    for attempt in range(max(1, retries)):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(str(target), onexc=_on_error)
            else:
                shutil.rmtree(str(target), onerror=_on_error)
        except OSError:
            pass
        if not target.exists():
            return True
        if attempt + 1 < retries:
            time.sleep(0.15 * (attempt + 1))
    return not target.exists()


def _atomic_write_json(path: Path, data: Any, mode: Optional[int] = 0o600) -> None:
    """Write JSON atomically so a crash can never leave a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    tmp = Path(handle.name)
    try:
        with handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        if mode is not None:
            try:
                os.chmod(str(tmp), mode)
            except OSError:
                pass
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_text_atomic(path: Path, text: str, mode: Optional[int] = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    tmp = Path(handle.name)
    try:
        with handle:
            handle.write(text)
        if mode is not None:
            try:
                os.chmod(str(tmp), mode)
            except OSError:
                pass
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
