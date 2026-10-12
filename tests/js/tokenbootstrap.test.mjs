/*
 * Unit tests for the extension's WebSocket-token bootstrap
 * (extension-gecko/background.js: getConnectionToken / parseTokenEndpoint / handleUnauthorized,
 * and the 4401 branch of ws.onclose).
 *
 * The flow: the server always has a token now (it mints one on first run). A browser on
 * the SAME machine is never asked to be configured — the server closes its socket with
 * 4401 and a close reason naming a localhost-only endpoint, and the extension fetches the
 * token from there, stores it and reconnects. A browser on ANOTHER machine cannot reach
 * that endpoint (that is the security limit, not a bug) and must be given the token by
 * hand in the options page.
 *
 * What these tests lock down, in order of how much damage each would do:
 *   1. NEVER fetch a token from a non-loopback URL — the close reason is attacker-
 *      influencable input, and the token we fetch is the one we then hand to a server.
 *   2. NEVER put the auto-pulled token in storage.SYNC — Firefox Sync would copy a secret
 *      belonging to one machine's server onto the user's other machines.
 *   3. NEVER loop: a server that keeps saying 4401 must not become a fetch loop.
 *   4. The user's typed token wins over the auto-pulled one, always.
 *
 * Like connection.test.mjs / identity.test.mjs: background.js is a top-level extension
 * script, so we slice the connection block out of the source and instantiate it with fake
 * WebSocket/fetch/timers/storage. Run with: npm run test:js
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

// A token exactly as the server would mint one — generated at runtime. Project rule: a
// token value is never written down anywhere, tests included.
const mintToken = () => randomUUID() + randomUUID().replace(/-/g, '');

function makeStorageArea(backing = {}) {
  const area = {
    store: backing,
    sets: 0,
    gets: 0,
    fail: false,       // both directions; failGet / failSet break just one
    async get(defaults) {
      area.gets++;
      if (area.fail || area.failGet) throw new Error('storage unavailable');
      const out = {};
      const keys = defaults ? Object.keys(defaults) : Object.keys(area.store);
      for (const k of keys) {
        out[k] = (k in area.store) ? area.store[k] : (defaults ? defaults[k] : undefined);
      }
      return out;
    },
    async set(obj) {
      if (area.fail || area.failSet) throw new Error('storage unavailable');
      area.sets++;
      Object.assign(area.store, obj);
    }
  };
  return area;
}

function loadModule(opts = {}) {
  const start = BG_SRC.indexOf('function connectToMCPServer()');
  const end = BG_SRC.indexOf('// --- Server-health probe over the WS', start);
  assert.ok(start >= 0 && end > start, 'connection block not found in background.js');
  const block = BG_SRC.slice(start, end);

  const deps = {
    local: opts.local || makeStorageArea(),
    sync: opts.sync || makeStorageArea(),
    // fetch responses, in order. Each is { ok, status, body } or { throws: 'message' }.
    responses: opts.responses ? [...opts.responses] : [],
    fetches: [],
    storageListeners: [],
    brandingFetches: [],
    randomUUID
  };

  // eslint-disable-next-line no-new-func
  const factory = new Function('__deps', `
    const errors = [];
    const warnings = [];
    const console = {
      log() {}, trace() {},
      warn(...a) { warnings.push(a.join(' ')); },
      error(...a) { errors.push(a.join(' ')); }
    };
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
    // The real CloseEvent carries code + reason — that IS the channel the server uses.
    WebSocket.prototype.fireClose = function (code, reason) {
      this.readyState = WebSocket.CLOSED;
      if (this.onclose) this.onclose({ code: code === undefined ? 1006 : code,
                                       reason: reason || '' });
    };

    let timerSeq = 1;
    const timers = new Map();
    function setTimeout(fn, ms) { const id = timerSeq++; timers.set(id, { fn, ms }); return id; }
    function clearTimeout(id) { timers.delete(id); }

    // The token endpoint is the ONLY network call the extension makes outside the
    // WebSocket -- which is exactly what these tests assert, so deps.fetches must stay
    // token-only. The identity code also reads chrome://branding (the browser naming
    // itself; not a network call, no host involved), so serve that and keep it out.
    // (No backticks in here: this whole block lives inside a template literal.)
    async function fetch(url, init) {
      if (String(url).startsWith('chrome://branding')) {
        __deps.brandingFetches.push(url);
        return { ok: true, status: 200, text: async () => 'brandFullName=Firefox' };
      }
      __deps.fetches.push(url);
      const next = __deps.responses.shift();
      if (!next) throw new Error('connection refused');
      if (next.throws) throw new Error(next.throws);
      return {
        ok: next.ok !== false,
        status: next.status || 200,
        json: async () => next.body
      };
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
    // Lives past the sliced block (it is defined with the browser-state pushes,
    // below the health probe) but ws.onopen calls it — stub it like the probe.
    function sendBrowserState() {}
    function stopHealthProbe() {}
    async function handleMessage() {}
    async function getProxyPermissionState() { return { has_proxy_permission: false }; }
    const window = {};
    const crypto = { randomUUID: () => __deps.randomUUID() };
    const browser = {
      runtime: { getManifest: () => ({ version: '1.4.14' }) },
      extension: { isAllowedFileSchemeAccess: async () => true },
      storage: { sync: __deps.sync, local: __deps.local,
                 onChanged: { addListener: (fn) => __deps.storageListeners.push(fn) } }
    };

    ${block}

    return {
      connectToMCPServer, handleUnauthorized, parseTokenEndpoint, getConnectionToken,
      adoptServerToken, markTokenProven, endpointMatchesConfig,
      setLastMcpPort: (p) => { lastServerMcpPort = p; },
      // A finished handshake on the current socket, made with this token.
      setHandshake: (token, encrypted) => { handshakeToken = token; wire = { encrypted }; },
      handshakeToken: () => handshakeToken,
      fireStorageChange: (changes, area) => { for (const fn of __deps.storageListeners) fn(changes, area); },
      status: () => tokenTrustStatus(),
      sockets, errors, warnings, CONFIG,
      hellosOn: (i) => sockets[i].sent.filter(f => typeof f === 'string').map(JSON.parse).filter(m => m.type === 'extension_hello'),
      reconnectTimersLive: () => [...timers.values()].filter(t => t.ms === ${RETRY_MS}).length,
      setStoppedByUser: (v) => { stoppedByUser = v; },
      state: () => ({ connectionPhase, retryAttempts, current: websocket,
                      tokenBootstrapAttempts, lastAuthError,
                      tokenRejected: tokenTrust.rejected,
                      pendingAdoptionEndpoint: tokenTrust.pendingEndpoint })
    };
  `);
  const m = factory(deps);
  m.deps = deps;
  return m;
}

const settle = async () => {
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
};

// The close reason the server really sends (server.py: _auth_close_reason). The port is
// the server's REAL MCP port — the extension cannot guess it, which is the whole reason
// it travels in the close reason.
const reasonFor = (port = 2831) => `token required: GET http://127.0.0.1:${port}/ws-token`;

// Bring a module up to "connected, then rejected with 4401".
async function rejected(opts, reason = reasonFor()) {
  const m = loadModule(opts);
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();
  m.sockets[0].fireClose(4401, reason);
  await settle();
  return m;
}

// ---- the happy path: a same-machine browser configures itself -----------------

test('a 4401 makes the extension fetch the token, store it in storage.LOCAL and reconnect', async () => {
  const token = mintToken();
  const m = await rejected({ responses: [{ body: { token } }] }, reasonFor(2831));

  assert.deepEqual(m.deps.fetches, ['http://127.0.0.1:2831/ws-token'],
    'it fetched exactly the endpoint the server named');
  assert.equal(m.deps.local.store.autoToken, token, 'the token is kept in storage.local');
  // Firefox Sync would copy this secret to the user's OTHER machines/profiles, where it
  // is worthless at best and a leak at worst.
  assert.equal('autoToken' in m.deps.sync.store, false, 'nothing was written to storage.sync');
  assert.equal(m.deps.sync.sets, 0, 'storage.sync was never written at all');

  assert.equal(m.sockets.length, 2, 'it reconnected immediately (no 5s backoff)');
  assert.equal(m.reconnectTimersLive(), 0, 'and did not ALSO arm the retry timer');
  // Chrome lists every console.error on chrome://extensions under Errors, so a first run
  // that worked must not leave one there.
  assert.deepEqual(m.errors, [], 'a bootstrap that worked logs no error');

  // The retry proves the token it just fetched, through the encrypted handshake: the
  // handshake request goes first and no plaintext frame carries the token.
  m.sockets[1].fireOpen();
  await settle();
  assert.equal(wires.tokenOf(m.sockets[1]), token);
  assert.ok(wires.isRequestFrame(m.sockets[1].sent[0]), 'the handshake request goes first');
  assert.equal(m.sockets[1].sent.some(f => typeof f === 'string' && f.includes(token)), false,
    'the token never crosses the socket in plaintext');
});

test('every hello announces the bootstrap capability (this is what saves old extensions)', async () => {
  const m = loadModule();
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();

  // The server needs to tell "can fetch a token" apart from "predates the feature": the
  // latter it must let in rather than brick.
  assert.equal(m.hellosOn(0)[0].data.tokenBootstrap, true);
});

test('the token never enters CONFIG (which is console-logged and shipped to the popup)', async () => {
  const token = mintToken();
  const m = await rejected({ responses: [{ body: { token } }] });

  assert.equal(JSON.stringify(m.CONFIG).includes(token), false);
  assert.equal(m.errors.concat(m.warnings).some(line => line.includes(token)), false,
    'and it is never logged');
});

// ---- precedence: the user's typed token wins ---------------------------------

test('a manual (storage.sync) token wins over the auto-pulled one', async () => {
  const manual = mintToken();
  const auto = mintToken();
  const m = loadModule({
    sync: makeStorageArea({ connectionToken: manual }),
    local: makeStorageArea({ autoToken: auto })
  });
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();

  assert.equal(wires.tokenOf(m.sockets[0]), manual, 'the user-typed value is what the handshake proves');
  assert.equal(await m.getConnectionToken(), manual);
});

test('a rejected MANUAL token is never silently replaced — it backs off and says why', async () => {
  const m = await rejected({
    sync: makeStorageArea({ connectionToken: mintToken() }),
    responses: [{ body: { token: mintToken() } }]
  });

  assert.deepEqual(m.deps.fetches, [], 'a token the user typed is not overwritten behind their back');
  assert.equal(m.reconnectTimersLive(), 1, 'it falls back to the normal reconnect backoff');
  assert.ok(m.errors.some(e => /Options/.test(e)), 'the user is told where to fix it');
  assert.ok(m.state().lastAuthError, 'and the state is surfaced, not swallowed');
});

// ---- 1. never fetch a token from a stranger ----------------------------------

test('a NON-loopback endpoint in the close reason is IGNORED (never fetched)', async () => {
  const m = await rejected({ responses: [{ body: { token: mintToken() } }] },
    'token required: GET http://evil.example.com/ws-token');

  assert.deepEqual(m.deps.fetches, [], 'we do not take a token from a remote host');
  assert.equal('autoToken' in m.deps.local.store, false, 'nothing was stored');
  assert.equal(m.reconnectTimersLive(), 1, 'it just backs off like any other failure');
});

test('parseTokenEndpoint accepts only loopback http URLs', () => {
  const m = loadModule();
  const ok = [
    ['token required: GET http://127.0.0.1:3456/ws-token', 'http://127.0.0.1:3456/ws-token'],
    ['token required: GET http://localhost:3000/ws-token', 'http://localhost:3000/ws-token'],
    ['token required: GET http://[::1]:3456/ws-token', 'http://[::1]:3456/ws-token']
  ];
  for (const [reason, href] of ok) {
    assert.equal(m.parseTokenEndpoint(reason), href, reason);
  }

  const rejectedReasons = [
    'token required: GET http://evil.example.com/ws-token',   // someone else's server
    'token required: GET http://10.0.0.5:3456/ws-token',      // a LAN host is not us
    'token required: GET http://127.0.0.1.evil.com/ws-token', // prefix trickery
    'token required: GET https://127.0.0.1:3456/ws-token',    // the server never serves TLS
    'token required: GET file:///etc/passwd',
    'token required: paste the token into the extension options',  // the REMOTE reason
    'unauthorized',
    ''
  ];
  for (const reason of rejectedReasons) {
    assert.equal(m.parseTokenEndpoint(reason), null, reason);
  }
});

test('a remote server rejecting us never triggers a fetch (its reason names no endpoint)', async () => {
  const m = await rejected({ responses: [{ body: { token: mintToken() } }] },
    "token required: paste a device token made for this browser into the extension options");

  assert.deepEqual(m.deps.fetches, []);
  assert.equal(m.reconnectTimersLive(), 1);
});

// ---- 3. never loop -----------------------------------------------------------

test('a server that keeps rejecting us does NOT become a fetch loop', async () => {
  // Every reconnect is rejected again (e.g. the server was reconfigured with a token the
  // endpoint no longer hands out). Each round trip must be bounded.
  const responses = [];
  for (let i = 0; i < 10; i++) responses.push({ body: { token: mintToken() } });
  const m = loadModule({ responses });

  m.connectToMCPServer();
  for (let i = 0; i < 8 && m.sockets.length > i; i++) {
    m.sockets[i].fireOpen();
    await settle();
    m.sockets[i].fireClose(4401, reasonFor());
    await settle();
  }

  assert.ok(m.deps.fetches.length <= 3,
    `bounded fetches, got ${m.deps.fetches.length}`);
  assert.equal(m.reconnectTimersLive(), 1, 'it ends up in the ordinary backoff loop');
  assert.ok(m.errors.some(e => /Gave up|backing off/i.test(e)), 'and says so');
});

test('the same token coming back a second time is not retried forever', async () => {
  // The server hands out a token and then rejects it anyway (a bug, or a server that
  // rotated mid-flight). Refetching cannot help — stop pulling.
  const token = mintToken();
  const m = loadModule({ responses: [{ body: { token } }, { body: { token } }] });

  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();
  m.sockets[0].fireClose(4401, reasonFor());
  await settle();
  assert.equal(m.sockets.length, 2, 'first fetch -> reconnect');

  m.sockets[1].fireOpen();
  await settle();
  m.sockets[1].fireClose(4401, reasonFor());   // rejected again, with the very same token
  await settle();

  assert.equal(m.deps.fetches.length, 2);
  assert.equal(m.sockets.length, 2, 'no third connect attempt was fired off immediately');
  assert.equal(m.reconnectTimersLive(), 1, 'backoff instead');
  assert.ok(m.errors.some(e => /rejected the very token/i.test(e)));
});

test('a failed fetch stores nothing and backs off', async () => {
  for (const response of [{ throws: 'connection refused' }, { ok: false, status: 500 },
                          { body: {} }, { body: { token: '' } }]) {
    const m = await rejected({ responses: [response] });
    assert.equal('autoToken' in m.deps.local.store, false,
      `nothing stored for ${JSON.stringify(response)}`);
    assert.equal(m.sockets.length, 1, 'no immediate reconnect on a failed bootstrap');
    assert.equal(m.reconnectTimersLive(), 1, 'the normal retry loop takes over');
  }
});

test('a storage.local failure does not lose the connection — it backs off', async () => {
  const local = makeStorageArea();
  const m = loadModule({ local, responses: [{ body: { token: mintToken() } }] });
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();
  local.fail = true;                       // the write is what fails
  m.sockets[0].fireClose(4401, reasonFor());
  await settle();

  assert.equal(m.reconnectTimersLive(), 1);
  assert.equal(m.sockets.length, 1);
});

// ---- the rest of the close path is untouched ---------------------------------

test('a user Stop during a 4401 is honoured (no fetch, no reconnect)', async () => {
  const m = loadModule({ responses: [{ body: { token: mintToken() } }] });
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();

  m.setStoppedByUser(true);                // the popup's Stop, before the close lands
  m.sockets[0].fireClose(4401, reasonFor());
  await settle();

  assert.deepEqual(m.deps.fetches, [], 'a stopped connection does not go fetching');
  assert.equal(m.reconnectTimersLive(), 0);
  assert.equal(m.state().connectionPhase, 'stopped');
});

test('an ORDINARY close (server restart) still just reconnects — no token machinery', async () => {
  const m = loadModule({ responses: [{ body: { token: mintToken() } }] });
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();

  m.sockets[0].fireClose(1006, '');        // the everyday case
  await settle();

  assert.deepEqual(m.deps.fetches, [], 'a normal disconnect never fetches a token');
  assert.equal(m.reconnectTimersLive(), 1);
  assert.equal(m.state().lastAuthError, null);
});

// ---- review S5: a token that already worked is not re-keyed by a 4401 ----------

const provenStore = () => ({ autoToken: mintToken(), autoTokenProven: true });

test('S5: a 4401 after a proven token does NOT fetch or overwrite it; it waits for the user', async () => {
  const local = makeStorageArea(provenStore());
  const before = local.store.autoToken;
  const m = await rejected({ local, responses: [{ body: { token: mintToken() } }] },
    reasonFor(2830));

  assert.deepEqual(m.deps.fetches, [], 'a squatter token endpoint is never fetched unprompted');
  assert.equal(local.store.autoToken, before, 'the stored token is untouched');
  assert.equal(m.state().tokenRejected, true, 'the rejected state is surfaced');
  assert.equal(m.state().pendingAdoptionEndpoint, 'http://127.0.0.1:2830/ws-token');
  assert.equal(m.sockets.length, 1, 'no immediate reconnect with a new token');
  assert.equal(m.reconnectTimersLive(), 1, 'ordinary backoff continues with the old token');
});

test('S5: the first-ever token is still adopted automatically (nothing proven yet)', async () => {
  const token = mintToken();
  const m = await rejected({ responses: [{ body: { token } }] });
  assert.equal(m.deps.local.store.autoToken, token);
  assert.equal(m.state().tokenRejected, false);
});

test('S5: an unproven stored token (never completed a handshake) is still replaced', async () => {
  const token = mintToken();
  const local = makeStorageArea({ autoToken: mintToken() });
  const m = await rejected({ local, responses: [{ body: { token } }] });
  assert.equal(local.store.autoToken, token);
});

test('S5: markTokenProven records the flag only for an encrypted handshake with the auto token', async () => {
  const bare = loadModule();
  bare.setHandshake('', true);
  await bare.markTokenProven();
  assert.equal('autoTokenProven' in bare.deps.local.store, false, 'no token, nothing proven');

  const auto = mintToken();
  const local = makeStorageArea({ autoToken: auto });
  const m = loadModule({ local });
  m.setHandshake(auto, true);
  await m.markTokenProven();
  assert.equal(local.store.autoTokenProven, true);
});

test('S5: the user click adopts the server token and resets the proven flag', async () => {
  const local = makeStorageArea(provenStore());
  const fresh = mintToken();
  const m = await rejected({ local, responses: [{ body: { token: fresh } }] }, reasonFor(2831));

  const res = await m.adoptServerToken();
  await settle();
  assert.deepEqual(res, { ok: true });
  assert.deepEqual(m.deps.fetches, ['http://127.0.0.1:2831/ws-token']);
  assert.equal(local.store.autoToken, fresh);
  assert.equal(local.store.autoTokenProven, false, 'the new token must earn trust again');
  assert.equal(m.state().tokenRejected, false);
  assert.equal(m.sockets.length, 2, 'it reconnected with the new token');
});

test('S5: the click refuses an endpoint outside the configured origin and port range', async () => {
  const local = makeStorageArea(provenStore());
  const before = local.store.autoToken;
  const m = await rejected({ local, responses: [{ body: { token: mintToken() } }] },
    reasonFor(9999));   // loopback, but not a port the server's seek can land on

  assert.equal(m.state().tokenRejected, true, 'the user still sees the rejection');
  assert.equal(m.state().pendingAdoptionEndpoint, null, 'but nothing is offered from that endpoint');
  const res = await m.adoptServerToken();
  assert.equal(res.ok, false);
  assert.deepEqual(m.deps.fetches, []);
  assert.equal(local.store.autoToken, before);
});

test('S5: adoptServerToken with nothing pending does nothing', async () => {
  const m = loadModule({ responses: [{ body: { token: mintToken() } }] });
  const res = await m.adoptServerToken();
  assert.equal(res.ok, false);
  assert.deepEqual(m.deps.fetches, []);
});

test('S5: endpointMatchesConfig needs a loopback configured host', () => {
  const m = loadModule();
  assert.equal(m.endpointMatchesConfig('http://127.0.0.1:2830/ws-token'), true);
  assert.equal(m.endpointMatchesConfig('http://[::1]:2830/ws-token'), true);
  assert.equal(m.endpointMatchesConfig('http://127.0.0.1:80/ws-token'), false);
  m.CONFIG.hostname = 'server.example.org';
  assert.equal(m.endpointMatchesConfig('http://127.0.0.1:2830/ws-token'), false,
    'a browser configured for a remote server never fetches from loopback');
});

test('S5: a configured mcpPort outside the seek range is honoured, and only that one', () => {
  const m = loadModule();
  assert.equal(m.endpointMatchesConfig('http://127.0.0.1:44216/ws-token'), false,
    'a random port nobody configured is refused');
  m.CONFIG.mcpPort = 44216;
  assert.equal(m.endpointMatchesConfig('http://127.0.0.1:44216/ws-token'), true);
  assert.equal(m.endpointMatchesConfig('http://127.0.0.1:44217/ws-token'), false,
    'a neighbouring port a squatter names is still refused');
  m.CONFIG.hostname = 'server.example.org';
  assert.equal(m.endpointMatchesConfig('http://127.0.0.1:44216/ws-token'), false,
    'and never for a browser configured against a remote server');
});

// The test harness's path: a fresh profile (nothing proven) whose config names a random
// MCP port, as the integration fixtures seed it. The unit suite used to model only ports
// inside 2830..2942, so refusing every other port went unnoticed until the sweep.
test('S5: first-ever adoption works from a random loopback port the config names', async () => {
  const token = mintToken();
  const m = loadModule({ responses: [{ body: { token } }] });
  m.CONFIG.mcpPort = 44216;
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();
  m.sockets[0].fireClose(4401, reasonFor(44216));
  await settle();
  assert.deepEqual(m.deps.fetches, ['http://127.0.0.1:44216/ws-token']);
  assert.equal(m.deps.local.store.autoToken, token);
  assert.equal(m.sockets.length, 2, 'it reconnected with the new token');
});

test('S5: the same random port is refused when the config does not name it', async () => {
  const m = loadModule({ responses: [{ body: { token: mintToken() } }] });
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();
  m.sockets[0].fireClose(4401, reasonFor(44216));
  await settle();
  assert.deepEqual(m.deps.fetches, [], 'a squatter naming an arbitrary port is not fetched from');
});

test('S5 glue: loadConfig feeds CONFIG.mcpPort from the mcpPort setting', () => {
  assert.match(BG_SRC, /CONFIG\.mcpPort = validPort\(/);
});

test('S5 glue: the health probe marks the token proven and the message handler is wired', () => {
  const probe = BG_SRC.slice(BG_SRC.indexOf('async function probeServerHealth()'));
  assert.match(probe.slice(0, 2500), /markTokenProven\(data && data\.mcp_port\)/);
  assert.match(BG_SRC, /request\.action === 'adoptServerToken'[\s\S]{0,120}adoptServerToken\(\)/);
  assert.match(BG_SRC, /\.\.\.tokenTrustStatus\(\)/);
});

test('S5: the FIRST adoption is also pinned to the MCP port range (no arbitrary port)', async () => {
  const m = await rejected({ responses: [{ body: { token: mintToken() } }] }, reasonFor(3456));
  assert.deepEqual(m.deps.fetches, [], 'a squatter naming port 3456 is not fetched from');
  assert.equal('autoToken' in m.deps.local.store, false);
  assert.equal(m.reconnectTimersLive(), 1);
});

// ---- review S5 follow-ups: what proves a token, and what the popup is told -----------

test('S5: the hello records which token the socket\'s handshake uses', async () => {
  const manual = mintToken();
  const sync = makeStorageArea({ connectionToken: manual });
  const local = makeStorageArea({ autoToken: mintToken() });
  const m = loadModule({ sync, local });
  m.connectToMCPServer();
  m.sockets[0].fireOpen();
  await settle();
  assert.equal(m.handshakeToken(), manual, 'manual wins, so the handshake used it');
  m.connectToMCPServer();
  assert.equal(m.handshakeToken(), null, 'a new socket has not started a handshake');
});

test('S5: a socket that authenticated with the MANUAL token proves nothing about the auto one', async () => {
  const manual = mintToken();
  const local = makeStorageArea({ autoToken: mintToken() });
  const m = loadModule({ local, sync: makeStorageArea({ connectionToken: manual }) });
  m.setHandshake(manual, true);
  await m.markTokenProven();
  assert.equal('autoTokenProven' in local.store, false,
    'a stale auto token was marked proven by the manual token\'s handshake');
});

test('S5: a server with auth off (no handshake) proves nothing either', async () => {
  const auto = mintToken();
  const local = makeStorageArea({ autoToken: auto });
  const m = loadModule({ local });
  m.setHandshake(auto, false);
  await m.markTokenProven();
  assert.equal('autoTokenProven' in local.store, false);
});

test('S5: the MCP port an authenticated server reported is still trusted after reconnects', async () => {
  const local = makeStorageArea(provenStore());
  const fresh = mintToken();
  const m = loadModule({ local, responses: [{ body: { token: fresh } }] });
  m.setHandshake(local.store.autoToken, true);
  await m.markTokenProven(9000);           // a health answer from a server pinned at 9000
  m.connectToMCPServer();                  // the next attempt starts from scratch
  m.sockets[0].fireOpen();
  await settle();
  m.sockets[0].fireClose(4401, reasonFor(9000));
  await settle();
  assert.equal(m.state().pendingAdoptionEndpoint, 'http://127.0.0.1:9000/ws-token');
  m.connectToMCPServer();                  // the retry loop runs again before the click
  assert.deepEqual(await m.adoptServerToken(), { ok: true });
  assert.equal(local.store.autoToken, fresh);
});

test('S5: an MCP port saved in Options applies without a restart', () => {
  const m = loadModule();
  assert.equal(m.endpointMatchesConfig('http://127.0.0.1:9000/ws-token'), false);
  m.fireStorageChange({ mcpPort: { newValue: 9000 } }, 'sync');
  assert.equal(m.CONFIG.mcpPort, 9000);
  assert.equal(m.endpointMatchesConfig('http://127.0.0.1:9000/ws-token'), true);
  m.fireStorageChange({ mcpPort: { newValue: 'junk' } }, 'sync');
  assert.equal(m.CONFIG.mcpPort, null);
});

test('S5: an ordinary close after a rejection stops reporting "token rejected"', async () => {
  const m = await rejected({ local: makeStorageArea(provenStore()) }, reasonFor(2830));
  assert.equal(m.status().tokenRejected, true);
  m.connectToMCPServer();
  m.sockets[1].fireClose(1006, '');        // the server is gone: connection refused
  await settle();
  assert.deepEqual(m.status(), { tokenRejected: false, tokenAdoptable: false, adoptError: null });
});

test('S5: a rejected MANUAL token drops a pending re-authorize', async () => {
  const sync = makeStorageArea();
  const m = await rejected({ sync, local: makeStorageArea(provenStore()) }, reasonFor(2830));
  assert.equal(m.status().tokenAdoptable, true);
  sync.store.connectionToken = mintToken();
  m.connectToMCPServer();
  m.sockets[1].fireOpen();
  await settle();
  m.sockets[1].fireClose(4401, reasonFor(2830));
  await settle();
  assert.equal(m.status().tokenRejected, false, 'Re-authorize would store a token the manual one overrides');
});

test('S5: a failed re-authorize says why in the status the popup reads', async () => {
  const m = await rejected({ local: makeStorageArea(provenStore()),
                             responses: [{ ok: false, status: 404, body: {} }] }, reasonFor(2830));
  const res = await m.adoptServerToken();
  assert.equal(res.ok, false);
  assert.match(m.status().adoptError, /could not fetch a token: HTTP 404/);
  assert.equal(m.status().tokenAdoptable, true, 'still rejected, still offered');
});

// ---- review follow-ups: the guard fails closed and keeps what it told the popup ------

test('S5: an unreadable proven flag counts as proven, so a 4401 waits for the user', async () => {
  const local = makeStorageArea();
  local.failGet = true;                    // the read that decides "proven" is what fails
  const m = await rejected({ local, responses: [{ body: { token: mintToken() } }] }, reasonFor(2830));
  assert.deepEqual(m.deps.fetches, [], 'an unreadable flag let the 4401\'s endpoint be fetched unprompted');
  assert.equal(m.state().tokenRejected, true);
  assert.equal(m.reconnectTimersLive(), 1);
});

test('S5: a proven flag whose write failed is written again on the next health answer', async () => {
  const auto = mintToken();
  const local = makeStorageArea({ autoToken: auto });
  const m = loadModule({ local });
  m.setHandshake(auto, true);
  local.failSet = true;
  await m.markTokenProven();
  assert.equal('autoTokenProven' in local.store, false);
  local.failSet = false;
  await m.markTokenProven();
  assert.equal(local.store.autoTokenProven, true,
    'the flag lived only in memory, so the next browser start would adopt a 4401\'s token again');
});

test('S5: once the token is proven, a health answer does not read storage', async () => {
  const auto = mintToken();
  const local = makeStorageArea({ autoToken: auto });
  const m = loadModule({ local });
  m.setHandshake(auto, true);
  await m.markTokenProven(2830);
  assert.equal(local.store.autoTokenProven, true);
  const before = local.gets;
  for (let i = 0; i < 3; i++) await m.markTokenProven(2830);
  assert.equal(local.gets, before, 'every health probe read the auto token back from storage');
});

test('S5: the retry loop\'s next 4401 keeps a failed re-authorize\'s reason', async () => {
  const m = await rejected({ local: makeStorageArea(provenStore()),
                             responses: [{ ok: false, status: 500, body: {} }] }, reasonFor(2830));
  await m.adoptServerToken();
  assert.match(m.status().adoptError, /HTTP 500/);
  m.connectToMCPServer();                  // the scheduled reconnect
  m.sockets[1].fireOpen();
  await settle();
  m.sockets[1].fireClose(4401, reasonFor(2830));
  await settle();
  assert.match(m.status().adoptError || '', /HTTP 500/, 'the next 4401 wiped the reason off the popup');
});

test('S5: a new endpoint in the 4401 drops the old re-authorize error', async () => {
  const m = await rejected({ local: makeStorageArea(provenStore()),
                             responses: [{ ok: false, status: 500, body: {} }] }, reasonFor(2830));
  await m.adoptServerToken();
  m.connectToMCPServer();
  m.sockets[1].fireOpen();
  await settle();
  m.sockets[1].fireClose(4401, reasonFor(2831));   // the server moved: a new offer
  await settle();
  assert.equal(m.status().adoptError, null);
});

test('S5: a 4401 naming an untrusted port is adoptable once Options names that MCP port', async () => {
  // What the popup's hint tells the user to do (popup.test.mjs pins the hint).
  const m = await rejected({ local: makeStorageArea(provenStore()) }, reasonFor(9000));
  assert.deepEqual(m.status(), { tokenRejected: true, tokenAdoptable: false, adoptError: null });
  m.fireStorageChange({ mcpPort: { newValue: 9000 } }, 'sync');
  m.connectToMCPServer();
  m.sockets[1].fireOpen();
  await settle();
  m.sockets[1].fireClose(4401, reasonFor(9000));
  await settle();
  assert.equal(m.status().tokenAdoptable, true);
});
