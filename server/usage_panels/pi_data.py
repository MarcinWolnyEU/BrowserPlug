"""Best-effort pi provider/model detection from its local session transcripts.

pi (``@earendil-works/pi-coding-agent``) appends a JSONL transcript per session under
``~/.pi/agent/sessions/<mangled cwd>/<UTC timestamp>_<uuid>.jsonl``, whose first line
records the session's ``cwd`` and whose ``model_change`` lines carry
``provider`` + ``modelId`` (one at startup, one more every time the model is switched
mid-session). That is the same thing Kilo's log gives us, so the popup renders it with
the same badge; only the plumbing differs.

The Kilo trick — map PID to file via ``psutil.Process.open_files()`` — does NOT work
here: pi appends and closes, so a live pi holds no handle on its own transcript
(live-verified). What links a PID to a file instead is the pair the transcript path is
built from: the process's **cwd** picks the directory, and the file name's UTC timestamp
sits within a second of the process's **create_time**. A resumed session (``--continue``
/ ``--resume``) keeps its ORIGINAL, older file name, so that case falls back to "the
transcript in this directory that has been written to since the process started".

This parses pi's PRIVATE, UNDOCUMENTED session format (as of pi 0.83). It is
best-effort by design: any failure returns ``None`` so callers degrade to "unknown".
"""
import os
import re

_SESSIONS_ROOT = os.path.join(os.path.expanduser("~"), ".pi", "agent", "sessions")
# `{"type":"model_change", … "provider":"openrouter","modelId":"deepseek/deepseek-v4-flash-0731"}`
_MODEL_RE = re.compile(rb'"type":"model_change".*?"provider":"([^"]+)".*?"modelId":"([^"]+)"')
# `2026-08-05T07-22-36-373Z_019fd0cd-….jsonl` — date and clock, dashes throughout.
_NAME_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})-(\d{3})Z_")
_TAIL_BYTES = 256 * 1024   # transcripts reach megabytes; the recent switches are at the end
_HEAD_BYTES = 64 * 1024    # …but a session that never switched has its only one at the start
# How long after the process started its transcript may appear and still be its own. pi
# writes the file within a second; the slack is for a slow start. The window is one-sided
# on purpose, and that is the load-bearing half: a transcript that existed BEFORE the
# process started cannot belong to it, and a symmetric window claimed exactly that.
# (Live, 2026-08-05: a pi in ardent-suite was polled a second after launch, before it had
# written its own transcript, and reported the model of the run that had died there a
# minute earlier — a wrong answer, not a missing one.) Clock granularity gets a couple of
# seconds the other way, far below the gap between two launches.
_START_TOLERANCE_SECONDS = 60
_START_BACKDATE_SLACK_SECONDS = 3


def parse_last_model_change(data):
    """(provider, model) from the last model_change in a bytes blob, or None."""
    matches = _MODEL_RE.findall(data)
    if not matches:
        return None
    provider, model = matches[-1]  # append-only transcript → last match is current
    return provider.decode("utf-8", "replace"), model.decode("utf-8", "replace")


def session_dir_name(cwd):
    """pi's directory name for a session cwd: every non-alphanumeric run becomes a dash.

    ``C:\\dr\\myproject`` → ``--C--dr-myproject--``. The leading/trailing dashes are pi's own
    wrapping; `dir_matches_cwd` compares with them stripped so a change of wrapping
    can't silently stop the lookup.
    """
    return "--" + re.sub(r"[^A-Za-z0-9]", "-", cwd or "") + "--"


def dir_matches_cwd(dir_name, cwd):
    """Whether a sessions subdirectory belongs to `cwd` (Windows: case-insensitively)."""
    if not dir_name or not cwd:
        return False
    want = session_dir_name(cwd).strip("-")
    got = str(dir_name).strip("-")
    if os.name == "nt":
        want, got = want.lower(), got.lower()
    return want == got


def _name_timestamp(filename):
    """Epoch seconds encoded in a transcript's file name (UTC), or None."""
    m = _NAME_TS_RE.match(filename)
    if not m:
        return None
    import calendar
    y, mo, d, h, mi, s, ms = (int(g) for g in m.groups())
    try:
        return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) + ms / 1000.0
    except (ValueError, OverflowError):
        return None


def pick_session_file(entries, create_time):
    """Choose one process's transcript from `entries` = [(name, mtime), …].

    Primary key is the file-name timestamp against the process's create_time, which
    survives several pi instances sharing one directory. A resumed session's name is
    OLD, so the fallback is the most recently written transcript that has been touched
    since the process started; that one can be fooled by a SECOND pi resuming in the
    same directory, which is as far as this goes without pi telling us its session id.
    Returns a name, or None when nothing can be claimed.
    """
    best = None       # (abs_delta, name)
    fallback = None   # (mtime, name)
    for name, mtime in entries:
        started = _name_timestamp(name)
        if started is not None and create_time:
            delta = started - create_time
            if (-_START_BACKDATE_SLACK_SECONDS <= delta <= _START_TOLERANCE_SECONDS
                    and (best is None or abs(delta) < abs(best[0]))):
                best = (delta, name)
        # Written since the process started (small slack: a transcript is created a
        # moment after create_time, and file mtime granularity is coarse on some FS).
        if create_time and mtime >= create_time - 5 and (fallback is None or mtime > fallback[0]):
            fallback = (mtime, name)
    if best is not None:
        return best[1]
    return fallback[1] if fallback is not None else None


def _model_change_in_file(path):
    """Last model_change in a transcript: tail first, then head for a session that
    never switched models (its only one is line 2, which a long transcript's tail
    has long since scrolled past)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            pm = parse_last_model_change(fh.read())
            if pm:
                return pm
            if size <= _TAIL_BYTES:
                return None     # the tail WAS the whole file; nothing more to read
            fh.seek(0)
            return parse_last_model_change(fh.read(_HEAD_BYTES))
    except OSError:
        return None


# Same cost profile as the Kilo detector (see kilocode_data.py): this runs from
# get_connections_info(), reached from both the extension's ~2s poll and every
# launcher's /health, so a short TTL keeps one directory scan + tail read shared
# between all of them. A model switch takes effect on the next turn, so serving a
# few-second-old answer is indistinguishable from a fresh one.
_CACHE_TTL_SECONDS = 10
_cache = {}  # pid -> (expires_at_monotonic, result)


def detect_provider_model(pid, _ttl=_CACHE_TTL_SECONDS, sessions_root=None):
    """``{'provider','model'}`` for a running pi PID, or ``None``.

    Results (misses included; a miss costs the same scan) are cached for ``_ttl`` seconds.
    """
    if not pid:
        return None
    import time as _time
    now = _time.monotonic()
    hit = _cache.get(pid)
    if hit is not None and hit[0] > now:
        return hit[1]
    result = _detect_provider_model_uncached(pid, sessions_root or _SESSIONS_ROOT)
    # Bound the map: a long-lived server sees PIDs come and go, and an entry per dead
    # PID would accumulate forever. Expired entries have no value to anyone.
    if len(_cache) > 64:
        for dead in [k for k, v in _cache.items() if v[0] <= now]:
            del _cache[dead]
    _cache[pid] = (now + _ttl, result)
    return result


def _detect_provider_model_uncached(pid, sessions_root):
    try:
        import psutil
        proc = psutil.Process(pid)
        cwd = proc.cwd()
        create_time = proc.create_time()
        argv = " ".join(proc.cmdline()).lower()
    except Exception:
        return None
    if "--no-session" in argv:
        return None     # ephemeral run: there is no transcript to read

    try:
        dirs = os.listdir(sessions_root)
    except OSError:
        return None
    match = next((d for d in dirs if dir_matches_cwd(d, cwd)), None)
    if match is None:
        return None
    session_dir = os.path.join(sessions_root, match)

    entries = []
    try:
        for name in os.listdir(session_dir):
            if not name.endswith(".jsonl"):
                continue
            try:
                entries.append((name, os.path.getmtime(os.path.join(session_dir, name))))
            except OSError:
                continue
    except OSError:
        return None
    chosen = pick_session_file(entries, create_time)
    if chosen is None:
        return None
    pm = _model_change_in_file(os.path.join(session_dir, chosen))
    if not pm:
        return None
    return {"provider": pm[0], "model": pm[1]}
