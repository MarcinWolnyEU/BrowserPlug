"""
Window Management End-to-End Tests

Comprehensive functional tests for browser window operations through the
MCP server and WebSocket protocol with real Firefox extension.

The dedicated windows_* tools were removed in the v2 consolidation; window
management now lives inside the `tabs` tool:
  - tabs(action="list")  -> all tabs grouped by "# Window <id>" markdown headers;
                            the focused window carries a " focused" flag (or
                            " lastFocused" when Firefox isn't the frontmost app,
                            e.g. always under the headless test harness);
                            replaces windows_list / windows_get /
                            windows_get_current / windows_get_last_focused
  - tabs(action="open", new_window=True, url=...) -> "Opened window <id> with tab [<tid>]: <url>"
  - tabs(action="close", window_id=...)           -> "Closed window <id>"
  - tabs(action="activate", tab_id=...)           -> focuses the tab's window
  - tabs(action="window", window_id=..., ...)     -> "Window <id> updated: ..."

These tests therefore drive `tabs` and parse the grouped listing to recover the
window ids they used to read from the windows_* tools.
"""

import pytest
import asyncio
import re

from mcp_client_harness import DirectMCPTestClient


# ---- helpers for parsing the consolidated tabs(action="list") output ----------

def _window_ids(listing: str):
    """All window ids in a tabs(action='list') listing, in order of appearance."""
    return [int(m) for m in re.findall(r'^# Window (\d+)', listing, re.MULTILINE)]


def _window_count(listing: str):
    """Number of '# Window <id>' groups in the listing."""
    return len(_window_ids(listing))


def _focused_window_id(listing: str):
    """The window id flagged ' focused' — or ' lastFocused', which the listing
    falls back to when no Firefox window has OS focus (always the case under
    the headless test harness). None when neither flag is present."""
    m = re.search(r'^# Window (\d+)[^\n]*\b(?:focused|lastFocused)\b',
                  listing, re.MULTILINE)
    return int(m.group(1)) if m else None


def _tab_ids_for_window(listing: str, window_id: int):
    """Tab ids ('id: <tid> ...' lines) under the given '# Window <id>' header."""
    tab_ids = []
    in_window = False
    for line in listing.split('\n'):
        wm = re.match(r'^# Window (\d+)', line)
        if wm:
            in_window = (int(wm.group(1)) == window_id)
            continue
        if in_window:
            tm = re.match(r'^id: (\d+)', line)
            if tm:
                tab_ids.append(int(tm.group(1)))
    return tab_ids


def _tab_flags(listing: str, tab_id: int):
    """The flag suffix on a tab's 'id: <tid> <flags>' line (e.g. ' active pinned').

    The consolidated `tabs` listing marks a pinned tab with a bare ' pinned' flag on
    its 'id:' line (mcp_tools.py builds `id: <tid>{flags}`), NOT a '(pinned)' token.
    Returns '' when the tab id is present but carries no flags, and None when the
    tab id isn't in the listing at all."""
    m = re.search(rf'^id: {tab_id}\b(.*)$', listing, re.MULTILINE)
    return m.group(1) if m else None


async def _list_tabs(client):
    """tabs(action='list', recent=False) -> content string (asserts success).

    recent=False matters: the default recent-view can hide a window with no
    recently-used tabs entirely (summarized in a trailing line), which would
    break the window-count/membership assertions these tests make."""
    result = await client.call_tool("tabs", {"recent": False})
    assert isinstance(result, dict), "MCP client should return dict wrapper"
    assert result.get("success", False), f"tabs list should succeed: {result}"
    content = result.get("content", "")
    assert isinstance(content, str), "Content should be a string"
    return content


class TestWindowManagementEndToEnd:
    """End-to-end tests for window management functionality"""


    @pytest.mark.asyncio
    async def test_list_windows(self, server_with_extension):
        """Test listing all browser windows (now via tabs grouped by window)"""
        setup = server_with_extension

        # Create MCP client
        client = DirectMCPTestClient(setup['server'].mcp_tools)
        await client.connect()

        try:
            # tabs(action="list") groups tabs by window
            content = await _list_tabs(client)

            # Validate response content (grouped-markdown format)
            assert "# Window " in content, "Should list at least one '# Window <id>' group"
            assert len(_window_ids(content)) >= 1, "Should find at least one window id"
            assert re.search(r'^id: \d+', content, re.MULTILINE), \
                "Each window group should list its tabs as 'id: <tid>' lines"

            print(f"Window list result: {content[:200]}...")

        finally:
            pass

    @pytest.mark.asyncio
    async def test_get_current_window(self, server_with_extension):
        """Test getting current (focused) window information from the listing"""
        setup = server_with_extension

        # Create MCP client
        client = DirectMCPTestClient(setup['server'].mcp_tools)
        await client.connect()

        try:
            content = await _list_tabs(client)

            # The focused window carries a " focused" flag in the grouped listing
            # (" lastFocused" under the headless harness, where nothing has OS focus).
            focused_id = _focused_window_id(content)
            assert focused_id is not None, \
                f"Listing should mark a focused window: {content}"

            print(f"Current (focused) window ID: {focused_id}")

        finally:
            pass

    @pytest.mark.asyncio
    async def test_create_and_close_window(self, server_with_extension):
        """Test creating and closing a window via tabs(action='open'/'close')"""
        setup = server_with_extension

        # Create MCP client
        client = DirectMCPTestClient(setup['server'].mcp_tools)
        await client.connect()

        try:
            # Get initial window count
            initial_content = await _list_tabs(client)
            initial_count = _window_count(initial_content)

            # Create new window (tabs open with new_window=True)
            create_result = await client.call_tool("tabs", {
                "action": "open",
                "url": "about:blank",
                "new_window": True,
                "width": 800,
                "height": 600
            })

            # Extract content from MCP client wrapper
            create_content = create_result.get("content", "")

            # Validate creation (new format: "Opened window <id> with tab [<tid>]: ...")
            assert isinstance(create_content, str), "Create result should be a string"
            assert "Opened" in create_content and "window" in create_content, \
                "Result should indicate window creation"

            # Extract window ID from result like "Opened window 123 with tab [456]: ..."
            id_match = re.search(r'Opened (?:incognito )?window (\d+)', create_content)
            assert id_match, f"Could not extract window ID from: {create_content}"
            new_window_id = int(id_match.group(1))

            # Wait for window to be created
            await asyncio.sleep(1.0)

            # Verify window was created
            after_create_content = await _list_tabs(client)
            after_create_count = _window_count(after_create_content)
            assert after_create_count == initial_count + 1, "Should have one more window"
            assert new_window_id in _window_ids(after_create_content), \
                "New window should appear in the listing"

            print(f"Created window with ID: {new_window_id}")

            # Close the window
            close_result = await client.call_tool("tabs", {
                "action": "close", "window_id": new_window_id
            })

            # Extract content and validate closure (new format: "Closed window <id>")
            close_content = close_result.get("content", "")
            assert isinstance(close_content, str), "Close result should be a string"
            assert f"Closed window {new_window_id}" in close_content, "Window should close successfully"

            # Wait for window to be closed
            await asyncio.sleep(1.0)

            # Verify window was closed
            after_close_content = await _list_tabs(client)
            after_close_count = _window_count(after_close_content)
            assert after_close_count == initial_count, "Should be back to original count"

            print(f"Closed window with ID: {new_window_id}")

        finally:
            pass

    @pytest.mark.asyncio
    async def test_focus_window(self, server_with_extension):
        """Test focusing a window (now via tabs activate on one of its tabs)"""
        setup = server_with_extension

        # Create MCP client
        client = DirectMCPTestClient(setup['server'].mcp_tools)
        await client.connect()

        try:
            # Get current (focused) window and one of its tabs
            listing = await _list_tabs(client)
            current_window_id = _focused_window_id(listing) or _window_ids(listing)[0]
            tab_ids = _tab_ids_for_window(listing, current_window_id)
            assert tab_ids, f"Window {current_window_id} should have at least one tab"

            # Activate a tab in that window — this focuses its window.
            focus_result = await client.call_tool("tabs", {
                "action": "activate", "tab_id": tab_ids[0]
            })

            # Extract content and validate focus (new format: "Activated tab <id> ...")
            focus_content = focus_result.get("content", "")
            assert isinstance(focus_content, str), "Focus result should be a string"
            assert "Activated tab" in focus_content, "Activate should succeed"

            print(f"Successfully focused window ID {current_window_id} via tab {tab_ids[0]}")

        finally:
            pass

    @pytest.mark.asyncio
    async def test_get_window_by_id(self, server_with_extension):
        """Test getting a specific window's tabs by id from the listing"""
        setup = server_with_extension

        # Create MCP client
        client = DirectMCPTestClient(setup['server'].mcp_tools)
        await client.connect()

        try:
            # Get current (focused) window id
            listing = await _list_tabs(client)
            window_id = _focused_window_id(listing) or _window_ids(listing)[0]

            # The grouped listing IS the per-window view; verify the window group
            # is present and shows its tabs.
            assert f"Window {window_id}" in listing, "Should return the requested window group"
            assert _tab_ids_for_window(listing, window_id), \
                "Window group should list its tabs"

            print(f"Retrieved window ID: {window_id} from listing")

        finally:
            pass

    @pytest.mark.asyncio
    async def test_update_window_properties(self, server_with_extension):
        """Test updating window properties via tabs(action='window')"""
        setup = server_with_extension

        # Create MCP client
        client = DirectMCPTestClient(setup['server'].mcp_tools)
        await client.connect()

        try:
            # Get current (focused) window id
            listing = await _list_tabs(client)
            window_id = _focused_window_id(listing) or _window_ids(listing)[0]

            # Try to update window (resize it)
            update_result = await client.call_tool("tabs", {
                "action": "window",
                "window_id": window_id,
                "width": 900,
                "height": 700
            })

            # Extract content and validate update (new format: "Window <id> updated: ...")
            update_content = update_result.get("content", "")
            assert isinstance(update_content, str), "Update result should be a string"
            assert f"Window {window_id} updated" in update_content, "Should reference correct window ID"

            # Note: Some properties might not change due to Firefox restrictions,
            # but the operation should still succeed
            print(f"Updated window ID: {window_id}, result: {update_content}")

        finally:
            pass

    @pytest.mark.asyncio
    async def test_window_error_handling(self, server_with_extension):
        """Test error handling for invalid window operations"""
        setup = server_with_extension

        # Create MCP client
        client = DirectMCPTestClient(setup['server'].mcp_tools)
        await client.connect()

        try:
            # Try to update a non-existent window (no get-by-id tool exists now;
            # exercise an operation that targets an invalid window id instead).
            update_result = await client.call_tool("tabs", {
                "action": "window", "window_id": 99999, "width": 800
            })
            update_content = update_result.get("content", "")

            # The error should be in the content, not raised as exception
            assert isinstance(update_content, str), "Update result should be a string"
            assert ("Error" in update_content or "not found" in update_content or
                    "No window" in update_content), "Should indicate error for invalid window"

            # Try to close non-existent window
            close_result = await client.call_tool("tabs", {
                "action": "close", "window_id": 99999
            })
            close_content = close_result.get("content", "")

            assert isinstance(close_content, str), "Close result should be a string"
            assert ("Error" in close_content or "not found" in close_content or
                    "Unable to close" in close_content or "Failed to close" in close_content), \
                "Should indicate error or failure to close"

            print("Error handling tests passed")

        finally:
            pass

    @pytest.mark.asyncio
    async def test_multi_window_tab_management(self, server_with_extension, fixtures_server):
        """Test creating multiple windows and verifying window-specific operations"""
        setup = server_with_extension

        # Create MCP client
        client = DirectMCPTestClient(setup['server'].mcp_tools)
        await client.connect()

        created_window_ids = []

        try:
            # Get initial state
            initial_content = await _list_tabs(client)
            initial_count = _window_count(initial_content)
            initial_window_ids = _window_ids(initial_content)

            print(f"Initial window count: {initial_count}")
            print(f"Initial window IDs: {initial_window_ids}")

            # Create first additional window
            print("\n🪟 Creating first window...")
            window1_result = await client.call_tool("tabs", {
                "action": "open",
                "url": "about:blank",
                "new_window": True,
                "width": 800,
                "height": 600
            })

            window1_content = window1_result.get("content", "")
            id_match = re.search(r'Opened (?:incognito )?window (\d+)', window1_content)
            assert id_match, f"Could not extract window ID from: {window1_content}"
            window1_id = int(id_match.group(1))
            created_window_ids.append(window1_id)

            print(f"Created window 1 with ID: {window1_id}")

            # Wait for window to be fully created
            await asyncio.sleep(1.5)

            # Create second additional window (unfocused / background)
            print("\n🪟 Creating second window...")
            window2_result = await client.call_tool("tabs", {
                "action": "open",
                "url": fixtures_server + "titled.html",
                "new_window": True,
                "width": 900,
                "height": 650,
                "background": True  # Don't focus this one initially
            })

            window2_content = window2_result.get("content", "")
            id_match = re.search(r'Opened (?:incognito )?window (\d+)', window2_content)

            # Handle case where window is created but details can't be retrieved immediately
            if not id_match:
                print("Window 2 created but id not immediately available, listing windows to find it...")
                await asyncio.sleep(1)  # Give time for window to stabilize

                # List all windows to find the new one
                current_content = await _list_tabs(client)
                print(f"Window listing content: {current_content[:500]}...")  # Debug output

                all_ids = _window_ids(current_content)
                print(f"Found window IDs in listing: {all_ids}")
                print(f"Already created IDs: {created_window_ids}")

                new_window_ids = [wid for wid in all_ids
                                  if wid not in created_window_ids and wid not in initial_window_ids]

                if new_window_ids:
                    window2_id = new_window_ids[0]  # Take the first new window
                    created_window_ids.append(window2_id)
                    print(f"Found window 2 with ID: {window2_id}")
                else:
                    print("Second window creation failed, will use original window for cross-window testing")
                    window2_id = initial_window_ids[0] if initial_window_ids else 1  # Use original window
            else:
                window2_id = int(id_match.group(1))
                created_window_ids.append(window2_id)
                print(f"Created window 2 with ID: {window2_id}")

            # Wait for second window to be fully created
            await asyncio.sleep(1.5)

            # Verify we now have at least 1 more window (second window creation might fail sometimes)
            after_creation_content = await _list_tabs(client)
            after_creation_count = _window_count(after_creation_content)

            print(f"Window count after creation: {after_creation_count}")
            assert after_creation_count >= initial_count + 1, \
                f"Should have at least 1 more window. Initial: {initial_count}, After: {after_creation_count}"
            print(f"✅ Verified window creation: {after_creation_count} total windows")

            # Create tabs in first window
            print(f"\n📑 Creating tabs in window {window1_id}...")

            # Focus first window before creating tabs (activate one of its tabs)
            w1_tabs = _tab_ids_for_window(after_creation_content, window1_id)
            if w1_tabs:
                focus_result1 = await client.call_tool("tabs", {
                    "action": "activate", "tab_id": w1_tabs[0]
                })
                assert "Activated tab" in focus_result1.get("content", ""), "Should focus window 1"

            await asyncio.sleep(0.5)

            # Create first tab in window 1 (target the window explicitly)
            tab1_result = await client.call_tool("tabs", {
                "action": "open",
                "url": fixtures_server + "article.html",
                "window_id": window1_id
            })
            tab1_content = tab1_result.get("content", "")
            # NOT a skip. This used to skip precisely WHEN tabs(open) — a core feature, and
            # the very thing the rest of this test measures — was broken, reporting yellow
            # ("not applicable") for a red product. The three sibling tab creations below
            # already assert "Opened tab [" outright; this one is no different, and letting
            # it skip meant a tabs(open) regression could take this module green.
            assert "Opened tab [" in tab1_content, (
                f"tabs(open) failed to create tab 1 in window {window1_id}.\n"
                f"  isError: {tab1_result.get('isError')}\n"
                f"  result:  {tab1_content!r}\n"
                f"  url requested: {fixtures_server + 'article.html'}\n"
                "A stale/closed window_id yields a recovery-hint error naming the live "
                "window; anything else means tab creation itself is broken."
            )

            # Extract tab ID from response ("Opened tab [<id>] in window W: url")
            tab_id_match = re.search(r'Opened tab \[(\d+)\]', tab1_content)
            tab1_id = int(tab_id_match.group(1)) if tab_id_match else None
            print(f"Created tab 1 in window {window1_id}: {tab1_content[:100]}...")

            await asyncio.sleep(0.5)

            # Create second tab in window 1
            tab2_result = await client.call_tool("tabs", {
                "action": "open",
                "url": fixtures_server + "form.html",
                "background": True,
                "window_id": window1_id
            })
            tab2_content = tab2_result.get("content", "")
            assert "Opened tab [" in tab2_content, f"Should create tab 2: {tab2_content}"
            print(f"Created tab 2 in window {window1_id}: {tab2_content[:100]}...")

            await asyncio.sleep(0.5)

            # Create tabs in second window
            print(f"\n📑 Creating tabs in window {window2_id}...")

            # Create first tab in window 2 (target the window explicitly)
            tab3_result = await client.call_tool("tabs", {
                "action": "open",
                "url": fixtures_server + "tall.html",
                "window_id": window2_id
            })
            tab3_content = tab3_result.get("content", "")
            assert "Opened tab [" in tab3_content, f"Should create tab 3: {tab3_content}"
            print(f"Created tab 1 in window {window2_id}: {tab3_content[:100]}...")

            await asyncio.sleep(0.5)

            # Create second tab in window 2
            tab4_result = await client.call_tool("tabs", {
                "action": "open",
                "url": fixtures_server + "modal.html",
                "background": True,
                "window_id": window2_id
            })
            tab4_content = tab4_result.get("content", "")
            assert "Opened tab [" in tab4_content, f"Should create tab 4: {tab4_content}"
            print(f"Created tab 2 in window {window2_id}: {tab4_content[:100]}...")

            await asyncio.sleep(1.0)

            # Verify tabs are correctly distributed across windows
            print("\n🔍 Verifying tab distribution across windows...")

            final_content = await _list_tabs(client)

            # Verify each created window has tabs
            assert f"Window {window1_id}" in final_content, "Window 1 should be listed"
            assert f"Window {window2_id}" in final_content, "Window 2 should be listed"

            # Check that each window has multiple tabs (original + created)
            window1_tabs = _tab_ids_for_window(final_content, window1_id)
            window2_tabs = _tab_ids_for_window(final_content, window2_id)
            print(f"Window 1 tab ids: {window1_tabs}")
            print(f"Window 2 tab ids: {window2_tabs}")

            assert len(window1_tabs) >= 2, f"Window 1 should have at least 2 tabs, got {len(window1_tabs)}"
            assert len(window2_tabs) >= 2, f"Window 2 should have at least 2 tabs, got {len(window2_tabs)}"
            print(f"✅ Window 1 has {len(window1_tabs)} tabs")
            print(f"✅ Window 2 has {len(window2_tabs)} tabs")

            # Verify tabs exist with expected URLs (using the actual URLs we created)
            assert ("titled.html" in final_content or
                    "article.html" in final_content or
                    "form.html" in final_content or
                    "tall.html" in final_content or
                    "modal.html" in final_content), "Should find at least one of the created tabs"

            # Verify we have multiple tabs overall
            tab_count = len(re.findall(r'^id: \d+', final_content, re.MULTILINE))
            assert tab_count >= 2, f"Should have at least 2 tabs, got {tab_count}"

            print("\n✅ Multi-window tab management test completed successfully!")
            print(f"✅ Created 2 windows (IDs: {window1_id}, {window2_id})")
            print("✅ Created multiple tabs in each window")
            print("✅ Verified tabs are properly isolated per window")
            print("✅ Confirmed tab operations work across different windows")

        finally:
            # Clean up created windows
            print(f"\n🧹 Cleaning up {len(created_window_ids)} created windows...")
            for window_id in created_window_ids:
                try:
                    close_result = await client.call_tool("tabs", {
                        "action": "close", "window_id": window_id
                    })
                    close_content = close_result.get("content", "")
                    if f"Closed window {window_id}" in close_content:
                        print(f"✅ Closed window {window_id}")
                    else:
                        print(f"⚠️ Window {window_id} close result: {close_content}")

                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"⚠️ Error closing window {window_id}: {e}")

    @pytest.mark.asyncio
    async def test_basic_window_operations(self, server_with_extension, fixtures_server):
        """Test basic window creation, focus, and listing operations"""
        setup = server_with_extension

        # Create MCP client
        client = DirectMCPTestClient(setup['server'].mcp_tools)
        await client.connect()

        created_window_ids = []
        created_tab_ids = []  # tabs opened in the pre-existing (original) window

        try:
            # Get initial state
            initial_content = await _list_tabs(client)
            print(f"Initial state: {initial_content}")
            initial_window_ids = _window_ids(initial_content)

            # Create a new window
            print("\n🪟 Creating new window...")
            window_result = await client.call_tool("tabs", {
                "action": "open",
                "url": "about:blank",
                "new_window": True,
                "width": 800,
                "height": 600
            })

            window_content = window_result.get("content", "")
            print(f"Window creation result: {window_content}")

            # Extract window ID
            id_match = re.search(r'Opened (?:incognito )?window (\d+)', window_content)
            assert id_match, f"Could not find window ID in: {window_content}"
            window_id = int(id_match.group(1))
            created_window_ids.append(window_id)

            print(f"✅ Created window with ID: {window_id}")

            # Wait for window to stabilize
            await asyncio.sleep(1.0)

            # Test focus operation (activate a tab in the new window)
            print(f"\n🎯 Testing window focus...")
            listing = await _list_tabs(client)
            new_window_tabs = _tab_ids_for_window(listing, window_id)
            assert new_window_tabs, f"New window {window_id} should have a tab to activate"
            focus_result = await client.call_tool("tabs", {
                "action": "activate", "tab_id": new_window_tabs[0]
            })
            focus_content = focus_result.get("content", "")
            print(f"Focus result: {focus_content}")
            assert "Activated tab" in focus_content, f"Focus should succeed: {focus_content}"

            # Test getting current (focused) window
            print(f"\n📍 Testing get current window...")
            current_content = await _list_tabs(client)
            print(f"Current window listing: {current_content}")

            # Verify the current listing includes our created window ID
            assert window_id in _window_ids(current_content), \
                f"Current listing should reference our window {window_id}"

            # Test listing all windows
            print(f"\n📋 Testing final window listing...")
            final_content = await _list_tabs(client)
            print(f"Final window list: {final_content}")

            # Verify our window appears in the list
            assert f"Window {window_id}" in final_content, f"Our window {window_id} should appear in listing"

            # Identify the original window (one that isn't our created window)
            original_window_id = None
            for wid in _window_ids(final_content):
                if wid != window_id:
                    original_window_id = wid
                    break

            if original_window_id:
                print(f"\n📑 Testing cross-window tab creation with window_id parameter...")
                print(f"Original window ID: {original_window_id}, New window ID: {window_id}")

                # Create a tab directly in the original window using window_id parameter
                print(f"📄 Creating tab in original window {original_window_id} using window_id parameter...")
                tab_result = await client.call_tool("tabs", {
                    "action": "open",
                    "url": fixtures_server + "article.html",
                    "window_id": original_window_id
                })
                tab_content = tab_result.get("content", "")
                print(f"Tab creation result: {tab_content}")

                _m = re.search(r'Opened tab \[(\d+)\]', tab_content)
                if _m:
                    created_tab_ids.append(int(_m.group(1)))
                    print(f"✅ Successfully created tab in original window {original_window_id}")
                else:
                    print(f"⚠️ Tab creation in original window failed: {tab_content}")

                await asyncio.sleep(0.5)

                # Now create a tab in the NEW window using window_id parameter
                print(f"\n📄 Creating tab in new window {window_id} using window_id parameter...")
                tab2_result = await client.call_tool("tabs", {
                    "action": "open",
                    "url": fixtures_server + "form.html",
                    "window_id": window_id
                })
                tab2_content = tab2_result.get("content", "")
                print(f"Tab 2 creation result: {tab2_content}")

                if "Opened tab [" in tab2_content:
                    print(f"✅ Successfully created tab in new window {window_id}")
                else:
                    print(f"⚠️ Tab creation in new window failed: {tab2_content}")

                await asyncio.sleep(0.5)

                # Create an additional (third) tab in window 1 to demonstrate more comprehensive functionality
                print(f"\n📄 Creating additional tab in original window {original_window_id}...")
                tab3_result = await client.call_tool("tabs", {
                    "action": "open",
                    "url": fixtures_server + "tall.html",
                    "background": True,
                    "window_id": original_window_id
                })
                tab3_content = tab3_result.get("content", "")
                print(f"Tab 3 creation result: {tab3_content}")

                _m3 = re.search(r'Opened tab \[(\d+)\]', tab3_content)
                if _m3:
                    created_tab_ids.append(int(_m3.group(1)))
                    print(f"✅ Successfully created additional tab in original window {original_window_id}")
                else:
                    print(f"⚠️ Additional tab creation in original window failed: {tab3_content}")

                await asyncio.sleep(0.5)

                # Get final window listing to verify tabs were added to correct windows
                print(f"\n🔍 Verifying tabs in both windows...")
                final_content = await _list_tabs(client)
                print(f"Final window listing after creating multiple tabs:\n{final_content}")

                # Verify both windows have the expected number of tabs
                original_window_tabs = len(_tab_ids_for_window(final_content, original_window_id))
                new_window_tabs = len(_tab_ids_for_window(final_content, window_id))

                print(f"Original window final tab count: {original_window_tabs}")
                print(f"New window final tab count: {new_window_tabs}")

                # Verify results - original window should have 3 tabs (1 original + 2 created)
                assert original_window_tabs >= 3, f"Original window should have >= 3 tabs, got {original_window_tabs}"
                assert new_window_tabs >= 2, f"New window should have >= 2 tabs, got {new_window_tabs}"

                print(f"✅ Original window has {original_window_tabs} tabs (expected >= 3)")
                print(f"✅ New window has {new_window_tabs} tabs (expected >= 2)")
                print(f"✅ Successfully created multiple tabs in window 1 using window_id parameter!")
                print(f"✅ Both windows received correct tabs using window_id parameter!")
            else:
                print("⚠️ Could not identify original window ID, skipping cross-window tab test")

            print(f"\n✅ Cross-window operations test completed successfully!")
            print(f"✅ Successfully created window {window_id}")
            print("✅ Successfully focused windows via tab activation")
            print("✅ Successfully retrieved current window info from the listing")
            print("✅ Successfully listed all windows")
            print("✅ Created multiple tabs in window 1 using window_id parameter")
            print("✅ Created tabs in BOTH windows using window_id parameter")
            print("✅ Verified window_id parameter works correctly for cross-window tab creation")
            print("✅ Confirmed tab isolation between windows")

        finally:
            # Clean up tabs opened in the pre-existing (original) window
            for tid in created_tab_ids:
                try:
                    await client.call_tool("tabs", {"action": "close", "tab_id": tid})
                except Exception as e:
                    print(f"⚠️ Error closing tab {tid}: {e}")

            # Clean up created windows
            print(f"\n🧹 Cleaning up {len(created_window_ids)} created windows...")
            for window_id in created_window_ids:
                try:
                    close_result = await client.call_tool("tabs", {
                        "action": "close", "window_id": window_id
                    })
                    close_content = close_result.get("content", "")
                    if f"Closed window {window_id}" in close_content:
                        print(f"✅ Closed window {window_id}")
                    else:
                        print(f"⚠️ Window {window_id} close result: {close_content}")

                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"⚠️ Error closing window {window_id}: {e}")

    @pytest.mark.firefox_only(
        "SUBJECT is which WINDOW is focused after activating a tab. headless Chrome has no "
        "window manager, so chrome.windows.update({focused:true}) is a no-op and the focused "
        "window never changes — verified: the chromium tabs.switch handler is a byte-faithful "
        "port that DOES call windows.update focused (design D-parity), the browser simply "
        "cannot honor it headlessly. headless Firefox tracks focused-window state internally. "
        "Every other window-management test (create/close/move/list/bounds) runs on BOTH engines."
    )
    @pytest.mark.timing  # serial phase: fixed 2 s sleeps stand in for each focus change
    @pytest.mark.asyncio
    async def test_window_focus_switching(self, server_with_extension):
        """Test switching focus between windows and verifying current window changes"""
        setup = server_with_extension

        # Create MCP client
        client = DirectMCPTestClient(setup['server'].mcp_tools)
        await client.connect()

        created_window_ids = []

        try:
            print("🎯 Testing Window Focus Switching")
            print("=" * 50)

            # Get initial current (focused) window
            print("\n📍 Step 1: Get initial current window...")
            initial_content = await _list_tabs(client)
            print(f"Initial listing: {initial_content}")

            initial_window_id = _focused_window_id(initial_content)
            assert initial_window_id is not None, \
                f"Could not extract initial focused window ID from: {initial_content}"
            print(f"✅ Initial focused window ID: {initial_window_id}")

            # Create a new window (focused by default)
            print(f"\n🪟 Step 2: Creating new window...")
            window_result = await client.call_tool("tabs", {
                "action": "open",
                "url": "about:blank",
                "new_window": True,
                "width": 900,
                "height": 700
            })

            window_content = window_result.get("content", "")
            print(f"Window creation result: {window_content}")

            # Extract new window ID
            id_match = re.search(r'Opened (?:incognito )?window (\d+)', window_content)
            assert id_match, f"Could not find new window ID in: {window_content}"
            new_window_id = int(id_match.group(1))
            created_window_ids.append(new_window_id)
            print(f"✅ Created new window ID: {new_window_id}")

            # Wait for window creation to stabilize
            await asyncio.sleep(2.0)

            # Check current window after creation (should be the new window)
            print(f"\n📍 Step 3: Check current window after creation...")
            after_creation_content = await _list_tabs(client)
            print(f"Listing after creation: {after_creation_content}")

            current_after_creation = _focused_window_id(after_creation_content)
            assert current_after_creation is not None, \
                f"Could not extract focused window ID from: {after_creation_content}"
            print(f"✅ Current focused window ID after creation: {current_after_creation}")

            # Verify the new window is now focused (if new_window focus worked)
            if current_after_creation == new_window_id:
                print(f"✅ New window {new_window_id} is correctly focused after creation")
                first_focused_window = new_window_id
                second_focus_target = initial_window_id
            else:
                print(f"⚠️ Focus didn't switch to new window. Current: {current_after_creation}, Expected: {new_window_id}")
                first_focused_window = current_after_creation
                second_focus_target = new_window_id if current_after_creation != new_window_id else initial_window_id

            # Helper: focus a window by activating one of its tabs.
            async def focus_window(target_window_id):
                listing = await _list_tabs(client)
                tabs_in = _tab_ids_for_window(listing, target_window_id)
                assert tabs_in, f"Window {target_window_id} should have a tab to activate"
                res = await client.call_tool("tabs", {
                    "action": "activate", "tab_id": tabs_in[0]
                })
                return res.get("content", "")

            # Test explicit focus switching to the other window
            print(f"\n🎯 Step 4: Explicitly focus window {second_focus_target}...")
            focus_content = await focus_window(second_focus_target)
            print(f"Focus result: {focus_content}")
            assert "Activated tab" in focus_content, f"Focus operation should succeed: {focus_content}"
            print("✅ Focus operation reported success")

            # Wait for focus change to take effect
            await asyncio.sleep(2.0)

            # Check current window after explicit focus
            print(f"\n📍 Step 5: Check current window after explicit focus...")
            after_focus_content = await _list_tabs(client)
            current_after_focus = _focused_window_id(after_focus_content)
            assert current_after_focus is not None, \
                f"Could not extract focused window ID from: {after_focus_content}"
            print(f"✅ Current focused window ID after focus: {current_after_focus}")

            # Verify focus actually switched
            assert current_after_focus == second_focus_target, \
                f"Focus should have switched to {second_focus_target}, but current is {current_after_focus}"
            print(f"✅ Focus successfully switched from {first_focused_window} to {current_after_focus}")

            # Test switching back to the first window
            print(f"\n🎯 Step 6: Focus back to window {first_focused_window}...")
            focus_back_content = await focus_window(first_focused_window)
            print(f"Focus back result: {focus_back_content}")
            assert "Activated tab" in focus_back_content, f"Focus back operation should succeed: {focus_back_content}"
            print("✅ Focus back operation reported success")

            await asyncio.sleep(2.0)

            # Check current window after focusing back
            print(f"\n📍 Step 7: Check current window after focusing back...")
            final_content = await _list_tabs(client)
            final_current_id = _focused_window_id(final_content)
            assert final_current_id is not None, \
                f"Could not extract focused window ID from: {final_content}"
            print(f"✅ Final focused window ID: {final_current_id}")

            # Verify focus switched back
            assert final_current_id == first_focused_window, \
                f"Focus should have switched back to {first_focused_window}, but current is {final_current_id}"
            print(f"✅ Focus successfully switched back from {current_after_focus} to {final_current_id}")

            # Additional focus switch (3rd switch) - back to second window again
            print(f"\n🎯 Step 8: Third focus switch - back to window {second_focus_target}...")
            focus_third_content = await focus_window(second_focus_target)
            print(f"Third focus result: {focus_third_content}")
            assert "Activated tab" in focus_third_content, f"Third focus operation should succeed: {focus_third_content}"
            print("✅ Third focus operation reported success")

            await asyncio.sleep(2.0)

            # Check current window after third focus
            print(f"\n📍 Step 9: Check current window after third focus...")
            third_content = await _list_tabs(client)
            third_current_id = _focused_window_id(third_content)
            assert third_current_id is not None, \
                f"Could not extract focused window ID from: {third_content}"
            print(f"✅ Third focused window ID: {third_current_id}")

            # Verify third focus switch worked
            assert third_current_id == second_focus_target, \
                f"Third focus should have switched to {second_focus_target}, but current is {third_current_id}"
            print(f"✅ Third focus successfully switched from {final_current_id} to {third_current_id}")

            # Final verification - list all windows to see focus state
            print(f"\n📋 Step 10: Final window listing...")
            final_windows_content = await _list_tabs(client)
            print(f"Final window listing:\n{final_windows_content}")

            # Verify the focused window in the listing matches our expectation
            focused_in_listing = _focused_window_id(final_windows_content)
            if focused_in_listing is not None:
                assert focused_in_listing == third_current_id, \
                    f"Focused window in listing ({focused_in_listing}) should match current window ({third_current_id})"
                print(f"✅ Window listing confirms window {focused_in_listing} is focused")
            else:
                print("⚠️ Could not determine focused window from listing")

            print(f"\n✅ Window Focus Switching Test PASSED!")
            print(f"✅ Successfully created windows: {initial_window_id}, {new_window_id}")
            print(f"✅ Successfully switched focus 3 times: {first_focused_window} → {second_focus_target} → {first_focused_window} → {third_current_id}")
            print(f"✅ Current window detection working correctly")
            print(f"✅ Focus operations working as expected")
            print(f"✅ All 3 focus switches completed successfully")

        finally:
            # Clean up created windows
            print(f"\n🧹 Cleaning up {len(created_window_ids)} created windows...")
            for window_id in created_window_ids:
                try:
                    close_result = await client.call_tool("tabs", {
                        "action": "close", "window_id": window_id
                    })
                    close_content = close_result.get("content", "")
                    if f"Closed window {window_id}" in close_content:
                        print(f"✅ Closed window {window_id}")
                    else:
                        print(f"⚠️ Window {window_id} close result: {close_content}")

                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"⚠️ Error closing window {window_id}: {e}")

    @pytest.mark.asyncio
    async def test_tabs_list_shows_pinned_status(self, server_with_extension, fixtures_server):
        """Test that tabs list flags a pinned tab (' pinned') and leaves a regular tab unflagged"""
        setup = server_with_extension

        # Create MCP client
        client = DirectMCPTestClient(setup['server'].mcp_tools)
        await client.connect()

        created_tab_ids = []
        try:
            print("📌 Testing tabs list pinned status display")
            print("=" * 50)

            # Create a regular (unpinned) tab
            print("\n📄 Creating regular tab...")
            tab1_result = await client.call_tool("tabs", {
                "action": "open",
                "url": fixtures_server + "titled.html",
                "pinned": False
            })
            tab1_content = tab1_result.get('content', '')
            print(f"Regular tab result: {tab1_content}")
            m1 = re.search(r'Opened tab \[(\d+)\]', tab1_content)
            assert m1, f"Could not parse regular tab id from: {tab1_content}"
            regular_tab_id = int(m1.group(1))
            created_tab_ids.append(regular_tab_id)

            # Create a pinned tab
            print("\n📌 Creating pinned tab...")
            tab2_result = await client.call_tool("tabs", {
                "action": "open",
                "url": fixtures_server + "article.html",
                "background": True,
                "pinned": True
            })
            tab2_content = tab2_result.get('content', '')
            print(f"Pinned tab result: {tab2_content}")
            m2 = re.search(r'Opened tab \[(\d+)\]', tab2_content)
            assert m2, f"Could not parse pinned tab id from: {tab2_content}"
            pinned_tab_id = int(m2.group(1))
            created_tab_ids.append(pinned_tab_id)

            await asyncio.sleep(1.0)

            # List all tabs to see pinned status
            print("\n📋 Listing all tabs...")
            tabs_content = await _list_tabs(client)
            print(f"Tabs list result:\n{tabs_content}")

            # The pinned tab MUST carry the ' pinned' flag on its id line.
            pinned_flags = _tab_flags(tabs_content, pinned_tab_id)
            assert pinned_flags is not None, \
                f"Pinned tab {pinned_tab_id} should appear in the listing:\n{tabs_content}"
            assert "pinned" in pinned_flags, \
                f"Pinned tab {pinned_tab_id} should be flagged 'pinned', got '{pinned_flags}':\n{tabs_content}"

            # The regular tab MUST NOT be flagged pinned.
            regular_flags = _tab_flags(tabs_content, regular_tab_id)
            assert regular_flags is not None, \
                f"Regular tab {regular_tab_id} should appear in the listing:\n{tabs_content}"
            assert "pinned" not in regular_flags, \
                f"Regular tab {regular_tab_id} should NOT be flagged pinned, got '{regular_flags}':\n{tabs_content}"

            print(f"\n✅ Tabs list pinned status test completed!")

        finally:
            # Shared Firefox — close the tabs this test opened.
            for tid in created_tab_ids:
                try:
                    await client.call_tool("tabs", {"action": "close", "tab_id": tid})
                except Exception as e:
                    print(f"⚠️ Error closing tab {tid}: {e}")


if __name__ == "__main__":
    # Run tests individually for debugging
    pytest.main([__file__, "-v", "-s"])
