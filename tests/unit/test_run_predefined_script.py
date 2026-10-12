"""
Unit tests for tools.predefined_script.run_predefined_script, the page_eval script_name path.

$BROWSERPLUG_EXT_SCRIPTS names a directory of VETTED executables whose stdout becomes
JavaScript that runs in the user's logged-in browser, so the name validation is a
security boundary: anything that could smuggle a path (separators, '..') or other
hostile characters must be rejected BEFORE the filesystem is consulted or a
subprocess spawned. Until now only the "env not set" branch had coverage
(test_page_tools.py::test_page_eval_requires_code_or_script_name); these pin every
rejection — plus positive controls, so a reject-everything regression cannot pass.

Same conventions as tests/unit/test_page_tools.py (MockServer, exact strings).
"""

import json
import os
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest

import test_imports  # noqa: F401
from facade_double import FacadeDouble
from server.tools.mcp_tools import BrowserPlugTools
from server.tools.predefined_script import run_predefined_script


class MockServer(FacadeDouble):
    """Records what would hit the wire (same shape as test_page_tools.MockServer)."""

    def __init__(self):
        super().__init__()
        self.sent = []

    async def send_request_and_wait(self, request, timeout=30.0, browser=None):
        self.sent.append(request)
        return {"type": "response", "data": {"result": "ran", "url": "http://x/"}}


@pytest.fixture
def server():
    return MockServer()


@pytest.fixture
def tools(server):
    return BrowserPlugTools(server)


def write_script(tmp_path, monkeypatch, stdout_line):
    """A generator the current OS can execute directly (no shell, see the page_eval
    docstring). Returns (script_name, script_args_json).

    CONSCIOUS CHANGE (T1, 2026-09-29): the fixture used to be a gen.bat. Batch files are
    now refused outright (cmd.exe injection through script_args), so on Windows the
    "script" is the real python.exe from the base install, pointed at by
    BROWSERPLUG_EXT_SCRIPTS, with `-c print(...)` as its arguments. Elsewhere it is a
    shebang script in tmp_path."""
    if os.name == "nt":
        monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", sys.base_prefix)
        return "python.exe", json.dumps(["-c", f"print({stdout_line!r})"])
    script = tmp_path / "gen.sh"
    script.write_text(f"#!/bin/sh\necho '{stdout_line}'\n")
    script.chmod(0o755)
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(tmp_path))
    return script.name, ""


# --- the name filters: nothing hostile ever reaches the filesystem or a process ---

SEPARATOR_RULE = "Script names cannot contain path separators or '..' sequences"
CHARSET_RULE = "Only alphanumeric characters, underscore, dash, and dot are allowed"


@pytest.mark.parametrize("name,rule", [
    ("../evil", SEPARATOR_RULE),
    ("..\\evil", SEPARATOR_RULE),
    ("sub/gen.sh", SEPARATOR_RULE),
    ("sub\\gen.bat", SEPARATOR_RULE),
    ("a..b", SEPARATOR_RULE),
    ("..", SEPARATOR_RULE),
    # page_eval's own `if script_name:` guard means "" can only arrive from a
    # different caller — the static method must still reject it (defense in depth).
    ("", SEPARATOR_RULE),
    ("evil name", CHARSET_RULE),
    ("evil;rm", CHARSET_RULE),
    ("$(evil)", CHARSET_RULE),
    ("evil|x", CHARSET_RULE),
    ("evil&x", CHARSET_RULE),
    ("C:evil", CHARSET_RULE),
    ("~evil", CHARSET_RULE),
])
@pytest.mark.asyncio
async def test_hostile_script_names_never_reach_a_subprocess(tmp_path, monkeypatch, name, rule):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    # Make the traversal REAL: "../evil" resolves to an existing file, so if a name
    # filter ever vanished the lookup would find something runnable. The rejection
    # must fire anyway — by the filter, with the filter's exact message (a different
    # message would mean a later, weaker layer caught it instead).
    (tmp_path / "evil").write_text("#!/bin/sh\necho pwned\n")
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(scripts))

    with patch("server.tools.predefined_script.subprocess.run") as run:
        out = await run_predefined_script(name, "")

    assert out.text == f"Error: Invalid script name '{name}'. {rule}"
    run.assert_not_called()


@pytest.mark.asyncio
async def test_page_eval_surfaces_the_rejection_and_sends_nothing(tools, server, tmp_path, monkeypatch):
    """The tool-level contract: the rejection comes back verbatim to the agent and
    nothing reaches the extension (the validation-failure convention shared by the
    other page_* tools)."""
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(tmp_path))
    fn = (await tools.mcp.get_tool("page_eval")).fn
    out = await fn(script_name="../evil")
    assert out == f"Error: Invalid script name '../evil'. {SEPARATOR_RULE}"
    assert server.sent == []  # validation failures never hit the wire


@pytest.mark.asyncio
async def test_the_containment_backstop_rejects_an_escaping_resolution(tmp_path, monkeypatch):
    """The abspath-containment check is defense in depth: today's name filters make
    it unreachable (no separator or '..' survives them). Pin the backstop anyway by
    simulating exactly the case it exists for — a name that PASSES the filters but
    whose joined path resolves outside the scripts dir (a symlink, or a future
    filter regression)."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    outside = str(tmp_path / "elsewhere" / "evil")
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(scripts))

    real_abspath = os.path.abspath

    def hijacked(p):
        # Only the SCRIPT path resolution is hijacked; scripts_dir stays real.
        if str(p).endswith("innocent.sh"):
            return outside
        return real_abspath(p)

    with patch("server.tools.predefined_script.os.path.abspath", side_effect=hijacked), \
         patch("server.tools.predefined_script.subprocess.run") as run:
        out = await run_predefined_script("innocent.sh", "")

    assert out.text == "Error: Script path 'innocent.sh' escapes the allowed directory"
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_missing_script_is_reported_not_run(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(tmp_path))
    with patch("server.tools.predefined_script.subprocess.run") as run:
        out = await run_predefined_script("missing.exe", "")
    assert out.text == f"Error: Script 'missing.exe' not found in {os.path.abspath(str(tmp_path))}"
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_script_that_is_not_executable_is_refused(tmp_path, monkeypatch):
    """POSIX: a real 0644 file. Windows has no execute bit (os.access(.., X_OK) is true
    for any existing file), so the rule there is the extension: only .exe/.com start.
    CONSCIOUS CHANGE (T7): this test used to patch os.access to False on Windows."""
    if os.name == "nt":
        (tmp_path / "gen.js").write_text('console.log("nope")\n')
        monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(tmp_path))
        name = "gen.js"
    else:
        name, _args = write_script(tmp_path, monkeypatch, 'console.log("nope")')
        (tmp_path / name).chmod(0o644)

    with patch("server.tools.predefined_script.subprocess.run") as run:
        out = await run_predefined_script(name, "")

    assert out.ok is False
    assert out.text.startswith(f"Error: Script '{name}' is not executable")
    run.assert_not_called()


# --- positive controls: the valid path really works (so the rejections above can
# --- never be satisfied by a reject-everything regression) ------------------------

@pytest.mark.asyncio
async def test_a_valid_script_runs_and_its_stdout_is_the_returned_js(tmp_path, monkeypatch):
    name, args = write_script(tmp_path, monkeypatch, 'console.log("generated")')
    out = await run_predefined_script(name, args)
    assert out == (True, 'console.log("generated")')


@pytest.mark.asyncio
async def test_page_eval_sends_the_generated_js_to_the_extension(tools, server, tmp_path, monkeypatch):
    name, args = write_script(tmp_path, monkeypatch, 'console.log("generated")')
    fn = (await tools.mcp.get_tool("page_eval")).fn
    out = await fn(script_name=name, script_args=args)
    assert len(server.sent) == 1
    req = server.sent[0]
    assert req["action"] == "content.execute_script"
    assert req["data"]["script"] == 'console.log("generated")'
    assert out == "Script result (http://x/):\nran"


# --- T1 (server-launcher review 2026-09-29): batch files, control characters in args,
# --- and a blocking subprocess. These go through the page_eval tool so they pin the
# --- contract an agent sees, whatever the helper's own signature is. -----------------

@pytest.mark.skipif(os.name != "nt", reason="batch files only execute through cmd.exe on Windows")
@pytest.mark.asyncio
@pytest.mark.parametrize("ext", ["bat", "cmd", "BAT"])
async def test_batch_scripts_are_refused_and_injected_args_never_run(tools, server, tmp_path, monkeypatch, ext):
    """cmd.exe re-parses its command line, so an arg like '"&echo INJECTED' runs a second
    command (list2cmdline does not defend against cmd's quote rules). The only sound fix
    is to never start a batch file. Red before the fix: the injected echo executed and
    came back as the generated JS."""
    script = tmp_path / f"gen.{ext}"
    script.write_text("@echo off\r\necho console.log(%1)\r\n")
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(tmp_path))
    fn = (await tools.mcp.get_tool("page_eval")).fn
    out = await fn(script_name=script.name, script_args=json.dumps(['"&echo INJECTED_B']))
    assert out.startswith("Error:") and "batch" in out.lower()
    assert "INJECTED" not in out
    assert server.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["gen.bat", "gen.cmd", "GEN.CMD", "gen.bat."])
async def test_batch_names_are_refused_on_every_os_before_any_process(tools, server, tmp_path, monkeypatch, name):
    (tmp_path / name).write_text("@echo off\r\necho x\r\n")
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(tmp_path))
    fn = (await tools.mcp.get_tool("page_eval")).fn
    with patch("server.tools.predefined_script.subprocess.run") as run:
        out = await fn(script_name=name)
    assert out.startswith("Error:") and "batch" in out.lower()
    run.assert_not_called()
    assert server.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["a" + chr(10) + "b", "a" + chr(13) + "b", "a" + chr(0) + "b"])
async def test_newline_and_nul_in_args_are_refused_for_every_script_type(tools, server, tmp_path, monkeypatch, bad):
    args = json.dumps([bad])
    name, _ = write_script(tmp_path, monkeypatch, "1")
    fn = (await tools.mcp.get_tool("page_eval")).fn
    with patch("server.tools.predefined_script.subprocess.run") as run:
        out = await fn(script_name=name, script_args=args)
    assert out.startswith("Error:") and "argument" in out.lower()
    run.assert_not_called()
    assert server.sent == []


@pytest.mark.asyncio
async def test_a_slow_script_does_not_stall_the_event_loop(tools, server, tmp_path, monkeypatch):
    """The subprocess used to run inline in `async def page_eval`, freezing every client
    and /health for up to 30 s. A ticker must keep running while the script 'runs'."""
    import asyncio, subprocess, time
    name, _ = write_script(tmp_path, monkeypatch, "1")

    def slow_run(*a, **kw):
        time.sleep(0.5)
        return subprocess.CompletedProcess(a, 0, stdout="1\n", stderr="")

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    fn = (await tools.mcp.get_tool("page_eval")).fn
    t = asyncio.create_task(ticker())
    with patch("server.tools.predefined_script.subprocess.run", side_effect=slow_run):
        await fn(script_name=name)
    t.cancel()
    assert ticks >= 10, f"event loop was blocked (only {ticks} ticks in 0.5 s)"


@pytest.mark.skipif(os.name != "nt", reason="uses the Windows python.exe as the vetted executable")
@pytest.mark.asyncio
async def test_shell_metacharacters_in_args_reach_a_real_executable_as_data(tmp_path, monkeypatch):
    """Positive control for the refusal above: a real .exe receives the hostile string
    verbatim (CreateProcess, no cmd.exe), so nothing extra runs."""
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", sys.base_prefix)
    hostile = '"&echo INJECTED_B'
    out = await run_predefined_script(
        "python.exe", json.dumps(["-c", "import sys; print(sys.argv[1])", hostile]))
    assert out == (True, hostile)


# --- T7 (server-launcher review 2026-09-29): the runner's failures must never become JS ---
# page_eval used to tell failure from generated code with `startswith("Error:")`, but two
# branches said "Error executing..." and "Unexpected error...", so that prose was sent to
# the page as JavaScript.

@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    __import__("subprocess").SubprocessError("boom"),
    OSError(193, "%1 is not a valid Win32 application"),
])
async def test_a_runner_exception_is_never_sent_to_the_page_as_js(tools, server, tmp_path, monkeypatch, exc):
    name, args = write_script(tmp_path, monkeypatch, 'console.log("x")')
    fn = (await tools.mcp.get_tool("page_eval")).fn
    with patch("server.tools.predefined_script.subprocess.run", side_effect=exc):
        out = await fn(script_name=name, script_args=args)
    assert server.sent == [], f"error text went to the page as JS: {server.sent}"
    assert "Script executed" not in out and "Script result" not in out


@pytest.mark.skipif(os.name != "nt", reason="Windows starts a file by extension, not an execute bit")
@pytest.mark.asyncio
async def test_a_js_file_is_data_on_windows_and_is_not_started(tools, server, tmp_path, monkeypatch):
    """os.access(X_OK) is true for any existing file on Windows, so gen.js used to reach
    CreateProcess and fail with WinError 193 (whose text then became the script)."""
    (tmp_path / "gen.js").write_text("console.log(1)\n")
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(tmp_path))
    fn = (await tools.mcp.get_tool("page_eval")).fn
    with patch("server.tools.predefined_script.subprocess.run") as run:
        out = await fn(script_name="gen.js")
    run.assert_not_called()
    assert out.startswith("Error:") and "gen.js" in out and "executable" in out
    assert server.sent == []


@pytest.mark.asyncio
async def test_the_runner_returns_a_structured_result(tmp_path, monkeypatch):
    name, args = write_script(tmp_path, monkeypatch, "console.log(1)")
    ok = await run_predefined_script(name, args)
    assert ok.ok is True and ok.text == "console.log(1)"
    bad = await run_predefined_script("../x", "")
    assert bad.ok is False and bad.text.startswith("Error:")


# --- .py scripts: one runner rule on every OS, and the bundled examples --------------
# A .py runs under the server's own interpreter, so the examples in docs/custom-scripts-samples/ work on
# Windows (where only .exe/.com start) and need no execute bit anywhere. See
# docs/predefined-scripts.md.

EXAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "custom-scripts-samples")


def write_py(tmp_path, monkeypatch, body, name="gen.py"):
    (tmp_path / name).write_text(body, encoding="utf-8")  # no chmod: the bit is irrelevant
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(tmp_path))
    return name


@pytest.mark.asyncio
async def test_a_py_script_runs_without_an_execute_bit_on_every_os(tmp_path, monkeypatch):
    name = write_py(tmp_path, monkeypatch, "print('console.log(1)')\n")
    out = await run_predefined_script(name, "")
    assert out == (True, "console.log(1)")


@pytest.mark.asyncio
async def test_a_py_script_gets_its_args_as_data(tmp_path, monkeypatch):
    """No shell sits between the server and the interpreter, so metacharacters arrive as
    plain text (the .bat refusal exists because cmd.exe would run them)."""
    name = write_py(tmp_path, monkeypatch, "import sys\nprint(sys.argv[1] + '|' + sys.argv[2])\n")
    hostile = '"&echo INJECTED_B; $(x) `y`'
    out = await run_predefined_script(name, json.dumps([hostile, "b"]))
    assert out == (True, f"{hostile}|b")


@pytest.mark.asyncio
async def test_a_py_script_runs_under_the_servers_interpreter_isolated(tmp_path, monkeypatch):
    """`-I`: the scripts directory is not on sys.path, so a module of the same name as a
    standard one (here json.py) cannot shadow it, and PYTHON* variables are ignored."""
    (tmp_path / "json.py").write_text("raise RuntimeError('shadowed')\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    name = write_py(tmp_path, monkeypatch,
                    "import json, sys\nprint(json.dumps(sys.executable))\n")
    out = await run_predefined_script(name, "")
    assert out.ok, out.text
    assert json.loads(out.text) == sys.executable


@pytest.mark.asyncio
async def test_py_script_output_is_utf8_whatever_the_locale(tmp_path, monkeypatch):
    """The JS is read as UTF-8. Reading it with the locale code page (cp1252 on Windows)
    turned the bullet in the Google Calendar examples into mojibake."""
    name = write_py(tmp_path, monkeypatch, "print('\u2022 \u00e9 \u4e2d')\n")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")  # -I must make this irrelevant
    out = await run_predefined_script(name, "")
    assert out == (True, "\u2022 \u00e9 \u4e2d")


@pytest.mark.asyncio
async def test_a_py_script_failure_reports_its_exit_code_and_stderr(tmp_path, monkeypatch):
    name = write_py(tmp_path, monkeypatch, "import sys\nsys.exit('bad args')\n")
    out = await run_predefined_script(name, "")
    assert out.ok is False
    assert f"Script '{name}' failed with exit code 1. stderr: bad args" in out.text


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["GEN.PY", "gen.Py"])
async def test_the_py_extension_is_matched_case_insensitively(tmp_path, monkeypatch, name):
    write_py(tmp_path, monkeypatch, "print('1')\n", name=name)
    assert await run_predefined_script(name, "") == (True, "1")


@pytest.mark.asyncio
async def test_a_directory_named_like_a_py_script_is_not_run(tmp_path, monkeypatch):
    (tmp_path / "dir.py").mkdir()
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(tmp_path))
    with patch("server.tools.predefined_script.subprocess.run") as run:
        out = await run_predefined_script("dir.py", "")
    assert out.ok is False and "not executable" in out.text
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_py_script_is_not_run_when_a_batch_name_hides_behind_trailing_dots(tmp_path, monkeypatch):
    """The .py rule must not loosen the .bat refusal: 'gen.bat.' still starts gen.bat."""
    (tmp_path / "gen.bat.py").write_text("print('1')\n")  # a real .py whose name holds .bat.
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", str(tmp_path))
    assert await run_predefined_script("gen.bat.py", "") == (True, "1")
    with patch("server.tools.predefined_script.subprocess.run") as run:
        out = await run_predefined_script("gen.bat.", "")
    assert "batch" in out.text.lower()
    run.assert_not_called()


@pytest.fixture
def examples(monkeypatch):
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", os.path.abspath(EXAMPLES_DIR))


@pytest.mark.parametrize("name", [
    "youtube-play-pause.py", "gcal-monthly-events-js.py", "dom-summarize.py",
])
@pytest.mark.asyncio
async def test_the_argumentless_examples_generate_an_iife(examples, name):
    out = await run_predefined_script(name, "")
    assert out.ok, out.text
    assert out.text.startswith("(function() {") and out.text.endswith("})();")
    assert "@@" not in out.text  # every placeholder was substituted


@pytest.mark.asyncio
async def test_youtube_example_takes_an_action_and_rejects_anything_else(examples):
    out = await run_predefined_script("youtube-play-pause.py", json.dumps(["pause"]))
    assert out.ok and "let action = 'pause';" in out.text
    out = await run_predefined_script("youtube-play-pause.py", "")
    assert out.ok and "let action = 'toggle';" in out.text
    # An unknown action used to become JS that paused the video; now it fails to start.
    bad = await run_predefined_script("youtube-play-pause.py", json.dumps(["x'); evil(); ('"]))
    assert bad.ok is False and "invalid choice" in bad.text


@pytest.mark.asyncio
async def test_daily_events_example_injects_integers_and_validates_them(examples):
    out = await run_predefined_script("gcal-daily-events-js.py", json.dumps(["25", "12", "2024"]))
    assert out.ok and "return getDailyEvents(25, 12, 2024);" in out.text
    for bad in (["0"], ["32"], ["1", "13"], ["1", "1", "2025", "x"], ["1); evil(("]):
        res = await run_predefined_script("gcal-daily-events-js.py", json.dumps(bad))
        assert res.ok is False, bad


@pytest.mark.asyncio
async def test_event_example_embeds_strings_as_json_literals(examples):
    title = 'Thinker / "Haik" 1:1 ' + chr(92) + ' \u2022'
    out = await run_predefined_script(
        "gcal-cal-event-js.py", json.dumps([title, "8", '11:30am"); evil(("']))
    assert out.ok, out.text
    expected_call = ("return getEventDetails(" + json.dumps(title) + ", 8, "
                     + json.dumps('11:30am"); evil(("') + ");")
    assert expected_call in out.text
    assert "@@" not in out.text


@pytest.mark.asyncio
async def test_dom_summarize_example_modes(examples):
    out = await run_predefined_script("dom-summarize.py", json.dumps(["onscreen", "withpos"]))
    assert out.ok
    assert 'const FILTER_MODE = "onscreen";' in out.text
    assert 'const INCLUDE_POS = "withpos";' in out.text
    bad = await run_predefined_script("dom-summarize.py", json.dumps(["all", "withpos"]))
    assert bad.ok is False and "requires 'onscreen' mode" in bad.text


@pytest.mark.asyncio
async def test_page_eval_runs_a_bundled_example_end_to_end(tools, server, examples):
    fn = (await tools.mcp.get_tool("page_eval")).fn
    await fn(script_name="youtube-play-pause.py", script_args=json.dumps(["play"]))
    assert len(server.sent) == 1
    assert "let action = 'play';" in server.sent[0]["data"]["script"]


# --- the Google Calendar scripts pick a day's cell from the grid's first column --------
# Google's month grid starts on the user's first day of the week (Monday on a European
# profile). The scripts used to assume Sunday, which shifted every date by one cell: a
# request for the 14th returned the 15th's events. They now read the first column header.
# The JS runs under node against a minimal stand-in for the DOM; a real Calendar page is
# only ever checked by hand.

NODE = shutil.which("node")

CALENDAR_HARNESS = """
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const [header, lang, targetIndex] = JSON.parse(process.argv[3]);
const eventEl = { textContent: '9am Standup', tagName: 'SPAN', className: 'nHqeVd' };
const containers = Array.from({ length: 35 }, (_, i) => ({
  querySelectorAll: () => (i === targetIndex ? [eventEl] : []),
}));
globalThis.document = {
  documentElement: { lang },
  querySelector: (sel) => (sel === '[role="columnheader"]' && header !== null ? { textContent: header } : null),
  querySelectorAll: (sel) => (sel === '.qLWd9c' ? containers : []),
};
console.log = () => {};  // the script logs its progress; stdout carries only the result
Promise.resolve((0, eval)(src)).then((r) => process.stdout.write(JSON.stringify(r)));
"""


async def run_calendar_day(tmp_path, monkeypatch, header, lang, target_index):
    monkeypatch.setenv("BROWSERPLUG_EXT_SCRIPTS", os.path.abspath(EXAMPLES_DIR))
    js = await run_predefined_script("gcal-daily-events-js.py", json.dumps(["14", "10", "2026"]))
    assert js.ok, js.text
    (tmp_path / "script.js").write_text(js.text, encoding="utf-8")
    (tmp_path / "harness.js").write_text(CALENDAR_HARNESS, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(tmp_path / "harness.js"), str(tmp_path / "script.js"),
         json.dumps([header, lang, target_index])],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize("header,lang,cell", [
    ("MondayMon", "en-GB", 16),    # 1 October 2026 is a Thursday: 3 cells after Monday
    ("SundaySun", "en-US", 17),
    ("SaturdaySat", "en-US", 18),
    ("poniedzia\u0142ekpon.", "pl", 16),  # the header text follows the page language
    (None, "en-US", 17),          # no header found: Sunday, the old behaviour
    ("zzz", "en-GB", 17),         # an unknown header falls back the same way
])
async def test_the_daily_example_finds_the_cell_for_the_grids_first_weekday(
        tmp_path, monkeypatch, header, lang, cell):
    result = await run_calendar_day(tmp_path, monkeypatch, header, lang, cell)
    assert result["success"] is True
    assert result["containerIndex"] == cell
    assert result["eventCount"] == 1  # the event sits in exactly that cell
    event = result["events"][0]
    assert event["times"] == ["9am"] and event["title"] == "Standup"  # minutes are optional


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.asyncio
async def test_the_daily_example_does_not_return_a_neighbouring_cell(tmp_path, monkeypatch):
    """Monday grid, event in the cell the OLD arithmetic would have picked (17)."""
    result = await run_calendar_day(tmp_path, monkeypatch, "MondayMon", "en-GB", 17)
    assert result["containerIndex"] == 16 and result["eventCount"] == 0
