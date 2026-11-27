"""Remote debug logging stays compiled off in both extension trees.

PRIVACY.md promises that the extension never sends its own debug log to the server. The
promise rests on one constant in each `background.js`. Nothing else enforces it, so these
tests fail when the constant is missing, changed, or no longer guards what sends.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TREES = ("extension-gecko", "extension-chromium")
FLAG = "ENABLE_DEBUG_LOGGING_TO_SERVER"
HOOK = f"if ({FLAG}) {{"


def _source(tree):
    return (ROOT / tree / "background.js").read_text(encoding="utf-8")


def _hook_span(src):
    """Offsets of the guarded console hook: from its `if` to the closing brace at column 0."""
    start = src.index(f"\n{HOOK}\n") + 1
    return start, src.index("\n}\n", start)


@pytest.mark.parametrize("tree", TREES)
def test_the_flag_is_one_const_set_to_false(tree):
    src = _source(tree)
    declarations = re.findall(rf"^(?:const|let|var)\s+{FLAG}\b.*$", src, re.M)
    assert declarations == [f"const {FLAG} = false;"], declarations


@pytest.mark.parametrize("tree", TREES)
def test_the_flag_has_exactly_two_uses(tree):
    """The declaration and the `if` around the console hook. Any other mention (an
    assignment, a second branch) needs a look."""
    uses = [ln.strip() for ln in _source(tree).splitlines() if FLAG in ln]
    assert uses == [f"const {FLAG} = false;", HOOK], uses


@pytest.mark.parametrize("tree", TREES)
def test_a_debug_log_envelope_is_built_only_inside_the_guarded_hook(tree):
    src = _source(tree)
    start, end = _hook_span(src)
    found = [m.start() for m in re.finditer(r"""['"]debug_log['"]""", src)]
    assert found, "the hook no longer builds a debug_log envelope; update this test"
    for offset in found:
        assert start <= offset <= end, f"'debug_log' built outside the guarded hook ({offset})"
