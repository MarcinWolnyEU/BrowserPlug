"""A duck-typed BrowserPlugServer for the tool tests: every `ServerFacade` member, benign.

`server/tools/` calls the members of `server_facade.ServerFacade` directly, so a test double
has to have all of them. Subclass `FacadeDouble`, call `super().__init__()`, and override
what the test is about (usually `send_request_and_wait`, sometimes `live_browsers` and
`resolve_target_browser`). `tests/unit/test_server_facade.py` asserts the double and the real
`BrowserPlugServer` satisfy the same protocol, so a rename in one is a failing test in both.

The defaults describe a server with no browser registry: no live browsers, and
`resolve_target_browser` returning None, which keeps the tools on the legacy single-browser
path (`send_request_and_wait` called without `browser=`).
"""

import functools
import importlib.util
import inspect
from pathlib import Path

from server.browsers.routing_notice import RoutingMemory

_PARITY_LIB = Path(__file__).resolve().parent / "parity" / "parity_lib.py"
_handled = None


def handled_wire_actions():
    """The wire actions BOTH extensions handle: the intersection of the two background.js.

    Read from the real source, through the parity suite's own parser, so a fake can never
    accept an action that exists only in a test. The parity ledger lists no action one
    engine lacks; one that failed closed would sit in `[action_strings].exempt` and still
    counts as handled here, so it is unioned back in.
    """
    global _handled
    if _handled is None:
        spec = importlib.util.spec_from_file_location("_wire_parity_lib", _PARITY_LIB)
        lib = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lib)
        reg = lib.load_registry()
        sets = [lib.parse_action_strings(lib.read_text(lib.tree_dir(reg, t) / "background.js"))
                for t in ("gecko", "chromium")]
        _handled = (sets[0] & sets[1]) | set(reg["action_strings"].get("exempt", []))
    return _handled


def check_wire_request(request):
    """What the real `send_request_and_wait` refuses, plus what an extension would.

    The real method raises ValueError without an `id`, and a real extension answers an
    action it has no `case` for with an error. A fake that answered `{"ok": True}` to both
    let a misspelt action or a forgotten id pass every tool test (review Q4).
    """
    if not request.get("id"):
        raise ValueError("Request must have an ID")
    action = request.get("action")
    if action not in handled_wire_actions():
        raise AssertionError(
            f"the tool sent wire action {action!r}, which the extensions do not handle "
            f"(no `case` for it in both background.js files)")


def _real_send_parameters():
    from server.server import BrowserPlugServer
    return list(inspect.signature(BrowserPlugServer.send_request_and_wait).parameters)


class FacadeDouble:
    def __init_subclass__(cls, **kwargs):
        """Hold every fake `send_request_and_wait` to the real signature and the real checks.

        A subclass overrides the method to script its answers. This wraps the override so
        it takes exactly the real parameters (a rename or a dropped `browser=` fails when
        the class is defined) and rejects a missing id or an unhandled action first.
        """
        super().__init_subclass__(**kwargs)
        fn = cls.__dict__.get("send_request_and_wait")
        if fn is None:
            return
        got, want = list(inspect.signature(fn).parameters), _real_send_parameters()
        if got != want:
            raise TypeError(
                f"{cls.__qualname__}.send_request_and_wait takes {got}, but "
                f"BrowserPlugServer.send_request_and_wait takes {want}")

        @functools.wraps(fn)
        async def checked(self, request, *args, **kwargs):
            check_wire_request(request)
            return await fn(self, request, *args, **kwargs)
        cls.send_request_and_wait = checked

    def __init__(self):
        self.http_clients = {}
        self.browser_selection = {}
        self.routing_memory = RoutingMemory()
        self.extension_connection = None
        self.extension_version = None
        self.extension_proxy_access = None
        self._departed = {}
        self.outcomes = []

    def record_call_outcome(self, tool_name, outcome, call=None):
        self.outcomes.append((tool_name, outcome, call))

    def live_browsers(self):
        return []

    def preferred_browser(self):
        live = self.live_browsers()
        return live[0] if live else None

    def resolve_target_browser(self, client_key):
        return None

    async def ensure_default_browser_running(self):
        return None

    def get_connections_info(self):
        return {"count": 0, "connections": []}

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        raise NotImplementedError("the test double must define send_request_and_wait")
