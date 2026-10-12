/*
 * Unit tests for the browser identity carried in `extension_hello`
 * (extension-gecko/background.js: getInstanceId / getBrowserIdentity /
 * getConnectionToken / sendExtensionHello).
 *
 * What these lock down: the server keys its multi-browser registry on the
 * extension-minted `instance_id`, so the id must be minted ONCE, persisted in
 * storage.LOCAL (storage.sync would ride Firefox Sync to other profiles and hand
 * two live browsers the same identity), reused across a Firefox restart / extension
 * reload, and re-announced on EVERY (re)connect. The hello must also keep carrying
 * the fields today's server already reads (version / fileAccess / proxyAccess), and
 * an identity hiccup must not cost us the hello entirely.
 *
 * Like connection.test.mjs: background.js is a top-level extension script, so we
 * slice the connection block out of the source and instantiate it with fake
 * WebSocket/timers/storage. Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
// The connection block sends through the wire this registers on globalThis.
import '../../extension-gecko/ws-crypto.js';
// The re-keying guard (review S5) the connection block delegates to.
import '../../extension-gecko/token-trust.js';
import { spyWires } from './wire-spy.mjs';
const wires = spyWires(globalThis.BrowserplugWsCrypto);
import { randomUUID } from 'node:crypto';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

const RETRY_MS = 7777;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

// A storage area that behaves like browser.storage.*: get(defaults) merges stored
// values over the defaults. `backing` is shared across module instances to model a
// profile that survives an extension reload / Firefox restart.
function makeStorageArea(backing = {}) {
  const area = {
    store: backing,
    sets: 0,
    fail: false,
    async get(defaults) {
      if (area.fail) throw new Error('storage unavailable');
      const out = {};
      const keys = defaults ? Object.keys(defaults) : Object.keys(area.store);
      for (const k of keys) {
        out[k] = (k in area.store) ? area.store[k] : (defaults ? defaults[k] : undefined);
      }
      return out;
    },
    async set(obj) {
      if (area.fail) throw new Error('storage unavailable');
      area.sets++;
      Object.assign(area.store, obj);
    }
  };
  return area;
}

// What runtime.getBrowserInfo() really returns, verbatim from the user's browsers.
// Zen lies on name AND vendor — and then adds a key of its own, which is the only
// thing that outs it (and the only place its real version appears).
const ZEN_BROWSER_INFO = {
  name: 'Firefox', vendor: 'Mozilla', version: '152.0.5',
  buildID: '20260708113753', zen: { version: '1.21.6b' }
};

function loadModule(opts = {}) {
  const start = BG_SRC.indexOf('function connectToMCPServer()');
  const end = BG_SRC.indexOf('// --- Server-health probe over the WS', start);
  assert.ok(start >= 0 && end > start, 'connection block not found in background.js');
  const block = BG_SRC.slice(start, end);

  const deps = {
    local: opts.local || makeStorageArea(),
    sync: opts.sync || makeStorageArea(),
    manifestVersion: opts.manifestVersion || '1.4.14',
    platformInfo: opts.platformInfo === undefined ? { os: 'win', arch: 'x86-64' } : opts.platformInfo,
    browserInfo: opts.browserInfo === undefined ? ZEN_BROWSER_INFO : opts.browserInfo,
    randomUUID
  };

  // eslint-disable-next-line no-new-func
  const factory = new Function('__deps', `
    const console = { log() {}, warn() {}, error(...a) { errors.push(a.join(' ')); }, trace() {} };
    const errors = [];
    const sockets = [];

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
    WebSocket.prototype.fireClose = function () {
      this.readyState = WebSocket.CLOSED;
      if (this.onclose) this.onclose();
    };

    let timerSeq = 1;
    const timers = new Map();
    function setTimeout(fn, ms) { const id = timerSeq++; timers.set(id, { fn, ms }); return id; }
    function clearTimeout(id) { timers.delete(id); }

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
    // Lives past the sliced block (it is defined with the browser-state pushes,
    // below the health probe) but ws.onopen calls it — stub it like the probe.
    function sendBrowserState() {}
    function stopHealthProbe() {}
    async function handleMessage() {}
    async function getProxyPermissionState() { return { has_proxy_permission: true }; }
    const window = {};
    const crypto = { randomUUID: () => __deps.randomUUID() };
    const browser = {
      runtime: {
        getManifest: () => ({ version: __deps.manifestVersion }),
        getPlatformInfo: __deps.platformInfo ? (async () => __deps.platformInfo) : undefined,
        getBrowserInfo: __deps.browserInfo ? (async () => __deps.browserInfo) : undefined
      },
      extension: { isAllowedFileSchemeAccess: async () => true },
      storage: { sync: __deps.sync, local: __deps.local }
    };

    ${block}

    return {
      connectToMCPServer, sendExtensionHello, disconnect, sockets, errors,
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
  const m = factory(deps);
  m.deps = deps;
  return m;
}

// Drain the microtask chain of the hello's awaits (storage/runtime stubs all
// resolve immediately), then let any queued macrotask run.
const settle = async () => {
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
};

// Bring a module up to "connected, first hello sent".
async function connected(opts) {
  const m = loadModule(opts);
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();
  return m;
}

// ---- instance_id: minted once, in storage.local ------------------------------

test('the hello carries an instance_id minted into storage.LOCAL (never storage.sync)', async () => {
  const m = await connected();
  const [hello] = m.hellos();

  assert.ok(hello, 'a hello was sent on connect');
  assert.match(hello.data.instance_id, UUID_RE, 'instance_id is a UUID');
  assert.equal(m.deps.local.store.instanceId, hello.data.instance_id,
    'the id is persisted in storage.local');
  // Firefox Sync would propagate a sync\'d id to the user\'s other profiles/machines,
  // giving two live browsers the same identity.
  assert.equal('instanceId' in m.deps.sync.store, false, 'nothing was written to storage.sync');
  assert.equal(m.deps.sync.sets, 0, 'storage.sync was never written at all');
});

test('the instance_id survives an extension reload / Firefox restart (same profile)', async () => {
  const profile = makeStorageArea();          // one profile's storage.local
  const first = await connected({ local: profile });
  const id = first.hellos()[0].data.instance_id;
  assert.equal(profile.sets, 1, 'minted exactly once');

  // Reload: a brand-new background script instance over the SAME profile storage.
  const second = await connected({ local: profile });

  assert.equal(second.hellos()[0].data.instance_id, id, 'the same identity comes back');
  assert.equal(profile.sets, 1, 'the stored id was reused, not re-minted');
});

test('a different profile gets a different instance_id', async () => {
  const a = await connected();
  const b = await connected();
  assert.notEqual(a.hellos()[0].data.instance_id, b.hellos()[0].data.instance_id);
});

test('concurrent hellos mint the id exactly once', async () => {
  const m = loadModule();
  m.connectToMCPServer();
  m.sockets[0].fireOpen();                    // hello #1 (from onopen)
  await Promise.all([m.sendExtensionHello(), m.sendExtensionHello()]);
  await settle();

  const ids = new Set(m.hellos().map(h => h.data.instance_id));
  assert.equal(m.hellos().length >= 3, true, 'all three hellos went out');
  assert.equal(ids.size, 1, 'every hello carries the same id');
  assert.equal(m.deps.local.store.instanceId, [...ids][0]);
  assert.equal(m.deps.local.sets, 1, 'the id was written once, not once per caller');
});

// ---- the rest of the payload -------------------------------------------------

test('the hello keeps the fields the current server reads, and adds platform/browserInfo', async () => {
  const m = await connected();
  const { data } = m.hellos()[0];

  // Unchanged contract with today's server.
  assert.equal(data.version, '1.4.14');
  assert.deepEqual(data.fileAccess, { can_read_files: true, browser_major: 152 });
  assert.deepEqual(data.proxyAccess, { has_proxy_permission: true });
  // New display material (a remote browser has no PID/profile for the server to probe).
  assert.deepEqual(data.platform, { os: 'win', arch: 'x86-64' });
  assert.deepEqual(data.browserInfo, ZEN_BROWSER_INFO);
});

test('a hello goes out on every (re)connect', async () => {
  const m = loadModule();
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();
  assert.equal(m.helloOn(0).length, 1);

  m.sockets[0].fireClose();                   // server restart
  assert.equal(m.fireReconnectTimer(), true, 'the reconnect loop is armed');
  m.sockets[1].fireOpen();
  await settle();

  const second = m.helloOn(1);
  assert.equal(second.length, 1, 'the reconnected socket re-announced the browser');
  assert.equal(second[0].data.instance_id, m.helloOn(0)[0].data.instance_id,
    'identity is stable across the reconnect');
});

// ---- connection token passthrough --------------------------------------------
// Tokens are minted at runtime here — never a literal value anywhere in the repo.

test('a configured connection token is proved by the handshake, never sent in a hello', async () => {
  const token = randomUUID();
  const sync = makeStorageArea({ connectionToken: token });
  const m = await connected({ sync });

  assert.equal(wires.tokenOf(m.sockets[0]), token);
  assert.equal(m.hellos().length, 0, 'no plaintext hello goes out while we hold a token');
  assert.equal('token' in wires.encryptedHelloOf(m.sockets[0]).data, false,
    'the sealed hello carries no token either');
});

test('no token configured (or a blank one) => the hello omits the key entirely', async () => {
  const none = await connected();
  assert.equal('token' in none.hellos()[0].data, false, 'unset: no token key');

  const blank = await connected({ sync: makeStorageArea({ connectionToken: '   ' }) });
  assert.equal('token' in blank.hellos()[0].data, false,
    'a blank token must not be offered as an answer to a server that requires one');
});

// ---- degradation --------------------------------------------------------------

test('a storage.local failure costs the instance_id, not the whole hello', async () => {
  const local = makeStorageArea();
  local.fail = true;
  const m = await connected({ local });

  const [hello] = m.hellos();
  assert.ok(hello, 'the hello still went out');
  assert.equal(hello.data.instance_id, null, 'identity degrades to null (server synthesizes one)');
  assert.equal(hello.data.version, '1.4.14', 'the fields the server acts on are intact');
  assert.deepEqual(hello.data.proxyAccess, { has_proxy_permission: true });
});

test('a failed mint is not cached — the next hello retries it', async () => {
  const local = makeStorageArea();
  local.fail = true;
  const m = await connected({ local });
  assert.equal(m.hellos()[0].data.instance_id, null);

  local.fail = false;                          // storage comes back
  await m.sendExtensionHello();
  await settle();

  const last = m.hellos().pop();
  assert.match(last.data.instance_id, UUID_RE, 'the retry minted the id');
  assert.equal(local.store.instanceId, last.data.instance_id);
});

test('an old Firefox without getPlatformInfo/getBrowserInfo still sends a valid hello', async () => {
  const m = await connected({ platformInfo: null, browserInfo: null });
  const { data } = m.hellos()[0];

  assert.match(data.instance_id, UUID_RE);
  assert.equal(data.platform, null);
  assert.equal(data.browserInfo, null);
  assert.equal(data.version, '1.4.14');
});

// ---- what outs a fork: its OWN key in getBrowserInfo() -----------------------
// A fork claims to be Firefox on every field it is asked about — name, vendor, the
// user-agent, navigator.buildID (all live-verified on Zen). But it ADDS a key, and
// that key is the only signal of what it really is that reaches the server from
// another machine (chrome://branding, which says "Zen Browser" outright, is
// forbidden to extension pages — Firefox refuses the load).
//
// So the hello must forward getBrowserInfo() WHOLE. Hand-picking the fields we know
// about is exactly how the fork's own key got thrown away.

test('the hello forwards getBrowserInfo() whole, including a fork key we do not know', async () => {
  const m = await connected();
  const { data } = m.hellos()[0];

  assert.deepEqual(data.browserInfo, ZEN_BROWSER_INFO);
  assert.deepEqual(data.browserInfo.zen, { version: '1.21.6b' },
    'the fork key survives — it is the only thing that identifies Zen remotely');
  assert.equal(data.browserInfo.name, 'Firefox',
    '…while every field it is asked about still says Firefox');
});

test('a fork key the extension has never heard of rides along too', async () => {
  const m = await connected({
    browserInfo: { name: 'Firefox', version: '150.0', moonwolf: { version: '9.9' } }
  });
  assert.deepEqual(m.hellos()[0].data.browserInfo.moonwolf, { version: '9.9' },
    'nothing here may filter on a list of forks we happen to know');
});
