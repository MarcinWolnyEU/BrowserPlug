/*
 * Unit tests for actWait in extension-gecko/background.js — the background-driven poll
 * loop behind a content.act 'wait' step.
 *
 * The polling loop is where the HISTORY of a wait lives: automation.js's waitCheck
 * is a ONE-SHOT check of the current document and cannot tell "the container never
 * rendered" from "the container was removed" — only the sequence of polls can. A
 * `gone` wait must therefore NOT be satisfied by a scope that has not appeared yet
 * (that is a vacuous success), but MUST be satisfied by a scope that appeared and
 * then disappeared.
 *
 * Like proxy.test.mjs / connection.test.mjs: background.js is a top-level extension
 * script, so we slice actWait out of the source and drive it with a stubbed
 * runAutomation, fake timers and a fake clock (so a 10s wait costs no wall time).
 * Run with: npm run test:js  (== node --test "tests/js/*.test.mjs")
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

function loadActWait() {
  const start = BG_SRC.indexOf('// Background-driven wait used by content.act');
  const end = BG_SRC.indexOf('// Console buffer reader (console.read action)', start);
  assert.ok(start >= 0 && end > start, 'actWait block not found in background.js');
  const block = BG_SRC.slice(start, end);

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    // Fake clock + timers: the poll loop is driven by Date.now(), so advancing the
    // clock inside setTimeout makes a 10s wait resolve in a few microtasks.
    let now = 100000;
    const Date = { now: () => now };
    function setTimeout(fn, ms) { now += ms; queueMicrotask(fn); return 0; }

    const calls = [];
    let responder = () => ({ matched: null });
    async function runAutomation(tabId, method, args, frameId) {
      calls.push({ tabId, method, args, frameId });
      const r = responder(calls.length - 1, args);
      if (r && r.__throw) throw new Error(r.__throw);
      return r;
    }

    // tab.status gate: default 'complete' (settled) so existing tests are unchanged;
    // a per-poll responder lets a test drive the mid-navigation placeholder window.
    let tabStatusFor = () => 'complete';
    const browser = { tabs: { get: async (id) => ({ id, status: tabStatusFor(calls.length - 1) }) } };

    ${block}

    return {
      actWait, calls,
      setResponder: (fn) => { responder = fn; },
      setTabStatus: (fn) => { tabStatusFor = fn; }
    };
  `);
  return factory();
}

const TIMEOUT = 1000; // 4 polls at 250ms on the fake clock

// ---- a `gone` wait on a scope that has not rendered YET ----------------------

test('a gone-wait whose `within` never renders does NOT succeed vacuously', async () => {
  const m = loadActWait();
  // The new waitCheck contract: an unresolved `within` reports scopeMissing and
  // matched:null (it never auto-satisfies `gone`).
  m.setResponder(() => ({ matched: null, scopeMissing: '#panel',
                          selectorMatchCount: 0, selectorVisible: false }));

  const res = await m.actWait(1, { action: 'wait', target: '.spinner', gone: true,
                                   within: '#panel', timeout_ms: TIMEOUT });

  assert.equal(res.ok, false, 'a container that never appeared cannot prove its spinner is gone');
  assert.equal(res.timedOut, true);
  assert.equal(res.lastSeen, 'scope-never-appeared');
  assert.equal(res.scopeMissing, '#panel');
  assert.ok(m.calls.length > 1, 'it kept polling — the scope might still render');
});

test('a gone-wait whose `within` appeared and then vanished SUCCEEDS (scope-gone)', async () => {
  const m = loadActWait();
  m.setResponder((i) => i === 0
    // poll 1: the modal is there, the spinner inside it is there
    ? { matched: null, selectorMatchCount: 1, selectorVisible: true }
    // poll 2: the whole modal was torn down — what it contained is gone with it
    : { matched: null, scopeMissing: '#modal', selectorMatchCount: 0, selectorVisible: false });

  const res = await m.actWait(1, { action: 'wait', target: '.spinner', gone: true,
                                   within: '#modal', timeout_ms: TIMEOUT });

  assert.equal(res.ok, true);
  assert.equal(res.matched, 'scope-gone');
  assert.equal(res.scopeMissing, '#modal');
});

// ---- a gone-wait must not be satisfied by the mid-navigation placeholder -----
// During a full-page navigation an executeScript binds to Firefox's transient
// placeholder document (right URL, readyState "complete", but EMPTY), where every
// node is absent — so a gone-wait's condition matches VACUOUSLY. That is the round-2
// vacuous-truth harm one level up (document-wide instead of container-scoped): the
// wait would end against a page that has not parsed yet. tab.status is the only
// signal that exposes the placeholder, so a gone success from a still-loading tab is
// discarded and the loop keeps polling until the destination document settles.

test('a gone-wait does NOT succeed off an empty placeholder while the tab is loading (condition match)', async () => {
  const m = loadActWait();
  // poll 0: real page, spinner present -> gone not satisfied.
  // polls 1-2: mid-navigation placeholder, 0 elements -> gone MATCHES (vacuous).
  // poll 3: destination settled, spinner genuinely gone -> real gone.
  m.setResponder((i) => i === 0
    ? { matched: null, selectorMatchCount: 1, selectorVisible: true }
    : { matched: 'selector', selectorMatchCount: 0, url: 'https://x.test/' });
  m.setTabStatus((i) => (i >= 1 && i <= 2) ? 'loading' : 'complete');

  const res = await m.actWait(1, { action: 'wait', target: '.spinner', gone: true,
                                   timeout_ms: TIMEOUT });

  assert.equal(res.ok, true, 'it eventually succeeds — once the real page confirms the spinner gone');
  assert.equal(res.matched, 'selector');
  assert.equal(m.calls.length, 4,
    'it polled THROUGH the two loading placeholder polls instead of false-succeeding at poll 1');
});

test('a scope-gone does NOT succeed off an empty placeholder while the tab is loading', async () => {
  const m = loadActWait();
  // poll 0: modal (scope) present -> scopeEverSeen. polls 1-2: placeholder, scope
  // absent -> scope-gone would fire, but the tab is loading. poll 3: settled, scope gone.
  m.setResponder((i) => i === 0
    ? { matched: null, selectorMatchCount: 1, selectorVisible: true }
    : { matched: null, scopeMissing: '#modal', selectorMatchCount: 0, selectorVisible: false });
  m.setTabStatus((i) => (i >= 1 && i <= 2) ? 'loading' : 'complete');

  const res = await m.actWait(1, { action: 'wait', target: '.spinner', gone: true,
                                   within: '#modal', timeout_ms: TIMEOUT });

  assert.equal(res.ok, true);
  assert.equal(res.matched, 'scope-gone');
  assert.equal(m.calls.length, 4,
    'the placeholder scope-miss during navigation did not end the wait early');
});

// Finding 3a: a within carrying only a page-global predicate must not be ended by the
// container vanishing — the predicate is global and still true, so keep polling.
test('a gone predicate wait with a within is NOT satisfied by the scope vanishing', async () => {
  const m = loadActWait();
  // poll 0: modal present (scopeEverSeen). polls 1+: modal gone, but the page-global
  // predicate (evaluated by waitCheck itself) is STILL true — reported as matched:null.
  m.setResponder((i) => i === 0
    ? { matched: null, selectorMatchCount: 0, selectorVisible: false }
    : { matched: null, scopeMissing: '#modal', selectorMatchCount: 0, selectorVisible: false });
  const res = await m.actWait(1, { action: 'wait', predicate: 'window.uploading', gone: true,
                                   within: '#modal', timeout_ms: TIMEOUT });
  assert.equal(res.ok, false, 'the predicate is page-global and still true — do not scope-gone it');
  assert.equal(res.timedOut, true);
});

// Finding 4: a single transient scope-miss (framework key-swap / Suspense) must not end a
// gone-wait — require the miss to persist across polls.
test('a gone-wait does NOT fire on a SINGLE transient scope-miss (debounce)', async () => {
  const m = loadActWait();
  // poll 1 is the one-poll unmount gap; the scope (and its spinner) is present otherwise.
  m.setResponder((i) => i === 1
    ? { matched: null, scopeMissing: '#modal', selectorMatchCount: 0, selectorVisible: false }
    : { matched: null, selectorMatchCount: 1, selectorVisible: true });
  const res = await m.actWait(1, { action: 'wait', target: '.spinner', gone: true,
                                   within: '#modal', timeout_ms: TIMEOUT });
  assert.equal(res.ok, false, 'a one-poll blip is not the scope permanently gone');
  assert.ok(m.calls.length >= 4, 'it kept polling past the transient miss');
});

// Finding 5: an empty injection result mid-wait is a transient (like a thrown injection),
// not a fatal error — keep polling.
test('an empty injection result mid-wait keeps polling (not a fatal error)', async () => {
  const m = loadActWait();
  m.setResponder((i) => i === 1
    ? { ok: false, error: 'no result from injected automation' }
    : i >= 2
      ? { matched: 'selector', selectorMatchCount: 1, url: 'https://x.test/' }
      : { matched: null, selectorMatchCount: 0, selectorVisible: false });
  const res = await m.actWait(1, { action: 'wait', target: '.row', timeout_ms: TIMEOUT });
  assert.equal(res.ok, true, 'the transient empty result did not abort the wait');
  assert.equal(res.matched, 'selector');
  assert.ok(m.calls.length >= 3, 'it kept polling through the empty result');
});

test('a scopeMissing poll does NOT pre-empt the normal classification', async () => {
  const m = loadActWait();
  // The scope IS there for most of the wait (so the condition really was evaluated),
  // and only the FINAL poll catches it mid-rerender. The report must still be about
  // the selector the agent waited for, not about the scope.
  m.setResponder((i) => i < 3
    ? { matched: null, selectorMatchCount: 0, selectorVisible: false }
    : { matched: null, scopeMissing: '#panel', selectorMatchCount: 0, selectorVisible: false });

  const res = await m.actWait(1, { action: 'wait', target: '.row', within: '#panel',
                                   timeout_ms: TIMEOUT });

  assert.equal(res.ok, false);
  assert.equal(res.lastSeen, 'never-matched', 'the element never appeared — say that, not "scope-never-appeared"');
  assert.equal(res.scopeMissing, '#panel', 'the scope state is still reported alongside');
});

// ---- selectorVisible sharpens the gone-wait timeout report -------------------

test('a gone-wait timeout distinguishes still-present-and-visible from still-present-but-hidden', async () => {
  const visible = loadActWait();
  visible.setResponder(() => ({ matched: null, selectorMatchCount: 1, selectorVisible: true }));
  const a = await visible.actWait(1, { action: 'wait', target: '.modal', gone: true, timeout_ms: TIMEOUT });
  assert.equal(a.lastSeen, 'still-present-and-visible');
  assert.equal(a.matchCount, 1);

  const hidden = loadActWait();
  // The node is still in the DOM but no longer rendered — a different problem, and a
  // different fix (the agent probably wanted require_visible, or another selector).
  hidden.setResponder(() => ({ matched: null, selectorMatchCount: 1, selectorVisible: false }));
  const b = await hidden.actWait(1, { action: 'wait', target: '.modal', gone: true, timeout_ms: TIMEOUT });
  assert.equal(b.lastSeen, 'still-present-but-hidden');
});

// ---- a failed FINAL injection must not erase the report ----------------------

test('an injection failure on the last poll keeps the last GOOD observation', async () => {
  const m = loadActWait();
  m.setResponder((i) => i < 2
    ? { matched: null, selectorMatchCount: 2, selectorVisible: false }
    // routine: the page navigated, so the last injection(s) fail
    : { __throw: 'Missing host permission for the tab' });

  const res = await m.actWait(1, { action: 'wait', target: '.row', require_visible: true,
                                   timeout_ms: TIMEOUT });

  assert.equal(res.ok, false);
  assert.equal(res.lastSeen, 'present-but-hidden', 'the diagnosis survived the failed injection');
  assert.equal(res.matchCount, 2);
  assert.match(res.injectError, /host permission/);
});

// ---- the existing classifications still hold --------------------------------

test('a plain selector wait that matches returns immediately (the "present" timeout branch is unreachable)', async () => {
  const m = loadActWait();
  m.setResponder(() => ({ matched: 'selector', selectorMatchCount: 2, url: 'https://x.test/' }));

  const res = await m.actWait(1, { action: 'wait', target: '.row', timeout_ms: TIMEOUT });
  assert.equal(res.ok, true);
  assert.equal(res.matched, 'selector');
  assert.equal(m.calls.length, 1);
});

test('a selector that never appears times out as never-matched', async () => {
  const m = loadActWait();
  m.setResponder(() => ({ matched: null, selectorMatchCount: 0, selectorVisible: false }));

  const res = await m.actWait(1, { action: 'wait', target: '.row', timeout_ms: TIMEOUT });
  assert.equal(res.lastSeen, 'never-matched');
  assert.equal(res.waitedFor.selector, '.row');
});

test('an invalid-selector error from waitCheck fails the wait immediately', async () => {
  const m = loadActWait();
  m.setResponder(() => ({ matched: null, error: 'invalid selector: ((' }));

  const res = await m.actWait(1, { action: 'wait', target: '((', timeout_ms: TIMEOUT });
  assert.equal(res.ok, false);
  assert.match(res.error, /invalid selector/);
  assert.equal(m.calls.length, 1, 'a selector that can never parse is not worth polling');
});

// ---- the act loop's `watch` sub-call must inherit the step's scope -----------
// (source-level guard: the watch call lives inside handleContentAction's giant
// switch, which cannot be sliced out on its own.)

test('the act loop pushes the step\'s `within` down into every watch target', () => {
  const loopStart = BG_SRC.indexOf("if (step.watch && res.ok !== false)");
  const loopEnd = BG_SRC.indexOf("res.step = i;", loopStart);
  assert.ok(loopStart >= 0 && loopEnd > loopStart,
    'act-loop watch block not found in background.js — the slice anchors moved, so this ' +
    'guard is stale (fix the anchors), not the product');
  const loop = BG_SRC.slice(loopStart, loopEnd);
  assert.ok(/within:\s*step\.within/.test(loop),
    'inspect resolves each watch entry on its own args — without step.within pushed in, ' +
    'a scoped step would report the WRONG panel\'s elements');
  assert.ok(/runAutomation\(actTabId, 'inspect', \{ watch: watch \}/.test(loop));
});
