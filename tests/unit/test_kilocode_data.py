"""Unit tests for server/usage_panels/kilocode_data.py — Kilo log parsing (no psutil / live process).

Run: uv run --locked --script tests/run.py unit/test_kilocode_data.py -q
"""
import os
from datetime import datetime

from server.usage_panels import kilocode_data  # noqa: E402
from server.server import BrowserPlugServer  # noqa: E402


def test_parse_last_llm_openrouter_slug():
    data = (b"INFO  2026-07-03T18:34:40 +0ms service=llm providerID=openrouter "
            b"modelID=deepseek/deepseek-v4-flash session.id=ses_x agent=code\n")
    assert kilocode_data.parse_last_llm(data) == ("openrouter", "deepseek/deepseek-v4-flash")


def test_parse_last_llm_gitlab():
    data = (b"INFO  2026-07-03T18:37:20 +0ms service=llm providerID=gitlab "
            b"modelID=duo-chat-haiku-4-5 session.id=ses_y small=false\n")
    assert kilocode_data.parse_last_llm(data) == ("gitlab", "duo-chat-haiku-4-5")


def test_parse_last_llm_takes_most_recent():
    data = (b"service=llm providerID=kilo modelID=kilo-auto/small a\n"
            b"service=llm providerID=gitlab modelID=duo-chat-haiku-4-5 b\n")
    assert kilocode_data.parse_last_llm(data) == ("gitlab", "duo-chat-haiku-4-5")


def test_parse_last_llm_none_when_absent():
    assert kilocode_data.parse_last_llm(b"just some log lines, no llm turns here\n") is None


def test_detect_provider_model_none_for_bad_pid():
    assert kilocode_data.detect_provider_model(0) is None
    assert kilocode_data.detect_provider_model(2_000_000_000) is None


def test_last_llm_in_file_reads_tail_of_large_file(tmp_path):
    # The target llm line sits at the END, after >256 KB of unrelated padding —
    # detect_provider_model only reads the tail, so this proves that's enough.
    p = tmp_path / "big.log"
    filler = b"INFO 2026-07-04 some unrelated log line, padding padding padding\n" * 8000
    target = b"INFO service=llm providerID=openrouter modelID=deepseek/deepseek-v4-flash x\n"
    p.write_bytes(filler + target)
    assert p.stat().st_size > 256 * 1024
    assert kilocode_data._last_llm_in_file(str(p)) == ("openrouter", "deepseek/deepseek-v4-flash")


def test_last_llm_in_file_missing_or_empty():
    assert kilocode_data._last_llm_in_file(os.path.join("no", "such", "file.log")) is None


def test_get_connections_info_gates_kilo_detection(monkeypatch):
    """kilo_details=False must SKIP the per-Kilo log reading (the perf gate);
    kilo_details=True runs it and attaches provider/model."""
    server = BrowserPlugServer(host="localhost", port=8772)
    now = datetime.now()
    server.http_clients["sid-kilo"] = {
        "pid": 4242,
        "friendly": "Kilo CLI",
        "cwd": r"C:\dr\browserplug",
        "exe_path": r"C:\nvm\node_modules\@kilocode\cli\bin\kilo.exe",
        "start_time": now,
        "last_activity": now,
        "history": [],
    }

    calls = []
    def spy(pid):
        calls.append(pid)
        return {"provider": "gitlab", "model": "duo-chat-haiku-4-5"}
    monkeypatch.setattr(kilocode_data, "detect_provider_model", spy)

    def kilo_row(info):
        return next(c for c in info["connections"] if c.get("friendly") == "Kilo CLI")

    # ON → detector runs, provider/model attached.
    on = kilo_row(server.get_connections_info(kilo_details=True))
    assert calls == [4242]
    assert on["provider"] == "gitlab"
    assert on["model"] == "duo-chat-haiku-4-5"

    # OFF → detector NOT called, no provider/model on the row.
    calls.clear()
    off = kilo_row(server.get_connections_info(kilo_details=False))
    assert calls == []
    assert "provider" not in off
    assert "model" not in off


# --- the TTL cache ------------------------------------------------------------
# detect_provider_model() is reached from get_connections_info(), which the extension
# polls ~2s AND every launcher's GET /health polls ~3s. Each call is an open_files()
# sweep plus a 256KB tail read per candidate log. The kilo_details switch only silences
# the extension's half — /health has no user preference to read — so without a cache the
# scan is continuous on any box with a launcher up.

class TestDetectProviderModelCache:

    def setup_method(self):
        kilocode_data._cache.clear()

    teardown_method = setup_method

    def _spy(self, monkeypatch, result={"provider": "openrouter", "model": "m1"}):
        calls = []
        def fake(pid):
            calls.append(pid)
            return result
        monkeypatch.setattr(kilocode_data, "_detect_provider_model_uncached", fake)
        return calls

    def test_repeated_calls_scan_once(self, monkeypatch):
        calls = self._spy(monkeypatch)
        for _ in range(10):
            assert kilocode_data.detect_provider_model(4242) == {"provider": "openrouter",
                                                                "model": "m1"}
        assert calls == [4242], f"the scan ran {len(calls)}x for one TTL window"

    def test_a_miss_is_cached_too(self, monkeypatch):
        """A miss costs the SAME open_files() sweep — caching only hits would leave the
        common case (a non-Kilo process, or Kilo before its first turn) uncached."""
        calls = self._spy(monkeypatch, result=None)
        for _ in range(5):
            assert kilocode_data.detect_provider_model(99) is None
        assert calls == [99]

    def test_different_pids_do_not_share_an_entry(self, monkeypatch):
        calls = self._spy(monkeypatch)
        kilocode_data.detect_provider_model(1)
        kilocode_data.detect_provider_model(2)
        assert calls == [1, 2]

    def test_the_answer_refreshes_after_the_ttl(self, monkeypatch):
        """A cache that never expires is just a wrong answer with better latency: the
        user switches model in Kilo and the badge must follow."""
        seq = [{"provider": "openrouter", "model": "old"},
               {"provider": "openrouter", "model": "new"}]
        monkeypatch.setattr(kilocode_data, "_detect_provider_model_uncached",
                            lambda pid: seq.pop(0))
        assert kilocode_data.detect_provider_model(7, _ttl=0)["model"] == "old"
        assert kilocode_data.detect_provider_model(7, _ttl=0)["model"] == "new"

    def test_the_cache_cannot_grow_without_bound(self, monkeypatch):
        """A long-lived server sees PIDs come and go; one entry per dead PID forever
        is a leak."""
        self._spy(monkeypatch)
        for pid in range(200):
            kilocode_data.detect_provider_model(pid, _ttl=0)  # every entry expires at once
        assert len(kilocode_data._cache) <= 65, f"cache grew to {len(kilocode_data._cache)}"

    def test_pid_zero_or_none_never_scans(self, monkeypatch):
        calls = self._spy(monkeypatch)
        assert kilocode_data.detect_provider_model(None) is None
        assert kilocode_data.detect_provider_model(0) is None
        assert calls == []
