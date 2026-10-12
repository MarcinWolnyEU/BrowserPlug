"""Symbols removed from server/ because nothing in production called them.

Each one either had no caller at all or was a test-only helper; the helpers now live under
tests/ (token_test_helpers.py, ui_test_helpers.py). A revival has to argue for itself here.
"""

import importlib

import pytest

REMOVED = [
    ("browserplug_shared.device_tokens", "is_valid"),
    ("server.ws.ws_auth", "tokens_match"),
    ("server.ws.ws_crypto", "request_frame"),
    ("server.tools.url_diff", "classify_interstitial"),
    ("server.web.peer_owner", "peer_is_foreign"),
    ("server.reporting.reports.SnapshotCache", "invalidate"),
    ("server.browsers.routing_notice.RoutingMemory", "forget"),
]


def _resolve(dotted):
    parts = dotted.split(".")
    obj = importlib.import_module(".".join(parts[:2]))
    for name in parts[2:]:
        obj = getattr(obj, name)
    return obj


@pytest.mark.parametrize("owner,name", REMOVED)
def test_the_symbol_stays_removed(owner, name):
    assert not hasattr(_resolve(owner), name), f"{owner}.{name} is dead code again"
