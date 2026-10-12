/*
 * CHROMIUM TWIN of tests/js/identity.test.mjs — the browser identity carried in
 * `extension_hello` (extension-chromium/background.js: getInstanceId / getBrowserIdentity /
 * buildBrowserInfo / pickPrimaryBrand / chromeMajorFromUA / sendExtensionHello).
 *
 * instance_id semantics are engine-independent (minted ONCE into storage.LOCAL, reused
 * across restarts, re-announced on every reconnect — storage.sync would clone the identity
 * to sibling profiles). What DIVERGES from gecko is browserInfo: Chromium has no
 * runtime.getBrowserInfo(), so identity is SYNTHESIZED from navigator.userAgentData
 * (brands + high-entropy fullVersionList), a navigator.brave probe, and the UA-string
 * major. The same fork-survival lesson as gecko applies — the WHOLE userAgentData is
 * forwarded, so a brand key the extension never anticipated (Edge/Brave/…) survives — and
 * a top-level `engine:"blink"` discriminator rides alongside for the server's naming path.
 *
 * Like the gecko original: slice the connection block and instantiate it with a fake
 * WebSocket/timers/storage plus a configurable `self.navigator` (the SW's navigator).
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

const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');

const RETRY_MS = 7777;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

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

// What real Chrome's navigator.userAgentData looks like: a GREASE placeholder brand
// ("Not;A=Brand"), the generic "Chromium" brand, and the real product brand last. The
// high-entropy list carries full x.y.z.w versions. buildBrowserInfo must pick the real
// product brand over the placeholder + Chromium, and forward the WHOLE list.
const CHROME_BRANDS = [
  { brand: 'Not;A=Brand', version: '99' },
  { brand: 'Chromium', version: '151' },
  { brand: 'Google Chrome', version: '151' },
];
const CHROME_FULL_VERSIONS = [
  { brand: 'Not;A=Brand', version: '99.0.0.0' },
  { brand: 'Chromium', version: '151.0.7100.5' },
  { brand: 'Google Chrome', version: '151.0.7100.5' },
];

// Build the `self.navigator` the SW reads. `undefined` for userAgentData models an old
// Chromium (or a non-secure context); `brave` models the Brave private probe.
function makeNavigator(opts = {}) {
  const nav = {
    userAgent: opts.userAgent === undefined ? 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0.7100.5 Safari/537.36' : opts.userAgent,
    vendor: opts.vendor === undefined ? 'Google Inc.' : opts.vendor,
  };
  if (opts.userAgentData !== null) {
    const brands = opts.brands || CHROME_BRANDS;
    const high = opts.high === null ? null : (opts.high || {
      fullVersionList: opts.fullVersionList || CHROME_FULL_VERSIONS,
      platform: 'Windows', platformVersion: '15.0.0', architecture: 'x86', bitness: '64',
      uaFullVersion: '151.0.7100.5'
    });
    nav.userAgentData = {
      brands, mobile: false, platform: 'Windows',
      getHighEntropyValues: async () => {
        if (high === null) throw new Error('high-entropy unavailable');
        return high;
      }
    };
  }
  if (opts.brave) nav.brave = { isBrave: async () => true };
  return nav;
}

function loadModule(opts = {}) {
  const start = BG_SRC.indexOf('function connectToMCPServer()');
  const end = BG_SRC.indexOf('// --- Server-health probe over the WS', start);
  assert.ok(start >= 0 && end > start, 'connection block not found in background.js');
  const block = BG_SRC.slice(start, end);

  const deps = {
    local: opts.local || makeStorageArea(),
    sync: opts.sync || makeStorageArea(),
    manifestVersion: opts.manifestVersion || '1.5.0',
    platformInfo: opts.platformInfo === undefined ? { os: 'win', arch: 'x86-64' } : opts.platformInfo,
    navigator: makeNavigator(opts.nav || {}),
    fileAccess: opts.fileAccess === undefined ? true : opts.fileAccess,
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
      if (this.onclose) this.onclose({ code: 1006, reason: '' });
    };

    let timerSeq = 1;
    const timers = new Map();
    function setTimeout(fn, ms) { const id = timerSeq++; timers.set(id, { fn, ms }); return id; }
    function clearTimeout(id) { timers.delete(id); }
    function setInterval() { return 0; }
    function clearInterval() {}

    const self = { navigator: __deps.navigator };

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
    function clearBrowserProxy() {}
    // getProxyPermissionState (in the sliced hello code) asks it; defined further down the file.
    async function hasProxyPermission() { return false; }
    async function handleMessage() {}
    const window = {};
    const crypto = { randomUUID: () => __deps.randomUUID() };
    const browser = {
      runtime: {
        getManifest: () => ({ version: __deps.manifestVersion }),
        getPlatformInfo: __deps.platformInfo ? (async () => __deps.platformInfo) : undefined
      },
      extension: { isAllowedFileSchemeAccess: async () => __deps.fileAccess },
      action: {},
      storage: { sync: __deps.sync, local: __deps.local }
    };

    ${block}

    return {
      connectToMCPServer, sendExtensionHello, disconnect, buildBrowserInfo, pickPrimaryBrand,
      sockets, errors,
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

// buildBrowserInfo awaits getHighEntropyValues + (maybe) brave.isBrave + storage — give
// the microtask chain room to drain, then let a queued macrotask run.
const settle = async () => {
  for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r));
};

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
  // Chrome Sync would propagate a sync'd id to the user's other profiles/machines,
  // giving two live browsers the same identity.
  assert.equal('instanceId' in m.deps.sync.store, false, 'nothing was written to storage.sync');
  assert.equal(m.deps.sync.sets, 0, 'storage.sync was never written at all');
});

test('the instance_id survives an extension reload / browser restart (same profile)', async () => {
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

test('concurrent hellos mint the id exactly once (getInstanceId memoizes the promise)', async () => {
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

test('the hello keeps the fields the current server reads, adds engine/platform/browserInfo', async () => {
  const m = await connected();
  const { data } = m.hellos()[0];

  // Unchanged contract with today's server.
  assert.equal(data.version, '1.5.0');
  // browser_major is parsed from the UA string (Chrome/151...) — the honest chromium
  // analog of gecko's Firefox major.
  assert.deepEqual(data.fileAccess, { can_read_files: true, browser_major: 151 });
  // Chromium fails the proxy feature CLOSED: it advertises it as unsupported (design D7).
  assert.deepEqual(data.proxyAccess, { has_proxy_permission: false, proxy_supported: true, proxy_scope: 'browser' });
  // New display material.
  assert.deepEqual(data.platform, { os: 'win', arch: 'x86-64' });
  assert.equal(data.engine, 'blink', 'the top-level engine discriminator');
  assert.equal(data.browserInfo.engine, 'blink', 'and it is carried inside browserInfo too');
  assert.equal(data.tokenBootstrap, true);
});

test('browserInfo picks the REAL product brand over the GREASE placeholder and Chromium', async () => {
  const m = await connected();
  const { browserInfo } = m.hellos()[0].data;

  assert.equal(browserInfo.name, 'Google Chrome', 'the real brand wins, not "Chromium"/"Not;A=Brand"');
  assert.equal(browserInfo.version, '151.0.7100.5', 'the version comes from the high-entropy fullVersionList');
  assert.equal(browserInfo.vendor, 'Google Inc.');
});

test('the high-entropy fullVersionList is forwarded whole (brands + full x.y.z.w versions)', async () => {
  const m = await connected();
  const { browserInfo } = m.hellos()[0].data;

  assert.deepEqual(browserInfo.userAgentData.brands, CHROME_BRANDS, 'the low-entropy brands ride along');
  assert.deepEqual(browserInfo.userAgentData.fullVersionList, CHROME_FULL_VERSIONS,
    'and the high-entropy full version list');
  assert.equal(browserInfo.userAgentData.platformVersion, '15.0.0');
  assert.equal(browserInfo.userAgentData.uaFullVersion, '151.0.7100.5');
});

// ---- what outs a fork: a brand key we did not anticipate ---------------------
// The gecko lesson (forward getBrowserInfo() WHOLE so a fork's own key survives) maps to
// forwarding userAgentData WHOLE: a brand the extension has never heard of must ride
// along, both as the picked name and inside the forwarded brands list.

test('an unanticipated product brand rides along and is chosen as the name', async () => {
  const m = await connected({
    nav: {
      brands: [{ brand: 'Not;A=Brand', version: '99' }, { brand: 'Chromium', version: '151' },
               { brand: 'Wolf Browser', version: '3' }],
      fullVersionList: [{ brand: 'Not;A=Brand', version: '99.0.0.0' },
                        { brand: 'Chromium', version: '151.0.7100.5' },
                        { brand: 'Wolf Browser', version: '3.2.1.0' }]
    }
  });
  const { browserInfo } = m.hellos()[0].data;

  assert.equal(browserInfo.name, 'Wolf Browser', 'a brand we never heard of is still chosen over Chromium');
  assert.ok(browserInfo.userAgentData.brands.some(b => b.brand === 'Wolf Browser'),
    'and the whole brands list is forwarded — nothing filters on a known-fork allowlist');
});

test('the navigator.brave probe outs Brave (its private key), like Zen`s zen: key', async () => {
  const m = await connected({ nav: { brave: true } });
  const { browserInfo } = m.hellos()[0].data;

  assert.equal(browserInfo.name, 'Brave', 'the brave probe wins the name');
  assert.deepEqual(browserInfo.brave, { isBrave: true }, 'and the private brave key survives');
});

test('a browser exposing only GREASE + Chromium brands is named "Chromium"', async () => {
  const m = await connected({
    nav: {
      brands: [{ brand: 'Not;A=Brand', version: '99' }, { brand: 'Chromium', version: '151' }],
      fullVersionList: [{ brand: 'Not;A=Brand', version: '99.0.0.0' },
                        { brand: 'Chromium', version: '151.0.7100.5' }]
    }
  });
  assert.equal(m.hellos()[0].data.browserInfo.name, 'Chromium');
});

test('a hello goes out on every (re)connect, identity stable', async () => {
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
  assert.equal(hello.data.version, '1.5.0', 'the fields the server acts on are intact');
  assert.equal(hello.data.engine, 'blink');
  assert.deepEqual(hello.data.proxyAccess, { has_proxy_permission: false, proxy_supported: true, proxy_scope: 'browser' });
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

test('an old Chromium without getPlatformInfo still sends a valid hello', async () => {
  const m = await connected({ platformInfo: null });
  const { data } = m.hellos()[0];

  assert.match(data.instance_id, UUID_RE);
  assert.equal(data.platform, null);
  assert.equal(data.engine, 'blink');
  assert.equal(data.version, '1.5.0');
});

test('a browser without navigator.userAgentData still yields a browserInfo (engine + Chromium fallback)', async () => {
  const m = await connected({ nav: { userAgentData: null } });
  const { browserInfo } = m.hellos()[0].data;

  assert.equal(browserInfo.engine, 'blink', 'the engine discriminator is always present');
  assert.equal(browserInfo.name, 'Chromium', 'and it degrades to a Chromium name');
  assert.equal('userAgentData' in browserInfo, false, 'with no userAgentData to forward');
});

test('high-entropy hints being unavailable falls back to the low-entropy brands', async () => {
  // getHighEntropyValues throws (a locked-down context) — the low-entropy brands still
  // name the browser; nothing crashes.
  const m = await connected({ nav: { high: null } });
  const { browserInfo } = m.hellos()[0].data;

  assert.equal(browserInfo.name, 'Google Chrome', 'the low-entropy brand still picks the product');
  assert.deepEqual(browserInfo.userAgentData.brands, CHROME_BRANDS);
  assert.equal(browserInfo.userAgentData.fullVersionList, undefined, 'no high-entropy list was obtained');
});
