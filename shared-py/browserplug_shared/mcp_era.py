"""Which MCP protocol eras BrowserPlug serves: the one setting behind the 2026-07-28 valve.

MCP has two wire eras. The handshake era (`initialize`, then an `Mcp-Session-Id` on every
request) is what Claude Code, GitLab Duo and every other client has always been served.
2026-07-28 is stateless: each request carries its own protocol version and client
capabilities, and no session exists. `mcp.era` decides where BrowserPlug carries the
second one:

  handshake  Nowhere. The HTTP gate refuses a 2026-07-28 request with -32022 naming the
             handshake versions, `--stdio` and the proxy front serve the handshake only,
             and the stdio relay sends nothing modern.
  direct     HTTP /mcp, `--stdio` and the proxy front. The stdio relay stays on the
             handshake era, so a client behind it never meets 2026-07-28.
  all        Everything `direct` serves, and the stdio relay too, for clients that ask
             for it. The default.

The value is BROWSERPLUG_MCP_ERA when it names one of the three, else the `era` key of the
`mcp` section of config.json, else `all`. A value that is present but unknown is skipped
and listed in `Setting.ignored`, so a typo can be reported and never stops a server or a
launcher from starting. Callers read the setting once, at process start: changing it takes
a server restart, and a reconnect for clients behind a relay.

Stdlib only. The launcher imports this on the bare interpreter, and
tests/unit/test_import_boundaries.py enforces that.
"""

import os
import re
from typing import Mapping, NamedTuple, Optional, Tuple

from browserplug_shared import config_store

ERA_ENV = "BROWSERPLUG_MCP_ERA"
CONFIG_SECTION = "mcp"
CONFIG_KEY = "era"

HANDSHAKE = "handshake"
DIRECT = "direct"
ALL = "all"
ERA_VALUES = (HANDSHAKE, DIRECT, ALL)
ERA_DEFAULT = ALL

SOURCE_ENV = "env"
SOURCE_CONFIG = "config"
SOURCE_DEFAULT = "default"

# A request that names no Mcp-Session-Id carries this instead: the relay (and, later, the
# proxy backend) mint one uuid4 hex per process and send it on every 2026-07-28 request, so
# the server can tell one stdio connection from another and the key survives a /restart.
CONNECTION_ID_HEADER = "X-BrowserPlug-Connection-Id"
_CONNECTION_ID = re.compile(r"[0-9a-f]{32}")


class Setting(NamedTuple):
    era: str
    source: str
    # What was present but skipped as not one of ERA_VALUES, as "name=value" text.
    ignored: Tuple[str, ...] = ()


def _clean(raw) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    return raw.strip().lower() or None


def resolve(env: Optional[Mapping[str, str]] = None) -> Setting:
    """The era setting and where it came from. Never raises: an unreadable config.json
    reads as absent (config_store.load)."""
    environ = os.environ if env is None else env
    ignored = []
    named = _clean(environ.get(ERA_ENV))
    if named is not None:
        if named in ERA_VALUES:
            return Setting(named, SOURCE_ENV)
        ignored.append(f"{ERA_ENV}={named}")
    stored = config_store.read_section(CONFIG_SECTION).get(CONFIG_KEY)
    if stored is not None:
        cleaned = _clean(stored)
        if cleaned in ERA_VALUES:
            return Setting(cleaned, SOURCE_CONFIG, tuple(ignored))
        ignored.append(f"{CONFIG_SECTION}.{CONFIG_KEY}={stored!r}")
    return Setting(ERA_DEFAULT, SOURCE_DEFAULT, tuple(ignored))


def era(env: Optional[Mapping[str, str]] = None) -> str:
    """The era setting: one of ERA_VALUES."""
    return resolve(env).era


def serves_modern(value: str) -> bool:
    """Whether the server's own transports (HTTP /mcp, `--stdio`, the proxy front) carry
    2026-07-28 under `value`."""
    return value in (DIRECT, ALL)


def relay_carries_modern(value: str) -> bool:
    """Whether the stdio relay carries 2026-07-28 for its client under `value`."""
    return value == ALL


def is_connection_id(value) -> bool:
    """Whether `value` is a well-formed connection id: 32 lowercase hex characters."""
    return isinstance(value, str) and _CONNECTION_ID.fullmatch(value) is not None
