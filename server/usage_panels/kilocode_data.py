"""Best-effort Kilo CLI provider/model detection from its local logs.

Kilo (`@kilocode/cli`) writes per-run logs under ``~/.local/share/kilo/log/`` and
keeps the current one(s) open while running, emitting one
``service=llm providerID=<p> modelID=<m>`` line per LLM turn. We map a Kilo PID
to its open ``.log`` file(s) through its open-file list, a deterministic link (the process
holds the handle), so no fragile start-time/timezone matching; then we read the tail and
take the most recent such line. The list comes from ``handle_scan`` (a bounded child
process, answered from a cache), never from a psutil call in the server: nothing on a
/health path may do unbounded kernel-wide work.

This parses Kilo's PRIVATE, UNDOCUMENTED logs (format as of Kilo v7.4.1). It is
best-effort by design: any failure returns ``None`` so callers degrade to
"unknown". Expect it to need updating if Kilo changes its log format or paths.
"""
import os
import re

from server.usage_panels import handle_scan

# e.g. `service=llm providerID=openrouter modelID=deepseek/deepseek-v4-flash …`
_LLM_RE = re.compile(rb"service=llm\s+providerID=(\S+)\s+modelID=(\S+)")
_TAIL_BYTES = 256 * 1024  # logs grow large; only the tail holds the recent turns


def parse_last_llm(data):
    """(provider, model) from the last llm line in a bytes blob, or None."""
    matches = _LLM_RE.findall(data)
    if not matches:
        return None
    provider, model = matches[-1]  # append-only log → last match is most recent
    return provider.decode("utf-8", "replace"), model.decode("utf-8", "replace")


def _last_llm_in_file(path):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            data = fh.read()
    except OSError:
        return None
    return parse_last_llm(data)


# Detection is not free: each candidate log costs a 256KB tail read (the open-file sweep is
# handle_scan's, cached for minutes and off this path). It runs from get_connections_info(),
# which is reached from the extension's poll (~2s), GET /health (~3s per launcher) and
# server_info. All of them honour the "Load Kilo session details" switch through
# usage_panels; /health and server_info read the stored value, the poll also passes its
# own. With a few launchers up that is a continuous read.
#
# A short TTL fixes the cost without touching any payload: a Kilo turn's provider/model
# changes on the order of minutes, so serving a few-second-old answer is indistinguishable
# from a fresh one, and every caller now shares one scan instead of racing their own.
_CACHE_TTL_SECONDS = 10
_cache = {}  # pid -> (expires_at_monotonic, result)


def _cache_get(pid, now):
    hit = _cache.get(pid)
    if hit is not None and hit[0] > now:
        return hit  # (expiry, result): result may legitimately be None
    return None


def detect_provider_model(pid, _ttl=_CACHE_TTL_SECONDS):
    """``{'provider','model'}`` for a running Kilo PID, or ``None``.

    Uses the process's open files (``handle_scan``) to find its own Kilo log(s), then,
    across them, takes the llm line from the most recently modified file. Results (misses
    included) are cached for ``_ttl`` seconds.
    """
    if not pid:
        return None
    import time as _time
    now = _time.monotonic()
    hit = _cache_get(pid, now)
    if hit is not None:
        return hit[1]
    try:
        result = _detect_provider_model_uncached(pid)
    except handle_scan.Pending:
        return None  # the open files are not known yet; not cached, the next poll asks again
    # Bound the map: a long-lived server sees PIDs come and go, and an entry per dead
    # PID would accumulate forever. Expired entries have no value to anyone.
    if len(_cache) > 64:
        for dead in [k for k, v in _cache.items() if v[0] <= now]:
            del _cache[dead]
    _cache[pid] = (now + _ttl, result)
    return result


def _detect_provider_model_uncached(pid):
    scan = handle_scan.lookup(pid)
    if scan is None:
        raise handle_scan.Pending
    logs = [p for p in scan.paths if p.lower().endswith(".log") and "kilo" in p.lower()]

    best = None  # (mtime, provider, model)
    for path in logs:
        pm = _last_llm_in_file(path)
        if not pm:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        if best is None or mtime > best[0]:
            best = (mtime, pm[0], pm[1])
    if best is None:
        return None
    return {"provider": best[1], "model": best[2]}
