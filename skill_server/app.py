"""ASGI entrypoint for multi-worker deployment.

``skill-mcp`` (the CLI) runs one process, which is the right shape for local
use. Under real load the bottleneck is a single Python event loop, so run this
module under several uvicorn workers instead:

    uv run uvicorn skill_server.app:app --workers 8 --port 8000 --loop uvloop

This only works because the server is *stateless*: each request carries
everything needed to serve it, so it does not matter which worker lands it.
Each worker keeps its own skill index — a few hundred KB of RAM, refreshed on
its own timer, so they converge within one refresh interval after an edit.

Configuration comes from the environment, since worker processes are spawned by
uvicorn rather than by our own argument parsing:

    SKILL_MCP_ROOTS               os.pathsep-separated skill directories
    SKILL_MCP_PATH                mount path (default /mcp)
    SKILL_MCP_REFRESH             seconds between index refreshes (default 5)
    SKILL_MCP_SCRIPT_CONCURRENCY  concurrent subprocesses *per worker* (default 8)
"""

from __future__ import annotations

import os
from pathlib import Path

from skill_server.server import DEFAULT_SKILLS_DIR, build_server


def _roots() -> list[Path]:
    raw = os.getenv("SKILL_MCP_ROOTS")
    if not raw:
        return [DEFAULT_SKILLS_DIR]
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p]


mcp = build_server(
    _roots(),
    refresh_interval=float(os.getenv("SKILL_MCP_REFRESH", "5")),
    max_script_concurrency=int(os.getenv("SKILL_MCP_SCRIPT_CONCURRENCY", "8")),
)

#: Plain-JSON, stateless HTTP: no SSE framing, no session affinity.
app = mcp.http_app(
    path=os.getenv("SKILL_MCP_PATH", "/mcp"),
    json_response=True,
    stateless_http=True,
)
