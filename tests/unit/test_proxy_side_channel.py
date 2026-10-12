"""server/proxy/side_channel.py: the proxy transport's /timing report and /health watch.

Ported from the pre-SDK tests/unit/test_launcher.py (TestProbesIgnoreProxyEnv,
TestReportTiming, TestWatchToolsDiff), where these were launcher._report_timing and
launcher._watch_tools. The assertions are the same; only the home moved, and the launcher's
own pid and build now arrive in the ProxyConfig the launcher hands its proxy child.

  report_timing()  the fire-and-forget POST /timing carrying the stdio-side half of
                   last_command.timings. Two contracts: the payload the server's
                   record_launcher_timing() matches on, and "never hurt the call path".

  watch_tools()    the (server_pid, tools_signature) diff that decides whether to push
                   tools/list_changed. The loop is infinite, so each test scripts a finite
                   sequence of /health payloads and ends it by raising CancelledError out of
                   the fetch (the only exception the loop re-raises); TOOLS_POLL_SECONDS is
                   patched to 0 so nothing sleeps on the wall clock.

No network, no sleeps-as-assertions.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2
import pytest
from mcp.server.subscriptions import InMemorySubscriptionBus, ToolsListChanged

from server.proxy import policy, side_channel

LAUNCHER_PID = 5151
CLIENT_PID = 4242


def _config(port=2830):
    return policy.ProxyConfig(backend_url=f"http://127.0.0.1:{port}/mcp", client_pid=CLIENT_PID,
                              launcher_pid=LAUNCHER_PID, launcher_version="9.9.9")


# --------------------------------------------------------------------------- #
# fake httpx2.AsyncClient
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Recorder:
    """Captures what the side channel did with httpx2 across one test."""

    def __init__(self, get_script=None, post_error=None):
        self.init_kwargs = []
        self.posts = []          # (url, json_body)
        self.post_headers = []   # headers sent with each POST (the /timing signature)
        self.post_content = []   # the raw bytes of a POST that sends `content=`
        self.gets = []           # url
        self.get_headers = []    # headers sent with each GET (launcher identity)
        self.get_script = list(get_script or [])
        self.post_error = post_error
        self.closed = 0

    def client_class(self):
        recorder = self

        class _FakeAsyncClient:
            def __init__(self, **kwargs):
                recorder.init_kwargs.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                recorder.closed += 1
                return False

            async def post(self, url, json=None, headers=None, content=None):
                if content is not None:
                    # Serialized by the sender so the signature covers these exact bytes
                    # (S7); keep them for the assertions.
                    import json as _json
                    recorder.post_content.append(content)
                    json = _json.loads(content)
                recorder.posts.append((url, json))
                recorder.post_headers.append(headers)
                if recorder.post_error is not None:
                    raise recorder.post_error
                return _FakeResponse({})

            # Keep this signature in step with the real call site: an unexpected kwarg
            # raises inside the watcher's `except Exception: continue`, which with
            # TOOLS_POLL_SECONDS patched to 0 is a silent, busy INFINITE LOOP: the suite
            # hangs instead of failing.
            async def get(self, url, headers=None):
                recorder.gets.append(url)
                recorder.get_headers.append(headers)
                if not recorder.get_script:
                    # Ends the watcher's infinite loop: CancelledError is a BaseException,
                    # so it slips past the loop's `except Exception` guards and hits the
                    # `except asyncio.CancelledError: raise`.
                    raise asyncio.CancelledError()
                step = recorder.get_script.pop(0)
                if isinstance(step, Exception):
                    raise step
                return _FakeResponse(step)

        return _FakeAsyncClient


def _health(pid, sig):
    return {"service": "browserplug", "server_pid": pid, "tools_signature": sig}


async def _run_watch(holder, recorder, port=2830, bus=None):
    """Drive watch_tools over a scripted /health sequence until it runs out."""
    with patch.object(side_channel, "TOOLS_POLL_SECONDS", 0), \
         patch.object(httpx2, "AsyncClient", recorder.client_class()):
        with pytest.raises(asyncio.CancelledError):
            await side_channel.watch_tools(holder, _config(port), bus)


def _bus():
    """A real bus and the events it delivered, in order."""
    bus, events = InMemorySubscriptionBus(), []
    bus.subscribe(events.append)
    return bus, events


# --------------------------------------------------------------------------- #
# proxy-blindness (A-F12)
# --------------------------------------------------------------------------- #

class TestProbesIgnoreProxyEnv:
    """Every side-channel HTTP call targets our own loopback server, so none of them may
    honour HTTP_PROXY from the environment.

    httpx's default is trust_env=True: on a machine with a corporate proxy exported and
    no NO_PROXY covering localhost, these calls went to the proxy, which cannot reach
    127.0.0.1, so a perfectly healthy server read as DOWN and the tool-surface watch
    never saw a change.
    """

    @pytest.mark.asyncio
    async def test_watch_tools_client_does_not_trust_env(self):
        rec = _Recorder(get_script=[_health(1, "sig")])
        await _run_watch({"session": None}, rec)

        assert rec.init_kwargs, "no httpx client was constructed"
        for kwargs in rec.init_kwargs:
            assert kwargs.get("trust_env") is False, (
                "the /health watch client honours HTTP_PROXY: a proxied localhost "
                f"probe can never reach our server (kwargs={kwargs})")

    @pytest.mark.asyncio
    async def test_watch_tools_stamps_this_launchers_identity_on_every_poll(self):
        """The /health poll is how the server learns which BUILD each launcher runs.

        It has to ride this poll rather than the MCP payload path: /health is answered
        before any MCP session exists, and it keeps being answered for the launcher's
        whole life, including a launcher whose client never calls a tool. The pid is the
        LAUNCHER's (handed to the proxy child), the hop the server's map draws."""
        rec = _Recorder(get_script=[_health(1, "sig")])
        await _run_watch({"session": None}, rec)

        assert rec.get_headers and all(h for h in rec.get_headers), (
            "a poll without the identity headers teaches the server nothing")
        for headers in rec.get_headers:
            assert headers[policy.LAUNCHER_VERSION_HEADER] == "9.9.9"
            assert headers[policy.LAUNCHER_PID_HEADER] == str(LAUNCHER_PID), (
                "the pid is what ties the version to a launcher NODE on the map")

    @pytest.mark.asyncio
    async def test_report_timing_client_does_not_trust_env(self):
        rec = _Recorder()
        with patch.object(httpx2, "AsyncClient", rec.client_class()):
            await side_channel.report_timing(_config(), "tabs", 1752600000.5, 1.0)

        assert rec.init_kwargs, "no httpx client was constructed"
        assert rec.init_kwargs[0].get("trust_env") is False, (
            f"the /timing client honours HTTP_PROXY (kwargs={rec.init_kwargs[0]})")


# --------------------------------------------------------------------------- #
# report_timing
# --------------------------------------------------------------------------- #

class TestReportTiming:

    @pytest.mark.asyncio
    async def test_posts_the_payload_the_server_matches_on(self):
        rec = _Recorder()
        with patch.object(httpx2, "AsyncClient", rec.client_class()):
            await side_channel.report_timing(_config(), "tabs", 1752600000.5, 123.456)

        assert len(rec.posts) == 1
        url, body = rec.posts[0]
        assert url == "http://127.0.0.1:2830/timing"
        assert body == {
            "source": "launcher",
            "launcher_pid": LAUNCHER_PID,
            "client_pid": CLIENT_PID,     # the client that spawned the launcher
            "tool": "tabs",
            "started_epoch": 1752600000.5,
            "total_ms": 123.5,            # rounded to 0.1 ms
        }
        # server.record_launcher_timing() keys off exactly these.
        assert set(body) >= {"tool", "started_epoch", "total_ms",
                             "launcher_pid", "client_pid"}

    @pytest.mark.asyncio
    async def test_the_report_is_signed_with_the_local_token(self, monkeypatch):
        """S2: /timing demands X-BrowserPlug-Auth, so an unsigned report is a lost one."""
        from browserplug_shared import request_signing
        monkeypatch.setenv("BROWSERPLUG_WS_TOKEN", "t" * 43)
        monkeypatch.setattr("browserplug_shared.local_auth._master_cache", None)
        rec = _Recorder()
        with patch.object(httpx2, "AsyncClient", rec.client_class()):
            await side_channel.report_timing(_config(), "tabs", 1.0, 2.0)
        assert request_signing.AUTH_HEADER in rec.post_headers[0]
        header = rec.post_headers[0][request_signing.AUTH_HEADER]
        assert header.startswith("v2 ")
        # The header names the exact bytes that were sent (S7).
        assert header.split(" ")[4] == request_signing.body_hash(rec.post_content[0])

    @pytest.mark.asyncio
    async def test_port_is_taken_from_the_config(self):
        rec = _Recorder()
        with patch.object(httpx2, "AsyncClient", rec.client_class()):
            await side_channel.report_timing(_config(2942), "navigate", 1.0, 2.0)
        assert rec.posts[0][0] == "http://127.0.0.1:2942/timing"

    @pytest.mark.asyncio
    async def test_total_ms_is_rounded_not_truncated(self):
        rec = _Recorder()
        with patch.object(httpx2, "AsyncClient", rec.client_class()):
            await side_channel.report_timing(_config(), "page_read", 0.0, 9.99)
        assert rec.posts[0][1]["total_ms"] == 10.0

    @pytest.mark.asyncio
    async def test_a_missing_tool_name_is_still_reported(self):
        rec = _Recorder()
        with patch.object(httpx2, "AsyncClient", rec.client_class()):
            await side_channel.report_timing(_config(), None, 5.0, 1.0)
        assert rec.posts[0][1]["tool"] is None

    @pytest.mark.asyncio
    async def test_uses_a_short_timeout_and_closes_the_client(self):
        rec = _Recorder()
        with patch.object(httpx2, "AsyncClient", rec.client_class()):
            await side_channel.report_timing(_config(), "tabs", 0.0, 1.0)
        # The timeout is what this test is about; assert THAT, not the whole kwargs
        # dict (trust_env=False lives here too, see TestProbesIgnoreProxyEnv).
        assert [k["timeout"] for k in rec.init_kwargs] == [2.0]
        assert rec.closed == 1

    @pytest.mark.asyncio
    async def test_connection_error_never_escapes(self):
        """Fire-and-forget: a dead or restarting server must cost a diagnostics line,
        not an exception on the call path."""
        rec = _Recorder(post_error=httpx2.ConnectError("connection refused"))
        with patch.object(httpx2, "AsyncClient", rec.client_class()):
            await side_channel.report_timing(_config(), "tabs", 0.0, 1.0)   # must not raise
        assert len(rec.posts) == 1          # it really tried

    @pytest.mark.asyncio
    async def test_timeout_never_escapes(self):
        rec = _Recorder(post_error=httpx2.ReadTimeout("timed out"))
        with patch.object(httpx2, "AsyncClient", rec.client_class()):
            await side_channel.report_timing(_config(), "tabs", 0.0, 1.0)   # must not raise

    @pytest.mark.asyncio
    async def test_a_broken_client_constructor_never_escapes(self):
        """Even a failure building the client is swallowed, not just a failed POST."""
        boom = MagicMock(side_effect=RuntimeError("httpx exploded"))
        with patch.object(httpx2, "AsyncClient", boom):
            await side_channel.report_timing(_config(), "tabs", 0.0, 1.0)   # must not raise
        assert boom.called

    @pytest.mark.asyncio
    async def test_report_timing_does_not_log_spam(self):
        """'never let timing hurt (or even log-spam) the call path': the swallow must be
        silent."""
        rec = _Recorder(post_error=httpx2.ConnectError("nope"))
        with patch.object(httpx2, "AsyncClient", rec.client_class()), \
             patch.object(side_channel, "log") as log:
            await side_channel.report_timing(_config(), "tabs", 0.0, 1.0)
        log.assert_not_called()


# --------------------------------------------------------------------------- #
# watch_tools: the diff decision
# --------------------------------------------------------------------------- #

class TestWatchToolsDiff:

    @pytest.mark.asyncio
    async def test_first_observation_never_pushes(self):
        """last_key starts None: the very first /health establishes the baseline, it is
        not a 'change' (otherwise every client session would be told its tool list
        changed the moment it started)."""
        sess = AsyncMock()
        rec = _Recorder(get_script=[_health(100, "sig-a")])
        await _run_watch({"session": sess}, rec)
        sess.send_tool_list_changed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unchanged_signature_does_not_push(self):
        sess = AsyncMock()
        rec = _Recorder(get_script=[_health(100, "sig-a")] * 4)
        await _run_watch({"session": sess}, rec)
        sess.send_tool_list_changed.assert_not_awaited()
        # 4 scripted polls + the 5th that ends the loop.
        assert rec.gets == ["http://127.0.0.1:2830/health"] * 5

    @pytest.mark.asyncio
    async def test_changed_tools_signature_pushes_once(self):
        """Same server, re-shaped tools in place (the extension's optional `proxy`
        permission toggled): one push, and no repeat while it then holds steady."""
        sess = AsyncMock()
        rec = _Recorder(get_script=[
            _health(100, "sig-a"),
            _health(100, "sig-b"),   # <- the only change
            _health(100, "sig-b"),
            _health(100, "sig-b"),
        ])
        await _run_watch({"session": sess}, rec)
        assert sess.send_tool_list_changed.await_count == 1

    @pytest.mark.asyncio
    async def test_changed_server_pid_pushes(self):
        """A /restart picks up code edits: the same signature is not enough to stay quiet
        if the PID moved."""
        sess = AsyncMock()
        rec = _Recorder(get_script=[
            _health(100, "sig-a"),
            _health(101, "sig-a"),
        ])
        await _run_watch({"session": sess}, rec)
        assert sess.send_tool_list_changed.await_count == 1

    @pytest.mark.asyncio
    async def test_each_distinct_change_pushes_again(self):
        sess = AsyncMock()
        rec = _Recorder(get_script=[
            _health(100, "sig-a"),
            _health(101, "sig-a"),   # restart      -> push
            _health(101, "sig-b"),   # re-shape     -> push
            _health(101, "sig-b"),   # steady       -> quiet
            _health(102, "sig-c"),   # both moved   -> push (one, not two)
        ])
        await _run_watch({"session": sess}, rec)
        assert sess.send_tool_list_changed.await_count == 3

    @pytest.mark.asyncio
    async def test_a_flap_back_to_a_previous_key_still_pushes(self):
        """The watcher compares against the LAST key, not a history: returning to an
        older tool surface is still a change the client must re-fetch."""
        sess = AsyncMock()
        rec = _Recorder(get_script=[
            _health(100, "sig-a"),
            _health(100, "sig-b"),
            _health(100, "sig-a"),
        ])
        await _run_watch({"session": sess}, rec)
        assert sess.send_tool_list_changed.await_count == 2

    @pytest.mark.asyncio
    async def test_a_failed_health_fetch_is_skipped_and_keeps_the_baseline(self):
        """A server down for a restart must not (a) crash the watcher, nor (b) look like
        a change once it answers again with the SAME surface."""
        sess = AsyncMock()
        rec = _Recorder(get_script=[
            _health(100, "sig-a"),
            httpx2.ConnectError("server restarting"),
            _health(100, "sig-a"),   # same surface came back -> still quiet
        ])
        await _run_watch({"session": sess}, rec)
        sess.send_tool_list_changed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_restart_seen_across_an_outage_pushes(self):
        sess = AsyncMock()
        rec = _Recorder(get_script=[
            _health(100, "sig-a"),
            httpx2.ConnectError("server restarting"),
            _health(200, "sig-a"),   # new PID after the outage
        ])
        await _run_watch({"session": sess}, rec)
        assert sess.send_tool_list_changed.await_count == 1

    @pytest.mark.asyncio
    async def test_no_session_yet_absorbs_the_change_without_crashing(self):
        """The session is only captured once a message has crossed the front's
        middleware; a change before that must neither raise nor be replayed later."""
        holder = {"session": None}
        rec = _Recorder(get_script=[
            _health(100, "sig-a"),
            _health(100, "sig-b"),   # nothing to push to
            _health(100, "sig-b"),
        ])
        await _run_watch(holder, rec)   # the pytest.raises inside asserts it survived

    @pytest.mark.asyncio
    async def test_a_session_without_the_method_is_skipped(self):
        sess = MagicMock(spec=[])       # no send_tool_list_changed attribute
        rec = _Recorder(get_script=[_health(100, "sig-a"), _health(100, "sig-b")])
        await _run_watch({"session": sess}, rec)   # must not raise

    @pytest.mark.asyncio
    async def test_a_failing_push_is_logged_and_the_watcher_survives(self):
        """One dead stdio session must not stop the watcher: the next change still gets
        pushed."""
        sess = AsyncMock()
        sess.send_tool_list_changed.side_effect = [
            RuntimeError("stdio session closed"), None]
        rec = _Recorder(get_script=[
            _health(100, "sig-a"),
            _health(100, "sig-b"),   # push raises
            _health(100, "sig-c"),   # push succeeds
        ])
        with patch.object(side_channel, "log") as log:
            await _run_watch({"session": sess}, rec)
        assert sess.send_tool_list_changed.await_count == 2
        assert any("send_tool_list_changed failed" in str(c.args[0])
                   for c in log.call_args_list)

    @pytest.mark.asyncio
    async def test_the_session_is_re_read_from_the_holder_each_time(self):
        """The front swaps the live session into the holder; the watcher must push to the
        CURRENT one, not one it captured earlier."""
        first, second = AsyncMock(), AsyncMock()
        holder = {"session": first}
        rec = _Recorder(get_script=[_health(100, "a"), _health(100, "b")])

        original_client_class = rec.client_class()

        class _SwapOnFirstGet(original_client_class):
            async def get(self, url, headers=None):
                resp = await super().get(url, headers=headers)
                holder["session"] = second   # session replaced between polls
                return resp

        with patch.object(side_channel, "TOOLS_POLL_SECONDS", 0), \
             patch.object(httpx2, "AsyncClient", _SwapOnFirstGet):
            with pytest.raises(asyncio.CancelledError):
                await side_channel.watch_tools(holder, _config())

        first.send_tool_list_changed.assert_not_awaited()
        assert second.send_tool_list_changed.await_count == 1

    @pytest.mark.asyncio
    async def test_cancellation_propagates_so_serve_can_stop_the_watcher(self):
        """front.serve() cancels the watcher when stdio ends; the loop re-raises
        CancelledError rather than swallowing it into its generic handler."""
        task = asyncio.create_task(side_channel.watch_tools({"session": None}, _config()))
        await asyncio.sleep(0)      # let it reach the first await
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_polls_the_health_endpoint_of_the_configured_port(self):
        sess = AsyncMock()
        rec = _Recorder(get_script=[_health(1, "s")])
        await _run_watch({"session": sess}, rec, port=2915)
        assert set(rec.gets) == {"http://127.0.0.1:2915/health"}
        # A fresh, short-timeout client per poll, never a long-lived one.
        assert [k["timeout"] for k in rec.init_kwargs] == [2.0] * len(rec.gets)
        assert rec.closed == len(rec.init_kwargs)   # every client is closed


# --------------------------------------------------------------------------- #
# watch_tools: the 2026-07-28 era's copy, published on the front's bus
# --------------------------------------------------------------------------- #

class TestWatchToolsPublishes:

    @pytest.mark.asyncio
    async def test_each_change_is_published_beside_the_push(self):
        sess = AsyncMock()
        bus, events = _bus()
        rec = _Recorder(get_script=[
            _health(100, "sig-a"),   # baseline  -> nothing
            _health(100, "sig-b"),   # re-shape  -> push and publish
            _health(100, "sig-b"),   # steady    -> nothing
            _health(101, "sig-b"),   # restart   -> push and publish
        ])
        await _run_watch({"session": sess}, rec, bus=bus)
        assert events == [ToolsListChanged()] * 2
        assert sess.send_tool_list_changed.await_count == 2

    @pytest.mark.asyncio
    async def test_a_change_is_published_with_no_stdio_session(self):
        """The publish does not hang off the captured session: a listen stream is the
        2026-07-28 client's only channel, whatever the holder holds."""
        bus, events = _bus()
        rec = _Recorder(get_script=[_health(100, "sig-a"), _health(100, "sig-b")])
        await _run_watch({"session": None}, rec, bus=bus)
        assert events == [ToolsListChanged()]

    @pytest.mark.asyncio
    async def test_a_failing_push_still_publishes(self):
        sess = AsyncMock()
        sess.send_tool_list_changed.side_effect = RuntimeError("stdio session closed")
        bus, events = _bus()
        rec = _Recorder(get_script=[_health(100, "sig-a"), _health(100, "sig-b")])
        with patch.object(side_channel, "log"):
            await _run_watch({"session": sess}, rec, bus=bus)
        assert events == [ToolsListChanged()]

    @pytest.mark.asyncio
    async def test_a_failing_publish_is_logged_and_the_watcher_survives(self):
        sess = AsyncMock()
        bus = MagicMock()
        bus.publish = AsyncMock(side_effect=[RuntimeError("bus gone"), None])
        rec = _Recorder(get_script=[
            _health(100, "sig-a"),
            _health(100, "sig-b"),   # publish raises
            _health(100, "sig-c"),   # publish succeeds
        ])
        with patch.object(side_channel, "log") as log:
            await _run_watch({"session": sess}, rec, bus=bus)
        assert sess.send_tool_list_changed.await_count == 2
        assert [c.args for c in bus.publish.await_args_list] == [(ToolsListChanged(),)] * 2
        assert any("publishing ToolsListChanged failed" in str(c.args[0])
                   for c in log.call_args_list)
