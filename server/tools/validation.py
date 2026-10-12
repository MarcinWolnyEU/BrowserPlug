"""Argument checks at the tool boundary: numbers with ranges, enums with spellings.

Pure, no I/O. A tool calls these BEFORE it sends any wire action, so a bad value costs
nothing and comes back as one clear line naming the accepted range or values. Reject what
has no sensible reading (zero or negative durations, an empty step list, an unknown enum
value); clamp what merely exceeds a cap the extension enforces anyway, so the server's
deadline and the extension's stay the same number.

The signatures and docstrings of the tools are pinned by tests/unit/golden/tools_list.json,
which is why these checks live in code and not in Literal/Field types.
"""

from typing import Any, Iterable, Optional, Tuple

# The extension caps every settle and wait budget at this (background.js:
# Math.min(x || default, 120000)). Clamping here keeps the server deadline derived from
# the same number the extension will actually use.
MAX_TIMEOUT_MS = 120_000
# Ceiling for one page_read window. The extension floors a positive value at 50 and has no
# ceiling of its own; the response carries the continue-with-offset handle either way.
MAX_READ_CHARS = 500_000
# Entries the extension's rolling per-tab request log holds (REQ_LOG_MAX).
MAX_REQUEST_LIMIT = 300


def enum_value(value: Any, name: str, valid: Iterable[str]) -> Tuple[Optional[str], Optional[str]]:
    """(canonical value, error). Case and surrounding whitespace are forgiven."""
    options = tuple(valid)
    if isinstance(value, str) and value.strip().lower() in options:
        return value.strip().lower(), None
    return None, f'Error: unknown {name}="{value}" (use {" | ".join(options)})'


def positive_ms(value: Any, name: str) -> Tuple[Optional[int], Optional[str]]:
    """A duration that must be >= 1 ms; anything above MAX_TIMEOUT_MS is clamped to it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 1:
        return None, (f"Error: {name} must be a positive number of milliseconds "
                      f"(1..{MAX_TIMEOUT_MS}), got {value!r}")
    return min(int(value), MAX_TIMEOUT_MS), None


def non_negative(value: Any, name: str) -> Optional[str]:
    """Error text unless `value` is an integer >= 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
        return f"Error: {name} must be 0 or greater, got {value!r}"
    return None


def read_window(max_chars: Any, offset: Any) -> Tuple[Optional[int], Optional[str]]:
    """(clamped max_chars, error) for a page_read / fused-read window.

    max_chars <= 0 is refused here because the extension answers it with a length probe
    (no content), which the server would then misreport as a failed extraction.
    """
    if isinstance(max_chars, bool) or not isinstance(max_chars, (int, float)) or max_chars != max_chars or max_chars < 1:
        return None, (f"Error: max_chars must be a positive number of characters "
                      f"(1..{MAX_READ_CHARS}), got {max_chars!r}")
    err = non_negative(offset, "offset")
    if err:
        return None, err
    return min(int(max_chars), MAX_READ_CHARS), None


def request_limit(limit: Any) -> Tuple[Optional[int], Optional[str]]:
    """(clamped limit, error) for requests(action="recent")."""
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit != limit or limit < 1:
        return None, (f"Error: limit must be a positive number of entries "
                      f"(1..{MAX_REQUEST_LIMIT}), got {limit!r}")
    return min(int(limit), MAX_REQUEST_LIMIT), None


def act_steps_error(steps: Any) -> Optional[str]:
    """Error text for a page_act step list that cannot run, else None.

    Checks the numeric fields the server derives its deadline from; a negative wait would
    otherwise give the server a negative timeout after the earlier clicks were already sent.
    """
    if not steps:
        return "Error: steps is empty; pass at least one step (e.g. [{\"action\":\"click\",\"target\":\"Save\"}])"
    for i, s in enumerate(steps):
        get = s.get if isinstance(s, dict) else (lambda k, _s=s: getattr(_s, k, None))
        t = get("timeout_ms")
        if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or t != t or t < 1):
            return (f"Error: step {i} timeout_ms must be a positive number of milliseconds "
                    f"(1..{MAX_TIMEOUT_MS}), got {t!r}")
        st = get("settle_ms")
        if st is not None:
            err = non_negative(st, f"step {i} settle_ms")
            if err:
                return err
    return None
