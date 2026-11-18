"""Terminal output helpers (no third-party dependency, no telemetry)."""

from __future__ import annotations

import os
import sys

_COLOR = None


def _use_color() -> bool:
    global _COLOR
    if _COLOR is None:
        _COLOR = (
            sys.stdout.isatty()
            and os.environ.get("NO_COLOR", "") == ""
            and os.environ.get("TERM", "") != "dumb"
        )
    return _COLOR


def _paint(text: str, code: str) -> str:
    return "\033[%sm%s\033[0m" % (code, text) if _use_color() else text


def banner(title: str) -> None:
    print()
    print(_paint("==> " + title, "36"))


def step(title: str) -> None:
    print()
    print(_paint("==> " + title, "36"))


def ok(message: str) -> None:
    print("    " + _paint("OK: ", "32") + message)


def info(message: str = "") -> None:
    print(("    " + message) if message else "")


def warn(message: str) -> None:
    print("    " + _paint("warn: ", "33") + message)


def error(message: str) -> None:
    print("    " + _paint("error: ", "31") + message, file=sys.stderr)


def hint(message: str) -> None:
    print("    " + _paint("hint: ", "35") + message)


def done(message: str) -> None:
    print()
    print(_paint("==> " + message, "32"))
    print()
