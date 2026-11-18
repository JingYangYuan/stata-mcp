"""MCP client configuration snippets.

Two hard rules, both about privacy and non-interference:

1. Nothing is ever read from, or written to, another product's configuration
   unless the user passes the exact path with ``--write-config``. Default action
   is printing a snippet (or writing it to a file the user names).
2. When a path *is* given, only the single server key is touched, every other
   key/value in the file is preserved byte-for-byte in meaning, the file is
   copied to a timestamped backup first, and an unparseable file is refused
   rather than overwritten.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import paths

DEFAULT_SERVER_KEY = "stata-mcp"
DEFAULT_ROOT = "mcpServers"
TRANSPORTS = ("stdio", "http")


def server_entry(
    transport: str,
    python: str,
    bridge: Path,
    host: str,
    port: int,
    home: Path,
    include_env: bool = True,
) -> Dict[str, Any]:
    """Build the MCP server entry for the requested transport."""
    if transport == "http":
        return {"type": "http", "url": "http://%s:%d/mcp-streamable" % (host, port)}
    entry: Dict[str, Any] = {"command": python, "args": [str(bridge)]}
    if include_env:
        env: Dict[str, str] = {}
        default_home = os.path.abspath(str(paths.default_home()))
        if os.path.abspath(str(home)) != default_home:
            env["STATA_MCP_HOME"] = str(home)
        if (host, port) != ("127.0.0.1", 4001):
            env["STATA_MCP_HOST"] = host
            env["STATA_MCP_PORT"] = str(port)
        if env:
            entry["env"] = env
    return entry


def snippet(entry: Dict[str, Any], root: str = DEFAULT_ROOT, key: str = DEFAULT_SERVER_KEY) -> Dict[str, Any]:
    """Wrap an entry in the standard ``mcpServers`` shape (dotted root allowed)."""
    document: Dict[str, Any] = {}
    node = document
    parts = [part for part in root.split(".") if part]
    if not parts:
        raise ValueError("root must not be empty")
    for part in parts[:-1]:
        child: Dict[str, Any] = {}
        node[part] = child
        node = child
    node[parts[-1]] = {key: entry}
    return document


def render(document: Dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Optional merge into a user-specified JSON config file
# ---------------------------------------------------------------------------
def _descend(document: Dict[str, Any], root: str, create: bool) -> Optional[Dict[str, Any]]:
    node: Any = document
    for part in [p for p in root.split(".") if p]:
        if not isinstance(node, dict):
            return None
        if part not in node:
            if not create:
                return None
            node[part] = {}
        if not isinstance(node[part], dict):
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def write_config(
    path: Path,
    root: str,
    key: str,
    entry: Dict[str, Any],
    dry_run: bool = False,
) -> Tuple[str, Optional[Path]]:
    """Merge one server entry into ``path``.

    Returns ``(status, backup_path)`` where status is one of
    ``"created"``, ``"updated"``, ``"unchanged"``.
    """
    backup: Optional[Path] = None
    existing: Dict[str, Any] = {}

    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("cannot read %s: %s" % (path, exc))
        if text.strip():
            try:
                loaded = json.loads(text)
            except ValueError as exc:
                raise ValueError(
                    "%s is not valid JSON (%s). Refusing to modify it; edit it "
                    "manually and paste the snippet instead." % (path, exc)
                )
            if not isinstance(loaded, dict):
                raise ValueError("%s does not contain a JSON object; refusing to modify it" % path)
            existing = loaded

    target = _descend(existing, root, create=True)
    if target is None:
        raise ValueError(
            "cannot use root %r in %s: a non-object value already occupies that path" % (root, path)
        )
    if target.get(key) == entry:
        return "unchanged", None

    status = "updated" if path.exists() else "created"
    if dry_run:
        return status, None

    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(path.name + ".bak-" + stamp)
        try:
            shutil.copy2(str(path), str(backup))
        except OSError:
            backup = None

    target[key] = entry
    mode: Optional[int] = 0o600
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        pass
    paths.write_text_atomic(path, render(existing), mode=mode or 0o600)
    return status, backup
