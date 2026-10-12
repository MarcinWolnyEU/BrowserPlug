"""Unit tests for server/usage_panels/copilot_data.py: the Copilot Chat log parse, the session match
against a synthetic store, the TTL cache and the connections-row hook. No live VS Code;
every log line and store row here is synthetic.

Run: uv run --locked --script tests/run.py unit/test_copilot_data.py -q
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

from server.usage_panels import copilot_data  # noqa: E402
from server.server import BrowserPlugServer  # noqa: E402

LOG = (
    b"2026-09-29 11:31:04.259 [warning] Failed to get copilot token due to: Thank you for "
    b"using GitHub Copilot. Your subscription has ended. You are currently logged in as "
    b"stale-login.\n"
    b"2026-09-29 15:18:54.155 [info] Got Copilot token for first-login\n"
    b"2026-09-29 15:18:54.193 [info] copilot token sku: free_plan\n"
    b"2026-09-29 15:19:02.216 [info] ccreq:0000aaaa.copilotmd | success | helper-mini | "
    b"903ms | [progressMessages]\n"
    b"2026-09-29 15:19:06.587 [info] ccreq:0000bbbb.copilotmd | success | chat-model-a | "
    b"5134ms | [panel/editAgent]\n"
    b"2026-09-29 15:19:07.405 [info] ccreq:0000cccc.copilotmd | success | helper-mini | "
    b"723ms | [copilotLanguageModelWrapper]\n"
    b"2026-09-29 16:40:00.000 [info] Got Copilot token for second-login\n"
    b"2026-09-29 16:40:00.010 [info] copilot token sku: pro_plan\n"
    b"2026-09-29 16:52:05.910 [info] ccreq:0000dddd.copilotmd | markdown\n"
    b"2026-09-29 16:52:10.468 [info] ccreq:0000eeee.copilotmd | success | chat-model-b | "
    b"4342ms | [panel/editAgent]\n"
    b"2026-09-29 16:52:11.000 [info] ccreq:0000ffff.copilotmd | success | helper-mini | "
    b"500ms | [progressMessages]\n"
)


def _req(ts, model, context="panel/editAgent", status="success"):
    return (f"{ts} [info] ccreq:0000abcd.copilotmd | {status} | {model} | 1000ms | "
            f"[{context}]\n").encode()


# --- the log --------------------------------------------------------------------------

def test_parse_log_counts_only_the_users_chat_requests():
    info = copilot_data.parse_log(LOG)
    # helper-mini is logged last, but it answers Copilot's own [progressMessages] calls.
    assert info["model"] == "chat-model-b"
    assert info["requests"] == 2
    assert info["last_request"] == datetime(2026, 9, 29, 16, 52, 10, 468000)


def test_parse_log_takes_the_last_issued_account_and_sku():
    info = copilot_data.parse_log(LOG)
    assert info["account"] == "second-login"
    assert info["sku"] == "pro_plan"


def test_a_failed_token_does_not_name_the_account():
    failed = LOG.splitlines(keepends=True)[0]
    info = copilot_data.parse_log(failed)
    assert info["account"] is None and info["sku"] is None


def test_an_unsuccessful_request_moves_the_clock_but_not_the_model():
    info = copilot_data.parse_log(
        LOG + _req("2026-09-29 16:59:01.000", "chat-model-c", status="canceled"))
    assert info["model"] == "chat-model-b"
    assert info["requests"] == 2
    assert info["last_request"] == datetime(2026, 9, 29, 16, 59, 1)


def test_parse_log_of_nothing():
    assert copilot_data.parse_log(b"no requests here\n") == {
        "model": None, "account": None, "sku": None, "requests": 0, "last_request": None}


def test_read_tail_of_a_large_log_reads_only_the_tail(tmp_path):
    p = tmp_path / "GitHub Copilot Chat.log"
    head = _req("2026-09-29 09:00:00.000", "model-from-the-head")
    filler = b"2026-09-29 10:00:00.000 [info] unrelated padding, padding, padding\n" * 8000
    p.write_bytes(head + filler + LOG)
    assert p.stat().st_size > 256 * 1024
    info = copilot_data.parse_log(copilot_data._read_tail(str(p)))
    assert info["model"] == "chat-model-b"
    assert info["requests"] == 2, "the request at the head is outside the 256 KB tail"


def test_read_tail_drops_the_cut_first_line(tmp_path):
    p = tmp_path / "GitHub Copilot Chat.log"
    line = _req("2026-09-29 09:00:00.000", "cut-model").rstrip(b"\n")
    # Sized so the 256 KB tail starts 10 bytes into `line`.
    pad = b"x" * (256 * 1024 + 10 - len(line) - 1 - len(LOG))
    p.write_bytes(line + pad + b"\n" + LOG)
    assert copilot_data._read_tail(str(p)) == LOG, "the cut line must not reach the parser"


def test_read_tail_of_a_missing_file():
    assert copilot_data._read_tail(os.path.join("no", "such", "file.log")) is None


# --- the session store ----------------------------------------------------------------

_T = datetime(2026, 9, 29, 14, 52, 12, 723000, tzinfo=timezone.utc)


def _iso(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _local(dt):
    """The log's spelling of an instant: local wall-clock time, no zone."""
    return dt.astimezone().replace(tzinfo=None)


def _store(tmp_path, sessions, turns=()):
    d = tmp_path / "github.copilot-chat"
    d.mkdir(exist_ok=True)
    path = d / "session-store.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT,
            host_type TEXT, branch TEXT, summary TEXT, agent_name TEXT,
            agent_description TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL, user_message TEXT, assistant_response TEXT,
            timestamp TEXT);
    """)
    for sid, cwd, summary, updated in sessions:
        con.execute("INSERT INTO sessions (id, cwd, summary, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)", (sid, cwd, summary, updated, updated))
    for sid, idx in turns:
        con.execute("INSERT INTO turns (session_id, turn_index) VALUES (?, ?)", (sid, idx))
    con.commit()
    con.close()
    return str(path)


def test_match_session_picks_the_session_updated_with_this_windows_last_request(tmp_path):
    db = _store(tmp_path, [
        ("near", r"c:\work\proj", "Synthetic title", _iso(_T + timedelta(seconds=3))),
        ("nearish", r"c:\work\other", "Other window", _iso(_T - timedelta(seconds=90))),
        ("old", r"c:\work\old", "Old chat", _iso(_T - timedelta(days=3))),
    ], turns=[("near", 0), ("near", 1), ("old", 0)])
    assert copilot_data.match_session(db, _local(_T)) == {
        "title": "Synthetic title", "turns": 2, "cwd": r"c:\work\proj"}


def test_match_session_rejects_a_session_outside_the_window(tmp_path):
    db = _store(tmp_path, [
        ("s", r"c:\work\proj", "t", _iso(_T + timedelta(seconds=121))),
        ("s2", r"c:\work\proj", "t", _iso(_T - timedelta(seconds=121)))])
    assert copilot_data.match_session(db, _local(_T)) is None


def test_match_session_accepts_the_window_edge(tmp_path):
    db = _store(tmp_path, [("s", None, None, _iso(_T - timedelta(seconds=119)))])
    assert copilot_data.match_session(db, _local(_T)) == {
        "title": None, "turns": 0, "cwd": None}


def test_match_session_on_a_missing_or_foreign_db(tmp_path):
    assert copilot_data.match_session(str(tmp_path / "absent.db"), _local(_T)) is None
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database")
    assert copilot_data.match_session(str(junk), _local(_T)) is None


# --- the workspace fallback -----------------------------------------------------------

class _Proc:
    def __init__(self, cwd, children=()):
        self._cwd, self._children = cwd, list(children)

    def cwd(self):
        if isinstance(self._cwd, Exception):
            raise self._cwd
        return self._cwd

    def children(self):
        return self._children


_INSTALL = r"C:\Programs\VS Code"


def test_child_workspace_takes_the_common_child_cwd():
    proc = _Proc(_INSTALL, [
        _Proc(r"c:\work\proj"), _Proc(r"c:\work\proj"), _Proc(r"c:\work\elsewhere"),
        _Proc(_INSTALL), _Proc(_INSTALL), _Proc(_INSTALL),     # the host's own cwd
        _Proc(os.path.join(tempfile.gettempdir(), "mcp")),     # an MCP launcher child
        _Proc(tempfile.gettempdir()), _Proc(tempfile.gettempdir()),
        _Proc(PermissionError("access denied")),
    ])
    assert copilot_data.child_workspace(proc) == r"c:\work\proj"


def test_child_workspace_with_no_usable_child():
    assert copilot_data.child_workspace(_Proc(_INSTALL)) is None
    assert copilot_data.child_workspace(_Proc(_INSTALL, [_Proc(_INSTALL)])) is None


# --- detect_session end to end, over a fake process -------------------------------------

def _fake_scan(monkeypatch, files, workspace=None):
    """Stand in for handle_scan: the extension host's open files, as already scanned."""
    from server.usage_panels import handle_scan
    scan = handle_scan.Scan(tuple(str(f) for f in files), workspace)
    monkeypatch.setattr(handle_scan, "lookup", lambda pid, workspace=False: scan)
    copilot_data._cache.clear()


def test_detect_session_joins_the_log_and_the_store(tmp_path, monkeypatch):
    db = _store(tmp_path, [
        ("s1", r"c:\work\proj", "Synthetic title", _iso(_T + timedelta(seconds=3)))],
        turns=[("s1", 0), ("s1", 1)])
    log = tmp_path / "GitHub Copilot Chat.log"
    log.write_bytes(LOG + _req(_local(_T).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                               "chat-model-z"))
    _fake_scan(monkeypatch, [tmp_path / "unrelated.txt", log, db, db + "-wal"],
               workspace=r"c:\work\child-cwd")
    assert copilot_data.detect_session(14256) == {
        "model": "chat-model-z",
        "account": "second-login",
        "sku": "pro_plan",
        "requests": 3,
        "last_request_hms": _local(_T).strftime("%H:%M:%S"),
        "chat_title": "Synthetic title",
        "chat_turns": 2,
        "workspace": r"c:\work\proj",   # the session's cwd beats the children's
    }


def test_detect_session_without_a_matching_session_falls_back_to_the_children(
        tmp_path, monkeypatch):
    log = tmp_path / "GitHub Copilot Chat.log"
    log.write_bytes(LOG)
    _fake_scan(monkeypatch, [log], workspace=r"c:\work\child-cwd")
    got = copilot_data.detect_session(14256)
    assert got["model"] == "chat-model-b"
    assert got["chat_title"] is None and got["chat_turns"] is None
    assert got["workspace"] == r"c:\work\child-cwd"


def test_detect_session_finds_nothing(tmp_path, monkeypatch):
    _fake_scan(monkeypatch, [tmp_path / "unrelated.log"])
    assert copilot_data.detect_session(14256) is None


def test_detect_session_none_for_bad_pid():
    copilot_data._cache.clear()
    assert copilot_data.detect_session(0) is None
    assert copilot_data.detect_session(None) is None
    assert copilot_data.detect_session(2_000_000_000) is None


def test_detect_session_never_touches_the_process_itself(monkeypatch):
    """The rule behind handle_scan: nothing on the report path asks psutil about another
    process (open_files() is kernel-wide work on Windows). The detector only reads the
    cached scan, so a psutil that explodes on any call changes nothing."""
    import psutil
    from server.usage_panels import handle_scan

    def forbidden(*a, **k):
        raise AssertionError("psutil.Process called on the report path")

    monkeypatch.setattr(psutil, "Process", forbidden)
    monkeypatch.setattr(handle_scan, "lookup", lambda pid, workspace=False: None)
    copilot_data._cache.clear()
    assert copilot_data.detect_session(14256) is None


def test_an_unknown_scan_answers_none_and_is_not_cached(tmp_path, monkeypatch):
    """Until the scan has an answer, the first poll gets None; the answer must not stick
    for the 10 s TTL, or the badge would appear 10 s late."""
    from server.usage_panels import handle_scan
    log = tmp_path / "GitHub Copilot Chat.log"
    log.write_bytes(LOG)
    answers = [None, handle_scan.Scan((str(log),), None)]
    monkeypatch.setattr(handle_scan, "lookup", lambda pid, workspace=False: answers.pop(0))
    copilot_data._cache.clear()
    assert copilot_data.detect_session(14256) is None
    assert copilot_data.detect_session(14256)["model"] == "chat-model-b"


def test_the_workspace_asked_of_the_scan_is_the_childrens_cwd(monkeypatch):
    from server.usage_panels import handle_scan
    asked = []

    def lookup(pid, workspace=False):
        asked.append(workspace)
        return None
    monkeypatch.setattr(handle_scan, "lookup", lookup)
    copilot_data._cache.clear()
    copilot_data.detect_session(14256)
    assert asked == [True]


# --- the TTL cache --------------------------------------------------------------------

class TestDetectSessionCache:

    def setup_method(self):
        copilot_data._cache.clear()

    teardown_method = setup_method

    def _spy(self, monkeypatch, result={"model": "m1"}):
        calls = []

        def fake(pid):
            calls.append(pid)
            return result
        monkeypatch.setattr(copilot_data, "_detect_session_uncached", fake)
        return calls

    def test_repeated_calls_scan_once(self, monkeypatch):
        calls = self._spy(monkeypatch)
        for _ in range(10):
            assert copilot_data.detect_session(4242) == {"model": "m1"}
        assert calls == [4242]

    def test_a_miss_is_cached_too(self, monkeypatch):
        calls = self._spy(monkeypatch, result=None)
        for _ in range(5):
            assert copilot_data.detect_session(99) is None
        assert calls == [99]

    def test_the_answer_refreshes_after_the_ttl(self, monkeypatch):
        seq = [{"model": "old"}, {"model": "new"}]
        monkeypatch.setattr(copilot_data, "_detect_session_uncached", lambda pid: seq.pop(0))
        assert copilot_data.detect_session(7, _ttl=0)["model"] == "old"
        assert copilot_data.detect_session(7, _ttl=0)["model"] == "new"

    def test_the_cache_cannot_grow_without_bound(self, monkeypatch):
        self._spy(monkeypatch)
        for pid in range(1, 201):
            copilot_data.detect_session(pid, _ttl=0)
        assert len(copilot_data._cache) <= 65

    def test_a_detector_crash_is_a_cached_miss(self, monkeypatch):
        def boom(pid):
            raise RuntimeError("format changed")
        monkeypatch.setattr(copilot_data, "_detect_session_uncached", boom)
        assert copilot_data.detect_session(5) is None


# --- the connections row --------------------------------------------------------------

_DETECTED = {
    "model": "chat-model-b", "account": "second-login", "sku": "pro_plan", "requests": 2,
    "last_request_hms": "16:52:10", "chat_title": "Synthetic title", "chat_turns": 2,
    "workspace": r"c:\work\proj",
}


def _copilot_server():
    server = BrowserPlugServer(host="localhost", port=8772)
    now = datetime.now()
    server.http_clients["sid-copilot"] = {
        "pid": 14256,
        "friendly": "GitHub Copilot Chat",
        "cwd": _INSTALL,
        "exe_path": _INSTALL + r"\Code.exe",
        "start_time": now,
        "last_activity": now,
        "history": [],
    }
    return server


def _copilot_row(info):
    return next(c for c in info["connections"] if c.get("friendly") == "GitHub Copilot Chat")


def test_get_connections_info_gates_copilot_detection(monkeypatch):
    """kilo_details=False skips the Copilot log and store reads like it skips Kilo's;
    kilo_details=True attaches the details, the badge and the workspace."""
    server = _copilot_server()
    calls = []

    def spy(pid):
        calls.append(pid)
        return dict(_DETECTED)
    monkeypatch.setattr(copilot_data, "detect_session", spy)

    on = _copilot_row(server.get_connections_info(kilo_details=True))
    assert calls == [14256]
    assert on["copilot"] == _DETECTED
    assert on["provider"] == "github-copilot"
    assert on["model"] == "chat-model-b"
    assert on["cwd"] == r"c:\work\proj"
    assert on["label"] == "GitHub Copilot Chat \u2014 c:\\work\\proj"

    calls.clear()
    off = _copilot_row(server.get_connections_info(kilo_details=False))
    assert calls == []
    assert "copilot" not in off and "provider" not in off and "model" not in off
    assert off["cwd"] == _INSTALL
    assert off["label"] == f"GitHub Copilot Chat \u2014 {_INSTALL}"


def test_copilot_details_without_model_or_workspace_leave_the_row_alone(monkeypatch):
    server = _copilot_server()
    sparse = dict(_DETECTED, model=None, workspace=None, chat_title=None, chat_turns=None)
    monkeypatch.setattr(copilot_data, "detect_session", lambda pid: sparse)
    row = _copilot_row(server.get_connections_info(kilo_details=True))
    assert row["copilot"] == sparse
    assert "provider" not in row and "model" not in row
    assert row["cwd"] == _INSTALL


def test_other_clients_never_reach_the_copilot_detector(monkeypatch):
    server = _copilot_server()
    server.http_clients["sid-copilot"]["friendly"] = "Claude Code"
    calls = []
    monkeypatch.setattr(copilot_data, "detect_session", lambda pid: calls.append(pid))
    server.get_connections_info(kilo_details=True)
    assert calls == []
