"""The surface the MCP tools use on the server object, written down once.

Reaching the server through `getattr(server, name, default)` calls, private names
included, turns a rename in `server.py` into a feature that switches off without a
failing test, and lets every test double implement only the members its own tests happen
to touch. `server/tools/` goes through this protocol instead.

`ServerFacade` names exactly the members the tools read, call or write. The tools are typed
against it and call those members directly, so a rename fails at the call, in production and
in the tests alike. `tests/facade_double.py` implements the same protocol for the tool tests,
and `tests/unit/test_server_facade.py` asserts that the double and the real
`BrowserPlugServer` both satisfy it and that the tools use nothing outside it.

Stdlib only, and it imports nothing from the server. A member's contract lives with the
code that owns it (`.tmp/dev-docs/tool-surface.md` has the table); the comments here say only what the
tools rely on.
"""

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ServerFacade(Protocol):
    """What `server/tools/` needs from `BrowserPlugServer`. Nothing else may be used."""

    # ---- routing and the browser registry ---------------------------------------
    def live_browsers(self) -> List[Any]:
        """Browsers a call can be routed to, in slot order."""
        ...

    def preferred_browser(self) -> Optional[Any]:
        """Where a client with no selection of its own lands."""
        ...

    def resolve_target_browser(self, client_key: str) -> Any:
        """The caller's browser, or an agent-facing error string. None is a test double
        with no registry, and keeps the legacy single-browser path."""
        ...

    async def ensure_default_browser_running(self) -> Optional[Any]:
        """Start the pinned browser when nothing is connected; the browser, or None."""
        ...

    #: client_key -> instance_id. `browsers(action="use")` writes it.
    browser_selection: Dict[str, str]

    #: `RoutingMemory`: which browser served each client last (`served`, `chose`).
    routing_memory: Any

    @property
    def _departed(self) -> Dict[str, Any]:
        """instance_id -> what the registry kept about a browser after it left."""
        ...

    #: session id -> client record, for `routing.client_key`.
    http_clients: Dict[str, Any]

    # ---- the wire ---------------------------------------------------------------
    async def send_request_and_wait(self, request: Dict[str, Any], timeout: float = 30.0,
                                    browser: Optional[Any] = None) -> Dict[str, Any]:
        """Send one request to a browser's extension and await the response."""
        ...

    # ---- the legacy single-browser view (server_info, version-skew hint, proxy gate) --
    extension_connection: Any
    extension_version: Optional[str]
    extension_proxy_access: Optional[Dict[str, Any]]

    # ---- diagnostics ------------------------------------------------------------
    def get_connections_info(self) -> Dict[str, Any]:
        """The connection report `server_info` summarises."""
        ...

    def record_call_outcome(self, tool_name: str, outcome: Optional[Dict[str, Any]],
                            call: Optional[Any] = None) -> None:
        """File a finished call's verdict on its own record; `call` is (session id,
        JSON-RPC request id). Never on whichever call is newest."""
        ...


#: Every member of `ServerFacade`, by name. The parity test compares this tuple with the
#: protocol itself, so adding a member to one and not the other fails.
FACADE_MEMBERS = (
    "live_browsers",
    "preferred_browser",
    "resolve_target_browser",
    "ensure_default_browser_running",
    "browser_selection",
    "routing_memory",
    "_departed",
    "http_clients",
    "send_request_and_wait",
    "extension_connection",
    "extension_version",
    "extension_proxy_access",
    "get_connections_info",
    "record_call_outcome",
)
