"""Who is on the other end of an MCP session — the launcher walk and the friendly name.

Both halves failed on the SAME live connection (Reasonix, 2026-08-03) and neither had any
unit coverage, which is why the miss was silent: the popup's Connections panel showed
`Python — C:\\dr\\browserplug`, a row that is not obviously wrong (the launcher IS a python in
that directory), so the client looked like an unknown Python tool rather than a walk that
stopped one hop short.

  * resolve_client_chain used to demand the literal string "browserplug" in the launcher's
    cmdline. Reasonix spawns `python.exe server/launcher.py` — RELATIVE, from the project
    cwd — and the uv venv trampoline re-execs that same argv under the interpreter in
    ~/AppData/Roaming/uv/python/…, so neither hop names the project anywhere in argv. The
    walk broke at hop 0 and attributed the connection to the launcher itself.
  * friendly_from_cmdline then had no marker for the client it should have reached.

The fixtures below are the real command lines of that session (pids 34068 → 11396 → 1480).
"""

import os
import sys
import types
from unittest.mock import patch

import pytest

from server.reporting import client_identity

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UV_PYTHON = ("C:\\Users\\Someone\\AppData\\Roaming\\uv\\python\\"
             "cpython-3.12-windows-x86_64-none\\python.exe")
REASONIX_EXE = ("C:\\nvm\\v24.15.0\\node_modules\\reasonix\\node_modules\\"
                "@reasonix\\cli-win32-x64\\bin\\reasonix.exe")
DSH_BIN = ("C:\\Users\\Someone\\AppData\\Local\\npm-cache\\_npx\\1e7f6d9597241db0\\"
           "node_modules\\.bin\\..\\@deepseek-ai\\dsh\\lib\\bin.js")
POWERSHELL = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"


class FakeProc:
    """Only what the walk touches: cmdline(), cwd(), ppid(). Raises like psutil can."""

    def __init__(self, pid, argv, cwd=None, ppid=None, denied=()):
        self.pid, self._argv, self._cwd, self._ppid = pid, argv, cwd, ppid
        self._denied = set(denied)

    def _maybe_deny(self, what):
        if what in self._denied:
            raise RuntimeError(f"AccessDenied: {what}")

    def cmdline(self):
        self._maybe_deny("cmdline")
        return list(self._argv)

    def cwd(self):
        self._maybe_deny("cwd")
        return self._cwd

    def ppid(self):
        return self._ppid


# ---- is_launcher_process ------------------------------------------------------------

def test_relative_launcher_argv_is_recognized_via_the_process_cwd():
    """THE regression: no "browserplug" anywhere in argv, and it is still our launcher."""
    proc = FakeProc(34068, [UV_PYTHON, "server/launcher.py"], cwd=PROJECT_ROOT)
    assert "browserplug" not in " ".join(proc.cmdline()).lower()  # the old test would fail here
    assert client_identity.is_launcher_process(proc) is True


def test_absolute_launcher_argv_still_matches_without_consulting_cwd():
    """The Claude Code shape — and the cwd lookup must not even be needed for it."""
    proc = FakeProc(11396, ["C:\\dr\\browserplug\\.venv\\Scripts\\python.exe", "server/launcher.py"],
                    denied=("cwd",))
    assert client_identity.is_launcher_process(proc) is True


def test_the_trampoline_is_recognized_by_the_argv_it_re_execd():
    """A process's CURRENT cwd is not the cwd it was STARTED with, and the uv venv
    trampoline moves its own: live it runs `.venv/Scripts/python.exe server/launcher.py`
    — a relative argv that only ever worked from the project root — while reporting cwd
    %TEMP%. Nothing about the process alone can place it, so it is recognized by the hop
    below it: same script argv, and that hop is already a confirmed launcher of ours."""
    trampoline = FakeProc(21528, [".venv/Scripts/python.exe", "server/launcher.py"],
                          cwd="C:\\Users\\MARCIN~1\\AppData\\Local\\Temp")
    assert client_identity.is_launcher_process(trampoline) is False   # alone: unplaceable
    assert client_identity.is_launcher_process(
        trampoline, child_argv=[UV_PYTHON, "server/launcher.py"]) is True


def test_two_bare_interpreters_do_not_match_each_other():
    """The argv test compares the SCRIPT args; an empty tail must never count as a match."""
    assert client_identity.is_launcher_process(
        FakeProc(1, ["python.exe"], cwd="C:\\x"), child_argv=["python.exe"]) is False


def test_a_stranger_launcher_py_from_another_project_is_not_ours():
    """The walk must stop at an unrelated `launcher.py`, or it would credit the connection
    to that process's parent — a client this server never spoke to."""
    proc = FakeProc(500, ["python.exe", "launcher.py"], cwd="C:\\somebody\\else")
    assert client_identity.is_launcher_process(proc) is False


def test_the_client_itself_is_never_taken_for_a_launcher():
    proc = FakeProc(1480, [REASONIX_EXE], cwd="C:\\dr\\browserplug")
    assert client_identity.is_launcher_process(proc) is False


def test_an_unreadable_process_is_not_guessed_to_be_a_launcher():
    """psutil denies cmdline for some processes; degrade to "not a launcher" (attribute the
    connection to the peer we can see) rather than walking up to an unrelated parent."""
    assert client_identity.is_launcher_process(
        FakeProc(9, ["python.exe", "server/launcher.py"], denied=("cmdline",))) is False


# ---- resolve_client_chain -----------------------------------------------------------

@pytest.fixture
def fake_psutil():
    """Stand in for psutil inside resolve_client_chain (it imports it lazily)."""
    procs = {}
    mod = types.ModuleType("psutil")
    mod.Process = lambda pid: procs[pid]
    previous = sys.modules.get("psutil")
    sys.modules["psutil"] = mod
    try:
        yield procs
    finally:
        if previous is None:
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = previous


def _reasonix_chain(procs):
    """The live tree: uv-python launcher -> .venv trampoline launcher -> reasonix.exe."""
    procs[34068] = FakeProc(34068, [UV_PYTHON, "server/launcher.py"],
                            cwd=PROJECT_ROOT, ppid=11396)
    procs[11396] = FakeProc(11396, ["C:\\dr\\browserplug\\.venv\\Scripts\\python.exe",
                                    "server/launcher.py"], cwd=PROJECT_ROOT, ppid=1480)
    procs[1480] = FakeProc(1480, [REASONIX_EXE], cwd="C:\\dr\\browserplug", ppid=2480)


def test_the_walk_crosses_BOTH_launcher_hops_to_reach_the_client(fake_psutil):
    """A uv venv reaches the interpreter through a trampoline, so `launcher.py` appears
    twice before the client does. The launcher reported is the hop nearest the server —
    the process that actually holds the HTTP socket and stamps its own pid on /health."""
    _reasonix_chain(fake_psutil)
    with patch.object(client_identity, "pid_for_local_port", return_value=34068):
        assert client_identity.resolve_client_chain(4242) == (1480, 34068)


def test_the_claude_code_chain_whose_trampoline_moved_its_cwd(fake_psutil):
    """The same two-hop shape, but the trampoline reports %TEMP% (live: pids 37728 ->
    21528 -> 10516). Before the argv test this stopped one hop short and a Claude Code
    read as `Python — C:\\Users\\…\\Temp`, a row that looks like an unknown client."""
    fake_psutil[37728] = FakeProc(37728, [UV_PYTHON, "server/launcher.py"],
                                  cwd=PROJECT_ROOT, ppid=21528)
    fake_psutil[21528] = FakeProc(21528, [".venv/Scripts/python.exe", "server/launcher.py"],
                                  cwd="C:\\Users\\MARCIN~1\\AppData\\Local\\Temp", ppid=10516)
    fake_psutil[10516] = FakeProc(10516, ["C:\\Users\\x\\.local\\bin\\claude.exe",
                                          "--dangerously-skip-permissions"],
                                  cwd=PROJECT_ROOT, ppid=17856)
    with patch.object(client_identity, "pid_for_local_port", return_value=37728):
        assert client_identity.resolve_client_chain(4242) == (10516, 37728)


def test_a_direct_http_client_is_itself_with_no_launcher_hop(fake_psutil):
    fake_psutil[777] = FakeProc(777, ["curl.exe", "http://localhost:2830/mcp"], cwd="C:\\")
    with patch.object(client_identity, "pid_for_local_port", return_value=777):
        assert client_identity.resolve_client_chain(4242) == (777, None)


# ---- uv between the launcher and the client -------------------------------------------

UV_EXE = "C:\\Users\\x\\.local\\bin\\uv.exe"
UV_ENV_PYTHON = ("C:\\Users\\x\\AppData\\Local\\uv\\cache\\environments-v2\\"
                 "launcher-0123\\Scripts\\python.exe")
STUB = os.path.join(PROJECT_ROOT, "server", "launcher.py")


def _uv_chain(procs, uv_argv, uv_cwd=PROJECT_ROOT):
    """`uv run --script server/launcher.py`: real python -> env trampoline -> uv -> client."""
    procs[5100] = FakeProc(5100, [UV_PYTHON, STUB], cwd=PROJECT_ROOT, ppid=5090)
    procs[5090] = FakeProc(5090, [UV_ENV_PYTHON, STUB], cwd=PROJECT_ROOT, ppid=5080)
    procs[5080] = FakeProc(5080, uv_argv, cwd=uv_cwd, ppid=5000)
    procs[5000] = FakeProc(5000, ["C:\\Users\\x\\.local\\bin\\claude.exe"],
                           cwd=PROJECT_ROOT, ppid=4000)


def test_the_uv_run_chain_reaches_the_client(fake_psutil):
    """uv.exe stays resident between the client and the launcher for the whole session; the
    row must name the client, and the launcher is still the hop nearest the server."""
    _uv_chain(fake_psutil, [UV_EXE, "run", "--script", STUB])
    with patch.object(client_identity, "pid_for_local_port", return_value=5100):
        assert client_identity.resolve_client_chain(4242) == (5000, 5100)


def test_a_uv_whose_argv_cannot_place_the_launcher_is_crossed_by_name(fake_psutil):
    """A relative script path from another cwd (or uvx) is not recognisable as our launcher,
    but a uv.exe right above one is never the client."""
    _uv_chain(fake_psutil, [UV_EXE, "run", "--script", "server/launcher.py"],
              uv_cwd="C:\\somewhere\\else")
    with patch.object(client_identity, "pid_for_local_port", return_value=5100):
        assert client_identity.resolve_client_chain(4242) == (5000, 5100)


def test_uv_is_only_crossed_above_a_launcher(fake_psutil):
    """The process holding the socket is the client by definition, uv or not."""
    fake_psutil[5080] = FakeProc(5080, [UV_EXE, "run", "some-http-client"], cwd="C:\\",
                                 ppid=5000)
    with patch.object(client_identity, "pid_for_local_port", return_value=5080):
        assert client_identity.resolve_client_chain(4242) == (5080, None)


def test_is_uv_wrapper_matches_uv_and_uvx_by_image_name_only():
    assert client_identity.is_uv_wrapper(FakeProc(1, [UV_EXE, "run"])) is True
    assert client_identity.is_uv_wrapper(FakeProc(1, ["C:\\bin\\UVX.EXE", "ruff"])) is True
    assert client_identity.is_uv_wrapper(FakeProc(1, ["uv"])) is True
    assert client_identity.is_uv_wrapper(FakeProc(1, ["C:\\uv\\python.exe"])) is False
    assert client_identity.is_uv_wrapper(FakeProc(1, [])) is False
    assert client_identity.is_uv_wrapper(FakeProc(1, [UV_EXE], denied=("cmdline",))) is False


# ---- the proxy transport: two more hops below the launcher -----------------------------

PROXY_ENTRY = os.path.join(PROJECT_ROOT, "server", "proxy", "__main__.py")
SERVER_ENV_PYTHON = ("C:\\Users\\x\\AppData\\Local\\browserplug\\server-env\\"
                     "Scripts\\python.exe")
PROXY_ARGS = ["--backend-url", "http://127.0.0.1:2830/mcp", "--client-pid", "5000",
              "--launcher-pid", "5100", "--launcher-version", "1.9.10"]


def _proxy_chain(procs, uv_argv=None, uv_cwd=PROJECT_ROOT):
    """BROWSERPLUG_LAUNCHER_RELAY=0 under `uv run --script`: proxy python (the peer) ->
    proxy trampoline -> launcher python -> launcher trampoline -> uv.exe -> the client."""
    procs[5300] = FakeProc(5300, [UV_PYTHON, PROXY_ENTRY, *PROXY_ARGS], cwd=PROJECT_ROOT,
                           ppid=5200)
    procs[5200] = FakeProc(5200, [SERVER_ENV_PYTHON, PROXY_ENTRY, *PROXY_ARGS],
                           cwd="C:\\Users\\x\\AppData\\Local\\Temp", ppid=5100)
    _uv_chain(procs, uv_argv or [UV_EXE, "run", "--script", STUB], uv_cwd)


def test_the_proxy_chain_reaches_the_client_and_names_the_launcher(fake_psutil):
    """Five hops between the socket and the client. The launcher reported is the launcher
    python, not the proxy child that holds the socket: that is the pid the proxy stamps on
    its /health poll and /timing reports, so the map's launcher node gets its version."""
    _proxy_chain(fake_psutil)
    with patch.object(client_identity, "pid_for_local_port", return_value=5300):
        assert client_identity.resolve_client_chain(4242) == (5000, 5100)


def test_the_proxy_chain_with_a_uv_that_cannot_place_the_launcher(fake_psutil):
    _proxy_chain(fake_psutil, [UV_EXE, "run", "--script", "server/launcher.py"],
                 uv_cwd="C:\\somewhere\\else")
    with patch.object(client_identity, "pid_for_local_port", return_value=5300):
        assert client_identity.resolve_client_chain(4242) == (5000, 5100)


def test_the_hop_bound_covers_the_proxy_chain():
    """Pins a floor only. The two proxy-chain tests above resolve with a bound of 4 as
    well: the loop crosses four proxy and launcher hops, and uv.exe is crossed in the loop
    when its argv places the launcher, else by the is_uv_wrapper step after it.
    MAX_LAUNCHER_HOPS keeps room for a chain longer than any known today."""
    assert client_identity.MAX_LAUNCHER_HOPS >= 5


def test_a_proxy_run_by_hand_has_no_launcher_and_its_parent_is_the_client(fake_psutil):
    fake_psutil[5300] = FakeProc(5300, [UV_PYTHON, PROXY_ENTRY, *PROXY_ARGS],
                                 cwd=PROJECT_ROOT, ppid=4700)
    fake_psutil[4700] = FakeProc(4700, [POWERSHELL], cwd=PROJECT_ROOT, ppid=1)
    with patch.object(client_identity, "pid_for_local_port", return_value=5300):
        assert client_identity.resolve_client_chain(4242) == (4700, None)


def test_the_proxy_entry_by_absolute_path_is_recognised_without_its_cwd():
    proc = FakeProc(5300, [UV_PYTHON, PROXY_ENTRY, *PROXY_ARGS], denied=("cwd",))
    assert client_identity.is_proxy_process(proc) is True
    assert client_identity.is_launcher_process(proc) is False, "the proxy is not a launcher"


def test_the_proxy_trampoline_is_recognised_by_the_argv_it_re_execd():
    trampoline = FakeProc(5200, [".venv/Scripts/python.exe", "server/proxy/__main__.py"],
                          cwd="C:\\Users\\x\\AppData\\Local\\Temp")
    assert client_identity.is_proxy_process(trampoline) is False   # alone: unplaceable
    assert client_identity.is_proxy_process(
        trampoline, child_argv=[UV_PYTHON, "server/proxy/__main__.py"]) is True


def test_dash_m_server_proxy_counts_only_from_this_checkout():
    assert client_identity.is_proxy_process(
        FakeProc(1, [UV_PYTHON, "-m", "server.proxy"], cwd=PROJECT_ROOT)) is True
    assert client_identity.is_proxy_process(
        FakeProc(1, [UV_PYTHON, "-m", "server.proxy"], cwd="C:\\somebody\\else")) is False


def test_a_relative_proxy_path_is_resolved_against_the_cwd():
    assert client_identity.is_proxy_process(
        FakeProc(1, [UV_PYTHON, "server\\proxy\\__main__.py"], cwd=PROJECT_ROOT)) is True
    assert client_identity.is_proxy_process(
        FakeProc(1, [UV_PYTHON, "server/proxy/__main__.py"], cwd="C:\\somebody\\else")) is False


def test_the_launcher_and_other_processes_are_not_the_proxy():
    assert client_identity.is_proxy_process(
        FakeProc(1, [UV_PYTHON, STUB], cwd=PROJECT_ROOT)) is False
    assert client_identity.is_proxy_process(FakeProc(1, [REASONIX_EXE], cwd=PROJECT_ROOT)) is False
    assert client_identity.is_proxy_process(
        FakeProc(1, [UV_PYTHON, PROXY_ENTRY], denied=("cmdline",))) is False


# ---- shells between the launcher and the client --------------------------------------

def _dsh_through_a_shell(procs, shell_argv):
    """launcher -> launcher -> <shell> -> the DeepSeek Harness that spawned the lot."""
    procs[4764] = FakeProc(4764, [UV_PYTHON, "server/launcher.py"],
                           cwd=PROJECT_ROOT, ppid=4740)
    procs[4740] = FakeProc(4740, [".venv/Scripts/python.exe", "server/launcher.py"],
                           cwd=PROJECT_ROOT, ppid=4700)
    procs[4700] = FakeProc(4700, shell_argv, cwd=PROJECT_ROOT, ppid=31524)
    procs[31524] = FakeProc(31524, ["node", DSH_BIN, "web"], cwd=PROJECT_ROOT, ppid=37268)


def test_the_walk_crosses_a_shell_the_client_spawned_the_launcher_through(fake_psutil):
    """Live 2026-08-17: a DeepSeek Harness whose MCP config ran the launcher through
    PowerShell showed up as a SECOND client called `powershell.exe`, next to the same
    harness's directly-spawned row. A wrapper shell is not a client; cross it and both
    connections land on the one process that is."""
    _dsh_through_a_shell(fake_psutil, [POWERSHELL, "-NoProfile", "-NonInteractive",
                                       "-Command", "& .venv\\Scripts\\python.exe launcher.py"])
    with patch.object(client_identity, "pid_for_local_port", return_value=4764):
        assert client_identity.resolve_client_chain(4242) == (31524, 4764)


def test_a_cmd_exe_wrapper_is_crossed_too(fake_psutil):
    """The npm/npx shape on Windows: `cmd.exe /d /s /c <command>`."""
    _dsh_through_a_shell(fake_psutil, ["C:\\Windows\\system32\\cmd.exe", "/d", "/s", "/c",
                                       "python server\\launcher.py"])
    with patch.object(client_identity, "pid_for_local_port", return_value=4764):
        assert client_identity.resolve_client_chain(4242) == (31524, 4764)


def test_an_interactive_shell_is_where_the_walk_stops(fake_psutil):
    """No command flag = a terminal a human typed the launcher into. There the shell IS
    the nearest thing to a client; walking past it would name whatever owns the window."""
    _dsh_through_a_shell(fake_psutil, [POWERSHELL])
    with patch.object(client_identity, "pid_for_local_port", return_value=4764):
        assert client_identity.resolve_client_chain(4242) == (4700, 4764)


def test_a_shell_is_only_crossed_above_a_launcher(fake_psutil):
    """Whoever holds the HTTP socket is a process this server talks to directly, so it is
    the client by definition — never a wrapper, whatever its argv looks like."""
    fake_psutil[4700] = FakeProc(4700, [POWERSHELL, "-Command", "something"],
                                 cwd=PROJECT_ROOT, ppid=31524)
    fake_psutil[31524] = FakeProc(31524, ["node", DSH_BIN, "web"], cwd=PROJECT_ROOT)
    with patch.object(client_identity, "pid_for_local_port", return_value=4700):
        assert client_identity.resolve_client_chain(4242) == (4700, None)


# ---- friendly_from_cmdline ----------------------------------------------------------

# VS Code's default per-user install location, and its Insiders sibling. The editor and
# the process hosting its MCP client are the same binary; only the argv separates them.
VSCODE_EXE = "C:\\Users\\dev\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe"
CODEX_EXE = "C:\\Users\\dev\\AppData\\Local\\OpenAI\\Codex\\bin\\codex.exe"
# What a real install looks like: the native build lands under a per-build hash
# directory, so the marker must key on the OpenAI/Codex segment and never on a fixed
# bin/codex.exe tail.
CODEX_HASHED_EXE = ("C:\\Users\\dev\\AppData\\Local\\OpenAI\\Codex\\bin"
                    "\\fd4c151a749f3ab4\\codex.exe")
VSCODE_INSIDERS_EXE = "C:\\Users\\dev\\AppData\\Local\\Programs\\Microsoft VS Code Insiders\\Code.exe"

@pytest.mark.parametrize("cmdline,exe_path,expected", [
    # The native CLI: the only tell is the @reasonix npm path it sits under, and the exe
    # name alone ("reasonix.exe") is what the row would otherwise print.
    (REASONIX_EXE, REASONIX_EXE, "Reasonix"),
    # The node entry point, when Reasonix is launched from its JS shim instead.
    ("C:\\nvm4w\\nodejs\\node.exe C:/nvm4w/nodejs/node_modules/reasonix/bin/reasonix.js",
     "C:\\nvm4w\\nodejs\\node.exe", "Reasonix"),
    # DSH (DeepSeek Harness): a node CLI, so exe_path is bare node.exe and the scoped
    # package name in argv is the whole tell. Both shapes it runs in. The name is the
    # SHORT one; this string is what the popup prints on the row, and a row is one line.
    (f"node {DSH_BIN} web", "C:\\nvm\\v24.15.0\\node.exe", "DSH"),
    ("C:\\nvm4w\\nodejs\\node.exe C:\\nvm4w\\nodejs/node_modules/npm/bin/npx-cli.js "
     "@deepseek-ai/dsh web", "C:\\nvm4w\\nodejs\\node.exe", "DSH"),
    # A prompt in argv that names another agent must not rename the row (the reason the
    # scoped-package markers are tested before the bare-word ones).
    (f"node {DSH_BIN} run -p \"ask claude about this\"", "C:\\nvm\\v24.15.0\\node.exe",
     "DSH"),
    # Unchanged neighbours — one marker must not swallow another.
    ("node C:\\Users\\x\\AppData\\Roaming\\npm\\claude.js", "C:\\nvm4w\\nodejs\\node.exe",
     "Claude Code"),
    ("python.exe C:\\tools\\vibe.exe", UV_PYTHON, "Mistral Vibe"),
    ("kilo.exe", "C:\\npm\\node_modules\\@kilocode\\cli\\bin\\kilo.exe", "Kilo CLI"),
    # Codex CLI, both install shapes: the npm package, and the native build under
    # %LOCALAPPDATA%/OpenAI/Codex/bin whose exe name alone reads as "codex.exe".
    ("node C:\\npm\\node_modules\\@openai\\codex\\bin\\codex.js exec",
     "C:\\nvm4w\\nodejs\\node.exe", "Codex CLI"),
    (CODEX_EXE, CODEX_EXE, "Codex CLI"),
    # The shape actually observed on this machine, argv and all: an `app-server` run
    # whose -c overrides embed a JSON object with its own quoting.
    (CODEX_HASHED_EXE + ' -c features.code_mode_host=true app-server'
     ' --analytics-default-enabled -c mcp_servers.codex_app={"command"="cmd.exe"}',
     CODEX_HASHED_EXE, "Codex CLI"),
    # `codex exec "<prompt>"` carries the prompt in argv, so a prompt naming another
    # agent must not rename the row — the reason the marker sits in the early group.
    (CODEX_EXE + ' exec "ask claude to review this"', CODEX_EXE, "Codex CLI"),
    ("duo.exe", "C:\\Program Files\\glab-cli\\bin\\duo.exe", "GitLab Duo CLI"),
    ("Code.exe", "C:\\Program Files\\Microsoft VS Code\\Code.exe", "Visual Studio Code"),
    # VS Code hosts an MCP client in a spawned utility process, and what drives that is
    # GitHub Copilot Chat. Same exe as the editor above; the argv is the whole tell.
    (VSCODE_EXE + " --type=utility --utility-sub-type=node.mojom.NodeService --lang=en-US"
     " --service-sandbox-type=none --dns-result-order=ipv4first",
     VSCODE_EXE, "GitHub Copilot Chat"),
    # An Insiders install lives one directory along and must resolve the same way.
    (VSCODE_INSIDERS_EXE + " --type=utility --utility-sub-type=node.mojom.NodeService",
     VSCODE_INSIDERS_EXE, "GitHub Copilot Chat"),
    # Nothing recognized: the caller's default stands.
    ("python.exe some_script.py", UV_PYTHON, "Python"),
])
def test_friendly_names(cmdline, exe_path, expected):
    assert client_identity.friendly_from_cmdline(cmdline, exe_path, "Python") == expected


def test_the_editor_and_the_extension_talking_through_it_are_separate_rows():
    """Both are `Code.exe` under the same install directory. Collapsing them would put
    the editor's name on every Copilot tool call, and the popup row is the only place a
    user sees which of their tools is driving the browser."""
    editor = client_identity.friendly_from_cmdline("Code.exe", VSCODE_EXE, "Node")
    host = client_identity.friendly_from_cmdline(
        VSCODE_EXE + " --type=utility --utility-sub-type=node.mojom.NodeService",
        VSCODE_EXE, "Node")
    assert (editor, host) == ("Visual Studio Code", "GitHub Copilot Chat")


@pytest.mark.parametrize("friendly,vendor", [
    ("GitHub Copilot Chat", "Microsoft"),
    ("Visual Studio Code", "Microsoft"),
    ("Claude Code", "Anthropic"),
    ("Codex CLI", "OpenAI"),
    ("DSH", "DeepSeek"),
    ("pi", "Earendil Works"),
    # An unrecognized client has no vendor rather than a guessed one; the popup then
    # prints nothing at all for it.
    ("Node", None),
    ("MCP client", None),
])
def test_a_named_client_carries_its_vendor(friendly, vendor):
    assert client_identity.client_vendor(friendly) == vendor


def test_every_vendor_key_is_a_name_the_identifier_can_actually_return():
    """The two tables are keyed on the same strings. A rename in one and not the other
    leaves a vendor nothing can reach, which nothing else here would fail on."""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(client_identity.friendly_from_cmdline)))
    # Only a string that a `return` statement hands back counts. A name that survives in a
    # comment or the docstring after its branch is deleted must not satisfy this.
    returned = {n.value.value for n in ast.walk(tree)
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)}
    for friendly in client_identity.CLIENT_VENDORS:
        assert friendly in returned, f"{friendly} is not a name friendly_from_cmdline returns"


# ---- sanitized_cmdline (what "Copy connection info" carries) -------------------------

def test_the_copied_command_line_keeps_what_identifies_the_client():
    """exe_path is `node.exe` for every node CLI there is; argv is what names one. This is
    the field that makes an unrecognized row worth copying at all."""
    line = client_identity.sanitized_cmdline(["node", DSH_BIN, "web"])
    assert "@deepseek-ai" in line and line.startswith("node ")


@pytest.mark.parametrize("argv,masked", [
    (["mcp-client", "--api-key", "sk-ant-secret-value"], "sk-ant-secret-value"),
    (["mcp-client", "--token=ghp_secret_value"], "ghp_secret_value"),
    (["mcp-client", "--auth-header", "Bearer abc123"], "Bearer abc123"),
    (["mcp-client", "--password", "hunter2"], "hunter2"),
])
def test_a_secret_never_rides_along_into_the_clipboard(argv, masked):
    """A copied row is something the user pastes into a chat or a bug report, and an MCP
    client started with a key on its command line must not put it there."""
    line = client_identity.sanitized_cmdline(argv)
    assert masked not in line
    assert "***" in line
    assert "mcp-client" in line   # the identifying part survives the masking


def test_one_runaway_argument_cannot_fill_the_clipboard():
    """An agent CLI in `-p` mode carries the whole prompt in argv."""
    line = client_identity.sanitized_cmdline(["node", "cli.js", "-p", "x" * 5000])
    assert len(line) <= 501            # the line cap, plus the ellipsis
    assert line.startswith("node cli.js -p ")


def test_no_command_line_at_all_reads_as_absent_rather_than_empty():
    """psutil denies cmdline for some processes; the popup must skip the row, not print
    a `cmdline` label with nothing after it."""
    assert client_identity.sanitized_cmdline([]) is None
    assert client_identity.sanitized_cmdline(None) is None


# S12: secrets that are not the value of a dash flag ----------------------------------

@pytest.mark.parametrize("argv,secret", [
    (["run", "API_KEY=s3cr3tvalue"], "s3cr3tvalue"),
    (["run", "GITHUB_TOKEN=ghp_abc123"], "ghp_abc123"),
    (["run", "PGPASSWORD=hunter2"], "hunter2"),
    (["run", "client_secret=abc987"], "abc987"),
    (["run", "password=\"two words\""], "two words"),
    (["run", "password='two words'"], "two words"),
    (["run", "apikey: abc987"], "abc987"),
    (["curl", "-H", "Authorization: Bearer abc987"], "abc987"),
    (["curl", "-H", "authorization:basic dXNlcjpwdw"], "dXNlcjpwdw"),
    (["curl", "-H", "X-Api-Key: abc987"], "abc987"),
    (["curl", "--header=Authorization: Bearer abc987"], "abc987"),
    (["sh", "-c", "curl -H \"Authorization: Bearer abc987\" host"], "abc987"),
    (["sh", "-c", "curl -H 'Authorization: Bearer abc987' host"], "abc987"),
    (["sh", "-c", "run --token=abc987 --x 1"], "abc987"),
    (["xfreerdp", "/v:host", "/password:hunter2"], "hunter2"),
    (["xfreerdp", "/v:host", "/p:hunter2"], "hunter2"),
    (["run", "Bearer abc987"], "abc987"),
    (["run", "--db=postgres://user:pw0rd@db.example.org/x"], "pw0rd"),
    (["run", "https://user:pw0rd@example.org/x"], "pw0rd"),
    (["run", "https://example.org/x?token=abc987&a=1"], "abc987"),
    (["run", "--url=https://example.org/x?api_key=abc987"], "abc987"),
])
def test_secrets_outside_dash_flags_are_masked(argv, secret):
    line = client_identity.sanitized_cmdline(argv)
    assert secret not in line
    assert line.startswith(argv[0])


def test_masking_keeps_the_identifying_text_around_a_secret():
    line = client_identity.sanitized_cmdline(
        ["node", DSH_BIN, "HOME=/h", "API_KEY=abc987", "--port", "80"])
    assert "@deepseek-ai" in line and "HOME=/h" in line and "--port 80" in line
    assert "API_KEY=***" in line


# ---- masking is linear: a hostile argument must not freeze the server ---------------------
# 2026-10-02: one identifier-like argument of 4000 characters took the old pattern 51 s, on
# the thread that serves every client, with the GIL held.

def _random_identifier(n, seed=1):
    import random
    rng = random.Random(seed)
    words = ["key", "token", "secret", "password", "auth", "bearer", "credential",
             "apikey", "api_key"]
    out, size = [], 0
    while size < n:
        piece = (rng.choice(words) if rng.random() < 0.3
                 else rng.choice("abcdefghij0123456789_.-") * rng.randint(1, 5))
        out.append(piece)
        size += len(piece)
    return "".join(out)[:n]


HOSTILE_ARGUMENTS = {
    "keyword run": "key" * 1400,
    "long run then keyword": "a" * 4000 + "key",
    "auth run": "auth_" * 800,
    "random keyword soup": _random_identifier(8000),
    "enormous run": "x" * 200_000,
    "keyword names without a separator": ("token" + "a" * 60 + "=") * 600,
    "quote soup": 'key="' * 3000,
    "labels": "authorization:" * 3000,
    "bearer": "bearer " * 20_000,
    "urls": "http://a/?" + "key=&" * 3000,
}


@pytest.mark.parametrize("name", sorted(HOSTILE_ARGUMENTS))
def test_a_hostile_argument_is_described_in_milliseconds(name):
    import time
    argv = ["prog", HOSTILE_ARGUMENTS[name], HOSTILE_ARGUMENTS[name][::-1]]
    start = time.perf_counter()
    line = client_identity.sanitized_cmdline(argv)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05, f"{name}: {elapsed * 1000:.0f} ms"
    assert line is None or len(line) <= 501


def test_a_thousand_arguments_cost_no_more_than_the_line_that_can_be_shown():
    import time
    start = time.perf_counter()
    line = client_identity.sanitized_cmdline(["prog"] + ["--flag", "value" * 50] * 5000)
    assert time.perf_counter() - start < 0.05
    assert line.startswith("prog --flag")


def test_a_quoted_secret_is_masked_even_when_the_argument_was_cut_before_its_quote_closed():
    """Each argument is cut at 1024 characters before masking. The closing quote can be
    on the far side of the cut; the value is still masked to the end of what is left."""
    line = client_identity.sanitized_cmdline(["run", 'password="' + "s3cr3t " * 400 + '"'])
    assert "s3cr3t" not in line and "password=" in line


def test_the_name_before_a_secret_keyword_is_kept_whole():
    line = client_identity.sanitized_cmdline(["run", "my-service.api_key=abc987", "ok=1"])
    assert line == "run my-service.api_key=*** ok=1"


def test_a_secret_beyond_the_cut_is_never_shown():
    """The shown line stops at 140 characters per argument; a secret that starts after the
    cut cannot appear in it."""
    line = client_identity.sanitized_cmdline(["run", "x" * 2000 + " api_key=abc987"])
    assert "abc987" not in line


@pytest.mark.parametrize("extra", [0, 1, 63, 64, 65, 66, 100, 255, 256])
def test_a_long_name_after_the_secret_keyword_still_has_its_value_masked(extra):
    """The first cap on the name after the keyword was 64: `key` plus 65 name characters,
    then `=SECRET`, was shown in full where the unbounded pattern masked it."""
    name = "key" + "n" * extra
    assert client_identity._mask_secret_text(f"{name}=S3CR3T") == f"{name}=***", (
        f"{extra} name characters after the keyword leaked")


@pytest.mark.parametrize("extra", [0, 64, 65, 100])
def test_a_long_name_is_masked_in_the_shown_command_line_too(extra):
    line = client_identity.sanitized_cmdline(["run", "key" + "n" * extra + "=S3CR3T", "ok=1"])
    assert "S3CR3T" not in line and "=***" in line and "ok=1" in line


def test_a_name_past_the_cap_has_its_value_beyond_the_cut_anyway():
    """Past 256 characters of name the pattern gives up, and so does the display: an
    argument is shown for 140 characters, so the value that follows could never appear."""
    line = client_identity.sanitized_cmdline(["run", "key" + "n" * 300 + "=S3CR3T"])
    assert "S3CR3T" not in line


# ---- tracking is split: a cheap note on the loop, the lookup on a worker -------------------

class _Boom:
    """psutil stand-ins that fail the test if the cheap half touches them."""

    @staticmethod
    def chain(_port):
        raise AssertionError("the loop-side note walked the process table")

    @staticmethod
    def describe(_pid):
        raise AssertionError("the loop-side note described a process")


def test_noting_a_client_never_looks_at_a_process():
    clients = {}
    with patch.object(client_identity, "resolve_client_chain", _Boom.chain), \
            patch.object(client_identity, "describe_pid", _Boom.describe):
        entry = client_identity.note_http_client(clients, "s1", ("127.0.0.1", 5000), 4242)
    assert entry is clients["s1"]
    assert entry["pid"] == 4242, "the forwarded pid is known at once"
    assert entry["friendly"] is None and entry["identified"] is False
    assert entry["history"] == []


def test_identifying_fills_the_entry_the_note_made():
    clients = {}
    entry = client_identity.note_http_client(clients, "s1", ("127.0.0.1", 5000))
    described = {"pid": 77, "friendly": "Claude Code", "exe_path": "C:/c.exe",
                 "cmdline": "claude", "cwd": "C:/w", "start_time": "t0"}
    with patch.object(client_identity, "resolve_client_chain", lambda port: (77, 66)), \
            patch.object(client_identity, "describe_pid",
                         lambda pid: dict(described, pid=pid, start_time=f"t{pid}")):
        client_identity.identify_http_client(entry, ("127.0.0.1", 5000))
    assert entry["identified"] is True
    assert (entry["pid"], entry["friendly"], entry["launcher_pid"]) == (77, "Claude Code", 66)
    assert entry["launcher_start_time"] == "t66"
    assert entry["first_seen"] <= entry["last_activity"]


def test_a_session_is_looked_up_once_not_on_every_request():
    clients = {}
    first = client_identity.note_http_client(clients, "s1", ("127.0.0.1", 5000))
    second = client_identity.note_http_client(clients, "s1", ("127.0.0.1", 5000))
    assert first is not None and second is None, (
        "a lookup already started for this session; the second request must not start one")


def test_an_unresolved_peer_is_tried_again_but_not_on_every_request(monkeypatch):
    clients = {}
    now = [100.0]
    monkeypatch.setattr(client_identity, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    entry = client_identity.note_http_client(clients, "s1", ("127.0.0.1", 5000))
    with patch.object(client_identity, "resolve_client_chain", lambda port: (None, None)), \
            patch.object(client_identity, "describe_pid", lambda pid: {"pid": None}):
        client_identity.identify_http_client(entry, ("127.0.0.1", 5000))
    assert entry["identified"] is False
    assert client_identity.note_http_client(clients, "s1", ("127.0.0.1", 5000)) is None
    now[0] += client_identity.IDENTITY_RETRY_SECONDS + 0.1
    assert client_identity.note_http_client(clients, "s1", ("127.0.0.1", 5000)) is entry


def test_a_request_without_a_session_or_a_way_to_name_its_client_files_nothing():
    clients = {}
    assert client_identity.note_http_client(clients, None, ("127.0.0.1", 1)) is None
    assert clients == {}
    entry = client_identity.note_http_client(clients, "s1", None)
    assert entry is None and "s1" in clients, "recorded, but nothing to look up"


def test_the_synchronous_form_still_files_a_fully_identified_row():
    clients = {}
    with patch.object(client_identity, "resolve_client_chain", lambda port: (88, None)), \
            patch.object(client_identity, "describe_pid",
                         lambda pid: {"pid": pid, "friendly": "Codex CLI"}):
        client_identity.track_http_client(clients, "s1", ("127.0.0.1", 5000))
    assert clients["s1"]["pid"] == 88 and clients["s1"]["friendly"] == "Codex CLI"
