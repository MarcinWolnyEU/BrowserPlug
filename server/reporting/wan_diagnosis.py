"""WAN reachability self-diagnosis, isolated from server.py on purpose.

Backs `GET /diagnose-wan`, called by the options page's `wan-diagnosis.js` whenever the
"Available online" check-host.net probe reports the server unreachable, and by the LAN
panel whenever the server is bound past localhost. This module owns the entire
investigation: is the port actually listening (OS-level, not just "we called bind() and it
didn't throw"), will Windows Firewall admit inbound connections on BOTH ports a remote
browser needs (the extension WebSocket and the HTTP port that serves the options page's
data), over the network this machine is actually on, and what's the default gateway (for
the port-forwarding instructions). server.py's only involvement is a single dispatch line in
`_wrap_client_tracking`; see there.

CGNAT is deliberately out of scope: the message this backs assumes a router port-forward is
the fix, which the user asked to keep simple.

Windows-only for the firewall/gateway half (this project runs on Windows); the listening
check is OS-agnostic (psutil). On any other platform, or if PowerShell is unavailable/slow/
broken, the Windows half degrades to "not checked" rather than raising. A diagnostic
endpoint must never itself become a new way for the options page to break.
"""

import asyncio
import json
import logging
import os
import platform
import sys
import time

logger = logging.getLogger(__name__)

WINDOWS_DIAG_TIMEOUT_SECONDS = 5

# /diagnose-wan starts a powershell.exe per uncached call. The options page asks on every
# panel open, and a caller with the token could loop, so a result is reused this long.
CACHE_SECONDS = 30.0
_CACHE = {}       # key -> (monotonic time, payload bytes)
_INFLIGHT = {}    # key -> asyncio.Task running the same diagnosis


def _now():
    return time.monotonic()

# One PowerShell round-trip for the whole Windows half: every inbound allow rule that would
# admit ANY of the ports we care about, which of them each rule covers, the profile it
# applies to, the category Windows has assigned to the networks this machine is ON, and the
# default gateway. `{ports}` and `{exe}` are substituted before running.
#
# BOTH ports are checked in the one pass: the extension WebSocket (8772) and the HTTP port
# (2830), which since 1.7.31 follows the same LAN bind and is just as able to be blocked
# while the other is open. Asking about one of them produced a confident "ok" for a machine
# whose options page still could not load, which is the failure this whole check exists to
# stop.
#
# THREE whole-table reads joined in memory by InstanceID, never a per-rule cmdlet call.
# Both obvious pipelines blow WINDOWS_DIAG_TIMEOUT_SECONDS: piping port filters into
# Get-NetFirewallRule measured 7.9s on an ordinary desktop, and the reverse direction is
# the same shape (the older comment here warned about it for application filters). Reading
# the tables whole and joining them measured 0.86s, 0.75s of which is the rule table.
#
# A rule matches when it names this TCP port, or when its application filter names THIS
# interpreter. A blanket rule (Program 'Any' AND LocalPort 'Any') is deliberately NOT
# counted: a stock Windows carries dozens (Wireless Display, Proximity sharing, 29 others
# on this box), each scoped to its own program in practice, and counting them would turn
# every machine into a confident "the firewall is fine".
_POWERSHELL_SCRIPT = """
$ErrorActionPreference = 'SilentlyContinue'
$ports = @({ports})
$exe = '{exe}'

$rules = Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True
$portMap = @{{}}
foreach ($f in Get-NetFirewallPortFilter) {{ $portMap[$f.InstanceID] = $f }}
$appMap = @{{}}
foreach ($f in Get-NetFirewallApplicationFilter) {{ $appMap[$f.InstanceID] = $f }}

$matched = foreach ($r in $rules) {{
    $pf = $portMap[$r.InstanceID]
    $af = $appMap[$r.InstanceID]
    $hit = @()
    if ($pf -and $pf.Protocol -eq 'TCP') {{
        foreach ($p in $ports) {{ if ($pf.LocalPort -contains $p) {{ $hit += $p }} }}
    }}
    $appOk = $af -and $af.Program -and $af.Program -ne 'Any' -and $af.Program -ieq $exe
    # An application rule admits the program on every port, so it covers both of ours.
    if ($appOk) {{ $hit = $ports }}
    if ($hit.Count -gt 0) {{
        [PSCustomObject]@{{
            name = [string]$r.DisplayName
            profiles = [string]$r.Profile
            ports = @($hit)
        }}
    }}
}}

$active = foreach ($p in Get-NetConnectionProfile) {{
    [PSCustomObject]@{{ name = [string]$p.Name; category = [string]$p.NetworkCategory }}
}}

$gw = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' |
    Sort-Object -Property RouteMetric | Select-Object -First 1).NextHop

[PSCustomObject]@{{
    rules = @($matched)
    active_networks = @($active)
    gateway = [string]$gw
}} | ConvertTo-Json -Compress -Depth 4
"""

# Get-NetConnectionProfile reports a NetworkCategory; a firewall rule carries a Profile.
# The same three buckets under two spellings, and only the domain one differs.
_CATEGORY_TO_PROFILE = {
    "public": "public",
    "private": "private",
    "domainauthenticated": "domain",
}


def _rule_profiles(rule):
    """['domain', 'private'] out of a rule's 'Domain, Private'. Lowercased for matching."""
    raw = (rule or {}).get("profiles") or ""
    return [p.strip().lower() for p in str(raw).split(",") if p.strip()]


def _rule_name(rule):
    return str((rule or {}).get("name") or "?")


def _covers(rule, category):
    """Would this rule apply to a network Windows filed under `category`?"""
    profiles = _rule_profiles(rule)
    if "any" in profiles:
        return True
    wanted = _CATEGORY_TO_PROFILE.get(str(category or "").strip().lower())
    return bool(wanted) and wanted in profiles


def _ps_quote(value):
    """A PowerShell single-quoted literal. A rule's display name is user-supplied and can
    contain an apostrophe, which doubles inside single quotes."""
    return "'" + str(value).replace("'", "''") + "'"


def _profile_argument(names):
    """Profile names in the order the firewall cmdlets print them, deduplicated.

    `names` is materialized first because callers pass generators, and a set comprehension
    inside the loop below would consume it on the first pass and compare every later
    profile against an empty set.
    """
    wanted = {str(x).strip().lower() for x in names}
    picked = [n for n in ("Domain", "Private", "Public") if n.lower() in wanted]
    return ",".join(picked) or "Any"


def _join(items, conjunction="and"):
    """'a', 'a and b', 'a, b and c'."""
    items = [str(i) for i in items]
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


def _rules_for_port(rules, port):
    """Rules whose port filter (or application filter) admits this port."""
    want = str(port)
    out = []
    for r in rules:
        covered = [str(x) for x in ((r or {}).get("ports") or [])]
        if want in covered:
            out.append(r)
    return out


def firewall_verdict(rules, active_networks, ports):
    """Will inbound connections on EVERY port this app needs survive the firewall, on the
    network this machine is currently on?

    `ports` is an ordered list of {"port": int, "label": str}. Both are checked in one pass
    and answered in ONE message, because they fail the same way and a reader fixing a
    firewall wants a single verdict, not a per-port scoreboard. Checking only the WebSocket
    was a live blind spot: it reported a confident `ok` for a machine whose options page
    still could not load from another host, since 2830 was blocked while 8772 was open.

    Pure, so the half worth testing needs no Windows box. The old check asked "is there a
    rule", and asked it only of rules carrying an application filter, so it answered a flat
    `false` for a machine whose port rule was perfectly good. Existence is also the wrong
    question: a rule scoped to the Private profile does nothing while Windows has the
    active Wi-Fi filed as Public, and that failure presents as complete silence — the SYN
    is dropped, so the server logs nothing and the remote browser only says Disconnected.

    Returns {'ok', 'reason', 'detail', 'fix_command'}. `reason` is ok / profile_mismatch /
    no_rule / mixed / unknown_network; `ok` is None only when the answer is unknowable.
    """
    rules = list(rules or [])
    active = list(active_networks or [])
    ports = [{"port": int(p["port"]), "label": str(p.get("label") or "")} for p in ports]
    categories = [str(n.get("category") or "") for n in active]
    needed = _profile_argument(_CATEGORY_TO_PROFILE.get(c.strip().lower(), "")
                               for c in categories)

    if not active:
        return {
            "ok": None,
            "reason": "unknown_network",
            "detail": "Windows reported no connected network, so which firewall profile is "
                      "in force could not be determined.",
            "fix_command": None,
        }

    net = active[0]
    net_name = str(net.get("name") or "this network")
    net_category = str(net.get("category") or "?")

    allowed, blocked, commands, allowing = [], [], [], []
    for spec in ports:
        port, label = spec["port"], spec["label"]
        candidates = _rules_for_port(rules, port)
        # A rule counts only if it covers EVERY active network; a second adapter sitting on
        # an uncovered profile is exactly the case that bites.
        covering = [r for r in candidates
                    if all(_covers(r, n.get("category")) for n in active)]
        if covering:
            allowed.append(spec)
            allowing.extend(_rule_name(r) for r in covering)
            continue

        if not candidates:
            blocked.append((spec, "no_rule", f"port {port} ({label}) has no rule"))
            commands.append(
                f"New-NetFirewallRule -DisplayName 'BrowserPlug {port} (LAN)' "
                f"-Direction Inbound -Protocol TCP -LocalPort {port} -Action Allow "
                f"-Profile {needed} -RemoteAddress LocalSubnet")
        else:
            held = {pr for r in candidates for pr in _rule_profiles(r)}
            profiles = ", ".join(sorted(pr.title() for pr in held)) or "?"
            names = _join(sorted({_rule_name(r) for r in candidates}))
            blocked.append((spec, "profile_mismatch",
                            f"port {port} ({label}) has a rule ({names}) that applies only "
                            f"to the {profiles} profile"))
            widened = _profile_argument(
                held | {_CATEGORY_TO_PROFILE.get(c.strip().lower(), "") for c in categories})
            for r in candidates:
                commands.append(f"Set-NetFirewallRule -DisplayName "
                                f"{_ps_quote(_rule_name(r))} -Profile {widened}")

    port_list = _join(p["port"] for p in ports)

    if not blocked:
        allowed_by = _join(sorted(set(allowing)))
        return {
            "ok": True,
            "reason": "ok",
            "detail": f"Ports {port_list} are both allowed on your active network "
                      f"'{net_name}' ({net_category}), by {allowed_by}."
            if len(ports) > 1 else
            f"Port {port_list} is allowed on your active network '{net_name}' "
            f"({net_category}), by {allowed_by}.",
            "fix_command": None,
        }

    reasons = {r for _spec, r, _text in blocked}
    reason = reasons.pop() if len(reasons) == 1 else "mixed"
    ok_note = (f"Port {_join(p['port'] for p in allowed)} is allowed, but "
               if allowed else "")
    detail = (f"{ok_note}{_join(text for _s, _r, text in blocked)}, "
              f"while Windows has your active network '{net_name}' filed as "
              f"{net_category}. Until that is fixed a browser on another machine cannot "
              f"reach the server.")
    # Dedupe while keeping the order the ports were given in; two blocked ports sharing one
    # rule must not print the same Set-NetFirewallRule twice.
    seen, ordered = set(), []
    for c in commands:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return {
        "ok": False,
        "reason": reason,
        "detail": detail,
        "fix_command": "\n".join(ordered) or None,
    }


def _wan_listening_detail(ws_port):
    """OS-level confirmation a LISTEN socket on ws_port belongs to THIS process: the same
    psutil.net_connections()-filtered-by-pid idiom server.py already uses elsewhere for
    connection diagnostics, not just trusting that our own bind() succeeded.

    Returns (ok, detail): ok is True/False/None (None = couldn't verify, e.g. psutil
    threw), detail is a human-readable string either way.
    """
    try:
        import psutil
        pid = os.getpid()
        for c in psutil.net_connections(kind='inet'):
            if (c.pid == pid and c.status == psutil.CONN_LISTEN
                    and c.laddr and c.laddr.port == ws_port):
                return True, f"{c.laddr.ip}:{c.laddr.port} LISTEN (pid {pid})"
        return False, f"no LISTEN socket found for port {ws_port} on this process"
    except Exception as e:
        logger.debug(f"WAN diagnosis: listening check failed: {e}")
        return None, f"could not verify: {e}"


def _unknown_windows():
    return {"firewall": None, "gateway_ip": None, "rules": [], "active_networks": []}


async def _windows_wan_diagnostics(ports):
    """The Windows half, best-effort, never raises. Non-Windows, a missing or broken
    powershell.exe, a timeout, and unparseable output all degrade to the unknown shape,
    where `firewall` is None and the caller reports that it could not be checked."""
    if platform.system() != "Windows":
        return _unknown_windows()

    script = _POWERSHELL_SCRIPT.format(
        ports=",".join(f"'{int(p['port'])}'" for p in ports),
        exe=sys.executable.replace("'", "''"),
    )
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=WINDOWS_DIAG_TIMEOUT_SECONDS)
        data = json.loads(stdout.decode("utf-8", "replace") or "{}")
        gateway_ip = data.get("gateway") or None
        if gateway_ip in ("0.0.0.0", ""):
            gateway_ip = None
        # ConvertTo-Json hands back a bare object, not a one-element array, when a list
        # has a single member; normalize rather than trusting the shape.
        rules = data.get("rules") or []
        active = data.get("active_networks") or []
        if isinstance(rules, dict):
            rules = [rules]
        if isinstance(active, dict):
            active = [active]
        return {
            "firewall": firewall_verdict(rules, active, ports),
            "gateway_ip": gateway_ip,
            "rules": rules,
            "active_networks": active,
        }
    except Exception as e:
        logger.debug(f"WAN diagnosis: windows firewall/gateway probe failed: {e}")
        # wait_for cancels communicate() but leaves the child running; a hung PowerShell
        # would otherwise outlive every request that started it.
        if proc is not None and getattr(proc, "returncode", None) is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return _unknown_windows()


async def diagnose_wan_reachability(ws_port, ws_host, mcp_port=None, lan_rows=()):
    """Assemble the full diagnosis served at /diagnose-wan. Never raises: every sub-check
    degrades to an 'unknown' shape rather than propagating into the ASGI handler.

    `lan_rows` is the adapter-named address list (net_addresses.ws_lan_addresses()); the
    caller passes it in so this module never has to import the server."""
    # Both ports the app needs from another machine, checked and answered together. The
    # labels are the caller's because only server.py knows which port does what.
    ports = [{"port": ws_port, "label": "the extension WebSocket"}]
    if mcp_port:
        ports.append({"port": mcp_port, "label": "the options page's HTTP port"})

    listening_ok, listening_detail = _wan_listening_detail(ws_port)
    windows = await _windows_wan_diagnostics(ports)
    lan_ip = lan_rows[0]["ip"] if lan_rows else None
    host_bind_ok = ws_host in ("0.0.0.0", "::")

    verdict = windows["firewall"]
    firewall = {
        # `checked` and `ok` keep their old spelling and their old meaning: an options page
        # from an older extension reads exactly those two and has to keep working.
        "checked": verdict is not None,
        "ok": verdict["ok"] if verdict else None,
        "reason": verdict["reason"] if verdict else "unknown",
        "detail": (verdict["detail"] if verdict
                   else "The Windows Firewall state could not be checked."),
        "fix_command": verdict.get("fix_command") if verdict else None,
        "rules": windows["rules"],
        "active_networks": windows["active_networks"],
    }

    return {
        "listening": {"ok": listening_ok, "detail": listening_detail},
        "host_bind": {"ok": host_bind_ok, "host": ws_host},
        "firewall": firewall,
        "gateway_ip": windows["gateway_ip"],
        "lan_ip": lan_ip,
        "port": ws_port,
        "ports": [p["port"] for p in ports],
    }


async def handle_diagnose_wan_request(scope, send, server):
    """The full ASGI leaf handler for GET /diagnose-wan. server.py just dispatches here.

    The peer check that used to live here moved to http_auth.authorize, which runs for
    every request on this port before any endpoint does: a local caller is free, a remote
    one needs the token. The payload (the LAN IP, the router's gateway, the firewall's
    rules) is worth exactly that much protection and no less.
    """
    lan_rows = server.ws_lan_addresses()
    key = (server.port, server.ws_host, getattr(server, "mcp_port", None),
           json.dumps(lan_rows, sort_keys=True, default=str))
    hit = _CACHE.get(key)
    if hit is not None and _now() - hit[0] < CACHE_SECONDS:
        payload = hit[1]
    else:
        # Concurrent misses share one run instead of one PowerShell each.
        task = _INFLIGHT.get(key)
        if task is None:
            task = _INFLIGHT[key] = asyncio.ensure_future(diagnose_wan_reachability(
                server.port, server.ws_host, getattr(server, "mcp_port", None),
                lan_rows=lan_rows))
            task.add_done_callback(lambda _t, k=key: _INFLIGHT.pop(k, None))
        payload = json.dumps(await asyncio.shield(task)).encode("utf-8")
        _CACHE.clear()
        _CACHE[key] = (_now(), payload)
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": payload})
