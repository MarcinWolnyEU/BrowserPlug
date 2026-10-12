"""Tool names agents still call from a stale tools/list, and the error text that answers them.

The rename happened mid-corpus and agents kept calling the old names from a cached
tools/list; each call came back as "Unknown tool: 'x'" with nothing pointing at the
replacement, so the version skew never surfaced. A call to one of these names now fails
with a message naming the replacement and the way back to a current list.

Pure and stdlib-only on purpose: two layers answer a stale name with this exact text, the
server's tools/call dispatch (server/tools/mcp_server.py) and the launcher's proxy
transport, which resolves tool names against its own cached tools/list and so rejects a
stale name before the server sees it. tests/unit/test_tool_outcome.py pins every message.
"""

from typing import Optional

# Value = (replacement tool, how to express the old intent).
RENAMED_TOOLS = {
    "page_wait_for":       ("page_act",    'a step {"action": "wait", "target": "<css>"}'),
    "tabs_list":           ("tabs",        'action="list"'),
    "tabs_activate":       ("tabs",        'action="activate", tab_id=<id>'),
    "page_navigate":       ("navigate",    'url="<url>"'),
    "page_execute_script": ("page_eval",   'code="<js>"'),
    "page_get_text":       ("page_read",   'format="text"'),
    "page_reload":         ("navigate",    'action="reload"'),
    "server_status":       ("server_info", ""),
}
# `history_query` is deliberately NOT mapped: its replacement, `history`, was removed as
# well, so "renamed" would be a lie and the plain "Unknown tool" text is the right answer.


def renamed_tool_message(stale: str) -> Optional[str]:
    """The error text for a call to a renamed tool, or None if `stale` is not a name we
    renamed: an unmapped name keeps the plain "Unknown tool" text (already an error),
    because calling an unknown tool "renamed" would be a lie.

    ASCII only: the launcher side prints to whatever console spawned it (cp1252 on
    Windows).
    """
    entry = RENAMED_TOOLS.get(stale)
    if entry is None:
        return None
    new, how = entry
    with_how = f" with {how}" if how else ""
    return (f"Unknown tool '{stale}': it was renamed. Use '{new}'{with_how}. "
            f"Your tool list is stale - re-read it (tools/list); this server "
            f"exposes 9 tools (10 with 2+ browsers connected).")
