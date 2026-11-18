#!/usr/bin/env python3
"""Entry point: ``python3 install.py <command>``.

Requires Python 3.8+ and nothing else (standard library only). The default
command is ``install``, so ``python3 install.py`` behaves like the documented
one-shot flow.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stata_mcp_local.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
