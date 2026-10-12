"""The full tools/list JSON, pinned byte for byte (tool descriptions are a measured budget).

The descriptions and input schemas are re-sent to every client on every `tools/list`
(about 17 KB by default), and the server's `tools_signature` is a hash of the parts that can
change while it runs. Splitting `mcp_tools.py` into `server/tools/` must not move a single
character of either, and neither may a later edit that forgets the budget.

`golden/tools_list.json` was produced from the tree before the split, by importing the old
`mcp_tools` and dumping `list_tools()` in four situations: the default surface, the proxy
argument granted, `BROWSERPLUG_EXT_SCRIPTS` set, and two browsers connected (which adds the
`browsers` tool). Each entry keeps the registration order too. One deliberate change: in
`proxy_granted`, `tabs` and `navigate` now stay first. The old refresh removed and re-added
them, which moved them last and left a window where a concurrent `tools/list` lacked both
(`test_proxy_visibility_refresh_never_hides_tabs_or_navigate`); clients key tools by name, so
list order carries no meaning. If the surface changes ON
PURPOSE, regenerate with `BROWSERPLUG_UPDATE_GOLDEN=1` on this file alone (the variable
also rewrites test_reports_golden's goldens) and read the diff.

Each entry is `mcp_types.Tool.model_dump(mode="json", exclude_none=True)` of the listed
tool: name, description, input_schema. The golden file is written with sorted keys, so
`params` records each tool's property order separately: the order is part of the contract
since a model that writes arguments in schema order lost `tabs`' url after a run of
optional ones (`url` now comes last in `tabs` and `navigate`, after everything a model writes
before it; server/tools/tabs.py). The move to the SDK's low-level Server dropped the
per-tool title, output_schema and meta the old tool layer generated; the names, order,
descriptions, input schemas and signatures stayed byte-identical to the previous golden.
Every situation is also listed through an MCP client, and what arrives must equal that
dump, so the golden pins the wire and not only the registry.
"""

import json
import os
import types
from datetime import datetime
from pathlib import Path

import pytest
from mcp import Client

import test_imports  # noqa: F401
from facade_double import FacadeDouble
from server.tools.mcp_tools import BrowserPlugTools

GOLDEN = Path(__file__).resolve().parent / "golden" / "tools_list.json"
UPDATE = bool(os.environ.get("BROWSERPLUG_UPDATE_GOLDEN"))


def _browser(slot, name, edition, engine):
    return types.SimpleNamespace(
        slot=slot, name=name, edition=edition, friendly=edition,
        family="Firefox" if engine == "gecko" else "Chromium", engine=engine,
        fork_version=None, browser_info={"version": "150.0"}, is_remote=False,
        remote_addr=None, connected_at=datetime(2026, 1, 1, 10, 0, 0),
        last_activity=None, last_action=None, instance_id=f"inst-{slot}",
        proxy_access={"has_proxy_permission": False},
        describe=lambda s=slot, n=name: f"[{s}] {n}")


class _Server(FacadeDouble):
    def __init__(self, browsers=(), proxy=None):
        super().__init__()
        self._live = list(browsers)
        self.extension_proxy_access = proxy

    def live_browsers(self):
        return list(self._live)

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        return {}


async def _dump(tools):
    listed = await tools.mcp.list_tools()
    dump = {"order": [t.name for t in listed],
            "params": {t.name: list((t.to_mcp_tool().input_schema or {}).get("properties", {}))
                       for t in listed},
            "signature": tools.tools_signature,
            "tools": [t.to_mcp_tool().model_dump(mode="json", exclude_none=True)
                      for t in sorted(listed, key=lambda t: t.name)]}
    # What tools/list actually sends, read by an MCP client: the server's list handler is
    # BrowserPlug's own, so the accessor above alone would not see a change to it.
    async with Client(tools.mcp, mode="legacy") as client:
        wire = (await client.list_tools()).tools
    assert [t.name for t in wire] == dump["order"], "tools/list order differs from the registry"
    assert {t.name: list((t.input_schema or {}).get("properties", {})) for t in wire}         == dump["params"], "tools/list parameter order differs from the registry"
    assert [t.model_dump(mode="json", exclude_none=True)
            for t in sorted(wire, key=lambda t: t.name)] == dump["tools"], \
        "tools/list on the wire differs from the registry's tools"
    return dump


async def _variants(monkeypatch):
    monkeypatch.delenv("BROWSERPLUG_EXT_SCRIPTS", raising=False)
    result = {}
    tools = BrowserPlugTools(_Server())
    result["default"] = await _dump(tools)

    tools = BrowserPlugTools(_Server(proxy={"has_proxy_permission": True}))
    tools.refresh_proxy_tool_visibility()
    result["proxy_granted"] = await _dump(tools)

    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", "/some/scripts")
    result["scripts_enabled"] = await _dump(BrowserPlugTools(_Server()))
    monkeypatch.delenv("BROWSERPLUG_EXT_SCRIPTS")

    two = [_browser(1, "firefox-dev", "Firefox Developer Edition", "gecko"),
           _browser(2, "chrome", "Chrome", "blink")]
    result["two_browsers"] = await _dump(BrowserPlugTools(_Server(browsers=two)))
    return result


@pytest.mark.asyncio
async def test_tools_list_matches_the_pre_split_golden(monkeypatch):
    actual = await _variants(monkeypatch)
    if UPDATE:
        GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True, ensure_ascii=False),
                          encoding="utf-8", newline="\n")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert set(actual) == set(expected)
    for situation in expected:
        got, want = actual[situation], expected[situation]
        assert got["order"] == want["order"], f"{situation}: tool registration order moved"
        assert got["params"] == want["params"], f"{situation}: a parameter order moved"
        assert got["signature"] == want["signature"], f"{situation}: tools_signature moved"
        assert [t["name"] for t in got["tools"]] == [t["name"] for t in want["tools"]]
        for g, w in zip(got["tools"], want["tools"]):
            assert g["description"] == w["description"], f"{situation}/{w['name']}: description"
            assert g == w, f"{situation}/{w['name']}: schema"

