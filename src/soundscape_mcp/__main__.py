"""Entry point.
    python -m soundscape_mcp                 run the MCP server on stdio
    python -m soundscape_mcp TOOL [JSON]     call one tool from a shell, e.g.
    python -m soundscape_mcp listen '{"source": "output", "seconds": 20, "focus": "key"}'
"""

from __future__ import annotations

import atexit
import json
import sys

from . import environment as env
from .sessions import REGISTRY


class _ShellCtx:
    req_id = None

    def progress(self, message, progress=None, total=None):
        print(f"… {message}", file=sys.stderr, flush=True)

    def cancelled(self):
        return False


def main(argv: list[str]) -> int:
    env.setup_logging()
    env.cleanup_old_sessions()
    atexit.register(REGISTRY.stop_all)
    if not argv or argv[0] in ("serve", "--stdio"):
        from .protocol import Server
        Server().serve()
        return 0
    from .tools import BY_NAME, ToolError
    name = argv[0]
    if name in ("-h", "--help", "help") or name not in BY_NAME:
        print(__doc__.strip() + "\n\ntools: " + ", ".join(BY_NAME), file=sys.stderr)
        return 0 if name in ("-h", "--help", "help") else 2
    args = json.loads(argv[1]) if len(argv) > 1 else {}
    try:
        print(BY_NAME[name]["handler"](args, _ShellCtx()))
        return 0
    except (ToolError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
