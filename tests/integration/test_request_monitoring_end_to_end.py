"""
Web Request Monitoring End-to-End Tests

Comprehensive functional tests for web request monitoring operations through the
MCP server and WebSocket protocol with real Firefox extension.
"""

import pytest
import pytest_asyncio
import asyncio
import json
import os
import re

# Set up consistent imports

# Import project modules

# Import test utilities
from mcp_client_harness import DirectMCPTestClient


async def poll_until(predicate, timeout, interval=0.25):
    """Bounded poll for an async condition — the suite's preferred pattern over a bare
    sleep (mirrors poll_until in test_ws_token_bootstrap.py). Returns the predicate's
    final value so the CALLER's existing assertions still supply the diagnostics and
    the verdict. Every timeout here is >= the fixed sleep it replaced, so a poll can
    never be more flaky than the sleep was — it just stops waiting once the condition
    holds instead of always burning the full duration.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if loop.time() >= deadline:
            return value
        await asyncio.sleep(interval)


FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "fixtures")


def parse_list_result(list_result):
    """Parse a `requests action=list` tool result -> (total_requests, requests).
    Mirrors the inline parsing the assertions below do."""
    if not isinstance(list_result, dict):
        return 0, []
    content = list_result.get('content', '')
    try:
        data = json.loads(content)
        return data.get('total_requests', 0), data.get('requests', [])
    except json.JSONDecodeError:
        match = re.search(r'"total_requests":\s*(\d+)', content or '')
        return (int(match.group(1)) if match else 0), []


class TestRequestMonitoringEndToEnd:
    """End-to-end tests for web request monitoring functionality"""

    @pytest_asyncio.fixture
    async def full_monitoring_system(self, server_with_extension):
        """Complete monitoring testing system with MCP client"""
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
            'mcp_port': mcp_port
        }

        # Cleanup handled by server_with_extension fixture

    @pytest.mark.asyncio
    async def test_request_monitoring_with_firefox(self, full_monitoring_system):
        """Test complete request monitoring workflow with real Firefox extension"""
        system = full_monitoring_system
        server = system['server']
        mcp_client = system['mcp_client']
        firefox = system['firefox']

        await mcp_client.connect()

        print("\n🔍 Testing web request monitoring with real Firefox...")

        # Test URL patterns that the extension can monitor
        test_url_patterns = ["https://example.org/*", "*"]

        try:
            # Step 1: Start monitoring
            print("🔍 Starting request monitoring...")
            start_result = await mcp_client.call_tool("requests", {
                "action": "start",
                "url_patterns": test_url_patterns,
                "options": {
                    "capture_request_bodies": True,
                    "capture_response_bodies": True,
                    "max_body_size": 50000,
                    "content_types_to_capture": ["text/html", "application/json"]
                }
            })

            print(f"Start monitoring result: {start_result}")

            # Extract monitor_id from result
            monitor_id = None
            if isinstance(start_result, dict):
                start_content = start_result.get('content', '')
                if 'monitor_id' in start_content:
                    # Parse JSON from content
                    try:
                        start_data = json.loads(start_content)
                        monitor_id = start_data.get('monitor_id')
                    except json.JSONDecodeError:
                        # Try to extract from string
                        import re
                        match = re.search(r'"monitor_id":\s*"([^"]+)"', start_content)
                        if match:
                            monitor_id = match.group(1)

            assert monitor_id, f"No monitor_id found in response: {start_result}"
            print(f"✅ Monitoring started with ID: {monitor_id}")

            # Step 2: Trigger some web requests by creating a tab and navigating
            print("🌐 Creating tab and navigating to test URL...")
            create_result = await mcp_client.call_tool("tabs", {
                "action": "open",
                "url": "https://example.org/"
            })
            print(f"📄 Tab created: {create_result}")
            # Capture the opened tab id so the finally can close it (.tmp/dev-docs/testing.md cleanup
            # rule). A conditional `import re` lives later in THIS function (:139), which
            # makes bare `re` function-local throughout — use a distinct alias to avoid
            # UnboundLocalError.
            import re as _re
            _tab_m = _re.search(r'Opened tab \[(\d+)\]', create_result.get('content', ''))
            created_tab_id = int(_tab_m.group(1)) if _tab_m else None

            # Wait for requests to be captured. Bounded poll (<=5s, the old fixed
            # sleep's duration) for exactly what is asserted below — at least one
            # captured request — falling through to that assertion on timeout.
            print("⏳ Polling (<=5s) for requests to be captured...")

            async def _requests_captured():
                probe = await mcp_client.call_tool("requests", {
                    "action": "list",
                    "monitor_id": monitor_id
                })
                probe_total, probe_requests = parse_list_result(probe)
                return probe_total > 0 and bool(probe_requests)

            await poll_until(_requests_captured, timeout=5.0)

            # Step 3: List captured requests
            print("📋 Listing captured requests...")
            list_result = await mcp_client.call_tool("requests", {
                "action": "list",
                "monitor_id": monitor_id
            })

            print(f"List result: {list_result}")

            # Parse the list result
            total_requests = 0
            requests_data = []

            if isinstance(list_result, dict):
                list_content = list_result.get('content', '')
                try:
                    list_data = json.loads(list_content)
                    total_requests = list_data.get('total_requests', 0)
                    requests_data = list_data.get('requests', [])
                except json.JSONDecodeError:
                    # Handle string responses
                    if 'total_requests' in list_content:
                        import re
                        match = re.search(r'"total_requests":\s*(\d+)', list_content)
                        if match:
                            total_requests = int(match.group(1))

            print(f"📊 Captured {total_requests} requests")

            # Capture MUST work — that is the whole point of this test. A zero here
            # used to hit the else below, print "No requests were captured", and PASS.
            # list_captured summaries come from the webRequest listeners, which fire
            # even for cache hits, so a navigation must yield >=1 entry.
            assert total_requests > 0 and requests_data, (
                f"No requests captured (total_requests={total_requests}); monitoring "
                f"must capture the example.org navigation. Raw list result: {list_result}")

            # Verify we captured some requests
            if total_requests > 0 and requests_data:
                print("🎯 Sample captured requests:")
                for i, req in enumerate(requests_data[:3]):  # Show first 3
                    print(f"  {i+1}. {req.get('method', 'UNKNOWN')} {req.get('url', 'NO_URL')} "
                          f"-> {req.get('status_code', 'NO_CODE')} ({req.get('duration_ms', 0)}ms)")

                # Step 4: Get content for a specific request
                test_request = requests_data[0]
                print(f"🔍 Getting content for request: {test_request.get('request_id', 'NO_ID')}")

                content_result = await mcp_client.call_tool("requests", {
                    "action": "get",
                    "monitor_id": monitor_id,
                    "request_id": test_request.get('request_id'),
                    "include_binary": False
                })

                print(f"📄 Content result: {content_result}")

                # Verify response body content was captured
                if isinstance(content_result, dict) and content_result.get('content'):
                    try:
                        content_data = json.loads(content_result['content'])
                        response_body = content_data.get('response_body', {})

                        # Check if response body was captured
                        if response_body.get('included'):
                            response_content = response_body.get('content', '')
                            print(f"✅ Response body captured! Content length: {len(response_content)} chars")
                            print(f"   Content type: {response_body.get('content_type', 'unknown')}")
                            print(f"   Truncated: {response_body.get('truncated', False)}")

                            # Verify it contains actual HTML content from example.org
                            if 'example.org' in response_content.lower() or 'html' in response_content.lower():
                                print("✅ Response body contains expected content from example.org")
                            else:
                                print(f"⚠️  Response body doesn't contain expected content. First 200 chars: {response_content[:200]}")
                        else:
                            print("⚠️  Response body was not captured")
                            print(f"   Reason: {response_body.get('note', 'Unknown')}")

                    except json.JSONDecodeError as e:
                        print(f"⚠️  Could not parse content result: {e}")

                    print("✅ Content retrieved successfully!")
                else:
                    print("⚠️  Content retrieval may not have worked as expected")

        except Exception as e:
            print(f"❌ Test error: {e}")
            raise

        finally:
            # Step 5: Stop monitoring
            print("🛑 Stopping monitoring...")
            try:
                if 'monitor_id' in locals():
                    stop_result = await mcp_client.call_tool("requests", {
                        "action": "stop",
                        "monitor_id": monitor_id
                    })
                    print(f"✅ Monitoring stopped: {stop_result}")
                else:
                    print("⚠️  No monitor_id available for stopping")

            except Exception as e:
                print(f"⚠️  Error stopping monitoring: {e}")

            # Close the tab this test opened (.tmp/dev-docs/testing.md cleanup rule).
            try:
                if locals().get('created_tab_id'):
                    await mcp_client.call_tool("tabs", {
                        "action": "close", "tab_id": created_tab_id})
            except Exception as e:
                print(f"⚠️  Error closing tab: {e}")

        print("✅ Request monitoring Firefox test completed")

    @pytest.mark.asyncio
    async def test_monitoring_error_scenarios(self, full_monitoring_system):
        """Test error scenarios in monitoring APIs"""
        system = full_monitoring_system
        mcp_client = system['mcp_client']

        await mcp_client.connect()

        print("\n❌ Testing error scenarios...")

        # Test 1: Empty URL patterns
        result = await mcp_client.call_tool("requests", {
            "action": "start",
            "url_patterns": []
        })
        print(f"Empty patterns result: {result}")
        # Should get an error
        if isinstance(result, dict):
            content = result.get('content', '')
            assert 'error' in content.lower() or 'required' in content.lower()
        print("✅ Empty URL patterns properly rejected")

        # Test 2: Invalid monitor ID
        result = await mcp_client.call_tool("requests", {
            "action": "list",
            "monitor_id": "invalid_monitor_id"
        })
        print(f"Invalid monitor result: {result}")
        # Should handle gracefully
        print("✅ Invalid monitor ID handled")

        # Test 3: Invalid request for content
        result = await mcp_client.call_tool("requests", {
            "action": "get",
            "monitor_id": "invalid_monitor",
            "request_id": "invalid_request"
        })
        print(f"Invalid content request result: {result}")
        # Should handle gracefully
        print("✅ Invalid content request handled")

        print("✅ Error scenario testing completed")

    @pytest.mark.firefox_only(
        "asserts capture of an HTML DOCUMENT response body via the gecko StreamFilter; "
        "chromium body capture is fetch/XHR-only (design D5), document bodies are not "
        "capturable without chrome.debugger")
    @pytest.mark.asyncio
    async def test_response_body_capture_verification(self, full_monitoring_system,
                                                      fixtures_server):
        """A monitored DOCUMENT load's body reaches `get` whole: included:true, the
        fixture's bytes, and its exact size.

        It starts the monitor as soon as the browser is connected and loads the page with
        navigate(wait="load"), in the browser's first seconds, when Firefox's parent
        process has little idle time. Firefox then delivers the non-blocking
        onHeadersReceived late and in batches, so the stream filter can finish before the
        body's Content-Type is known; background.js parks the bytes until it is. The
        fixture is a static file served with a Content-Length, so the size is asserted
        exactly.
        """
        system = full_monitoring_system
        mcp_client = system['mcp_client']
        page_url = fixtures_server + "known-length.html"
        with open(os.path.join(FIXTURES_DIR, "known-length.html"), "rb") as f:
            expected = f.read()

        await mcp_client.connect()
        monitor_id = None
        try:
            start = await mcp_client.call_tool("requests", {
                "action": "start",
                "url_patterns": [f"{fixtures_server}*"],
                "options": {
                    "capture_response_bodies": True,
                    "max_body_size": 50000,
                    "content_types_to_capture": ["text/html", "application/json"],
                },
            })
            monitor_id = json.loads(start.get('content', '{}')).get('monitor_id')
            assert monitor_id, f"No monitor_id in: {start}"

            opened = await mcp_client.call_tool("tabs", {"action": "open", "url": "about:blank"})
            tab_match = re.search(r'Opened tab \[(\d+)\]', opened.get('content', ''))
            assert tab_match, f"Could not parse tab id from: {opened}"
            tab_id = int(tab_match.group(1))
            nav = await mcp_client.call_tool("navigate", {
                "tab_id": tab_id, "url": page_url, "wait": "load"})
            assert not nav.get('isError'), nav

            # onCompleted lists the request, and the body is committed by then; poll for
            # the listing rather than guess a delay.
            seen = {}

            async def document_listed():
                seen['listed'] = json.loads((await mcp_client.call_tool("requests", {
                    "action": "list", "monitor_id": monitor_id}))['content'])
                seen['doc'] = next((r for r in seen['listed'].get('requests', [])
                                    if r.get('url') == page_url), None)
                return seen['doc'] is not None

            await poll_until(document_listed, timeout=10.0)
            assert seen['doc'], f"{page_url} not in the captured list: {seen['listed']}"

            got = json.loads((await mcp_client.call_tool("requests", {
                "action": "get", "monitor_id": monitor_id,
                "request_id": seen['doc']['request_id']}))['content'])
            rb = got.get('response_body') or {}
            # The note names the recorded reason a body is missing (type_excluded,
            # filter_error, ...), so it is the diagnosis when this fails.
            assert rb.get('included') is True, f"document body not captured: {rb}"
            assert rb.get('content') == expected.decode('utf-8'), \
                f"captured body is not {page_url}: {(rb.get('content') or '')[:200]}"
            assert rb.get('size_bytes') == len(expected), rb
            assert rb.get('truncated') is False, rb
            assert (rb.get('content_type') or '').startswith('text/html'), rb
        finally:
            if monitor_id:
                try:
                    await mcp_client.call_tool("requests", {
                        "action": "stop", "monitor_id": monitor_id})
                except Exception as e:
                    print(f"⚠️  Error stopping monitoring: {e}")
            # Close the tab this test opened (.tmp/dev-docs/testing.md cleanup rule).
            try:
                if locals().get('tab_id'):
                    await mcp_client.call_tool("tabs", {
                        "action": "close", "tab_id": tab_id})
            except Exception as e:
                print(f"⚠️  Error closing tab: {e}")

    @pytest.mark.firefox_only(
        "drives body capture through page_eval-issued fetch + asserts included:true; on "
        "chromium page_eval is EVAL_UNAVAILABLE (userScripts toggle, not headlessly "
        "grantable) and document/fetch body capture differs (design D5)")
    @pytest.mark.asyncio
    async def test_body_correlation_attaches_captured_content(self, full_monitoring_system,
                                                              fixtures_server):
        """Deterministic (local-fixture) end-to-end proof that a monitored request's
        body really reaches `get` with included:true.

        Named for the URL+method+status correlation bug it originally caught (captures
        were keyed by the content script's own req_<ts>_<rand> ids and correlated
        against a never-set `response_status_code`). That whole path is gone: bodies are
        captured at the network layer by a StreamFilter keyed on the webRequest
        requestId and found by direct lookup. The JS tests fire the registered listeners
        against a stubbed browser; this test is the one that drives arming -> capture ->
        retrieval through a real StreamFilter and Firefox's own event order.

        The fetch goes out the moment navigate(wait="load") returns, while the page load
        still keeps Firefox busy, so its onHeadersReceived can reach the extension after
        the small body has finished streaming: the order background.js parks bodies for.
        """
        system = full_monitoring_system
        mcp_client = system['mcp_client']
        await mcp_client.connect()
        monitor_id = None
        try:
            start = await mcp_client.call_tool("requests", {
                "action": "start",
                "url_patterns": [f"{fixtures_server}*"],
                "options": {
                    "capture_response_bodies": True,
                    "content_types_to_capture": ["text/html", "application/json"],
                },
            })
            monitor_id = json.loads(start.get('content', '{}')).get('monitor_id')
            assert monitor_id, f"No monitor_id in: {start}"

            opened = await mcp_client.call_tool("tabs", {"action": "open", "url": "about:blank"})
            tab_match = re.search(r'Opened tab \[(\d+)\]', opened.get('content', ''))
            assert tab_match, f"Could not parse tab id from: {opened}"
            tab_id = int(tab_match.group(1))
            nav = await mcp_client.call_tool("navigate", {
                "tab_id": tab_id, "url": fixtures_server + "titled.html", "wait": "load"})
            assert not nav.get('isError'), nav

            # Fetch a DIFFERENT fixture from the page so the captured body is
            # unambiguous; page_eval awaits the returned promise.
            ev = await mcp_client.call_tool("page_eval", {
                "tab_id": tab_id,
                "code": f"fetch('{fixtures_server}article.html')"
                        ".then(r => r.text()).then(t => 'fetched:' + t.length)",
            })
            assert "fetched:" in ev.get('content', ''), f"fixture fetch failed: {ev}"

            # onCompleted and the capture message reach the background some time after
            # the fetch resolves: poll for the attached body rather than guess a delay.
            seen = {}

            async def body_attached():
                seen['listed'] = json.loads((await mcp_client.call_tool("requests", {
                    "action": "list", "monitor_id": monitor_id}))['content'])
                seen['article'] = next((r for r in seen['listed'].get('requests', [])
                                        if r.get('url', '').endswith('article.html')), None)
                if not seen['article']:
                    return False
                seen['got'] = json.loads((await mcp_client.call_tool("requests", {
                    "action": "get", "monitor_id": monitor_id,
                    "request_id": seen['article']['request_id']}))['content'])
                return (seen['got'].get('response_body') or {}).get('included') is True

            await poll_until(body_attached, timeout=15.0)
            listed, article = seen['listed'], seen['article']
            assert article, f"article.html not in captured list: {listed}"

            got = seen['got']
            rb = got.get('response_body') or {}
            assert rb.get('included') is True, \
                f"correlation did not attach the captured body: {rb}"
            assert 'WebSocket Reconnection' in (rb.get('content') or ''), \
                f"captured body is not article.html: {(rb.get('content') or '')[:200]}"
            assert rb.get('size_bytes', 0) > 100, rb.get('size_bytes')
        finally:
            if monitor_id:
                try:
                    await mcp_client.call_tool("requests", {
                        "action": "stop", "monitor_id": monitor_id})
                except Exception as e:
                    print(f"⚠️  Error stopping monitoring: {e}")
            # Close the tab this test opened (.tmp/dev-docs/testing.md cleanup rule).
            try:
                if locals().get('tab_id'):
                    await mcp_client.call_tool("tabs", {
                        "action": "close", "tab_id": tab_id})
            except Exception as e:
                print(f"⚠️  Error closing tab: {e}")

    @pytest.mark.asyncio
    async def test_monitoring_api_registration(self, full_monitoring_system):
        """Test that the consolidated monitoring API is properly registered"""
        system = full_monitoring_system
        server = system['server']

        print("\n🔍 Testing API registration...")

        # Get all tools
        try:
            tool_names = {t.name for t in await server.mcp_tools.mcp.list_tools()}

            # The four old monitoring tools are now one consolidated `requests` tool
            assert "requests" in tool_names, f"Missing consolidated tool: requests"

            removed_tools = [
                "requests_start",
                "requests_stop",
                "requests_list",
                "requests_get_content"
            ]
            for old_name in removed_tools:
                assert old_name not in tool_names, f"Removed tool {old_name} still registered"

            print("✅ Consolidated monitoring API properly registered")

        except Exception as e:
            print(f"Error checking tools: {e}")
            raise


# Utility test for environment verification
@pytest.mark.asyncio
async def test_monitoring_test_environment():
    """The harness this module drives must expose the interface every test relies on.

    Previously this only printed and swallowed ImportError, so a broken harness still
    passed. The imports must succeed and DirectMCPTestClient must expose the async
    connect()/call_tool() coroutines the fixtures and tests here call.
    """
    import test_imports  # noqa: F401  (path setup; must import cleanly)
    from mcp_client_harness import DirectMCPTestClient

    assert asyncio.iscoroutinefunction(DirectMCPTestClient.connect), \
        "DirectMCPTestClient.connect must be an async method"
    assert asyncio.iscoroutinefunction(DirectMCPTestClient.call_tool), \
        "DirectMCPTestClient.call_tool must be an async method"