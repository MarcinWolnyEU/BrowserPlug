"""`page_act`: the step model, the request plan and the report.

The tool function is in `page.py`. What sits here has no I/O: `plan_act` turns the caller's
steps into the wire payload and the server-side deadline, and `format_act_result` renders the
extension's per-step results. Both are testable without a browser.
"""

from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field, model_validator

from server.tools.response_hints import check_instead_of_click_hint, watch_hint

from server.tools.formatting import compact, digest_suffix, fmt_ms, page_state_warning
from server.tools.validation import MAX_TIMEOUT_MS


# One step of a page_act sequence. No class docstring on purpose: pydantic would send it as
# the items description on every tools/list, and the tool description already says it.
class ActStep(BaseModel):
    action: str = Field(description="click, fill, select, check, press, hover, scroll or wait")
    target: Optional[str] = Field(default=None, description="snapshot ref such as e5, CSS selector or accessible-name text, tried in that order. wait: the CSS selector to wait for")
    value: Optional[Union[str, bool, int]] = Field(default=None, description="fill, select: the value; a checkbox takes true or false. check: true or false. press: a key such as Enter or Escape. scroll: top, bottom or a pixel offset")
    role: Optional[str] = Field(default=None, description="ARIA role filter for a text target, such as button or link")
    nth: Optional[int] = Field(default=None, description="0-based pick when several elements match")
    within: Optional[str] = Field(default=None, description="only match inside this ref or CSS selector")
    enabled_only: Optional[bool] = Field(default=None, description="only match enabled elements")
    submit: Optional[bool] = Field(default=None, description="fill: press Enter after setting the value")
    text_contains: Optional[str] = Field(default=None, description="wait: until page text contains this")
    url_contains: Optional[str] = Field(default=None, description="wait: until the URL contains this")
    predicate: Optional[str] = Field(default=None, description="wait: JS boolean expression, isolated world")
    gone: Optional[bool] = Field(default=None, description="wait: until target, text or url is ABSENT")
    require_visible: Optional[bool] = Field(default=None, description="wait: the selector must be visible, not just present")
    timeout_ms: Optional[int] = Field(default=None, description="wait: max ms, default 10000")
    settle_ms: Optional[int] = Field(default=None, description="ms to settle after the step, default 600 for click, else 0")
    watch: Optional[List[str]] = Field(default=None, description="targets whose visible, enabled and value state to report before and after this step")
    count: Optional[List[str]] = Field(default=None, description="CSS selectors to count before and after this step, to see whether a click loaded more")
    continue_on_error: Optional[bool] = Field(default=None, description="run later steps even if this one fails")

    @model_validator(mode="before")
    @classmethod
    def _accept_text_alias(cls, data):
        """Forgiving input: accept `text` as an alias for `value` (fill/select), so an
        agent that passes text="hi" doesn't silently no-op. Non-breaking; `value`
        stays the advertised field; `text` is only consumed when `value` is absent."""
        if isinstance(data, dict) and data.get("value") is None and data.get("text") is not None:
            data = dict(data)
            data["value"] = data.pop("text")
        return data


def plan_act(steps: List[ActStep], tab_id: Optional[int], frame_id: Optional[int]
             ) -> Tuple[List[Dict[str, Any]], float, Dict[str, Any]]:
    """(step dicts, seconds the server will wait, wire data) for a `content.act` request."""
    step_dicts = []
    wait_budget = 0
    for s in steps:
        if not isinstance(s, ActStep):
            # Direct callers (test harness, scripts) can hand raw dicts,
            # skipping the registry's argument validation; normalize through
            # ActStep so aliases (text -> value) and field pruning apply
            # on EVERY path. Without this a stray `text` reaches the
            # extension, where `text` is a TARGETING field, and hijacks
            # element resolution away from `target`.
            try:
                s = ActStep(**dict(s))
            except Exception:
                pass  # unparseable; forward raw; the extension reports precisely
        d = s.model_dump(exclude_none=True) if isinstance(s, ActStep) else dict(s)
        # The extension caps a step's timeout at MAX_TIMEOUT_MS. Send the capped value so
        # the deadline below is computed from the number the extension will use.
        if isinstance(d.get("timeout_ms"), (int, float)):
            d["timeout_ms"] = min(d["timeout_ms"], MAX_TIMEOUT_MS)
        step_dicts.append(d)
        if d.get("action") == "wait":
            wait_budget += (d.get("timeout_ms") or 10000)
        else:
            # The click reserve rose from 2100 when the extension started measuring
            # the page BEFORE and AFTER the step and waiting (bounded) for it to
            # quiesce, which can add up to ~1.5s on top of the settle. Reserve it
            # here or the server gives up first and the whole structured report is
            # lost; precisely the timeout this budget exists to prevent.
            step_ms = d.get("settle_ms") or (3600 if d.get("action") == "click" else 500)
            if d.get("count") or d.get("watch"):
                step_ms += 1500      # the caller asked to be measured; pay for it
            # A non-wait step may name its OWN deadline. The server has
            # to be prepared to wait that long, or it gives up before the
            # extension's structured timeout report can even be sent.
            wait_budget += max(step_ms, d.get("timeout_ms") or 0)
    act_timeout = 20.0 + wait_budget / 1000
    data: Dict[str, Any] = {"steps": step_dicts}
    # Hand the extension the budget WE will wait for, minus a margin,
    # so a step that hangs (a click whose navigation tore down the injected
    # document) comes back as a step result carrying the page's state
    # instead of as a bare transport timeout the agent cannot interpret.
    data["budgetMs"] = max(6000, int(act_timeout * 1000) - 4000)
    if tab_id is not None:
        data["tabId"] = tab_id
    if frame_id is not None:
        data["frameId"] = frame_id
    return step_dicts, act_timeout, data


def resolve_navigated_steps(r: Dict[str, Any]) -> Dict[str, Any]:
    """`r` with each timed-out step that NAVIGATED counted as done. Pure; `r` is not changed.

    A click on a submit button or a link replaces the document the step was injected
    into, so the step's own report can never come back: the extension waits out the step
    budget and reports a timeout whose `navigated` flag says a new document committed
    while the step ran. That is what success looks like for such a step, and the eval of
    2026-10-08 had every successful form submit flagged as an error. Only a committed
    navigation counts: a step that timed out with the DOM or the network moving, or with
    nothing observed, stays a failure, because there the missing report may mean the
    action never happened.

    The batch stops at a timed-out step (the extension breaks out of its loop), so the
    steps after it never ran; `stoppedAfterNavigation` lists them for the report.
    """
    results = r.get("results") or []
    if not any(_navigated_timeout(res) for res in results):
        return r
    out = dict(r)
    fixed = []
    for res in results:
        if _navigated_timeout(res):
            res = dict(res, ok=True, navigatedDuringStep=True)
        fixed.append(res)
    out["results"] = fixed
    total = r.get("totalSteps", len(fixed))
    out["completedSteps"] = sum(1 for res in fixed
                                if isinstance(res, dict) and res.get("ok") is not False)
    last = fixed[-1] if fixed else None
    if isinstance(last, dict) and last.get("navigatedDuringStep") and not r.get("notRunSteps"):
        after = last.get("step")
        unrun = list(range(after + 1 if isinstance(after, int) else len(fixed), total))
        if unrun:
            out["stoppedAfterNavigation"] = unrun
    out["ok"] = (all(isinstance(res, dict) and res.get("ok") is not False for res in fixed)
                 and not out.get("stoppedAfterNavigation") and not r.get("notRunSteps")
                 and not r.get("batchTimedOut"))
    return out


def _navigated_timeout(res: Any) -> bool:
    return (isinstance(res, dict) and res.get("ok") is False and bool(res.get("actionTimedOut"))
            and bool(res.get("navigated")) and not res.get("tabClosed")
            and res.get("stepAction") != "wait")


def format_act_result(r: Dict[str, Any], step_dicts: List[Dict[str, Any]]
                      ) -> Tuple[str, int, int]:
    """(report text, completed steps, total steps) for a `content.act` response."""
    head = ("OK" if r.get("ok") else "FAILED")
    lines = [f"{head}: {r.get('completedSteps', 0)}/{r.get('totalSteps', len(step_dicts))} steps completed"]
    if r.get("batchTimedOut"):
        not_run = r.get("notRunSteps") or []
        lines.append(
            "BATCH BUDGET EXHAUSTED: "
            + (f"steps {', '.join(str(s.get('step')) for s in not_run)} never ran"
               if not_run else "the remaining steps never ran")
            + "; the earlier steps used the whole budget. Re-issue the remaining steps as a "
              "separate page_act call (and check the page first: the steps that DID run may "
              "have changed it).")
    for res in r.get("results", []):
        i = res.get("step", "?")
        kind = res.get("stepAction", "?")
        if res.get("navigatedDuringStep"):
            # See resolve_navigated_steps: the page the step ran in was replaced, which is
            # why no report came back. Say where it landed; that is the step's result.
            where = res.get("urlAfter") or "?"
            if res.get("titleAfter"):
                where += f' ("{str(res["titleAfter"])[:80]}")'
            bits = [f"the page navigated to {where} while the step ran"]
            if res.get("urlBefore") and res.get("urlChanged"):
                bits.append(f"was {res['urlBefore']}")
            if res.get("tabStatus") and res.get("tabStatus") != "complete":
                bits.append(f"tab {res['tabStatus']}")
            lines.append(f"Step {i} {kind}: ok — {'; '.join(bits)}. The step's own report "
                         "was lost with the old page; read the new one before acting again.")
            suffix = digest_suffix(res)
            if suffix:
                lines.append(suffix.strip("\n"))
            continue
        if res.get("ok") is False:
            if res.get("actionTimedOut"):
                # An action (not a wait) that overran its deadline. The
                # extension already worked out what the page looks like now;
                # print it, because "timed out" alone reads as "no effect".
                reason = (f"timed out after {fmt_ms(res.get('elapsedMs'))} — "
                          f"{res.get('error', 'no page context')}")
                lines.append(f"Step {i} {kind}: TIMED OUT — {reason}")
                state_bits = []
                if res.get("urlAfter"):
                    state_bits.append(f"url now {res['urlAfter']}")
                if res.get("urlBefore") and res.get("urlChanged"):
                    state_bits.append(f"was {res['urlBefore']}")
                if res.get("titleAfter"):
                    state_bits.append(f'title "{res["titleAfter"]}"')
                if res.get("tabStatus"):
                    state_bits.append(f"tab {res['tabStatus']}")
                if res.get("navigated"):
                    state_bits.append("NAVIGATED")
                if state_bits:
                    lines.append("  page state: " + "; ".join(state_bits))
                suffix = digest_suffix(res)
                if suffix:
                    lines.append(suffix.strip("\n"))
                continue
            if res.get("timedOut"):
                reason = (f"timed out after {fmt_ms(res.get('elapsedMs'))} "
                          f"(requested {fmt_ms(res.get('requestedTimeoutMs'))})")
                if res.get("timeoutClampedToMs"):
                    reason += (f"; NOTE the wait was clamped to "
                               f"{fmt_ms(res['timeoutClampedToMs'])} by this call's overall "
                               f"budget; issue it as its own page_act to get the full timeout")
                pe = res.get("predicateError")
                if pe:
                    reason += f"; predicate error: {pe}"
                # R2: which sub-condition failed; never-matched /
                # present-but-hidden / still-present; with the match count.
                # A SCOPED wait that never saw its `within` container is a
                # different failure: the condition was never evaluated at
                # all, so reporting the target as "never-matched" would send
                # the agent hunting the wrong element.
                ls = res.get("lastSeen")
                if ls == "scope-never-appeared":
                    sm = res.get("scopeMissing")
                    reason += ("; the within-scope "
                               + (f"{sm} " if sm else "")
                               + "never appeared; the condition was never "
                                 "evaluated inside it (fix the scope, not the target)")
                elif ls:
                    reason += f"; selector {ls}"
                    mc = res.get("matchCount")
                    if mc is not None:
                        reason += (f" ({mc} match{'es' if mc != 1 else ''}"
                                   f" at last check)")
            else:
                reason = res.get("error", "unknown error")
            lines.append(f"Step {i} {kind}: FAILED — {reason}")
            detail = {k: v for k, v in res.items()
                      if k in ("target", "candidates", "available", "waitedFor",
                               "scopeMissing", "control", "reachability")}
            if detail:
                lines.append(f"  {compact(detail)}")
            # The page state is worded by the server (formatting.page_state_warning), not
            # dumped: an older extension's own text in it says to activate the tab.
            warn = page_state_warning(res.get("pageState"))
            if warn:
                lines.append(f"  WARNING: {warn}")
        else:
            bits = []
            if kind == "wait":
                matched = res.get("matched")
                # "scope-gone" = the `within` container itself left the DOM,
                # which SATISFIES a scoped gone-wait; spell that out or it
                # reads like an internal token.
                what = ("scope-gone (the within container is no longer in the DOM)"
                        if matched == "scope-gone" else matched)
                bits.append(f"matched {what} in {fmt_ms(res.get('elapsedMs'))}")
            if res.get("target") and isinstance(res["target"], dict):
                t = res["target"]
                bits.append(f"{t.get('ref', '')} {t.get('role', '')} \"{t.get('name', '')}\"".strip())
            if res.get("confirmedValue") is not None:
                bits.append(f"value now {compact(res['confirmedValue'])}")
            if res.get("selectedLabel"):
                bits.append(f"selected \"{res['selectedLabel']}\" ({res.get('selectedValue')})")
            for flag in ("urlChanged", "dialogOpened", "navigated", "expandedChanged"):
                if res.get(flag):
                    bits.append(flag)
            if res.get("ambiguous"):
                bits.append(f"AMBIGUOUS ({res.get('candidates')} matches; acted on the first actionable one; use nth/within to pin)")
            # A click aimed at a toggle reports the control's actual
            # post-settle state, so "ok" never has to be taken on trust.
            ctl = res.get("control")
            if isinstance(ctl, dict) and ctl.get("checked") is not None:
                moved = ("checked" if ctl["checked"] else "unchecked")
                bits.append(f"control {ctl.get('ref', '')} is now {moved}"
                            f" (was {'checked' if ctl.get('checkedBefore') else 'unchecked'})")
            # What the step CHANGED, quantitatively; the question 64 of 101
            # acts in the corpus spent a whole second round trip asking, because
            # "OK: 1/1 steps completed" answers "did the mechanism run", which is
            # never what the agent wanted to know.
            delta = res.get("nodeDelta")
            if delta is not None:
                if delta:
                    bits.append(f"{delta:+d} element nodes "
                                f"({res.get('nodesBefore')} -> {res.get('nodesAfter')})")
                elif res.get("effect") == "rerendered":
                    # 0 NET nodes but real churn: the list replaced its rows rather
                    # than growing. domChanged could never express this, and reading
                    # it as "nothing happened" is how a working click gets retried.
                    bits.append(f"re-rendered in place: {res.get('nodesAdded', 0)} added / "
                                f"{res.get('nodesRemoved', 0)} removed, net 0")
                else:
                    bits.append("0 element nodes added or removed")
                if res.get("measureSettled") is False:
                    bits.append("(page still changing; that count is a LOWER BOUND)")
            elif res.get("measureFailed"):
                bits.append("change NOT measured (the page could not be probed)")
            for c in (res.get("counts") or []):
                if c.get("error"):
                    bits.append(f"count {c.get('selector')}: NOT MEASURED — {c['error']}")
                elif c.get("before") is not None and c.get("after") is not None:
                    bits.append(f"count {c['selector']}: {c['before']} -> {c['after']} "
                                f"({c.get('delta', c['after'] - c['before']):+d})")
            if kind == "click" and not res.get("domChanged") and not res.get("urlChanged"):
                bits.append("NO visible effect (domChanged=false)")
                if res.get("noEffectHint"):
                    bits.append(f"probable cause: {res['noEffectHint']}")
            if res.get("labelForMissing"):
                bits.append(f"WARNING: label points at for=\"{res['labelForMissing']}\" "
                            f"which is not in the document")
            if res.get("urlAfter") and res.get("urlChanged"):
                bits.append(f"now at {res['urlAfter']}")
            lines.append(f"Step {i} {kind}: ok" + (f" — {'; '.join(b for b in bits if b)}" if any(bits) else ""))
            suffix = digest_suffix(res)
            if suffix:
                lines.append(suffix.strip("\n"))
            if res.get("watched"):
                # Pair each watched target with its BEFORE row when the
                # extension captured one. "visible, enabled" alone does not answer
                # "did the Submit button enable?"; only the transition does.
                before_by_target = {b.get("watch"): b
                                    for b in (res.get("watchedBefore") or [])}

                def _state(row):
                    s = ["visible" if row.get("visible") else "hidden",
                         "enabled" if row.get("enabled") else "disabled"]
                    if row.get("value") is not None:
                        s.append(f"value={compact(row['value'])}")
                    return ", ".join(s)

                for w in res["watched"]:
                    if not w.get("found"):
                        lines.append(f"  watch {w.get('watch')}: NOT FOUND")
                        continue
                    now = _state(w)
                    prev = before_by_target.get(w.get("watch"))
                    if prev is not None and prev.get("found") and _state(prev) != now:
                        lines.append(f"  watch {w.get('watch')}: {w.get('ref', '')} "
                                     f"{_state(prev)} -> {now}")
                    elif prev is not None and not prev.get("found"):
                        lines.append(f"  watch {w.get('watch')}: {w.get('ref', '')} "
                                     f"APPEARED; now {now}")
                    else:
                        lines.append(f"  watch {w.get('watch')}: {w.get('ref', '')} {now}")
    # Steer from the RESPONSE, at the moment of the waste. `check` was issued
    # 0 times in 150 steps while every checkbox in the corpus went through `click`
    # (nine of which reported success without changing state), and `watch` was
    # passed 0 times while 64 of 101 acts were followed by a probe asking exactly
    # what watch answers. Both are documented; description prose did not move
    # either number, so name them here, with the value to pass.
    unrun = r.get("stoppedAfterNavigation") or []
    if unrun:
        lines.append(
            f"Step{'s' if len(unrun) > 1 else ''} {', '.join(str(s) for s in unrun)} never "
            "ran: the batch stops at a step that replaced the page. Re-issue "
            f"{'them' if len(unrun) > 1 else 'it'} as a new page_act call against the new page.")
    completed = r.get("completedSteps", 0)
    total_steps = r.get("totalSteps", len(step_dicts))
    for hint in (check_instead_of_click_hint(r.get("results", [])),
                 watch_hint(step_dicts, r.get("results", []))):
        if hint:
            lines.append(hint)
    out = "\n".join(lines)
    return out, completed, total_steps
