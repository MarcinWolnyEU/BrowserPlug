"""
Browser Functionality Integration Tests
Tests browser-specific functionality like tabs, content, navigation, screenshots
"""

import pytest
import pytest_asyncio
import asyncio
import re

from mcp_client_harness import DirectMCPTestClient


# The `tabs` list output is a per-window markdown block, NOT a flat bullet list:
#
#   # Window 1 focused
#   ## Example Domain
#   id: 5 active
#   url: https://example.com/
#   lastAccess: 2m ago
#   ---
#
# so the tab id lives on its own `id:` line, one block above the `url:` line.

def _tab_id_from_open(open_result):
    """Tab id out of `tabs(action="open")` — "Opened tab [<id>] in window <w>: <url>"."""
    m = re.search(r'Opened tab \[(\d+)\]', open_result.get('content', ''))
    return int(m.group(1)) if m else None


def _tab_ids_from_list(list_content):
    """Every tab id in a `tabs` (list) result, in listing order."""
    return [int(n) for n in re.findall(r'^id: (\d+)', list_content, re.MULTILINE)]


async def _close_tabs(mcp_client, tab_ids):
    """Best-effort close of every tab a test opened. Each test gets its own Firefox,
    but it still leaves no residual tabs (the cleanup rule in .tmp/dev-docs/testing.md)."""
    for tid in tab_ids:
        try:
            await mcp_client.call_tool("tabs", {"action": "close", "tab_id": tid})
        except Exception:
            pass


async def _open_settled(mcp_client, url):
    """Open a tab at `url`, then navigate(wait="settled") so the page is fully
    loaded before we read/act on it; return the new tab_id. Mirrors the proven
    pattern in test_agent_experience — replaces blind post-open sleeps and the
    internet-flakiness skips."""
    res = await mcp_client.call_tool("tabs", {"action": "open", "url": url})
    assert not res.get('isError', False), f"tabs open should not error: {res}"
    tab_id = _tab_id_from_open(res)
    assert tab_id is not None, f"tabs open should report the new tab id: {res.get('content', '')}"
    nav = await mcp_client.call_tool("navigate", {"tab_id": tab_id, "url": url, "wait": "settled"})
    assert not nav.get('isError', False), f"navigate settled should not error: {nav}"
    return tab_id


async def _wait_for_tab_listed(mcp_client, tab_id, timeout=10.0, interval=0.25):
    """Bounded poll: list tabs until `tab_id` appears, instead of a blind sleep.
    Returns the last listing content seen. Modeled on the in-tree bounded-poll
    pattern the review points to (unit/test_command_timings)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    content = ""
    while loop.time() < deadline:
        res = await mcp_client.call_tool("tabs", {})
        content = res.get('content', '')
        if not res.get('isError', False) and tab_id in _tab_ids_from_list(content):
            return content
        await asyncio.sleep(interval)
    return content


class TestBrowserFunctionality:
    """Browser functionality integration tests"""

    @pytest_asyncio.fixture
    async def full_mcp_system(self, server_with_extension):
        """Complete browser testing system with MCP client"""
        server = server_with_extension['server']
        firefox = server_with_extension['firefox']
        test_port = server_with_extension['test_port']
        mcp_port = server_with_extension['mcp_port']

        # Create direct MCP client (more reliable for testing)
        mcp_client = DirectMCPTestClient(server.mcp_tools)

        yield {
            'server': server,
            'firefox': firefox,
            'mcp_client': mcp_client,
            'test_port': test_port,
            'mcp_port': mcp_port,
            'websocket_port': test_port,  # compatibility alias
            'coordination_file': server_with_extension.get('coordination_file')
        }

        # Cleanup
        await mcp_client.disconnect()

    @pytest.mark.asyncio
    async def test_end_to_end_tab_creation_and_listing(self, full_mcp_system, fixtures_server):
        """Test complete end-to-end tab creation and listing with actual browser tabs"""
        system = full_mcp_system
        server = system['server']
        mcp_client = system['mcp_client']

        # Connect MCP client
        await mcp_client.connect()

        print("\n🧪 Testing End-to-End Tab Creation and Listing")

        created_tab_ids = []
        try:
            # Step 1: Test tabs list when no tabs exist (or only about:blank)
            print("\n1️⃣  Testing tabs list with minimal tabs...")
            result = await mcp_client.call_tool("tabs", {})
            assert not result.get('isError', False), f"tabs list should not error: {result}"
            initial_content = result.get('content', '')
            print(f"   Initial tab state: {initial_content}")

            # Step 2: Create test tabs using deterministic local fixture pages
            print("\n2️⃣  Creating test tabs via MCP tools...")
            test_urls = [
                fixtures_server + "titled.html",
                fixtures_server + "article.html",
                fixtures_server + "form.html",
            ]
            for i, url in enumerate(test_urls):
                print(f"   Creating tab {i+1}: {url}")
                result = await mcp_client.call_tool("tabs", {"action": "open", "url": url, "background": True})
                assert not result.get('isError', False), f"tabs open should not error for {url}: {result}"
                tid = _tab_id_from_open(result)
                assert tid is not None, f"tabs open should report the new tab id: {result.get('content', '')}"
                created_tab_ids.append(tid)
                print(f"   ✅ Tab created: {result.get('content', '')}")
            print(f"   ✅ Successfully created {len(created_tab_ids)} test tabs")

            # Step 3: Test tabs list with created tabs (bounded wait for the last one)
            print("\n3️⃣  Testing tabs list with created tabs...")
            tab_content = await _wait_for_tab_listed(mcp_client, created_tab_ids[-1])
            print(f"   Tab list content: {tab_content}")
            assert "No tabs found" not in tab_content, "Should find tabs after creating them"
            assert "# Window " in tab_content, f"Should group tabs by window: {tab_content}"
            listed = _tab_ids_from_list(tab_content)
            assert listed, f"Should list tab ids: {tab_content}"
            for tid in created_tab_ids:
                assert tid in listed, f"Created tab {tid} should be listed: {tab_content}"

            # Step 4: Verify tab creation tool
            print("\n4️⃣  Testing tabs(action='open') via MCP...")
            create_result = await mcp_client.call_tool("tabs", {
                "action": "open",
                "url": fixtures_server + "tall.html"
            })
            assert not create_result.get('isError', False), f"tabs open should not error: {create_result}"
            create_content = create_result.get('content', '')
            print(f"   Tab creation result: {create_content}")
            assert "Opened tab [" in create_content, "Should confirm tab creation"
            new_tab_id = _tab_id_from_open(create_result)
            assert new_tab_id is not None, f"tabs open should report the new tab id: {create_content}"
            created_tab_ids.append(new_tab_id)

            # Step 5: Final tabs list to verify all tabs
            print("\n5️⃣  Final tabs list verification...")
            final_content = await _wait_for_tab_listed(mcp_client, new_tab_id)
            print(f"   Final tab count verification: {final_content}")
            tab_ids = _tab_ids_from_list(final_content)
            assert len(tab_ids) >= 3, f"Should have at least 3 tabs, found {len(tab_ids)}: {final_content}"
            assert new_tab_id in tab_ids, f"Newly opened tab {new_tab_id} should be listed: {final_content}"
            print(f"✅ End-to-end tab test successful! Found {len(tab_ids)} tabs")
        finally:
            await _close_tabs(mcp_client, created_tab_ids)

    @pytest.mark.firefox_only(
        "page_eval/execute_script runs arbitrary code strings, which on chromium needs the "
        "per-extension 'Allow user scripts' toggle (chrome.userScripts). That toggle cannot "
        "be enabled headlessly (empirically verified: pre-seeding Preferences is ignored), "
        "so chromium returns EVAL_UNAVAILABLE; the error path is covered instead.")
    @pytest.mark.asyncio
    async def test_end_to_end_content_execute_script(self, full_mcp_system, fixtures_server):
        """Test complete end-to-end JavaScript execution in browser tabs via MCP"""
        system = full_mcp_system
        server = system['server']
        mcp_client = system['mcp_client']

        # Connect MCP client
        await mcp_client.connect()

        print("\n🧪 Testing End-to-End JavaScript Execution")

        created_tab_ids = []
        try:
            # Step 1: Get existing tabs to find one we can test with
            print("\n1️⃣  Getting existing tabs...")
            tabs_result = await mcp_client.call_tool("tabs", {})
            assert not tabs_result.get('isError', False), f"tabs list should not error: {tabs_result}"
            tab_content = tabs_result.get('content', '')
            print(f"   Available tabs: {tab_content}")
            tab_ids = _tab_ids_from_list(tab_content)
            assert tab_ids, f"Expected at least one open tab: {tab_content}"
            print(f"   ✅ Found test tab ID: {tab_ids[0]}")

            # Step 2: Open a deterministic local page and settle it before running JS.
            print("\n2️⃣  Creating tab with a settled local page...")
            test_tab_id = await _open_settled(mcp_client, fixtures_server + "titled.html")
            created_tab_ids.append(test_tab_id)
            print(f"   ✅ Using new web tab ID: {test_tab_id}")

            new_tabs_result = await mcp_client.call_tool("tabs", {})
            assert not new_tabs_result.get('isError', False), f"tabs list should not error: {new_tabs_result}"
            new_tab_content = new_tabs_result.get('content', '')
            assert test_tab_id in _tab_ids_from_list(new_tab_content), \
                f"Newly opened tab {test_tab_id} should be listed: {new_tab_content}"

            # Step 3: Test simple JavaScript execution
            print("\n3️⃣  Testing simple JavaScript execution...")
            script_result = await mcp_client.call_tool("page_eval", {
                "tab_id": test_tab_id,
                "code": "document.title"
            })
            assert not script_result.get('isError', False), f"Simple script should not error: {script_result}"
            script_content = script_result.get('content', '')
            print(f"   Script result: {script_content}")
            # Non-empty return renders as "Script result (<url>):\n<value>".
            assert "Script result (" in script_content, "Should get a script execution result"
            # The fixture's <title> is deterministic, so the eval must return it verbatim.
            assert "BrowserPlug Titled Page" in script_content, \
                f"page_eval should return the fixture's document.title: {script_content}"
            print("✅ End-to-end JavaScript execution test successful!")
        finally:
            await _close_tabs(mcp_client, created_tab_ids)

    @pytest.mark.asyncio
    async def test_end_to_end_navigation_reload(self, full_mcp_system, fixtures_server):
        """Test complete end-to-end page reload functionality via MCP"""
        system = full_mcp_system
        server = system['server']
        mcp_client = system['mcp_client']

        # Connect MCP client
        await mcp_client.connect()

        print("\n🧪 Testing End-to-End Navigation Reload")

        created_tab_ids = []
        try:
            # Step 1: Create a settled test tab (deterministic local page)
            print("\n1️⃣  Creating test tab...")
            test_tab_id = await _open_settled(mcp_client, fixtures_server + "titled.html")
            created_tab_ids.append(test_tab_id)
            print(f"   ✅ Created and settled tab {test_tab_id}")

            # Step 2: Confirm the tab is listed
            print("\n2️⃣  Confirming test tab is listed...")
            tabs_result = await mcp_client.call_tool("tabs", {})
            assert not tabs_result.get('isError', False), f"tabs list should not error: {tabs_result}"
            tab_content = tabs_result.get('content', '')
            assert test_tab_id in _tab_ids_from_list(tab_content), \
                f"Newly opened tab {test_tab_id} should be listed: {tab_content}"
            print(f"   ✅ Found test tab ID: {test_tab_id}")

            # Step 3: Test normal reload
            print("\n3️⃣  Testing normal reload...")
            reload_result = await mcp_client.call_tool("navigate", {
                "action": "reload",
                "tab_id": test_tab_id,
                "bypass_cache": False
            })
            assert not reload_result.get('isError', False), f"Normal reload should not error: {reload_result}"
            reload_content = reload_result.get('content', '')
            print(f"   Reload result: {reload_content}")
            assert "Reloaded tab" in reload_content, "Should confirm successful reload"
            assert str(test_tab_id) in reload_content, "Should mention the tab ID"
            print("✅ End-to-end navigation reload test successful!")
        finally:
            await _close_tabs(mcp_client, created_tab_ids)

    @pytest.mark.asyncio
    async def test_end_to_end_content_get_text(self, full_mcp_system, fixtures_server):
        """Test complete end-to-end text content extraction from browser tabs via MCP"""
        system = full_mcp_system
        server = system['server']
        mcp_client = system['mcp_client']

        # Connect MCP client
        await mcp_client.connect()

        print("\n🧪 Testing End-to-End Content Text Extraction")

        created_tab_ids = []
        try:
            # Step 1: Create a settled tab with a text-rich local page
            print("\n1️⃣  Creating test tab with HTML content...")
            test_tab_id = await _open_settled(mcp_client, fixtures_server + "article.html")
            created_tab_ids.append(test_tab_id)
            print(f"   ✅ Created and settled tab {test_tab_id}")

            # Step 2: Confirm the tab is listed
            print("\n2️⃣  Confirming test tab is listed...")
            tabs_result = await mcp_client.call_tool("tabs", {})
            assert not tabs_result.get('isError', False), f"tabs list should not error: {tabs_result}"
            tab_content = tabs_result.get('content', '')
            assert test_tab_id in _tab_ids_from_list(tab_content), \
                f"Newly opened tab {test_tab_id} should be listed: {tab_content}"
            print(f"   ✅ Found test tab ID: {test_tab_id}")

            # Step 3: Test page_read text extraction (unlimited)
            print("\n3️⃣  Testing page_read format='text' (unlimited)...")
            text_result = await mcp_client.call_tool("page_read", {
                "tab_id": test_tab_id,
                "format": "text"
            })
            print(f"   page_read result: {text_result}")
            assert not text_result.get('isError', False), f"page_read should not error: {text_result}"
            text_content = text_result.get('content', '')
            print(f"   Text content received (full): {len(text_content)} chars")
            # Output begins with "<title> — <url>" header + a metadata line, THEN the body.
            assert " — " in text_content, "Should return a 'title — url' header"
            assert "[text]" in text_content, "Metadata line should report the text format/range"
            body = text_content.split("\n", 2)[-1]
            assert len(body) > 50, "Should return meaningful text content"

            # Step 4: Test page_read with a small max_chars cap
            print("\n4️⃣  Testing page_read with max_chars=100...")
            limited_text_result = await mcp_client.call_tool("page_read", {
                "tab_id": test_tab_id,
                "format": "text",
                "max_chars": 100
            })
            assert not limited_text_result.get('isError', False), f"page_read with max_chars should not error: {limited_text_result}"
            limited_content = limited_text_result.get('content', '')
            print(f"   Limited text content received: {len(limited_content)} chars")
            limited_body = limited_content.split("\n", 2)[-1]
            assert len(limited_body) < len(body), "Capped read body should be shorter than unlimited"
            assert "continue with offset=" in limited_content, "Capped read should advertise pagination"
            print("✅ End-to-end page_read text test successful!")
        finally:
            await _close_tabs(mcp_client, created_tab_ids)

    @pytest.mark.firefox_only(
        "screenshots the DEFAULT active tab, which on chromium is the privileged "
        "chrome://newtab/. captureVisibleTab cannot capture chrome:// pages (host perms "
        "exclude the scheme) and MV3 has no captureTab equivalent that gecko uses for "
        "about: pages. Chromium screenshots of NORMAL pages work — covered by "
        "test_agent_experience.test_full_page_screenshot_returns_image[chromium].")
    @pytest.mark.asyncio
    async def test_end_to_end_tab_screenshot_capture(self, full_mcp_system):
        """Test complete end-to-end screenshot capture from browser tabs via MCP"""
        system = full_mcp_system
        server = system['server']
        mcp_client = system['mcp_client']

        # Connect MCP client
        await mcp_client.connect()

        print("\n🧪 Testing End-to-End Screenshot Capture")

        # page_screenshot returns a viewable image (CallToolResult with a text
        # summary + an ImageContent block), not a giant base64 text blob.
        def assert_screenshot(result):
            assert not result.get('isError', False), f"Screenshot should not error: {result}"
            tr = result.get('content')
            blocks = getattr(tr, 'content', tr)
            kinds = {getattr(b, 'type', None) for b in blocks}
            assert 'image' in kinds, f"Screenshot must include a viewable image block, got: {blocks}"
            text_blocks = [b for b in blocks if getattr(b, 'type', None) == 'text']
            assert any('Screenshot' in getattr(b, 'text', '') for b in text_blocks), \
                "Screenshot should include a text summary"
            img = next(b for b in blocks if getattr(b, 'type', None) == 'image')
            assert getattr(img, 'mime_type', '').startswith('image/'), "Image block needs an image/* mime_type"
            assert getattr(img, 'data', ''), "Image block must carry base64 data"

        # Step 1: Basic screenshot of the active tab
        print("\n1️⃣  Testing basic screenshot capture...")
        screenshot_result = await mcp_client.call_tool("page_screenshot", {})
        assert_screenshot(screenshot_result)
        print("   ✅ Basic screenshot capture successful")

        # Step 2: PNG format screenshot
        print("\n2️⃣  Testing PNG format screenshot...")
        png_result = await mcp_client.call_tool("page_screenshot", {
            "format": "png",
            "quality": 100
        })
        assert_screenshot(png_result)
        print("   ✅ PNG format screenshot successful")

        print("✅ End-to-end screenshot capture test successful!")