/*
 * Unit tests for the WebSocket connection lifecycle in extension-gecko/background.js:
 * connectToMCPServer / scheduleReconnect / disconnect.
 *
 * The hazard these lock down: a socket we REPLACED (forceReconnect, config change)
 * still fires its `close` event afterwards — late, when the successor is already
 * open. Without a socket-identity guard that stale handler clears isConnected,
 * stops the NEW connection's health probe and schedules a reconnect on top of a
 * perfectly healthy link; and scheduleReconnect stacking timers turns one retry
 * into several racing connect attempts.
 *
 * Like proxy.test.mjs / neterror.test.mjs: background.js is a top-level extension
 * script, so we slice the connection block out of the source and instantiate it
 * with a fake WebSocket, fake timers and stubbed browser/UI globals.
 * Run with: npm run test:js  (== node --test "tests/js/*.test.mjs")
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
// The connection block sends through the wire this registers on globalThis.
import '../../extension-gecko/ws-crypto.js';
// The re-keying guard (review S5) the connection block delegates to.
import '../../extension-gecko/token-trust.js';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

const RETRY_MS = 7777; // distinctive, so a reconnect timer is identifiable among the stubs

function loadConnection() {
  const start = BG_SRC.indexOf('function connectToMCPServer()');
  const end = BG_SRC.indexOf('// --- Server-health probe over the WS', start);
  assert.ok(start >= 0 && end > start, 'connection block not found in background.js');
  const block = BG_SRC.slice(start, end);

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const console = { log() {}, warn() {}, error() {}, trace() {} };
    const sockets = [];
    const probe = { starts: 0, stops: 0 };
    const phases = [];

    // Fake WebSocket: the close event fires only when a test says so — that IS the
    // late-close race we are testing.
    function WebSocket(url) {
      this.url = url;
      this.readyState = WebSocket.CONNECTING;
      this.sent = [];
      sockets.push(this);
    }
    WebSocket.CONNECTING = 0; WebSocket.OPEN = 1; WebSocket.CLOSING = 2; WebSocket.CLOSED = 3;
    WebSocket.prototype.send = function (m) { this.sent.push(m); };
    WebSocket.prototype.close = function () { this.readyState = WebSocket.CLOSING; };
    WebSocket.prototype.fireOpen = function () {
      this.readyState = WebSocket.OPEN;
      if (this.onopen) this.onopen();
    };
    WebSocket.prototype.fireClose = function () {
      this.readyState = WebSocket.CLOSED;
      if (this.onclose) this.onclose();
    };

    // Fake timers: nothing runs on its own; we only observe what is ARMED.
    let timerSeq = 1;
    const timers = new Map();
    function setTimeout(fn, ms) { const id = timerSeq++; timers.set(id, { fn, ms }); return id; }
    function clearTimeout(id) { timers.delete(id); }

    // Connection state — declared above the sliced block in background.js.
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

    function setConnectionPhase(p) { connectionPhase = p; phases.push(p); }
    function updateActionIndicator() {}
    function startHealthProbe() { probe.starts++; }
    // Lives past the sliced block (it is defined with the browser-state pushes,
    // below the health probe) but ws.onopen calls it — stub it like the probe.
    function sendBrowserState() {}
    function stopHealthProbe() { probe.stops++; }
    async function handleMessage() {}
    async function getProxyPermissionState() { return { has_proxy_permission: false }; }
    const window = {};
    const browser = {
      runtime: { getManifest: () => ({ version: 'test' }) },
      extension: { isAllowedFileSchemeAccess: async () => true },
      storage: { sync: { get: async (d) => d, set: async () => {} } }
    };

    ${block}

    return {
      connectToMCPServer, scheduleReconnect, disconnect, updateConfig,
      sockets, probe, phases,
      // Only scheduleReconnect arms a timer at retryInterval; onopen's debug
      // timeouts use other delays.
      reconnectTimersLive: () => [...timers.values()].filter(t => t.ms === ${RETRY_MS}).length,
      fireReconnectTimer: () => {
        for (const [id, t] of timers) {
          if (t.ms === ${RETRY_MS}) { timers.delete(id); t.fn(); return true; }
        }
        return false;
      },
      state: () => ({ isConnected, connectionPhase, retryAttempts, stoppedByUser,
                      current: websocket }),
      setStoppedByUser: (v) => { stoppedByUser = v; }
    };
  `);
  return factory();
}

// The health probe is the OTHER half of the connection lifecycle: it AWAITS a
// response, so it can resolve LATE — on a socket that has since been replaced —
// and then writes the GLOBAL health state. Sliced separately (the block above stubs
// startHealthProbe/stopHealthProbe out to counters, so it never exercises this).
function loadHealthProbe() {
  const start = BG_SRC.indexOf('// --- Server-health probe over the WS');
  const end = BG_SRC.indexOf('// --- Requests this extension sends TO the server', start);
  assert.ok(start >= 0 && end > start, 'health-probe block not found in background.js');
  const block = BG_SRC.slice(start, end);

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const console = { log() {}, warn() {}, error() {} };
    const WebSocket = { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 };
    const makeSocket = () => ({ readyState: WebSocket.OPEN, closed: false,
                                close() { this.closed = true; this.readyState = WebSocket.CLOSING; } });

    let websocket = null;
    let serverHealthy = null;
    let healthProbeFailures = 0;
    let healthOkThisConnection = false;
    let lastHealthOkAt = null;
    let lastServerMcpPort = null;
    let healthProbeTimer = null;
    let stoppedByUser = false;
    // Declared in the token block (above this slice in background.js): a probe that
    // ANSWERS proves the socket is authenticated, so it clears the bootstrap budget.
    let tokenBootstrapAttempts = 0;
    let lastAuthError = null;
    const HEALTH_PROBE_INTERVAL_MS = 15000;
    const HEALTH_PROBE_FAILURES_BEFORE_RECONNECT = 2;
    const CONFIG = { pingTimeout: 5000 };
    function updateActionIndicator() {}
    function markTokenProven() {}   // token section lives outside the sliced probe block

    // Every probe's request is parked here until the test settles it — that delay IS
    // the window in which the socket gets replaced.
    const pending = [];
    function requestFromServer() {
      return new Promise((resolve, reject) => pending.push({ resolve, reject }));
    }

    let intervals = 0;
    function setInterval() { intervals++; return intervals; }
    function clearInterval() {}

    ${block}

    return {
      probeServerHealth, startHealthProbe, stopHealthProbe, makeSocket,
      setSocket: (s) => { websocket = s; },
      resetConnectionState: () => {
        serverHealthy = null; healthProbeFailures = 0;
        healthOkThisConnection = false; lastServerMcpPort = null;
      },
      seed: (s) => {
        if (s.healthOkThisConnection !== undefined) healthOkThisConnection = s.healthOkThisConnection;
        if (s.healthProbeFailures !== undefined) healthProbeFailures = s.healthProbeFailures;
      },
      settle: (i, ok, value) => ok ? pending[i].resolve(value) : pending[i].reject(new Error(value || 'timed out')),
      pendingCount: () => pending.length,
      spendBootstrapBudget: () => { tokenBootstrapAttempts = 99; lastAuthError = 'rejected'; },
      state: () => ({ serverHealthy, healthProbeFailures, healthOkThisConnection,
                      lastServerMcpPort, current: websocket,
                      tokenBootstrapAttempts, lastAuthError })
    };
  `);
  return factory();
}

// ---- the stale-socket race --------------------------------------------------

test('a superseded socket\'s late close does NOT stomp the new connection', () => {
  const m = loadConnection();
  m.connectToMCPServer();
  const first = m.sockets[0];
  first.fireOpen();
  assert.equal(m.state().isConnected, true);

  // A config change replaces the socket (same shape as the popup's Reconnect).
  m.updateConfig({ port: 9999 }, false);
  assert.equal(m.sockets.length, 2, 'a replacement socket was opened');
  const second = m.sockets[1];
  second.fireOpen();

  const probesStopped = m.probe.stops;
  first.fireClose(); // the dead socket's close finally lands

  const s = m.state();
  assert.equal(s.isConnected, true, 'the live connection stays connected');
  assert.equal(s.connectionPhase, 'connected');
  assert.equal(s.current, second, 'the current socket is untouched');
  assert.equal(m.probe.stops, probesStopped, "the new connection's health probe was not killed");
  assert.equal(m.reconnectTimersLive(), 0, 'no redundant reconnect was scheduled');
});

test('the CURRENT socket closing still schedules a reconnect (server restart / health drop)', () => {
  const m = loadConnection();
  m.connectToMCPServer();
  const sock = m.sockets[0];
  sock.fireOpen();

  sock.fireClose();

  const s = m.state();
  assert.equal(s.isConnected, false);
  assert.equal(s.connectionPhase, 'waiting');
  assert.equal(m.probe.stops, 1, 'health probe stopped with its socket');
  assert.equal(m.reconnectTimersLive(), 1, 'the reconnect loop is armed');
});

test('a user Stop is honoured by the close handler (no auto-reconnect)', () => {
  const m = loadConnection();
  m.connectToMCPServer();
  const sock = m.sockets[0];
  sock.fireOpen();

  m.setStoppedByUser(true);
  sock.fireClose();

  assert.equal(m.state().connectionPhase, 'stopped');
  assert.equal(m.reconnectTimersLive(), 0, 'a stopped connection never re-arms');
});

// ---- reconnect timers must not stack ----------------------------------------

test('scheduleReconnect never stacks timers', () => {
  const m = loadConnection();
  m.scheduleReconnect();
  m.scheduleReconnect();
  m.scheduleReconnect();
  assert.equal(m.reconnectTimersLive(), 1, 'one pending retry, not three');
  assert.equal(m.state().retryAttempts, 3);
});

test('starting a connect disarms the pending retry (the watchdog / Reconnect path)', () => {
  const m = loadConnection();
  m.scheduleReconnect();
  assert.equal(m.reconnectTimersLive(), 1);

  m.connectToMCPServer(); // e.g. the 1-minute watchdog alarm, or the popup's Reconnect

  assert.equal(m.reconnectTimersLive(), 0, 'the redundant retry was cancelled');
  assert.equal(m.sockets.length, 1, 'exactly one connection attempt is in flight');
});

test('a fired retry does not leave a stale timer handle behind', () => {
  const m = loadConnection();
  m.scheduleReconnect();
  assert.equal(m.fireReconnectTimer(), true);
  assert.equal(m.sockets.length, 1, 'the retry connected');

  // The connect that the retry started must be able to schedule the NEXT retry.
  m.sockets[0].fireClose();
  assert.equal(m.reconnectTimersLive(), 1);
});

// ---- the stale HEALTH PROBE race --------------------------------------------

test('a probe that FAILS on a superseded socket cannot drop the new one', async () => {
  const m = loadHealthProbe();
  const first = m.makeSocket();
  m.setSocket(first);
  // The old socket had answered before, and had already missed one probe: one more
  // failure is what drops a socket.
  m.seed({ healthOkThisConnection: true, healthProbeFailures: 1 });

  const probe = m.probeServerHealth();          // in flight on `first`
  const second = m.makeSocket();                // reconnect: a brand-new socket + state
  m.setSocket(second);
  m.resetConnectionState();

  m.settle(0, false, 'Request to server timed out'); // the OLD socket's probe finally gives up
  await probe;

  const s = m.state();
  assert.equal(second.closed, false, 'the healthy new socket was NOT dropped by the old probe');
  assert.equal(s.healthProbeFailures, 0, "the old socket's failure was not charged to the new connection");
  assert.equal(s.serverHealthy, null, 'health state untouched — no probe has answered on this socket yet');
});

test('a probe that SUCCEEDS on a superseded socket does not certify the new one', async () => {
  const m = loadHealthProbe();
  m.setSocket(m.makeSocket());

  const probe = m.probeServerHealth();
  m.setSocket(m.makeSocket());
  m.resetConnectionState();

  m.settle(0, true, { mcp_port: 3456 });        // the dead socket's late "I'm fine"
  await probe;

  const s = m.state();
  assert.equal(s.serverHealthy, null, 'a stale OK must not turn the new (unverified) connection green');
  assert.equal(s.healthOkThisConnection, false);
  assert.equal(s.lastServerMcpPort, null);
});

test('a probe on the CURRENT socket still drives the health state (and drops a wedged socket)', async () => {
  const m = loadHealthProbe();
  const sock = m.makeSocket();
  m.setSocket(sock);

  const ok = m.probeServerHealth();
  m.settle(0, true, { mcp_port: 3456 });
  await ok;
  let s = m.state();
  assert.equal(s.serverHealthy, true);
  assert.equal(s.lastServerMcpPort, 3456, 'the server\'s real MCP port is remembered');

  // It then goes silent: two failures on a socket that HAD answered = wedged.
  const f1 = m.probeServerHealth(); m.settle(1, false); await f1;
  assert.equal(m.state().serverHealthy, false);
  assert.equal(sock.closed, false, 'one miss is not enough');
  const f2 = m.probeServerHealth(); m.settle(2, false); await f2;
  s = m.state();
  assert.equal(s.healthProbeFailures, 2);
  assert.equal(sock.closed, true, 'the wedged socket is dropped so the reconnect loop can recover');
});

test('a probe on a socket that is no longer OPEN never even asks', async () => {
  const m = loadHealthProbe();
  const sock = m.makeSocket();
  sock.readyState = 3; // CLOSED
  m.setSocket(sock);

  await m.probeServerHealth();
  assert.equal(m.pendingCount(), 0);
});

test('a probe that answers proves we are authenticated — it refills the token budget', async () => {
  // The server ANSWERING is the only proof this socket got past the token check. That
  // refill is what lets a LATER 4401 (the user rotated the token) self-heal instead of
  // inheriting an exhausted counter from an old failure.
  const m = loadHealthProbe();
  m.setSocket(m.makeSocket());
  m.spendBootstrapBudget();

  const ok = m.probeServerHealth();
  m.settle(0, true, { mcp_port: 3456 });
  await ok;

  const s = m.state();
  assert.equal(s.tokenBootstrapAttempts, 0, 'the bootstrap budget is restored');
  assert.equal(s.lastAuthError, null, 'and the stale auth error is cleared');
});

// ---- disconnect -------------------------------------------------------------

test('disconnect stops the health probe and drops the socket', () => {
  const m = loadConnection();
  m.connectToMCPServer();
  m.sockets[0].fireOpen();

  m.disconnect();

  const s = m.state();
  assert.equal(s.isConnected, false);
  assert.equal(s.current, null);
  assert.equal(m.probe.stops, 1);
  // The socket it closed is no longer current, so its close is inert.
  m.sockets[0].fireClose();
  assert.equal(m.reconnectTimersLive(), 0, 'a disconnect()ed socket schedules nothing');
});
