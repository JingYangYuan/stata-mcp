"""Cross-platform Stata discovery.

The server needs a *Stata installation root* (for example ``/Applications/Stata``,
``/Applications/StataNow``, ``C:\\Program Files\\Stata18`` or ``/usr/local/stata18``).
Nothing here assumes a particular edition, version, vendor bundle name, drive
letter, or user name: candidates come from an explicit value, the environment,
``PATH``, the platform's package/registry metadata, and directory globs.

Only directory listings and (on Windows) the uninstall registry keys are read.
No user document, dataset, or application configuration file is inspected.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

ENV_KEYS = ("STATA_PATH", "STATA_HOME", "STATA_DIR")

# Executable stems, ordered from most to least capable edition.
_STEMS: "tuple[tuple[str, str], ...]" = (
    ("stata-mp", "mp"),
    ("stata-se", "se"),
    ("stata-be", "be"),
    ("stata", "mp"),
    ("StataMP-64", "mp"),
    ("StataSE-64", "se"),
    ("StataBE-64", "be"),
    ("StataMP", "mp"),
    ("StataSE", "se"),
    ("StataBE", "be"),
    ("Stata", "mp"),
)

MAC_APP_NAMES = {
    "mp": ("StataMP.app", "Stata.app", "StataMP64.app"),
    "se": ("StataSE.app", "Stata.app", "StataSE64.app"),
    "be": ("StataBE.app", "Stata.app", "StataBE64.app"),
}

_EDITION_RANK = {"mp": 0, "se": 1, "be": 2, "": 3}


@dataclass
class Candidate:
    path: Path
    edition: str = "mp"
    version: str = ""
    source: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        bits = [self.edition.upper() or "n/a"]
        if self.version:
            bits.append("v" + self.version)
        return " ".join(bits)


def _strip_app_bundle(path: Path) -> Path:
    """Map anything inside a macOS ``*.app`` bundle back to the install root."""
    if path.suffix == ".app":
        return path.parent
    for parent in path.parents:
        if parent.suffix == ".app":
            return parent.parent
    return path


def _as_root(value: str) -> Optional[Path]:
    """Accept a root directory or an executable/app path and return the root."""
    if not value:
        return None
    raw = str(value).strip().strip('"').strip("'")
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.exists():
        return None
    if path.is_file():
        return _strip_app_bundle(path.parent)
    return _strip_app_bundle(path)


def _version_from_marker(root: Path) -> str:
    """Stata drops an ``isstata.<NNN>`` marker (180 -> 18.0, 195 -> 19.5)."""
    try:
        markers = sorted(root.glob("isstata.*"))
    except OSError:
        return ""
    if not markers:
        return ""
    digits = re.sub(r"\D", "", markers[-1].name)
    if not digits:
        return ""
    # Stata writes ``isstata.<major><minor>``: 180 -> 18.0, 195 -> 19.5.
    if len(digits) >= 3:
        return "%d.%d" % (int(digits) // 10, int(digits) % 10)
    if len(digits) == 2:
        return "%d.%d" % (int(digits[0]), int(digits[1]))
    return digits


def _detect_edition(root: Path) -> str:
    lowered = {item.name.lower() for item in _safe_listdir(root)}
    for edition in ("mp", "se", "be"):
        for name in MAC_APP_NAMES[edition]:
            if name.lower() in lowered:
                return edition
    for stem, edition in _STEMS:
        for suffix in ("", ".exe"):
            if (stem + suffix).lower() in lowered:
                return edition
    return ""


def _safe_listdir(path: Path) -> List[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def looks_like_stata(root: Path) -> bool:
    """True when ``root`` looks like a Stata installation directory."""
    if not root or not root.is_dir():
        return False
    names = {item.name.lower() for item in _safe_listdir(root)}
    if any(name.endswith(".app") and name.startswith("stata") for name in names):
        return True
    for stem, _edition in _STEMS:
        for suffix in ("", ".exe"):
            if (stem + suffix).lower() in names:
                return True
    # StataNow/Stata 19+ ship the pystata interface even without a root binary.
    if "utilities" in names and (root / "utilities" / "pystata").is_dir():
        return True
    if "stata.lic" in names or "auto.dta" in names:
        return True
    return False


def _candidates_from_globs(roots: Iterable[Path], patterns: Iterable[str], source: str) -> Iterable[Candidate]:
    for base in roots:
        if not base or not base.is_dir():
            continue
        for pattern in patterns:
            try:
                matches = sorted(base.glob(pattern))
            except OSError:
                continue
            for match in matches:
                root = _as_root(str(match))
                if root and looks_like_stata(root):
                    yield Candidate(root, source=source)


def _mac_roots() -> Iterable[Candidate]:
    bases = [Path("/Applications"), Path("/Applications/Stata"), Path.home() / "Applications"]
    patterns = ["Stata*", "Stata*/Stata*.app", "stata*"]
    yield from _candidates_from_globs(bases, patterns, "macOS application directory")


def _windows_roots() -> Iterable[Candidate]:
    bases: List[Path] = []
    for env_name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        value = os.environ.get(env_name)
        if value:
            bases.append(Path(value))
    # Other drives are inspected one level deep only; no recursive disk walk.
    for letter in "CDEFGH":
        drive = Path(letter + ":/")
        if drive.is_dir():
            bases.append(drive)
    for cand in _candidates_from_globs(bases, ["Stata*", "stata*"], "Windows program directory"):
        yield cand

    # Uninstall registry entries are metadata, not configuration.
    try:
        import winreg  # type: ignore
    except Exception:
        return
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as parent:
                count = winreg.QueryInfoKey(parent)[0]
                for index in range(count):
                    try:
                        name = winreg.EnumKey(parent, index)
                        with winreg.OpenKey(parent, name) as entry:
                            display = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                            if not display.lower().startswith("stata"):
                                continue
                            location = str(winreg.QueryValueEx(entry, "InstallLocation")[0])
                    except OSError:
                        continue
                    root = _as_root(location)
                    if root and looks_like_stata(root):
                        yield Candidate(root, source="Windows registry")
        except OSError:
            continue


def _unix_roots() -> Iterable[Candidate]:
    bases = [Path("/usr/local"), Path("/opt"), Path("/usr/lib"), Path("/usr/share")]
    patterns = ["stata*", "Stata*", "stata*/Stata*"]
    yield from _candidates_from_globs(bases, patterns, "system program directory")


def _from_path() -> Iterable[Candidate]:
    for stem, edition in _STEMS:
        found = shutil.which(stem)
        if not found:
            continue
        exe = Path(found)
        try:
            # Follow launcher shims/symlinks back to the real installation.
            real = Path(os.path.realpath(str(exe)))
        except OSError:
            real = exe
        root = _as_root(str(real)) or _as_root(str(exe))
        if not root:
            continue
        resolved = root
        for _ in range(4):
            if looks_like_stata(resolved):
                break
            if resolved.parent == resolved:
                break
            resolved = _strip_app_bundle(resolved.parent)
        yield Candidate(
            resolved,
            edition=edition,
            source="PATH entry (%s)" % exe.name,
            notes=["found executable: %s" % exe],
        )


def _from_env() -> Iterable[Candidate]:
    for key in ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if not value:
            continue
        root = _as_root(value)
        if root and looks_like_stata(root):
            yield Candidate(root, source="environment variable %s" % key)


def _enrich(cand: Candidate) -> Candidate:
    if not cand.edition:
        cand.edition = _detect_edition(cand.path)
    if not cand.version:
        cand.version = _version_from_marker(cand.path)
    if not cand.edition:
        cand.edition = "mp"
    return cand


def discover(explicit: Optional[str] = None) -> List[Candidate]:
    """Return installation candidates, best first.

    Order: explicit value, environment variables, ``PATH``, platform
    directories/registry. Duplicates (same resolved path) are removed.
    """
    collected: List[Candidate] = []

    if explicit:
        root = _as_root(explicit)
        if root and looks_like_stata(root):
            collected.append(Candidate(root, source="explicit --stata-path"))
        elif root:
            collected.append(
                Candidate(root, source="explicit --stata-path", notes=["directory does not look like Stata"])
            )

    for provider in (_from_env, _from_path, _mac_roots if sys.platform == "darwin" else _unix_roots):
        for cand in provider():
            collected.append(cand)
    if os.name == "nt":
        for cand in _windows_roots():
            collected.append(cand)

    seen = set()
    unique: List[Candidate] = []
    for cand in collected:
        try:
            key = str(cand.path.resolve())
        except OSError:
            key = str(cand.path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(_enrich(cand))

    def sort_key(cand: Candidate):
        valid = 0 if looks_like_stata(cand.path) else 1
        try:
            version = tuple(int(part) for part in (cand.version or "0").split("."))
        except ValueError:
            version = (0,)
        return (valid, _EDITION_RANK.get(cand.edition, 9), tuple(-v for v in version))

    unique.sort(key=sort_key)
    # An explicit path wins over ranked autodetection, as long as it is usable.
    if explicit:
        explicit_root = _as_root(explicit)
        if explicit_root:
            try:
                key = str(explicit_root.resolve())
            except OSError:
                key = str(explicit_root)
            for index, cand in enumerate(unique):
                try:
                    cand_key = str(cand.path.resolve())
                except OSError:
                    cand_key = str(cand.path)
                if cand_key == key and looks_like_stata(cand.path):
                    unique.insert(0, unique.pop(index))
                    break
    return unique


def best(explicit: Optional[str] = None) -> Optional[Candidate]:
    for cand in discover(explicit):
        if looks_like_stata(cand.path):
            return cand
    return None


def describe(cand: Candidate) -> str:
    parts = [str(cand.path)]
    if cand.edition:
        parts.append(cand.edition + ("-" + cand.version if cand.version else ""))
    if cand.source:
        parts.append("[" + cand.source + "]")
    return " ".join(parts)
