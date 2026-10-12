"""Who is on the other end of a socket, resolved from the local process table.

An MCP client is known only by the TCP port it connected from. These functions map that
port to a pid, walk up through our stdio launcher and any shell that started it, and name
the client from its command line and exe path. `browser_instance_identity` does the same
for a browser: it turns the pid that owns the extension socket into the top-most process
of that browser, which brands the registry entry.

Everything is a plain synchronous function over psutil (imported inside each function, so
a failed import degrades to an empty answer) and none of it keeps state. Callers can run
them inline or hand them to an executor. `track_http_client` and the launcher-version
helpers take the dict they update; the server owns those dicts.

Nothing here imports server.py, and importing the module has no side effects.
"""

import logging
import os
import re
import time
from datetime import datetime

from server.browsers import browser_identity
from server.reporting.arg_redaction import redact_urls_in_text

logger = logging.getLogger(__name__)


def pid_for_local_port(port):
    """PID of the process whose ESTABLISHED socket has local port == port."""
    if not port:
        return None
    try:
        import psutil
        for c in psutil.net_connections(kind='inet'):
            if c.laddr and c.laddr.port == port and c.pid and c.status == psutil.CONN_ESTABLISHED:
                return c.pid
    except Exception as e:
        logger.debug(f"pid lookup failed for port {port}: {e}")
    return None


# Our own launcher, absolute: the script a client spawns to reach this server.
LAUNCHER_PATH = os.path.normcase(os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "launcher.py")))
# The real launcher (server/launcher.py is a forwarding stub that runs it).
REAL_LAUNCHER_PATH = os.path.normcase(os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                 "launcher", "launcher.py")))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The proxy transport's process, which the launcher runs by this absolute path
# (BROWSERPLUG_LAUNCHER_RELAY=0); `-m server.proxy` from the checkout is the other spelling.
PROXY_ENTRY_PATH = os.path.normcase(os.path.join(_PROJECT_ROOT, "server", "proxy",
                                                 "__main__.py"))

# How many proxy and launcher hops the loop in resolve_client_chain may cross. The longest
# real chain is the proxy transport's under `uv run --script`: proxy python -> proxy
# trampoline -> launcher python -> launcher trampoline -> uv.exe -> client. Four of those
# hops are proxy or launcher processes. uv.exe is crossed in the loop when its argv places
# the launcher, and otherwise by the is_uv_wrapper step after it, so the chain resolves
# with a bound of 4 too. No chain known today needs more than that; the bound leaves room
# for a longer one.
MAX_LAUNCHER_HOPS = 6


def is_proxy_process(proc, child_argv=None):
    """Is this the launcher's proxy transport child (server/proxy/__main__.py)?

    It holds the HTTP socket for a proxy session, and the launcher that started it is its
    parent (two hops up, through the server environment's trampoline). It is recognised
    by an argv token naming this checkout's proxy entry (or, as for the launcher, any path
    under a "browserplug" directory), or by `-m server.proxy` run from this checkout.
    `child_argv` is the argv of a hop already confirmed to be the proxy: the trampoline
    above it re-execs the identical script argv.
    """
    try:
        argv = [str(a) for a in proc.cmdline()]
    except Exception:
        return False
    if child_argv is not None and argv[1:] and argv[1:] == [str(a) for a in child_argv[1:]]:
        return True
    cmd = " ".join(argv).replace("\\", "/").lower()
    if "server/proxy" not in cmd and "server.proxy" not in cmd:
        return False
    try:
        cwd = proc.cwd()
    except Exception:
        cwd = None
    for i, token in enumerate(argv[1:], 1):
        if token == "-m" and argv[i + 1:i + 2] == ["server.proxy"]:
            if cwd and os.path.normcase(os.path.abspath(cwd)) == os.path.normcase(_PROJECT_ROOT):
                return True
            continue
        if not token.replace("\\", "/").lower().endswith("server/proxy/__main__.py"):
            continue
        if not os.path.isabs(token) and not cwd:
            continue
        resolved = os.path.normcase(os.path.abspath(os.path.join(cwd or "", token)))
        if resolved == PROXY_ENTRY_PATH or "browserplug" in resolved.replace("\\", "/"):
            return True
    return False


def is_launcher_process(proc, child_argv=None):
    """Is this process one of OUR stdio launchers (so the real client is further up)?

    A substring test for "browserplug" on the raw cmdline is not enough, and the miss is
    silent: the walk stops at hop 0, the connection is attributed to the LAUNCHER,
    and the client reads as a bare "Python" whose cwd happens to be the project.
    Two real shapes carry no project path in argv at all:
      • a client that spawns the launcher with a RELATIVE script path from the
        project cwd (`python.exe server/launcher.py`: Reasonix does this, where
        Claude Code passes the absolute path), and
      • the uv-managed venv trampoline, which re-execs the same relative argv under
        ~/AppData/Roaming/uv/python/…/python.exe, so even the interpreter path
        stops naming the project (that hop is the one that talks HTTP to us).
    So resolve each `launcher.py` argv token against the process's OWN cwd and
    compare with our launcher; the "browserplug" test stays as the fallback that still
    recognizes a launcher run from a different checkout.

    `child_argv` is the argv of the hop we just came from, when that hop was already
    confirmed to be our launcher: the ONLY way to recognize the trampoline, whose
    cwd cannot be trusted (below).
    """
    try:
        argv = proc.cmdline()
    except Exception:
        return False
    cmd = " ".join(argv).lower()
    if "launcher.py" not in cmd:
        return False
    if "browserplug" in cmd:
        return True
    # The trampoline hop: a process's CURRENT cwd is not the one it was launched
    # with, and the uv venv trampoline moves its own: live, `.venv/Scripts/
    # python.exe server/launcher.py` reports cwd %TEMP%, which no resolve-against-cwd
    # test can rescue (that relative argv only ever worked from the project root).
    # But it re-execs the identical SCRIPT argv, and the child it re-exec'd into is
    # already a confirmed launcher of ours: same launcher, one process lower.
    if child_argv is not None and argv[1:] and argv[1:] == child_argv[1:]:
        return True
    try:
        cwd = proc.cwd()
    except Exception:
        return False
    for token in argv:
        if not token.lower().replace("\\", "/").endswith("launcher.py"):
            continue
        try:
            resolved = os.path.normcase(os.path.abspath(os.path.join(cwd, token)))
        except Exception:
            continue
        if resolved in (LAUNCHER_PATH, REAL_LAUNCHER_PATH) or "browserplug" in resolved.replace("\\", "/"):
            return True
    return False

# Shells that can sit BETWEEN a client and the launcher it spawned, and the flags
# that mean this one was handed a command to run rather than a human to talk to.
SHELL_WRAPPER_NAMES = frozenset({
    "cmd.exe", "cmd", "powershell.exe", "powershell", "pwsh.exe", "pwsh",
    "bash.exe", "bash", "sh.exe", "sh", "zsh.exe", "zsh", "wsl.exe",
})
SHELL_COMMAND_FLAGS = frozenset({"-c", "/c", "-command", "-encodedcommand", "-ec"})
# uv, when a client registers `uv run --script <checkout>/server/launcher.py`: it stays
# resident between the client and the launcher's trampoline for the whole session. Same
# names as launcher/client_pid.py (test_client_pid keeps them equal).
UV_WRAPPER_NAMES = frozenset({"uv.exe", "uv", "uvx.exe", "uvx"})


def is_uv_wrapper(proc):
    """Is this uv (or uvx) itself? Only asked of the process right above a launcher hop,
    where uv is never the client: it ran the launcher on the client's behalf."""
    try:
        argv = proc.cmdline()
    except Exception:
        return False
    if not argv:
        return False
    return os.path.basename(str(argv[0]).replace("\\", "/")).lower() in UV_WRAPPER_NAMES


def is_shell_wrapper(proc):
    """Is this a shell that was handed a COMMAND (cmd /c …, powershell -Command …)?

    Such a shell is a spawn wrapper: the real client asked it to start the launcher,
    so it sits between the two and the chain walk has to cross it. Stopping there
    credits the whole connection to "powershell.exe", which is what the DeepSeek
    Harness row read as (2026-08-17) while its config still went through a shell.

    A shell with no command flag is an INTERACTIVE one, i.e. a terminal a human typed
    the launcher into; there the shell is the nearest thing to a client there is, and
    walking past it would name whatever happens to own the terminal window.
    """
    try:
        argv = proc.cmdline()
    except Exception:
        return False
    if not argv:
        return False
    exe = os.path.basename(str(argv[0]).replace("\\", "/")).lower()
    if exe not in SHELL_WRAPPER_NAMES:
        return False
    return any(str(tok).lower() in SHELL_COMMAND_FLAGS for tok in argv[1:])


def resolve_client_chain(peer_port):
    """(client_pid, launcher_pid) for an HTTP peer port.

    When a client connects through the stdio launcher (server/launcher.py),
    the TCP peer is the launcher/proxy, not Claude Code. Detect that and
    attribute the connection to the launcher's parent (the Claude Code that
    spawned it) so the per-client cwd/PID diagnostics stay accurate, but
    KEEP the launcher's own pid: the options page's connection map draws the
    launcher as its own hop. Direct HTTP clients (no launcher) come back as
    (peer_pid, None).

    The walk allows several launcher hops in a row on purpose: a uv environment reaches
    the interpreter through a trampoline, so `launcher.py` appears TWICE in the
    chain before the client does, three times under `uv run --script`, whose uv.exe
    argv names the launcher too. A uv.exe whose argv does not place the launcher (a
    relative path from another cwd, uvx) is crossed on its own, once, right above the
    launcher hops. Past those it crosses SCRIPTED SHELLS too, for the same reason: a
    client that spawns the launcher through `cmd /c` or `powershell -Command` is one hop
    further up still, and the shell it used says nothing about who it is.

    On the proxy transport the peer is the launcher's proxy child (is_proxy_process),
    with its own trampoline above it. Those hops are crossed first and are not the
    launcher: the launcher is the first launcher.py hop above them, the process whose
    pid the proxy stamps on its /health poll and /timing reports, so the map's launcher
    node and its version line up.
    """
    pid = pid_for_local_port(peer_port)
    launcher_pid = None
    try:
        import psutil
        hops = 0
        child_argv = None   # argv of the last CONFIRMED hop
        child_kind = None   # what that hop was: "proxy" or "launcher"
        while pid and hops < MAX_LAUNCHER_HOPS:
            proc = psutil.Process(pid)
            if launcher_pid is None and is_proxy_process(
                    proc, child_argv if child_kind == "proxy" else None):
                child_kind = "proxy"
            elif is_launcher_process(proc, child_argv if child_kind == "launcher" else None):
                child_kind = "launcher"
                if launcher_pid is None:
                    launcher_pid = pid   # the launcher hop nearest the server is THE launcher
            else:
                break
            try:
                child_argv = proc.cmdline()
            except Exception:
                child_argv = None
            pid = proc.ppid()
            hops += 1
        if launcher_pid and pid:
            proc = psutil.Process(pid)
            if is_uv_wrapper(proc):
                pid = proc.ppid()
        # Only ABOVE a launcher, because that is the only place a wrapper shell can
        # be: whoever holds the HTTP socket is a process we talk to directly, and a
        # shell never does that. Bounded like the launcher walk; a chain of shells
        # this deep is somebody else's tree, not a client we can name.
        shell_hops = 0
        while launcher_pid and pid and shell_hops < 3:
            proc = psutil.Process(pid)
            if not is_shell_wrapper(proc):
                break
            pid = proc.ppid()
            shell_hops += 1
    except Exception as e:
        logger.debug(f"client pid resolve failed for port {peer_port}: {e}")
    return pid, launcher_pid


def friendly_from_cmdline(cmdline, exe_path, default):
    """Name a known MCP client from its command line and full exe path.

    The process name alone is uninformative when the client is a tool launched
    under a generic interpreter (python/node) or a bland native shim. Each
    marker is matched against the field it actually lives in for the *resolved*
    client process (both lowercased, path separators normalized to '/'):
      • cmdline: the launched app/script in argv. Mistral Vibe is a uv Python
        tool run as `python.exe …\vibe.exe` (exe_path is just the cpython
        binary); node-launched Claude Code carries "claude" in its argv script
        path (a native `claude.exe` is already named before this helper).
      • cmdline, before every other marker: pi (`@earendil-works/pi-coding-agent`)
        runs as `node …/pi-coding-agent/dist/cli.js`, and in `-p` mode its argv also
        carries the user's prompt, so it is matched first; a prompt naming another
        agent must not rename the row. The DeepSeek Harness (`@deepseek-ai/dsh`,
        `node …/@deepseek-ai/dsh/lib/bin.js`) is another node CLI that takes a prompt
        in argv, and is matched in the same early group for the same reason. Codex
        CLI (`codex exec "<prompt>"`) is the third, and it is matched on both fields
        because it ships as an npm package AND as a native binary, which lands
        under %LOCALAPPDATA%/OpenAI/Codex/bin once separators are normalized.
      • exe_path: the install path. Kilo CLI ships a native `kilo.exe` whose
        only tell is the `@kilocode/cli` npm path it sits under; VS Code is a
        `Code.exe` under `Microsoft VS Code`. VS Code does not connect to an MCP
        server as the editor, though: it hosts the client in a spawned utility
        process (`--type=utility --utility-sub-type=node.mojom.NodeService`), and
        what drives that is GitHub Copilot Chat. So the two are separate rows —
        the utility process is named for the extension actually talking, and a
        bare `Code.exe` keeps the editor's own name.
        Reasonix ships a native `reasonix.exe` under `@reasonix/cli-<platform>`
        (it also has a node entry point, `…/reasonix/bin/reasonix.js`, which the
        cmdline test covers); the exe name alone would read as "reasonix.exe".
    Fall back to `default` when nothing matches.
    """
    def norm(s):
        return (s or "").lower().replace("\\", "/")
    cmd = norm(cmdline)
    path = norm(exe_path)

    # pi is checked first on purpose: it is a node CLI whose argv carries the whole
    # user prompt in `-p` mode, so a prompt that happens to mention claude would
    # otherwise name the row after the wrong agent. The npm package path is the
    # unambiguous tell.
    if "pi-coding-agent" in cmd or "pi-coding-agent" in path:
        return "pi"
    # DSH = DeepSeek Harness, named here by the short form it ships under, because
    # this string is what the popup's row prints and a row is one line. Whether it
    # runs through npx (the argv is the npx shim's `@deepseek-ai/dsh web`) or from
    # the unpacked package (`…/@deepseek-ai/dsh/lib/bin.js`), the scoped package
    # name is in argv either way; exe_path is just node.exe, which is what the row
    # read as before this marker existed.
    if "@deepseek-ai/dsh" in cmd or "@deepseek-ai/dsh" in path:
        return "DSH"
    # Codex CLI, OpenAI's agent CLI. Two install shapes: the npm package
    # (`@openai/codex`) and the native build, which lands under
    # %LOCALAPPDATA%\OpenAI\Codex\bin and would otherwise read as "codex.exe".
    # Matched in this early group for the same reason as the two above:
    # `codex exec "<prompt>"` carries the whole prompt in argv, and a prompt that
    # mentions another agent must not rename the row.
    if ("@openai/codex" in cmd or "@openai/codex" in path
            or "/openai/codex/" in path or "/openai/codex/" in cmd):
        return "Codex CLI"
    if "claude" in cmd:
        return "Claude Code"
    if "vibe" in cmd or "mistral" in cmd:
        return "Mistral Vibe"
    if "reasonix" in cmd or "reasonix" in path:
        return "Reasonix"
    if "-cli/bin/duo.exe" in path:
        # GitLab Duo CLI ships a native `duo.exe` under the glab-cli install
        # dir (…/glab-cli/bin/duo.exe); its only tell is that path tail. The
        # `-cli` prefix is elastic (glab-cli / gitlab-cli) so match the tail.
        return "GitLab Duo CLI"
    if "@kilocode/cli" in path:
        return "Kilo CLI"
    if "microsoft vs code" in path:
        # Checked before the plain editor name: an MCP client hosted by VS Code is
        # this utility process, and every one of them is a `Code.exe` whose argv is
        # the only thing separating "the editor" from "the thing making tool calls".
        if "node.mojom.nodeservice" in cmd:
            return "GitHub Copilot Chat"
        return "Visual Studio Code"
    return default


# Who ships each client we can name. Display only: it rides the popup row's tooltip
# and its copied block, so an unfamiliar name says whose tool it is. Keyed on the
# friendly name _friendly_from_cmdline just returned, so the two tables cannot drift
# apart on spelling. Anything unnamed (or a browser, which carries its own vendor in
# browser_info) resolves to None and prints nothing.
CLIENT_VENDORS = {
    "Claude Code": "Anthropic",
    "Codex CLI": "OpenAI",
    "DSH": "DeepSeek",
    "GitHub Copilot Chat": "Microsoft",
    "GitLab Duo CLI": "GitLab",
    "Kilo CLI": "Kilo Code",
    "Mistral Vibe": "Mistral AI",
    "Reasonix": "Reasonix",
    "Visual Studio Code": "Microsoft",
    "pi": "Earendil Works",
}

def client_vendor(friendly):
    """Vendor for a friendly client name, or None when we do not know one."""
    return CLIENT_VENDORS.get(friendly)


# A flag whose VALUE must never be copied out of a command line. Matched on the flag,
# not on the value, because a secret is unrecognizable on its own.
SECRET_FLAG_RE = re.compile(r"key|token|secret|password|passwd|credential|auth", re.I)

# Secrets that are not the value of a dash flag. Each pattern keeps the label and masks
# only the value, so the copied line still says what was passed. Order matters: the
# Authorization header goes first because its value is "<scheme> <credential>", and the
# generic KEY=value rule would otherwise mask the scheme word and leave the credential.
_VALUE = r"""(?:"[^"]*"?|'[^']*'?|[^\s'"]+)"""
_AUTH_HEADER_RE = re.compile(
    r"(?i)((?:proxy-)?authorization\s*[:=]\s*)(?:(?:bearer|basic|token|digest)\s+)?" + _VALUE)
_BEARER_RE = re.compile(r"(?i)(?<![A-Za-z])bearer\s+[\w.~+/=-]{6,}")
# KEY=value / key: value where the key NAME looks secret (API_KEY, PGPASSWORD, x-api-key,
# /password:). Also reaches ?api_key=... inside a URL, which redact_url does not know.
#
# Linear on purpose. The pattern starts AT the keyword: the part of the name before it needs no
# match because the label is kept as it stands and only the value is replaced. The old
# `[\w.-]*` on both sides made one long identifier-like argument cost O(n^2) to O(n^3)
# (4000 characters took 51 s) while holding the GIL, which froze the whole server on
# 2026-10-02. The name after the keyword is bounded for the same reason, at 256: well past
# the 140 characters of one argument that can ever be shown (token_limit), so a longer name
# only ever hid a value that the cut would have dropped anyway. A cap of 64 let
# `key<65 name characters>=SECRET` through, which the unbounded pattern had masked.
_SECRET_PAIR_RE = re.compile(
    r"(?i)((?:token|secret|passw(?:or)?d|apikey|api[-_]key|auth|bearer|credential|key)"
    r"[\w.-]{0,256}\s*[:=]\s*)" + _VALUE)
# How much of one argv entry is looked at for secrets. Everything shown is cut to token_limit
# afterwards, so this only bounds the work an enormous argument can cause. The quote in
# _VALUE closes optionally for the same reason: a quoted secret cut off by this limit has no
# closing quote left, and must still be masked to its end.
_MASK_INPUT_LIMIT = 1024
# RDP-style /p:value and /pw:value, which the pair rule cannot see (the key is too short).
_SLASH_PASS_RE = re.compile(r"(?i)(?<!\S)(/(?:p|pw|pass):)\S+")

def _mask_secret_text(text):
    text = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + "***", text)
    text = _SECRET_PAIR_RE.sub(lambda m: m.group(1) + "***", text)
    text = _SLASH_PASS_RE.sub(lambda m: m.group(1) + "***", text)
    return _BEARER_RE.sub("Bearer ***", text)


def sanitized_cmdline(argv, limit=500, token_limit=140):
    """argv as one line for the popup's "Copy connection info", secrets masked.

    The command line is the ONLY thing that names a client running under a generic
    interpreter (`node …/@deepseek-ai/dsh/lib/bin.js` behind an exe_path of plain
    node.exe), so a copied row without it says nothing about what the connection is.
    The same line is also where an MCP client's `--api-key` ends up, and a copied row
    is something the user pastes into a chat or a bug report, so a value that follows
    a secret-looking flag is replaced rather than carried along, as is the value of a
    secret-looking KEY=value pair, an Authorization header and a /password: switch. Long arguments (a
    prompt passed in argv) are cut per token and the line is cut as a whole, so one
    runaway argument cannot fill the clipboard.
    """
    out, mask_next, total = [], False, 0
    for token in (argv or []):
        if total > limit:
            break   # the line is cut at `limit` below; later entries cannot show
        text = str(token)
        if mask_next:
            out.append("***")
            mask_next = False
            continue
        flag, sep, _value = text.partition("=")
        if text.startswith("-") and sep and SECRET_FLAG_RE.search(flag):
            out.append(f"{flag}=***")
            continue
        # `not sep`: "--header=Authorization: Bearer x" is a value, not a bare flag awaiting one.
        if text.startswith("-") and not sep and SECRET_FLAG_RE.search(text):
            out.append(text)
            mask_next = True   # the value is the NEXT argv entry
            continue
        capped = len(text) > _MASK_INPUT_LIMIT
        if capped:
            text = text[:_MASK_INPUT_LIMIT]
        text = redact_urls_in_text(text)   # userinfo, query string and fragment
        text = _mask_secret_text(text)
        if capped or len(text) > token_limit:
            text = text[:token_limit] + "…"
        out.append(text)
        total += len(text) + 1
    line = " ".join(out)
    return (line if len(line) <= limit else line[:limit] + "…") or None


def describe_pid(pid):
    """Best-effort {pid, friendly, exe_name, exe_path, cmdline} for a PID."""
    info = {"pid": pid, "friendly": None, "exe_name": None, "exe_path": None,
            "cwd": None, "start_time": None, "cmdline": None}
    if not pid:
        return info
    try:
        import psutil
        p = psutil.Process(pid)
        name = p.name()
        info["exe_name"] = name
        try:
            info["exe_path"] = p.exe()
        except Exception:
            info["exe_path"] = None
        try:
            info["cwd"] = p.cwd()
        except Exception:
            info["cwd"] = None
        try:
            info["start_time"] = datetime.fromtimestamp(p.create_time())
        except Exception:
            info["start_time"] = None
        low = (name or "").lower()
        try:
            argv = p.cmdline()
        except Exception:
            argv = []
        cmdline = " ".join(argv)
        info["cmdline"] = sanitized_cmdline(argv)
        family, engine = browser_identity.browser_brand(name, info["exe_path"])
        if family:
            info["friendly"] = browser_identity.browser_variant(family, engine, info["exe_path"])
        elif "claude" in low:
            info["friendly"] = "Claude Code"
        else:
            # Identify a known client from its argv AND full exe path; this
            # covers both generic interpreters (Mistral Vibe = a uv-installed
            # Python tool launched as `python.exe …\vibe.exe`, which would else
            # read as bare "Python") and native launchers (Kilo CLI ships a
            # `kilo.exe` whose only tell is `@kilocode\cli` in its path). Falls
            # back to the runtime name for a plain python/node, else the exe name.
            default = ("Node" if "node" in low
                       else "Python" if "python" in low else name)
            info["friendly"] = friendly_from_cmdline(
                cmdline, info["exe_path"], default)
    except Exception as e:
        logger.debug(f"describe pid {pid} failed: {e}")
    return info


# A session whose identity could not be resolved is looked up again on a later request, but
# not on every request: each attempt walks the TCP table.
IDENTITY_RETRY_SECONDS = 5.0


def note_http_client(http_clients, session_id, client, client_pid=None):
    """The cheap half of tracking, safe on an event loop: file or touch the session's entry.

    Returns the entry when its identity (process name, argv, launcher) still has to be
    looked up with `identify_http_client`, which walks processes and must run OFF the
    loop, else None. No psutil here. A new entry carries only what the request names
    (the forwarded client pid, the peer port) until the lookup fills the rest.
    """
    if not session_id:
        return None
    now = datetime.now()
    peer_port = client[1] if client else None
    fresh = {
        "pid": client_pid,
        "friendly": None,
        "exe_path": None,
        "cmdline": None,
        "cwd": None,
        "start_time": None,
        "launcher_pid": None,
        "launcher_start_time": None,
        "peer_port": peer_port,
        "first_seen": now,
        "last_activity": now,
        "history": [],
        "identified": False,
        "identity_tried": None,
    }
    # setdefault is atomic: two first requests of one session can race here (tests and
    # worker threads call this too), and a plain store let the second replace the first
    # entry along with its history.
    entry = http_clients.setdefault(session_id, fresh)
    if entry is not fresh:
        entry["last_activity"] = now
    if entry.get("identified") or not (client_pid or peer_port):
        return None
    tried = entry.get("identity_tried")
    if tried is not None and time.monotonic() - tried < IDENTITY_RETRY_SECONDS:
        return None
    entry["identity_tried"] = time.monotonic()
    return entry


def identify_http_client(entry, client, client_pid=None):
    """The slow half of tracking: resolve who is behind `entry` and fill it in.

    Blocking (a TCP-table sweep and a process walk), so callers run it on a worker
    thread. It marks the entry identified only when it found a pid, so a peer that could
    not be resolved is tried again by a later request (note_http_client throttles that).
    """
    peer_port = client[1] if client else None
    # Even with the pid forwarded in the header, the peer chain is what knows whether a
    # launcher sits in between (and which process it is).
    resolved_pid, launcher_pid = resolve_client_chain(peer_port)
    desc = describe_pid(client_pid or resolved_pid)
    launcher_start = (describe_pid(launcher_pid).get("start_time")
                      if launcher_pid else None)
    if not desc.get("pid"):
        return
    entry.update({
        "pid": desc["pid"],
        "friendly": desc.get("friendly"),
        "exe_path": desc.get("exe_path"),
        "cmdline": desc.get("cmdline"),
        "cwd": desc.get("cwd"),
        "start_time": desc.get("start_time"),
        "launcher_pid": launcher_pid,
        # The launcher's OS start time; its map node reports "started" like every other
        # party on the connection map.
        "launcher_start_time": launcher_start,
        "peer_port": peer_port,
        "identified": True,
    })


def track_http_client(http_clients, session_id, client, client_pid=None):
    """Record/update an MCP HTTP client, looking its identity up inline.

    client_pid: when set (forwarded by the stdio launcher via the
    X-BrowserPlug-Client-Pid header), it identifies the real Claude Code process
    behind the proxy. Without it we fall back to mapping the TCP peer port to
    a PID (correct for direct HTTP clients).

    The synchronous form, for callers that are not an event loop. The HTTP layer calls
    `note_http_client` on the loop and `identify_http_client` on a worker instead.
    """
    entry = note_http_client(http_clients, session_id, client, client_pid)
    if entry is not None:
        identify_http_client(entry, client, client_pid)


# How long a launcher's reported version outlives its last /health poll. The
# launcher polls every TOOLS_POLL_SECONDS (3s) for its whole life, so anything
# past this is a launcher that exited, and a dead launcher must not keep
# producing a version-skew warning.
LAUNCHER_VERSION_TTL_SECONDS = 60

def record_launcher_version(launcher_versions, launcher_pid, version):
    """Remember which build a launcher process is running (from its /health poll)."""
    if not launcher_pid or not version:
        return
    now = datetime.now()
    launcher_versions[launcher_pid] = {"version": str(version), "seen": now}
    cutoff = now.timestamp() - LAUNCHER_VERSION_TTL_SECONDS
    # list(): /health runs on worker threads, one per polling launcher.
    for pid in [p for p, e in list(launcher_versions.items())
                if e["seen"].timestamp() < cutoff]:
        launcher_versions.pop(pid, None)

def launcher_version(launcher_versions, launcher_pid):
    """The version a launcher last reported, or None (never reported / gone quiet)."""
    entry = launcher_versions.get(launcher_pid)
    if not entry:
        return None
    if entry["seen"].timestamp() < datetime.now().timestamp() - LAUNCHER_VERSION_TTL_SECONDS:
        return None
    return entry["version"]


def connected_browser_pids(ws_port):
    """PIDs of browser process(es) (Firefox, a fork, or a Chromium browser) with an
    ESTABLISHED connection to the WS port. (/health serves the result as `browser_pids`,
    and still as the legacy `firefox_pids` for older options pages.)"""
    pids = set()
    try:
        import psutil
        # ONE table for the whole report. The owner of each browser-side port is read from
        # this same snapshot; pid_for_local_port would scan the table again per socket.
        table = psutil.net_connections(kind='inet')
        owner = {}
        for c in table:
            if c.laddr and c.pid and c.status == psutil.CONN_ESTABLISHED:
                owner.setdefault(c.laddr.port, c.pid)
        for c in table:
            if (c.laddr and c.laddr.port == ws_port and c.raddr
                    and c.status == psutil.CONN_ESTABLISHED):
                pid = owner.get(c.raddr.port)
                if not pid:
                    continue
                try:
                    p = psutil.Process(pid)
                    try:
                        exe = p.exe()
                    except Exception:
                        exe = None
                    if browser_identity.browser_brand(p.name(), exe)[1] is not None:
                        pids.add(pid)
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"browser pid scan failed: {e}")
    return sorted(pids)


# ---- Browser identity probe (local browsers; display enrichment only) --------

def browser_instance_identity(socket_pid):
    """Resolve a WS-socket-owning PID to its browser INSTANCE identity (Firefox or
    any fork (browser_brand names it; a fork's own extension API generally won't).

    Walks up to the top-most ancestor of the SAME browser (the socket PID is a
    content/socket process; the top-most one is stable across extension reloads);
    that process's name/exe is what brands the browser in the roster.
    """
    ident = {"pid": socket_pid, "exe_path": None,
             "brand": None, "friendly": None, "engine": None, "start_time": None,
             "is_browser": False}
    if not socket_pid:
        return ident
    try:
        import psutil
        p = psutil.Process(socket_pid)
        top = p
        hops = 0
        while hops < 6:
            try:
                par = p.parent()
            except Exception:
                par = None
            # Climb only through the SAME executable: a browser's parent process is
            # its own launcher, never another app (Firefox's parent is explorer.exe).
            if par and browser_identity.process_stem(par.name()) == browser_identity.process_stem(top.name()):
                p = par
                top = par
                hops += 1
                continue
            break
        ident["pid"] = top.pid
        try:
            ident["exe_path"] = top.exe()
        except Exception:
            pass
        try:
            ident["start_time"] = datetime.fromtimestamp(top.create_time())
        except Exception:
            pass
        family, engine = browser_identity.browser_brand(top.name(), ident["exe_path"])
        ident["is_browser"] = engine is not None
        ident["engine"] = engine
        ident["brand"] = family
        ident["friendly"] = (browser_identity.browser_variant(family, engine, ident["exe_path"])
                             if family else None)
    except Exception as e:
        logger.debug(f"browser identity resolve failed for {socket_pid}: {e}")
    return ident
