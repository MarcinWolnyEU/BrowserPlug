"""Unit tests for server/usage_panels/pi_data.py — pi session-transcript parsing (no psutil / live process).

Run: uv run --locked --script tests/run.py unit/test_pi_data.py -q

The fixtures are verbatim lines from a real transcript (pi 0.83, 2026-08-05).
"""
import os
import sys

from server.usage_panels import pi_data  # noqa: E402
from server.reporting import client_identity  # noqa: E402

SESSION_LINE = (b'{"type":"session","version":3,"id":"019fd0cd-7fd5-7353-af99-3ac69c075d7a",'
                b'"timestamp":"2026-08-05T07:22:36.373Z","cwd":"C:\\\\dr\\\\myproject"}\n')
MODEL_LINE = (b'{"type":"model_change","id":"d9c7bc93","parentId":null,'
              b'"timestamp":"2026-08-05T07:22:36.941Z","provider":"openrouter",'
              b'"modelId":"deepseek/deepseek-v4-flash-0731"}\n')
SWITCHED_LINE = (b'{"type":"model_change","id":"b99cf183","parentId":"a1",'
                 b'"timestamp":"2026-08-05T08:10:00.000Z","provider":"openrouter",'
                 b'"modelId":"mistralai/devstral-2512"}\n')


# ---- parse_last_model_change ---------------------------------------------------------

def test_parses_provider_and_model():
    assert pi_data.parse_last_model_change(SESSION_LINE + MODEL_LINE) == (
        "openrouter", "deepseek/deepseek-v4-flash-0731")


def test_a_mid_session_switch_wins_over_the_startup_model():
    """Ctrl+P mid-session appends another model_change; the badge must follow it."""
    assert pi_data.parse_last_model_change(MODEL_LINE + SWITCHED_LINE) == (
        "openrouter", "mistralai/devstral-2512")


def test_none_when_no_model_change_present():
    assert pi_data.parse_last_model_change(SESSION_LINE) is None


def test_a_message_quoting_the_word_model_change_is_not_mistaken_for_one():
    turn = (b'{"type":"message","message":{"role":"user","content":[{"type":"text",'
            b'"text":"what does type model_change mean, and which provider is it"}]}}\n')
    assert pi_data.parse_last_model_change(turn) is None


# ---- cwd -> session directory --------------------------------------------------------

def test_session_dir_name_matches_pis_own_mangling():
    assert pi_data.session_dir_name("C:\\dr\\myproject") == "--C--dr-myproject--"
    assert pi_data.session_dir_name("C:\\dr\\ardent-suite") == "--C--dr-ardent-suite--"


def test_dir_matches_cwd_ignores_the_wrapping_dashes():
    assert pi_data.dir_matches_cwd("--C--dr-myproject--", "C:\\dr\\myproject")
    assert pi_data.dir_matches_cwd("C--dr-myproject", "C:\\dr\\myproject")


def test_two_project_dirs_do_not_match_each_other():
    """The whole point of the lookup: pi in ardent-suite must not read browserplug's model."""
    assert not pi_data.dir_matches_cwd("--C--dr-ardent-suite--", "C:\\dr\\myproject")


# ---- picking one process's transcript ------------------------------------------------

CREATE_TIME = 1785914556.373   # == 2026-08-05T07:22:36.373Z, the file-name stamp below
MINE = "2026-08-05T07-22-36-373Z_019fd0cd-7fd5-7353-af99-3ac69c075d7a.jsonl"
NEIGHBOUR = "2026-08-05T07-40-00-000Z_019fd0ff-aaaa-bbbb-cccc-ddddddddddd0.jsonl"


def test_picks_the_transcript_whose_name_stamp_matches_this_process():
    """Two pi instances in ONE project directory — and the neighbour was written to
    more recently, so "newest file" alone would hand back the wrong model."""
    entries = [(MINE, CREATE_TIME + 30), (NEIGHBOUR, CREATE_TIME + 9999)]
    assert pi_data.pick_session_file(entries, CREATE_TIME) == MINE


def test_a_transcript_that_predates_the_process_is_never_claimed():
    """THE live miss (2026-08-05): polled a second after launch, before pi had written
    its own transcript, and the only file in the directory was the run that had died
    there a minute earlier. A symmetric window matched it and the row reported that
    run's model — a wrong answer. Nothing to say yet must read as nothing."""
    dead_neighbour = "2026-08-05T07-33-06-098Z_019fd0d7-1bb2-7c41-be54-baa755256d7d.jsonl"
    entries = [(dead_neighbour, CREATE_TIME - 60)]
    assert pi_data.pick_session_file(entries, CREATE_TIME + 61) is None


def test_the_process_own_transcript_still_wins_once_it_appears():
    dead_neighbour = "2026-08-05T07-33-06-098Z_019fd0d7-1bb2-7c41-be54-baa755256d7d.jsonl"
    entries = [(dead_neighbour, CREATE_TIME - 60), (MINE, CREATE_TIME + 2)]
    assert pi_data.pick_session_file(entries, CREATE_TIME - 0.5) == MINE


def test_a_resumed_session_falls_back_to_the_transcript_being_written():
    """`pi --continue` reuses the ORIGINAL file name (days old), so the name stamp can
    never match; what identifies it is that it has been appended to since this process
    started, unlike every stale transcript beside it."""
    old = "2026-07-27T12-02-38-506Z_019fa374-a52a-7124-a760-8dad3cd80aad.jsonl"
    entries = [(old, CREATE_TIME + 20), ("2026-06-01T10-00-00-000Z_x.jsonl", CREATE_TIME - 99999)]
    assert pi_data.pick_session_file(entries, CREATE_TIME) == old


def test_nothing_is_claimed_when_no_transcript_was_touched_since_the_process_started():
    stale = [("2026-06-01T10-00-00-000Z_x.jsonl", CREATE_TIME - 99999)]
    assert pi_data.pick_session_file(stale, CREATE_TIME) is None


def test_no_transcripts_at_all():
    assert pi_data.pick_session_file([], CREATE_TIME) is None


# ---- end to end over a real directory tree -------------------------------------------

def _write_session(root, cwd, name, body):
    d = os.path.join(root, pi_data.session_dir_name(cwd))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as fh:
        fh.write(body)
    return p


def test_detect_reads_the_model_for_the_right_project(tmp_path, monkeypatch):
    root = str(tmp_path)
    mine = _write_session(root, "C:\\dr\\myproject", MINE, SESSION_LINE + MODEL_LINE)
    _write_session(root, "C:\\dr\\ardent-suite", MINE, SESSION_LINE + SWITCHED_LINE)
    os.utime(mine, (CREATE_TIME + 5, CREATE_TIME + 5))

    monkeypatch.setattr(pi_data, "_cache", {})
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(
        cwd="C:\\dr\\myproject", create_time=CREATE_TIME,
        argv=["node", "C:/nvm4w/nodejs/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"]))
    assert pi_data.detect_provider_model(4242, sessions_root=root) == {
        "provider": "openrouter", "model": "deepseek/deepseek-v4-flash-0731"}


def test_an_ephemeral_run_has_no_transcript_to_read(tmp_path, monkeypatch):
    """`--no-session` writes nothing; claiming a neighbouring transcript would be a lie."""
    root = str(tmp_path)
    mine = _write_session(root, "C:\\dr\\myproject", MINE, SESSION_LINE + MODEL_LINE)
    os.utime(mine, (CREATE_TIME + 5, CREATE_TIME + 5))
    monkeypatch.setattr(pi_data, "_cache", {})
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(
        cwd="C:\\dr\\myproject", create_time=CREATE_TIME, argv=["node", "cli.js", "--no-session"]))
    assert pi_data.detect_provider_model(4243, sessions_root=root) is None


def test_the_startup_model_is_found_past_a_multi_megabyte_tail(tmp_path, monkeypatch):
    """A long session sets its model on line 2 and never switches; the tail read scrolls
    right past it, so the head read is what keeps the badge from vanishing on exactly
    the sessions that have been running longest."""
    root = str(tmp_path)
    filler = b'{"type":"message","message":{"role":"user","content":"padding padding"}}\n' * 8000
    mine = _write_session(root, "C:\\dr\\myproject", MINE, SESSION_LINE + MODEL_LINE + filler)
    assert os.path.getsize(mine) > pi_data._TAIL_BYTES
    os.utime(mine, (CREATE_TIME + 5, CREATE_TIME + 5))
    monkeypatch.setattr(pi_data, "_cache", {})
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(
        cwd="C:\\dr\\myproject", create_time=CREATE_TIME, argv=["node", "cli.js"]))
    assert pi_data.detect_provider_model(4244, sessions_root=root) == {
        "provider": "openrouter", "model": "deepseek/deepseek-v4-flash-0731"}


def test_detect_provider_model_none_for_bad_pid():
    assert pi_data.detect_provider_model(0) is None
    assert pi_data.detect_provider_model(2_000_000_000) is None


def _fake_psutil(cwd, create_time, argv):
    import types

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def cwd(self):
            return cwd

        def create_time(self):
            return create_time

        def cmdline(self):
            return list(argv)

    mod = types.ModuleType("psutil")
    mod.Process = _Proc
    return mod


# ---- the row's name ------------------------------------------------------------------

def test_pi_is_named_from_its_npm_package_path():
    cmd = ("C:\\nvm4w\\nodejs\\node.exe "
           "C:/nvm4w/nodejs/node_modules/@earendil-works/pi-coding-agent/dist/cli.js")
    assert client_identity.friendly_from_cmdline(
        cmd, "C:\\nvm4w\\nodejs\\node.exe", "Node") == "pi"


def test_a_prompt_that_mentions_another_agent_does_not_rename_the_row():
    """pi -p puts the whole prompt in argv, and "claude" in argv is Claude Code's own
    marker — so the package path has to win, or the row names the wrong agent."""
    cmd = ("C:\\nvm4w\\nodejs\\node.exe "
           "C:/nvm4w/nodejs/node_modules/@earendil-works/pi-coding-agent/dist/cli.js "
           "-p read CLAUDE.md and tell me what claude code would do here")
    assert client_identity.friendly_from_cmdline(
        cmd, "C:\\nvm4w\\nodejs\\node.exe", "Node") == "pi"
