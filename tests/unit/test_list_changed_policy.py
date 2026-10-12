"""server/tools/list_changed_policy.py: when tools/list_changed is owed, and to whom.

Pure: no SDK, no event loop, no sleep. The clock is the `now` each call is given.
"""

import threading

import test_imports  # noqa: F401  (path setup)
from server.tools.list_changed_policy import (MAX_SESSIONS, MAX_WAIT_SECONDS, QUIET_SECONDS,
                                              ChangeGate, SessionBook)


# =============================================================================
# ChangeGate
# =============================================================================

def test_the_shipped_debounce_is_a_quiet_period_capped_at_the_relay_poll():
    """0.3 s after the last change, never later than 3 s after the first: the stdio relay
    polls /health every 3 s, so an HTTP client never hears later than a relay client."""
    assert (QUIET_SECONDS, MAX_WAIT_SECONDS) == (0.3, 3.0)
    assert ChangeGate().quiet == QUIET_SECONDS and ChangeGate().max_wait == MAX_WAIT_SECONDS


def test_one_change_is_due_after_the_quiet_period():
    gate = ChangeGate(quiet=0.3, max_wait=3.0)
    gate.reset("S1")
    assert gate.change("S2", now=10.0) == 0.3
    assert gate.pending
    assert gate.settle() is True
    assert not gate.pending


def test_a_burst_moves_the_deadline_but_never_past_the_cap():
    gate = ChangeGate(quiet=0.3, max_wait=1.0)
    gate.reset("S0")
    assert gate.change("S1", now=0.0) == 0.3
    assert gate.change("S2", now=0.2) == 0.3, "a later change restarts the quiet period"
    assert round(gate.change("S3", now=0.8), 6) == 0.2, "capped at first + max_wait = 1.0"
    assert gate.change("S4", now=1.5) == 0.0, "past the cap: due at once"
    assert gate.settle() is True
    assert gate.settle() is False, "settled: nothing owed until the next change"


def test_a_signature_that_did_not_move_arms_nothing():
    """A recompute that reshaped nothing must not push."""
    gate = ChangeGate()
    gate.reset("S1")
    assert gate.change("S1", now=0.0) is None
    assert not gate.pending and gate.settle() is False


def test_a_repeat_of_the_current_signature_leaves_an_armed_deadline_alone():
    gate = ChangeGate(quiet=0.3, max_wait=3.0)
    gate.reset("S1")
    gate.change("S2", now=0.0)
    assert gate.change("S2", now=0.1) is None
    assert gate.pending and gate.settle() is True


def test_a_list_taken_inside_the_window_still_gets_its_push():
    """A browser connects and leaves within the quiet period while a client lists in
    between (a new client's first tools/list, a relay rebuilding its session). The client
    holds the intermediate surface, so the change back must still be announced, or its
    next call to the vanished `browsers` tool gets 'Unknown tool'."""
    gate = ChangeGate(quiet=0.3, max_wait=3.0)
    gate.reset("S1")
    gate.change("S2", now=0.0)          # the second browser connects
    client_holds = "S2"                  # a client lists inside the window
    gate.change("S1", now=0.1)          # and the browser leaves again
    assert gate.settle() is True, (
        f"the client holds {client_holds} while the server has S1: it needs the push")


def test_reset_rebaselines_and_drops_what_was_pending():
    gate = ChangeGate()
    gate.reset("S1")
    gate.change("S2", now=0.0)
    gate.reset("S2")
    assert not gate.pending
    assert gate.change("S2", now=1.0) is None
    assert gate.change("S3", now=1.0) == gate.quiet


# =============================================================================
# SessionBook
# =============================================================================

LOOP_A, LOOP_B = object(), object()


def test_note_forget_and_clear():
    book = SessionBook()
    book.note("a", "session-a", LOOP_A)
    book.note("b", "session-b", LOOP_A)
    assert len(book) == 2 and book.keys() == ["a", "b"]
    assert book.forget("a") is True
    assert book.forget("a") is False, "forgetting twice is harmless"
    assert book.keys() == ["b"]
    book.clear()
    assert len(book) == 0


def test_a_known_key_keeps_its_newest_session_and_becomes_most_recent():
    book = SessionBook()
    book.note("a", "first", LOOP_A)
    book.note("b", "other", LOOP_A)
    book.note("a", "second", LOOP_A)
    assert book.keys() == ["b", "a"]
    assert book.on_loop(LOOP_A) == [("b", "other"), ("a", "second")]


def test_past_the_cap_the_least_recently_active_session_goes():
    book = SessionBook(cap=3)
    for key in "abc":
        book.note(key, key, LOOP_A)
    book.note("a", "a", LOOP_A)          # a is active again: b is now the oldest
    book.note("d", "d", LOOP_A)
    assert book.keys() == ["c", "a", "d"]
    assert SessionBook().cap == MAX_SESSIONS == 512


def test_a_broadcast_sees_only_the_sessions_of_its_own_loop():
    """The SDK's streams belong to the loop serving them; pushing from another thread's
    loop would touch them from the wrong thread."""
    book = SessionBook()
    book.note("http", "on-uvicorn", LOOP_A)
    book.note("inproc", "on-test-loop", LOOP_B)
    assert book.on_loop(LOOP_A) == [("http", "on-uvicorn")]
    assert book.on_loop(LOOP_B) == [("inproc", "on-test-loop")]
    assert book.forget_loop(LOOP_A) == 1
    assert book.keys() == ["inproc"]


def test_concurrent_notes_forgets_and_reads_raise_nothing():
    book = SessionBook(cap=64)
    errors = []

    def hammer(prefix):
        try:
            for i in range(2000):
                key = f"{prefix}{i % 100}"
                book.note(key, i, LOOP_A if i % 2 else LOOP_B)
                if i % 3 == 0:
                    book.forget(key)
                book.on_loop(LOOP_A)
                if i % 50 == 0:
                    book.forget_loop(LOOP_B)
                len(book)
        except Exception as e:      # any exception is the failure
            errors.append(repr(e))

    threads = [threading.Thread(target=hammer, args=(p,)) for p in "wxyz"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(book) <= 64
