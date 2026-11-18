"""Locating or fetching the MCP server implementation.

This project intentionally does **not** vendor or patch the upstream server. It
either uses a source tree the user already has (``--server-dir``) or clones the
public repository into the private state directory at install time.

The repository URL, mirror prefix and reference are all parameters, so the
installer works with any fork, any local mirror and any offline copy. Nothing is
downloaded without the plan being printed first (``--print-plan``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

DEFAULT_REPO = "https://github.com/hanlulong/stata-mcp.git"
ENTRY_RELATIVE = ("src/stata_mcp_server.py", "stata_mcp_server.py")
REQUIREMENTS_RELATIVE = ("src/requirements.txt", "requirements.txt")


class SourceError(RuntimeError):
    """Raised when the server source cannot be located or fetched."""


@dataclass
class Source:
    root: Path
    entry: Path
    requirements: Optional[Path]
    origin: str  # "local" | "git"
    commit: str = ""

    def describe(self) -> str:
        suffix = " (%s)" % self.commit if self.commit else ""
        return "%s [%s]%s" % (self.root, self.origin, suffix)


def find_entry(root: Path) -> Optional[Path]:
    for rel in ENTRY_RELATIVE:
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def find_requirements(root: Path) -> Optional[Path]:
    for rel in REQUIREMENTS_RELATIVE:
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def inspect(root: Path) -> Optional[Source]:
    entry = find_entry(root)
    if not entry:
        return None
    return Source(
        root=root,
        entry=entry,
        requirements=find_requirements(root),
        origin="local",
        commit=_git_commit(root),
    )


def require_local(path: str) -> Source:
    root = Path(path).expanduser()
    if not root.is_dir():
        raise SourceError("--server-dir is not a directory: %s" % root)
    source = inspect(root)
    if not source:
        raise SourceError(
            "--server-dir %s does not contain any of %s"
            % (root, ", ".join(ENTRY_RELATIVE))
        )
    return source


def git_available() -> bool:
    return shutil.which("git") is not None


def _run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 900) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Never block headless installs on a credential prompt, and never leak
    # repository credentials through a pager or editor.
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GIT_PAGER": "cat", "GCM_INTERACTIVE": "never"})
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def _git_commit(root: Path) -> str:
    if not git_available():
        return ""
    try:
        result = _run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def mirror_url(repo: str, mirror: Optional[str]) -> str:
    """Prefix an https repository URL with a mirror (``https://mirror/``)."""
    if not mirror:
        return repo
    if not repo.startswith(("http://", "https://")):
        return repo  # local path or ssh/scp style, nothing to rewrite
    base = mirror.strip()
    if not base.endswith("/"):
        base += "/"
    return base + repo


def clone(repo: str, dest: Path, mirror: Optional[str] = None, ref: Optional[str] = None) -> Source:
    """Clone ``repo`` into ``dest`` (replacing any previous checkout)."""
    if not git_available():
        raise SourceError(
            "git is required to fetch the server source. Install git, or point "
            "--server-dir at an existing source checkout."
        )
    url = mirror_url(repo, mirror)
    if dest.exists():
        shutil.rmtree(str(dest), ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone", "--depth", "1", "--no-tags", "--quiet"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url, str(dest)]

    try:
        result = _run(cmd)
    except subprocess.TimeoutExpired:
        raise SourceError("git clone timed out after 15 minutes: %s" % url)
    except OSError as exc:
        raise SourceError("could not run git: %s" % exc)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = detail[-4:] if detail else []
        message = "git clone failed for %s" % url
        if tail:
            message += "\n" + "\n".join("      " + line for line in tail)
        message += (
            "\n      Alternatives: --server-dir <existing checkout>, "
            "--mirror <prefix, e.g. https://your-mirror.example/>, --repo <url>."
        )
        raise SourceError(message)

    source = inspect(dest)
    if not source:
        raise SourceError(
            "cloned %s but none of %s exists; is the repository layout expected?"
            % (url, ", ".join(ENTRY_RELATIVE))
        )
    source.origin = "git"
    return source


def acquire(
    server_dir: Optional[str],
    dest: Path,
    repo: str = DEFAULT_REPO,
    mirror: Optional[str] = None,
    ref: Optional[str] = None,
    refresh: bool = False,
) -> Source:
    """Return a usable source tree, reusing an existing checkout when possible."""
    if server_dir:
        return require_local(server_dir)

    if dest.is_dir() and not refresh:
        source = inspect(dest)
        if source:
            source.origin = "git"
            return source

    return clone(repo, dest, mirror=mirror, ref=ref)
