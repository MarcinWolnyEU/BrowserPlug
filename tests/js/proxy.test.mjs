/*
 * Unit tests for the per-tab SOCKS proxy feature in extension-gecko/background.js
 * parseProxyEndpoint / applyTabProxy / proxyDecision, PLUS the
 * fail-closed properties that keep a routed (.onion / Tor) tab from ever leaking direct:
 *   - persistence + restore of TAB_PROXY across a background-page restart, and the
 *     fail-CLOSED hold on proxyDecision while that restore is in flight;
 *   - a genuine browser restart discards stale tab-id routing (runtime.onStartup);
 *   - the routing decision is null-terminated so an unreachable SOCKS proxy ERRORS
 *     rather than failing over to a browser-defined (possibly direct) proxy.
 *
 * background.js is a top-level extension script (browser.* listeners run on load), so
 * — like requests-capture / observability — we slice the contiguous proxy block out of
 * the source and instantiate it with a stubbed `browser`. That stub (in-memory
 * storage.local, controllable tabs.get / onStartup / permission) is what lets us drive
 * the restore paths deterministically, browser-free.
 * Run with: npm run test:js  (== node --test "tests/js/*.test.mjs")
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

// A minimal, controllable WebExtension `browser` for the sliced proxy block.
function makeBrowserStub(opts = {}) {
  const storageData = JSON.parse(JSON.stringify(opts.storage || {}));
  const tabExists = opts.tabExists || (() => true); // (tabId) -> bool
  let startupCb = null;
  return {
    storage: {
      local: {
        get: (key) => Promise.resolve(
          Object.prototype.hasOwnProperty.call(storageData, key) ? { [key]: storageData[key] } : {}),
        set: (obj) => { Object.assign(storageData, obj); return Promise.resolve(); },
      },
    },
    runtime: {
      onStartup: {
        addListener: (cb) => { startupCb = cb; if (opts.fireStartupOnLoad) cb(); },
      },
    },
    tabs: {
      get: (id) => {
        // Hook to model onStartup firing WHILE restore is suspended on tabs.get —
        // the race the post-await recheck in restoreProxyState guards.
        if (opts.onTabsGet) opts.onTabsGet(id);
        return tabExists(id) ? Promise.resolve({ id })
                             : Promise.reject(new Error('No tab with id ' + id));
      },
    },
    // present so ensureProxyListener()'s guard (browser.proxy?.onRequest) is truthy
    proxy: { onRequest: { addListener() {}, removeListener() {}, hasListener() { return false; } } },
    __storageData: storageData,
    __fireStartup: () => { if (startupCb) startupCb(); },
  };
}

// Slice the proxy block (parseProxyEndpoint through proxyDecision, which now includes
// the persistence + restore helpers) and instantiate it against a stubbed browser.
// hasProxyPermission / ensureProxyListener live just past the slice end in background.js,
// so we provide test doubles for them.
function loadProxyHelpers(opts = {}) {
  const start = BG_SRC.indexOf('function parseProxyEndpoint');
  const end = BG_SRC.indexOf('// NOTE: Firefox refuses to proxy loopback', start);
  assert.ok(start >= 0 && end > start, 'proxy block not found in background.js');
  const block = BG_SRC.slice(start, end);
  // eslint-disable-next-line no-new-func
  const factory = new Function('browser', 'ext', `
    const TAB_PROXY = new Map();
    let _ensureCalls = 0;
    async function hasProxyPermission() { return ext.hasProxyPermission; }
    function ensureProxyListener() { _ensureCalls++; }
    ${block}
    return {
      parseProxyEndpoint, applyTabProxy, proxyDecision, proxyDecisionSync,
      persistTabProxy, setTabProxy, deleteTabProxy, clearTabProxy, restoreProxyState,
      TAB_PROXY,
      ready: () => _proxyRestorePromise,
      restoreDone: () => _proxyRestoreDone,
      ensureCalls: () => _ensureCalls,
    };
  `);
  const stub = makeBrowserStub(opts);
  const api = factory(stub, { hasProxyPermission: opts.hasProxyPermission !== false });
  api.stub = stub;
  api.persisted = () => stub.__storageData.browserplugTabProxy;
  return api;
}

// ---- proxy: parseProxyEndpoint, valid endpoints -----------------------------

test('proxy: parseProxyEndpoint accepts socks5 with host:port and enables remote DNS', () => {
  const r = loadProxyHelpers().parseProxyEndpoint('socks5://127.0.0.1:9050');
  assert.equal(r.ok, true);
  assert.equal(r.direct, false);
  assert.deepEqual(r.info, {
    type: 'socks', host: '127.0.0.1', port: 9050, proxyDNS: true,
    endpoint: 'socks5://127.0.0.1:9050',
  });
});

test('proxy: parseProxyEndpoint maps schemes: socks5h/socks -> socks, socks4 -> socks4', () => {
  const { parseProxyEndpoint } = loadProxyHelpers();
  assert.equal(parseProxyEndpoint('socks5h://h:1080').info.type, 'socks');
  assert.equal(parseProxyEndpoint('socks://h:1080').info.type, 'socks');
  assert.equal(parseProxyEndpoint('socks4://h:1080').info.type, 'socks4');
  assert.equal(parseProxyEndpoint('socks4a://h:1080').info.type, 'socks4');
  assert.equal(parseProxyEndpoint('SOCKS5://Host:9050').info.endpoint, 'socks5://Host:9050');
});

test('proxy: parseProxyEndpoint treats unset / "" / "direct" as direct (valid, no proxy)', () => {
  const { parseProxyEndpoint } = loadProxyHelpers();
  for (const v of [undefined, null, '', 'direct']) {
    const r = parseProxyEndpoint(v);
    assert.equal(r.ok, true, `${v} should be ok`);
    assert.equal(r.direct, true, `${v} should be direct`);
  }
});

test('proxy: parseProxyEndpoint rejects malformed / wrong-scheme / bad-port endpoints', () => {
  const { parseProxyEndpoint } = loadProxyHelpers();
  for (const bad of [
    '127.0.0.1:9050', 'http://1.2.3.4:8080', 'socks5://host', 'socks5://:9050',
    'socks5://h:0', 'socks5://h:70000', 'not-a-socks-url',
  ]) {
    const r = parseProxyEndpoint(bad);
    assert.equal(r.ok, false, `${bad} should be rejected`);
    assert.match(r.error, /Invalid proxy/i, `${bad} error should explain`);
  }
});

// ---- proxy: applyTabProxy ----------------------------------------------------

test('proxy: applyTabProxy sets, clears, rejects, and no-ops correctly', () => {
  const { applyTabProxy, TAB_PROXY } = loadProxyHelpers();
  assert.equal(applyTabProxy(1, 'socks5://h:1080'), null);
  assert.equal(TAB_PROXY.get(1).endpoint, 'socks5://h:1080');

  assert.equal(applyTabProxy(1, ''), null);       // '' clears
  assert.equal(TAB_PROXY.has(1), false);

  const err = applyTabProxy(2, 'nope');           // invalid -> error, no routing
  assert.match(err, /Invalid proxy/i);
  assert.equal(TAB_PROXY.has(2), false);

  applyTabProxy(3, 'socks5://h:1');               // undefined leaves it unchanged
  assert.equal(applyTabProxy(3, undefined), null);
  assert.equal(TAB_PROXY.get(3).endpoint, 'socks5://h:1');
});

// ---- proxy: the routing decision (fail-closed, null-terminated) --------------

test('proxy: proxyDecision routes a tab through ONLY its SOCKS proxy, terminated by null', async () => {
  const h = loadProxyHelpers();
  await h.ready();                                 // let the (empty) restore settle
  h.applyTabProxy(5, 'socks5://127.0.0.1:9050');

  const routed = h.proxyDecision({ tabId: 5 });
  // An ARRAY ending in null: Firefox tries the SOCKS proxy and, if it is unreachable,
  // does NOT fall over to a browser-defined (possibly direct) proxy — the request
  // errors instead of leaking to clearnet. A bare object would fail OPEN here.
  assert.ok(Array.isArray(routed), 'routed decision must be a null-terminated array');
  assert.deepEqual(routed[0], { type: 'socks', host: '127.0.0.1', port: 9050, proxyDNS: true });
  assert.equal(routed[routed.length - 1], null, 'must end in null to suppress browser-proxy failover');

  assert.deepEqual(h.proxyDecision({ tabId: 999 }), { type: 'direct' }); // unrouted -> direct
});

// ---- proxy: persistence + restore across a background restart ----------------

test('proxy: TAB_PROXY is persisted to storage.local on every change', () => {
  const h = loadProxyHelpers();
  h.setTabProxy(7, { type: 'socks', host: '127.0.0.1', port: 9050, proxyDNS: true,
                     endpoint: 'socks5://127.0.0.1:9050' });
  assert.ok(h.persisted() && h.persisted()['7'], 'set must persist');

  h.deleteTabProxy(7);
  assert.ok(!h.persisted()['7'], 'delete must persist the removal');
});

test('proxy: routing is RESTORED after a background restart (same session, tab still open)', async () => {
  // Simulate the prior session: a routed tab whose config is already in storage.
  const info = { type: 'socks', host: '127.0.0.1', port: 9050, proxyDNS: true,
                 endpoint: 'socks5://127.0.0.1:9050' };
  const h = loadProxyHelpers({ storage: { browserplugTabProxy: { 42: info } }, tabExists: () => true });
  await h.ready();
  assert.deepEqual(h.TAB_PROXY.get(42), info, 'the routed tab must keep its proxy after restart');
  assert.ok(h.ensureCalls() >= 1, 'the routing listener must be re-armed');
});

test('proxy: proxyDecision FAIL-CLOSES over the restore window (never direct before restore)', async () => {
  const info = { type: 'socks', host: '127.0.0.1', port: 9050, proxyDNS: true,
                 endpoint: 'socks5://127.0.0.1:9050' };
  const h = loadProxyHelpers({ storage: { browserplugTabProxy: { 42: info } }, tabExists: () => true });
  // Decide BEFORE awaiting restore — this is the exact window a leak would happen in.
  assert.equal(h.restoreDone(), false, 'precondition: restore still pending');
  const decision = h.proxyDecision({ tabId: 42 });
  assert.equal(typeof decision.then, 'function', 'must hold the request (a Promise), not answer direct');
  const resolved = await decision;
  assert.ok(Array.isArray(resolved) && resolved[0].port === 9050,
    'once restored, the held request routes through the proxy — never leaked direct');
});

test('proxy: restore DROPS a routed tab that was closed while the background was down', async () => {
  const info = { type: 'socks', host: 'h', port: 1, proxyDNS: true, endpoint: 'socks5://h:1' };
  const h = loadProxyHelpers({ storage: { browserplugTabProxy: { 99: info } }, tabExists: () => false });
  await h.ready();
  assert.equal(h.TAB_PROXY.has(99), false, 'a gone tab must not be restored');
  assert.ok(!h.persisted() || !h.persisted()['99'], 'and the dead entry is pruned from storage');
});

test('proxy: a browser restart before restore reads storage DISCARDS stale routing', async () => {
  const info = { type: 'socks', host: 'h', port: 1, proxyDNS: true, endpoint: 'socks5://h:1' };
  // fireStartupOnLoad: onStartup fires at listener registration, before restore's read.
  const h = loadProxyHelpers({ storage: { browserplugTabProxy: { 42: info } },
                               tabExists: () => true, fireStartupOnLoad: true });
  await h.ready();
  assert.equal(h.TAB_PROXY.size, 0, 'stale routing from a previous session must not be re-applied');
  assert.deepEqual(h.persisted(), {}, 'and the persisted copy is cleared on browser start');
});

test('proxy: a browser restart DURING restore (mid tabs.get) still discards — the guard', async () => {
  // This is the ordering the top-of-loop check alone would MISS: restore has already
  // read storage and passed the initial guard; onStartup fires while it is suspended on
  // tabs.get. Without the post-await recheck, the set() below would resurrect the stale
  // entry onStartup just cleared. Fire startup exactly once, on the first tabs.get.
  const info = { type: 'socks', host: 'h', port: 1, proxyDNS: true, endpoint: 'socks5://h:1' };
  let fired = false;
  const opts = { storage: { browserplugTabProxy: { 42: info } }, tabExists: () => true };
  opts.onTabsGet = () => { if (!fired) { fired = true; opts.__stub.__fireStartup(); } };
  const h = loadProxyHelpers(opts);
  opts.__stub = h.stub;                          // let the hook reach the stub
  await h.ready();
  assert.ok(fired, 'precondition: startup fired during the restore loop');
  assert.equal(h.TAB_PROXY.size, 0, 'an entry must NOT be resurrected after a mid-restore restart');
  assert.deepEqual(h.persisted(), {}, 'storage stays cleared');
});

test('proxy: restore refuses to route when the proxy permission is no longer held', async () => {
  const info = { type: 'socks', host: 'h', port: 1, proxyDNS: true, endpoint: 'socks5://h:1' };
  const h = loadProxyHelpers({ storage: { browserplugTabProxy: { 42: info } },
                               tabExists: () => true, hasProxyPermission: false });
  await h.ready();
  assert.equal(h.TAB_PROXY.size, 0, 'cannot enforce routing without the permission -> do not pretend');
  assert.deepEqual(h.persisted(), {}, 'unenforceable persisted routing is cleared');
});
