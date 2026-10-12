/*
 * Unit tests for the per-tab OBSERVABILITY CORE in extension-gecko/background.js:
 * the TAB_REQUESTS / TAB_CONSOLE / TAB_NAV ring buffers and the webRequest /
 * webNavigation listeners that fill them, netDigestSince and resolveTabId.
 * (The main-frame outcome scan that reads TAB_REQUESTS back — redirect stitching,
 * dialed peer, HTTP status — moved into mainFrameOutcome and is covered by the
 * navdiag.test.mjs twins, which own that block now.)
 *
 * Why these need sliced tests: this machinery is ALWAYS ON and BOUNDED — it runs
 * on every request of every tab for the life of the browser, so an eviction bug
 * is an unbounded memory leak and an off-by-one in the "since t0" filter silently
 * drops the very 401 an agent is trying to see. Until now its only guard was the
 * Firefox-gated integration suite, which cannot force 300+ requests, a stale tab
 * id, or FF152's channel-replacing redirects deterministically.
 *
 * Like proxy.test.mjs / neterror.test.mjs / connection.test.mjs / actwait.test.mjs:
 * background.js is a top-level extension script, so we slice each block out of the
 * source by stable anchors and instantiate it with stubbed browser APIs.
 * Run with: npm run test:js  (== node --test "tests/js/*.test.mjs")
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

// A missing anchor must be LOUD: a slice that finds nothing would make every
// assertion below vacuously pass. If one of these fires, the anchors moved in
// background.js — fix the anchors here, not the product.
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
// The block registers its listeners at load time, so a stub `browser` whose
// addListener just records the callback hands us the real handlers to drive.

function loadTracking() {
  const block = slice(
    'const TAB_REQUESTS = new Map();',
    '// --- Most-recently-focused Firefox window tracking',
    'per-tab observability');

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
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
    // tabs.onRemoved calls deleteTabProxy (defined in the proxy block, not this slice).
    // Only its map effect matters here; persistence is covered in proxy.test.mjs.
    const deleteTabProxy = (id) => TAB_PROXY.delete(id);
    // Same situation: forgetSnapshotDocs lives next to runAutomation, outside this
    // slice. Record the calls so the leak test can still prove the handler releases
    // the snapshot-document baselines for the closed tab.
    const FORGOTTEN_SNAPSHOT_DOCS = [];
    const forgetSnapshotDocs = (id) => { FORGOTTEN_SNAPSHOT_DOCS.push(id); };
    // And wakeSettle, which sits beside waitForSettle: record which tabs the listeners wake.
    const WOKEN = [];
    const wakeSettle = (id) => { WOKEN.push(id); };

    ${block}

    return {
      L, TAB_REQUESTS, TAB_NET, TAB_NAV, TAB_CONSOLE, TAB_PROXY,
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
    let privileged = (u) => /^(about|moz-extension|chrome):/.test(u || '');

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

test('onErrorOccurred records the symbolic error with no status', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1));
  m.L.onErrorOccurred({ ...req(1), timeStamp: 1200, error: 'NS_ERROR_CONNECTION_REFUSED' });

  const e = m.TAB_REQUESTS.get(1)[0];
  assert.equal(e.error, 'NS_ERROR_CONNECTION_REFUSED');
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
// onBeforeRedirect — per-hop recording + the FF152 "never lingers pending" fix
// =============================================================================

test('onBeforeRedirect records the hop, follows the URL, and FINISHES the entry', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1, { url: 'http://x.test/a', type: 'main_frame' }));
  m.L.onBeforeRedirect({ ...req(1), timeStamp: 1100, statusCode: 301,
                         redirectUrl: 'https://x.test/b', ip: '9.9.9.9' });

  const e = m.TAB_REQUESTS.get(1)[0];
  assert.deepEqual(e.redirects, [{ from: 'http://x.test/a', to: 'https://x.test/b', status: 301 }]);
  assert.equal(e.url, 'https://x.test/b', 'the entry follows the hop, so a later failure names the final hop');
  assert.equal(e.ip, '9.9.9.9');
  // FF152 replaces the channel on a redirect: this requestId will NEVER see
  // onCompleted, so if the hop were not finished here it would linger "pending".
  assert.equal(e.status, 301);
  assert.equal(e.end, 1100);
});

test('a redirect chain on ONE requestId (older FF) accumulates all hops in order', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1, { url: 'http://x.test/a', type: 'main_frame' }));
  m.L.onBeforeRedirect({ ...req(1), statusCode: 301, redirectUrl: 'https://x.test/b' });
  m.L.onBeforeRedirect({ ...req(1), statusCode: 302, redirectUrl: 'https://x.test/c' });

  assert.deepEqual(m.TAB_REQUESTS.get(1)[0].redirects.map(h => [h.from, h.to, h.status]), [
    ['http://x.test/a', 'https://x.test/b', 301],
    ['https://x.test/b', 'https://x.test/c', 302],
  ]);
});

test('a later onCompleted on the same id overwrites the redirect\'s provisional status', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1, { type: 'main_frame' }));
  m.L.onBeforeRedirect({ ...req(1), timeStamp: 1100, statusCode: 301, redirectUrl: 'https://x.test/b' });
  m.L.onCompleted({ ...req(1), timeStamp: 1900, statusCode: 200 });

  const e = m.TAB_REQUESTS.get(1)[0];
  assert.equal(e.status, 200, 'the id DID continue (older FF) — the real outcome wins');
  assert.equal(e.end, 1900);
  assert.equal(e.redirects.length, 1, 'the hop history survives');
});

// =============================================================================
// TAB_NAV — commit / error state
// =============================================================================

test('a main-frame commit records the URL and CLEARS a prior nav error', () => {
  const m = loadTracking();
  m.L.navError({ tabId: 1, frameId: 0, error: 'NS_ERROR_UNKNOWN_HOST',
                 url: 'https://nope.test/', timeStamp: 500 });
  assert.equal(m.TAB_NAV.get(1).lastNavError, 'NS_ERROR_UNKNOWN_HOST');

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
  m.L.navError({ tabId: 1, frameId: 3, error: 'NS_ERROR_CONNECTION_REFUSED', timeStamp: 960 });

  const nav = m.TAB_NAV.get(1);
  assert.equal(nav.lastCommittedUrl, 'https://x.test/', 'an iframe must not claim the tab committed elsewhere');
  assert.equal(nav.lastCommitted, 900);
  assert.equal(nav.lastNavError, null, 'a broken ad iframe is not the page failing to load');
});

test('an uncommitted navigation records the error and where it was headed', () => {
  const m = loadTracking();
  m.L.navError({ tabId: 1, frameId: 0, error: 'Error code 2152398878',
                 url: 'https://nope.test/', timeStamp: 400 });

  const nav = m.TAB_NAV.get(1);
  assert.equal(nav.lastNavError, 'Error code 2152398878');
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
// tabs.onRemoved — every per-tab map is released
// =============================================================================

test('closing a tab releases EVERY per-tab buffer (no leak per closed tab)', () => {
  const m = loadTracking();
  const sink = loadConsoleSink(m.TAB_CONSOLE, m.CONSOLE_MAX);
  m.L.onBeforeRequest(req(1, { tabId: 1 }));
  sink({ type: 'console_entries', entries: [{ level: 'log', text: 'a', ts: 1 }] }, { tab: { id: 1 } });
  m.L.navCommitted({ tabId: 1, frameId: 0, url: 'https://x.test/', timeStamp: 900 });
  m.TAB_PROXY.set(1, { type: 'socks', host: 'h', port: 1 });
  m.L.onBeforeRequest(req(9, { tabId: 2 }));

  m.L.tabRemoved(1);

  for (const [name, map] of Object.entries({ TAB_REQUESTS: m.TAB_REQUESTS, TAB_NET: m.TAB_NET,
                                             TAB_NAV: m.TAB_NAV, TAB_CONSOLE: m.TAB_CONSOLE,
                                             TAB_PROXY: m.TAB_PROXY })) {
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
  // The click that triggered the request can be stamped a few ms AFTER the request
  // starts — the 50ms lead-in is what stops the action's own request being dropped.
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
      error: 'NS_ERROR_CONNECTION_REFUSED' },
    { id: '4', url: 'https://x.test/ok', method: 'GET', start: T0 + 7, end: T0 + 8, status: 204 },
  ]);

  const d = netDigestSince(1, T0);
  assert.equal(d.requests, 4);
  assert.deepEqual(d.failed, [
    { method: 'POST', url: 'https://x.test/login', status: 401, error: undefined },
    { method: 'GET', url: 'https://x.test/api', status: 500, error: undefined },
    { method: 'GET', url: 'https://dead.test/', status: undefined, error: 'NS_ERROR_CONNECTION_REFUSED' },
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

const REQ_ABORT = 'NS_BINDING_ABORTED';
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
      start: T0 + 20, end: T0 + 30, error: 'NS_ERROR_CONNECTION_REFUSED' },
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
  m.setTabs([{ id: 5, active: true, url: 'about:addons?tab=1', title: 'Add-ons' }]);

  const err = await m.resolveTabId({ tabId: 5 }, { requireContent: true }).then(() => null, (e) => e);
  assert.equal(err.code, 'CONTENT_UNREACHABLE');
  assert.match(err.message, /Tab 5 is a privileged page \(about:addons\)/, 'the query string is stripped');
  assert.match(err.message, /page_read\/page_snapshot\/page_act\/page_eval are unavailable/);
  assert.match(err.message, /page_screenshot returns the painted pixels/,
    'the privileged-safe alternatives are named so the agent is not just stuck');
  assert.match(err.message, /requests returns its network log/);
});

test('a privileged about:blank reports the failed navigation that PUT it there', async () => {
  const m = loadResolveTabId();
  m.setTabs([{ id: 5, active: true, url: 'about:blank', title: '' }]);
  m.TAB_NAV.set(5, { lastAttemptedUrl: 'https://nope.test/', lastNavError: 'NS_ERROR_UNKNOWN_HOST' });

  const err = await m.resolveTabId({ tabId: 5 }, { requireContent: true }).then(() => null, (e) => e);
  assert.equal(err.code, 'CONTENT_UNREACHABLE');
  // Without this, an agent sees "privileged page about:blank" and never learns DNS failed.
  assert.match(err.message, /The last attempted navigation was https:\/\/nope\.test\//);
  assert.match(err.message, /Last navigation error: HUMANIZED\(NS_ERROR_UNKNOWN_HOST\)/);
  assert.match(err.message, /Navigate it to an http\(s\) page first/);
});

test('a non-blank privileged page does NOT get the navigation-failure narrative', async () => {
  const m = loadResolveTabId();
  m.setTabs([{ id: 5, active: true, url: 'about:addons', title: 'Add-ons' }]);
  m.TAB_NAV.set(5, { lastAttemptedUrl: 'https://nope.test/', lastNavError: 'NS_ERROR_UNKNOWN_HOST' });

  const err = await m.resolveTabId({ tabId: 5 }, { requireContent: true }).then(() => null, (e) => e);
  assert.doesNotMatch(err.message, /last attempted navigation/,
    'about:addons is where the user went on purpose — a stale nav error is not the story');
  assert.match(err.message, /Navigate it to a normal http\(s\) page first/);
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

// ---- the background must survive a browser with no windows API ----------------
// Firefox for ANDROID has no windows API at all (every member is android:false in
// browser-compat-data). This block sits at TOP LEVEL, so an unguarded touch does not
// merely break window tracking: it throws during evaluation and kills the rest of the
// script. Function declarations hoist, so the extension keeps answering and the damage
// surfaces far away — a screenshot failed with "can't access lexical declaration
// 'VISIBLE_CAPTURE_TIMEOUT_MS' before initialization", 800 lines below, and the whole
// uiSurface block silently never ran. Evaluate the block against a phone-shaped
// `browser` and assert it completes.

function loadFocusTracking(hasWindows) {
  const block = slice(
    '// --- Most-recently-focused Firefox window tracking',
    '// --- Per-tab SOCKS proxy routing',
    'focused-window tracking');

  // eslint-disable-next-line no-new-func
  const factory = new Function('hasWindows', `
    const L = {};
    const browser = {};
    if (hasWindows) {
      browser.windows = {
        WINDOW_ID_NONE: -1,
        getLastFocused: async () => ({ id: 7 }),
        onFocusChanged: { addListener: (fn) => { L.focus = fn; } },
        onRemoved: { addListener: (fn) => { L.removed = fn; } },
      };
    }
    ${block}
    return { L, evaluated: true, get id() { return LAST_FOCUSED_WINDOW_ID; } };
  `);
  return factory(hasWindows);
}

test('the focused-window block evaluates on a browser with NO windows API', () => {
  // The whole point: this must not throw. A throw here is a dead background script on
  // every phone, and the failure shows up as unrelated features breaking.
  const out = loadFocusTracking(false);
  assert.equal(out.evaluated, true, 'the block ran to completion');
  assert.equal(out.L.focus, undefined, 'nothing may be registered against a missing API');
  assert.equal(out.L.removed, undefined);
  assert.equal(out.id, null, 'and no window is tracked');
});

test('with a windows API it still tracks focus as before', async () => {
  const out = loadFocusTracking(true);
  assert.equal(typeof out.L.focus, 'function', 'the listeners are registered on desktop');
  assert.equal(typeof out.L.removed, 'function');
  out.L.focus(12);
  assert.equal(out.id, 12);
  out.L.focus(-1);
  assert.equal(out.id, 12, 'WINDOW_ID_NONE is an intermediate event, never a clear');
});

test('no top-level browser.windows touch is left unguarded', () => {
  // The slice above proves the one block. This proves nobody adds another: a top-level
  // `browser.windows.` at column 0 is evaluated at load and takes the script down with
  // it on Android. Uses inside functions are fine — they run only when called.
  const offenders = BG_SRC.split(/\r?\n/)
    .map((line, i) => ({ line, n: i + 1 }))
    .filter(({ line }) => /^browser\.windows\./.test(line));
  assert.deepEqual(offenders, [],
    'guard it with HAS_WINDOWS: Firefox for Android has no windows API, and an '
    + 'unguarded top-level touch aborts the whole background script');
});

// ---- windows.* on a browser that has no windows API ---------------------------------
// Firefox for Android implements none of it. Before this guard every windows.* action
// handed the agent a raw TypeError ("can't access property \"create\", browser.windows is
// undefined"), which reads as a BrowserPlug bug rather than a platform fact. Verified
// live against Nightly 157 on 2026-09-09.

function loadWindowsHandler(hasWindows) {
  const block = slice(
    'async function handleWindowsAction(',
    'async function handleRequestsAction(',
    'windows handlers');

  // eslint-disable-next-line no-new-func
  const factory = new Function('hasWindows', `
    const console = { log() {}, warn() {}, error() {} };
    const sent = [];
    const sendError = (id, code, message) => sent.push({ id, code, message });
    const sendResponse = (id, action, data) => sent.push({ id, action, data });
    const browser = {};
    if (hasWindows) {
      browser.windows = {
        getAll: async () => [{ id: 1, focused: true }],
        create: async (o) => ({ id: 2, ...o }),
        getLastFocused: async () => ({ id: 1 }),
      };
    }
    ${block}
    return { sent, handleWindowsAction };
  `);
  return factory(hasWindows);
}

test('windows.* refuses with a platform fact when there is no windows API', async () => {
  const h = loadWindowsHandler(false);
  await h.handleWindowsAction('w1', 'windows.create', { url: 'https://example.org/' });
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].code, 'WINDOWS_UNSUPPORTED');
  assert.match(h.sent[0].message, /Firefox for Android/);
  assert.match(h.sent[0].message, /tabs actions/, 'it must name what DOES work');
  assert.doesNotMatch(h.sent[0].message, /undefined/,
    'never hand the agent the raw TypeError');
});

test('every windows action is covered, not just create', async () => {
  for (const action of ['windows.list', 'windows.get', 'windows.get_current',
                        'windows.get_last_focused', 'windows.close', 'windows.focus',
                        'windows.update']) {
    const h = loadWindowsHandler(false);
    await h.handleWindowsAction('w', action, {});
    assert.equal(h.sent[0].code, 'WINDOWS_UNSUPPORTED', action);
  }
});

test('with a windows API the handler still does its job', async () => {
  const h = loadWindowsHandler(true);
  await h.handleWindowsAction('w2', 'windows.list', {});
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].code, undefined, 'no refusal on a browser that has windows');
  assert.ok(h.sent[0].data, 'it answered with real data');
});

// ---- nothing at TOP LEVEL may touch an API Firefox for Android lacks ------------------
// This is the regression guard for the worst bug this extension has had on a phone: one
// unguarded `browser.windows` touch at top level threw during evaluation, and because
// function declarations hoist, the extension went on connecting and answering while every
// `const` below that line stayed in the temporal dead zone. Seven tools failed naming
// seven different constants; the cause was one line.
//
// Two shapes can do it, and both are checked: a statement at column 0 that touches a
// namespace, and a bare CALL at column 0 to a function that does (which is exactly what
// `reseedLastFocusedWindow()` was). An async IIFE is deliberately NOT a risk: a throw
// inside one rejects its promise and leaves evaluation alone.

// Shipped on Firefox for Android, so a top-level touch is safe. Anything outside this set
// must be guarded (`if (browser.x && browser.x.y)`) or moved inside a function.
const ANDROID_SAFE_NAMESPACES = new Set([
  'storage', 'runtime', 'webRequest', 'webNavigation', 'tabs', 'alarms', 'extension',
]);

const topLevelLines = () => BG_SRC.split(/\r?\n/)
  .map((line, i) => ({ line, n: i + 1 }))
  .filter(({ line }) => line.length && !/^\s/.test(line));

test('every UNGUARDED top-level browser.* touch is an API Android actually has', () => {
  const offenders = topLevelLines()
    .filter(({ line }) => /^(browser|chrome)\./.test(line))
    .map(({ line, n }) => ({ n, ns: (line.match(/^(?:browser|chrome)\.(\w+)/) || [])[1], line }))
    .filter(({ ns }) => ns && !ANDROID_SAFE_NAMESPACES.has(ns));
  assert.deepEqual(offenders.map(o => `${o.n}: ${o.ns}`), [],
    'guard it, or move it inside a function: an unguarded top-level touch of an API '
    + 'Firefox for Android lacks aborts the whole background script there');
});

test('no bare top-level CALL reaches an API Android lacks without guarding it', () => {
  // The shape the original bug took. For each bare call at column 0, pull the named
  // function out of the source and check that any missing-API touch inside it is
  // preceded by a bail-out.
  const MISSING = ['browser.windows', 'browser.menus', 'browser.sidebarAction',
                   'browser.contextMenus'];
  const calls = topLevelLines()
    .filter(({ line }) => /^[A-Za-z_$][\w$]*\s*\(/.test(line)
                          && !/^(if|for|while|switch|catch|function|return)\b/.test(line))
    .map(({ line, n }) => ({ n, name: line.match(/^([A-Za-z_$][\w$]*)/)[1] }));
  assert.ok(calls.length, 'the scan must actually be finding the calls');

  for (const { n, name } of calls) {
    const start = BG_SRC.search(new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`));
    assert.ok(start >= 0, `${name}() is called at line ${n} but not declared here`);
    let i = BG_SRC.indexOf('{', start) + 1;
    for (let depth = 1; depth > 0 && i < BG_SRC.length; i++) {
      if (BG_SRC[i] === '{') depth++;
      else if (BG_SRC[i] === '}') depth--;
    }
    const body = BG_SRC.slice(start, i);
    for (const api of MISSING) {
      if (!body.includes(api)) continue;
      const ns = api.split('.')[1];
      assert.match(body, new RegExp(`if \\(!browser\\.${ns}\\)|if \\(browser\\.${ns}`),
        `${name}() runs at top level (line ${n}) and touches ${api}, which Firefox for `
        + 'Android does not implement, without checking for it first — that throw kills '
        + 'the whole background script');
    }
  }
});

// =============================================================================
// The network-quiet clock and the settle wake-ups
// =============================================================================
// waitForSettle sleeps instead of polling, so the listeners that can end its wait must
// wake it; and a request that cannot put content in the page stays off its clock.

test('a beacon request is logged but kept off the network-quiet clock', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req('b', { type: 'beacon', timeStamp: 1000 }));
  assert.equal(m.TAB_REQUESTS.get(1).length, 1, 'requests still lists it');
  assert.equal(m.TAB_NET.has(1), false, 'it neither counts in flight nor moves lastActivity');
  m.L.onBeforeRequest(req('x'));
  m.L.onCompleted({ ...req('b', { type: 'beacon' }), statusCode: 204 });
  assert.equal(m.TAB_NET.get(1).inflight, 1, 'its completion does not end the xhr still in flight');
});

test('a request that ends wakes a sleeping settle on its tab', () => {
  const m = loadTracking();
  m.L.onBeforeRequest(req(1));
  m.L.onCompleted({ ...req(1), statusCode: 200 });
  m.L.onBeforeRequest(req(2));
  m.L.onErrorOccurred({ ...req(2), error: 'NS_ERROR_NET_RESET' });
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
