"""Cross-platform, client-agnostic installer/runtime for a local Stata MCP server.

Design constraints (deliberate, see docs/PRIVACY.md and docs/ARCHITECTURE.md):

* Standard library only. No third-party import is required to install, inspect,
  configure, start or stop the runtime.
* No coupling to any editor, IDE, agent host or MCP client. Configuration is
  emitted as a plain MCP snippet that the user hands to whichever client they
  run; nothing is auto-detected or auto-merged into another product's files.
* No hard-coded machine paths, vendor names, or personal identifiers. Stata and
  the optional upstream source checkout are *discovered* or passed explicitly.
"""

__version__ = "1.2.1"
__all__ = ["__version__"]
