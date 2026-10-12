#!/usr/bin/env python3
"""Response-time steering hints (agent-experience feedback, 2026-08-03).

Advocating a capability in a tool DESCRIPTION is measurably INERT. page_eval's
description opens with "(escape hatch; prefer page_act/page_read/page_snapshot)"
and it was still called 143 times against page_snapshot's 35; page_act's already
promises that step results report state "without an extra round trip", and 32
re-probe calls followed anyway. What DOES get followed is text delivered IN THE
RESPONSE at the moment of the waste: page_read's "— continue with offset=N" is
obeyed, and page_snapshot's frame list is obeyed because it names the parameter
AND the value to pass.

So every function here is a pure, stdlib-only renderer of ONE short hint that
names the capability, the exact argument, and the value to pass, and returns ""
whenever the situation is not the pattern. That last part IS the design: a hint
that fires on every call is wallpaper within one session, which is the exact
failure this module exists to remove. Each one is a couple of lines of hook in
server/tools/ (the eval_hints.py precedent: pure module, minimal seam).

The gaps these close, measured over one 150-step corpus:
  * `check` was issued 0 times; every checkbox was driven with `click`, and
    nine of those reported success without the control's state moving;
  * `watch` was passed 0 times, so the batch that already knew the outcome was
    followed by a second call asking what happened;
  * a truncated snapshot said "scope with root= or raise max_elements"; it
    named the parameters but never a VALUE for either, which is the difference
    between the offset hint (followed) and this one (not);
  * only page_snapshot ever mentions frames, and page_read's `frame_id` doc
    says "from page_snapshot's frame list", pointing at a tool the read/act
    loop never calls. Content one frame down therefore reads as simply absent.
"""

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --- check-instead-of-click ---------------------------------------------------

# automation.js `controlFor` labels a resolved toggle "native" (a real
# <input type=checkbox|radio>) or "aria" (role=checkbox|radio|switch|menuitem*).
# Anything else, an ordinary button, a link, has no `control` on its result at
# all, so ordinary clicks never see this hint.
_TOGGLE_CONTROL_KINDS = ("native", "aria")

# --- watch --------------------------------------------------------------------

# The step kinds whose effect an agent will want to verify. press/hover/scroll/
# read_clipboard are excluded: a batch of those is usually navigation-of-the-eye,
# not a state change, and hinting there is how a hint becomes wallpaper.
_MUTATING_STEP_ACTIONS = ("click", "fill", "select", "check")

# Fallback example when the results carry no usable selector. Deliberately
# generic-looking so it reads as a shape to copy, not as a claim about this page.
_GENERIC_WATCH_TARGETS = ("#result", "button[type=submit]")

# A hint must stay one or two short lines. A 200-char cssPath in the example
# would blow that and teach nothing, so an over-long selector is skipped rather
# than truncated; a truncated selector is a broken example.
_MAX_WATCH_SELECTOR = 60

# --- snapshot root ------------------------------------------------------------

# Share of elements that must sit under ONE #id-anchored container before a
# concrete root= is suggested. An id is unique in a valid document, which is what
# makes the suggestion safe; cssPath (automation.js) caps at depth 4, so a
# non-id path could match an earlier same-shaped element. Do NOT relax this to
# make the hint fire more often; a confidently wrong root= re-creates exactly
# the failure records-mode extraction was built to remove.
_ROOT_SHARE_THRESHOLD = 0.6

# A "container" holding a single element is not a container; requiring two keeps
# a degenerate one-element snapshot from producing an authoritative-looking root.
_MIN_ROOT_ELEMENTS = 2

# Roles mode="forms" actually renders (radio/checkbox groups, inputs, selects,
# buttons). Used only to decide whether naming mode="forms" is worth a clause.
_FORM_ROLES = frozenset({
    "textbox", "searchbox", "checkbox", "radio", "switch",
    "combobox", "listbox", "button", "slider", "spinbutton",
})

# --- frames -------------------------------------------------------------------

# Byte-identical to the line page_snapshot emits for its own frame list. Both
# surfaces must say the same thing: an agent that learned `frame_id` from a
# snapshot has to recognize it in a page_read/page_act response.
FRAMES_HEADER = "Frames (pass frame_id to page_* tools to reach inside):"

# One-line leads for the two callers that reach the frame list from a FAILURE
# rather than from a successful map, so the block names the cause as well as the
# next action. Exported so both hooks say it the same way.
FRAMES_LEAD_EMPTY_READ = ("Nothing was extracted from the top document — the content may be "
                          "inside an iframe, and page_read takes frame_id too:")
FRAMES_LEAD_NO_MATCH = ("The target matched nothing in the top document — it may be inside "
                        "an iframe, and page_act takes frame_id too:")


# --- no browser attached ------------------------------------------------------

# The one failure in this whole surface that an agent CANNOT resolve by trying
# something else, and the terse original invited exactly the wrong recovery: agents
# have gone off to install a browser, launch a headless one, or start a fresh
# profile, none of which can ever attach to this server, rather than spend one
# sentence asking the person sitting in front of it. So the guidance names the human
# step and rules the workarounds out by name.
#
# The MARKER is what server.py's routing layer returns and what tool_outcome
# classifies on. It is matched, never rewritten: tests/unit/test_browser_routing.py
# pins that exact string by EQUALITY, and tool_outcome.fail() must hand its argument
# back byte-identical. The guidance is therefore appended at the response seam
# (_error_of), where the text an agent reads is assembled.
NO_BROWSER_MARKER = "No extension connection available"

NO_BROWSER_GUIDANCE = (
    " This needs the USER to fix and cannot be worked around from here. Ask them to check "
    "that their browser is running and that the BrowserPlug extension's popup says "
    "Connected (it has a Reconnect link), then retry. Do not install, launch, or automate "
    "another browser instead: none is attached to this server, and a fresh one is logged "
    "in to nothing. Run server_info to see what the server currently sees."
)


def no_browser_hint(message: Optional[str]) -> str:
    """The guidance to append to `message`, or "" when it is not that failure.

    Returns "" (not the guidance) when the marker is already followed by it, so a
    message that crosses two seams cannot collect the paragraph twice.
    """
    if not message or NO_BROWSER_MARKER not in message:
        return ""
    if NO_BROWSER_GUIDANCE.strip() in message:
        return ""
    # The marker carries no full stop of its own ("...connection available"), while the
    # roster callers append to a sentence that already ends in one. Supply the period only
    # when it is missing, so the join never reads "available This needs" or "connected..".
    lead = "" if message.rstrip().endswith((".", "!", "?", ":")) else "."
    return lead + NO_BROWSER_GUIDANCE


def _selector_of(res: Dict[str, Any]) -> Optional[str]:
    """The CSS selector automation.js `describe()` attached to a step's target."""
    target = res.get("target")
    if not isinstance(target, dict):
        return None
    sel = target.get("selector")
    return sel if isinstance(sel, str) and sel.strip() else None


def check_instead_of_click_hint(step_results: Sequence[Any]) -> str:
    """Name the `check` step action after a click landed on a toggle, or "".

    `check` was issued 0 times in the 150-step corpus while every checkbox was
    driven with `click`, and a click is a TOGGLE, so it only lands on the state
    the agent wanted by luck. `check` takes the state it wants and is idempotent.

    Fires on the first click step whose result carries a resolved `control`
    (automation.js `controlFor`: a real checkbox/radio, or an aria toggle),
    success or failure alike: the nine clicks that reported success without the
    control moving are precisely the ones that needed this. Once per batch.

    Ordinary buttons and links carry no `control` at all, so they never match.
    """
    for res in step_results or []:
        if not isinstance(res, dict):
            continue
        if res.get("stepAction") != "click":
            continue
        if res.get("value") is not None:
            continue  # not a bare click; the agent is already passing state
        ctl = res.get("control")
        if not isinstance(ctl, dict) or ctl.get("kind") not in _TOGGLE_CONTROL_KINDS:
            continue
        ref = ctl.get("ref") or (res.get("target") or {}).get("ref")
        if not ref:
            # Without a ref there is no valid example to print, and an example
            # naming a target that cannot be passed is worse than no hint.
            continue
        role = ctl.get("role") or "checkbox"
        return (f'  HINT: {ref} is a {role} — use '
                f'{{"action":"check","target":"{ref}","value":true}} to SET it (idempotent); '
                f'a click toggles whatever it currently is.')
    return ""


def watch_hint(sent_steps: Sequence[Any], step_results: Sequence[Any]) -> str:
    """Name `watch` after a state-changing batch that did not use it, or "".

    `watch` was passed 0 times in 150 steps, and 32 calls followed a page_act
    purely to ask what it had done, which `watch` answers in the SAME call.

    Silent when the batch already used `watch` (the agent knows), when it
    contains a `wait` step (it is already verifying an outcome), and when
    nothing in it changes state (press/hover/scroll batches).
    Once per batch. The example uses selectors the results actually reported, so
    it can be pasted as-is; the generic pair is the fallback when none is usable.

    Also silent once the step's own measurement ALREADY answered the question. A
    step that came back with a node delta or a `count` has told the agent what
    changed; appending "you could have asked what changed" to that is exactly the
    every-call noise this module exists to remove, and noise is how the frames hint
    would have died too.
    """
    steps = [s for s in (sent_steps or []) if isinstance(s, dict)]
    if not steps:
        return ""
    if any(s.get("action") == "wait" for s in steps):
        return ""
    if any(s.get("watch") for s in steps):
        return ""
    if not any(s.get("action") in _MUTATING_STEP_ACTIONS for s in steps):
        return ""
    if any(isinstance(r, dict) and (r.get("nodeDelta") is not None or r.get("counts"))
           for r in (step_results or [])):
        return ""

    selectors: List[str] = []
    for res in step_results or []:
        if not isinstance(res, dict):
            continue
        sel = _selector_of(res)
        if sel and len(sel) <= _MAX_WATCH_SELECTOR and sel not in selectors:
            selectors.append(sel)
        if len(selectors) == 2:
            break
    if not selectors:
        selectors = list(_GENERIC_WATCH_TARGETS)

    rendered = json.dumps(selectors, separators=(",", ":"), ensure_ascii=False)
    return (f'  HINT: add "watch":{rendered} to a step and the SAME call reports those '
            f"elements' visible/enabled/value afterwards; no second call to check.")


def _dominant_id_root(elements: Sequence[Dict[str, Any]]) -> Tuple[Optional[str], int]:
    """(root selector, how many elements sit under it) for the one #id-anchored
    container that holds at least _ROOT_SHARE_THRESHOLD of the elements, else
    (None, 0). cssPath emits "#id > tag > tag ...", so the first segment is the
    nearest id-bearing ancestor within its depth-4 budget."""
    total = len(elements)
    if not total:
        return None, 0
    counts: Dict[str, int] = {}
    for el in elements:
        sel = el.get("selector")
        if not isinstance(sel, str):
            continue
        head = sel.split(" > ", 1)[0].strip()
        if len(head) < 2 or not head.startswith("#"):
            continue
        counts[head] = counts.get(head, 0) + 1
    if not counts:
        return None, 0
    # Sorted, not max(): ties below the threshold must still resolve the same way
    # on every run, or the same page produces two different refusals.
    head, n = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    if n < _MIN_ROOT_ELEMENTS or n < total * _ROOT_SHARE_THRESHOLD:
        return None, 0
    return head, n


def snapshot_root_hint(elements: Sequence[Any], count: int, max_elements: int) -> str:
    """The truncation line for page_snapshot, naming VALUES rather than parameters.

    The old text ("TRUNCATED at 150; scope with root= or raise max_elements")
    named both parameters and neither value, and was not followed. Two tiers:

      * confident: most elements share one #id-anchored container: name it, so
        the re-run is a paste rather than a guess;
      * no pattern: REFUSE to invent a selector and name the concrete
        alternatives instead (a bigger cap, mode="forms", mode="records").

    Always returns text: it replaces a notice the caller emits only when the
    snapshot really did truncate, and dropping that notice would hide the loss.
    """
    els = [e for e in (elements or []) if isinstance(e, dict)]
    cap = max_elements if isinstance(max_elements, int) and max_elements > 0 else 150
    seen = count if isinstance(count, int) and count > 0 else cap
    suggested = cap * 2

    root, under = _dominant_id_root(els)
    if root:
        return (f'TRUNCATED at {cap} of {seen}+ — {under} of them are under {root}: '
                f're-run with root="{root}" (and max_elements={suggested} if you need the rest).')

    forms = sum(1 for e in els if (e.get("role") or "") in _FORM_ROLES)
    forms_bit = f', or mode="forms" ({forms} inputs/buttons here)' if forms else ""
    return (f'TRUNCATED at {cap} — no single container holds most of them. '
            f'Re-run with max_elements={suggested}{forms_bit} '
            f'or mode="records" if this is a listing.')


def frames_hint(frames: Sequence[Any], lead: Optional[str] = None) -> str:
    """The subframe list, worded exactly as page_snapshot words it, or "".

    page_read's `frame_id` doc points an agent at "page_snapshot's frame list",
    i.e. at a tool its loop never calls, so a page whose content lives one frame
    down reads as simply empty. This renders the SAME block page_snapshot emits
    (same header, same `frame_id=N url` lines) at the two moments the read/act
    loop actually meets the problem: an empty extraction and a target that
    matched nothing. `lead` prepends one line naming that cause; pass
    FRAMES_LEAD_EMPTY_READ or FRAMES_LEAD_NO_MATCH.

    Returns "" when the page has no subframes, so a normal page pays nothing.
    """
    subframes = [f for f in (frames or [])
                 if isinstance(f, dict) and f.get("frameId", 0) != 0]
    if not subframes:
        return ""
    lines = [FRAMES_HEADER]
    for f in subframes:
        lines.append(f"  frame_id={f['frameId']} {f.get('url', '?')}")
    if lead:
        lines.insert(0, lead)
    return "\n".join(lines)


# --- a call without the url it needs ---------------------------------------------

# Seen in an eval of weak models: one emitted `url` AFTER it had closed the tool call,
# so the call arrived without it, and it then repeated the same broken call dozens of
# times while its reasoning said "I need to include the url". The refusal therefore
# carries a complete call to copy, in schema order: an example that leaves out a required
# argument gets copied as is. url comes last in both tools' schemas, after the arguments a
# model writes first (action, tab_id, background): a provider that decodes arguments in
# schema order can only emit url while nothing after it has been written yet (the schema
# A/B loop of 2026-10-08, tool-economy.md).
URL_PLACEHOLDER = "https://…"


def missing_url_text(tool: str, action: str, tab_id: Optional[int] = None) -> str:
    """The refusal for `tabs` open/find or `navigate` goto sent without `url`."""
    if tool == "tabs" and action == "find":
        return ('Error: url is required for action="find": a word from the title or URL of '
                'the tab you want: tabs(action="find", url="<word>").')
    if tool == "tabs":
        return ('Error: url is required for action="open"; tabs never opens an empty tab. '
                f'Call it like this: tabs(action="open", background=true, url="{URL_PLACEHOLDER}").')
    tab = str(tab_id) if tab_id is not None else "<your tab's id>"
    return ("Error navigating: url is required for goto, the default action: "
            f'navigate(tab_id={tab}, url="{URL_PLACEHOLDER}"). A tab that tabs action open '
            "created is already loading its url; read it with "
            f"page_read(tab_id={tab}) instead of navigating again.")


# --- a tab id this browser does not have ------------------------------------------

# The extension's STALE_TAB sentence, and the hint older builds appended to it: the user's
# active tab, its title and its URL, with "omit tab_id to target the active tab". That
# hint steered agents onto the tab the user was looking at, and it was wrong whenever the
# id came from another connected browser, which is what a call that ran beside a browsers
# switch returns. Current builds send the first sentence alone; both get the text below.
_STALE_TAB_RE = re.compile(
    r"Tab (\d+) no longer exists\.(?: It was probably closed\..*?"
    r"omit tab_id to target the active tab\.)?", re.S)


def stale_tab_text(message: Optional[str], browser_label: str = "") -> Optional[str]:
    """`message` with the stale-tab sentence replaced by one that sends the agent to its
    own tab, or `message` unchanged when it holds no such sentence. `browser_label`
    names the browser the call reached ("[2] Firefox Developer Edition") while two or
    more are connected, and is "" with one."""
    if not message:
        return message
    open_call = f'tabs(action="open", background=true, url="{URL_PLACEHOLDER}")'

    def repl(m: "re.Match[str]") -> str:
        tab = m.group(1)
        if browser_label:
            return (f"Tab {tab} no longer exists in {browser_label}. Tab ids belong to one "
                    "browser, so an id returned by another browser, or by a call that ran "
                    "before a browsers switch, is not valid here. Open your own tab here: "
                    f"{open_call}.")
        return (f"Tab {tab} no longer exists: it was closed, or never opened in this browser. "
                f"Open your own tab and use its id: {open_call}.")

    return _STALE_TAB_RE.sub(repl, message, count=1)


# --- a private window Firefox will not open -------------------------------------

# Firefox's own refusals, from browser/components/extensions/parent/ext-windows.js
# (windows.create): the first while the user has not let the extension run in private
# windows, the second while private browsing itself is switched off (a policy or the
# "never remember history" setting).
INCOGNITO_NOT_ALLOWED = "Extension does not have permission for incognito mode"
INCOGNITO_DISABLED = "`incognito` cannot be used if incognito mode is disabled"


def incognito_hint(message: Optional[str]) -> str:
    """The fix for a refused incognito window, or "" for any other error. Firefox's text
    names neither the setting nor the person who can change it, and an agent that reads
    "Windows API error" retries or gives up instead of asking."""
    if not message:
        return ""
    if INCOGNITO_NOT_ALLOWED in message:
        return (" Firefox opens private windows for an extension only when the user allows "
                "it: about:addons > BrowserPlug > Details > Run in Private Windows > Allow. "
                "Ask the user to switch that on, then retry; or open the page without "
                "incognito.")
    if INCOGNITO_DISABLED in message:
        return (" Private browsing is switched off in this Firefox (by a policy or the "
                "history settings), so no private window can be opened here; open the "
                "page without incognito.")
    return ""


# --- a frame id the tab does not have ------------------------------------------------

# Both engines answer an unknown frame with "Invalid frame IDs: [N]", which names neither
# where frame ids come from nor the two mistakes behind most such calls in the schema A/B
# loop and the eval: a frame_id sent without its tab_id (so the call ran on the user's
# active tab), and the tab id passed as the frame id.
_INVALID_FRAME = "Invalid frame ID"


def frame_id_hint(message: Optional[str], tab_id: Optional[int], frame_id: Optional[int]) -> str:
    """The fix for a refused frame id, or "" for any other error or a call without one."""
    if not message or frame_id is None or _INVALID_FRAME not in message:
        return ""
    where = (f"page_snapshot(tab_id={tab_id})" if tab_id is not None
             else "page_snapshot(tab_id=<your tab's id>)")
    if tab_id is None:
        return (" A frame id belongs to one tab, and this call named none, so it ran on the "
                "user's active tab: pass tab_id with frame_id. Frame ids are the frame_id= "
                f"lines {where} lists under Frames.")
    if frame_id == tab_id:
        return (f" frame_id {frame_id} is the tab id. Frame ids are the frame_id= lines "
                f"{where} lists under Frames; omit frame_id for the top document.")
    return (f" Frame ids change when the page reloads; take a current one from {where}, "
            "or omit frame_id for the top document.")
