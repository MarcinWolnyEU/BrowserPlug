/*
 * Unit tests for the connection journal and the cached hello, chromium twin
 * (extension-chromium/connection-journal.js + the connection block of
 * extension-chromium/background.js).
 *
 * The wedge these exist for was a Firefox one (2026-09-21: the WebSocket dropped, two
 * reconnects opened a socket and were dropped for "no valid extension_hello within 5s",
 * and only a browser restart recovered it), but the shape is not engine-specific. This
 * worker builds its hello from the same four awaits and can be evicted mid-outage, so
 * it caches the same way and keeps the same record.
 *
 * The module under test is shared and byte-identical; the glue is not, which is why
 * this is a twin rather than one shared run. Chromium's differences show up in the
 * hello payload (engine: "blink", a synthesized browserInfo) and in where the file is
 * loaded from (importScripts, not a manifest background.scripts array).
 *
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
// The connection block sends through the wire this registers on globalThis.
import '../../extension-chromium/ws-crypto.js';
// The re-keying guard (review S5) the connection block delegates to.
import '../../extension-chromium/token-trust.js';
import { spyWires } from '../js/wire-spy.mjs';
const wires = spyWires(globalThis.BrowserplugWsCrypto);
import { randomUUID } from 'node:crypto';

const JOURNAL_SRC = fs.readFileSync(
  new URL('../../extension-chromium/connection-journal.js', import.meta.url), 'utf-8');
const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');

const RETRY_MS = 7777;

// ---- loading the shared module ------------------------------------------------
// connection-journal.js is an IIFE that writes to globalThis. Shadowing globalThis
// with a plain object gives each load its own registry, and keeps every value it
// returns in THIS realm so deepEqual can compare it.
function loadJournalModule(browserStub) {
  const g = {};
  if (browserStub) g.browser = browserStub;
  // eslint-disable-next-line no-new-func
  const factory = new Function('globalThis', 'console', `${JOURNAL_SRC}\nreturn globalThis;`);
  return factory(g, { log() {}, warn() {}, error() {} });
}

// A storage area shaped like browser.storage.*: get(defaults) merges stored values
// over the defaults. `gate` holds every read open until release() is called, which is
// how a browser too busy to answer a storage request is modelled.
function makeStorageArea(backing = {}, opts = {}) {
  const held = [];
  const area = {
    store: backing,
    sets: 0,
    gets: 0,
    fail: false,
    gate: false,
    setThrows: !!opts.setThrows,
    async get(defaults) {
      area.gets++;
      if (area.gate) await new Promise((r) => held.push(r));
      if (area.fail) throw new Error('storage unavailable');
      const out = {};
      const keys = defaults ? Object.keys(defaults) : Object.keys(area.store);
      for (const k of keys) {
        out[k] = (k in area.store) ? area.store[k] : (defaults ? defaults[k] : undefined);
      }
      return out;
    },
    async set(obj) {
      if (area.setThrows) throw new Error('quota exceeded');
      if (area.fail) throw new Error('storage unavailable');
      area.sets++;
      Object.assign(area.store, obj);
    },
    // Opens the gate and lets through anything already waiting. A read that has not
    // reached the gate yet must not be caught by it either, or a chain parked behind
    // an earlier await would hang forever.
    release() { area.gate = false; while (held.length) held.shift()(); }
  };
  return area;
}

const settle = async (n = 6) => {
  for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r));
};

// ---- the ring ------------------------------------------------------------------

test('the ring holds the last 30 events and drops the oldest', () => {
  const { ConnectionJournal } = loadJournalModule();
  const j = ConnectionJournal.createJournal({});
  for (let i = 0; i < 35; i++) j.note('retry', { attempt: i });

  const all = j.all();
  assert.equal(all.length, 30, 'capped at the ring size');
  assert.equal(all[0].attempt, 5, 'the first five were evicted');
  assert.equal(all[29].attempt, 34, 'the newest is last');
});

test('every append is written through to storage.local', async () => {
  const area = makeStorageArea();
  const { ConnectionJournal } = loadJournalModule();
  const j = ConnectionJournal.createJournal({ storage: area });

  j.note('open');
  j.note('close', { code: 1006 });
  await settle(2);

  const stored = area.store[ConnectionJournal.STORAGE_KEY];
  assert.equal(Array.isArray(stored), true, 'the key holds an array');
  assert.deepEqual(stored.map((e) => e.e), ['open', 'close'],
    'both events reached storage without waiting for a flush');
  assert.equal(stored[1].code, 1006);
});

test('a storage write that throws costs the write, not the event', async () => {
  const area = makeStorageArea({}, { setThrows: true });
  const { ConnectionJournal } = loadJournalModule();
  const j = ConnectionJournal.createJournal({ storage: area });

  j.note('open');
  j.note('error', { reason: 'socket error' });
  await settle(2);

  assert.deepEqual(j.all().map((e) => e.e), ['open', 'error'],
    'the in-memory ring is intact');
  assert.equal(ConnectionJournal.STORAGE_KEY in area.store, false, 'nothing was stored');
});

test('a journal with no storage at all still records', () => {
  const { ConnectionJournal } = loadJournalModule();
  const j = ConnectionJournal.createJournal({ storage: null });
  j.note('open');
  assert.equal(j.all().length, 1);
});

// ---- the entry shape -----------------------------------------------------------

test('an entry carries t/e always, and code/reason/attempt only when given', () => {
  const { ConnectionJournal } = loadJournalModule();
  const j = ConnectionJournal.createJournal({ now: () => 1700000000123.7 });

  const bare = j.note('open');
  assert.deepEqual(Object.keys(bare).sort(), ['e', 't']);
  assert.equal(bare.t, 1700000000123, 'the timestamp is an integer of epoch ms');
  assert.equal(Number.isInteger(bare.t), true);

  const full = j.note('close', { code: '1006', reason: 'going away', attempt: 2 });
  assert.deepEqual(full, { t: 1700000000123, e: 'close', code: 1006, reason: 'going away', attempt: 2 });
  assert.equal(Number.isInteger(full.code), true, 'a numeric string is coerced to an integer');
});

test('a reason longer than 120 characters is cut to 120', () => {
  const { ConnectionJournal } = loadJournalModule();
  const j = ConnectionJournal.createJournal({});
  const long = 'hello timeout: ' + 'x'.repeat(400);

  const entry = j.note('rejected', { code: 4401, reason: long });
  assert.equal(entry.reason.length, ConnectionJournal.MAX_REASON);
  assert.equal(entry.reason.length, 120);
  assert.equal(entry.reason.startsWith('hello timeout: '), true,
    'the cut keeps the head, so the phrase the server leads with survives');
});

test('an empty or missing reason leaves the key off entirely', () => {
  const { ConnectionJournal } = loadJournalModule();
  const j = ConnectionJournal.createJournal({});
  assert.equal('reason' in j.note('close', { reason: '' }), false);
  assert.equal('reason' in j.note('close', { reason: null }), false);
  assert.equal('code' in j.note('close', {}), false);
});

// ---- the since-last-hello window ------------------------------------------------

test('a hello reports what happened since the previous hello, and nothing older', () => {
  const { ConnectionJournal } = loadJournalModule();
  const j = ConnectionJournal.createJournal({});

  j.note('open');
  assert.deepEqual(j.sinceLastHello().map((e) => e.e), ['open']);

  j.noteHelloSent();
  assert.deepEqual(j.sinceLastHello(), [], 'the window is empty right after a hello');

  j.note('close', { code: 1006 });
  j.note('retry', { attempt: 1 });
  j.note('open');
  assert.deepEqual(j.sinceLastHello().map((e) => e.e), ['close', 'retry', 'open']);

  j.noteHelloSent();
  assert.deepEqual(j.sinceLastHello(), []);
});

test('the hello window keeps the NEWEST 20 when more than 20 happened', () => {
  const { ConnectionJournal } = loadJournalModule();
  const j = ConnectionJournal.createJournal({});
  j.noteHelloSent();
  for (let i = 0; i < 26; i++) j.note('retry', { attempt: i });

  const window = j.sinceLastHello();
  assert.equal(window.length, ConnectionJournal.MAX_IN_HELLO);
  assert.equal(window.length, 20);
  assert.equal(window[0].attempt, 6, 'the oldest six are dropped');
  assert.equal(window[19].attempt, 25,
    'an outage of 26 events is better described by its last 20 than its first 20');
});

test('the hello itself is journalled, so it shows up in the NEXT window', () => {
  const { ConnectionJournal } = loadJournalModule();
  const j = ConnectionJournal.createJournal({});
  j.note('open');
  j.noteHelloSent();
  j.note('close');

  assert.deepEqual(j.all().map((e) => e.e), ['open', 'hello', 'close']);
  assert.deepEqual(j.sinceLastHello().map((e) => e.e), ['close']);
});

// ---- surviving a force-kill -----------------------------------------------------

test('load() brings back the tail a killed browser left in storage', async () => {
  const stored = [
    { t: 1, e: 'open' },
    { t: 2, e: 'hello' },
    { t: 3, e: 'close', code: 1006 },
    { t: 4, e: 'retry', attempt: 1 },
    { t: 5, e: 'rejected', code: 4401, reason: 'hello timeout: none in 15s' }
  ];
  const { ConnectionJournal } = loadJournalModule();
  const area = makeStorageArea({ [ConnectionJournal.STORAGE_KEY]: stored });
  const j = ConnectionJournal.createJournal({ storage: area });

  await j.load();

  assert.deepEqual(j.all().map((e) => e.e), ['open', 'hello', 'close', 'retry', 'rejected']);
  assert.deepEqual(j.sinceLastHello().map((e) => e.e), ['close', 'retry', 'rejected'],
    'the previous life reported everything up to its last hello; the rest is news');
  assert.equal(j.sinceLastHello()[2].reason, 'hello timeout: none in 15s');
});

test('a stored tail with no hello in it counts entirely as unreported', async () => {
  const { ConnectionJournal } = loadJournalModule();
  const area = makeStorageArea({
    [ConnectionJournal.STORAGE_KEY]: [{ t: 1, e: 'close' }, { t: 2, e: 'retry' }]
  });
  const j = ConnectionJournal.createJournal({ storage: area });
  await j.load();
  assert.equal(j.pendingCount(), 2);
});

test('a corrupt or foreign stored value degrades to an empty journal', async () => {
  const { ConnectionJournal } = loadJournalModule();
  for (const value of ['not an array', 42, { e: 'open' }, [null, 7, { nope: true }]]) {
    const area = makeStorageArea({ [ConnectionJournal.STORAGE_KEY]: value });
    const j = ConnectionJournal.createJournal({ storage: area });
    await j.load();
    assert.deepEqual(j.all(), [], `a stored ${typeof value} is ignored, not thrown on`);
  }
});

test('a storage read that throws leaves the journal usable', async () => {
  const { ConnectionJournal } = loadJournalModule();
  const area = makeStorageArea();
  area.fail = true;
  const errors = [];
  const j = ConnectionJournal.createJournal({ storage: area, onError: (e) => errors.push(e) });

  await j.load();

  assert.equal(errors.length, 1, 'the failure is reported');
  j.note('open');
  assert.equal(j.all().length, 1, 'and recording carries on');
});

// ---- the singleton the background reaches for ------------------------------------

test('loading the file registers a singleton bound to this browser storage.local', async () => {
  const stored = [{ t: 1, e: 'close', code: 1006 }];
  const area = makeStorageArea({ connectionJournal: stored });
  const g = loadJournalModule({ storage: { local: area } });

  assert.ok(g.browserplugJournal, 'background.js finds it on globalThis');
  await settle(2);
  assert.deepEqual(g.browserplugJournal.all().map((e) => e.e), ['close'],
    'the module loads the stored tail itself, with nothing to wire up at the call site');

  g.browserplugJournal.note('open');
  await settle(2);
  assert.deepEqual(area.store.connectionJournal.map((e) => e.e), ['close', 'open'],
    'and writes through to the same area');
});

test('recent(n) returns the last n entries for the popup', () => {
  const { ConnectionJournal } = loadJournalModule();
  const j = ConnectionJournal.createJournal({});
  for (let i = 0; i < 14; i++) j.note('retry', { attempt: i });
  assert.deepEqual(j.recent(10).map((e) => e.attempt), [4, 5, 6, 7, 8, 9, 10, 11, 12, 13]);
  assert.equal(j.recent(3).length, 3);
});

// ---- the background glue ----------------------------------------------------------
// Everything above is the shared module. Below is the connection block of this tree's
// background.js, driven with fake sockets, timers and storage — because a green module
// proves nothing about a call site that was never wired up.

// What the service worker reads out of self.navigator to synthesize browserInfo.
const CHROME_BRANDS = [
  { brand: 'Not;A=Brand', version: '99' },
  { brand: 'Chromium', version: '151' },
  { brand: 'Google Chrome', version: '151' }
];
const CHROME_FULL_VERSIONS = [
  { brand: 'Not;A=Brand', version: '99.0.0.0' },
  { brand: 'Chromium', version: '151.0.7100.5' },
  { brand: 'Google Chrome', version: '151.0.7100.5' }
];

function makeNavigator() {
  return {
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0.7100.5 Safari/537.36',
    vendor: 'Google Inc.',
    userAgentData: {
      brands: CHROME_BRANDS,
      mobile: false,
      platform: 'Windows',
      getHighEntropyValues: async () => ({
        fullVersionList: CHROME_FULL_VERSIONS,
        platform: 'Windows', platformVersion: '15.0.0', architecture: 'x86', bitness: '64',
        uaFullVersion: '151.0.7100.5'
      })
    }
  };
}

function loadModule(opts = {}) {
  const start = BG_SRC.indexOf('function connectToMCPServer()');
  const end = BG_SRC.indexOf('// --- Server-health probe over the WS', start);
  assert.ok(start >= 0 && end > start, 'connection block not found in background.js');
  const block = BG_SRC.slice(start, end);

  const journalArea = opts.journalArea || makeStorageArea();
  const { ConnectionJournal } = loadJournalModule();
  const journal = opts.journal || ConnectionJournal.createJournal({ storage: journalArea });

  const deps = {
    local: opts.local || makeStorageArea(),
    sync: opts.sync || makeStorageArea(),
    manifestVersion: opts.manifestVersion || '1.8.8',
    platformInfo: { os: 'win', arch: 'x86-64' },
    navigator: makeNavigator(),
    serverToken: opts.serverToken || randomUUID(),
    randomUUID
  };

  // eslint-disable-next-line no-new-func
  const factory = new Function('__deps', 'globalThis', `
    const console = { log() {}, debug() {}, warn(...a) { warnings.push(a.join(' ')); },
                      error(...a) { errors.push(a.join(' ')); }, trace() {} };
    const errors = [];
    const warnings = [];
    const sockets = [];
    const fetchCalls = [];
    const permAdded = [];
    const permRemoved = [];
    const storageWatchers = [];

    function WebSocket(url) {
      this.url = url;
      this.readyState = WebSocket.CONNECTING;
      this.sent = [];
      sockets.push(this);
    }
    WebSocket.CONNECTING = 0; WebSocket.OPEN = 1; WebSocket.CLOSING = 2; WebSocket.CLOSED = 3;
    WebSocket.prototype.send = function (m) {
      if (this.readyState !== WebSocket.OPEN) throw new Error('InvalidStateError');
      this.sent.push(m);
    };
    WebSocket.prototype.close = function () { this.readyState = WebSocket.CLOSING; };
    WebSocket.prototype.fireOpen = function () {
      this.readyState = WebSocket.OPEN;
      if (this.onopen) this.onopen();
    };
    WebSocket.prototype.fireClose = function (code, reason) {
      this.readyState = WebSocket.CLOSED;
      if (this.onclose) this.onclose({ code: code === undefined ? 1006 : code, reason: reason || '' });
    };
    WebSocket.prototype.fireError = function (message) {
      if (this.onerror) this.onerror({ message: message || 'socket error' });
    };

    let timerSeq = 1;
    const timers = new Map();
    function setTimeout(fn, ms) { const id = timerSeq++; timers.set(id, { fn, ms }); return id; }
    function clearTimeout(id) { timers.delete(id); }
    function setInterval() { return 0; }
    function clearInterval() {}

    async function fetch(url) {
      fetchCalls.push(url);
      return { ok: true, status: 200, json: async () => ({ token: __deps.serverToken }) };
    }

    let websocket = null;
    let isConnected = false;
    let retryAttempts = 0;
    let connectionRetryAttempts = 0;
    let reconnectTimer = null;
    let connectionPhase = 'connecting';
    let nextRetryAt = null;
    let stoppedByUser = false;
    let serverHealthy = null;
    let healthProbeFailures = 0;
    let healthOkThisConnection = false;
    let lastServerMcpPort = null;
    const MAX_ABSOLUTE_RETRIES = 50;
    const CONFIG = { hostname: 'localhost', port: 8772, retryInterval: ${RETRY_MS},
                     maxRetries: -1, pingTimeout: 5000 };

    function setConnectionPhase(p) { connectionPhase = p; }
    function updateActionIndicator() {}
    function startHealthProbe() {}
    function sendBrowserState() {}
    function stopHealthProbe() {}
    function clearBrowserProxy() {}
    async function hasProxyPermission() { return false; }
    async function handleMessage() {}
    // getProxyPermissionState lives INSIDE this block on chromium (it is a constant
    // refusal, not a permission read), so it is not stubbed here.
    const self = { navigator: __deps.navigator };
    const window = {};
    const crypto = { randomUUID: () => __deps.randomUUID() };
    const browser = {
      runtime: {
        getManifest: () => ({ version: __deps.manifestVersion }),
        getPlatformInfo: async () => __deps.platformInfo
      },
      extension: { isAllowedFileSchemeAccess: async () => true },
      action: {},
      permissions: {
        onAdded: { addListener: (fn) => permAdded.push(fn) },
        onRemoved: { addListener: (fn) => permRemoved.push(fn) }
      },
      storage: {
        sync: __deps.sync,
        local: __deps.local,
        onChanged: { addListener: (fn) => storageWatchers.push(fn) }
      }
    };

    ${block}

    return {
      connectToMCPServer, sendExtensionHello, sendExtensionHelloNow, disconnect,
      invalidateHelloCache, isHelloTimeoutReason,
      journalNote, journalSinceLastHello, journalRecent,
      sockets, errors, warnings, fetchCalls,
      phase: () => connectionPhase,
      listenerCounts: () => ({ added: permAdded.length, removed: permRemoved.length,
                               storage: storageWatchers.length }),
      firePermissionAdded: (perms) => permAdded.forEach((fn) => fn(perms || { permissions: ['proxy'] })),
      firePermissionRemoved: (perms) => permRemoved.forEach((fn) => fn(perms || { permissions: ['proxy'] })),
      fireStorageChanged: (changes, area) => storageWatchers.forEach((fn) => fn(changes, area)),
      hellos: () => sockets.flatMap(s => s.sent.filter(f => typeof f === 'string').map(JSON.parse))
        .filter(m => m.type === 'extension_hello'),
      helloOn: (i) => sockets[i].sent.filter(f => typeof f === 'string').map(JSON.parse).filter(m => m.type === 'extension_hello'),
      fireReconnectTimer: () => {
        for (const [id, t] of timers) {
          if (t.ms === ${RETRY_MS}) { timers.delete(id); t.fn(); return true; }
        }
        return false;
      }
    };
  `);
  const m = factory(deps, { browserplugJournal: journal,
                           BrowserplugWsCrypto: globalThis.BrowserplugWsCrypto,
                           BrowserplugTokenTrust: globalThis.BrowserplugTokenTrust });
  m.deps = deps;
  m.journal = journal;
  m.journalArea = journalArea;
  return m;
}

async function connected(opts) {
  const m = loadModule(opts);
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();
  return m;
}

// ---- the cheap hello ---------------------------------------------------------------

test('with nothing cached, the first hello waits on storage — the case the cache fixes', async () => {
  const sync = makeStorageArea();
  sync.gate = true;
  const m = loadModule({ sync });

  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();
  assert.equal(m.helloOn(0).length, 0, 'a first connect has nothing to send from');

  sync.release();
  await settle();
  assert.equal(m.helloOn(0).length, 1, 'and goes out once storage answers');
});

test('a reconnect hello is on the wire before any storage read resolves', async () => {
  const sync = makeStorageArea();
  const m = loadModule({ sync });
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();
  assert.equal(m.helloOn(0).length, 1, 'the first connect filled the cache');

  // The browser is now too busy to answer a storage request. This is the 2026-09-21
  // shape: the socket opens, and the hello has to beat the server's deadline.
  sync.gate = true;
  m.deps.local.gate = true;
  m.sockets[0].fireClose();
  assert.equal(m.fireReconnectTimer(), true, 'the reconnect loop is armed');
  m.sockets[1].fireOpen();

  // No await anywhere between fireOpen() and here.
  assert.equal(m.helloOn(1).length, 1, 'the reconnected socket announced itself synchronously');
  assert.equal(m.helloOn(1)[0].data.instance_id, m.helloOn(0)[0].data.instance_id);
  assert.equal(m.helloOn(1)[0].data.version, '1.8.8');
});

test('the cached hello carries the same payload the live one built', async () => {
  const token = randomUUID();
  const m = await connected({ sync: makeStorageArea({ connectionToken: token }) });
  const live = wires.encryptedHelloOf(m.sockets[0]).data;

  m.sockets[0].fireClose();
  m.fireReconnectTimer();
  m.sockets[1].fireOpen();
  // The cached inputs start the handshake on the same tick, with the same token.
  assert.equal(wires.tokenOf(m.sockets[1]), token);
  assert.ok(wires.isRequestFrame(m.sockets[1].sent[0]), 'the handshake request is already out');
  const cached = wires.encryptedHelloOf(m.sockets[1]).data;

  assert.equal(cached.engine, 'blink', 'the engine discriminator survives the cache');
  assert.deepEqual(cached.fileAccess, live.fileAccess);
  assert.deepEqual(cached.proxyAccess, live.proxyAccess);
  assert.deepEqual(cached.browserInfo, live.browserInfo);
  assert.deepEqual(cached.platform, live.platform);
  assert.equal(cached.tokenBootstrap, true);
});

test('a granted or revoked permission invalidates the cache', async () => {
  const sync = makeStorageArea();
  const m = await connected({ sync });
  assert.ok(m.listenerCounts().added >= 1, 'the grant listener is registered');
  assert.ok(m.listenerCounts().removed >= 1, 'so is the revoke listener');

  sync.gate = true;                 // the refresh cannot land
  m.firePermissionAdded();

  m.sockets[0].fireClose();
  m.fireReconnectTimer();
  m.sockets[1].fireOpen();
  assert.equal(m.helloOn(1).length, 0,
    'a refreshed grant must never ride a hello built before it');

  sync.release();
  await settle();
  assert.equal(m.helloOn(1).length, 1, 'the slow path still gets the hello out');
});

test('either token key changing invalidates the cache', async () => {
  for (const [changes, area] of [
    // Minted, never written down: a literal here would be a token value in the repo.
    [{ connectionToken: { newValue: randomUUID() } }, 'sync'],
    [{ autoToken: { newValue: randomUUID() } }, 'local']
  ]) {
    const sync = makeStorageArea();
    const m = await connected({ sync });
    assert.ok(m.listenerCounts().storage >= 1, 'the storage watcher is registered');

    sync.gate = true;
    m.fireStorageChanged(changes, area);

    m.sockets[0].fireClose();
    m.fireReconnectTimer();
    m.sockets[1].fireOpen();
    assert.equal(m.helloOn(1).length, 0, `a ${area} token change forces a fresh read`);

    sync.release();
    await settle();
    assert.equal(m.helloOn(1).length, 1);
  }
});

test('an unrelated storage change leaves the cache alone', async () => {
  const sync = makeStorageArea();
  const m = await connected({ sync });

  sync.gate = true;
  m.fireStorageChanged({ uiSurface: { newValue: 'sidebar' } }, 'sync');

  m.sockets[0].fireClose();
  m.fireReconnectTimer();
  m.sockets[1].fireOpen();
  assert.equal(m.helloOn(1).length, 1, 'still the fast path');
});

test('flipping the proxy consent sends a hello at once, both ways', async () => {
  // The server reads the grant from the last hello before it starts Tor for proxy="tor".
  const m = await connected();
  assert.equal(m.helloOn(0).length, 1);
  m.fireStorageChanged({ allowBrowserProxy: { newValue: true } }, 'local');
  await settle();
  assert.equal(m.helloOn(0).length, 2, 'turning it on is announced without a reconnect');
  m.fireStorageChanged({ allowBrowserProxy: { newValue: false } }, 'local');
  await settle();
  assert.equal(m.helloOn(0).length, 3, 'and so is turning it off');
});

// ---- the journal on the wire ---------------------------------------------------------

test('the hello carries the events since the previous hello', async () => {
  const m = await connected();
  assert.deepEqual(m.helloOn(0)[0].data.journal.map((e) => e.e), ['open'],
    'the first hello reports the socket that carried it');

  m.sockets[0].fireClose(1006, 'no close frame received or sent');
  assert.equal(m.fireReconnectTimer(), true);
  m.sockets[1].fireOpen();
  await settle();

  const journal = m.helloOn(1)[0].data.journal;
  assert.deepEqual(journal.map((e) => e.e), ['close', 'retry', 'open'],
    'the whole outage, oldest first');
  assert.equal(journal[0].code, 1006);
  assert.equal(journal[0].reason, 'no close frame received or sent');
  assert.equal(journal[1].attempt, 1, 'the retry says which attempt it was');
  assert.equal(journal[1].reason, `${RETRY_MS}ms`, 'and how long the wait is');
});

test('a hello with nothing to report omits the journal key entirely', async () => {
  const m = await connected();
  await m.sendExtensionHello();          // a second hello, nothing in between
  await settle();

  const hellos = m.helloOn(0);
  assert.equal(hellos.length, 2);
  assert.equal('journal' in hellos[1].data, false,
    'a key that is present and empty on every healthy hello is noise');
});

test('a socket error is journalled and reaches the next hello', async () => {
  const m = await connected();
  m.sockets[0].fireError('connection reset');
  m.sockets[0].fireClose(1006, '');
  assert.equal(m.fireReconnectTimer(), true);
  m.sockets[1].fireOpen();
  await settle();

  assert.deepEqual(m.helloOn(1)[0].data.journal.map((e) => e.e),
    ['error', 'close', 'retry', 'open']);
  assert.equal(m.helloOn(1)[0].data.journal[0].reason, 'connection reset');
  // A server that is not up yet is a normal state. Logged at error level it would land in
  // the extension's error list; the journal entry above is the record instead.
  assert.deepEqual(m.errors, [], 'a socket error is not console.error output');
});

test('the journal written through storage is what a killed browser would leave behind', async () => {
  const m = await connected();
  m.sockets[0].fireClose(1006, '');
  await settle(2);

  const stored = m.journalArea.store.connectionJournal;
  assert.deepEqual(stored.map((e) => e.e), ['open', 'hello', 'close', 'retry'],
    'every event reached storage.local as it happened');
});

test('journalRecent feeds the popup the tail of the ring', async () => {
  const m = await connected();
  m.sockets[0].fireClose(1006, '');
  const recent = m.journalRecent(10);
  assert.deepEqual(recent.map((e) => e.e), ['open', 'hello', 'close', 'retry']);
});

// ---- the timeout-aware 4401 ------------------------------------------------------------

test('a hello-timeout 4401 reconnects without spending a token fetch', async () => {
  const m = await connected();
  const before = m.deps.sync.gets;

  m.sockets[0].fireClose(4401, 'hello timeout: no extension_hello within 15s');
  await settle();

  assert.deepEqual(m.fetchCalls, [], 'the token endpoint was never called');
  assert.equal(m.deps.sync.gets, before, 'nor was the token re-read from storage');
  assert.equal(m.fireReconnectTimer(), true, 'a reconnect is armed instead');
  assert.ok(m.warnings.some((w) => /late hello/.test(w)),
    'and the console says which 4401 this was');

  m.sockets[1].fireOpen();
  assert.equal(m.helloOn(1).length, 1, 'whose hello goes out at once, from the cache');
});

test('the rejection is journalled with its code and reason', async () => {
  const m = await connected();
  m.sockets[0].fireClose(4401, 'hello timeout: no extension_hello within 15s');
  await settle();

  const rejected = m.journal.all().filter((e) => e.e === 'rejected');
  assert.equal(rejected.length, 1);
  assert.equal(rejected[0].code, 4401);
  assert.equal(rejected[0].reason, 'hello timeout: no extension_hello within 15s');
  assert.equal(m.journal.all().some((e) => e.e === 'close'), false,
    'a 4401 is journalled once, as a rejection, not twice');
});

test('an ordinary token 4401 still fetches the token', async () => {
  const m = await connected();
  m.sockets[0].fireClose(4401, 'token required: GET http://127.0.0.1:2830/ws-token');
  await settle();

  assert.equal(m.fetchCalls.length, 1, 'the endpoint named in the reason was called');
  assert.equal(m.deps.local.store.autoToken, m.deps.serverToken, 'and the token stored');
  const fetches = m.journal.all().filter((e) => e.e === 'token_fetch');
  assert.deepEqual(fetches.map((e) => e.reason), ['stored']);
  assert.ok(m.journal.all().some((e) => e.e === 'reconnect' && e.reason === 'new token'));
});

test('isHelloTimeoutReason keys on the phrase the server leads with, and nothing else', () => {
  const m = loadModule();
  assert.equal(m.isHelloTimeoutReason('hello timeout: no extension_hello within 15s'), true);
  assert.equal(m.isHelloTimeoutReason('  Hello Timeout: 15s  '), true, 'case and padding');
  assert.equal(m.isHelloTimeoutReason('token required: GET http://127.0.0.1:2830/ws-token'), false);
  assert.equal(m.isHelloTimeoutReason(''), false);
  assert.equal(m.isHelloTimeoutReason(undefined), false);
  assert.equal(m.isHelloTimeoutReason('the server said hello timeout eventually'), false,
    'a mention mid-reason is not the server leading with it');
});

// ---- the call sites that sit outside the sliced block ------------------------------------
// Four events are recorded from handlers the slice does not reach (the watchdog alarm and
// the popup's messages), and the popup's status reply carries the tail. Everything in this
// file would stay green if any of them were deleted, which is this codebase's recurring
// failure mode — so they are checked against the source.

test('every event name the background records is in the module vocabulary', () => {
  const { ConnectionJournal } = loadJournalModule();
  const used = new Set(['hello']);   // noteHelloSent() writes this one
  for (const m of BG_SRC.matchAll(/journalNote\(\s*'([a-z_]+)'/g)) used.add(m[1]);

  assert.ok(used.size >= 8, `only ${used.size} event names found; the census regex rotted`);
  for (const name of used) {
    assert.ok(ConnectionJournal.EVENTS.includes(name),
      `background.js journals "${name}", which the shared module does not list as an event`);
  }
});

test('the out-of-slice call sites still record their events', () => {
  const sites = [
    [/journalNote\('reconnect', \{ reason: 'watchdog' \}\)/, 'the watchdog reconnect'],
    [/journalNote\('reconnect', \{ reason: 'user' \}\)/, "the popup's Reconnect"],
    [/journalNote\('stopped'\)/, "the popup's Stop"],
    [/journal: journalRecent\(10\)/, 'the getConnectionStatus reply']
  ];
  for (const [re, what] of sites) {
    assert.match(BG_SRC, re, `${what} no longer journals anything`);
  }
});

test('the shared journal file is importScripts-ed, after the browser shim', () => {
  // Anchored at column 0: the file's own header comment mentions importScripts() too.
  const call = /^importScripts\((.*)\);/m.exec(BG_SRC);
  assert.ok(call, 'the worker still loads its shared files with importScripts');
  const names = call[1].split(',').map((s) => s.trim().replace(/^['"]|['"]$/g, ''));
  assert.ok(names.includes('connection-journal.js'), 'the journal is among them');
  assert.ok(names.indexOf('shim.js') < names.indexOf('connection-journal.js'),
    'after shim.js, which is what puts globalThis.browser where the module looks for it');
});
