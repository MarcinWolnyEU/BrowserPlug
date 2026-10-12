"""
Agent-experience integration tests (consolidated 9-tool surface).

These cover the browser-driving primitives of the BrowserPlug 2.0 tool surface and the
new capabilities added in response to the agent-experience feedback. The 9 tools
exercised here are: tabs, navigate, page_read, page_snapshot, page_act, page_eval,
page_screenshot, requests, server_info. Every tool returns
a formatted STRING except page_screenshot, which returns a CallToolResult with
an inline image.

Each test runs against a deterministic, login-free fixture page served locally by
the `fixtures_server` session fixture (see conftest.py). The fixtures reproduce the
exact hostile patterns from the feedback (reactive inputs, deferred modals, hidden
twins, frames, controlled React/Vue forms, silent-failure logins, lazy elements,
console noise) so each test maps to a specific feedback item. Two
additional fixtures stress encoding handling: a genuine Windows-1252 page (cp1252-only
punctuation) and a UTF-8 page mixing Arabic (RTL), Chinese (CJK), Hebrew (RTL) and
astral-plane emoji — the inputs most likely to break a UTF-8-assuming pipeline.

The end-to-end tests require a real Firefox + extension via the shared
`server_with_extension` fixture (function-scoped: each test gets its own fresh-profile
Firefox). They skip only when Firefox is not installed; a Firefox that starts but never
connects is a hard FAILURE (see conftest.py). The fixtures-server smoke tests run
anywhere.

Timing assertions are deliberately lenient for CI. Firefox runs headless in the
harness, so document.hasFocus() may be false; headless layout still happens, so we
assert content correctness regardless of any pageState/painted warnings — we do NOT
assert that an unpainted WARNING fires on healthy fixtures.
"""

import base64
import io
import json
import re
import asyncio
import socket
import threading
import urllib.error
import urllib.request

import pytest
import pytest_asyncio

import test_imports  # noqa: F401  (path setup)
from mcp_client_harness import DirectMCPTestClient

try:
    from PIL import Image
    _HAS_PIL = True
except Exception:  # pragma: no cover - Pillow is a test-only dependency
    _HAS_PIL = False


def _free_port():
    """Bind an ephemeral port, then release it — a (briefly) closed port for the
    'navigation to a dead address' cases."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def blackhole_server():
    """A TCP listener that completes the handshake but never sends a byte — a
    deterministic clone of the live condition it was written for (a half-working
    proxy that accepts SYN and never relays HTTP). Navigations to
    it hang until aborted, so every 'open' variant must report did-not-commit."""
    conns = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.5)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def accept_loop():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                conns.append(c)  # hold the connection open; never respond
            except socket.timeout:
                continue
            except OSError:
                break

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        stop.set()
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
        srv.close()


def _screenshot_blocks(res):
    """The content blocks of a page_screenshot CallToolResult (text summary + image)."""
    tr = res["content"]
    return getattr(tr, "content", tr)


def _screenshot_png(res):
    """Raw PNG bytes from a page_screenshot CallToolResult's image block."""
    img = next(b for b in _screenshot_blocks(res) if getattr(b, "type", None) == "image")
    return base64.b64decode(img.data)


def _screenshot_summary(res):
    return next(b.text for b in _screenshot_blocks(res) if getattr(b, "type", None) == "text")


def test_fixtures_server_serves_pages(fixtures_server):
    """The local fixtures server serves the fixture pages (no Firefox needed)."""
    with urllib.request.urlopen(fixtures_server + "form.html", timeout=5) as r:
        body = r.read().decode("utf-8")
    assert "BrowserPlug Form Fixture" in body
    assert 'id="real-save"' in body
    with urllib.request.urlopen(fixtures_server + "reactive.html", timeout=5) as r:
        assert "Reactive input fixture" in r.read().decode("utf-8")

    # New fixture pages for the consolidated 2.0 tool surface
    expected = {
        "article.html": "The Complete Guide to WebSocket Reconnection",
        "console-noise.html": "thrown-error C",
        "duplicate-controls.html": 'id="real-btn"',
        "many-identical-controls.html": 'id="save-all"',
        "lazy-element.html": "READY",
        "reload-replaces-text.html": "STATE:NEW",
        "silent-fail-login.html": "/api/login",
        "main-world-global.html": "main-world-value",
        "react-controlled-form.html": "vendor/react.production.min.js",
        "vue-controlled-form.html": "vendor/vue.global.prod.js",
        "i18n-multilang.html": "DISTINCTIVE_MARKER_I18N_PAGE",
        "lazy-shot.html": "DISTINCTIVE_MARKER_LAZY_SHOT",
        "element-shot.html": "DISTINCTIVE_MARKER_ELEMENT_SHOT",
        "revert-input.html": 'id="ctl"',
    }
    for page, marker in expected.items():
        with urllib.request.urlopen(fixtures_server + page, timeout=5) as r:
            assert r.status == 200, page
            assert marker in r.read().decode("utf-8"), f"{page} missing {marker!r}"

    # Vendor JS bundles must be served and be real JS, not error pages
    for vendor in ("vendor/react.production.min.js",
                   "vendor/react-dom.production.min.js",
                   "vendor/vue.global.prod.js"):
        with urllib.request.urlopen(fixtures_server + vendor, timeout=5) as r:
            body = r.read()
            assert r.status == 200 and len(body) > 10000, vendor
            assert b"<html" not in body[:500].lower(), f"{vendor} looks like an error page"


def test_fixtures_server_api_endpoints(fixtures_server):
    """POST /api/login always 401s with JSON; POST /api/echo echoes (no Firefox needed)."""
    login_req = urllib.request.Request(
        fixtures_server + "api/login",
        data=json.dumps({"email": "a@b.c", "password": "hunter2"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(login_req, timeout=5)
    err = exc_info.value
    assert err.code == 401
    assert err.headers.get("Content-Type") == "application/json"
    assert err.headers.get("Access-Control-Allow-Origin") == "*"
    assert json.loads(err.read().decode("utf-8")) == {"error": "invalid credentials"}

    echo_req = urllib.request.Request(
        fixtures_server + "api/echo",
        data=json.dumps({"hello": "world"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(echo_req, timeout=5) as r:
        assert r.status == 200
        assert r.headers.get("Content-Type") == "application/json"
        assert r.headers.get("Access-Control-Allow-Origin") == "*"
        assert json.loads(r.read().decode("utf-8")) == {"hello": "world"}


def test_fixtures_server_dynamic_routes(fixtures_server):
    """The dynamic routes backing the navigation/screenshot tests work (no Firefox)."""
    # The redirect chain resolves through to titled.html.
    with urllib.request.urlopen(fixtures_server + "redirect-chain", timeout=5) as r:
        assert r.status == 200
        assert r.geturl().endswith("titled.html")
    # /slow-image returns a real PNG (magic bytes), used as the lazy below-fold image.
    with urllib.request.urlopen(fixtures_server + "slow-image?ms=0&c=2266dd", timeout=5) as r:
        body = r.read()
        assert r.headers.get("Content-Type") == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n", "slow-image is not a PNG"


def test_fixtures_server_encoding_pages_are_byte_correct(fixtures_server):
    """The encoding fixtures are served with their real bytes (no Firefox needed).

    encoding-windows-1252.html MUST be genuine cp1252 (containing 0x80-0x9F bytes
    that are NOT valid standalone UTF-8) and declare it via <meta charset>. The
    i18n page MUST be valid UTF-8 carrying Arabic/Chinese/Hebrew/astral-plane text.
    These are the inputs most likely to break a pipeline that assumes one codec.
    """
    # Windows-1252 page: served verbatim as cp1252 bytes.
    with urllib.request.urlopen(fixtures_server + "encoding-windows-1252.html", timeout=5) as r:
        raw = r.read()
    # Genuinely cp1252: at least one 0x80-0x9F byte present...
    assert any(0x80 <= b <= 0x9F for b in raw), "no cp1252-only bytes — file is not really cp1252"
    # ...and NOT valid UTF-8 (a UTF-8-assuming reader would crash on it).
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    # Decodes cleanly as cp1252 with the meta declaration and the smart-quote content.
    text = raw.decode("cp1252")
    assert 'charset="windows-1252"' in text
    assert "DISTINCTIVE_MARKER_CP1252_PAGE" in text
    assert "“Smart” quotes" in text       # curly double quotes “ ”
    assert "em-dash —" in text                  # em dash —
    assert "euro €" in text                     # €
    assert "Schrödinger" in text                # ö (cp1252 0xF6)

    # i18n page: valid UTF-8 with all four scripts/byte-widths intact.
    with urllib.request.urlopen(fixtures_server + "i18n-multilang.html", timeout=5) as r:
        body = r.read().decode("utf-8")  # must not raise
    assert "DISTINCTIVE_MARKER_I18N_PAGE" in body
    assert "مرحبا" in body  # Arabic "مرحبا"
    assert "你好" in body                     # Chinese "你好"
    assert "שָׁלוֹם" in body  # Hebrew "שָׁלוֹם"
    assert "\U0001f600" in body                       # 😀 astral-plane (4-byte UTF-8)


@pytest.mark.slow
class TestAgentExperience:
    """End-to-end browser-driving tests against local fixtures (require Firefox).

    All tests use the consolidated 9-tool surface. The `mcp` fixture wraps the live
    server's BrowserPlugTools via DirectMCPTestClient; call_tool returns a dict whose
    'content' is the tool's raw return value (a formatted string, or a CallToolResult for
    page_screenshot).
    """

    @pytest_asyncio.fixture
    async def mcp(self, server_with_extension):
        client = DirectMCPTestClient(server_with_extension['server'].mcp_tools)
        # Tag the client with the engine so the verification helpers can pick a
        # capability that works on this browser. On headless Chromium page_eval is
        # unavailable (the "Allow user scripts" toggle is un-grantable → the
        # extension returns EVAL_UNAVAILABLE), so a chromium run VERIFIES DOM state
        # via page_read / page_snapshot / page_act instead of page_eval. The
        # feature under test (fill/click/snapshot/iframe/waits) still runs on both.
        client.engine = server_with_extension['engine']
        await client.connect()
        yield client
        await client.disconnect()

    # ---- helpers ------------------------------------------------------------

    async def _open(self, mcp, url):
        """Open `url` in a new tab via tabs(action='open'); return its tab_id.

        The consolidated `tabs` tool reports "Opened tab [N] in window M: URL". This
        does ONE page load (no re-navigation), which matters for fixtures whose
        content changes between loads (e.g. reload-replaces-text via sessionStorage).
        NOTE: headless Firefox does not reliably switch the OS-active tab on open, so
        callers must address this tab by its returned tab_id, not via the active tab.
        """
        res = await mcp.call_tool("tabs", {"action": "open", "url": url})
        assert not res.get('isError'), res
        content = res['content']
        m = re.search(r'Opened tab \[(\d+)\]', content)
        assert m, f"could not parse tab id from: {content!r}"
        tab_id = int(m.group(1))
        # Give the content script a moment to attach to the freshly opened tab.
        await asyncio.sleep(1.0)
        return tab_id

    async def _open_settled(self, mcp, url, wait_for=None):
        """Open a tab then navigate() it once so the page is fully settled before acting.

        This re-navigates the same tab to `url` (a second load). Do NOT use for
        fixtures whose content differs across loads (reload-replaces-text); use
        _open for those.
        """
        tab_id = await self._open(mcp, url)
        args = {"tab_id": tab_id, "url": url, "wait": "settled"}
        if wait_for:
            args["wait_for"] = wait_for
        res = await mcp.call_tool("navigate", args)
        assert not res.get('isError'), res
        return tab_id

    @staticmethod
    async def _eval(mcp, tab_id, code, world="isolated"):
        """page_eval helper returning the raw formatted string result.

        Used ONLY where page_eval is the SUBJECT under test (firefox_only), or by
        the engine-aware verification helpers below on the firefox branch. Do NOT
        call this directly for VERIFICATION reads — use _dom_text / _dom_value /
        _el_present so a chromium run (where page_eval is EVAL_UNAVAILABLE) verifies
        through a working capability instead.
        """
        res = await mcp.call_tool("page_eval", {"tab_id": tab_id, "code": code, "world": world})
        assert not res.get('isError'), res
        return res['content']

    # ---- engine-aware VERIFICATION reads ------------------------------------
    # These read back DOM state to assert a feature worked. On firefox they use
    # page_eval (exact legacy behaviour — the firefox suite is the regression
    # baseline). On chromium page_eval is un-runnable headlessly (EVAL_UNAVAILABLE:
    # the "Allow user scripts" toggle cannot be granted programmatically), so they
    # read through page_read / page_snapshot / page_act, which DO work on MV3. The
    # caller's `assert <needle> in result` is unchanged: the needle values are
    # distinctive, so a whole-page text read (or a snapshot value= line) contains
    # them exactly when the element does.

    @staticmethod
    def _is_chromium(mcp):
        return getattr(mcp, "engine", "firefox") == "chromium"

    async def _dom_text(self, mcp, tab_id, code):
        """Verify-read of an element's TEXT content. firefox: page_eval(code).
        chromium: the tab's full text via page_read (the needle is unique)."""
        if self._is_chromium(mcp):
            return (await mcp.call_tool(
                "page_read", {"tab_id": tab_id, "format": "text"}))['content']
        return await self._eval(mcp, tab_id, code)

    async def _dom_value(self, mcp, tab_id, code):
        """Verify-read of an INPUT value. firefox: page_eval(code). chromium: the
        page_snapshot text, whose `value="..."` line carries input values that a
        plain text read omits."""
        if self._is_chromium(mcp):
            return (await mcp.call_tool(
                "page_snapshot", {"tab_id": tab_id}))['content']
        return await self._eval(mcp, tab_id, code)

    async def _el_present(self, mcp, tab_id, selector):
        """Return "true"/"false" for whether `selector` resolves. firefox: page_eval
        (!!querySelector). chromium: a one-shot page_act wait (resolves via CSS)."""
        if self._is_chromium(mcp):
            res = (await mcp.call_tool("page_act", {
                "tab_id": tab_id,
                "steps": [{"action": "wait", "target": selector, "timeout_ms": 2000}],
            }))['content']
            return "true" if res.splitlines()[0].startswith("OK") else "false"
        return await self._eval(mcp, tab_id, f"!!document.querySelector({selector!r})")

    # ---- migrated cases -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_snapshot_ref_handoff_to_act_fill_and_click(self, mcp, fixtures_server):
        """1. page_snapshot -> ref -> page_act fill/click handoff on form.html.

        The ref minted by page_snapshot must remain valid as a page_act target.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "form.html")
        snap = (await mcp.call_tool("page_snapshot", {"tab_id": tab_id}))['content']
        # Find the ref for the "Full name" textbox and for a "Save" button.
        name_ref = None
        save_ref = None
        for line in snap.splitlines():
            m = re.match(r'-\s+(e\d+)\s+textbox\s+"Full name"', line)
            if m:
                name_ref = m.group(1)
            m = re.match(r'-\s+(e\d+)\s+button\s+"Save"', line)
            if m and save_ref is None:
                save_ref = m.group(1)
        assert name_ref, f"no 'Full name' textbox ref in snapshot:\n{snap}"
        assert save_ref, f"no 'Save' button ref in snapshot:\n{snap}"

        # Fill by ref, then click a Save button by text (hidden-twin safe handled in #5);
        # here we just confirm the ref-based fill landed.
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [
                {"action": "fill", "target": name_ref, "value": "Grace"},
                {"action": "click", "target": "Save", "role": "button", "enabled_only": True},
            ],
        }))['content']
        assert "OK" in act and "FAILED" not in act.splitlines()[0], act
        saved = await self._dom_text(mcp, tab_id, "document.getElementById('saved').textContent")
        assert "SAVED:Grace" in saved, saved

    @pytest.mark.asyncio
    async def test_act_fill_drives_reactive_input(self, mcp, fixtures_server):
        """2. page_act fill on reactive.html updates #mirror (real input event only)."""
        tab_id = await self._open_settled(mcp, fixtures_server + "reactive.html")
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "fill", "target": "#reactive", "value": "hello-reactive"}],
        }))['content']
        assert "FAILED" not in act.splitlines()[0], act
        # The mirror updates only on a real 'input' event.
        mirror = await self._dom_text(mcp, tab_id, "document.getElementById('mirror').textContent")
        assert "hello-reactive" in mirror, mirror

    @pytest.mark.asyncio
    async def test_deferred_modal_click_then_wait(self, mcp, fixtures_server):
        """3. page_act [click #open, wait for the dialog] succeeds on modal.html."""
        tab_id = await self._open_settled(mcp, fixtures_server + "modal.html")
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [
                {"action": "click", "target": "#open"},
                {"action": "wait", "target": "[role=dialog]",
                 "require_visible": True, "timeout_ms": 5000},
            ],
        }))['content']
        first = act.splitlines()[0]
        assert first.startswith("OK"), act
        assert "2/2 steps completed" in first, act
        # The wait step reports a match.
        assert re.search(r"Step \d+ wait: ok", act), act

    @pytest.mark.asyncio
    async def test_iframe_list_works_without_an_explicit_tab_id(self, mcp, fixtures_server):
        """page_snapshot() with NO tab_id must still list the frames.

        tab_id is optional on every page_* tool and omitting it is the COMMON call
        path, but content.frames had no resolveTabId: getAllFrames({tabId: undefined})
        threw, mcp_tools swallowed the error, and the snapshot silently came back with
        no Frames section at all — while its docstring promised one. Every existing
        frame test passed tab_id explicitly, so nothing saw it.
        """
        # Drive the ACTIVE tab throughout: this test is about the omitted-tab_id
        # path, and headless Firefox neither activates a newly-opened tab (see
        # _open) nor honours tabs(action="activate") reliably. Navigating with no
        # tab_id targets the active tab, so the snapshot below lands on this page.
        nav = await mcp.call_tool("navigate", {"url": fixtures_server + "frames.html",
                                               "wait": "settled"})
        assert not nav.get('isError'), nav

        snap = (await mcp.call_tool("page_snapshot", {}))['content']

        assert "Frames (pass frame_id" in snap, (
            "no frame list in a snapshot taken WITHOUT tab_id (with tab_id it works):\n" + snap)
        assert re.search(r"frame_id=(\d+)\s+\S*frame_inner\.html", snap), (
            f"frame_inner.html not listed:\n{snap}")

    @pytest.mark.asyncio
    async def test_iframe_snapshot_and_act_by_frame_id(self, mcp, fixtures_server):
        """4. page_snapshot lists the frame; frame_id snapshot shows #inner-btn; act clicks it."""
        tab_id = await self._open_settled(mcp, fixtures_server + "frames.html")
        snap = (await mcp.call_tool("page_snapshot", {"tab_id": tab_id}))['content']
        assert "Frames (pass frame_id" in snap, f"no frame list in snapshot:\n{snap}"
        m = re.search(r"frame_id=(\d+)\s+\S*frame_inner\.html", snap)
        assert m, f"frame_inner.html frame not listed:\n{snap}"
        frame_id = int(m.group(1))

        inner = (await mcp.call_tool("page_snapshot",
                                     {"tab_id": tab_id, "frame_id": frame_id}))['content']
        assert "#inner-btn" in inner, f"#inner-btn not in inner-frame snapshot:\n{inner}"

        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id, "frame_id": frame_id,
            "steps": [{"action": "click", "target": "#inner-btn"}],
        }))['content']
        assert "FAILED" not in act.splitlines()[0], act
        if self._is_chromium(mcp):
            # Read the INNER frame's text directly (page_read takes a frame_id);
            # the top-tab text read cannot see into a child document.
            clicked = (await mcp.call_tool("page_read", {
                "tab_id": tab_id, "frame_id": frame_id, "format": "text"}))['content']
        else:
            clicked = await self._eval(
                mcp, tab_id,
                "document.querySelector('iframe').contentDocument.getElementById('inner-result').textContent")
        assert "INNER_CLICKED" in clicked, clicked

    @pytest.mark.asyncio
    async def test_hidden_twin_click_hits_visible_button(self, mcp, fixtures_server):
        """5. page_act click by text 'Save' hits the visible/enabled twin, not the hidden one."""
        tab_id = await self._open_settled(mcp, fixtures_server + "form.html")
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [
                {"action": "fill", "target": "#name", "value": "Ada"},
                {"action": "click", "target": "Save", "role": "button"},
            ],
        }))['content']
        assert "FAILED" not in act.splitlines()[0], act
        # The visible #real-save runs the onclick; the hidden #ghost-save does nothing.
        saved = await self._dom_text(mcp, tab_id, "document.getElementById('saved').textContent")
        assert "SAVED:Ada" in saved, saved

    @pytest.mark.asyncio
    async def test_page_read_text_reports_identity(self, mcp, fixtures_server):
        """6. page_read format='text' on titled.html returns title + url header + page text."""
        tab_id = await self._open_settled(mcp, fixtures_server + "titled.html")
        res = (await mcp.call_tool("page_read", {"tab_id": tab_id, "format": "text"}))['content']
        assert "BrowserPlug Titled Page" in res, res
        assert "titled.html" in res, res
        assert "DISTINCTIVE_MARKER_TITLED_PAGE" in res, res
        assert "untitled" not in res.splitlines()[0].lower(), res

    @pytest.mark.asyncio
    async def test_full_page_screenshot_returns_image(self, mcp, fixtures_server):
        """7. page_screenshot(full_page=True) on tall.html returns a CallToolResult with an image."""
        tab_id = await self._open_settled(mcp, fixtures_server + "tall.html")
        res = await mcp.call_tool("page_screenshot", {"tab_id": tab_id, "full_page": True})
        assert not res.get('isError'), res
        tr = res['content']
        blocks = getattr(tr, 'content', tr)
        kinds = {getattr(b, 'type', None) for b in blocks}
        assert 'image' in kinds, f"expected an image block, got {blocks!r}"
        assert 'text' in kinds, f"expected a text summary block, got {blocks!r}"
        # The image block carries non-trivial base64 data.
        img = next(b for b in blocks if getattr(b, 'type', None) == 'image')
        assert getattr(img, 'data', '') and len(img.data) > 1000, "image data looks empty"

    @pytest.mark.asyncio
    async def test_page_read_markdown_strips_chrome_and_paginates(self, mcp, fixtures_server):
        """8. page_read markdown on article.html: main content kept, nav/footer stripped, paginated."""
        tab_id = await self._open_settled(mcp, fixtures_server + "article.html")
        full = (await mcp.call_tool("page_read", {"tab_id": tab_id, "format": "markdown"}))['content']
        # Article body present...
        assert "The Complete Guide to WebSocket Reconnection" in full, full[:800]
        assert "exponential backoff" in full.lower(), "article body phrase missing"
        # ...nav/footer chrome stripped (Defuddle drops these).
        assert "Careers" not in full, "nav link 'Careers' leaked into markdown"
        assert "RSS" not in full, "footer link 'RSS' leaked into markdown"
        # Metadata line shows a word count.
        assert re.search(r"\d+\s+words", full), f"no word count in metadata line:\n{full[:400]}"

        # Pagination: a small cap surfaces a continue hint.
        page1 = (await mcp.call_tool("page_read", {
            "tab_id": tab_id, "format": "markdown", "max_chars": 600
        }))['content']
        m = re.search(r"continue with offset=(\d+)", page1)
        assert m, f"no 'continue with offset=' hint:\n{page1[:400]}"
        offset = int(m.group(1))
        page2 = (await mcp.call_tool("page_read", {
            "tab_id": tab_id, "format": "markdown", "max_chars": 600, "offset": offset
        }))['content']
        # The two pages' content bodies differ.
        body1 = page1.split("\n\n", 1)[-1]
        body2 = page2.split("\n\n", 1)[-1]
        assert body1 != body2, "second page returned identical content"

    @pytest.mark.asyncio
    async def test_silent_fail_login_surfaces_401(self, mcp, fixtures_server):
        """9. One page_act [fill, fill, click 'Log in'] surfaces the failed 401 POST inline."""
        tab_id = await self._open_settled(mcp, fixtures_server + "silent-fail-login.html")
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [
                {"action": "fill", "target": "#email", "value": "user@example.com"},
                {"action": "fill", "target": "#password", "value": "wrong-password"},
                {"action": "click", "target": "Log in", "role": "button"},
            ],
        }))['content']
        # The silent failure is surfaced by the click step's network digest.
        assert "401" in act, f"401 not surfaced by the click step:\n{act}"
        assert "/api/login" in act, f"/api/login not named in the digest:\n{act}"

    @pytest.mark.asyncio
    async def test_requests_recent_retroactively_shows_post(self, mcp, fixtures_server):
        """10. requests action='recent' shows the POST with no monitor; filter='api' narrows."""
        tab_id = await self._open_settled(mcp, fixtures_server + "silent-fail-login.html")
        # Trigger the login POST (no monitoring armed beforehand).
        await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [
                {"action": "fill", "target": "#email", "value": "user@example.com"},
                {"action": "fill", "target": "#password", "value": "wrong-password"},
                {"action": "click", "target": "Log in", "role": "button"},
            ],
        })
        await asyncio.sleep(0.5)
        recent = (await mcp.call_tool("requests", {"action": "recent", "tab_id": tab_id}))['content']
        assert "/api/login" in recent, f"POST not in retroactive log:\n{recent}"
        assert "POST" in recent, recent
        # Filtering by 'api' narrows to the API call.
        filtered = (await mcp.call_tool("requests", {
            "action": "recent", "tab_id": tab_id, "filter": "api"
        }))['content']
        assert "/api/login" in filtered, f"filter='api' lost the POST:\n{filtered}"

    @pytest.mark.timing  # serial phase: asserts a wall-clock window around a 2 s wait timeout
    @pytest.mark.asyncio
    async def test_wait_honors_timeout(self, mcp, fixtures_server):
        """11. A wait for #never fails ~within its timeout (not ~30s); #ready succeeds."""
        tab_id = await self._open_settled(mcp, fixtures_server + "lazy-element.html")

        # #never never appears: the wait must time out roughly at timeout_ms, not at 30s.
        t0 = asyncio.get_event_loop().time()
        miss = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "wait", "target": "#never", "timeout_ms": 2000}],
        }))['content']
        elapsed = asyncio.get_event_loop().time() - t0
        assert "FAILED" in miss, miss
        assert "timed out" in miss.lower(), miss
        # Lenient bound: well under a 30s generic timeout, with CI slack.
        assert 1.0 <= elapsed <= 12.0, f"wait took {elapsed:.1f}s (expected ~2s, not ~30s)"

        # #ready appears after ~1200ms: an 8s wait must succeed.
        hit = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "wait", "target": "#ready", "timeout_ms": 8000}],
        }))['content']
        assert hit.splitlines()[0].startswith("OK"), hit
        assert re.search(r"Step \d+ wait: ok", hit), hit

    @pytest.mark.asyncio
    async def test_wait_predicate_runs_in_page_without_eval_in_the_toolkit(self, mcp, fixtures_server):
        """11b. A wait predicate is evaluated outside the shared toolkit (which carries no
        eval, for AMO): a true one matches, a throwing one is reported at the deadline, one
        that does not parse fails at once with the reason.

        On chromium the evaluation rides page_eval's "Allow user scripts" gate, which the
        headless harness cannot grant (the ISOLATED world refuses eval under the extension
        CSP, so there is no other route): the predicate must report the toggle by name and
        a wait carrying a selector beside it must still succeed on the selector."""
        tab_id = await self._open_settled(mcp, fixtures_server + "lazy-element.html")

        if mcp.engine == "chromium":
            gated = (await mcp.call_tool("page_act", {
                "tab_id": tab_id,
                "steps": [{"action": "wait", "timeout_ms": 1500, "predicate": "1 === 1"}],
            }))['content']
            assert "FAILED" in gated and "Allow user scripts" in gated, gated
            assert "chrome://extensions/?id=" in gated, gated
            mixed = (await mcp.call_tool("page_act", {
                "tab_id": tab_id,
                "steps": [{"action": "wait", "timeout_ms": 8000, "predicate": "1 === 1",
                           "target": "#ready"}],
            }))['content']
            assert mixed.splitlines()[0].startswith("OK"), mixed
            return

        hit = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "wait", "timeout_ms": 8000,
                       "predicate": "!!document.getElementById('ready') // appears at 1200ms"}],
        }))['content']
        assert hit.splitlines()[0].startswith("OK"), hit
        assert re.search(r"Step \d+ wait: ok", hit), hit

        # A predicate that THROWS (a global that is not there) is an unknown: the wait
        # polls to its deadline and the report says what it threw.
        thrown = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "wait", "timeout_ms": 1500,
                       "predicate": "window.__browserplug_absent.flag"}],
        }))['content']
        assert "FAILED" in thrown and "predicate error" in thrown, thrown

        # A predicate that does not PARSE can never start matching: fail now, say why.
        t0 = asyncio.get_event_loop().time()
        broken = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "wait", "timeout_ms": 8000, "predicate": "1 +"}],
        }))['content']
        elapsed = asyncio.get_event_loop().time() - t0
        assert "FAILED" in broken and "predicate is not valid JavaScript" in broken, broken
        assert elapsed < 5.0, f"a parse failure must not poll to the 8s deadline ({elapsed:.1f}s)"

    @pytest.mark.timing  # serial phase: wait="none" must return within 8 s of wall clock
    @pytest.mark.asyncio
    async def test_navigate_settle_and_none(self, mcp, fixtures_server):
        """12. navigate(wait_for='#ready') settles with #ready present; wait='none' returns promptly."""
        tab_id = await self._open(mcp, fixtures_server + "lazy-element.html")
        settled = (await mcp.call_tool("navigate", {
            "tab_id": tab_id, "url": fixtures_server + "lazy-element.html",
            "wait": "settled", "wait_for": "#ready", "timeout_ms": 10000
        }))['content']
        assert "settled" in settled.lower() and "TIMED OUT" not in settled, settled
        # #ready is actually present once navigate returned.
        ready = await self._el_present(mcp, tab_id, "#ready")
        assert "true" in ready.lower(), ready

        # wait='none' returns promptly (does not block on settle).
        t0 = asyncio.get_event_loop().time()
        nowait = (await mcp.call_tool("navigate", {
            "tab_id": tab_id, "url": fixtures_server + "lazy-element.html", "wait": "none"
        }))['content']
        elapsed = asyncio.get_event_loop().time() - t0
        assert "Navigated" in nowait or "tab" in nowait, nowait
        assert elapsed <= 8.0, f"wait='none' took {elapsed:.1f}s (should return promptly)"

    @pytest.mark.asyncio
    async def test_navigate_reports_current_site_service_worker(self, mcp, fixtures_server):
        """navigate attaches the loaded origin's Service Worker registration(s).

        The extension reaches navigator.serviceWorker via the Xray waiver (it's
        undefined in the content-script world) and reports scope + script URL. We
        clean up the registration afterwards as hygiene — each test's Firefox has a
        fresh profile, so nothing persists across tests anyway.
        """
        url = fixtures_server + "service-worker.html"
        # First load registers the SW; the navigate re-load waits for it to go
        # active (#sw-ready) and then captures it post-settle.
        tab_id = await self._open(mcp, url)
        try:
            out = (await mcp.call_tool("navigate", {
                "tab_id": tab_id, "url": url, "wait": "settled",
                "wait_for": "#sw-ready", "timeout_ms": 15000
            }))['content']
            assert "TIMED OUT" not in out, out
            assert "service worker" in out.lower(), f"no SW reported:\n{out}"
            assert "sw.js" in out, f"SW scriptURL missing:\n{out}"
            assert ("scope " + fixtures_server) in out, f"SW scope missing:\n{out}"
        finally:
            # Unregister via the main world — navigator.serviceWorker doesn't exist
            # in the isolated content-script world.
            try:
                await mcp.call_tool("page_eval", {
                    "tab_id": tab_id, "world": "main",
                    "code": "navigator.serviceWorker.getRegistrations()"
                            ".then(rs => Promise.all(rs.map(r => r.unregister())))"
                            ".then(() => 'cleaned')"})
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_console_errors_reach_the_navigate_result(self, mcp, fixtures_server):
        """13. Load-time console errors surface WITHOUT a second call.

        Was `test_console_captures_load_errors_and_click_log`, driving the page_console
        tool. That tool was removed on 2026-08-03 (0 calls in 3,112 across three clients
        and 31 days) precisely BECAUSE navigate already carries console errors in its own
        response envelope — which is where agents were reading them. The capture itself is
        untouched, so this test now pins the surviving channel.

        Note the deliberate loss: post-hoc console *logs* (as opposed to errors) are no
        longer retrievable after the fact. Errors are, which is the signal the corpus shows
        agents actually acting on.
        """
        # A single settled load, so the errors raised during it land in this result's digest.
        nav = (await mcp.call_tool("navigate", {
            "url": fixtures_server + "console-noise.html", "wait": "settled",
        }))['content']

        assert "console error" in nav, f"navigate did not report console errors:\n{nav}"
        # Both the uncaught throw and the explicit console.error are captured at load,
        # before anything asked for them.
        assert "thrown-error C" in nav or "load-error B" in nav, \
            f"neither load-time error named in the digest:\n{nav}"

    @pytest.mark.asyncio
    async def test_console_digest_counts_only_its_own_navigation(self, mcp, fixtures_server):
        """13b. Back-to-back navigations each count their own document's errors.

        A wait="load" navigate of a small page returns about 30 ms after its load event, so
        the next call starts a few ms after that document's errors were logged. An allowance
        on the entries' own timestamps admits them, and the third of these navigations then
        reports 4 errors for a page that raises 2.
        """
        url = fixtures_server + "console-noise.html"
        tab_id = await self._open(mcp, url)
        try:
            counts = []
            for wait in ("settled", "load", "dom", "load", "load"):
                out = (await mcp.call_tool("navigate", {
                    "tab_id": tab_id, "url": url, "wait": wait}))['content']
                found = re.search(r"(\d+) console error\(s\)", out)
                assert found, f"wait={wait}: no console digest\n{out}"
                counts.append(int(found.group(1)))
            assert counts[0] >= 2, counts
            assert counts == [counts[0]] * len(counts), \
                f"each navigation must count only its own document's errors: {counts}"
        finally:
            await mcp.call_tool("tabs", {"action": "close", "tab_id": tab_id})

    @pytest.mark.asyncio
    async def test_snapshot_dedupe_on_many_identical_controls(self, mcp, fixtures_server):
        """14. snapshot collapses the 400 'Add' buttons (dedupe marker) yet keeps unique controls."""
        tab_id = await self._open_settled(mcp, fixtures_server + "many-identical-controls.html")
        snap = (await mcp.call_tool("page_snapshot", {"tab_id": tab_id}))['content']
        # Unique controls survive dedupe.
        assert "#save-all" in snap, snap
        assert "#search" in snap, snap
        assert "#settings" in snap, snap
        # The run of identical "Add" buttons is collapsed with an "(xN identical)" marker.
        assert re.search(r"\(x\d+ identical\)", snap), f"no dedupe marker:\n{snap}"
        # The header reflects collapsed duplicates.
        assert "duplicates collapsed" in snap, f"dedupedDuplicates not reflected:\n{snap}"

    @pytest.mark.asyncio
    async def test_disambiguation_on_duplicate_controls(self, mcp, fixtures_server):
        """15. click 'Provide answers' hits the ENABLED twin; enabled_only / nth are addressable."""
        # enabled_only -> the enabled #real-btn fires its onclick.
        tab_id = await self._open_settled(mcp, fixtures_server + "duplicate-controls.html")
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "Provide answers",
                       "role": "button", "enabled_only": True}],
        }))['content']
        assert "FAILED" not in act.splitlines()[0], act
        result = await self._dom_text(mcp, tab_id, "document.getElementById('result').textContent")
        assert "submitted" in result, result

        # nth is also a valid disambiguator: nth=1 (0-based) is the second "Provide
        # answers" button = the enabled #real-btn. Reset state first, then re-click.
        # (The reset is an eval mutation; on chromium page_eval is unavailable, so we
        # skip it — the nth=1 click still lands on the ENABLED twin, which the OK act
        # result below proves, since a disabled element cannot be clicked.)
        if not self._is_chromium(mcp):
            await self._eval(mcp, tab_id, "document.getElementById('result').textContent=''")
        act2 = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "Provide answers", "role": "button", "nth": 1}],
        }))['content']
        assert "FAILED" not in act2.splitlines()[0], act2
        result2 = await self._dom_text(mcp, tab_id, "document.getElementById('result').textContent")
        assert "submitted" in result2, result2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fixture", ["react-controlled-form.html", "vue-controlled-form.html"])
    async def test_controlled_form_fill_enables_submit(self, mcp, fixtures_server, fixture):
        """16. React/Vue controlled forms: fill+radio enables Submit; submit writes JSON #form-result."""
        tab_id = await self._open_settled(mcp, fixtures_server + fixture, wait_for="#submit-btn")
        # Let the framework finish its first render.
        await asyncio.sleep(0.5)

        forms = (await mcp.call_tool("page_snapshot", {"tab_id": tab_id, "mode": "forms"}))['content']
        # Forms mode lists the plan radios and the (currently disabled) Submit button.
        assert "Free" in forms and "Pro" in forms, f"plan radios missing from forms view:\n{forms}"
        assert "Submit" in forms, forms
        assert "[disabled]" in forms, f"Submit should start disabled:\n{forms}"

        # Discover refs for the text input and a plan radio (Free) and the submit button.
        text_ref = None
        free_ref = None
        submit_ref = None
        for line in forms.splitlines():
            m = re.search(r'(e\d+).*"Project name"', line)
            if m and text_ref is None:
                text_ref = m.group(1)
            m = re.search(r'(e\d+)\s+value=free', line)
            if m and free_ref is None:
                free_ref = m.group(1)
            m = re.search(r'(e\d+)\s+"Submit"', line)
            if m and submit_ref is None:
                submit_ref = m.group(1)
        assert text_ref, f"no Project name ref:\n{forms}"
        assert free_ref, f"no Free radio ref:\n{forms}"
        assert submit_ref, f"no Submit ref:\n{forms}"

        # Fill the text and pick a plan; watch the submit button's enabled state.
        # NOTE: a radio in a controlled React/Vue form must be CLICKED (not 'check'ed)
        # so the framework's onChange/v-model fires and derives canSubmit -> enabled.
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [
                {"action": "fill", "target": text_ref, "value": "Apollo"},
                {"action": "click", "target": free_ref, "watch": [submit_ref]},
            ],
        }))['content']
        assert "FAILED" not in act.splitlines()[0], act
        # The watch on submit reports it ENABLED after the steps.
        assert re.search(r"watch %s:.*enabled" % re.escape(submit_ref), act), \
            f"submit not reported enabled by watch:\n{act}"

        # Now actually submit and verify the JSON result the app writes.
        await mcp.call_tool("page_act", {
            "tab_id": tab_id, "steps": [{"action": "click", "target": submit_ref}]
        })
        await asyncio.sleep(0.3)
        result = await self._dom_text(mcp, tab_id, "document.getElementById('form-result').textContent")
        assert "Apollo" in result, f"form-result not populated with JSON:\n{result}"
        # It is genuine JSON containing both fields.
        m = re.search(r"\{.*\}", result, re.S)
        assert m, f"no JSON object in form-result:\n{result}"
        parsed = json.loads(m.group(0))
        assert parsed.get("name") == "Apollo" and parsed.get("plan") == "free", parsed

    @pytest.mark.asyncio
    @pytest.mark.firefox_only(
        "SUBJECT is page_eval itself (user-supplied code in the isolated vs main world). "
        "On headless Chromium page_eval requires the un-grantable 'Allow user scripts' "
        "toggle → EVAL_UNAVAILABLE (design.md D4/D12); the error path is covered by the "
        "js-chromium twins, not by this test."
    )
    async def test_page_eval_main_vs_isolated_world(self, mcp, fixtures_server):
        """17. page_eval world='main' sees window.__probe; world='isolated' does not."""
        tab_id = await self._open_settled(mcp, fixtures_server + "main-world-global.html")
        main = await self._eval(mcp, tab_id, "window.__probe", world="main")
        assert "main-world-value" in main, f"main world did not see __probe:\n{main}"
        # In the isolated content-script world, the page global is not directly visible.
        iso = await self._eval(mcp, tab_id, "typeof window.__probe", world="isolated")
        assert "undefined" in iso, f"isolated world unexpectedly saw __probe:\n{iso}"

    @pytest.mark.asyncio
    async def test_stale_tab_handle_recovery_hint(self, mcp, fixtures_server):
        """18. page_read on a closed tab_id errors with 'no longer exists' and names the active tab."""
        # Open then close a tab; capture its id.
        victim = await self._open(mcp, fixtures_server + "titled.html")
        closed = await mcp.call_tool("tabs", {"action": "close", "tab_id": victim})
        assert not closed.get('isError'), closed
        await asyncio.sleep(0.3)

        res = (await mcp.call_tool("page_read", {"tab_id": victim, "format": "text"}))['content']
        assert "no longer exists" in res.lower(), f"expected stale-tab recovery hint:\n{res}"
        # The recovery hint names the live active tab (some tab id is mentioned).
        assert re.search(r"\b\d+\b", res), f"recovery hint should name the active tab:\n{res}"

    @pytest.mark.asyncio
    async def test_tab_id_optional_reads_active_tab(self, mcp, fixtures_server):
        """19. navigate() then page_read(), both with NO tab_id, operate on the active tab.

        Headless Firefox does not reliably switch the OS-active tab when a NEW tab is
        opened, so we exercise the "tab_id optional" contract on the already-active
        tab: navigate it (no tab_id) to the fixture, then read it back (no tab_id).
        """
        nav = (await mcp.call_tool("navigate", {
            "url": fixtures_server + "titled.html", "wait": "settled"
        }))['content']
        assert "Navigated" in nav and "TIMED OUT" not in nav, nav
        # No tab_id: page_read resolves the active tab — the one we just navigated.
        res = (await mcp.call_tool("page_read", {"format": "text"}))['content']
        assert "DISTINCTIVE_MARKER_TITLED_PAGE" in res, res
        assert "BrowserPlug Titled Page" in res, res

    @pytest.mark.asyncio
    async def test_fresh_document_wait_guard_on_reload(self, mcp, fixtures_server):
        """20. After reload, a wait for STATE:NEW matches (not a stale pre-reload document)."""
        # Single load (NOT _open_settled): the fixture shows STATE:OLD on the FIRST load
        # and STATE:NEW on any subsequent load, so a second navigation would skip OLD.
        tab_id = await self._open(mcp, fixtures_server + "reload-replaces-text.html")
        # First load shows STATE:OLD (after a 300ms tick).
        firstwait = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "wait", "text_contains": "STATE:OLD", "timeout_ms": 5000}],
        }))['content']
        assert firstwait.splitlines()[0].startswith("OK"), firstwait

        # Reload without waiting, then wait for STATE:NEW. The wait must bind to the
        # fresh document (the #state div is briefly empty right after reload commit).
        await mcp.call_tool("navigate", {"tab_id": tab_id, "action": "reload", "wait": "none"})
        newwait = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "wait", "text_contains": "STATE:NEW", "timeout_ms": 10000}],
        }))['content']
        assert newwait.splitlines()[0].startswith("OK"), newwait
        assert re.search(r"Step \d+ wait: ok", newwait), newwait
        # Confirm via a read that the new state actually rendered.
        body = await self._dom_text(mcp, tab_id, "document.getElementById('state').textContent")
        assert "STATE:NEW" in body, body

    # ---- encoding / non-ASCII coverage --------------------------------------

    @pytest.mark.asyncio
    async def test_windows_1252_page_decodes_and_round_trips(self, mcp, fixtures_server):
        """21. A genuine Windows-1252 page (cp1252-only punctuation) decodes correctly
        and round-trips through read/snapshot/act without mojibake or replacement chars.

        The file is real cp1252 bytes declaring <meta charset="windows-1252">; the
        browser decodes by that declaration. Every tool must surface the resulting
        Unicode intact — these 0x80-0x9F bytes are exactly what corrupts a pipeline
        that wrongly assumes UTF-8.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "encoding-windows-1252.html")

        text = (await mcp.call_tool("page_read", {"tab_id": tab_id, "format": "text"}))['content']
        assert "DISTINCTIVE_MARKER_CP1252_PAGE" in text, text
        # cp1252-only punctuation came through as proper Unicode (not '?' or U+FFFD).
        for needle in ("“one”", "‘two’", "em-dash —", "en-dash –", "ellipsis…",
                       "bullet •", "trademark™", "euro €", "99¢",
                       "Café", "naïve", "Schrödinger", "© 2026"):
            assert needle in text, f"cp1252 char lost in page_read: {needle!r}\n{text}"
        assert "�" not in text, "replacement char (U+FFFD) — decode went wrong"

        # Snapshot surfaces the non-ASCII accessible names verbatim.
        snap = (await mcp.call_tool("page_snapshot", {"tab_id": tab_id}))['content']
        assert "Name — “your” full name" in snap, snap
        assert "Save “it” — now…" in snap, snap
        assert "�" not in snap, snap

        # Fill (with its own non-ASCII value) + click round-trips through JSON/WebSocket.
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [
                {"action": "fill", "target": "#cp-input", "value": "Zoë café"},
                {"action": "click", "target": "#cp-btn"},
            ],
        }))['content']
        assert "FAILED" not in act.splitlines()[0], act
        in_val = await self._dom_value(mcp, tab_id, "document.getElementById('cp-input').value")
        assert "Zoë café" in in_val, f"fill did not land the value:\n{in_val}"
        await asyncio.sleep(0.3)  # let the click's onclick commit before reading
        result = await self._dom_text(mcp, tab_id, "document.getElementById('cp-result').textContent")
        # The onclick prefixes "Saved™:" — the ™ and the typed accented value both survive.
        assert "Saved™:Zoë café" in result, f"round-trip corrupted:\n{result}"
        assert "�" not in result, result

    @pytest.mark.asyncio
    async def test_multilingual_rtl_cjk_round_trips_and_paginates(self, mcp, fixtures_server):
        """22. Arabic (RTL), Chinese (CJK), Hebrew (RTL) and astral-plane emoji survive
        read/snapshot/act, and max_chars pagination never splits a multi-byte char.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "i18n-multilang.html")

        text = (await mcp.call_tool("page_read", {"tab_id": tab_id, "format": "text"}))['content']
        assert "DISTINCTIVE_MARKER_I18N_PAGE" in text, text
        assert "مرحبا بالعالم" in text, "Arabic phrase lost"
        assert "你好，世界" in text, "Chinese phrase lost"
        assert "שָׁלוֹם עוֹלָם" in text, "Hebrew phrase (with niqqud) lost"
        assert "😀" in text and "𝕏" in text, "astral-plane chars lost"
        assert "�" not in text, "replacement char in i18n read"

        # Markdown extraction keeps the same non-ASCII body.
        md = (await mcp.call_tool("page_read", {"tab_id": tab_id, "format": "markdown"}))['content']
        assert "你好" in md and "مرحبا" in md and "שָׁלוֹם" in md, md

        # Snapshot exposes the multilingual button/input accessible names verbatim.
        snap = (await mcp.call_tool("page_snapshot", {"tab_id": tab_id}))['content']
        assert "الاسم الكامل / 全名 / שם מלא" in snap, snap
        assert "提交 / إرسال / שלח" in snap, snap

        # Fill a value spanning all scripts + emoji, click, verify the round-trip.
        typed = "اسم 名字 שם 😀"
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [
                {"action": "fill", "target": "#i18n-input", "value": typed},
                {"action": "click", "target": "#i18n-btn"},
            ],
        }))['content']
        assert "FAILED" not in act.splitlines()[0], act
        # Precondition: the fill landed the exact multilingual value into the input.
        in_val = await self._dom_value(mcp, tab_id, "document.getElementById('i18n-input').value")
        assert typed in in_val, f"fill did not land the value:\n{in_val}"
        await asyncio.sleep(0.3)  # let the click's onclick commit before reading
        result = await self._dom_text(mcp, tab_id, "document.getElementById('i18n-result').textContent")
        # onclick prefixes "تم：完成：בוצע:" then appends the typed value.
        assert typed in result, f"multilingual round-trip corrupted:\n{result}"
        assert "تم：完成：בוצע:" in result, result
        assert "�" not in result, result

        # Pagination must not split a multi-byte UTF-8 sequence: a tiny cap landing
        # mid-Arabic must still return well-formed text (no U+FFFD), and the next page
        # must continue cleanly.
        p1 = (await mcp.call_tool("page_read", {
            "tab_id": tab_id, "format": "text", "max_chars": 40
        }))['content']
        assert "�" not in p1, f"pagination split a multi-byte char:\n{p1}"
        m = re.search(r"continue with offset=(\d+)", p1)
        assert m, f"no continue hint on a capped i18n read:\n{p1}"
        p2 = (await mcp.call_tool("page_read", {
            "tab_id": tab_id, "format": "text", "max_chars": 200, "offset": int(m.group(1))
        }))['content']
        assert "�" not in p2, f"second page split a multi-byte char:\n{p2}"

    # ---- v2.1: errors + screenshots -----------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_PIL, reason="Pillow needed for pixel assertions")
    async def test_element_screenshot_crops_to_selector(self, mcp, fixtures_server):
        """page_screenshot(selector) crops to the element's box.

        #target is a 300x200 solid-green div below the fold; the capture must scroll
        to it and crop to (approximately) its box, with the green visible in the crop.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "element-shot.html")
        res = await mcp.call_tool("page_screenshot", {"tab_id": tab_id, "selector": "#target"})
        assert not res.get('isError'), res
        im = Image.open(io.BytesIO(_screenshot_png(res))).convert("RGB")
        # 300x200 CSS px, allowing for dpr scaling (1x-2x) + rounding.
        assert 240 <= im.width <= 900, im.size
        assert 150 <= im.height <= 700, im.size
        # The crop is the element's green (#11aa44), not the page background/black.
        r, g, b = im.getpixel((im.width // 2, im.height // 2))
        assert g > 90 and g > r and g > b, f"center pixel not green: {(r, g, b)}"
        assert "element '#target'" in _screenshot_summary(res), _screenshot_summary(res)

    @pytest.mark.asyncio
    async def test_element_screenshot_errors_on_zero_and_multi_match(self, mcp, fixtures_server):
        """A zero/multiple-match selector errors clearly — no silent viewport shot."""
        tab_id = await self._open_settled(mcp, fixtures_server + "element-shot.html")
        zero = await mcp.call_tool("page_screenshot", {"tab_id": tab_id, "selector": "#does-not-exist"})
        ztext = str(zero['content'])
        assert "no element" in ztext.lower() or "could not resolve" in ztext.lower(), ztext
        multi = await mcp.call_tool("page_screenshot", {"tab_id": tab_id, "selector": ".dup"})
        mtext = str(multi['content'])
        assert "matched 2" in mtext or "more specific" in mtext or "nth" in mtext, mtext

    @pytest.mark.timing  # serial phase: the capture's settle races a network-delayed image
    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_PIL, reason="Pillow needed for pixel assertions")
    async def test_full_page_settles_no_black_band_for_lazy_image(self, mcp, fixtures_server):
        """A full-page capture settles, so a network-delayed lazy image below the
        fold appears (blue) instead of stitching in as a black band."""
        tab_id = await self._open_settled(mcp, fixtures_server + "lazy-shot.html")
        res = await mcp.call_tool("page_screenshot", {"tab_id": tab_id, "full_page": True})
        assert not res.get('isError'), res
        im = Image.open(io.BytesIO(_screenshot_png(res))).convert("RGB")
        # Blue exists ONLY in the lazy image (#2266dd). A black band would yield ~none.
        W, H = im.size
        blue = 0
        for y in range(0, H, max(1, H // 120)):
            for x in range(0, W, max(1, W // 80)):
                r, g, b = im.getpixel((x, y))
                if b > 120 and b > r + 30 and b > g + 30:
                    blue += 1
        assert blue > 30, f"no blue lazy-image pixels in full-page capture (black band?) blue={blue}"
        # The lazy image actually decoded (the capture scrolled to it and settled).
        # firefox confirms this via eval; on chromium the blue pixels above ARE the
        # proof the image rendered (page_eval is EVAL_UNAVAILABLE headlessly).
        if not self._is_chromium(mcp):
            dec = await self._eval(
                mcp, tab_id,
                "(function(){var i=document.getElementById('shotimg');return !!(i&&i.complete&&i.naturalWidth>0);})()")
            assert "true" in dec.lower(), dec

    @pytest.mark.asyncio
    async def test_act_fill_accepts_text_alias_for_value(self, mcp, fixtures_server):
        """page_act fill accepts `text` as an alias for `value`."""
        tab_id = await self._open_settled(mcp, fixtures_server + "reactive.html")
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "fill", "target": "#reactive", "text": "via-text-alias"}],
        }))['content']
        assert "FAILED" not in act.splitlines()[0], act
        mirror = await self._dom_text(mcp, tab_id, "document.getElementById('mirror').textContent")
        assert "via-text-alias" in mirror, mirror

    @pytest.mark.asyncio
    async def test_stale_element_ref_after_reload_is_named(self, mcp, fixtures_server):
        """A snapshot ref reused after a reload fails with a DISTINCT stale-ref
        message that points at page_snapshot — not a bare 'not found'."""
        tab_id = await self._open_settled(mcp, fixtures_server + "form.html")
        snap = (await mcp.call_tool("page_snapshot", {"tab_id": tab_id}))['content']
        m = re.search(r'-\s+(e\d+)\s+textbox', snap)
        assert m, f"no textbox ref in snapshot:\n{snap}"
        ref = m.group(1)
        # Reload wipes the refs (fresh document, fresh toolkit). Reuse the OLD ref.
        await mcp.call_tool("navigate", {"tab_id": tab_id, "action": "reload", "wait": "settled"})
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id, "steps": [{"action": "fill", "target": ref, "value": "x"}],
        }))['content']
        assert "stale ref" in act.lower(), act
        assert "page_snapshot" in act, act

    @pytest.mark.asyncio
    async def test_about_blank_perception_names_safe_alternative(self, mcp):
        """page_read on an about:blank tab refuses with a message that NAMES the
        privileged-safe alternative; page_screenshot still works on it."""
        opened = (await mcp.call_tool("tabs", {"action": "open", "url": "about:blank"}))['content']
        m = re.search(r'Opened tab \[(\d+)\]', opened)
        assert m, opened
        tab_id = int(m.group(1))
        rd = (await mcp.call_tool("page_read", {"tab_id": tab_id, "format": "text"}))['content']
        assert "about:blank" in rd, rd
        assert "page_screenshot" in rd and "requests" in rd, rd
        # Screenshot on about:blank: firefox captures via captureTab, which needs no
        # content script or host permission, so it succeeds. Chromium captures via
        # captureVisibleTab, which cannot access a privileged/no-host page like
        # about:blank (url "") — so it degrades HONESTLY with a structured error
        # instead of a silent blank (design.md D6 — MV3 screenshots are
        # captureVisibleTab-based; there is no captureTab equivalent).
        shot = await mcp.call_tool("page_screenshot", {"tab_id": tab_id})
        if self._is_chromium(mcp):
            assert shot.get('isError'), f"expected an honest capture error on about:blank:\n{shot}"
            assert "screenshot" in str(shot.get('content', '')).lower(), shot
        else:
            assert not shot.get('isError'), shot
            assert any(getattr(b, "type", None) == "image" for b in _screenshot_blocks(shot)), shot

    @pytest.mark.asyncio
    @pytest.mark.firefox_only(
        "SUBJECT is page_eval reliability under reload churn (the 'still loading' note, "
        "'Receiving end' recovery). page_eval is EVAL_UNAVAILABLE on headless Chromium "
        "(design.md D4/D12); the reload-churn tab_id/settle behaviour is covered by the "
        "parametrized navigate/read tests on both engines."
    )
    async def test_reads_reliable_across_reload_churn(self, mcp, fixtures_server):
        """page_eval stays reliable under reload churn — never the old 'Receiving end
        does not exist'; a transient miss surfaces a status. An eval
        landing in a still-parsing document (empty title) must carry the
        'still loading' note instead of silently reading as final state."""
        tab_id = await self._open_settled(mcp, fixtures_server + "titled.html")
        for _ in range(6):
            await mcp.call_tool("navigate", {"tab_id": tab_id, "action": "reload", "wait": "none"})
            r = (await mcp.call_tool("page_eval", {"tab_id": tab_id, "code": "document.title"}))['content']
            assert "Receiving end" not in r, r
            assert ("BrowserPlug Titled Page" in r) or ("attempt" in r.lower()) \
                or ("still loading" in r.lower()), r
        await mcp.call_tool("navigate", {"tab_id": tab_id, "action": "reload", "wait": "settled"})
        fin = (await mcp.call_tool("page_eval", {"tab_id": tab_id, "code": "document.title"}))['content']
        assert "BrowserPlug Titled Page" in fin, fin
        assert "still loading" not in fin.lower(), fin  # settled reload reads final state

    @pytest.mark.asyncio
    async def test_navigation_failure_reports_actionable_cause(self, mcp, fixtures_server):
        """Navigating to a dead address surfaces the net-error CAUSE (not just a
        Gecko code), so the failure is diagnosable in-band."""
        dead = f"http://127.0.0.1:{_free_port()}/"
        tab_id = await self._open(mcp, fixtures_server + "titled.html")
        nav = (await mcp.call_tool("navigate", {
            "tab_id": tab_id, "url": dead, "wait": "settled", "timeout_ms": 8000})).get('content', '')
        low = nav.lower()
        assert ("net error" in low) or ("about:blank" in low), nav
        assert ("refused" in low) or ("aborted" in low) or ("timeout" in low) or ("ns_" in low), nav

    @pytest.mark.asyncio
    async def test_navigation_reports_redirect_chain(self, mcp, fixtures_server):
        """A navigation through redirects reports the hop chain."""
        tab_id = await self._open(mcp, fixtures_server + "titled.html")
        nav = (await mcp.call_tool("navigate", {
            "tab_id": tab_id, "url": fixtures_server + "redirect-chain",
            "wait": "settled", "timeout_ms": 10000}))['content']
        assert "redirect chain" in nav.lower(), nav
        assert "titled.html" in nav, nav

    @pytest.mark.asyncio
    async def test_open_awaits_commit_lands_on_url(self, mcp, fixtures_server):
        """tabs(open, url) awaits the commit — the tab is reported ON the URL, not
        parked on about:blank with the URL dropped."""
        opened = (await mcp.call_tool("tabs", {
            "action": "open", "url": fixtures_server + "titled.html"}))['content']
        assert "titled.html" in opened, opened
        assert "about:blank" not in opened, opened

    @pytest.mark.timing  # serial phase: navigation 2 must land while the 4 s one runs
    @pytest.mark.asyncio
    async def test_superseded_navigation_is_labeled(self, mcp, fixtures_server):
        """A navigation aborted by a NEWER same-tab navigation is labeled
        'superseded', not reported as a network error."""
        tab_id = await self._open_settled(mcp, fixtures_server + "titled.html")

        async def _nav(url, **kw):
            return (await mcp.call_tool("navigate", dict({"tab_id": tab_id, "url": url}, **kw)))['content']

        # Start a slow load, then supersede it mid-flight with a fast one.
        first_task = asyncio.create_task(_nav(fixtures_server + "slow?ms=4000", wait="settled", timeout_ms=9000))
        await asyncio.sleep(0.8)
        second = await _nav(fixtures_server + "titled.html", wait="settled", timeout_ms=9000)
        first = await first_task
        combined = f"{first}\n----\n{second}"
        assert "superseded" in combined.lower(), combined

    # ---- one-shot captures, wait diagnostics, tab accessors ------------------

    @pytest.mark.asyncio
    @pytest.mark.firefox_only(
        "SUBJECT is Firefox's one-shot captureTab({rect}) path (no activate + "
        "scroll-and-stitch, summary must NOT say 'stitched pages'). On Chromium "
        "element/full_page captures ARE the scroll+captureVisibleTab+stitch loop by "
        "design (design.md D6 — MV3 has no beyond-viewport rect capture), so the "
        "no-stitching assertion is Firefox-specific. The full-page screenshot itself "
        "is covered on both engines by test_full_page_screenshot_returns_image."
    )
    @pytest.mark.skipif(not _HAS_PIL, reason="Pillow needed for pixel assertions")
    async def test_element_and_full_page_capture_are_one_shot(self, mcp, fixtures_server):
        """Selector/full_page captures go through one-shot captureTab({rect}) —
        never the activate + scroll-and-stitch captureVisibleTab loop that hangs on
        unfocused windows. One-shot captures report pages=1, so the summary must not
        mention stitching, and the full-page bitmap must cover the whole scroll
        height, not just one viewport."""
        tab_id = await self._open_settled(mcp, fixtures_server + "element-shot.html")
        el = await mcp.call_tool("page_screenshot", {"tab_id": tab_id, "selector": "#target"})
        assert not el.get('isError'), el
        assert "stitched pages" not in _screenshot_summary(el), _screenshot_summary(el)

        tab_id2 = await self._open_settled(mcp, fixtures_server + "tall.html")
        metrics = await self._eval(
            mcp, tab_id2,
            "JSON.stringify({sh: document.documentElement.scrollHeight,"
            " vh: window.innerHeight, dpr: window.devicePixelRatio || 1})")
        m = json.loads(re.search(r"\{.*\}", metrics, re.S).group(0))
        full = await mcp.call_tool("page_screenshot", {"tab_id": tab_id2, "full_page": True})
        assert not full.get('isError'), full
        summary = _screenshot_summary(full)
        assert "stitched pages" not in summary, summary
        im = Image.open(io.BytesIO(_screenshot_png(full))).convert("RGB")
        expected_h = min(m["sh"], 20000) * m["dpr"]
        assert im.height >= 0.9 * expected_h, \
            f"full-page capture {im.size} does not cover scrollHeight {m['sh']} (dpr {m['dpr']})"
        assert im.height > 1.5 * m["vh"] * m["dpr"], \
            f"full-page capture {im.size} is barely one viewport ({m['vh']} CSS px)"

    @pytest.mark.asyncio
    async def test_wait_timeout_reports_why_it_failed(self, mcp, fixtures_server):
        """A timed-out wait distinguishes present-but-hidden (modal.html's #confirm
        exists but is display:none) from never-matched, and a gone-wait
        reports still-present — each with the selector match count."""
        tab_id = await self._open_settled(mcp, fixtures_server + "modal.html")

        hidden = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "wait", "target": "#confirm",
                       "require_visible": True, "timeout_ms": 1500}],
        }))['content']
        assert "FAILED" in hidden and "timed out" in hidden, hidden
        assert "present-but-hidden" in hidden, hidden
        assert "1 match at last check" in hidden, hidden

        never = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "wait", "target": "#no-such-node", "timeout_ms": 1500}],
        }))['content']
        assert "never-matched" in never, never
        assert "0 matches at last check" in never, never

        gone = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "wait", "target": "#open", "gone": True, "timeout_ms": 1500}],
        }))['content']
        assert "still-present" in gone, gone

    @pytest.mark.asyncio
    async def test_tabs_active_and_find(self, mcp, fixtures_server):
        """tabs(action='active') is a one-shot active-tab accessor; tabs(action='find')
        resolves a URL/title substring to live tab ids."""
        tab_id = await self._open_settled(mcp, fixtures_server + "titled.html")
        res = await mcp.call_tool("tabs", {"action": "activate", "tab_id": tab_id})
        assert not res.get('isError'), res

        active = (await mcp.call_tool("tabs", {"action": "active"}))['content']
        assert re.match(r"Active tab \[\d+\] in window \d+: ", active), active
        assert "\nurl: " in active, active
        # Headless Firefox does not reliably honor tab activation / window focus,
        # so don't assert the just-activated tab won. Assert CONSISTENCY instead:
        # the accessor must report a tab the full listing also flags `active`.
        active_id = re.match(r"Active tab \[(\d+)\]", active).group(1)
        listing = (await mcp.call_tool("tabs", {"action": "list", "recent": False}))['content']
        assert re.search(rf"^id: {active_id} active", listing, re.M), \
            f"accessor reported [{active_id}] but the listing disagrees:\n{active}\n----\n{listing}"

        found = (await mcp.call_tool("tabs", {"action": "find", "url": "titled"}))['content']
        assert f"[{tab_id}]" in found, found
        assert "titled.html" in found, found

        miss = (await mcp.call_tool("tabs", {"action": "find", "url": "zz-no-such-tab-zz"}))['content']
        assert miss.startswith("No open tab matches"), miss

    # ---- commit honesty, net-error naming, dialed peers, controlled fills -----

    @pytest.mark.asyncio
    async def test_open_new_window_blackhole_reports_did_not_commit(self, mcp, blackhole_server):
        """The new-window open path must be as commit-honest as the new-tab path —
        a URL that never commits is reported as about:blank +
        did-not-commit, never echoed back as if it had loaded."""
        res = await mcp.call_tool("tabs", {
            "action": "open", "url": blackhole_server, "new_window": True})
        content = res.get('content', '')
        # Capture the window id up front so the finally can always close it — even
        # if an assertion fails. Each test gets its own Firefox (function-scoped
        # fixture), so this is teardown hygiene — and the discipline a module-scoped
        # (shared-browser) fixture would need (see the module-scope note in .tmp/dev-docs/testing.md).
        m = re.search(r"Opened window (\d+)", content)
        try:
            assert not res.get('isError'), res
            assert "did not commit within 8s" in content, content
            # the tab's REAL location is reported on the headline, not the requested URL
            assert re.search(r"with tab \[\d+\]: about:blank", content), content
        finally:
            if m:
                await mcp.call_tool("tabs", {"action": "close", "window_id": int(m.group(1))})

    @pytest.mark.asyncio
    async def test_navigation_failure_names_symbolic_ns_error(self, mcp, fixtures_server):
        """A failed navigation's net error reads as a SYMBOLIC name
        (NS_ERROR_CONNECTION_REFUSED / NS_BINDING_ABORTED / ...), never a bare
        'Error code <decimal nsresult>' the agent must hand-decode."""
        dead = f"http://127.0.0.1:{_free_port()}/"
        tab_id = await self._open(mcp, fixtures_server + "titled.html")
        nav = (await mcp.call_tool("navigate", {
            "tab_id": tab_id, "url": dead, "wait": "settled", "timeout_ms": 8000}))['content']
        assert "net error:" in nav, nav
        assert not re.search(r"net error: Error code \d+", nav), \
            f"net error is still a raw nsresult integer:\n{nav}"
        # The net error reads as a SYMBOLIC name, never a raw integer. The vocabulary
        # is engine-specific (design.md D5: gecko NS_* nsresult names vs blink
        # net::ERR_* names) — the humanized, symbolic-not-numeric property is shared.
        if self._is_chromium(mcp):
            assert re.search(r"net::ERR_[A-Z_]+", nav), f"no symbolic net::ERR_ name:\n{nav}"
        else:
            assert re.search(r"NS_[A-Z_]+", nav), f"no symbolic error name:\n{nav}"

    @pytest.mark.asyncio
    async def test_requests_recent_reports_dialed_peer(self, mcp, fixtures_server, blackhole_server):
        """The request log carries the dialed peer IP for entries Firefox connected
        to, and marks the peer EXPLICITLY unknown on a stalled
        (pending) entry — the address-family facts stay diagnosable in-band."""
        tab_id = await self._open_settled(mcp, fixtures_server + "titled.html")
        try:
            recent = (await mcp.call_tool("requests", {"action": "recent", "tab_id": tab_id}))['content']
            assert "[dialed 127.0.0.1]" in recent, recent

            # a load that hangs forever: pending entry with an explicitly-unknown peer
            await mcp.call_tool("navigate", {"tab_id": tab_id, "url": blackhole_server, "wait": "none"})
            await asyncio.sleep(1.5)
            recent2 = (await mcp.call_tool("requests", {"action": "recent", "tab_id": tab_id}))['content']
            assert re.search(r"-> pending \[dialed: unknown\] \[main_frame\]", recent2), recent2
        finally:
            # Abort the hung blackhole load and leave the tab on a FULLY SETTLED page:
            # wait="settled" (not "none") so the fixture never tears down a Firefox
            # with an in-flight navigation. Function scope means no later test can
            # inherit it — kept as the worked example of the cleanup discipline the
            # Firefox-harness notes in .tmp/dev-docs/testing.md still require.
            await mcp.call_tool("navigate", {
                "tab_id": tab_id, "url": fixtures_server + "titled.html",
                "wait": "settled", "timeout_ms": 8000})

    @pytest.mark.asyncio
    async def test_act_fill_reports_value_did_not_take_on_controlled_input(self, mcp, fixtures_server):
        """Filling a framework-controlled input that ignores untrusted edits must
        NOT report success — the step fails with a clear
        'value did not take' signal instead of a silent no-op write."""
        tab_id = await self._open_settled(mcp, fixtures_server + "revert-input.html")
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "fill", "target": "#ctl", "value": "agent-typed"}],
        }))['content']
        assert "FAILED" in act, act
        assert "value did not take" in act, act
        # the fixture proves nothing was committed (the trusted-only mirror is unset).
        # firefox reads the data-committed attribute via eval; chromium reads the
        # control's snapshot value (which reverted to its bound value, not the typed
        # one) — both must NOT show the agent-typed string.
        if self._is_chromium(mcp):
            check = (await mcp.call_tool("page_snapshot", {"tab_id": tab_id}))['content']
        else:
            check = (await mcp.call_tool("page_eval", {
                "tab_id": tab_id,
                "code": "document.getElementById('ctl').getAttribute('data-committed')"}))['content']
        assert "agent-typed" not in check, check

    @pytest.mark.asyncio
    async def test_act_fill_still_succeeds_on_cooperative_input(self, mcp, fixtures_server):
        """Guard for the read-back check: a normal (or value-transforming) input keeps
        reporting success with the confirmed post-flush value — the revert detector
        must not turn ordinary fills into failures."""
        tab_id = await self._open_settled(mcp, fixtures_server + "reactive.html")
        act = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "fill", "target": "#reactive", "value": "hello-fill"}],
        }))['content']
        assert "value did not take" not in act, act
        assert "Step 0 fill: ok" in act, act
        assert "hello-fill" in act, act  # confirmedValue reports the post-flush value

    # ---- structured extraction from repeated listing cards -------------------

    @pytest.mark.asyncio
    async def test_records_extracts_one_row_per_card_without_index_drift(
            self, mcp, fixtures_server):
        """page_snapshot(mode="records") returns per-card rows against known ground truth.

        The whole point is the negative: card #2 embeds a capacity switcher whose second
        price node sits NEAR (not inside) the title anchor, which is precisely what makes
        a hand-rolled querySelectorAll-and-zip attribute 1199,00 to a 1829,74 drive. Every
        unambiguous row here must carry ITS OWN price, and the variant card must carry
        none at all.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "variant-grouped-cards.html")
        out = (await mcp.call_tool("page_snapshot", {
            "tab_id": tab_id, "mode": "records"}))['content']

        assert "Records (6 records" in out, out
        # each unambiguous title sits on the same line as its own price
        for title, price in [("Seagate Expansion 8TB", "799,00"),
                             ("Toshiba Canvio 6TB", "649,99"),
                             ("Seagate IronWolf 4TB", "199,00"),
                             ("WD Elements 12TB", "1099,00"),
                             ("WD Elements Desktop 14TB", "1849,00")]:
            line = next((l for l in out.splitlines() if title in l), None)
            assert line, f"no row for {title}:\n{out}"
            assert price in line, f"{title} row carries the wrong price:\n{line}"
        # the decoy nav strip (4 repeated links, no prices) must not win detection
        assert "Dyski</a>" not in out and '"SSD"' not in out, out

    @pytest.mark.asyncio
    async def test_records_reports_a_variant_card_as_ambiguous_instead_of_guessing(
            self, mcp, fixtures_server):
        """A card holding two live prices gets NO price and an explicit AMBIGUOUS marker."""
        tab_id = await self._open_settled(mcp, fixtures_server + "variant-grouped-cards.html")
        out = (await mcp.call_tool("page_snapshot", {
            "tab_id": tab_id, "mode": "records"}))['content']

        assert "1 AMBIGUOUS" in out, out
        line = next((l for l in out.splitlines() if "WD My Book 14TB" in l), None)
        assert line, out
        assert "AMBIGUOUS price" in line, line
        # both candidates are surfaced; neither is presented as THE price
        assert "1829,74" in line and "1199,00" in line, line
        assert "more than one live price inside the SAME card" in out, out

    @pytest.mark.asyncio
    async def test_records_separates_a_struck_was_price_and_unwraps_tracking_links(
            self, mcp, fixtures_server):
        """An old price is `was`, not a rival variant; a click-tracker link is unwrapped."""
        tab_id = await self._open_settled(mcp, fixtures_server + "variant-grouped-cards.html")
        out = (await mcp.call_tool("page_snapshot", {
            "tab_id": tab_id, "mode": "records"}))['content']

        was_line = next((l for l in out.splitlines() if "WD Elements 12TB" in l), None)
        assert was_line and "1099,00" in was_line, out
        assert "was 1499,00" in was_line, (
            "a struck-through price must survive as `was`, not vanish and not create "
            f"ambiguity:\n{was_line}")

        sponsored = next((l for l in out.splitlines() if "WD Elements Desktop 14TB" in l), None)
        assert sponsored and "/oferta/wd-elements-14tb" in sponsored, (
            f"the tracking redirector was not unwrapped to the product URL:\n{sponsored}")
        assert "events/clicks" not in sponsored, sponsored
        assert "link unwrapped from a tracking redirector" in out, out

    @pytest.mark.asyncio
    async def test_records_item_selector_overrides_detection(self, mcp, fixtures_server):
        """The escape hatch works, and a miss refuses rather than falling back to a guess."""
        tab_id = await self._open_settled(mcp, fixtures_server + "variant-grouped-cards.html")
        good = (await mcp.call_tool("page_snapshot", {
            "tab_id": tab_id, "mode": "records", "item_selector": "article.card"}))['content']
        assert "Records (6 records" in good, good
        assert "item_selector article.card" in good, good

        miss = (await mcp.call_tool("page_snapshot", {
            "tab_id": tab_id, "mode": "records", "item_selector": ".no-such-card"}))['content']
        assert "matched no elements" in miss, miss

    @pytest.mark.asyncio
    async def test_records_refuses_when_there_is_no_repeated_pattern_at_all(
            self, mcp, fixtures_server):
        """A page with nothing repeated yields a refusal, not invented rows."""
        tab_id = await self._open_settled(mcp, fixtures_server + "titled.html")
        out = (await mcp.call_tool("page_snapshot", {
            "tab_id": tab_id, "mode": "records"}))['content']
        assert "Error taking snapshot" in out, out
        assert "no repeated-card pattern" in out, out
        assert "item_selector" in out, out

    @pytest.mark.asyncio
    async def test_records_flags_a_related_links_list_as_low_confidence(
            self, mcp, fixtures_server):
        """An article's related-posts sidebar IS a repeated pattern — say so honestly.

        Refusing would be a lie (the rows are accurate) and silently presenting them as
        "the listing" would be the noise-as-data failure in the other direction. The
        answer is the rows PLUS an explicit low-confidence marker pointing at
        item_selector/root.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "article.html")
        out = (await mcp.call_tool("page_snapshot", {
            "tab_id": tab_id, "mode": "records"}))['content']
        assert "LOW CONFIDENCE" in out, out
        assert "related-links" in out, out
        assert "item_selector" in out, out

    # ---- a click must confirm the control's state actually changed -----------

    @pytest.mark.asyncio
    async def test_click_on_a_working_filter_reports_the_resulting_checked_state(
            self, mcp, fixtures_server):
        """The control case: a real label->checkbox toggles, and the state is reported."""
        tab_id = await self._open_settled(mcp, fixtures_server + "collapsed-filter-checkboxes.html")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#ok-label"}],
        }))['content']
        assert out.splitlines()[0].startswith("OK"), out
        assert "is now checked (was unchecked)" in out, out
        # and the filter really applied
        text = await self._dom_text(
            mcp, tab_id, "document.getElementById('results').textContent")
        assert "49 produktow" in text, text

    @pytest.mark.asyncio
    async def test_click_on_a_dead_toggle_fails_instead_of_reporting_ok(
            self, mcp, fixtures_server):
        """A role=checkbox whose handler never moves aria-checked is a FAILURE.

        This is the x-kom shape — nine clicks, nine "ok", nothing applied — and it is a
        correctness bug, not noise: an agent that trusts the "ok" reports the cheapest
        item from an unfiltered result set.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "collapsed-filter-checkboxes.html")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#dead-checkbox"}],
        }))['content']
        assert out.splitlines()[0].startswith("FAILED"), out
        assert "did not change state" in out, out
        assert "aria-checked is still false" in out, out

    @pytest.mark.asyncio
    async def test_click_on_a_label_whose_input_is_not_rendered_says_so(
            self, mcp, fixtures_server):
        """A <label for> naming an absent id is diagnosed, and the fix is spelled out."""
        tab_id = await self._open_settled(mcp, fixtures_server + "collapsed-filter-checkboxes.html")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#dangling-label"}],
        }))['content']
        assert out.splitlines()[0].startswith("FAILED"), out
        assert 'for="cap-4000"' in out, out
        assert "no element with that id exists" in out, out

        # Following the advice — expand the section, then click the real control —
        # works, which is what makes the diagnosis actionable rather than merely true.
        fixed = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#expand"},
                      {"action": "click", "target": "#cap-4000"}],
        }))['content']
        assert fixed.splitlines()[0].startswith("OK"), fixed
        assert "is now checked" in fixed, fixed

    # ---- a timed-out step still reports the page state ----------------------

    @pytest.mark.timing  # serial phase: a 1.5 s click budget against a 4 s stall, then a fixed 5 s wait
    @pytest.mark.asyncio
    async def test_timed_out_click_reports_page_state_not_a_bare_timeout(
            self, mcp, fixtures_server):
        """A click whose work outlives its budget comes back structured, and it WORKED.

        The live failure was a bare "Request timed out after 22.1 seconds", which reads
        as "nothing happened" — and was wrong every time it was checked. Here the step
        times out while the handler still holds the main thread, yet the extra items DO
        arrive, so the report must (a) carry the tab's url/title, (b) point at the
        network activity it saw, and (c) NOT claim the click had no effect.
        """
        tab_id = await self._open_settled(
            mcp, fixtures_server + "delayed-settle-past-timeout.html?block=4000")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#load-more", "timeout_ms": 1500}],
        }))['content']

        assert "Step 0 click: TIMED OUT" in out, out
        assert "did not report back within" in out, out
        assert "delayed-settle-past-timeout.html" in out, (
            f"the timed-out step must carry the page's URL:\n{out}")
        assert "probably had no effect" not in out, (
            "the click DID work (it just outran the budget) — saying it had no effect "
            f"is the exact wrong conclusion:\n{out}")

        # ...and the work really did land, which is the whole point.
        await asyncio.sleep(5)
        text = await self._dom_text(
            mcp, tab_id, "document.getElementById('count').textContent")
        assert "40" in text, f"the load-more never completed after all:\n{text}"

    @pytest.mark.asyncio
    async def test_a_fast_click_on_the_same_fixture_is_unaffected(self, mcp, fixtures_server):
        """Guard for the deadline: with the stall disabled the same click is plain ok.

        The timeout machinery must not turn ordinary slow-ish clicks into failures.
        """
        tab_id = await self._open_settled(
            mcp, fixtures_server + "delayed-settle-past-timeout.html?block=0")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#load-more"}],
        }))['content']
        assert out.splitlines()[0].startswith("OK"), out
        assert "TIMED OUT" not in out, out
        text = await self._dom_text(
            mcp, tab_id, "document.getElementById('count').textContent")
        assert "40" in text, text

    # ---- detection against adversarial page shapes --------------------------

    @pytest.mark.asyncio
    async def test_records_prefers_the_organic_grid_over_the_sponsored_strip(
            self, mcp, fixtures_server):
        """Two genuine card grids on one page — the bigger, richer one must win.

        Every real marketplace listing has a promo strip above the results. Picking
        the 3-card strip would return a confident, complete-looking table of the
        wrong three products, so the rejected alternative is reported too.
        """
        tab_id = await self._open_settled(
            mcp, fixtures_server + "sponsored-and-organic-grids.html")
        out = (await mcp.call_tool("page_snapshot", {
            "tab_id": tab_id, "mode": "records"}))['content']
        assert "Records (18 records" in out, out
        assert "PROMO" not in out.split("runner-up")[0], (
            f"a sponsored card leaked into the result rows:\n{out}")
        assert "runner-up pattern: 3x" in out, (
            f"the rejected 3-card strip must be named so the choice is reversible:\n{out}")

    @pytest.mark.asyncio
    async def test_records_handles_per_card_generated_class_names(
            self, mcp, fixtures_server):
        """Hashed per-card classes force the structural fallback grouping key."""
        tab_id = await self._open_settled(mcp, fixtures_server + "generated-class-cards.html")
        out = (await mcp.call_tool("page_snapshot", {
            "tab_id": tab_id, "mode": "records"}))['content']
        assert "Records (8 records, 8 with a single clear price)" in out, out
        assert "Samsung 990 PRO 2TB" in out and "Adata Legend 960 2TB" in out, out

    @pytest.mark.asyncio
    async def test_records_does_not_mistake_a_footer_link_matrix_for_the_listing(
            self, mcp, fixtures_server):
        """40 footer links must not outscore 12 priced product cards.

        Getting this wrong is not a near-miss: the tool would return 40 tidy rows of
        navigation, in the right shape, for a page whose listing it never looked at.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "footer-link-matrix.html")
        out = (await mcp.call_tool("page_snapshot", {
            "tab_id": tab_id, "mode": "records"}))['content']
        assert "Records (12 records, 12 with a single clear price)" in out, out
        assert "Routery Wi-Fi" not in out, f"footer navigation leaked into the rows:\n{out}"
        assert "LOW CONFIDENCE" not in out, out

    # ---- the control node is replaced, or unreachable ------------------------

    @pytest.mark.asyncio
    async def test_click_survives_the_control_being_replaced_by_a_re_render(
            self, mcp, fixtures_server):
        """A framework re-render must not turn a working click into a reported failure.

        The confirm poll captures the control before dispatch; a re-render detaches
        that node, so re-reading it forever would report ok:false on a click that
        worked — the inverse of the bug this check exists to catch.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "rerendered-checkbox.html")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#rr-toggle"}],
        }))['content']
        assert out.splitlines()[0].startswith("OK"), out
        assert "is now checked (was unchecked)" in out, out

    @pytest.mark.asyncio
    async def test_a_re_render_that_discards_the_change_is_still_caught(
            self, mcp, fixtures_server):
        """Re-resolving the control must not become a way to lose the no-op detection."""
        tab_id = await self._open_settled(mcp, fixtures_server + "rerendered-checkbox.html")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#rr-dead"}],
        }))['content']
        assert out.splitlines()[0].startswith("FAILED"), out
        assert "did not change state" in out, out

    @pytest.mark.asyncio
    async def test_a_control_removed_by_its_own_click_is_not_reported_as_dead(
            self, mcp, fixtures_server):
        """Nothing to re-resolve: say the control went away, do not claim it never moved."""
        tab_id = await self._open_settled(mcp, fixtures_server + "rerendered-checkbox.html")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#rr-gone"}],
        }))['content']
        assert out.splitlines()[0].startswith("OK"), out
        assert "did not change state" not in out, out

    @pytest.mark.asyncio
    async def test_a_dead_click_under_an_overlay_names_the_thing_covering_it(
            self, mcp, fixtures_server):
        """The occlusion branch — jsdom cannot exercise it, so this is its only cover.

        A synthetic click is delivered straight to the element, so the overlay never
        actually blocks US; the hit test is the EXPLANATION offered once the click
        turns out to have changed nothing ("a real user could not have reached this
        either"). Hence a deliberately dead control under a fixed banner.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "occluded-control.html")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#covered"}],
        }))['content']
        assert out.splitlines()[0].startswith("FAILED"), out
        assert "did not change state" in out, out
        assert "painted on top of it" in out or "centre point" in out, (
            f"the reachability diagnosis never fired:\n{out}")

    @pytest.mark.asyncio
    async def test_a_dead_click_in_a_collapsed_section_names_that_section(
            self, mcp, fixtures_server):
        """height:0 + overflow:hidden — the unexpanded accordion, invisible to jsdom."""
        tab_id = await self._open_settled(mcp, fixtures_server + "occluded-control.html")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#clipped"}],
        }))['content']
        assert out.splitlines()[0].startswith("FAILED"), out
        assert "did not change state" in out, out
        assert "collapsed" in out or "0x0" in out or "clipped" in out, (
            f"the collapsed ancestor was not diagnosed:\n{out}")

    @pytest.mark.asyncio
    async def test_a_dead_click_in_the_clear_does_not_invent_an_obstruction(
            self, mcp, fixtures_server):
        """The control case: the SAME dead control, unobstructed.

        Without this, an over-eager hit test could blame an overlay on every no-op
        and send the agent hunting a banner that is not there.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "occluded-control.html")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#plain"}],
        }))['content']
        assert out.splitlines()[0].startswith("FAILED"), out
        assert "did not change state" in out, out
        assert "painted on top of it" not in out, (
            f"claimed an obstruction for a control that is fully in the clear:\n{out}")
        assert "no DOM structure changed" in out, out

    # ---- the real root cause — a click that navigates ------------------------

    @pytest.mark.asyncio
    async def test_a_click_that_navigates_never_yields_a_context_free_timeout(
            self, mcp, fixtures_server):
        """The mechanism behind the original failure, not a proxy for it.

        The click commits a navigation, so the document the toolkit was injected into
        is torn down mid-call. The platform decides whether that promise rejects,
        resolves late or hangs — and it may differ by engine. The contract that must
        hold either way: the agent is NEVER left with a timeout carrying no page
        context. Either the step succeeds and reports the navigation, or it times out
        AND names where the tab ended up.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "navigating-click.html")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#go-away", "timeout_ms": 4000}],
        }))['content']

        assert "Request timed out" not in out, (
            f"a bare transport timeout is the failure this report exists to remove:\n{out}")
        if "TIMED OUT" in out:
            assert ("titled.html" in out or "url now" in out), (
                f"a timed-out navigating click must still say where the tab is:\n{out}")
        else:
            assert out.splitlines()[0].startswith("OK"), out

        # Whatever the step reported, the navigation really did happen — which is
        # exactly why "timed out" must never be read as "nothing happened".
        await asyncio.sleep(2)
        tabs = (await mcp.call_tool("tabs", {"action": "list"}))['content']
        assert "titled.html" in tabs, f"the click did not navigate after all:\n{tabs}"

    @pytest.mark.asyncio
    async def test_a_variant_toggle_that_secretly_navigates_is_reported_honestly(
            self, mcp, fixtures_server):
        """The worst observed case: a "16TB" control that is really a link elsewhere.

        A naive "timeout = no state change, retry" assumption derails the session onto
        the wrong product. The result must make the navigation visible one way or the
        other.
        """
        tab_id = await self._open_settled(mcp, fixtures_server + "navigating-click.html")
        out = (await mcp.call_tool("page_act", {
            "tab_id": tab_id,
            "steps": [{"action": "click", "target": "#go-wrong", "timeout_ms": 4000}],
        }))['content']
        assert "Request timed out" not in out, out
        signals = ("seagate-exos-16tb", "urlChanged", "navigated", "NAVIGATED",
                   "now at", "url now")
        assert any(s in out for s in signals), (
            f"nothing in the result reveals that the click left the product page:\n{out}")

        await asyncio.sleep(2)
        tabs = (await mcp.call_tool("tabs", {"action": "list"}))['content']
        assert "product=seagate-exos-16tb" in tabs, (
            f"the fixture did not navigate as designed:\n{tabs}")
