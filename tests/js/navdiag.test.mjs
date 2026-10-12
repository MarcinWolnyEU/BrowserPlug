/*
 * The two facts a landing check needs that tab.url cannot give it.
 *
 * The failure being fixed: a live allegro.pl 403 was reported as
 * `Navigated tab 5 -> https://allegro.pl/... (settled in 936ms)`. The status WAS
 * known — it sat on the main-frame TAB_REQUESTS entry — but nothing lifted it out,
 * so a 403/404 with a full HTML body was indistinguishable from a clean load. The
 * second missing fact is where the tab was BEFORE the call: without it, "the
 * navigation never took effect" and "it arrived at exactly that URL" render alike.
 *
 * mainFrameOutcome is also the ONE implementation of the redirect/dialed-peer/
 * net-error scan that used to sit inline in the switch (and was therefore only
 * reachable by slicing statements out of the middle of a case body).
 *
 * Like acttimeout.test.mjs: background.js is a top-level extension script, so the
 * block is sliced out by anchor and driven with stubs. The gecko/chromium twin pair
 * exists because the two engines model redirects differently — gecko replaces the
 * channel per hop (several entries), chromium keeps one requestId (one entry) — and
 * the SAME backwards scan must yield the final status under both.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from 'jsdom';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

function loadNavDiag() {
  const start = BG_SRC.indexOf('// ---- navigation landing diagnostics');
  const end = BG_SRC.indexOf('async function handleNavigationAction(', start);
  assert.ok(start >= 0 && end > start,
    'navigation landing diagnostics block not found in background.js');
  const block = BG_SRC.slice(start, end);

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const TAB_REQUESTS = new Map();
    let tabState = { url: 'https://shop.example/listing' };
    let tabGone = false;
    const browser = { tabs: { get: async () => {
      if (tabGone) throw new Error('No tab with id: 7');
      return tabState;
    } } };

    ${block}

    return {
      mainFrameOutcome, navUrlBefore,
      setLog: (log) => { TAB_REQUESTS.set(7, log); },
      setTab: (t) => { tabState = t; },
      killTab: () => { tabGone = true; }
    };
  `);
  return factory();
}

const T0 = 1_000_000;
const mainFrame = (over) => ({ type: 'main_frame', method: 'GET', ...over });

test('the main document HTTP status is lifted out of the request log', () => {
  const m = loadNavDiag();
  m.setLog([mainFrame({ id: 'r1', url: 'https://shop.example/gone', start: T0 + 1, status: 404 })]);
  assert.equal(m.mainFrameOutcome(7, T0).httpStatus, 404,
    'a 404 with a full HTML body renders as a normal page — the status is the only tell');
});

test('gecko multi-entry redirects: the FINAL status wins and the chain is stitched', () => {
  // FF152 replaces the channel per hop, so one navigation lands on SEVERAL entries;
  // the continuation is the LATEST of them and carries the real answer.
  const m = loadNavDiag();
  m.setLog([
    mainFrame({ id: 'r1', url: 'https://x.test/b', start: T0 + 1, status: 308,
                redirects: [{ from: 'http://x.test/a', to: 'https://x.test/b', status: 308 }] }),
    mainFrame({ id: 'r2', url: 'https://x.test/c', start: T0 + 2, status: 302,
                redirects: [{ from: 'https://x.test/b', to: 'https://x.test/c', status: 302 }] }),
    mainFrame({ id: 'r3', url: 'https://x.test/c', start: T0 + 3, status: 200 }),
  ]);
  const out = m.mainFrameOutcome(7, T0);
  assert.equal(out.httpStatus, 200, 'not the 308 of the first hop');
  assert.deepEqual(out.redirects, [
    { to: 'https://x.test/b', status: 308 },
    { to: 'https://x.test/c', status: 302 },
  ], 'the chain reads oldest-first, as the browser walked it');
});

test('chromium single-entry redirects produce the identical answer', () => {
  // The chromium model on the gecko copy: one entry carrying every hop plus the
  // final status. Both models must reconstruct the same chain, or an agent gets a
  // different redirect story depending on which browser it drives.
  const m = loadNavDiag();
  m.setLog([
    mainFrame({ id: 'r1', url: 'https://x.test/c', start: T0 + 1, status: 200,
                redirects: [{ from: 'http://x.test/a', to: 'https://x.test/b', status: 308 },
                            { from: 'https://x.test/b', to: 'https://x.test/c', status: 302 }] }),
  ]);
  const out = m.mainFrameOutcome(7, T0);
  assert.equal(out.httpStatus, 200);
  assert.deepEqual(out.redirects, [
    { to: 'https://x.test/b', status: 308 },
    { to: 'https://x.test/c', status: 302 },
  ]);
});

test('a still-pending main-frame request yields NO httpStatus key', () => {
  const m = loadNavDiag();
  m.setLog([mainFrame({ id: 'r1', url: 'https://slow.test/', start: T0 + 1 })]);
  const out = m.mainFrameOutcome(7, T0);
  assert.equal('httpStatus' in out, false,
    'absent, never undefined — "no status line" must not be readable as "it was 200"');
  assert.equal(out.redirects, undefined, 'and no empty redirect list either');
});

test('entries from BEFORE this navigation are ignored entirely', () => {
  const m = loadNavDiag();
  m.setLog([
    mainFrame({ id: 'old', url: 'https://prev.test/', start: T0 - 5000, status: 500,
                ip: '9.9.9.9',
                redirects: [{ from: 'https://prev.test/a', to: 'https://prev.test/b', status: 301 }] }),
    mainFrame({ id: 'r1', url: 'https://x.test/', start: T0 + 1, status: 200 }),
  ]);
  const out = m.mainFrameOutcome(7, T0);
  assert.equal(out.httpStatus, 200, "the previous page's 500 is not this navigation's status");
  assert.equal(out.redirects, undefined, "nor is the previous page's redirect chain");
  assert.equal(out.dialedAddress, undefined);
});

test('a SUB-RESOURCE failure never becomes the document status', () => {
  // The whole reason httpStatus exists is that a 500 buried among sub-resources was
  // unreadable. Promoting one to the document's status would be strictly worse.
  const m = loadNavDiag();
  m.setLog([
    mainFrame({ id: 'r1', url: 'https://x.test/', start: T0 + 1, status: 200 }),
    { type: 'script', method: 'GET', id: 's1', url: 'https://cdn.test/a.js', start: T0 + 2, status: 500 },
    { type: 'image', method: 'GET', id: 'i1', url: 'https://cdn.test/px.gif', start: T0 + 3, status: 404,
      redirects: [{ from: 'https://cdn.test/px', to: 'https://cdn.test/px.gif', status: 302 }] },
  ]);
  const out = m.mainFrameOutcome(7, T0);
  assert.equal(out.httpStatus, 200);
  assert.equal(out.redirects, undefined, 'a sub-resource redirect is not a navigation hop');
});

test('dialedAddress comes from the most recent main-frame entry; reqError from the errored one', () => {
  // A LATER successful sibling navigation must not mask our abort — hence two
  // separate cursors over the same backwards scan.
  const m = loadNavDiag();
  m.setLog([
    mainFrame({ id: 'a', url: 'https://x.test/1', start: T0 + 1, error: 'NS_BINDING_ABORTED' }),
    mainFrame({ id: 'b', url: 'https://x.test/2', start: T0 + 2, status: 200, ip: '5.5.5.5' }),
  ]);
  const out = m.mainFrameOutcome(7, T0);
  assert.equal(out.dialedAddress, '5.5.5.5');
  assert.equal(out.httpStatus, 200);
  assert.equal(out.reqError, 'NS_BINDING_ABORTED', 'the abort survives the later success');

  const m2 = loadNavDiag();
  m2.setLog([mainFrame({ id: 'r1', url: 'https://x.test/', start: T0 + 1, status: 200 })]);
  assert.equal(m2.mainFrameOutcome(7, T0).dialedAddress, undefined,
    'a pure hang never yields an ip, so the key is simply absent');
});

test('an untracked tab yields an empty outcome rather than throwing', () => {
  const m = loadNavDiag();
  assert.deepEqual(m.mainFrameOutcome(999, T0), {});
});

test('navUrlBefore reports the pre-navigation url, and null when the tab is unreachable', async () => {
  const m = loadNavDiag();
  m.setTab({ url: 'https://shop.example/listing?page=1' });
  assert.equal(await m.navUrlBefore(7), 'https://shop.example/listing?page=1');

  m.killTab();
  assert.equal(await m.navUrlBefore(7), null,
    'absent rather than wrong — the server then says nothing about where the tab was');

  const m2 = loadNavDiag();
  m2.setTab({});
  assert.equal(await m2.navUrlBefore(7), null, 'a tab with no url reads as unknown, not ""');
});

// ---- an abort that belongs to the load a navigation replaced --------------------------
// Firefox can deliver a replaced load's abort after the new document has committed, so it
// survives the commit that clears lastNavError. A background about:blank tab navigated at
// once produces it; so does a page still loading when the navigation begins. The events are
// replayed through the real webNavigation listeners into navigationError, the function the
// navigate result takes its net error from.

function loadNavEvents() {
  const start = BG_SRC.indexOf('function markNavCommitted(d) {');
  const end = BG_SRC.indexOf('// --- lastAccessed, observed', start);
  const dStart = BG_SRC.indexOf('// ---- navigation landing diagnostics');
  const dEnd = BG_SRC.indexOf('async function handleNavigationAction(', dStart);
  assert.ok(start >= 0 && end > start && dStart >= 0 && dEnd > dStart,
    'the webNavigation listeners or the landing diagnostics block was not found in background.js');
  // eslint-disable-next-line no-new-func
  return new Function(`
    const TAB_NAV = new Map();
    const TAB_REQUESTS = new Map();
    const L = {};
    const hook = (name) => ({ addListener: (fn) => { L[name] = fn; } });
    const browser = { webNavigation: { onCommitted: hook('committed'),
      onHistoryStateUpdated: hook('history'), onReferenceFragmentUpdated: hook('fragment'),
      onErrorOccurred: hook('error'), onCompleted: hook('completed') } };
    // wakeSettle sits beside waitForSettle, outside this slice; record who would be woken.
    const WOKEN = [];
    const wakeSettle = (tabId) => { WOKEN.push(tabId); };
    ${BG_SRC.slice(start, end)}
    ${BG_SRC.slice(dStart, dEnd)}
    return {
      navigationError, isNavAbort, abortAwaitingCompletion, navEventsSince, WOKEN,
      commit: (url, timeStamp) => L.committed({ tabId: 7, frameId: 0, url, timeStamp }),
      error: (url, error, timeStamp) => L.error({ tabId: 7, frameId: 0, url, error, timeStamp }),
      done: (url, timeStamp) => L.completed({ tabId: 7, frameId: 0, url, timeStamp }),
      nav: () => TAB_NAV.get(7) || {}
    };
  `)();
}

const ABORT = 'Error code 2152398850';            // webNavigation's NS_BINDING_ABORTED
const UNKNOWN_HOST = 'Error code 2152398878';
const TARGET = 'https://example.org/';

test('a replaced about:blank whose abort lands after the commit is not the navigation\'s error', () => {
  const m = loadNavEvents();
  m.commit(TARGET, T0 + 600);
  m.error('about:blank', ABORT, T0 + 610);
  assert.equal(m.navigationError(m.nav(), T0, { httpStatus: 200 }, TARGET), null);
});

test('nor is the abort of a page that was still loading when the navigation began', () => {
  const m = loadNavEvents();
  m.commit('https://example.com/', T0 - 400);
  m.commit(TARGET, T0 + 600);
  m.error('https://example.com/', ABORT, T0 + 610);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), null);
});

test('an abort that lands before the commit is cleared by it, as before', () => {
  const m = loadNavEvents();
  m.error('about:blank', ABORT, T0 + 300);
  m.commit(TARGET, T0 + 600);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), null);
});

test('a commit from just before the navigation does not make that page\'s abort its own', () => {
  const m = loadNavEvents();
  m.commit('https://example.com/', T0 - 10);
  m.error('https://example.com/', ABORT, T0 + 300);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), null);
});

test('an abort of the requested page, which never committed, is still reported', () => {
  const m = loadNavEvents();
  m.commit('https://example.com/', T0 - 400);
  m.error(TARGET, ABORT, T0 + 300);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), ABORT);
});

test('an abort of the committed page itself, stopped while loading, is still reported', () => {
  const m = loadNavEvents();
  m.commit(TARGET, T0 + 600);
  m.error(TARGET, ABORT, T0 + 700);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), ABORT);
});

test('an abort at a redirect target is the navigation\'s own', () => {
  const m = loadNavEvents();
  m.error('https://www.example.org/', ABORT, T0 + 300);
  const outcome = { redirects: [{ to: 'https://www.example.org/', status: 301 }] };
  assert.equal(m.navigationError(m.nav(), T0, outcome, TARGET), ABORT);
});

test('URLs are compared canonically and without the fragment', () => {
  const m = loadNavEvents();
  m.error('https://example.org/#top', ABORT, T0 + 300);
  assert.equal(m.navigationError(m.nav(), T0, {}, 'https://example.org'), ABORT);
});

test('back and forward know no target, so an abort they cannot place stays', () => {
  const m = loadNavEvents();
  m.error('https://example.com/', ABORT, T0 + 300);
  assert.equal(m.navigationError(m.nav(), T0, {}, null), ABORT);
});

test('only aborts are set aside: another error on another URL is still reported', () => {
  const m = loadNavEvents();
  m.commit(TARGET, T0 + 600);
  m.error('about:blank', UNKNOWN_HOST, T0 + 610);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), UNKNOWN_HOST);
});

test('a request-log error still reports when the replaced load\'s abort is set aside', () => {
  const m = loadNavEvents();
  m.commit(TARGET, T0 + 600);
  m.error('about:blank', ABORT, T0 + 610);
  assert.equal(m.navigationError(m.nav(), T0, { reqError: 'NS_ERROR_NET_RESET' }, TARGET),
    'NS_ERROR_NET_RESET');
});

test('an error from before the navigation began is ignored, as before', () => {
  const m = loadNavEvents();
  m.error(TARGET, UNKNOWN_HOST, T0 - 500);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), null);
});

// ---- the abort Firefox reports from the process a tab leaves ---------------------------
// A navigation that moves the tab to another content process is reported by both
// processes (WebNavigationContent.cpp, Bugzilla 1732564). The one the tab leaves ends its
// attempt at the SAME URL with NS_BINDING_ABORTED, on its own channel, so the abort can
// land after the new process committed the document. Live on Firefox 158 (extension
// 1.0.1): a background about:blank tab navigated at once to https://example.org/ came back
// "net error: request aborted ... [NS_BINDING_ABORTED = 2152398850]" in 3 to 9 runs of 10,
// with one 200 main-frame request in the log. The URL check above cannot set it aside,
// because the abort names the requested URL; the completed load of that URL does.

test('the leaving process\'s abort of the requested URL, after its document committed and loaded, is not an error', () => {
  const m = loadNavEvents();
  m.commit(TARGET, T0 + 586);
  m.error(TARGET, ABORT, T0 + 601);
  m.done(TARGET, T0 + 640);
  assert.equal(m.navigationError(m.nav(), T0, { httpStatus: 200 }, TARGET), null);
});

test('the same echo landing after the load finished is set aside too', () => {
  const m = loadNavEvents();
  m.commit(TARGET, T0 + 586);
  m.done(TARGET, T0 + 620);
  m.error(TARGET, ABORT, T0 + 700);
  assert.equal(m.navigationError(m.nav(), T0, { httpStatus: 200 }, TARGET), null);
});

test('a completion that names the URL only by another fragment still answers the abort', () => {
  const m = loadNavEvents();
  m.commit(TARGET + '#top', T0 + 586);
  m.error(TARGET, ABORT, T0 + 601);
  m.done(TARGET + '#top', T0 + 640);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), null);
});

test('without a completion the abort of the committed page stands, and waiting for one is worth it', () => {
  // The stopped-while-loading case above, seen from the wait's side: the navigate result
  // holds for ABORT_ECHO_GRACE_MS while abortAwaitingCompletion is true, then reports it.
  const m = loadNavEvents();
  m.commit(TARGET, T0 + 586);
  m.error(TARGET, ABORT, T0 + 601);
  assert.equal(m.abortAwaitingCompletion(m.nav(), T0), true);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), ABORT);
  m.done(TARGET, T0 + 640);
  assert.equal(m.abortAwaitingCompletion(m.nav(), T0), false, 'the completion ends the wait');
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), null);
});

test('a completion from before the navigation does not answer its abort', () => {
  // A reload of the same URL: the previous load's completion is no evidence for this one.
  const m = loadNavEvents();
  m.done(TARGET, T0 - 500);
  m.commit(TARGET, T0 + 300);
  m.error(TARGET, ABORT, T0 + 400);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), ABORT);
});

test('a completion of another URL does not answer an abort of the requested one', () => {
  const m = loadNavEvents();
  m.commit('https://example.com/', T0 - 400);
  m.done('https://example.com/', T0 + 100);
  m.error(TARGET, ABORT, T0 + 300);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), ABORT);
});

test('a completion never excuses an error that is not an abort', () => {
  const m = loadNavEvents();
  m.commit(TARGET, T0 + 300);
  m.done(TARGET, T0 + 350);
  m.error(TARGET, UNKNOWN_HOST, T0 + 400);
  assert.equal(m.navigationError(m.nav(), T0, {}, TARGET), UNKNOWN_HOST);
  assert.equal(m.abortAwaitingCompletion(m.nav(), T0), false);
});

test('only an abort of the committed document is worth waiting for', () => {
  const m = loadNavEvents();
  m.error(TARGET, ABORT, T0 + 300);
  assert.equal(m.abortAwaitingCompletion(m.nav(), T0), false, 'nothing committed: report it now');
  const m2 = loadNavEvents();
  m2.commit('https://example.com/', T0 + 300);
  m2.error(TARGET, ABORT, T0 + 400);
  assert.equal(m2.abortAwaitingCompletion(m2.nav(), T0), false, 'another document committed');
});

test('a reported net error carries the main-frame events in arrival order', () => {
  const m = loadNavEvents();
  m.commit('about:blank', T0 - 300);
  m.commit(TARGET, T0 + 586);
  m.error(TARGET, ABORT, T0 + 601);
  assert.deepEqual(m.navEventsSince(m.nav(), T0), [
    { ev: 'commit', url: TARGET, ms: 586 },
    { ev: 'error', url: TARGET, ms: 601, error: ABORT },
  ]);
});

test('a commit and a finished load wake a sleeping settle', () => {
  const m = loadNavEvents();
  m.commit(TARGET, T0 + 586);
  m.done(TARGET, T0 + 640);
  assert.deepEqual(m.WOKEN, [7, 7]);
});

test('an abort reads as one in every spelling, so a newer navigate is named as the cause', () => {
  const m = loadNavEvents();
  for (const spelling of ['NS_BINDING_ABORTED', ABORT, 'net::ERR_ABORTED']) {
    assert.equal(m.isNavAbort(spelling), true, spelling);
  }
  assert.equal(m.isNavAbort(UNKNOWN_HOST), false);
  assert.equal(m.isNavAbort(null), false);
});

// ---- waitForSettle: the blank tab's own commit is not the navigation's -----------------

function loadSettle() {
  const start = BG_SRC.indexOf('async function waitForSettle(tabId, opts = {}) {');
  const end = BG_SRC.indexOf('// Firefox answers a failed connection', start);
  assert.ok(start >= 0 && end > start, 'waitForSettle not found in background.js');
  const types = BG_SRC.indexOf('const QUIET_IGNORED_TYPES = ');
  assert.ok(types >= 0, 'QUIET_IGNORED_TYPES not found in background.js');
  // eslint-disable-next-line no-new-func
  return new Function(`
    const TAB_NAV = new Map();
    const TAB_NET = new Map();
    const TAB_REQUESTS = new Map();
    let clock = ${T0};
    let onTick = () => {};
    const Date = { now: () => clock };
    const setTimeout = (fn, ms) => { clock += ms; onTick(clock); Promise.resolve().then(fn); };
    let tab = { url: 'about:blank', title: 'New Tab', status: 'complete' };
    const browser = { tabs: { get: async () => tab } };
    ${BG_SRC.slice(types, BG_SRC.indexOf('\n', types))}
    ${BG_SRC.slice(start, end)}
    return {
      waitForSettle, wakeSettle,
      setNav: (nav) => TAB_NAV.set(7, nav),
      setNet: (net) => TAB_NET.set(7, net),
      setLog: (log) => TAB_REQUESTS.set(7, log),
      setTab: (t) => { tab = t; },
      onTick: (fn) => { onTick = fn; },
      now: () => clock
    };
  `)();
}

const LOAD = { sinceTs: T0, quietMs: 0, expectNav: true, timeoutMs: 20000 };

test('wait="load": the blank tab\'s own about:blank commit does not settle a navigation to a real URL', async () => {
  const m = loadSettle();
  m.setNav({ lastCommitted: T0 + 5, lastCommittedUrl: 'about:blank' });
  let ticks = 0;
  m.onTick(() => {
    if (++ticks === 2) {
      m.setNav({ lastCommitted: m.now(), lastCommittedUrl: TARGET });
      m.setTab({ url: TARGET, title: 'Example Domain', status: 'complete' });
    }
  });
  const r = await m.waitForSettle(7, { ...LOAD, ignoreBlankCommit: true });
  assert.equal(r.settled, true);
  assert.equal(r.url, TARGET, 'settled on the blank page the navigation was replacing');
});

test('without the option the about:blank commit still counts (page_act\'s settle is unchanged)', async () => {
  const m = loadSettle();
  m.setNav({ lastCommitted: T0 + 5, lastCommittedUrl: 'about:blank' });
  const r = await m.waitForSettle(7, LOAD);
  assert.equal(r.settled, true);
  assert.equal(r.url, 'about:blank');
});

test('a navigation that never leaves about:blank times out instead of settling there', async () => {
  const m = loadSettle();
  m.setNav({ lastCommitted: T0 + 5, lastCommittedUrl: 'about:blank' });
  const r = await m.waitForSettle(7, { ...LOAD, timeoutMs: 1000, ignoreBlankCommit: true });
  assert.equal(r.settled, false);
  assert.equal(r.timedOut, true);
});

test('the tab\'s url decides, not the order the commit events arrived in', async () => {
  // A late about:blank commit event after the real one must not hold a loaded page.
  const m = loadSettle();
  m.setNav({ lastCommitted: T0 + 400, lastCommittedUrl: 'about:blank' });
  m.setTab({ url: TARGET, title: 'Example Domain', status: 'complete' });
  const r = await m.waitForSettle(7, { ...LOAD, ignoreBlankCommit: true });
  assert.equal(r.settled, true);
  assert.equal(m.now(), T0, 'it waited for nothing');
});

test('the navigate case is wired to all four', () => {
  const src = BG_SRC.split('\r\n').join('\n');
  assert.match(src, /const net = netDigestFor\(navTabId, t0, committedAt >= t0 \? committedAt : null\);/);
  assert.match(src, /const rawErr = navigationError\(navState, t0, outcome,\n\s+op === 'goto' \? data\.url : \(op === 'reload' \? urlBefore : null\)\);/);
  assert.match(src, /if \(isNavAbort\(rawErr\) && navState\.lastNavIssuedAt && navState\.lastNavIssuedAt > t0\)/);
  assert.match(src, /expectNav: true,\n\s+ignoreBlankCommit: op === 'goto' && !!data\.url && data\.url !== 'about:blank',/);
});

// ---- waitForSettle sleeps until the answer can change -----------------------------------
// It used to poll every 150 ms, so a settle landed up to 150 ms after its condition held.
// Now it sleeps until the quiet deadline (or a young request ageing out) and the
// webNavigation, webRequest and tabs listeners wake it. Same predicate, same guarantee.

const SETTLED = { sinceTs: T0, quietMs: 250, expectNav: true, timeoutMs: 20000 };
const loadedAt = (m) => {
  m.setNav({ lastCommitted: T0 + 20, lastCommittedUrl: TARGET });
  m.setTab({ url: TARGET, title: 'Example Domain', status: 'complete' });
};

test('the quiet window ends at its deadline, not at the next poll', async () => {
  const m = loadSettle();
  loadedAt(m);
  m.setNet({ inflight: 0, lastActivity: T0 + 37 });
  const r = await m.waitForSettle(7, SETTLED);
  assert.equal(r.settled, true);
  assert.equal(m.now(), T0 + 37 + 250, 'a 150 ms poll would have answered at +300');
  assert.equal(r.waitedMs, 287);
  assert.deepEqual(r.timing, { commit: 0, load: 0, quiet: 287, settled: 287 });
});

test('a young request holds the settle until it ages out, as before', async () => {
  const m = loadSettle();
  loadedAt(m);
  m.setNet({ inflight: 1, lastActivity: T0 });
  m.setLog([{ id: 'x', type: 'xmlhttprequest', url: 'https://example.org/api', start: T0 }]);
  const r = await m.waitForSettle(7, SETTLED);
  assert.equal(r.settled, true);
  assert.equal(m.now(), T0 + 2500, 'only requests younger than 2.5 s count as in flight');
});

test('a pending beacon, ping or CSP report does not hold the quiet window', async () => {
  for (const type of ['beacon', 'ping', 'csp_report']) {
    const m = loadSettle();
    loadedAt(m);
    m.setLog([{ id: 'b', type, url: 'https://stats.example/collect', start: T0 + 5 }]);
    const r = await m.waitForSettle(7, SETTLED);
    assert.equal(r.settled, true, type);
    assert.equal(m.now(), T0 + 250, `${type} cannot deliver content to the page`);
  }
});

test('wait="dom" settles on the load event and the commit, whatever the network is doing', async () => {
  // chrome-devtools-mcp's goto waits for the load event only, then for a still DOM;
  // a page that keeps fetching must not hold the like-for-like wait at its settle.
  const m = loadSettle();
  loadedAt(m);
  m.setNet({ inflight: 1, lastActivity: T0 + 5 });
  m.setLog([{ id: 'x', type: 'xmlhttprequest', url: 'https://example.org/poll', start: T0 + 5 }]);
  const r = await m.waitForSettle(7, { ...LOAD, skipNetwork: true });
  assert.equal(r.settled, true);
  assert.equal(m.now(), T0, 'it waited for nothing');
});

test('navigate drops the network condition for wait="load" and "dom", never for "settled"', () => {
  const src = BG_SRC.split('\r\n').join('\n');
  assert.match(src, /quietMs: waitMode === 'settled' \? NAV_QUIET_MS : 0,\n\s+skipNetwork: waitMode !== 'settled',/);
  assert.match(src, /const quiet = !!opts\.skipNetwork \|\| \(young\.length === 0 && now >= quietAt\);/);
});

// The options the navigate case hands waitForSettle, built by the case's own object literal,
// so this follows the code rather than a copy of it.
function navSettleOpts(waitMode, commitBefore) {
  const src = BG_SRC.split('\r\n').join('\n');
  const m = /const settle = await waitForSettle\(navTabId, (\{[\s\S]*?\n\s*\})\);/.exec(src);
  assert.ok(m, 'the navigate case no longer calls waitForSettle(navTabId, {...})');
  // eslint-disable-next-line no-new-func
  return new Function('waitMode', 'timeoutMs', 'NAV_QUIET_MS', 't0', 'op', 'data', 'commitBefore',
    `return (${m[1]});`)(waitMode, 20000, 250, T0, 'goto', { url: TARGET }, commitBefore);
}

test('wait="load" returns at the load event with a request still in flight; "settled" waits it out', async () => {
  // The description promises the load event only. The network digest still reports the
  // request, so returning here hides nothing.
  for (const [mode, at] of [['load', T0], ['dom', T0], ['settled', T0 + 2500]]) {
    const m = loadSettle();
    loadedAt(m);
    m.setNet({ inflight: 1, lastActivity: T0 });
    m.setLog([{ id: 'x', type: 'xmlhttprequest', url: 'https://example.org/api', start: T0 }]);
    const r = await m.waitForSettle(7, navSettleOpts(mode));
    assert.equal(r.settled, true, mode);
    assert.equal(m.now(), at, mode);
  }
});

// Right after tabs.update, Firefox can still report the page being left as "complete", and
// its commit sits inside the 100 ms allowance. Only a commit newer than the one the tab had
// when the navigation was issued is the navigation's own.
const BEFORE = 'https://example.org/before';

test('a navigate issued just after a load does not settle on the page it is leaving', async () => {
  for (const mode of ['load', 'dom', 'settled']) {
    const m = loadSettle();
    m.setNav({ lastCommitted: T0 - 30, lastCommittedUrl: BEFORE });
    m.setTab({ url: BEFORE, title: 'Before', status: 'complete' });
    let ticks = 0;
    m.onTick(() => {
      if (++ticks === 2) {
        m.setNav({ lastCommitted: m.now(), lastCommittedUrl: TARGET });
        m.setTab({ url: TARGET, title: 'Example Domain', status: 'complete' });
      }
    });
    const r = await m.waitForSettle(7, navSettleOpts(mode, T0 - 30));
    assert.equal(r.settled, true, mode);
    assert.equal(r.url, TARGET, `${mode}: settled on the page the navigation was leaving`);
  }
});

test('without a newer commit that navigation times out instead of settling on the old page', async () => {
  const m = loadSettle();
  m.setNav({ lastCommitted: T0 - 30, lastCommittedUrl: BEFORE });
  m.setTab({ url: BEFORE, title: 'Before', status: 'complete' });
  const r = await m.waitForSettle(7, { ...navSettleOpts('load', T0 - 30), timeoutMs: 1000 });
  assert.equal(r.settled, false);
  assert.equal(r.timedOut, true);
});

test('the navigate case reads the tab\'s commit before it navigates and hands it to the settle', () => {
  const src = BG_SRC.split('\r\n').join('\n');
  const read = src.indexOf('const commitBefore = (TAB_NAV.get(navTabId) || {}).lastCommitted;');
  const go = src.indexOf("await browser.tabs.update(navTabId, { url: data.url });", read);
  assert.ok(read > 0 && go > read, 'read before tabs.update, or the new commit could be taken for the old');
  assert.equal(navSettleOpts('load', T0 - 30).priorCommitAt, T0 - 30);
});

// The same harness with timers that fire only when a test says so, so a wake-up can be
// shown to end a sleep before its timer.
function loadSettleManual() {
  const start = BG_SRC.indexOf('async function waitForSettle(tabId, opts = {}) {');
  const end = BG_SRC.indexOf('// Firefox answers a failed connection', start);
  const types = BG_SRC.indexOf('const QUIET_IGNORED_TYPES = ');
  assert.ok(start >= 0 && end > start && types >= 0, 'waitForSettle not found in background.js');
  // eslint-disable-next-line no-new-func
  return new Function(`
    const TAB_NAV = new Map();
    const TAB_NET = new Map();
    const TAB_REQUESTS = new Map();
    let clock = ${T0};
    const Date = { now: () => clock };
    const timers = [];
    const setTimeout = (fn, ms) => { timers.push({ fn, ms }); };
    let tab = { url: TARGET_URL, title: '', status: 'loading' };
    const browser = { tabs: { get: async () => tab } };
    ${BG_SRC.slice(types, BG_SRC.indexOf('\n', types))}
    ${BG_SRC.slice(start, end)}
    return {
      waitForSettle, wakeSettle, timers,
      setNav: (nav) => TAB_NAV.set(7, nav),
      setTab: (t) => { tab = t; },
      sleepers: () => (SETTLE_SLEEPERS.get(7) || new Set()).size,
      now: () => clock
    };
  `.replace('TARGET_URL', JSON.stringify(TARGET)))();
}

const flush = () => new Promise(r => setImmediate(r));

test('a commit and a finished load end the sleep at once', async () => {
  const m = loadSettleManual();
  const p = m.waitForSettle(7, { ...LOAD, ignoreBlankCommit: true });
  await flush();
  assert.equal(m.sleepers(), 1, 'it is asleep, not polling');
  assert.equal(m.timers.length, 1);
  assert.ok(m.timers[0].ms <= 1000, 'a missed event costs at most the recheck interval');
  m.setNav({ lastCommitted: T0 + 1, lastCommittedUrl: TARGET });
  m.setTab({ url: TARGET, title: 'Example Domain', status: 'complete' });
  m.wakeSettle(7);
  const r = await p;
  assert.equal(r.settled, true);
  assert.equal(m.now(), T0, 'no timer ran: the wake-up alone ended the wait');
  assert.equal(m.sleepers(), 0, 'nothing is left registered');
});

test('a wake-up for another tab does not end the sleep', async () => {
  const m = loadSettleManual();
  m.waitForSettle(7, LOAD);
  await flush();
  m.wakeSettle(8);
  await flush();
  assert.equal(m.sleepers(), 1);
});

test('navigate waits NAV_QUIET_MS (250 ms) for "settled" and none for "load" or "dom"', () => {
  const src = BG_SRC.split('\r\n').join('\n');
  assert.match(src, /const NAV_QUIET_MS = 250;/);
  assert.match(src, /quietMs: waitMode === 'settled' \? NAV_QUIET_MS : 0,/);
  assert.match(src, /const waitMode = \(data\.wait === 'load' \|\| data\.wait === 'dom'\) \? data\.wait : 'settled';/);
});

test('the navigate case waits for an echo\'s completion and reports the events with a net error', () => {
  const src = BG_SRC.split('\r\n').join('\n');
  assert.match(src, /if \(settle\.settled && abortAwaitingCompletion\(TAB_NAV\.get\(navTabId\) \|\| \{\}, t0\)\) \{\n\s+await waitForTabCondition\(navTabId, ABORT_ECHO_GRACE_MS,/);
  assert.match(src, /result\.netError = humanizeNetError\(rawErr\);\n\s+result\.navEvents = navEventsSince\(navState, t0\);/);
  assert.match(src, /browser\.webNavigation\.onCompleted\.addListener\(d => \{\n\s+if \(d\.frameId !== 0\) return;\n\s+noteNavEvent\(d, 'done'\);\n\s+wakeSettle\(d\.tabId\);/);
  assert.match(src, /browser\.tabs\.onUpdated\.addListener\(\(tabId, changeInfo\) => \{\n\s+if \(changeInfo && \(changeInfo\.status \|\| changeInfo\.url\)\) wakeSettle\(tabId\);/);
});

test('a goto with no url is refused before the proxy is applied', () => {
  const src = BG_SRC.split('\r\n').join('\n');
  const refuse = src.indexOf("if (op === 'goto' && !data.url) { sendError(id, 'INVALID_PARAMETER', 'url is required for goto'); return; }");
  const route = src.indexOf('const perr = applyTabProxy(navTabId, data.proxy);');
  assert.ok(refuse > 0 && route > 0, 'both statements are in the navigate case');
  assert.ok(refuse < route, 'the refusal comes first, so a refused call leaves the routing alone');
});

// ---- wait="dom": the load event, then a DOM that stopped changing ------------------------
// chrome-devtools-mcp's rule after its load event: 100 ms with no mutation record, bounded
// at 3 s. The background keeps the clock (a background tab throttles its own timers), and
// the page only records when its last mutation happened.

function loadDomWait() {
  const start = BG_SRC.indexOf('// ---- wait="dom": the DOM stops changing');
  const end = BG_SRC.indexOf('// ---- navigation landing diagnostics', start);
  assert.ok(start >= 0 && end > start, 'the wait="dom" block was not found in background.js');
  // eslint-disable-next-line no-new-func
  return new Function(`
    let clock = ${T0};
    const Date = { now: () => clock };
    const setTimeout = (fn, ms) => { clock += Math.max(0, ms); Promise.resolve().then(fn); };
    let lastMutation = ${T0};
    let mutating = false;
    let answer = null;
    let throws = null;
    const calls = [];
    let armListener = null;
    // Answers as the injected script does: the probe arguments are the tail of the code.
    const reply = (stop, stableMs, notBefore) => {
      const sinceLast = clock - lastMutation;
      const stable = stableMs != null && sinceLast >= stableMs && clock >= notBefore;
      return { sinceLast, records: 0, stable };
    };
    const browser = {
      webNavigation: { onCommitted: { addListener: (fn) => { armListener = fn; } } },
      tabs: { executeScript: async (tabId, details) => {
        calls.push({ tabId, details, args: JSON.parse('[' + details.code.slice(details.code.lastIndexOf('})(') + 3, -1) + ']') });
        if (throws) throw new Error(throws);
        if (answer) return [answer];
        if (mutating) lastMutation = clock - 10;
        const a = calls[calls.length - 1].args;
        return [reply(a[0], a[1], a[2])];
      } }
    };
    ${BG_SRC.slice(start, end)}
    return {
      waitForStableDom, armDomWatch, DOM_ARMED, DOM_ARM_MS, DOM_STABLE_MS, DOM_STABLE_MAX_MS,
      DOM_POST_LOAD_MIN_MS, DOM_WATCH_CODE, calls, armListener: () => armListener,
      keepMutating: () => { mutating = true; },
      quietSince: (msAgo) => { lastMutation = clock - msAgo; },
      answer: (a) => { answer = a; },
      throwing: (msg) => { throws = msg; },
      now: () => clock
    };
  `)();
}

test('a page that stops changing is stable after 100 ms, and the observer is taken down', async () => {
  const m = loadDomWait();
  const r = await m.waitForStableDom(7, m.DOM_STABLE_MAX_MS);
  assert.equal(m.DOM_STABLE_MS, 100);
  assert.equal(m.DOM_STABLE_MAX_MS, 3000);
  assert.equal(r.stable, true);
  assert.equal(r.ms, 100, 'the first quiet 100 ms ends the wait');
  assert.equal(m.calls.length, 2, 'one probe to start the wait and one that also stops the observer');
  assert.equal(m.calls[1].args[0], false, 'the stable probe takes the observer down itself, no stop probe follows');
});

test('a page still changing at 3 s returns anyway and says it is not stable', async () => {
  const m = loadDomWait();
  m.keepMutating();
  const r = await m.waitForStableDom(7, m.DOM_STABLE_MAX_MS);
  assert.equal(r.stable, false);
  assert.ok(r.ms >= 3000 && r.ms < 3200, `bounded at 3 s, took ${r.ms}`);
  assert.equal(m.calls[m.calls.length - 1].args[0], true, 'running out of budget stops the observer');
});

test('a smaller budget bounds the wait', async () => {
  const m = loadDomWait();
  m.keepMutating();
  const r = await m.waitForStableDom(7, 250);
  assert.equal(r.stable, false);
  assert.ok(r.ms <= 300, `took ${r.ms}`);
});

test('quiet that began before the load event counts toward the 100 ms', async () => {
  const m = loadDomWait();
  m.quietSince(30);
  const r = await m.waitForStableDom(7, m.DOM_STABLE_MAX_MS);
  assert.equal(r.stable, true);
  assert.equal(r.ms, 70, '30 ms of quiet were already behind the load event');
});

test('a DOM quiet for longer than 100 ms at the load event still gets the post-load minimum', async () => {
  const m = loadDomWait();
  m.quietSince(900);
  const r = await m.waitForStableDom(7, m.DOM_STABLE_MAX_MS);
  assert.equal(m.DOM_POST_LOAD_MIN_MS, 50);
  assert.equal(r.stable, true);
  assert.equal(r.ms, 50, 'a fetch the page starts at load gets 50 ms to answer');
  assert.equal(m.calls.length, 2);
});

test('the commit of an armed tab starts the observer early, once, for the top frame only', async () => {
  const m = loadDomWait();
  assert.equal(typeof m.armListener(), 'function', 'armDomWatch is registered on webNavigation.onCommitted');
  m.armDomWatch({ tabId: 7, frameId: 0 });
  await Promise.resolve();
  assert.equal(m.calls.length, 0, 'a tab nobody armed is left alone');
  m.DOM_ARMED.set(7, m.now() + m.DOM_ARM_MS);
  m.armDomWatch({ tabId: 7, frameId: 3 });
  await Promise.resolve();
  assert.equal(m.calls.length, 0, 'a subframe commit does not start it');
  m.armDomWatch({ tabId: 7, frameId: 0 });
  await Promise.resolve();
  assert.equal(m.calls.length, 1);
  assert.deepEqual(m.calls[0].args, [false, null, 0, true]);
  m.DOM_ARMED.set(8, m.now() - 1);
  m.armDomWatch({ tabId: 8, frameId: 0 });
  await Promise.resolve();
  assert.equal(m.calls.length, 1, 'a lapsed arming starts nothing');
  assert.equal(m.DOM_ARMED.has(8), false, 'and is dropped');
});

test('a page that cannot be observed is not called stable', async () => {
  const m = loadDomWait();
  m.answer({ sinceLast: 0, records: 0, error: 'MutationObserver is not defined' });
  const r = await m.waitForStableDom(7, 3000);
  assert.equal(r.stable, false);
  assert.match(r.error, /MutationObserver/);
  const m2 = loadDomWait();
  m2.throwing('No tab with id: 7');
  const r2 = await m2.waitForStableDom(7, 3000);
  assert.equal(r2.stable, false);
  assert.match(r2.error, /No tab/);
});

test('the in-page probe records the last mutation and stops when told', () => {
  const m = loadDomWait();
  const dom = new JSDOM('<!DOCTYPE html><body><div id="app"></div></body>', { runScripts: 'outside-only' });
  const first = dom.window.eval(m.DOM_WATCH_CODE + '(false)');
  assert.equal(first.error, undefined);
  assert.ok(first.sinceLast >= 0 && first.sinceLast < 50, 'install time counts as the last change');
  const watch = dom.window.__browserplugDomWatch;
  assert.ok(watch && watch.obs, 'the observer stays installed between probes');
  dom.window.eval(m.DOM_WATCH_CODE + '(true)');
  assert.equal(dom.window.__browserplugDomWatch, null, 'stop removes it');
});

test('the in-page probe removes itself once the DOM has been quiet long enough', () => {
  const m = loadDomWait();
  const dom = new JSDOM('<!DOCTYPE html><body></body>', { runScripts: 'outside-only' });
  const early = dom.window.eval(m.DOM_WATCH_CODE + '(false, null, 0, true)');
  assert.equal(early.error, undefined);
  assert.ok(dom.window.__browserplugDomWatch.obs, 'the early call installs and leaves it running');
  const busy = dom.window.eval(m.DOM_WATCH_CODE + '(false, 100, 0, false)');
  assert.equal(busy.stable, false, 'a fresh install is 0 ms quiet');
  assert.ok(dom.window.__browserplugDomWatch, 'not stable: the observer keeps running');
  dom.window.__browserplugDomWatch.last -= 500;
  const notYet = dom.window.eval(m.DOM_WATCH_CODE + '(false, 100, ' + (Date.now() + 60000) + ', false)');
  assert.equal(notYet.stable, false, 'quiet is not enough before notBefore');
  assert.ok(dom.window.__browserplugDomWatch);
  const done = dom.window.eval(m.DOM_WATCH_CODE + '(false, 100, 0, false)');
  assert.equal(done.stable, true);
  assert.equal(dom.window.__browserplugDomWatch, null, 'the stable probe is also the stop');
});

test('the early call installs nothing in a document with no root element yet', () => {
  const m = loadDomWait();
  const dom = new JSDOM('<!DOCTYPE html><body></body>', { runScripts: 'outside-only' });
  dom.window.document.removeChild(dom.window.document.documentElement);
  const out = dom.window.eval(m.DOM_WATCH_CODE + '(false, null, 0, true)');
  assert.equal(out, null);
  assert.equal(dom.window.__browserplugDomWatch, undefined);
});

test('navigate runs the DOM wait only for wait="dom", after a settle, within the timeout', () => {
  const src = BG_SRC.split('\r\n').join('\n');
  assert.match(src, /if \(waitMode === 'dom' && settle\.settled\) \{\n\s+const dom = await waitForStableDom\(navTabId,\n\s+Math\.min\(DOM_STABLE_MAX_MS, Math\.max\(0, timeoutMs - settle\.waitedMs\)\)\);/);
  assert.match(src, /result\.domStable = dom\.stable;/);
  assert.match(src, /result\.waitMode = waitMode;/);
  // armed before the navigation is issued, disarmed once the settle returns
  const armAt = src.indexOf("if (data.wait === 'dom') DOM_ARMED.set(navTabId, t0 + DOM_ARM_MS);");
  const navAt = src.indexOf("await browser.tabs.update(navTabId, { url: data.url });", armAt);
  assert.ok(armAt > 0 && navAt > armAt, 'the tab is armed before the navigation starts');
  assert.match(src, /result\.waitMode = waitMode;\n\s+DOM_ARMED\.delete\(navTabId\);\n\s+if \(waitMode === 'dom' && !settle\.settled\) \{\n\s+probeDomWatch\(navTabId, true\)/);
});
