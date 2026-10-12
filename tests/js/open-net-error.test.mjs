/*
 * failedMainFrameError and waitForCommit: how tabs.create / windows.create learn that
 * Firefox's error page is not the page that was asked for.
 *
 * Firefox answers a refused or reset connection by COMMITTING its own error page at the
 * requested URL, and onCommitted wipes webNavigation's lastNavError, so a commit alone
 * reported the open a success and the agent was told "Opened tab" for a page that never
 * loaded. The request log still holds the error. webRequest and webNavigation are separate
 * event streams, though, so the commit can be seen before onErrorOccurred lands; the
 * waitForCommit tests below drive exactly that order. Chromium needs nothing: its open
 * already reports these (measured by tests/integration/test_open_net_error.py, which runs
 * on both engines).
 *
 * Like navdiag.test.mjs, the functions are sliced out of background.js and driven with a
 * stubbed TAB_REQUESTS, TAB_NAV and clock. Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

// The gecko background.js is CRLF; the slice markers below are LF.
const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8')
  .split('\r\n').join('\n');

function sliceFunction(marker) {
  const start = BG_SRC.indexOf(marker);
  assert.ok(start >= 0, `${marker} not found in background.js`);
  return BG_SRC.slice(start, BG_SRC.indexOf('\n}\n', start) + 3);
}

// The webRequest listeners that fill the request log, so a test can replay the events
// Firefox fires instead of hand-writing the entry they would leave behind.
function listenerBlock() {
  const start = BG_SRC.indexOf('browser.webRequest.onBeforeRequest.addListener(d => {');
  const end = BG_SRC.indexOf('function markNavCommitted(d)', start);
  assert.ok(start >= 0 && end > start, 'the webRequest listener block was not found in background.js');
  return BG_SRC.slice(start, end);
}

// The request types the listeners keep off the network-quiet clock.
function quietIgnoredTypes() {
  const start = BG_SRC.indexOf('const QUIET_IGNORED_TYPES = ');
  assert.ok(start >= 0, 'QUIET_IGNORED_TYPES not found in background.js');
  return BG_SRC.slice(start, BG_SRC.indexOf('\n', start) + 1);
}

// The nsresult table that names webNavigation's "Error code N" strings.
function nsresultNames() {
  const start = BG_SRC.indexOf('const NSRESULT_NAMES = {');
  assert.ok(start >= 0, 'NSRESULT_NAMES not found in background.js');
  return BG_SRC.slice(start, BG_SRC.indexOf('\n};\n', start) + 3);
}

function load() {
  const start = BG_SRC.indexOf('function failedMainFrameError(');
  const end = BG_SRC.indexOf('// Digest of network activity in a tab since t0', start);
  assert.ok(start >= 0 && end > start, 'failedMainFrameError / waitForCommit not found in background.js');
  // eslint-disable-next-line no-new-func
  return new Function(`
    const TAB_REQUESTS = new Map();
    const TAB_NAV = new Map();
    const TAB_NET = new Map();
    const REQ_LOG_MAX = 300;
    const listeners = {};
    const webRequest = new Proxy({}, {
      get: (_, name) => ({ addListener: (fn) => { listeners[name] = fn; } })
    });
    // A fake clock: every setTimeout advances it and runs the tick hook first, so a test
    // can change the logs "while" waitForCommit polls.
    let clock = 1000000;
    let onTick = () => {};
    const Date = { now: () => clock };
    const setTimeout = (fn, ms) => { clock += ms; onTick(clock); Promise.resolve().then(fn); };
    let tab = { url: 'http://127.0.0.1:1/', title: 'Problem loading page', status: 'complete' };
    const browser = { tabs: { get: async () => tab }, webRequest };
    // The listeners wake a sleeping waitForSettle, which is not part of this harness.
    const wakeSettle = () => {};
    ${quietIgnoredTypes()}
    ${nsresultNames()}
    ${listenerBlock()}
    ${sliceFunction('function latestMainFrameRequest(')}
    ${sliceFunction('function isNavAbort(')}
    ${sliceFunction('function navUrlKey(')}
    ${sliceFunction('function abortOfReplacedLoad(')}
    ${sliceFunction('function abortOfCompletedLoad(')}
    ${sliceFunction('function abortAwaitingCompletion(')}
    ${BG_SRC.slice(start, end)}
    return {
      failedMainFrameError, waitForCommit,
      setLog: (log) => TAB_REQUESTS.set(7, log),
      log: () => TAB_REQUESTS.get(7),
      fire: (event, d) => listeners[event]({ tabId: 7, ...d }),
      setNav: (nav) => TAB_NAV.set(7, nav),
      nav: () => TAB_NAV.get(7),
      setTab: (t) => { tab = t; },
      onTick: (fn) => { onTick = fn; },
      now: () => clock
    };
  `)();
}

const T0 = 1_000_000;
const req = (over) => ({ type: 'main_frame', method: 'GET', start: T0 + 10, ...over });

test('the latest main-frame request ending in a net error is a failed open', () => {
  const m = load();
  m.setLog([req({ error: 'NS_ERROR_CONNECTION_REFUSED' })]);
  assert.equal(m.failedMainFrameError(7, T0), 'NS_ERROR_CONNECTION_REFUSED');
});

test('an unresolvable name is reported too', () => {
  const m = load();
  m.setLog([req({ error: 'NS_ERROR_UNKNOWN_HOST' })]);
  assert.equal(m.failedMainFrameError(7, T0), 'NS_ERROR_UNKNOWN_HOST');
});

test('an abort is a newer navigation replacing the request, not a failed page', () => {
  const m = load();
  m.setLog([req({ error: 'NS_BINDING_ABORTED' })]);
  assert.equal(m.failedMainFrameError(7, T0), null);
});

test('an HTTP error status is a page that loaded, so it is not a net error', () => {
  const m = load();
  m.setLog([req({ status: 404, end: T0 + 50 }), req({ status: 503, end: T0 + 90 })]);
  assert.equal(m.failedMainFrameError(7, T0), null);
});

test('a response that has started is not a failure, even before it completes', () => {
  const m = load();
  m.setLog([req({ responded: true })]);
  assert.equal(m.failedMainFrameError(7, T0), null);
});

test('an error followed by a later request that succeeded is not a failure', () => {
  const m = load();
  m.setLog([req({ error: 'NS_ERROR_NET_RESET', start: T0 + 10 }),
            req({ status: 200, end: T0 + 450, start: T0 + 400 })]);
  assert.equal(m.failedMainFrameError(7, T0), null);
});

test('a latest request with neither a response nor an error is undecided', () => {
  const m = load();
  m.setLog([req({ error: 'NS_ERROR_NET_RESET', start: T0 + 10 }), req({ start: T0 + 400 })]);
  assert.equal(m.failedMainFrameError(7, T0), undefined);
});

test('sub-resource errors and requests from before the open are ignored', () => {
  const m = load();
  m.setLog([req({ type: 'image', error: 'NS_ERROR_CONNECTION_REFUSED' }),
            req({ error: 'NS_ERROR_CONNECTION_REFUSED', start: T0 - 5000 })]);
  assert.equal(m.failedMainFrameError(7, T0), null);
});

test('no log for the tab is not a failure (about:, data: and file: have none)', () => {
  const m = load();
  assert.equal(m.failedMainFrameError(999, T0), null);
});

// ---- waitForCommit: the commit and the request's outcome arrive in either order -------

test('waitForCommit: the error page commits before onErrorOccurred, and it is still a failure', async () => {
  const m = load();
  const entry = req({});
  m.setLog([entry]);
  m.setNav({ lastCommitted: m.now() });
  let ticks = 0;
  m.onTick(() => { if (++ticks === 2) entry.error = 'NS_ERROR_CONNECTION_REFUSED'; });
  const c = await m.waitForCommit(7, T0, 8000);
  assert.equal(c.committed, false, 'the error page was reported as a loaded tab');
  assert.equal(c.navError, 'NS_ERROR_CONNECTION_REFUSED');
});

test('waitForCommit: a commit whose request already responded returns at once', async () => {
  const m = load();
  m.setLog([req({ responded: true })]);
  m.setNav({ lastCommitted: m.now() });
  const before = m.now();
  const c = await m.waitForCommit(7, T0, 8000);
  assert.equal(c.committed, true);
  assert.equal(m.now(), before, 'a page that answered waited for nothing');
});

test('waitForCommit: an undecided request stops holding the commit after the grace', async () => {
  const m = load();
  m.setLog([req({})]);
  m.setNav({ lastCommitted: m.now() });
  const before = m.now();
  const c = await m.waitForCommit(7, T0, 8000);
  assert.equal(c.committed, true);
  assert.ok(m.now() - before >= 1000 && m.now() - before < 1300, `waited ${m.now() - before} ms`);
});

test('waitForCommit: a budget that ends inside the grace still reports the commit', async () => {
  const m = load();
  m.setLog([req({})]);
  m.setNav({ lastCommitted: m.now() });
  const c = await m.waitForCommit(7, T0, 300);
  assert.equal(c.committed, true);
  assert.equal(c.timedOut, undefined);
});

// ---- the events Firefox really fires, replayed through the listeners ------------------

const main = (requestId, over) => ({ requestId, url: 'http://a.test/', method: 'GET',
  type: 'main_frame', timeStamp: T0 + 10, ...over });
const hop = (requestId, over) => ({ requestId, redirectUrl: 'http://127.0.0.1:18939/',
  statusCode: 302, timeStamp: T0 + 20, ...over });

test('a redirect hop that keeps its request id (Firefox before 152) does not decide the request', () => {
  const m = load();
  m.fire('onBeforeRequest', main('r1'));
  m.fire('onBeforeRedirect', hop('r1'));
  assert.equal(m.failedMainFrameError(7, T0), undefined, 'the 302 hop was taken for the outcome');
  m.fire('onErrorOccurred', { requestId: 'r1', error: 'NS_ERROR_CONNECTION_REFUSED', timeStamp: T0 + 30 });
  assert.equal(m.failedMainFrameError(7, T0), 'NS_ERROR_CONNECTION_REFUSED');
});

test('a redirect hop that keeps its request id and then loads is not a failure', () => {
  const m = load();
  m.fire('onBeforeRequest', main('r1'));
  m.fire('onBeforeRedirect', hop('r1'));
  m.fire('onResponseStarted', { requestId: 'r1', type: 'main_frame', ip: '127.0.0.1' });
  assert.equal(m.failedMainFrameError(7, T0), null);
});

test('a redirect that replaces the channel (Firefox 152+, seen on 158) is decided by the continuation', () => {
  const m = load();
  m.fire('onBeforeRequest', main('r1'));
  m.fire('onBeforeRedirect', hop('r1'));
  m.fire('onBeforeRequest', main('r2', { url: 'http://127.0.0.1:18939/', timeStamp: T0 + 25 }));
  assert.equal(m.failedMainFrameError(7, T0), undefined);
  m.fire('onErrorOccurred', { requestId: 'r2', error: 'NS_ERROR_CONNECTION_REFUSED', timeStamp: T0 + 30 });
  assert.equal(m.failedMainFrameError(7, T0), 'NS_ERROR_CONNECTION_REFUSED');
});

test('waitForCommit: an error page after a same-id redirect is still a failure', async () => {
  const m = load();
  m.fire('onBeforeRequest', main('r1'));
  m.fire('onBeforeRedirect', hop('r1'));
  m.setNav({ lastCommitted: m.now() });
  let ticks = 0;
  m.onTick(() => {
    if (++ticks === 2) {
      m.fire('onErrorOccurred', { requestId: 'r1', error: 'NS_ERROR_CONNECTION_REFUSED', timeStamp: T0 + 300 });
    }
  });
  const c = await m.waitForCommit(7, T0, 8000);
  assert.equal(c.committed, false, 'Firefox\'s error page was reported as a loaded tab');
  assert.equal(c.navError, 'NS_ERROR_CONNECTION_REFUSED');
});

// Measured on Firefox 158 against a local server: headers and a body, then a clean close
// short of Content-Length, renders what arrived; the same body followed by a reset gets
// Firefox's "The connection was reset" page.
test('a body cut short by a clean close is the page Firefox shows, not a failed open', () => {
  const m = load();
  m.fire('onBeforeRequest', main('r1'));
  m.fire('onResponseStarted', { requestId: 'r1', type: 'main_frame', ip: '127.0.0.1' });
  m.fire('onErrorOccurred', { requestId: 'r1', error: 'NS_ERROR_NET_PARTIAL_TRANSFER', timeStamp: T0 + 40 });
  assert.equal(m.failedMainFrameError(7, T0), null);
});

test('a reset after the response started is Firefox\'s error page, so it is still a failure', () => {
  const m = load();
  m.fire('onBeforeRequest', main('r1'));
  m.fire('onResponseStarted', { requestId: 'r1', type: 'main_frame', ip: '127.0.0.1' });
  m.fire('onErrorOccurred', { requestId: 'r1', error: 'NS_ERROR_NET_RESET', timeStamp: T0 + 40 });
  assert.equal(m.failedMainFrameError(7, T0), 'NS_ERROR_NET_RESET');
});

test('a partial transfer with no response at all is a failure', () => {
  const m = load();
  m.setLog([req({ error: 'NS_ERROR_NET_PARTIAL_TRANSFER' })]);
  assert.equal(m.failedMainFrameError(7, T0), 'NS_ERROR_NET_PARTIAL_TRANSFER');
});

test('waitForCommit: an error that lands as the budget runs out is still reported', async () => {
  const m = load();
  const entry = req({});
  m.setLog([entry]);
  m.setNav({ lastCommitted: m.now() });
  let ticks = 0;
  // A 300 ms budget polls at 0, 100 and 200 ms; the third sleep ends the loop at 300.
  m.onTick(() => { if (++ticks === 3) entry.error = 'NS_ERROR_CONNECTION_REFUSED'; });
  const c = await m.waitForCommit(7, T0, 300);
  assert.equal(c.committed, false, 'the error that arrived after the last poll was ignored');
  assert.equal(c.navError, 'NS_ERROR_CONNECTION_REFUSED');
});

test('a sub-resource response is not marked responded; only a main frame is', () => {
  const m = load();
  m.fire('onBeforeRequest', main('img', { type: 'image', url: 'http://a.test/x.png' }));
  m.fire('onBeforeRequest', main('doc'));
  m.fire('onResponseStarted', { requestId: 'img', type: 'image' });
  m.fire('onResponseStarted', { requestId: 'doc', type: 'main_frame' });
  const [img, doc] = m.log();
  assert.equal(img.responded, undefined);
  assert.equal(doc.responded, true);
});

// Firefox also reports the cut-short body through webNavigation.onErrorOccurred, AFTER it
// committed the part that arrived (seen in the Firefox integration run), so waitForCommit
// meets it before it reaches the request log.
const PARTIAL_NAV_ERROR = 'Error code 2152398924';   // NS_ERROR_NET_PARTIAL_TRANSFER

test('waitForCommit: a partial page reported through webNavigation after its commit is the open', async () => {
  const m = load();
  m.setTab({ url: 'http://a.test/', title: 'Partial page', status: 'complete' });
  m.fire('onBeforeRequest', main('r1'));
  m.fire('onResponseStarted', { requestId: 'r1', type: 'main_frame', ip: '127.0.0.1' });
  m.fire('onErrorOccurred', { requestId: 'r1', error: 'NS_ERROR_NET_PARTIAL_TRANSFER', timeStamp: T0 + 40 });
  m.setNav({ lastCommitted: m.now(), lastNavError: PARTIAL_NAV_ERROR, lastNavErrorAt: m.now() });
  const c = await m.waitForCommit(7, T0, 8000);
  assert.equal(c.committed, true, `reported ${c.navError} for a tab showing the page`);
});

test('waitForCommit: headers and then a clean close leave about:blank, and that is a failure', async () => {
  const m = load();
  m.setTab({ url: 'about:blank', title: '127.0.0.1/headers-only', status: 'complete' });
  m.fire('onBeforeRequest', main('r1'));
  m.fire('onResponseStarted', { requestId: 'r1', type: 'main_frame', ip: '127.0.0.1' });
  m.fire('onErrorOccurred', { requestId: 'r1', error: 'NS_ERROR_NET_PARTIAL_TRANSFER', timeStamp: T0 + 40 });
  m.setNav({ lastNavError: PARTIAL_NAV_ERROR, lastNavErrorAt: m.now() });
  const c = await m.waitForCommit(7, T0, 8000);
  assert.equal(c.committed, false);
  assert.equal(c.navError, PARTIAL_NAV_ERROR);
});

test('waitForCommit: a webNavigation partial transfer with no response is a failure', async () => {
  const m = load();
  m.setTab({ url: 'http://a.test/', title: 'a.test', status: 'complete' });
  m.fire('onBeforeRequest', main('r1'));
  m.fire('onErrorOccurred', { requestId: 'r1', error: 'NS_ERROR_NET_PARTIAL_TRANSFER', timeStamp: T0 + 40 });
  m.setNav({ lastCommitted: m.now(), lastNavError: PARTIAL_NAV_ERROR, lastNavErrorAt: m.now() });
  const c = await m.waitForCommit(7, T0, 8000);
  assert.equal(c.committed, false);
});

// ---- a proxied open replaces its own about:blank -------------------------------------
// tabs.create with a proxy opens about:blank, installs the routing, then navigates. The
// about:blank load can still be running, and its abort can reach webNavigation while
// waitForCommit polls. That abort names about:blank, not the URL the open asked for.
const NAV_ABORT = 'Error code 2152398850';            // NS_BINDING_ABORTED

test('waitForCommit: the abort of the about:blank a proxied open replaced does not fail the open', async () => {
  const m = load();
  m.setTab({ url: 'about:blank', title: 'New Tab', status: 'complete' });
  m.fire('onBeforeRequest', main('r1'));
  m.setNav({ lastCommitted: T0 + 5, lastCommittedUrl: 'about:blank', lastNavError: NAV_ABORT,
             lastNavErrorUrl: 'about:blank', lastNavErrorAt: T0 + 20 });
  let ticks = 0;
  m.onTick(() => {
    if (++ticks === 2) {
      m.fire('onResponseStarted', { requestId: 'r1', type: 'main_frame', ip: '127.0.0.1' });
      m.setNav({ lastCommitted: m.now(), lastCommittedUrl: 'http://a.test/' });
      m.setTab({ url: 'http://a.test/', title: 'a.test', status: 'loading' });
    }
  });
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, true, `the replaced load's ${c.navError} failed the open`);
});

test('waitForCommit: an abort of the requested URL itself still fails the open', async () => {
  const m = load();
  m.setTab({ url: 'about:blank', title: 'New Tab', status: 'complete' });
  m.setNav({ lastNavError: NAV_ABORT, lastNavErrorUrl: 'http://a.test/', lastNavErrorAt: T0 + 20 });
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, false);
  assert.equal(c.navError, NAV_ABORT);
});

test('waitForCommit: an abort at the URL its request was redirected to is the open\'s own', async () => {
  const m = load();
  m.setTab({ url: 'about:blank', title: 'New Tab', status: 'complete' });
  m.fire('onBeforeRequest', main('r1'));
  m.fire('onBeforeRedirect', hop('r1'));
  m.setNav({ lastNavError: NAV_ABORT, lastNavErrorUrl: 'http://127.0.0.1:18939/',
             lastNavErrorAt: T0 + 30 });
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, false);
  assert.equal(c.navError, NAV_ABORT);
});

// ---- the abort the process a tab leaves reports for the same URL ---------------------
// A proxied open is a blank tab navigated at once, the pattern that makes Firefox report
// the navigation from two content processes (navdiag.test.mjs has the mechanism). The
// process the tab leaves aborts its own attempt at the requested URL, and that abort can
// land after the new process committed it. The open holds its answer for the grace while
// that document can still report its load complete.

const committedWithEcho = (m) => {
  m.fire('onBeforeRequest', main('r1'));
  m.fire('onResponseStarted', { requestId: 'r1', type: 'main_frame', ip: '127.0.0.1' });
  m.setTab({ url: 'http://a.test/', title: 'a.test', status: 'loading' });
  m.setNav({ lastCommitted: T0 + 30, lastCommittedUrl: 'http://a.test/',
             lastNavError: NAV_ABORT, lastNavErrorUrl: 'http://a.test/', lastNavErrorAt: T0 + 40,
             events: [{ ev: 'commit', url: 'http://a.test/', at: T0 + 30 },
                      { ev: 'error', url: 'http://a.test/', at: T0 + 40, error: NAV_ABORT }] });
};

test('waitForCommit: the leaving process\'s abort of the committed URL waits for that load, which clears it', async () => {
  const m = load();
  committedWithEcho(m);
  let ticks = 0;
  m.onTick(() => {
    if (++ticks === 2) m.setNav({ ...m.nav(), events: [...m.nav().events,
      { ev: 'done', url: 'http://a.test/', at: m.now() }] });
  });
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, true, `the echo ${c.navError} failed an open whose page loaded`);
});

test('waitForCommit: with no completion inside the grace, the abort of the committed URL still fails the open', async () => {
  const m = load();
  committedWithEcho(m);
  const before = m.now();
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, false);
  assert.equal(c.navError, NAV_ABORT);
  assert.ok(m.now() - before >= 1000, 'it waited the grace before deciding');
  assert.ok(m.now() - before < 2000, 'and no longer');
});

test('waitForCommit: a budget that ends while the abort waits for its load reports the abort', async () => {
  const m = load();
  committedWithEcho(m);
  const c = await m.waitForCommit(7, T0, 300, 'http://a.test/');
  assert.equal(c.committed, false);
  assert.equal(c.navError, NAV_ABORT, 'an unanswered abort stands even when time runs out');
});

test('tabs.create and windows.create hand waitForCommit the URL they asked for', () => {
  assert.match(BG_SRC, /waitForCommit\(newTab\.id, t0commit, 8000, data\.url\)/);
  assert.match(BG_SRC, /waitForCommit\(wtab\.id, t0winCommit, 8000, data\.url\)/);
});

// ---- the same abort, delivered before the new process commits -------------------------
// Measured on Firefox Developer Edition 158 with a local SOCKS proxy (tabs open proxy=...):
// webNavigation reported the blank tab's attempt at the requested URL as aborted 95 to
// 119 ms in, the request kept running, the new process committed it 65 to 115 ms later and
// the page loaded. A proxied open that read the abort at its first poll reported "request
// aborted" with the tab on about:blank, in about half the runs. A request that really
// ends in an abort has an error in the request log, which the tests below keep failing.

const echoBeforeCommit = (m) => {
  m.setTab({ url: 'about:blank', title: 'New Tab', status: 'complete' });
  m.fire('onBeforeRequest', main('r1'));
  m.setNav({ lastCommitted: T0 + 5, lastCommittedUrl: 'about:blank', lastNavError: NAV_ABORT,
             lastNavErrorUrl: 'http://a.test/', lastNavErrorAt: T0 + 20 });
};
const commitsAt = (m, tick) => {
  let ticks = 0;
  m.onTick(() => {
    if (++ticks === tick) {
      m.fire('onResponseStarted', { requestId: 'r1', type: 'main_frame', ip: '127.0.0.1' });
      m.setNav({ lastCommitted: m.now(), lastCommittedUrl: 'http://a.test/' });
      m.setTab({ url: 'http://a.test/', title: 'a.test', status: 'loading' });
    }
  });
};

test('waitForCommit: an abort of the requested URL while its request still runs is not the end of the open', async () => {
  const m = load();
  echoBeforeCommit(m);
  commitsAt(m, 2);
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, true, `the early echo ${c.navError} failed an open whose page loaded`);
});

test('waitForCommit: a slow request (a Tor circuit) keeps the open waiting past the grace', async () => {
  const m = load();
  echoBeforeCommit(m);
  commitsAt(m, 40);   // about 4 s of 100 ms polls
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, true, `${c.navError || 'timed out'} after a request that was still running`);
});

test('waitForCommit: the same abort fails the open once its request ends in an abort', async () => {
  const m = load();
  echoBeforeCommit(m);
  let ticks = 0;
  m.onTick(() => {
    if (++ticks === 2) m.fire('onErrorOccurred', { requestId: 'r1', error: 'NS_BINDING_ABORTED', timeStamp: m.now() });
  });
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, false);
  assert.equal(c.navError, NAV_ABORT, 'a stopped load must still be reported');
});

test('waitForCommit: a request that ends clean but never commits stops holding the abort after the grace', async () => {
  const m = load();
  echoBeforeCommit(m);
  let ticks = 0;
  m.onTick(() => {
    if (++ticks === 2) m.fire('onCompleted', { requestId: 'r1', statusCode: 204, timeStamp: m.now() });
  });
  const before = m.now();
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, false);
  assert.equal(c.navError, NAV_ABORT);
  assert.ok(m.now() - before >= 1000 && m.now() - before < 2000, `waited ${m.now() - before} ms`);
});

test('waitForCommit: an abort naming a different URL than the running request is not set aside', async () => {
  const m = load();
  echoBeforeCommit(m);
  m.setNav({ ...m.nav(), lastNavErrorUrl: 'http://a.test/other' });
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/other');
  assert.equal(c.committed, false);
  assert.equal(c.navError, NAV_ABORT);
});

// ---- the request has not been reported yet: a commit with no main-frame request on file ----
// 2026-10-09: a 1.0.11 sweep opened an unresolvable .onion name on a loaded machine and was
// answered "Opened tab" with an empty request log. webRequest and webNavigation are separate
// streams, and the commit of Firefox's error page had been seen with no request logged.

test('a network page with no main-frame request on file is undecided, not loaded', () => {
  const m = load();
  assert.equal(m.failedMainFrameError(7, T0, 'http://a.test/'), undefined);
  assert.equal(m.failedMainFrameError(7, T0, 'https://a.test/'), undefined);
});

test('about:, data: and file: pages have no request by design, so none on file is not a failure', () => {
  const m = load();
  for (const u of ['about:blank', 'about:neterror', 'data:text/html,hi', 'file:///tmp/a.html', undefined]) {
    assert.equal(m.failedMainFrameError(7, T0, u), null, String(u));
  }
});

test('a failure webNavigation reported before the commit wiped lastNavError is still found', () => {
  const m = load();
  m.setNav({ lastCommitted: m.now(), events: [
    { ev: 'error', url: 'http://a.test/', at: T0 + 20, error: 'Error code 2152398878' },
    { ev: 'commit', url: 'http://a.test/', at: T0 + 30 }] });
  assert.equal(m.failedMainFrameError(7, T0, 'http://a.test/'), 'Error code 2152398878');
});

test('an abort, or an error from before the open began, is not the open\'s failure', () => {
  const m = load();
  m.setNav({ events: [
    { ev: 'error', url: 'http://a.test/', at: T0 - 5000, error: 'Error code 2152398878' },
    { ev: 'error', url: 'http://a.test/', at: T0 + 20, error: 'Error code 2152398850' }] });
  assert.equal(m.failedMainFrameError(7, T0, 'http://a.test/'), undefined);
});

test('a request on file still decides, whatever the event list holds', () => {
  const m = load();
  m.setLog([req({ responded: true })]);
  m.setNav({ events: [{ ev: 'error', url: 'http://a.test/', at: T0 + 20,
    error: 'Error code 2152398878' }] });
  assert.equal(m.failedMainFrameError(7, T0, 'http://a.test/'), null);
});

test('waitForCommit: a commit with no request logged and an earlier webNavigation error is a failure at once', async () => {
  const m = load();
  m.setNav({ lastCommitted: m.now(), events: [
    { ev: 'error', url: 'http://a.test/', at: T0 + 20, error: 'Error code 2152398878' },
    { ev: 'commit', url: 'http://a.test/', at: T0 + 30 }] });
  const before = m.now();
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, false);
  assert.equal(c.navError, 'Error code 2152398878');
  assert.equal(m.now(), before, 'nothing to wait for');
});

test('waitForCommit: a commit with no request logged waits for webRequest, and its error decides', async () => {
  const m = load();
  m.setNav({ lastCommitted: m.now() });
  m.setLog([]);
  let ticks = 0;
  m.onTick(() => {
    if (++ticks === 3) m.setLog([req({ error: 'NS_ERROR_UNKNOWN_HOST' })]);
  });
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, false, 'the error page was reported as a loaded tab');
  assert.equal(c.navError, 'NS_ERROR_UNKNOWN_HOST');
});

test('waitForCommit: a commit that webRequest never reports is a page after the grace', async () => {
  const m = load();
  m.setNav({ lastCommitted: m.now() });
  const before = m.now();
  const c = await m.waitForCommit(7, T0, 8000, 'http://a.test/');
  assert.equal(c.committed, true);
  assert.ok(m.now() - before >= 1000 && m.now() - before < 1300, `waited ${m.now() - before} ms`);
});

test('waitForCommit: an about:blank commit with no request is not held', async () => {
  const m = load();
  m.setTab({ url: 'about:neterror', title: 'x', status: 'complete' });
  m.setNav({ lastCommitted: m.now() });
  const before = m.now();
  const c = await m.waitForCommit(7, T0, 8000);
  assert.equal(c.committed, true);
  assert.equal(m.now(), before);
});

test('waitForCommit: no commit in the budget but the request ended in a net error is that error', async () => {
  const m = load();
  m.setTab({ url: 'about:blank', title: '', status: 'complete' });
  m.setNav({});
  m.setLog([req({ error: 'NS_ERROR_UNKNOWN_HOST', end: T0 + 6100 })]);
  const c = await m.waitForCommit(7, T0, 2000, 'http://a.test/');
  assert.equal(c.committed, false);
  assert.equal(c.navError, 'NS_ERROR_UNKNOWN_HOST');
  assert.ok(!c.timedOut);
});

test('waitForCommit: no commit and a request still running is a timeout, as before', async () => {
  const m = load();
  m.setTab({ url: 'about:blank', title: '', status: 'loading' });
  m.setNav({});
  m.setLog([req({})]);
  const c = await m.waitForCommit(7, T0, 2000, 'http://a.test/');
  assert.equal(c.timedOut, true);
});

test('waitForCommit: no commit and an aborted request is a timeout, not a failure', async () => {
  const m = load();
  m.setTab({ url: 'about:blank', title: '', status: 'loading' });
  m.setNav({});
  m.setLog([req({ error: 'NS_BINDING_ABORTED', end: T0 + 50 })]);
  const c = await m.waitForCommit(7, T0, 2000, 'http://a.test/');
  assert.equal(c.timedOut, true);
});
