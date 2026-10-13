/*
 * CHROMIUM TWIN of tests/js/observability.test.mjs — the per-tab OBSERVABILITY CORE in
 * extension-chromium/background.js: the TAB_REQUESTS / TAB_CONSOLE / TAB_NAV ring buffers
 * and the webRequest / webNavigation listeners that fill them, netDigestSince and
 * resolveTabId. (The main-frame outcome scan that reads TAB_REQUESTS back moved into
 * mainFrameOutcome; the navdiag.test.mjs twins own that block now.)
 *
 * Two engine differences vs. the gecko twin, both exercised here:
 *   1. Redirect model. Chromium keeps ONE requestId across a redirect chain (unlike FF152,
 *      which replaces the channel and mints a new requestId per hop). So onBeforeRedirect
 *      RECORDS the hop + follows the url but does NOT finish the entry — the same id lives
 *      on to onCompleted, which stamps the final status.
 *   2. No per-tab proxy. There is no TAB_PROXY map (chrome.proxy is global-only, design
 *      D7), so tabs.onRemoved releases four per-tab maps, not five.
 * The error vocabulary is Blink's net::ERR_* (gecko used NS_ERROR_*).
 *
 * Like the gecko original: background.js is a top-level SW script, so we slice each block
 * out of the source by stable anchors and instantiate it with stubbed browser APIs. A
 * missing anchor is LOUD (a vacuous-pass guard).
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');

function slice(startAnchor, endAnchor, what) {
  const start = BG_SRC.indexOf(startAnchor);
  const end = BG_SRC.indexOf(endAnchor, start);
  assert.ok(start >= 0 && end > start,
    `${what} block not found in background.js — the slice anchors moved, so this test ` +
    `is stale (fix the anchors), not the product. Anchors: ${JSON.stringify(startAnchor)} .. ` +
    JSON.stringify(endAnchor));
  return BG_SRC.slice(start, end);
}

// ---- harness: the per-tab tracking block + its listeners --------------------

function loadTracking() {
  const block = slice(
    'const TAB_REQUESTS = new Map();',
    '// --- Most-recently-focused browser window tracking',
    'per-tab observability');

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const console = { log() {}, warn() {}, error() {} };
    const L = {};
    const hook = (name) => ({ addListener: (fn) => { L[name] = fn; } });
    const browser = {
      webRequest: {
        onBeforeRequest: hook('onBeforeRequest'),
        onCompleted: hook('onCompleted'),
        onErrorOccurred: hook('onErrorOccurred'),
        onResponseStarted: hook('onResponseStarted'),
        onBeforeRedirect: hook('onBeforeRedirect'),
      },
      webNavigation: {
        onCommitted: hook('navCommitted'),
        onHistoryStateUpdated: hook('navHistory'),
        onReferenceFragmentUpdated: hook('navFragment'),
        onErrorOccurred: hook('navError'),
        onCompleted: hook('navCompleted'),
      },
      tabs: {
        onRemoved: hook('tabRemoved'),
        onActivated: hook('tabActivated'),
        onCreated: hook('tabCreated'),
        onUpdated: hook('tabUpdated'),
      },
    };
    // forgetSnapshotDocs lives next to runAutomation, outside this slice. Record the
    // calls so the leak test can still prove the handler releases the snapshot-document
    // baselines for the closed tab.
    const FORGOTTEN_SNAPSHOT_DOCS = [];
    const forgetSnapshotDocs = (id) => { FORGOTTEN_SNAPSHOT_DOCS.push(id); };
    // And wakeSettle, which sits beside waitForSettle: record which tabs the listeners wake.
    const WOKEN = [];
    const wakeSettle = (id) => { WOKEN.push(id); };

    ${block}

    return {
      L, TAB_REQUESTS, TAB_NET, TAB_NAV, TAB_CONSOLE,
      REQ_LOG_MAX, CONSOLE_MAX, FORGOTTEN_SNAPSHOT_DOCS, tabLastAccessedMs, WOKEN,
    };
  `);
  return factory();
}

// The console ring buffer is filled inside the runtime.onMessage listener, whose
// enclosing listener is far too large to slice; the console branch itself is a
// self-contained if-block, so we lift it into a callable of the same shape, with the
// takeConsoleEntries it hands the entries to (the flush answer goes through it too).
function loadConsoleSink(TAB_CONSOLE, CONSOLE_MAX) {
  const block = slice(
    '// Console entries relayed by the content script\'s capture layer',
    '// END console-entries sink',
    'console-entries sink');
  const take = slice('function takeConsoleEntries(tabId, entries)', '// ---- console flush',
    'takeConsoleEntries');

  // eslint-disable-next-line no-new-func
  const factory = new Function('TAB_CONSOLE', 'CONSOLE_MAX', `
    ${take}
    return function onMessage(request, sender) {
      ${block}
      return undefined;
    };
  `);
  return factory(TAB_CONSOLE, CONSOLE_MAX);
}

function loadNetDigest() {
  const block = slice(
    'function netDigestSince(tabId, t0)',
    'function consoleErrorsSince(tabId, t0)',
    'netDigestSince');

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const TAB_REQUESTS = new Map();
    ${block}
    return { netDigestSince, TAB_REQUESTS };
  `);
  return factory();
}

function loadResolveTabId() {
  const block = slice(
    '// Resolve the target tab for a request:',
    '// Wait until a tab "settles"',
    'resolveTabId');

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const TAB_NAV = new Map();
    let tabs = [];                       // [{id, active, title, url}]
    // Chromium's privileged-URL set (chrome/edge/devtools/chrome-extension/view-source/
    // about) — used as a stub since the real isPrivilegedUrl sits above this slice.
    let privileged = (u) => /^(about|chrome|edge|devtools|chrome-extension|view-source):/.test(u || '');

    const browser = { tabs: {
      query: async (q) => {
        if (q && q.active) return tabs.filter(t => t.active);
        return tabs.slice();
      },
      get: async (id) => {
        const t = tabs.find(t => t.id === id);
        if (!t) throw new Error('No tab with id: ' + id);
        return t;
      },
    } };
    function isPrivilegedUrl(u) { return privileged(u); }
    function humanizeNetError(code) { return code ? 'HUMANIZED(' + code + ')' : null; }

    ${block}

    return {
      resolveTabId, TAB_NAV,
      setTabs: (t) => { tabs = t; },
    };
  `);
  return factory();
}

// The chromium privileged-URL predicate, sliced whole: its scheme list is what decides
// whether page_read / page_eval refuse a tab at all, and Blink's list is not gecko's.
function loadIsPrivilegedUrl() {
  const block = slice(
    'function isPrivilegedUrl(url) {',
    '// Resolve the target tab for a request',
    'privileged-URL predicate');

  // eslint-disable-next-line no-new-func
  return new Function(`${block}\n    return { isPrivilegedUrl };`)();
}

// ---- fixtures ----------------------------------------------------------------

const req = (id, over = {}) => ({
  requestId: String(id), tabId: 1, url: `https://x.test/${id}`, method: 'GET',
  type: 'xmlhttprequest', timeStamp: 1000, ...over,
});

// =============================================================================
// TAB_REQUESTS — the bounded request ring buffer
// =============================================================================

test('onBeforeRequest logs a request against its tab and counts it in flight', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1, { url: 'https://x.test/a', method: 'POST', type: 'main_frame' }));

  const log = m.TAB_REQUESTS.get(1);
  assert.equal(log.length, 1);
  assert.deepEqual(log[0], { id: '1', url: 'https://x.test/a', method: 'POST',
                             type: 'main_frame', start: 1000 });
  assert.equal(m.TAB_NET.get(1).inflight, 1);
});

test('requests from no tab (tabId < 0) are never tracked', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1, { tabId: -1 }));
  assert.equal(m.TAB_REQUESTS.size, 0, 'a background/system request has no tab to attribute it to');
  assert.equal(m.TAB_NET.size, 0);
});

test('the request log is BOUNDED at REQ_LOG_MAX, evicting oldest-first', () => {
  const m = loadTracking();
  const cap = m.REQ_LOG_MAX;
  assert.ok(cap > 0, 'REQ_LOG_MAX is a real cap');

  for (let i = 0; i < cap + 5; i++) m.L.onBeforeRequest(req(i, { url: `https://x.test/${i}` }));

  const log = m.TAB_REQUESTS.get(1);
  assert.equal(log.length, cap, 'the log never grows past the cap — this buffer is forever-on');
  assert.equal(log[0].id, '5', 'the 5 oldest entries were dropped (FIFO), not the newest');
  assert.equal(log[log.length - 1].id, String(cap + 4), 'the most recent request is retained');
});

test('eviction is per-tab: a busy tab cannot evict a quiet tab\'s log', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req('keep', { tabId: 2 }));
  for (let i = 0; i < m.REQ_LOG_MAX + 10; i++) m.L.onBeforeRequest(req(i, { tabId: 1 }));

  assert.equal(m.TAB_REQUESTS.get(2).length, 1);
  assert.equal(m.TAB_REQUESTS.get(2)[0].id, 'keep');
});

// ---- finishTrackedRequest (onCompleted / onErrorOccurred) -------------------

test('onCompleted stamps status/end on the matching entry and clears in-flight', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1));
  m.L.onBeforeRequest(req(2));
  m.L.onCompleted({ ...req(1), timeStamp: 1500, statusCode: 404, ip: '1.2.3.4' });

  const [a, b] = m.TAB_REQUESTS.get(1);
  assert.equal(a.status, 404);
  assert.equal(a.end, 1500);
  assert.equal(a.ip, '1.2.3.4', 'the dialed peer is recorded when the connection got that far');
  assert.equal(b.status, undefined, 'the other in-flight request is untouched');
  assert.equal(m.TAB_NET.get(1).inflight, 1, 'one of the two completed');
});

test('onErrorOccurred records the net::ERR_* error with no status', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1));
  m.L.onErrorOccurred({ ...req(1), timeStamp: 1200, error: 'net::ERR_CONNECTION_REFUSED' });

  const e = m.TAB_REQUESTS.get(1)[0];
  assert.equal(e.error, 'net::ERR_CONNECTION_REFUSED');
  assert.equal(e.status, null);
  assert.equal(e.end, 1200, 'an errored request is finished, not left pending forever');
  assert.equal(m.TAB_NET.get(1).inflight, 0);
});

test('in-flight never goes negative when a completion arrives with no logged start', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1));
  m.L.onCompleted({ ...req(1), statusCode: 200 });
  m.L.onCompleted({ ...req(1), statusCode: 200 }); // duplicate / late

  assert.equal(m.TAB_NET.get(1).inflight, 0,
    'a negative inflight would make waitForSettle\'s network-quiet check unreachable');
});

test('a completion for an unknown requestId does not corrupt other entries', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1));
  m.L.onCompleted({ ...req('ghost'), statusCode: 500 });

  assert.equal(m.TAB_REQUESTS.get(1)[0].status, undefined);
});

test('onResponseStarted stamps the dialed ip on a still-PENDING entry', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1));
  m.L.onResponseStarted({ ...req(1), ip: '::1' });

  const e = m.TAB_REQUESTS.get(1)[0];
  assert.equal(e.ip, '::1', 'the ip diagnoses an address-family split before completion');
  assert.equal(e.end, undefined, 'response-started is not completion — the entry is still pending');
});

test('onResponseStarted without an ip leaves the entry ip-less (a pure hang)', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1));
  m.L.onResponseStarted({ ...req(1) });
  assert.equal(m.TAB_REQUESTS.get(1)[0].ip, undefined,
    'an absent ip must stay absent — the server reports "dialed: unknown" off exactly this');
});

// =============================================================================
// onBeforeRedirect — ONE requestId across hops (chromium keeps the channel)
// =============================================================================

test('onBeforeRedirect records the hop, follows the URL, sets ip, but does NOT finish the entry', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1, { url: 'http://x.test/a', type: 'main_frame' }));
  m.L.onBeforeRedirect({ ...req(1), timeStamp: 1100, statusCode: 301,
                         redirectUrl: 'https://x.test/b', ip: '9.9.9.9' });

  const e = m.TAB_REQUESTS.get(1)[0];
  assert.deepEqual(e.redirects, [{ from: 'http://x.test/a', to: 'https://x.test/b', status: 301 }]);
  assert.equal(e.url, 'https://x.test/b', 'the entry follows the hop, so a later failure names the final hop');
  assert.equal(e.ip, '9.9.9.9');
  // Chromium keeps ONE requestId across the redirect: this SAME id will still see
  // onCompleted, so the entry must stay pending here — NOT be finished like FF152.
  assert.equal(e.status, undefined, 'the hop is recorded but the entry is not finished (the id lives on)');
  assert.equal(e.end, undefined, 'still pending until onCompleted stamps the final outcome');
});

test('a redirect chain on ONE requestId accumulates all hops in order (the normal chromium case)', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1, { url: 'http://x.test/a', type: 'main_frame' }));
  m.L.onBeforeRedirect({ ...req(1), statusCode: 301, redirectUrl: 'https://x.test/b' });
  m.L.onBeforeRedirect({ ...req(1), statusCode: 302, redirectUrl: 'https://x.test/c' });

  assert.deepEqual(m.TAB_REQUESTS.get(1)[0].redirects.map(h => [h.from, h.to, h.status]), [
    ['http://x.test/a', 'https://x.test/b', 301],
    ['https://x.test/b', 'https://x.test/c', 302],
  ]);
});

test('the onCompleted that follows a redirect stamps the FINAL status; the hop history survives', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1, { type: 'main_frame' }));
  m.L.onBeforeRedirect({ ...req(1), timeStamp: 1100, statusCode: 301, redirectUrl: 'https://x.test/b' });
  m.L.onCompleted({ ...req(1), timeStamp: 1900, statusCode: 200 });

  const e = m.TAB_REQUESTS.get(1)[0];
  assert.equal(e.status, 200, 'the same id continued to the destination — the real outcome wins');
  assert.equal(e.end, 1900);
  assert.equal(e.redirects.length, 1, 'the hop history survives');
});

// =============================================================================
// TAB_NAV — commit / error state
// =============================================================================

test('a main-frame commit records the URL and CLEARS a prior nav error', () => {
  const m = loadTracking();
  m.L.navError({ tabId: 1, frameId: 0, error: 'net::ERR_NAME_NOT_RESOLVED',
                 url: 'https://nope.test/', timeStamp: 500 });
  assert.equal(m.TAB_NAV.get(1).lastNavError, 'net::ERR_NAME_NOT_RESOLVED');

  m.L.navCommitted({ tabId: 1, frameId: 0, url: 'https://ok.test/', timeStamp: 900 });

  const nav = m.TAB_NAV.get(1);
  assert.equal(nav.lastCommitted, 900);
  assert.equal(nav.lastCommittedUrl, 'https://ok.test/');
  assert.equal(nav.lastNavError, null, 'a successful commit must not leave a stale failure behind');
  assert.equal(nav.lastNavErrorAt, null);
});

test('SPA history/fragment updates count as commits (they are what waitForSettle sees)', () => {
  const m = loadTracking();
  m.L.navHistory({ tabId: 1, frameId: 0, url: 'https://x.test/#/a', timeStamp: 700 });
  assert.equal(m.TAB_NAV.get(1).lastCommitted, 700);

  m.L.navFragment({ tabId: 1, frameId: 0, url: 'https://x.test/#/b', timeStamp: 800 });
  assert.equal(m.TAB_NAV.get(1).lastCommittedUrl, 'https://x.test/#/b');
});

test('SUB-frame navigation never touches the tab\'s main-frame nav state', () => {
  const m = loadTracking();
  m.L.navCommitted({ tabId: 1, frameId: 0, url: 'https://x.test/', timeStamp: 900 });
  m.L.navCommitted({ tabId: 1, frameId: 3, url: 'https://ads.test/', timeStamp: 950 });
  m.L.navError({ tabId: 1, frameId: 3, error: 'net::ERR_CONNECTION_REFUSED', timeStamp: 960 });

  const nav = m.TAB_NAV.get(1);
  assert.equal(nav.lastCommittedUrl, 'https://x.test/', 'an iframe must not claim the tab committed elsewhere');
  assert.equal(nav.lastCommitted, 900);
  assert.equal(nav.lastNavError, null, 'a broken ad iframe is not the page failing to load');
});

test('an uncommitted navigation records the error and where it was headed', () => {
  const m = loadTracking();
  m.L.navError({ tabId: 1, frameId: 0, error: 'net::ERR_NAME_NOT_RESOLVED',
                 url: 'https://nope.test/', timeStamp: 400 });

  const nav = m.TAB_NAV.get(1);
  assert.equal(nav.lastNavError, 'net::ERR_NAME_NOT_RESOLVED');
  assert.equal(nav.lastNavErrorUrl, 'https://nope.test/');
  assert.equal(nav.lastNavErrorAt, 400);
  assert.equal(nav.lastCommitted, undefined, 'it never committed');
});

// =============================================================================
// TAB_CONSOLE — the bounded console ring buffer
// =============================================================================

test('the console buffer is BOUNDED at CONSOLE_MAX, evicting oldest-first', () => {
  const m = loadTracking();
  const sink = loadConsoleSink(m.TAB_CONSOLE, m.CONSOLE_MAX);
  const cap = m.CONSOLE_MAX;
  assert.ok(cap > 0, 'CONSOLE_MAX is a real cap');

  const entries = Array.from({ length: cap + 3 },
    (_, i) => ({ level: 'log', text: `line ${i}`, ts: i, url: 'https://x.test/' }));
  sink({ type: 'console_entries', entries }, { tab: { id: 1 } });

  const buf = m.TAB_CONSOLE.get(1);
  assert.equal(buf.length, cap, 'a chatty page cannot grow this buffer without bound');
  assert.equal(buf[0].text, 'line 3', 'the 3 oldest lines were dropped');
  assert.equal(buf[buf.length - 1].text, `line ${cap + 2}`);
});

test('the console buffer evicts across SEPARATE relays too (the cap is on the buffer)', () => {
  const m = loadTracking();
  const sink = loadConsoleSink(m.TAB_CONSOLE, m.CONSOLE_MAX);
  for (let i = 0; i < m.CONSOLE_MAX + 2; i++) {
    sink({ type: 'console_entries', entries: [{ level: 'error', text: `e${i}`, ts: i }] },
         { tab: { id: 1 } });
  }
  const buf = m.TAB_CONSOLE.get(1);
  assert.equal(buf.length, m.CONSOLE_MAX);
  assert.equal(buf[0].text, 'e2');
});

test('console entries are keyed by the SENDER tab, not by anything the page claims', () => {
  const m = loadTracking();
  const sink = loadConsoleSink(m.TAB_CONSOLE, m.CONSOLE_MAX);
  sink({ type: 'console_entries', tabId: 99, entries: [{ level: 'log', text: 'a', ts: 1 }] },
       { tab: { id: 4 } });

  assert.equal(m.TAB_CONSOLE.get(4).length, 1, 'the trusted sender.tab.id owns the buffer');
  assert.equal(m.TAB_CONSOLE.has(99), false, 'a page-supplied tabId is not honoured');
});

test('the console sink keeps one copy of an entry that arrives twice, and stamps its arrival', () => {
  // A batch still in flight when the relay answered a flush arrives in the batch and in
  // the answer; doc and n name the entry, so the second copy is dropped.
  const m = loadTracking();
  const sink = loadConsoleSink(m.TAB_CONSOLE, m.CONSOLE_MAX);
  const e = { level: 'error', text: 'boom', ts: 5, doc: 'd1', n: 1 };
  const before = Date.now();
  sink({ type: 'console_entries', entries: [e] }, { tab: { id: 1 } });
  sink({ type: 'console_entries', entries: [Object.assign({}, e), { ...e, n: 2 }] }, { tab: { id: 1 } });
  const buf = m.TAB_CONSOLE.get(1);
  assert.deepEqual(buf.map(x => x.n), [1, 2]);
  assert.ok(buf.every(x => x.rx >= before), 'each entry records when it reached the buffer');
  // Entries from another document are distinct even with the same number.
  sink({ type: 'console_entries', entries: [{ ...e, doc: 'd2' }] }, { tab: { id: 1 } });
  assert.equal(m.TAB_CONSOLE.get(1).length, 3);
});

test('a message from no tab, or of another type, is ignored by the console sink', () => {
  const m = loadTracking();
  const sink = loadConsoleSink(m.TAB_CONSOLE, m.CONSOLE_MAX);
  sink({ type: 'console_entries', entries: [{ level: 'log', text: 'a', ts: 1 }] }, {});
  sink({ type: 'something_else', entries: [{ level: 'log', text: 'b', ts: 1 }] }, { tab: { id: 1 } });
  assert.equal(m.TAB_CONSOLE.size, 0);
});

// =============================================================================
// tabs.onRemoved — every per-tab map is released (four maps, no TAB_PROXY)
// =============================================================================

test('closing a tab releases EVERY per-tab buffer (no leak per closed tab)', () => {
  const m = loadTracking();
  const sink = loadConsoleSink(m.TAB_CONSOLE, m.CONSOLE_MAX);
  m.L.onBeforeRequest(req(1, { tabId: 1 }));
  sink({ type: 'console_entries', entries: [{ level: 'log', text: 'a', ts: 1 }] }, { tab: { id: 1 } });
  m.L.navCommitted({ tabId: 1, frameId: 0, url: 'https://x.test/', timeStamp: 900 });
  m.L.onBeforeRequest(req(9, { tabId: 2 }));

  m.L.tabRemoved(1);

  // Chromium has no per-tab proxy routing — four maps, not gecko's five.
  for (const [name, map] of Object.entries({ TAB_REQUESTS: m.TAB_REQUESTS, TAB_NET: m.TAB_NET,
                                             TAB_NAV: m.TAB_NAV, TAB_CONSOLE: m.TAB_CONSOLE })) {
    assert.equal(map.has(1), false, `${name} still holds the closed tab`);
  }
  // SNAPSHOT_DOCS is keyed `tabId:frameId` and lives outside this slice, so assert the
  // release was REQUESTED for the closed tab (and only it).
  assert.deepEqual(m.FORGOTTEN_SNAPSHOT_DOCS, [1],
    'the snapshot-document baselines were not released for the closed tab');
  assert.equal(m.TAB_REQUESTS.has(2), true, 'the surviving tab keeps its log');
});

// =============================================================================
// netDigestSince — the outcome digest attached to act/navigate results
// =============================================================================

const T0 = 10000;

test('the digest counts only requests from this action, with a 50ms lead-in tolerance', () => {
  const { netDigestSince, TAB_REQUESTS } = loadNetDigest();
  TAB_REQUESTS.set(1, [
    { id: 'old', url: 'https://x.test/old', method: 'GET', start: T0 - 500, end: T0 - 400, status: 200 },
    { id: 'edge', url: 'https://x.test/edge', method: 'GET', start: T0 - 50, end: T0, status: 200 },
    { id: 'mine', url: 'https://x.test/mine', method: 'GET', start: T0 + 10, end: T0 + 20, status: 200 },
  ]);

  const d = netDigestSince(1, T0);
  assert.equal(d.requests, 2, 'the pre-action request is excluded; the boundary one is kept');
  assert.equal(d.pending, 0);
  assert.equal(d.failed, undefined, 'no failures — the key is absent, not an empty array');
});

test('a 4xx/5xx and a network error both surface as failures with their cause', () => {
  const { netDigestSince, TAB_REQUESTS } = loadNetDigest();
  TAB_REQUESTS.set(1, [
    { id: '1', url: 'https://x.test/login', method: 'POST', start: T0 + 1, end: T0 + 2, status: 401 },
    { id: '2', url: 'https://x.test/api', method: 'GET', start: T0 + 3, end: T0 + 4, status: 500 },
    { id: '3', url: 'https://dead.test/', method: 'GET', start: T0 + 5, end: T0 + 6,
      error: 'net::ERR_CONNECTION_REFUSED' },
    { id: '4', url: 'https://x.test/ok', method: 'GET', start: T0 + 7, end: T0 + 8, status: 204 },
  ]);

  const d = netDigestSince(1, T0);
  assert.equal(d.requests, 4);
  assert.deepEqual(d.failed, [
    { method: 'POST', url: 'https://x.test/login', status: 401, error: undefined },
    { method: 'GET', url: 'https://x.test/api', status: 500, error: undefined },
    { method: 'GET', url: 'https://dead.test/', status: undefined, error: 'net::ERR_CONNECTION_REFUSED' },
  ]);
});

test('a 3xx is not a failure (only >= 400 is)', () => {
  const { netDigestSince, TAB_REQUESTS } = loadNetDigest();
  TAB_REQUESTS.set(1, [
    { id: '1', url: 'https://x.test/r', method: 'GET', start: T0 + 1, end: T0 + 2, status: 302 },
    { id: '2', url: 'https://x.test/e', method: 'GET', start: T0 + 3, end: T0 + 4, status: 400 },
  ]);
  const d = netDigestSince(1, T0);
  assert.equal(d.failed.length, 1);
  assert.equal(d.failed[0].status, 400, '400 is the inclusive lower bound');
});

// navigate's digest (netDigestFor) leaves out what the navigation cancelled on the page it left.
function loadNavDigest() {
  const block = slice('function netDigestSince(tabId, t0)', 'function consoleErrorsSince(tabId, t0)',
    'netDigestSince');
  const abort = slice('function isNavAbort(', '// When a navigation replaces a load still in progress',
    'isNavAbort');
  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const TAB_REQUESTS = new Map();
    ${abort}
    ${block}
    return { netDigestFor, TAB_REQUESTS };
  `);
  return factory();
}

const REQ_ABORT = 'net::ERR_ABORTED';
const COMMIT = T0 + 300;

test('navigate: the left page\'s request that the navigation aborted is not a failure of the new page', () => {
  const { netDigestFor, TAB_REQUESTS } = loadNavDigest();
  TAB_REQUESTS.set(1, [
    { id: 'lazy', url: 'https://old.test/lazy.jpg', method: 'GET', type: 'image',
      start: T0 + 20, end: T0 + 250, error: REQ_ABORT },
    { id: 'doc', url: 'https://new.test/', method: 'GET', type: 'main_frame',
      start: T0 + 5, end: T0 + 200, status: 200 },
    { id: 'css', url: 'https://new.test/a.css', method: 'GET', type: 'stylesheet',
      start: COMMIT + 10, end: COMMIT + 40, status: 200 },
  ]);
  const d = netDigestFor(1, T0, COMMIT);
  assert.equal(d.failed, undefined);
  assert.equal(d.requests, 2);
  // No commit time (page_act, or a navigation that never committed): nothing is left out.
  assert.equal(netDigestFor(1, T0, null).failed.length, 1);
});

test('navigate: only an abort is left out; real failures, the main frame and the new page\'s own aborts list', () => {
  const { netDigestFor, TAB_REQUESTS } = loadNavDigest();
  TAB_REQUESTS.set(1, [
    { id: 'refused', url: 'https://old.test/api', method: 'GET', type: 'xmlhttprequest',
      start: T0 + 20, end: T0 + 30, error: 'net::ERR_CONNECTION_REFUSED' },
    { id: '404', url: 'https://old.test/x.js', method: 'GET', type: 'script',
      start: T0 + 25, end: T0 + 35, status: 404 },
    { id: 'doc', url: 'https://new.test/', method: 'GET', type: 'main_frame',
      start: T0 + 5, end: T0 + 100, error: REQ_ABORT },
    { id: 'own', url: 'https://new.test/feed', method: 'GET', type: 'xmlhttprequest',
      start: COMMIT + 50, end: COMMIT + 60, error: REQ_ABORT },
  ]);
  const d = netDigestFor(1, T0, COMMIT);
  assert.deepEqual(d.failed.map(f => f.url),
    ['https://old.test/api', 'https://old.test/x.js', 'https://new.test/', 'https://new.test/feed']);
});

test('favicon 404s are never surfaced as failures (ubiquitous noise)', () => {
  const { netDigestSince, TAB_REQUESTS } = loadNetDigest();
  TAB_REQUESTS.set(1, [
    { id: '1', url: 'https://x.test/favicon.ico', method: 'GET', start: T0 + 1, end: T0 + 2, status: 404 },
    { id: '2', url: 'https://x.test/favicon.ico?v=2', method: 'GET', start: T0 + 3, end: T0 + 4, status: 404 },
  ]);
  const d = netDigestSince(1, T0);
  assert.equal(d.requests, 2, 'they are still counted as requests');
  assert.equal(d.failed, undefined, 'but a missing favicon is never reported as a failure');
});

test('a URL that merely CONTAINS "favicon.ico" mid-path is still reported', () => {
  const { netDigestSince, TAB_REQUESTS } = loadNetDigest();
  TAB_REQUESTS.set(1, [
    { id: '1', url: 'https://x.test/favicon.ico/steal', method: 'GET', start: T0 + 1, end: T0 + 2, status: 500 },
  ]);
  assert.equal(netDigestSince(1, T0).failed.length, 1,
    'the noise filter is anchored at the end of the path — it must not swallow a real 500');
});

test('an unfinished request is counted as pending', () => {
  const { netDigestSince, TAB_REQUESTS } = loadNetDigest();
  TAB_REQUESTS.set(1, [
    { id: '1', url: 'https://x.test/sse', method: 'GET', start: T0 + 1 },
    { id: '2', url: 'https://x.test/done', method: 'GET', start: T0 + 2, end: T0 + 3, status: 200 },
  ]);
  const d = netDigestSince(1, T0);
  assert.equal(d.pending, 1);
  assert.equal(d.requests, 2);
});

test('the failure list is capped at 10 (a broken page must not flood the result)', () => {
  const { netDigestSince, TAB_REQUESTS } = loadNetDigest();
  TAB_REQUESTS.set(1, Array.from({ length: 25 }, (_, i) => ({
    id: String(i), url: `https://x.test/${i}`, method: 'GET', start: T0 + i, end: T0 + i + 1, status: 500,
  })));
  const d = netDigestSince(1, T0);
  assert.equal(d.requests, 25, 'the true count is still reported');
  assert.equal(d.failed.length, 10, 'only the first 10 are listed');
});

test('a long URL is truncated to 200 chars in the digest', () => {
  const { netDigestSince, TAB_REQUESTS } = loadNetDigest();
  const long = 'https://x.test/' + 'q'.repeat(500);
  TAB_REQUESTS.set(1, [{ id: '1', url: long, method: 'GET', start: T0 + 1, end: T0 + 2, status: 500 }]);
  assert.equal(netDigestSince(1, T0).failed[0].url.length, 200);
});

test('an untracked tab yields an empty digest rather than throwing', () => {
  const { netDigestSince } = loadNetDigest();
  assert.deepEqual(netDigestSince(999, T0), { requests: 0, pending: 0 });
});

// =============================================================================
// resolveTabId — active-tab default + the stale-id error
// =============================================================================

test('an omitted tabId resolves to the active tab AND is written back for downstream handlers', async () => {
  const m = loadResolveTabId();
  m.setTabs([{ id: 3, active: false, url: 'https://a.test/' },
             { id: 5, active: true, url: 'https://b.test/', title: 'B' }]);

  const data = {};
  assert.equal(await m.resolveTabId(data), 5);
  assert.equal(data.tabId, 5, 'downstream handlers read data.tabId — it must be populated');
});

test('an explicit LIVE tabId is returned untouched', async () => {
  const m = loadResolveTabId();
  m.setTabs([{ id: 3, active: true, url: 'https://a.test/' }, { id: 8, active: false, url: 'https://b.test/' }]);
  assert.equal(await m.resolveTabId({ tabId: 8 }), 8, 'a non-active tab is a perfectly valid target');
});

test('no active tab at all is a clear error, not a crash', async () => {
  const m = loadResolveTabId();
  m.setTabs([]);
  await assert.rejects(() => m.resolveTabId({}), /No tab_id given and no active tab found/);
});

test('a STALE tabId errors with STALE_TAB and the bare fact, never the user\'s active tab', async () => {
  const m = loadResolveTabId();
  m.setTabs([{ id: 3, active: false, url: 'https://a.test/' },
             { id: 5, active: true, url: 'https://b.test/page', title: 'Live Page' }]);

  const err = await m.resolveTabId({ tabId: 42 }).then(() => null, (e) => e);
  assert.ok(err, 'a stale id must not resolve');
  assert.equal(err.code, 'STALE_TAB', 'the code is what the server branches on');
  // The server words the next step (response_hints.stale_tab_text: open your own tab).
  // Naming the active tab here sent agents onto the tab the user was looking at.
  assert.equal(err.message, 'Tab 42 no longer exists.');
  assert.doesNotMatch(err.message, /Live Page|b\.test|active tab/);
});

test('a stale tabId with NO active tab errors the same way', async () => {
  const m = loadResolveTabId();
  m.setTabs([{ id: 3, active: false, url: 'https://a.test/' }]);

  const err = await m.resolveTabId({ tabId: 42 }).then(() => null, (e) => e);
  assert.equal(err.code, 'STALE_TAB');
  assert.equal(err.message, 'Tab 42 no longer exists.');
});

// ---- requireContent: privileged pages -------------------------------------

test('requireContent lets a normal http(s) tab through', async () => {
  const m = loadResolveTabId();
  m.setTabs([{ id: 5, active: true, url: 'https://b.test/', title: 'B' }]);
  assert.equal(await m.resolveTabId({ tabId: 5 }, { requireContent: true }), 5);
});

test('requireContent refuses a privileged page, naming the page and the alternatives', async () => {
  const m = loadResolveTabId();
  m.setTabs([{ id: 5, active: true, url: 'chrome://settings?tab=1', title: 'Settings' }]);

  const err = await m.resolveTabId({ tabId: 5 }, { requireContent: true }).then(() => null, (e) => e);
  assert.equal(err.code, 'CONTENT_UNREACHABLE');
  assert.match(err.message, /Tab 5 is a privileged page \(chrome:\/\/settings\)/, 'the query string is stripped');
  assert.match(err.message, /page_read\/page_snapshot\/page_act\/page_eval are unavailable/);
  assert.match(err.message, /page_screenshot returns the painted pixels/,
    'the privileged-safe alternatives are named so the agent is not just stuck');
  assert.match(err.message, /requests returns its network log/);
});

test('a privileged about:blank reports the failed navigation that PUT it there', async () => {
  const m = loadResolveTabId();
  m.setTabs([{ id: 5, active: true, url: 'about:blank', title: '' }]);
  m.TAB_NAV.set(5, { lastAttemptedUrl: 'https://nope.test/', lastNavError: 'net::ERR_NAME_NOT_RESOLVED' });

  const err = await m.resolveTabId({ tabId: 5 }, { requireContent: true }).then(() => null, (e) => e);
  assert.equal(err.code, 'CONTENT_UNREACHABLE');
  assert.match(err.message, /The last attempted navigation was https:\/\/nope\.test\//);
  assert.match(err.message, /Last navigation error: HUMANIZED\(net::ERR_NAME_NOT_RESOLVED\)/);
  assert.match(err.message, /Navigate it to an http\(s\) page first/);
});

test('a non-blank privileged page does NOT get the navigation-failure narrative', async () => {
  const m = loadResolveTabId();
  m.setTabs([{ id: 5, active: true, url: 'chrome://settings', title: 'Settings' }]);
  m.TAB_NAV.set(5, { lastAttemptedUrl: 'https://nope.test/', lastNavError: 'net::ERR_NAME_NOT_RESOLVED' });

  const err = await m.resolveTabId({ tabId: 5 }, { requireContent: true }).then(() => null, (e) => e);
  assert.doesNotMatch(err.message, /last attempted navigation/,
    'chrome://settings is where the user went on purpose — a stale nav error is not the story');
  assert.match(err.message, /Navigate it to a normal http\(s\) page first/);
});

// ---- the REAL chromium privileged-URL predicate -----------------------------

test('isPrivilegedUrl flags the chromium privileged schemes and the web store, passes http(s)', () => {
  const { isPrivilegedUrl } = loadIsPrivilegedUrl();
  for (const u of ['chrome://settings', 'edge://flags', 'devtools://devtools/bundled/',
                   'chrome-extension://abc/page.html', 'view-source:https://x.test/',
                   'about:blank', 'data:text/html,hi',
                   'https://chromewebstore.google.com/detail/foo',
                   'https://chrome.google.com/webstore/detail/foo', '']) {
    assert.equal(isPrivilegedUrl(u), true, `${u} must be privileged`);
  }
  for (const u of ['https://example.com/', 'http://localhost:3000/', 'https://x.test/a?b#c',
                   'https://chromewebstore.google.com.lookalike.test/']) {
    assert.equal(isPrivilegedUrl(u), false, `${u} must be reachable`);
  }
});

// ---- lastAccessed, observed --------------------------------------------------
// `tabs.Tab.lastAccessed` is undefined for every tab on Firefox for Android
// (Bugzilla 2032927: ext-android.js reads a Fennec property the Fenix rewrite
// dropped) and on Chromium before 121. Nothing can recover what the browser never
// wrote down, so the block records the moments itself. These pin both halves: that
// the fallback fills the hole, and that it stays OUT OF THE WAY where the browser
// keeps a real value, because there `lastAccessed` means "last SELECTED" and
// `tabs list` orders by it.

test('a tab the user switches to gets a lastAccessed the browser never gave us', () => {
  const { L, tabLastAccessedMs } = loadTracking();
  const bare = { id: 7 };                       // Android: no lastAccessed, ever
  assert.equal(tabLastAccessedMs(bare), undefined, 'nothing observed yet, so unknown');

  const before = Date.now();
  L.tabActivated({ tabId: 7, windowId: 1 });
  const got = tabLastAccessedMs(bare);
  assert.ok(got >= before && got <= Date.now(), `stamped now, got ${got}`);
});

test('a brand-new tab is stamped too (desktop stamps lastAccessed on creation)', () => {
  const { L, tabLastAccessedMs } = loadTracking();
  L.tabCreated({ id: 12, url: 'https://example.org/' });
  assert.ok(tabLastAccessedMs({ id: 12 }) > 0);
});

test('a navigation alone is enough — a tab that loaded a page is not "unknown"', () => {
  const { L, tabLastAccessedMs } = loadTracking();
  L.navCommitted({ tabId: 3, frameId: 0, url: 'https://example.org/', timeStamp: 5000 });
  assert.equal(tabLastAccessedMs({ id: 3 }), 5000);
});

test("the browser's own lastAccessed WINS — a background navigation must not reorder tabs list", () => {
  const { L, tabLastAccessedMs } = loadTracking();
  // Desktop: the tab was selected at t=1000 and later loaded a page in the
  // background at t=9000. "Last accessed" is still 1000; taking the max here would
  // float a tab the user never returned to to the top of `tabs list`.
  L.navCommitted({ tabId: 4, frameId: 0, url: 'https://example.org/', timeStamp: 9000 });
  assert.equal(tabLastAccessedMs({ id: 4, lastAccessed: 1000 }), 1000);
});

test('a zero or non-numeric lastAccessed counts as absent, not as 1970', () => {
  const { L, tabLastAccessedMs } = loadTracking();
  L.tabActivated({ tabId: 5 });
  const observed = tabLastAccessedMs({ id: 5 });
  assert.ok(observed > 0);
  assert.equal(tabLastAccessedMs({ id: 5, lastAccessed: 0 }), observed);
  assert.equal(tabLastAccessedMs({ id: 5, lastAccessed: undefined }), observed);
});

test('closing a tab forgets its observed access time (the map stays bounded)', () => {
  const { L, TAB_NAV, tabLastAccessedMs } = loadTracking();
  L.tabActivated({ tabId: 9 });
  assert.ok(TAB_NAV.has(9));
  L.tabRemoved(9);
  assert.equal(TAB_NAV.has(9), false);
  assert.equal(tabLastAccessedMs({ id: 9 }), undefined);
});

test('a malformed event is ignored rather than keyed under undefined', () => {
  const { L, TAB_NAV } = loadTracking();
  L.tabActivated({});
  L.tabActivated(null);
  L.tabCreated({});
  assert.equal(TAB_NAV.size, 0);
});

test('the tabs.list handler actually SENDS the resolved value', () => {
  // The resolver above is covered; its one call site is inside the request switch,
  // which no slice reaches. This codebase has shipped dead arming code past a green
  // suite before (the StreamFilter capture), so pin the wiring at the source.
  assert.match(BG_SRC, /lastAccessed: tabLastAccessedMs\(tab\)/,
    'tabs.list must send tabLastAccessedMs(tab), not the raw tab.lastAccessed — '
    + 'otherwise every tab on Firefox for Android reports "unknown" again');
});

// =============================================================================
// The network-quiet clock and the settle wake-ups
// =============================================================================
// waitForSettle sleeps instead of polling, so the listeners that can end its wait must
// wake it; and a request that cannot put content in the page stays off its clock.

test('a ping request is logged but kept off the network-quiet clock', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req('b', { type: 'ping', timeStamp: 1000 }));
  assert.equal(m.TAB_REQUESTS.get(1).length, 1, 'requests still lists it');
  assert.equal(m.TAB_NET.has(1), false, 'it neither counts in flight nor moves lastActivity');
  m.L.onBeforeRequest(req('x'));
  m.L.onCompleted({ ...req('b', { type: 'ping' }), statusCode: 204 });
  assert.equal(m.TAB_NET.get(1).inflight, 1, 'its completion does not end the xhr still in flight');
});

test('a request that ends wakes a sleeping settle on its tab', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1));
  m.L.onCompleted({ ...req(1), statusCode: 200 });
  m.L.onBeforeRequest(req(2));
  m.L.onErrorOccurred({ ...req(2), error: 'net::ERR_CONNECTION_RESET' });
  assert.deepEqual(m.WOKEN, [1, 1]);
});

test('a status or URL change wakes it; a title change does not', () => {
  const m = loadTracking();
  m.L.tabUpdated(4, { title: 'Loading...' });
  assert.deepEqual(m.WOKEN, []);
  m.L.tabUpdated(4, { status: 'complete' });
  m.L.tabUpdated(5, { url: 'https://x.test/' });
  assert.deepEqual(m.WOKEN, [4, 5]);
});

test('a closed tab wakes it, so the settle reports the close at once', () => {
  const m = loadTracking();
  m.L.tabRemoved(6);
  assert.deepEqual(m.WOKEN, [6]);
});

test('a finished main-frame load is recorded and wakes the settle; a sub-frame one is not', () => {
  const m = loadTracking();
  m.L.navCompleted({ tabId: 1, frameId: 3, url: 'https://ads.test/', timeStamp: 900 });
  assert.equal(m.TAB_NAV.has(1), false);
  m.L.navCompleted({ tabId: 1, frameId: 0, url: 'https://x.test/', timeStamp: 950 });
  assert.deepEqual(m.TAB_NAV.get(1).events, [{ ev: 'done', url: 'https://x.test/', at: 950 }]);
  assert.deepEqual(m.WOKEN, [1]);
});

test('the main-frame event record is bounded, oldest first', () => {
  const m = loadTracking();
  for (let i = 0; i < 40; i++) {
    m.L.navCommitted({ tabId: 1, frameId: 0, url: `https://x.test/${i}`, timeStamp: 1000 + i });
  }
  const events = m.TAB_NAV.get(1).events;
  assert.ok(events.length <= 12, `kept ${events.length}`);
  assert.equal(events[events.length - 1].url, 'https://x.test/39', 'the newest is kept');
});
