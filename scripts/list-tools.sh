#!/usr/bin/env bash
# List all MCP tools exposed by the running BrowserPlug server.
#
# Connects to the shared server's Streamable-HTTP MCP endpoint
# (http://localhost:<port>/mcp) with the MCP Python SDK's client in legacy (handshake)
# mode, the protocol era every BrowserPlug client speaks. The server must already be
# running (it's the detached server.py, not a launcher).
#
# The port is FOUND the same way the launcher finds it: 2830, then 2831 … 2942,
# taking the first whose /health identifies as BrowserPlug — so a server that had to
# relocate is still reachable. BROWSERPLUG_MCP_PORT pins a port and skips the search.
# Check by hand with: curl http://localhost:2830/health
#
# Usage:
#   scripts/list-tools.sh            name, description + parameter list (default)
#   scripts/list-tools.sh --names    just tool names, one per line
#   scripts/list-tools.sh --json     full JSON (name, description, inputSchema)
#   scripts/list-tools.sh --help
#
# Env:
#   BROWSERPLUG_MCP_PORT   MCP HTTP port (default: seek 2830-2942)

set -euo pipefail

usage() { sed -n '2,22p' "$0" | sed 's/^#\s\{0,1\}//'; }

MODE="table"
case "${1:-}" in
  --names)      MODE="names" ;;
  --json)       MODE="json" ;;
  -h|--help)    usage; exit 0 ;;
  "")           ;;
  *)            echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
esac

# Resolve repo root from this script's location (scripts/ -> repo root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# The client needs the SDK, so this runs on the server's own interpreter. It is looked up
# READ-ONLY (shared-py/browserplug_shared/server_env.py inspect_server_env: uv lock --check
# and uv sync --active --check against <state dir>/server-env), never through
# ensure_server_python: that syncs and may repair the very environment the running server is
# using. The lookup is stdlib-only; uv's python runs it. An environment that has not been
# built yet is built by `python server/control.py start`, not by this script.
command -v uv >/dev/null 2>&1 || { echo "uv not found on PATH (https://docs.astral.sh/uv/)" >&2; exit 1; }
PY="$(ROOT="$ROOT" uv run --no-project python - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["ROOT"], "shared-py"))
from browserplug_shared.server_env import inspect_server_env
state = inspect_server_env(os.environ["ROOT"])
usable = state["mode"] == "override" or state["env_current"]
if state["problem"] is None and usable and state["python"]:
    print(state["python"])
else:
    why = state["problem"] or "no usable server environment"
    fix = state["remedy"]
    if state["env_state"] in ("not_built", "out_of_date", "replace"):
        fix = "run `python server/control.py start` (stop the server first with `python server/control.py shutdown` if one is running)"
    sys.exit(f"no server interpreter: {why}" + (f". Fix: {fix}" if fix else ""))
PY
)"

# Find the running server the way the launcher does: BROWSERPLUG_MCP_PORT pins a port,
# otherwise walk 2830..2942 and take the first /health that identifies as BrowserPlug.
PORT="$(ROOT="$ROOT" "$PY" - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["ROOT"], "shared-py"))
from browserplug_shared.mcp_port import MCP_PORT_DEFAULT, pinned_mcp_port, seek_browserplug

pin = pinned_mcp_port()
if pin is not None:
    print(pin)
else:
    found, _health = seek_browserplug()
    print(found if found is not None else MCP_PORT_DEFAULT)
PY
)"

MODE="$MODE" PORT="$PORT" "$PY" - <<'PY'
import asyncio, json, os, sys

MODE = os.environ["MODE"]
PORT = os.environ["PORT"]
URL = f"http://127.0.0.1:{PORT}/mcp"

async def fetch():
    from mcp import Client
    async with Client(URL, mode="legacy") as c:
        tools, cursor = [], None
        while True:
            page = await c.list_tools(cursor=cursor)
            tools += page.tools
            cursor = page.next_cursor
            if not cursor:
                return tools

try:
    tools = asyncio.run(fetch())
except Exception as e:
    print(f"error: could not list tools from BrowserPlug server at {URL}", file=sys.stderr)
    print(f"  {type(e).__name__}: {e}", file=sys.stderr)
    print(f"  is the server up?  curl http://localhost:{PORT}/health", file=sys.stderr)
    sys.exit(1)

tools = sorted(tools, key=lambda t: t.name)

if MODE == "names":
    for t in tools:
        print(t.name)

elif MODE == "json":
    print(json.dumps([{
        "name": t.name,
        "description": getattr(t, "description", None),
        "inputSchema": getattr(t, "input_schema", None),
    } for t in tools], indent=2))

else:  # table: tool name + description, then a bullet list of its parameters
    def collapse(s):
        return " ".join((s or "").split())

    def types_of(s):
        # Collect declared JSON-Schema types, flattening list-types and
        # anyOf/oneOf/allOf unions; dedup while preserving order.
        if not isinstance(s, dict):
            return []
        out = []
        t = s.get("type")
        if isinstance(t, list):
            out += list(t)
        elif isinstance(t, str):
            out.append(t)
        for key in ("anyOf", "oneOf", "allOf"):
            for sub in s.get(key) or []:
                out += types_of(sub)
        seen, uniq = set(), []
        for x in out:
            if x not in seen:
                seen.add(x); uniq.append(x)
        return uniq

    def fmt_default(v):
        return v if isinstance(v, str) else json.dumps(v)

    # Colors: TTY-only, honoring NO_COLOR / FORCE_COLOR. sgr() returns "" when
    # disabled, so the plain layout is byte-identical when piped/redirected.
    def color_on():
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("FORCE_COLOR"):
            return True
        return sys.stdout.isatty()
    USE_COLOR = color_on()
    def sgr(code):
        return f"\033[{code}m" if USE_COLOR else ""
    RESET = sgr("0")
    BOLD  = sgr("1")
    C_SEG = sgr("38;5;245")    # the (...) segment punctuation — gray
    C_REQ = sgr("38;5;88")     # "required" — dark red
    # A distinct dark shade per JSON type (no reds). A default value is painted
    # in its own type's color (null in null's color, 10 in integer's, …).
    TYPE_CODES = {
        "string":  "38;5;240",  # dark gray
        "integer": "38;5;25",   # deep blue
        "number":  "38;5;31",   # dark teal
        "boolean": "38;5;28",   # dark green
        "null":    "38;5;96",   # mauve
        "array":   "38;5;94",   # amber/brown
        "object":  "38;5;60",   # slate blue
    }
    def tcol(name):
        return sgr(TYPE_CODES.get(name, "38;5;245"))
    def json_type_of(v):
        if v is None:               return "null"
        if isinstance(v, bool):     return "boolean"
        if isinstance(v, int):      return "integer"
        if isinstance(v, float):    return "number"
        if isinstance(v, str):      return "string"
        if isinstance(v, list):     return "array"
        if isinstance(v, dict):     return "object"
        return "any"

    width = max((len(t.name) for t in tools), default=0)
    for i, t in enumerate(tools):
        desc = (getattr(t, "description", "") or "").strip().splitlines()
        first = desc[0] if desc else ""
        if len(first) > 92:
            first = first[:89] + "..."
        if i:
            print()
        pad = " " * (width - len(t.name))
        print(f"##  {BOLD}{t.name}{RESET}{pad}  {first}")

        schema = getattr(t, "input_schema", None) or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        for pname, pschema in props.items():
            pschema = pschema or {}
            # each type in its own dark shade, returning to ambient gray (C_SEG)
            types = "||".join(f"{tcol(ty)}{ty}{C_SEG}" for ty in (types_of(pschema) or ["any"]))
            seg = [types]
            if pname in required:
                seg.append(f"{C_REQ}required{C_SEG}")
            if "default" in pschema:
                dv = pschema["default"]
                seg.append(f"default: {tcol(json_type_of(dv))}{fmt_default(dv)}{C_SEG}")
            pdesc = collapse(pschema.get("description", ""))
            line = f"  - {pname} {C_SEG}({', '.join(seg)}){RESET}"
            if pdesc:
                line += f" {pdesc}"
            print(line)

    print(f"\n{len(tools)} tools", file=sys.stderr)
PY
