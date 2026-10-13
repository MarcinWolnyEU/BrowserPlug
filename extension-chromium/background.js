/*
 * BrowserPlug Chromium Extension - Background Service Worker (MV3 port of extension-gecko/background.js)
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * MV3 service worker. Classic worker (NOT type:module): importScripts() loads the
 * namespace shim, the baked test/build config, the shared toolbar-icon module and the
 * two vendor DATA modules, exactly as gecko's background.scripts array does. Order
 * matters for toolbar-icon.js: it reads globalThis.browser at load, so shim.js first.
 * Everything below is the MV3 port of the gecko persistent background page; same WS
 * wire contract, same 51 action strings, same error-code vocabulary; the platform
 * mechanisms (injection, screenshots, body capture, clipboard, proxy) are adapted per
 * the chromium design doc.
 */
importScripts('shim.js', 'config.js', 'toolbar-icon.js', 'scheme-probe.js', 'connection-journal.js', 'vendor/mlkem.js', 'ws-crypto.js', 'body-censor.js', 'token-trust.js', 'vendor/period-history.js', 'vendor/anthropic-data.js', 'vendor/openai-data.js', 'vendor/copilot-data.js', 'vendor/kilocode-modelnames-data.js', 'vendor/usage-sampler.js');

// SINGLE CONNECTION CONSTRAINT: Only one WebSocket connection to MCP server allowed
let websocket = null;
let isConnected = false;
let connectionRetryAttempts = 0;


// Debug logging configuration - set to true to send extension logs to server
const ENABLE_DEBUG_LOGGING_TO_SERVER = false;

// Enhanced console logging that sends to server when available
if (ENABLE_DEBUG_LOGGING_TO_SERVER) {
  const originalConsoleLog = console.log;
  const originalConsoleError = console.error;

  // Buffer to store logs before WebSocket connection is established
  let logBuffer = [];

  function createLogMessage(level, args) {
    return {
      id: `debug_${Date.now()}`,
      type: "debug_log",
      action: "extension.debug",
      data: {
        level: level,
        message: args.map(arg => typeof arg === 'object' ? JSON.stringify(arg) : String(arg)).join(' '),
        timestamp: new Date().toISOString()
      }
    };
  }

  function sendLogMessage(logMessage) {
    if (websocket && websocket.readyState === WebSocket.OPEN) {
      try {
        return wireSend(logMessage);
      } catch (e) {
        // Ignore errors sending debug logs
      }
    }
    return false;
  }

  function flushLogBuffer() {
    if (logBuffer.length > 0 && websocket && websocket.readyState === WebSocket.OPEN) {
      const bufferedLogs = [...logBuffer];
      logBuffer = [];
      bufferedLogs.forEach(logMessage => {
        sendLogMessage(logMessage);
      });
    }
  }

  function enhancedLog(...args) {
    originalConsoleLog(...args);
    const logMessage = createLogMessage("log", args);
    if (!sendLogMessage(logMessage)) {
      logBuffer.push(logMessage);
      if (logBuffer.length > 100) logBuffer = logBuffer.slice(-50);
    }
  }

  function enhancedError(...args) {
    originalConsoleError(...args);
    const logMessage = createLogMessage("error", args);
    if (!sendLogMessage(logMessage)) {
      logBuffer.push(logMessage);
      if (logBuffer.length > 100) logBuffer = logBuffer.slice(-50);
    }
  }

  console.log = enhancedLog;
  console.error = enhancedError;

  // SW has no `window`; the flush hook lives on `self` (the worker global).
  self.flushDebugLogBuffer = flushLogBuffer;
}

// Default configuration - will be loaded from storage
let CONFIG = {
  hostname: 'localhost',
  port: 8772,
  retryInterval: 5000, // milliseconds (5 seconds default)
  maxRetries: -1, // -1 for infinite retries, or set a number
  pingTimeout: 5000 // ping timeout in milliseconds
};

let retryAttempts = 0;
const MAX_ABSOLUTE_RETRIES = 50; // Absolute maximum to prevent infinite loops

// Connection state surfaced to the popup for a live status line.
let reconnectTimer = null;
let connectionPhase = 'connecting'; // connected | connecting | waiting | stopped | gaveup
let nextRetryAt = null;             // epoch ms of the next scheduled reconnect (during 'waiting')
let stoppedByUser = false;          // user clicked Stop; suppress auto-reconnect + watchdog
let waitingTicker = null;           // 1s interval that ticks the toolbar countdown while 'waiting'

// Server health verified OVER the WebSocket, not just "socket is OPEN".
let serverHealthy = null;
let lastHealthOkAt = null;          // epoch ms of the last successful probe
let healthProbeTimer = null;        // setInterval handle while connected
let healthProbeFailures = 0;        // consecutive failures (drives the auto-reconnect)
let healthOkThisConnection = false; // has any probe succeeded since this socket opened?
let lastServerMcpPort = null;
const HEALTH_PROBE_INTERVAL_MS = 15000;   // <=20s: doubles as the SW keepalive (WS traffic resets the idle timer, Chrome 116+)
const HEALTH_PROBE_FAILURES_BEFORE_RECONNECT = 2;

// --- Live toolbar indicator ------------------------------------------------
// MV3: browser_action -> action. Identical colours and tooltips per phase, and the
// same collapse of the six phases onto four icon states, handed to the shared
// toolbar-icon.js, which picks the light or dark glyph set from a colour scheme
// that scheme-probe.js reads for it, because a service worker can see neither a theme
// nor prefers-color-scheme.
//
// The badge carries no TEXT (the icon says what its glyphs used to), so the
// Firefox-only setBadgeTextColor Chrome lacks is gone from both trees along with
// the text it coloured. The badge COLOUR is still written: with the text empty
// nothing renders it, and the popup reads it back as the one piece of connection
// state it can have synchronously on first paint -- see renderConnection() in
// popup/popup.js.
function updateActionIndicator() {
  const ba = browser.action;
  if (!ba || !ba.setBadgeText) return; // API unavailable (e.g. during teardown)

  const wsUrl = `ws://${CONFIG.hostname}:${CONFIG.port}`;
  let color, title, icon;
  switch (connectionPhase) {
    case 'connected':
      if (serverHealthy === false) {
        // Seated plug, amber: plugged in with nothing answering, not still dialling.
        color = '#f0ad4e'; icon = 'unhealthy';
        title = `BrowserPlug: Connected, but server not responding to health checks - ${wsUrl}`;
      } else if (serverHealthy === null) {
        // Just connected; first health probe is in flight. Brief, sub-second.
        color = '#1976d2'; icon = 'connecting'; title = `BrowserPlug: Connected, verifying server health… (${wsUrl})`;
      } else {
        // The one state somebody hovers to confirm rather than to diagnose, so it
        // answers "what is this button for?" instead of naming the socket.
        color = '#2e7d32'; icon = 'connected';
        title = 'BrowserPlug: Browser is connected to server which then can be used by AI Agents through MCP';
      }
      break;
    case 'connecting':
      color = '#1976d2'; icon = 'connecting'; title = `BrowserPlug: Connecting to ${wsUrl}…`;
      break;
    case 'waiting': {
      let when = '';
      if (nextRetryAt) {
        const s = Math.max(0, Math.ceil((nextRetryAt - Date.now()) / 1000));
        when = `, next try in ~${s}s`;
      }
      const max = (CONFIG.maxRetries > 0) ? `/${CONFIG.maxRetries}` : '';
      color = '#f0ad4e'; icon = 'connecting';
      title = `BrowserPlug: Reconnecting, attempt ${retryAttempts}${max}${when}`;
      break;
    }
    case 'stopped':
      color = '#757575'; icon = 'disconnected'; title = `BrowserPlug: Stopped`;
      break;
    case 'gaveup':
      color = '#757575'; icon = 'disconnected'; title = `BrowserPlug: Cannot connect after ${retryAttempts} attempts - ${wsUrl}`;
      break;
    default:
      color = '#757575'; icon = 'disconnected'; title = `BrowserPlug: Disconnected`;
  }

  try {
    // Written empty on every pass, not once at startup: an in-place update leaves
    // the old build's glyph sitting on the button until the browser restarts.
    ba.setBadgeText({ text: '' });
    ba.setBadgeBackgroundColor({ color });
    ba.setTitle({ title });
    if (globalThis.browserplugToolbarIcon) globalThis.browserplugToolbarIcon.apply(icon);
  } catch (e) {
    console.error('updateActionIndicator failed:', e);
  }
}

function setConnectionPhase(phase) {
  connectionPhase = phase;
  updateActionIndicator();
  if (phase === 'waiting') {
    if (!waitingTicker) waitingTicker = setInterval(updateActionIndicator, 1000);
  } else if (waitingTicker) {
    clearInterval(waitingTicker);
    waitingTicker = null;
  }
}

function connectToMCPServer() {
  try {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

    // Guard against multiple concurrent connects on rapid SW wake-ups (init +
    // watchdog alarm + a webRequest event can all fire close together): a socket
    // already CONNECTING is doing exactly this job; don't start a second race.
    if (websocket && websocket.readyState === WebSocket.CONNECTING) {
      return;
    }

    // Only one WebSocket at a time; disconnect any existing before creating a new one.
    if (websocket && websocket.readyState !== WebSocket.CLOSED) {
      console.log('🔌 Disconnecting existing connection before creating new one');
      disconnect();
    }

    const WS_URL = `ws://${CONFIG.hostname}:${CONFIG.port}`;
    console.log(`🔗 Connecting to ${WS_URL} (attempt ${retryAttempts + 1})`);

    nextRetryAt = null;
    serverHealthy = null;
    healthProbeFailures = 0;
    healthOkThisConnection = false;
    lastServerMcpPort = null;
    handshakeToken = null;
    setConnectionPhase('connecting');
    websocket = new WebSocket(WS_URL);

    // A socket we replaced still fires its events LATE; every handler bails unless
    // it is still THE current socket.
    const ws = websocket;
    // Everything that crosses this socket goes through its wire (ws-crypto.js): the
    // encrypted handshake when we hold a token, plaintext only for the token bootstrap.
    // Without the file nothing is sent at all; a plaintext fallback would put the token
    // back on the network.
    ws.binaryType = 'arraybuffer';
    const w = globalThis.BrowserplugWsCrypto
      ? globalThis.BrowserplugWsCrypto.createWire(ws, {
          onMessage: (text) => {
            if (websocket !== ws) return;
            let message;
            try {
              message = JSON.parse(text);
            } catch (e) {
              console.error('Unreadable frame from the server:', e);
              return;
            }
            Promise.resolve(handleMessage(message)).catch((e) => {
              console.error('Failed to handle a server message:', e);
            });
          },
          onFailure: (e) => {
            if (websocket !== ws) return;
            journalNote('error', { reason: `wire: ${(e && e.message) || e}` });
          }
        })
      : null;
    if (!w) console.error('ws-crypto.js did not load; this connection cannot send anything');
    wire = w;

    ws.onopen = () => {
      if (websocket !== ws) return;
      console.log('Connected to MCP server');
      journalNote('open');
      isConnected = true;
      retryAttempts = 0;
      connectionRetryAttempts = 0;
      stoppedByUser = false;
      nextRetryAt = null;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      setConnectionPhase('connected');

      // The server gives the hello a deadline and drops a socket that misses it, so
      // this sends from the cache when there is one and awaits nothing.
      sendExtensionHelloNow();
      // …and what this browser currently holds (window/tab counts for the map).
      sendBrowserState();
      startHealthProbe();

      setTimeout(() => {
        console.log('🔌 WebSocket connection established and ready');
      }, 200);

      if (typeof self.flushDebugLogBuffer === 'function') {
        setTimeout(() => { self.flushDebugLogBuffer(); }, 100);
      }
    };

    ws.onmessage = (event) => {
      if (websocket !== ws || !w) return;
      w.receive(event.data);
    };

    ws.onclose = (event) => {
      if (w) w.socketClosed(event && event.code, event && event.reason);
      if (websocket !== ws) return;
      console.log('Disconnected from MCP server');
      // A browser-wide proxy lives only as long as the connection that asked for it:
      // with the server gone, so is the Tor it pointed at (clearBrowserProxy is async
      // and idempotent; nothing here waits for it).
      clearBrowserProxy();
      isConnected = false;
      stopHealthProbe();
      serverHealthy = null;
      if (event && event.code === WS_CLOSE_UNAUTHORIZED) {
        // Journalled as `rejected` by handleUnauthorized, which is the one that knows
        // a 4401 from an ordinary close.
        handleUnauthorized(event.reason);
        return;
      }
      journalNote('close', { code: event && event.code, reason: event && event.reason });
      // Not a 4401: whatever refused the token is not what answers (or fails to) now, so
      // the popup stops saying "token rejected" and shows the real state.
      if (tokenTrust) tokenTrust.clearRejection();
      if (stoppedByUser) {
        nextRetryAt = null;
        setConnectionPhase('stopped');
      } else {
        scheduleReconnect();
      }
    };

    ws.onerror = (error) => {
      if (websocket !== ws) return;
      // Not console.error: an unreachable server is a normal state (browser up, server not
      // started yet), and error-level output lands in the extension's error list. The journal
      // below is the record.
      console.debug('WebSocket error:', error);
      journalNote('error', { reason: (error && error.message) || 'socket error' });
    };
  } catch (error) {
    console.error('Failed to connect to MCP server:', error);
    journalNote('error', { reason: (error && error.message) || String(error) });
    scheduleReconnect();
  }
}

// The current socket's wire (ws-crypto.js createWire), replaced with the socket.
let wire = null;

// The one way a JSON frame reaches the server. Frames sent before the hello is out wait
// behind it inside the wire, so nothing overtakes the handshake. Returns false when there
// is no open socket to send on.
function wireSend(message) {
  if (!websocket || websocket.readyState !== WebSocket.OPEN || !wire) return false;
  return wire.send(JSON.stringify(message));
}

// --- Connection journal ------------------------------------------------------
// The ring buffer, its storage write-through and the since-last-hello window live in
// the shared connection-journal.js, which registers the singleton these four helpers
// reach for. They are guarded rather than referencing it directly because the JS twin
// suites evaluate this block on its own, and a connection that stops working because
// a diagnostic file failed to load would be the wedge this exists to explain. On MV3
// the ring is worker memory, so an eviction empties it; the storage copy is what the
// next wake reads back.
function journalNote(event, fields) {
  const j = globalThis.browserplugJournal;
  if (!j) return null;
  try { return j.note(event, fields); } catch (e) { return null; }
}

function journalSinceLastHello() {
  const j = globalThis.browserplugJournal;
  if (!j) return [];
  try { return j.sinceLastHello(); } catch (e) { return []; }
}

function journalHelloSent() {
  const j = globalThis.browserplugJournal;
  if (!j) return;
  try { j.noteHelloSent(); } catch (e) { /* the frame is already on the wire */ }
}

function journalRecent(n) {
  const j = globalThis.browserplugJournal;
  if (!j) return [];
  try { return j.recent(n); } catch (e) { return []; }
}

// Report the extension's local-file (file://) reading capability for /health.
// Chromium: extension.isAllowedFileSchemeAccess() exists in Chrome and is the
// accurate signal (no getBrowserInfo version gate; that API is Firefox-only).
// browser_major is the BROWSER's own major version, parsed from the UA string
// (the honest analog of gecko's Firefox major; informational on chromium).
function chromeMajorFromUA() {
  try {
    const m = /Chrom(?:e|ium)\/(\d+)/.exec((self.navigator && self.navigator.userAgent) || '');
    return m ? parseInt(m[1], 10) : null;
  } catch (e) { return null; }
}

async function getFileAccessState() {
  const browserMajor = chromeMajorFromUA();
  let canReadFiles = null;
  try {
    if (browser.extension && browser.extension.isAllowedFileSchemeAccess) {
      canReadFiles = await browser.extension.isAllowedFileSchemeAccess();
    }
  } catch (e) {
    console.error('isAllowedFileSchemeAccess failed:', e);
  }
  return { can_read_files: canReadFiles, browser_major: browserMajor };
}

// --- Browser identity -------------------------------------------------------
// The server keys its browser registry on this id, so it must be STABLE across a
// browser restart AND an extension reload; hence minted once and persisted.
// storage.LOCAL, never storage.sync (sync would clone the identity to sibling profiles).
const INSTANCE_ID_KEY = 'instanceId';
let instanceIdPromise = null;

async function getInstanceId() {
  if (!instanceIdPromise) {
    instanceIdPromise = (async () => {
      const stored = await browser.storage.local.get({ [INSTANCE_ID_KEY]: null });
      if (stored && stored[INSTANCE_ID_KEY]) return stored[INSTANCE_ID_KEY];
      const id = crypto.randomUUID();
      await browser.storage.local.set({ [INSTANCE_ID_KEY]: id });
      return id;
    })().catch((e) => {
      instanceIdPromise = null;
      throw e;
    });
  }
  return instanceIdPromise;
}

// --- Which browser is this, really? -----------------------------------------
// Chromium has NO runtime.getBrowserInfo(). We SYNTHESIZE the browserInfo object
// from navigator.userAgentData (brands + high-entropy version list) plus a
// navigator.brave probe, mirroring gecko's whole-object forwarding: send the full
// userAgentData so a brand key we didn't anticipate (Edge/Brave/…) survives; the
// same fork-key-survival lesson as gecko forwarding getBrowserInfo() verbatim.
function pickPrimaryBrand(list) {
  if (!Array.isArray(list)) return null;
  const isPlaceholder = (b) => /not.?[a/].?brand/i.test(String(b || '')) || /not\/a\)brand/i.test(String(b || ''));
  // Prefer a real branded name (not Chromium, not the "Not;A=Brand" GREASE placeholder).
  let pref = list.find(e => e && e.brand && !isPlaceholder(e.brand) && !/^chromium$/i.test(e.brand));
  if (pref) return pref;
  let chromium = list.find(e => e && e.brand && /^chromium$/i.test(e.brand));
  if (chromium) return chromium;
  return list.find(e => e && e.brand && !isPlaceholder(e.brand)) || null;
}

async function buildBrowserInfo() {
  const info = { engine: 'blink' };
  try {
    const uaData = self.navigator && self.navigator.userAgentData;
    if (uaData) {
      info.userAgentData = { brands: uaData.brands, mobile: uaData.mobile, platform: uaData.platform };
      let high = null;
      try {
        high = await uaData.getHighEntropyValues([
          'fullVersionList', 'platform', 'platformVersion', 'architecture', 'bitness', 'uaFullVersion'
        ]);
      } catch (e) { /* high-entropy hints unavailable; keep the low-entropy brands */ }
      if (high) {
        info.userAgentData.fullVersionList = high.fullVersionList;
        info.userAgentData.platformVersion = high.platformVersion;
        info.userAgentData.architecture = high.architecture;
        info.userAgentData.bitness = high.bitness;
        info.userAgentData.uaFullVersion = high.uaFullVersion;
      }
      const list = (high && high.fullVersionList) || uaData.brands || [];
      const primary = pickPrimaryBrand(list);
      if (primary) {
        info.name = primary.brand;
        if (primary.version) info.version = primary.version;
      }
    }
  } catch (e) { /* degrade to the fallbacks below */ }
  // Brave outs itself the same way Zen's zen:{version} key does; a private key
  // that only that browser sets. Send the whole thing.
  try {
    const brave = self.navigator && self.navigator.brave;
    if (brave && typeof brave.isBrave === 'function') {
      const isBrave = await brave.isBrave();
      if (isBrave) { info.name = 'Brave'; info.brave = { isBrave: true }; }
    }
  } catch (e) { /* not brave */ }
  try { info.userAgent = self.navigator ? self.navigator.userAgent : undefined; } catch (e) {}
  try {
    info.vendor = (self.navigator && typeof self.navigator.vendor === 'string' && self.navigator.vendor)
      ? self.navigator.vendor : 'Google Inc.';
  } catch (e) { info.vendor = 'Google Inc.'; }
  if (!info.name) info.name = 'Chromium';
  return info;
}

async function getBrowserIdentity() {
  const identity = { instance_id: null, platform: null, browserInfo: null };
  try {
    identity.instance_id = await getInstanceId();
  } catch (e) {
    console.error('instance_id unavailable:', e);
  }
  try {
    if (browser.runtime.getPlatformInfo) {
      const p = await browser.runtime.getPlatformInfo();
      identity.platform = { os: p.os, arch: p.arch };
    }
  } catch (e) {
    console.error('getPlatformInfo failed:', e);
  }
  try {
    identity.browserInfo = await buildBrowserInfo();
  } catch (e) {
    console.error('buildBrowserInfo failed:', e);
  }
  return identity;
}

// --- The WebSocket token ----------------------------------------------------
// Precedence: manual `connectionToken` (storage.SYNC, typed by user) wins over auto
// `autoToken` (storage.LOCAL, self-fetched after a 4401). Identical to gecko.
const AUTO_TOKEN_KEY = 'autoToken';
const WS_CLOSE_UNAUTHORIZED = 4401;
const TOKEN_ENDPOINT_LOOPBACK_HOSTS = new Set(['127.0.0.1', '::1', 'localhost']);
const MAX_TOKEN_BOOTSTRAP_ATTEMPTS = 3;
let tokenBootstrapAttempts = 0;
let lastAuthError = null;

async function getManualToken() {
  try {
    const { connectionToken } = await browser.storage.sync.get({ connectionToken: '' });
    return typeof connectionToken === 'string' ? connectionToken.trim() : '';
  } catch (e) {
    console.error('Failed to read the connection token:', e);
    return '';
  }
}

async function getAutoToken() {
  try {
    const stored = await browser.storage.local.get({ [AUTO_TOKEN_KEY]: '' });
    const token = stored && stored[AUTO_TOKEN_KEY];
    return typeof token === 'string' ? token.trim() : '';
  } catch (e) {
    console.error('Failed to read the auto-pulled token:', e);
    return '';
  }
}

async function getConnectionToken() {
  const manual = await getManualToken();
  if (manual) return manual;
  return getAutoToken();
}

// Only a LOOPBACK http URL is ever honoured (ported verbatim from gecko).
function parseTokenEndpoint(reason) {
  const match = /\bhttps?:\/\/\S+/.exec(String(reason || ''));
  if (!match) return null;
  let url;
  try {
    url = new URL(match[0]);
  } catch (e) {
    return null;
  }
  const host = url.hostname.replace(/^\[|\]$/g, '');   // [::1] -> ::1
  if (url.protocol !== 'http:' || !TOKEN_ENDPOINT_LOOPBACK_HOSTS.has(host)) {
    console.warn('Ignoring a non-loopback token endpoint offered by the server');
    return null;
  }
  return url.href;
}

// --- Re-keying guard (review S5) ---------------------------------------------
// token-trust.js holds the rules: when the auto-pulled token may be replaced without a
// click, which token endpoints are honoured, and the "token rejected" state. These
// wrappers are the names the rest of this file (and the JS twins) call.
const tokenTrust = globalThis.BrowserplugTokenTrust
  ? globalThis.BrowserplugTokenTrust.create({ storage: () => browser.storage.local })
  : null;
let handshakeToken = null;   // the token this socket's handshake used; null until it starts

// Without the module no token endpoint is trusted, so every 4401 waits for the user.
async function isTokenProven() {
  return tokenTrust ? tokenTrust.isProven() : true;
}

// Every health answer proves this socket is authenticated.
async function markTokenProven(mcpPort) {
  if (!tokenTrust) return;
  await tokenTrust.noteAuthenticated({
    encrypted: !!(wire && wire.encrypted),
    handshakeToken,
    readAutoToken: getAutoToken,
    mcpPort
  });
}

function endpointMatchesConfig(href) {
  return !!tokenTrust && tokenTrust.endpointMatches(href, CONFIG);
}

function tokenTrustStatus() {
  return tokenTrust ? tokenTrust.status()
    : { tokenRejected: false, tokenAdoptable: false, adoptError: null };
}

// The user's Re-authorize click: token-trust.js checks, fetches and stores; this reconnects.
async function adoptServerToken() {
  if (!tokenTrust) return { ok: false, error: 'token-trust.js did not load' };
  const res = await tokenTrust.adopt(CONFIG, {
    fetch: (url, init) => fetch(url, init),
    storeToken: (token) => browser.storage.local.set({ [AUTO_TOKEN_KEY]: token }),
    journal: journalNote
  });
  if (!res.ok) return res;
  lastAuthError = null;
  tokenBootstrapAttempts = 0;
  invalidateHelloCache();
  journalNote('reconnect', { reason: 'new token' });
  connectToMCPServer();
  return res;
}

// The server's close reason for "your socket opened but no hello arrived in time".
// It leads with this phrase precisely so the extension can tell the two 4401s apart;
// a token rejection still reads "token required: …".
const HELLO_TIMEOUT_REASON = 'hello timeout';

function isHelloTimeoutReason(reason) {
  return String(reason || '').trim().toLowerCase().startsWith(HELLO_TIMEOUT_REASON);
}

async function handleUnauthorized(reason) {
  lastAuthError = String(reason || 'the server rejected this connection (no valid token)');
  // Not an error yet. A browser on its first run has no token, the server says so, and
  // the fetch below answers it in milliseconds; Chrome lists every console.error on
  // chrome://extensions under Errors, so logging this line there reported a working
  // bootstrap as a failure. Each path below that really fails logs `rejected` itself.
  const rejected = `Server rejected the connection: ${lastAuthError}`;
  journalNote('rejected', { code: WS_CLOSE_UNAUTHORIZED, reason: lastAuthError });

  if (stoppedByUser) {
    nextRetryAt = null;
    console.log(rejected);
    setConnectionPhase('stopped');
    return;
  }

  // Not a verdict on our credential: the server never saw a hello inside its window.
  // On a browser thrashing at the edge of its memory that is a timing failure, and
  // spending an HTTP round trip to re-fetch a token we already hold makes the next
  // attempt slower, which is the wrong direction. Reconnect instead — onopen sends
  // the cached hello with nothing to await.
  if (isHelloTimeoutReason(reason)) {
    console.warn('The server closed us for a late hello, not a bad token; reconnecting ' +
                 'without a token fetch.');
    scheduleReconnect();
    return;
  }

  const manual = await getManualToken();
  if (manual) {
    console.error(`${rejected}. ` +
                  'The token set in Options was rejected by the server. Check it against ' +
                  'the value the server printed; or clear it, and a browser on the same ' +
                  'machine as the server will configure itself.');
    // Manual wins, so adopting an auto token could not fix this; drop any offer to.
    if (tokenTrust) tokenTrust.clearRejection();
    scheduleReconnect();
    return;
  }

  const endpoint = parseTokenEndpoint(reason);
  if (await isTokenProven()) {
    // A token that already worked is never replaced on the server's say-so alone.
    if (tokenTrust) tokenTrust.noteRejected(endpoint, CONFIG);
    journalNote('token_fetch', { reason: 'needs user' });
    console.warn(`${rejected}. The stored token worked before; not replacing it ` +
                 'automatically. Re-authorize from the popup to adopt the server\'s token.');
    scheduleReconnect();
    return;
  }

  if (endpoint && !endpointMatchesConfig(endpoint)) {
    console.error(`${rejected}. The token endpoint is not on the origin this browser is ` +
                  'configured for; ignoring it.');
    journalNote('token_fetch', { reason: 'endpoint refused' });
    scheduleReconnect();
    return;
  }

  if (tokenBootstrapAttempts >= MAX_TOKEN_BOOTSTRAP_ATTEMPTS) {
    console.error(`${rejected}. Gave up fetching a token from the server; ` +
                  'falling back to the normal ' +
                  'reconnect backoff.');
    scheduleReconnect();
    return;
  }

  if (!endpoint) {
    console.error(rejected);
    scheduleReconnect();
    return;
  }

  console.log(`The server asked for a token; fetching it from ${endpoint}`);
  tokenBootstrapAttempts++;
  let token = '';
  try {
    const response = await fetch(endpoint, { cache: 'no-store' });
    if (!response.ok) {
      journalNote('token_fetch', { reason: `http ${response.status}` });
      throw new Error(`HTTP ${response.status}`);
    }
    const body = await response.json();
    token = (body && typeof body.token === 'string') ? body.token.trim() : '';
  } catch (e) {
    console.error(`${rejected}. Could not fetch a token from the server: ${e && e.message}`);
    scheduleReconnect();
    return;
  }
  if (!token) {
    console.error(`${rejected}. The server's token endpoint returned no token.`);
    journalNote('token_fetch', { reason: 'empty' });
    scheduleReconnect();
    return;
  }

  if (token === await getAutoToken()) {
    console.error(`${rejected}. The server rejected the very token it hands out; backing off.`);
    journalNote('token_fetch', { reason: 'same token' });
    scheduleReconnect();
    return;
  }

  try {
    await browser.storage.local.set({ [AUTO_TOKEN_KEY]: token });
  } catch (e) {
    console.error(`${rejected}. Could not store the token from the server:`, e);
    journalNote('token_fetch', { reason: 'store failed' });
    scheduleReconnect();
    return;
  }

  journalNote('token_fetch', { reason: 'stored' });
  // A hello built before this moment would present the token the server just rejected.
  invalidateHelloCache();
  console.log('Configured this browser with the local server\'s token; reconnecting');
  journalNote('reconnect', { reason: 'new token' });
  connectToMCPServer();
}

// Chromium cannot route one tab (chrome.proxy is browser-global), so `proxy` here switches
// the WHOLE browser and sits behind the optional "proxy" permission. The hello reports the
// grant, and proxy_scope tells the server which of the two meanings it is dealing with.
async function getProxyPermissionState() {
  return { has_proxy_permission: await hasProxyPermission(), proxy_supported: true,
           proxy_scope: 'browser' };
}

// Whether a CONTENT script has been observed reporting a documentId. Assigned by
// noteContentDocIdSupport (down by runAutomation) the first time the toolkit answers, and
// declared HERE so it sits inside the same region as its reader — the hello slice and the
// runAutomation slice are taken separately by the JS twins, and a reference that escapes
// its slice breaks the harness rather than the product. null = not yet observed; an SW
// eviction resets it to null, which is honest.
let CONTENT_DOC_ID_SUPPORT = null;

// Which optional platform APIs this browser actually has, probed rather than inferred
// from a version string.
//
// documentId: a stable per-document identity. frameId is NOT one — ids are reused, and a
// navigation replaces the document underneath the same frameId.
//
// On Blink this probe reports FALSE, and that is the correct answer rather than a bug.
// MV3 carries documentId as a FIELD on webNavigation/webRequest events and scripting
// targets, but Chrome ships no `runtime.getDocumentId(window)`; that method is Firefox
// 153+. The two are easy to conflate, and conflating them is what this probe caught:
// live-measured 2026-08-05, Chrome Canary 150 answers false while all three Gecko
// browsers answer true. Blink's document identity must therefore come from the
// BACKGROUND (documentIdForFrame below), never from a content script — which also means
// CONTENT_DOC_ID_SUPPORT is expected to settle on false here.
function getApiSupport() {
  // Read through a local alias, as the gecko twin does: addons-linter checks every
  // literal `browser.runtime.<name>` chain against the declared minimum version and
  // reports this feature-detected probe as an incompatibility; the alias says the same
  // thing to a reader and nothing to the linter.
  const rt = browser.runtime;
  return {
    // Probed in the BACKGROUND (service worker) context.
    documentId: !!rt && typeof rt.getDocumentId === 'function',
    // Probed where it is actually USED. The background answer is a necessary condition,
    // not a sufficient one: the features that depend on documentId run in content scripts
    // (automation.js stamping snapshot results) and in the ISOLATED-world read around
    // page_eval. Neither eval world can answer for itself here — MAIN is the page realm
    // and USER_SCRIPT gets no extension APIs — which is exactly why this is measured
    // rather than assumed. null until a content script has reported once; UNKNOWN, not
    // false, and an SW eviction resets it to null.
    documentIdInContent: CONTENT_DOC_ID_SUPPORT
  };
}

// --- The hello, and why it is cached -----------------------------------------
// Four of the hello's five inputs are IPC or disk round trips: getFileAccessState()
// (extension.isAllowedFileSchemeAccess), getProxyPermissionState(), getBrowserIdentity()
// (storage.local + getPlatformInfo + the userAgentData high-entropy probe) and
// getConnectionToken() (storage.sync + storage.local). Awaiting all four before the frame
// goes out costs nothing on a healthy browser and is the whole hello budget on one that is
// thrashing: on 2026-09-21 two Firefox reconnects opened their sockets and were dropped for
// a hello the server never received, and this worker has the same shape of problem plus a
// cold start behind it.
//
// So the answers are kept. Once a hello has been built, a reconnect sends from that cache
// synchronously out of onopen and refreshes it afterwards for the next time. The first-ever
// connect has nothing cached and takes the live path, which is also what the watchdog runs
// while connected — that periodic hello exists to re-report a file-access opt-in, so it must
// read the toggle rather than the memory of it. An SW eviction empties the cache, and the
// wake after it is a first connect again.
//
// Anything that can change an input invalidates the cache: a permission granted or revoked,
// either token key changing under us, and the token this extension stores itself after a
// 4401. A stale hello is worse than a slow one.
let helloCache = null;
let helloCacheGen = 0;        // bumped by every invalidation
let helloRefreshGen = -1;     // the generation a refresh is currently running for

async function buildHelloInputs() {
  const fileAccess = await getFileAccessState();
  const proxyAccess = await getProxyPermissionState();
  const identity = await getBrowserIdentity();
  const token = await getConnectionToken();
  return { fileAccess, proxyAccess, identity, token };
}

function helloDataFrom(inputs, encrypted) {
  const identity = inputs.identity || {};
  const data = {
    version: browser.runtime.getManifest().version,
    // Top-level engine discriminator for the server's browser-naming path (also
    // carried inside browserInfo). "blink" vs gecko's "gecko".
    engine: 'blink',
    fileAccess: inputs.fileAccess,
    proxyAccess: inputs.proxyAccess,
    // Read live on every hello: it is synchronous, and a content script may have
    // reported documentId support since the cache was built.
    apiSupport: getApiSupport(),
    instance_id: identity.instance_id,
    platform: identity.platform,
    browserInfo: identity.browserInfo,
    tokenBootstrap: true
  };
  // Only in a plaintext hello, which today means an older server. An encrypted hello
  // needs none: the handshake has already proved the token without sending it.
  if (inputs.token && !encrypted) data.token = inputs.token;
  // What happened to this connection since the last hello went out. Absent when
  // nothing did, which is the ordinary case for a browser that stays connected.
  const journal = journalSinceLastHello();
  if (journal.length) data.journal = journal;
  return data;
}

// The first hello on a socket starts its wire: with a token that is the encrypted
// handshake, whose first frame leaves synchronously, and the hello follows as the first
// sealed record. Later hellos (the watchdog's) are ordinary frames.
function sendHelloFrame(inputs) {
  if (!websocket || websocket.readyState !== WebSocket.OPEN || !wire) return false;
  const w = wire;
  const helloText = (encrypted) => {
    const text = JSON.stringify({ type: 'extension_hello', data: helloDataFrom(inputs, encrypted) });
    journalHelloSent();
    return text;
  };
  if (w.started) return w.send(helloText(w.encrypted));
  handshakeToken = inputs.token || '';
  w.start(inputs.token, helloText).then((r) => {
    if (r && r.encrypted) console.log(`Encrypted connection established (ML-KEM: ${r.kem})`);
  }, (e) => {
    // A 4401 from the server lands in onclose as well, which owns the recovery; this
    // only records what the handshake itself saw.
    console.warn(`Encrypted handshake did not complete: ${e && e.message}`);
  });
  return true;
}

function refreshHelloCache() {
  if (helloRefreshGen === helloCacheGen) return;   // already refreshing this generation
  const gen = helloCacheGen;
  helloRefreshGen = gen;
  buildHelloInputs()
    .then((inputs) => { if (gen === helloCacheGen) helloCache = inputs; })
    .catch((e) => { console.error('Could not refresh the cached hello:', e); })
    .then(() => { if (helloRefreshGen === gen) helloRefreshGen = -1; });
}

function invalidateHelloCache() {
  helloCache = null;
  helloCacheGen++;
  refreshHelloCache();
}

// Announce identity + version + diagnostic state to the server (surfaced on /health).
// Reads everything live, so this is what the watchdog runs while connected and what a
// first connect falls back to.
async function sendExtensionHello() {
  if (!websocket || websocket.readyState !== WebSocket.OPEN) return;
  try {
    const gen = helloCacheGen;
    const inputs = await buildHelloInputs();
    if (gen === helloCacheGen) helloCache = inputs;
    sendHelloFrame(inputs);
  } catch (e) {
    console.error('Failed to send extension_hello:', e);
  }
}

// The (re)connect path: on the wire before the event loop turns, whenever we have
// been connected once already.
function sendExtensionHelloNow() {
  if (helloCache) {
    try {
      sendHelloFrame(helloCache);
    } catch (e) {
      console.error('Failed to send extension_hello:', e);
    }
    refreshHelloCache();   // for the next reconnect, off the critical path
    return;
  }
  sendExtensionHello();
}

// A grant, a revocation or a token edited elsewhere must not ride a cached hello.
// Guarded per site: these are registered at top level, and the JS twins evaluate this
// block against a browser object that carries only the two storage areas. Blink has no
// optional permission to grant today (per-tab proxy cannot exist here), so the two
// permissions listeners are the gecko twin's shape kept live rather than dead weight —
// chrome.permissions is present and the day an optional permission arrives, the hello
// must already be following it. (That day is the browser-wide proxy: the hello carries
// its grant.)
if (browser.permissions && browser.permissions.onAdded) {
  browser.permissions.onAdded.addListener(() => { invalidateHelloCache(); });
}
if (browser.permissions && browser.permissions.onRemoved) {
  browser.permissions.onRemoved.addListener(() => { invalidateHelloCache(); });
}
if (browser.storage && browser.storage.onChanged) {
  browser.storage.onChanged.addListener((changes, area) => {
    if (!changes) return;
    if (area === 'sync' && changes.connectionToken) invalidateHelloCache();
    else if (area === 'local' && changes[AUTO_TOKEN_KEY]) invalidateHelloCache();
    // Options > General writes mcpPort straight to storage; endpointMatchesConfig reads it
    // from CONFIG, which loadConfig filled only once.
    if (area === 'sync' && changes.mcpPort) CONFIG.mcpPort = validPort(changes.mcpPort.newValue);
    else if (area === 'local' && changes.allowBrowserProxy) {
      // The consent switch: the hello carries it, and turning it off ends a live proxy.
      // Sent at once rather than at the next watchdog hello (up to 30 s): the server
      // refuses proxy="tor" before starting Tor unless the last hello reported the grant.
      invalidateHelloCache();
      sendExtensionHello();
      if (changes.allowBrowserProxy.newValue !== true) clearBrowserProxy();
    }
  });
}

function scheduleReconnect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

  if (stoppedByUser) {
    nextRetryAt = null;
    setConnectionPhase('stopped');
    return;
  }
  retryAttempts++;

  if (CONFIG.maxRetries > 0 && retryAttempts > CONFIG.maxRetries) {
    nextRetryAt = null;
    setConnectionPhase('gaveup');
    console.error(`Configured max retry attempts (${CONFIG.maxRetries}) exceeded. Stopping reconnection attempts.`);
    return;
  }

  if (CONFIG.maxRetries >= 0 && retryAttempts > MAX_ABSOLUTE_RETRIES) {
    nextRetryAt = null;
    setConnectionPhase('gaveup');
    console.error(`Absolute max retry attempts (${MAX_ABSOLUTE_RETRIES}) exceeded. Stopping reconnection attempts.`);
    return;
  }

  nextRetryAt = Date.now() + CONFIG.retryInterval;
  setConnectionPhase('waiting');
  console.log(`Scheduling reconnection attempt ${retryAttempts} in ${CONFIG.retryInterval}ms`);
  journalNote('retry', { attempt: retryAttempts, reason: `${CONFIG.retryInterval}ms` });
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectToMCPServer();
  }, CONFIG.retryInterval);
}

function updateConfig(newConfig, persist = true) {
  Object.assign(CONFIG, newConfig);
  console.log('Configuration updated:', CONFIG);
  console.log('New WebSocket URL will be:', `ws://${CONFIG.hostname}:${CONFIG.port}`);

  updateActionIndicator();

  if (persist) {
    browser.storage.sync.set({
      hostname: CONFIG.hostname,
      port: CONFIG.port,
      retryInterval: CONFIG.retryInterval,
      maxRetries: CONFIG.maxRetries,
      pingTimeout: CONFIG.pingTimeout
    });
  }

  if (isConnected || websocket) {
    console.log('Reconnecting with new configuration...');
    disconnect();
    connectToMCPServer();
  }
}

// Load configuration from storage on startup.
// BUILD_CONFIG (baked into config.js by the chromium test harness) wins over storage
// UNCONDITIONALLY when non-null (design D12); the sqlite-stamp analog. Production
// ships BUILD_CONFIG = null and this falls through to the storage/test-override path.
// The MCP HTTP port setting as a usable number, or null (unset, junk, out of range).
function validPort(value) {
  const n = Number(value);
  return Number.isInteger(n) && n >= 1 && n <= 65535 ? n : null;
}

async function loadConfig() {
  // BUILD_CONFIG is a top-level `const` in config.js, shared into this realm via
  // importScripts (the guard is defensive: if config.js failed to load, importScripts
  // would already have thrown and this SW would be inert; the intended fail-closed).
  if (typeof BUILD_CONFIG !== 'undefined' && BUILD_CONFIG && typeof BUILD_CONFIG === 'object') {
    CONFIG.hostname = BUILD_CONFIG.wsHost || 'localhost';
    CONFIG.port = BUILD_CONFIG.wsPort || 8772;
    CONFIG.mcpPort = validPort(BUILD_CONFIG.storageSeed && BUILD_CONFIG.storageSeed.mcpPort);
    console.log(`📋 Config from BUILD_CONFIG (test bake): ws://${CONFIG.hostname}:${CONFIG.port}`);
    // The WS connection above uses BUILD_CONFIG directly, but the popup / options /
    // test-helper handlers read chrome.storage.sync (testPort/testHostname/hostname/
    // port), which BUILD_CONFIG never touches; so on chromium they would report the
    // built-in 8772 default and the UI would look "out of sync". Gecko gets this for
    // free: its harness stamps storage-sync-v2.sqlite directly. MV3 has no such file,
    // so under testMode the SW mirrors the baked seed into storage.sync ONCE here, so
    // the UI genuinely reflects the test config (design D12). Awaited so the seed is
    // in place before the extension connects and the test can query it.
    if (BUILD_CONFIG.testMode && BUILD_CONFIG.storageSeed &&
        typeof BUILD_CONFIG.storageSeed === 'object') {
      try {
        await browser.storage.sync.set(BUILD_CONFIG.storageSeed);
      } catch (e) {
        console.warn('BUILD_CONFIG storage seed failed:', (e && e.message) || e);
      }
    }
    return;
  }

  const result = await browser.storage.sync.get({
    hostname: 'localhost',
    port: 8772,
    retryInterval: 5000,
    maxRetries: -1,
    pingTimeout: 5000,
    // Test configuration overrides (set by test framework)
    testPort: null,
    testHostname: null,
    mcpPort: null       // Options > General: the server's MCP HTTP port (serves /ws-token)
  });

  const isTestEnvironment = result.testPort !== null || result.testHostname !== null;
  const hasValidTestConfig = result.testPort && result.testPort !== 8772;

  if (isTestEnvironment && !hasValidTestConfig) {
    throw new Error('Test environment detected but configuration not ready');
  }

  CONFIG.hostname = result.testHostname || result.hostname;
  CONFIG.port = result.testPort || result.port;
  CONFIG.retryInterval = result.retryInterval;
  CONFIG.maxRetries = result.maxRetries;
  CONFIG.pingTimeout = result.pingTimeout;
  CONFIG.mcpPort = validPort(result.mcpPort);

  const overridden = (result.testPort || result.testHostname) ? ' (test overrides active)' : '';
  console.log(`📋 Config loaded: ws://${CONFIG.hostname}:${CONFIG.port}${overridden}`);
}

function disconnect() {
  stopHealthProbe();
  if (websocket) {
    websocket.close();
    websocket = null;
  }
  wire = null;
  isConnected = false;
}

// --- Server-health probe over the WS ---------------------------------------
async function probeServerHealth() {
  const ws = websocket;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const timeout = Math.max(2000, CONFIG.pingTimeout || 5000);
  try {
    const data = await requestFromServer('server.health', {}, timeout);
    if (websocket !== ws) return;
    serverHealthy = true;
    healthProbeFailures = 0;
    healthOkThisConnection = true;
    lastHealthOkAt = Date.now();
    tokenBootstrapAttempts = 0;
    lastAuthError = null;
    if (data && typeof data.mcp_port === 'number') lastServerMcpPort = data.mcp_port;
    markTokenProven(data && data.mcp_port);
    // Refresh the browser's window/tab counts on the same tick; the event
    // listeners cover the common changes, this covers everything else (and any
    // push lost while the socket was down) without a timer of its own.
    sendBrowserState();
    updateActionIndicator();
  } catch (e) {
    if (websocket !== ws) return;
    healthProbeFailures++;
    serverHealthy = false;
    updateActionIndicator();
    console.warn(`Server health probe failed (${healthProbeFailures}): ${e && e.message}`);
    if (healthOkThisConnection &&
        healthProbeFailures >= HEALTH_PROBE_FAILURES_BEFORE_RECONNECT &&
        ws.readyState === WebSocket.OPEN && !stoppedByUser) {
      console.warn('Health probe failed repeatedly; dropping the socket to force a reconnect');
      try { ws.close(); } catch (_) { /* onclose handles the rest */ }
    }
  }
}

function startHealthProbe() {
  stopHealthProbe();
  probeServerHealth();
  healthProbeTimer = setInterval(probeServerHealth, HEALTH_PROBE_INTERVAL_MS);
}

function stopHealthProbe() {
  if (healthProbeTimer) { clearInterval(healthProbeTimer); healthProbeTimer = null; }
}

// --- Browser state (what this BROWSER holds, vs. what this EXTENSION is) -----
// Window/tab counts, pushed to the server for the options page's connection map:
// the map draws the extension and the browser as two boxes, and this is the
// browser box's content. PUSHED rather than asked for, because the server builds
// that payload synchronously (get_connections_info) and cannot await a round-trip
// to us. Sent on connect, on every health-probe tick (~15s) and, debounced,
// whenever a tab or window comes or goes, so no timer of its own is needed.
//
// MV3: the listeners are registered SYNCHRONOUSLY at top level (a listener added
// later would be missing after an eviction), and each handler is a no-op while
// the socket is down. Tab/window events are user-paced, so waking the worker for
// one costs nothing measurable; and while we are connected it is awake anyway.
const BROWSER_STATE_DEBOUNCE_MS = 800;
let browserStateTimer = null;

// The foreground tab of each window is listed individually (the count alone said
// nothing: with one window it is forever "1 active"). Bounded like every other
// buffer here — a browser with dozens of windows must not push an unbounded list.
const ACTIVE_TAB_LIST_MAX = 12;
const ACTIVE_TAB_TITLE_MAX = 120;   // the tooltip truncates for display; keep some room

// The most recent thing we can honestly say happened in a tab: the last main-frame
// navigation we recorded, or the last time the user focused it, whichever is newer.
// null when neither is known (MV3 loses TAB_NAV on a service-worker eviction).
// `lastAccessed` comes from tabLastAccessedMs, so a browser that does not keep one
// of its own gets the same answer here as `tabs list` gives.
function tabLastEventMs(t) {
  const nav = TAB_NAV.get(t.id) || {};
  const committed = typeof nav.lastCommitted === 'number' ? nav.lastCommitted : 0;
  const accessed = tabLastAccessedMs(t) || 0;
  const ms = Math.max(committed, accessed);
  return ms > 0 ? ms : null;
}

// HH:MM:SS on the BROWSER's own clock: these are the browser's events, so the
// browser is the honest source for when they happened (a remote browser's clock
// is its own, and saying so beats re-stamping them with ours).
function browserStateHms(ms) {
  const d = new Date(ms);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, '0')).join(':');
}

async function collectBrowserState() {
  const [wins, tabs] = await Promise.all([
    browser.windows.getAll({ populate: false }),
    browser.tabs.query({})
  ]);
  let active = 0;
  let loaded = 0;
  const activeList = [];
  for (const t of tabs) {
    if (t.active) {
      active++;
      if (activeList.length < ACTIVE_TAB_LIST_MAX) {
        const at = tabLastEventMs(t);
        activeList.push({
          id: t.id,
          title: String(t.title || t.url || '').slice(0, ACTIVE_TAB_TITLE_MAX),
          last_hms: at === null ? null : browserStateHms(at),
        });
      }
    }
    // `discarded` is the unloaded-but-still-listed state; a tab that never loaded
    // reports it too, so "loaded" is the honest complement of "in memory".
    if (!t.discarded) loaded++;
  }
  return {
    windows: wins.length,
    tabs: tabs.length,
    active_tabs: active,
    loaded_tabs: loaded,
    active_tab_list: activeList
  };
}

async function sendBrowserState() {
  const ws = websocket;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  try {
    const data = await collectBrowserState();
    // The counting awaited: the socket may have been replaced meanwhile, and a
    // stale socket's send would throw.
    if (websocket !== ws || ws.readyState !== WebSocket.OPEN) return;
    wireSend({ type: 'browser_state', data });
  } catch (e) {
    console.error('Failed to send browser_state:', e);
  }
}

// Coalesce bursts (closing a window fires one onRemoved per tab plus the window's own).
function scheduleBrowserState() {
  if (browserStateTimer) clearTimeout(browserStateTimer);
  browserStateTimer = setTimeout(() => {
    browserStateTimer = null;
    sendBrowserState();
  }, BROWSER_STATE_DEBOUNCE_MS);
}

try {
  browser.tabs.onCreated.addListener(scheduleBrowserState);
  browser.tabs.onRemoved.addListener(scheduleBrowserState);
  browser.tabs.onDetached.addListener(scheduleBrowserState);
  browser.tabs.onAttached.addListener(scheduleBrowserState);
  browser.windows.onCreated.addListener(scheduleBrowserState);
  browser.windows.onRemoved.addListener(scheduleBrowserState);
} catch (e) {
  console.error('Could not watch tab/window counts:', e);
}

// --- Requests this extension sends TO the server, awaiting a response --------
let pendingServerRequests = {};
let serverRequestSeq = 0;

function requestFromServer(action, data = {}, timeoutMs = 8000) {
  return new Promise((resolve, reject) => {
    if (!websocket || websocket.readyState !== WebSocket.OPEN) {
      reject(new Error('Not connected to server'));
      return;
    }
    const id = `ext_${Date.now()}_${serverRequestSeq++}`;
    const timer = setTimeout(() => {
      delete pendingServerRequests[id];
      reject(new Error('Request to server timed out'));
    }, timeoutMs);
    pendingServerRequests[id] = { resolve, reject, timer };
    try {
      if (!wireSend({ id, type: 'request', action, data })) throw new Error('Not connected to server');
    } catch (e) {
      clearTimeout(timer);
      delete pendingServerRequests[id];
      reject(e);
    }
  });
}

async function handleMessage(message) {
  const { id, type, action, data } = message;

  if ((type === 'response' || type === 'error') && id && pendingServerRequests[id]) {
    const pending = pendingServerRequests[id];
    clearTimeout(pending.timer);
    delete pendingServerRequests[id];
    if (type === 'error') {
      pending.reject(new Error((data && data.message) || 'Server returned an error'));
    } else {
      pending.resolve(data);
    }
    return;
  }

  if (type !== 'request') return;

  switch (action.split('.')[0]) {
    case 'tabs':
      await handleTabsAction(id, action, data);
      break;
    case 'content':
      await handleContentAction(id, action, data);
      break;
    case 'navigation':
      await handleNavigationAction(id, action, data);
      break;
    case 'windows':
      await handleWindowsAction(id, action, data);
      break;
    case 'requests':
      await handleRequestsAction(id, action, data);
      break;
    case 'console':
      await handleConsoleAction(id, action, data);
      break;
    case 'test':
      await handleTestAction(id, action, data);
      break;
    default:
      sendError(id, 'UNKNOWN_ACTION', `Unknown action: ${action}`);
  }
}

function sendResponse(id, action, data) {
  if (!isConnected) return;

  const message = {
    id,
    type: 'response',
    action,
    data,
    timestamp: new Date().toISOString()
  };

  wireSend(message);
}

function sendError(id, code, message, details = {}) {
  if (!isConnected) return;

  const errorMessage = {
    id,
    type: 'error',
    action: '',
    data: {
      code,
      message,
      details
    },
    timestamp: new Date().toISOString()
  };

  wireSend(errorMessage);
}

function sendDebugLog(message, level = 'log') {
  if (!websocket || websocket.readyState !== WebSocket.OPEN) return;

  const debugMessage = {
    id: `debug_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
    type: 'debug_log',
    action: 'debug',
    data: {
      level: level,
      message: message,
      timestamp: new Date().toISOString()
    }
  };

  wireSend(debugMessage);
}

// --- Claude usage (account-wide session/weekly limits from claude.ai) --------
// The store lives in vendor/anthropic-data.js (loaded via importScripts). We
// instantiate it with this extension's storage, keep the refresh setting in sync,
// and expose it to the popup via getClaudeUsage. (Fetch rides the <all_urls>
// claude.ai cookies, no new perm.)
const claudeUsageStore = AnthropicData.createUsageStore({ local: browser.storage.local });
// The same, for OpenAI (vendor/openai-data.js). Its `local` holds only the weekly samples
// behind the Usage tab's weekly chart; there is no 5-hour chart, so no session history. Two calls per
// refresh, not one — the usage endpoint wants a bearer that only the session endpoint
// can mint — and both of them happen in here, so the popup still asks once.
const openaiUsageStore = OpenAIData.createUsageStore({ local: browser.storage.local });
// The same, for GitHub Copilot (vendor/copilot-data.js): the github.com login and the
// entitlement, two calls on this browser's cookies. The login is what lets the popup
// notice when this browser and VS Code are signed in to different GitHub accounts.
const copilotUsageStore = CopilotData.createUsageStore({ local: browser.storage.local });
// Kilo provider/model NAME resolver (vendor/kilocode-modelnames-data.js), storage.local cache.
const kiloNameResolver = KiloModelNames.createResolver({ local: browser.storage.local });
// ---- provider usage settings + the always-on sampler ----------------------
// One settings read feeds two things. The stores get their cache TTL, which is what a
// popup-driven read honours. The sampler (vendor/usage-sampler.js) gets a clock for each
// provider whose "Query every N min" box is ticked, so the Usage tab's reading and the
// history behind it keep moving with no popup open. Built at top level: an MV3 worker
// keeps only the listeners added on its first synchronous evaluation.
// The display-mode defaults MUST match PROVIDER_MODE_DEFAULTS in popup/popup.js, or a
// clock could run for a provider the popup shows nowhere, or the reverse.
const usageSampler = UsageSampler.createSampler({
  alarms: browser.alarms,
  providers: [
    { key: 'anthropic', sample: () => claudeUsageStore.get(true) },
    { key: 'openai', sample: () => openaiUsageStore.get(true) },
    { key: 'copilot', sample: () => copilotUsageStore.get(true) },
  ],
  onError: (e) => console.warn('Usage sample failed:', (e && e.message) || e),
});
const USAGE_SETTINGS_DEFAULTS = {
  anthropicUsageRefreshMinutes: 5, openaiUsageRefreshMinutes: 5, copilotUsageRefreshMinutes: 5,
  anthropicDisplayMode: 'client', openaiDisplayMode: 'disabled', copilotDisplayMode: 'client',
  anthropicBackgroundRefresh: false, openaiBackgroundRefresh: false, copilotBackgroundRefresh: false,
};
async function applyUsageSettings() {
  // A profile saved by 1.9.1 or older may still hold the retired 'always' mode. Rewrite it
  // before the read below, so that read already sees the new values.
  try {
    const legacy = UsageSampler.legacyModePatch(
      await browser.storage.sync.get(UsageSampler.LEGACY_MODE_KEYS));
    if (legacy) await browser.storage.sync.set(legacy);
  } catch (e) { /* no storage: the defaults below apply */ }
  let s = USAGE_SETTINGS_DEFAULTS;
  try { s = await browser.storage.sync.get(USAGE_SETTINGS_DEFAULTS); } catch (e) { /* keep defaults */ }
  claudeUsageStore.setRefreshMinutes(s.anthropicUsageRefreshMinutes);
  openaiUsageStore.setRefreshMinutes(s.openaiUsageRefreshMinutes);
  copilotUsageStore.setRefreshMinutes(s.copilotUsageRefreshMinutes);
  await usageSampler.apply({
    anthropic: { mode: s.anthropicDisplayMode, backgroundRefresh: s.anthropicBackgroundRefresh,
                 refreshMinutes: s.anthropicUsageRefreshMinutes },
    openai: { mode: s.openaiDisplayMode, backgroundRefresh: s.openaiBackgroundRefresh,
              refreshMinutes: s.openaiUsageRefreshMinutes },
    copilot: { mode: s.copilotDisplayMode, backgroundRefresh: s.copilotBackgroundRefresh,
               refreshMinutes: s.copilotUsageRefreshMinutes },
  });
}
applyUsageSettings();
// deepseekDisplayMode drives no clock. It is watched so that an 'always' synced in from
// an older build on another device still gets migrated.
browser.storage.onChanged.addListener((changes, area) => {
  if (area !== 'sync') return;
  if (changes.anthropicUsageRefreshMinutes || changes.openaiUsageRefreshMinutes
      || changes.copilotUsageRefreshMinutes
      || changes.anthropicDisplayMode || changes.openaiDisplayMode || changes.copilotDisplayMode
      || changes.anthropicBackgroundRefresh || changes.openaiBackgroundRefresh
      || changes.copilotBackgroundRefresh
      || changes.deepseekDisplayMode) applyUsageSettings();
});
// An update from a build whose DeepSeek default was 'client' keeps the clock it showed
// (UsageSampler.legacyDefaultsPatch). Top level, so an MV3 worker keeps the listener.
if (browser.runtime && browser.runtime.onInstalled) {
  browser.runtime.onInstalled.addListener((details) => {
    if (!details || details.reason !== 'update') return;
    browser.storage.sync.get(['deepseekDisplayMode'])
      .then((stored) => {
        const patch = UsageSampler.legacyDefaultsPatch(details.previousVersion, stored);
        return patch ? browser.storage.sync.set(patch) : null;
      })
      .catch((e) => console.warn('DeepSeek default migration failed:', (e && e.message) || e));
  });
}
// ---- end provider usage settings -------------------------------------------

// Handle popup requests for connection status (+ content-script relays: console
// entries, and the D5a body-capture messages get_monitor_state / net_body).
browser.runtime.onMessage.addListener((request, sender, sendResponse) => {

  if (request.action === 'adoptServerToken') {
    adoptServerToken().then(sendResponse);
    return true;
  }

  if (request.action === 'getConnectionStatus') {
    sendResponse({
      connected: isConnected,
      serverHealthy: serverHealthy,
      lastHealthOkAt: lastHealthOkAt,
      serverMcpPort: lastServerMcpPort,
      phase: connectionPhase,
      retryAttempts: retryAttempts,
      maxRetries: CONFIG.maxRetries,
      retryInterval: CONFIG.retryInterval,
      nextRetryAt: nextRetryAt,
      stopped: stoppedByUser,
      authError: lastAuthError,
      ...tokenTrustStatus(),
      // Whether this socket carries only sealed records; null while unknown (no socket,
      // or the handshake still running) so the popup never flashes a warning. The popup
      // says so when a live connection is not encrypted (an AUTH_OFF server).
      encrypted: !wire ? null : (wire.encrypted ? true : (wire.open ? false : null)),
      // The last few connection events, same shape as the hello's journal. Nothing
      // renders it yet; it is here so the popup can, without a new message type.
      journal: journalRecent(10),
      config: CONFIG
    });
    return true;
  }

  if (request.type === 'configUpdated') {
    updateConfig(request.config);
    sendResponse({ success: true });
    return true;
  }

  if (request.type === 'advancedConfigUpdated') {
    updateConfig(request.config);
    sendResponse({ success: true });
    return true;
  }

  if (request.type === 'resetToDefaults') {
    updateConfig({
      hostname: 'localhost',
      port: 8772,
      retryInterval: 5000,
      maxRetries: -1,
      pingTimeout: 5000
    }, false);
    try { claudeUsageStore.clearHistory(); } catch (e) {}
    try { openaiUsageStore.clearHistory(); } catch (e) {}
    try { copilotUsageStore.clearHistory(); } catch (e) {}
    try { kiloNameResolver.clear(); } catch (e) {}
    sendResponse({ success: true });
    return true;
  }

  if (request.type === 'getConnectionStatus') {
    sendResponse({
      connected: isConnected,
      serverHealthy: serverHealthy,
      serverMcpPort: lastServerMcpPort,
      phase: connectionPhase,
      authError: lastAuthError,
      ...tokenTrustStatus()
    });
    return true;
  }

  if (request.action === 'updateConfig') {
    updateConfig(request.config);
    sendResponse({ success: true, config: CONFIG });
    return true;
  }

  if (request.action === 'forceReconnect') {
    stoppedByUser = false;
    journalNote('reconnect', { reason: 'user' });
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (websocket) {
      websocket.close();
    }
    retryAttempts = 0;
    connectToMCPServer();
    sendResponse({ success: true });
    return true;
  }

  if (request.action === 'stopReconnect') {
    stoppedByUser = true;
    journalNote('stopped');
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (websocket && websocket.readyState !== WebSocket.CLOSED) {
      try { websocket.close(); } catch (e) { /* ignore */ }
    }
    isConnected = false;
    nextRetryAt = null;
    setConnectionPhase('stopped');
    sendResponse({ success: true });
    return true;
  }

  if (request.action === 'getServerConnections') {
    browser.storage.sync.get({ kiloLoadDetails: true })
      // server_host: the address WE dialled. The server cannot know it (it sees our
      // peer address, not its own destination), and without it the server row tells a
      // phone the server lives on localhost, which is the one place it cannot reach.
      .then(({ kiloLoadDetails }) => requestFromServer('server.get_connections',
        { kilo_details: !!kiloLoadDetails, server_host: CONFIG.hostname }))
      .then(data => sendResponse({ success: true, data }))
      .catch(err => sendResponse({ success: false, error: err.message }));
    return true; // async response
  }

  // Default-browser setting (Options ▸ General). It lives in the SERVER's config file,
  // so both halves are relayed over the WebSocket; that channel is already
  // authenticated and works from a browser on another machine, which the settings
  // endpoints on the MCP HTTP port do not.
  if (request.action === 'getDefaultBrowser') {
    requestFromServer('server.get_default_browser', {}, 8000)
      .then(data => sendResponse({ success: true, data }))
      .catch(err => sendResponse({ success: false, error: (err && err.message) || String(err) }));
    return true; // async response
  }

  if (request.action === 'setDefaultBrowser') {
    requestFromServer('server.set_default_browser', request.settings || {}, 8000)
      .then(data => sendResponse({ success: true, data }))
      .catch(err => sendResponse({ success: false, error: (err && err.message) || String(err) }));
    return true; // async response
  }

  if (request.action === 'restartServer') {
    requestFromServer('server.restart', {}, 8000)
      .then(data => sendResponse({ success: true, data }))
      .catch(err => sendResponse({ success: false, error: (err && err.message) || String(err) }));
    return true; // async response
  }

  if (request.action === 'getClaudeUsage') {
    claudeUsageStore.get(request.force)
      .then(data => sendResponse({ success: true, data }))
      .catch(err => sendResponse({ success: false, error: err.message }));
    return true; // async response
  }

  if (request.action === 'getOpenAIUsage') {
    openaiUsageStore.get(request.force)
      .then(data => sendResponse({ success: true, data }))
      .catch(err => sendResponse({ success: false, error: err.message }));
    return true; // async response
  }

  if (request.action === 'getCopilotUsage') {
    copilotUsageStore.get(request.force)
      .then(data => sendResponse({ success: true, data }))
      .catch(err => sendResponse({ success: false, error: err.message }));
    return true; // async response
  }

  if (request.action === 'resolveKiloNames') {
    kiloNameResolver.resolve(request.pairs || [])
      .then(data => sendResponse({ success: true, data }))
      .catch(err => sendResponse({ success: false, error: err.message }));
    return true; // async response
  }

  // Console entries relayed by the content script's capture layer
  if (request.type === 'console_entries' && sender && sender.tab && sender.tab.id != null) {
    takeConsoleEntries(sender.tab.id, request.entries);
    return false;
  }
  // END console-entries sink (observability twin slices to this anchor)

  // D5a: content.js pulls the armed body-capture state at document_start so a page
  // loaded mid-session arms its MAIN-world wrapper. Answer with the current state.
  if (request.type === 'get_monitor_state') {
    sendResponse({ armed: bodyCaptureArmed(), urlPatterns: bodyCaptureUrlPatterns() });
    return true;
  }

  // D5a: a captured fetch/XHR response body relayed up from the MAIN-world wrapper.
  // Correlate it to a webRequest log record and store it shape-identically to the
  // gecko StreamFilter result so requests.get_content is unchanged.
  if (request.type === 'net_body' && sender && sender.tab && sender.tab.id != null) {
    try { storeNetBody(sender.tab.id, request.entry); } catch (e) { /* best-effort */ }
    return false;
  }
});


// --- Per-tab observability: rolling request log, console buffer, nav state ---
// Always-on, bounded. In SW MEMORY: an SW eviction empties them (documented parity
// gap; the WS heartbeat keeps the SW warm during an active session; a fresh SW
// re-attaches these listeners and refills). Powers requests.recent, settle
// detection, nav digests.
const TAB_REQUESTS = new Map(); // tabId -> [{id,url,method,type,start,end,status,error,fromCache}]
const TAB_NET = new Map();      // tabId -> {inflight, lastActivity}
const TAB_NAV = new Map();      // tabId -> {lastCommitted,lastAccessObserved,...}
const TAB_CONSOLE = new Map();  // tabId -> [{level,text,ts,url}]
const REQ_LOG_MAX = 300;
const CONSOLE_MAX = 600;
const STATIC_TYPES = new Set(['stylesheet', 'script', 'image', 'imageset', 'font', 'media', 'object', 'speculative']);
// Request types that cannot put content into the page: a hyperlink ping or sendBeacon
// (Chrome files both as "ping") and a CSP violation report. They stay in the request log
// but not on the network-quiet clock, so an analytics beacon cannot hold a navigation
// open. Chrome's webRequest.ResourceType has no "beacon" or "speculative"; gecko's set
// lists those too.
const QUIET_IGNORED_TYPES = new Set(['ping', 'csp_report']);

// Map Chromium net::ERR_* network error codes to a short plain-English cause, so a
// failed navigation reads as "connection refused", not just an opaque code. This is
// the chromium twin of gecko's NSRESULT/NS_ERROR table: the VOCABULARY differs by
// engine (Blink reports net::ERR_* strings from BOTH webRequest and webNavigation;
// no decimal-nsresult form to decode), but the humanized OUTPUT FORMAT ("phrase
// [CODE]") is shared. Anything unmapped falls through unchanged.
const NET_ERROR_HUMAN = {
  'net::ERR_CONNECTION_REFUSED': 'connection refused — nothing is listening on that address/port',
  'net::ERR_PROXY_CONNECTION_FAILED': 'proxy connection failed — the proxy is not accepting connections (is it running?)',
  'net::ERR_SOCKS_CONNECTION_FAILED': 'SOCKS proxy connection failed — the SOCKS proxy is not accepting connections',
  'net::ERR_TIMED_OUT': 'network timeout — the connection was accepted but no response arrived (or the host is unreachable)',
  'net::ERR_CONNECTION_TIMED_OUT': 'connection timed out — no response from the host',
  'net::ERR_NAME_NOT_RESOLVED': 'unknown host — DNS could not resolve the domain',
  'net::ERR_NAME_RESOLUTION_FAILED': 'DNS resolution failed for the host',
  'net::ERR_CONNECTION_RESET': 'connection reset by the server',
  'net::ERR_CONNECTION_CLOSED': 'connection closed before the response completed',
  'net::ERR_CONNECTION_ABORTED': 'connection aborted before the response completed',
  'net::ERR_ABORTED': 'request aborted — usually a newer navigation replaced it, or the tab was stopped/closed',
  'net::ERR_INTERNET_DISCONNECTED': 'browser is offline',
  'net::ERR_ADDRESS_UNREACHABLE': 'address unreachable — no route to the host',
  'net::ERR_TOO_MANY_REDIRECTS': 'redirect loop',
  'net::ERR_UNSAFE_PORT': 'port blocked — the browser refuses to connect to this restricted port',
  'net::ERR_EMPTY_RESPONSE': 'the server closed the connection without sending any data',
  'net::ERR_INVALID_URL': 'request blocked — the URL is invalid',
  'net::ERR_UNKNOWN_URL_SCHEME': 'request blocked — the URL scheme is not handled',
  'net::ERR_BLOCKED_BY_CLIENT': 'blocked by an extension (e.g. an ad/content blocker)',
  'net::ERR_BLOCKED_BY_ADMINISTRATOR': 'blocked by administrator policy',
  'net::ERR_SSL_PROTOCOL_ERROR': 'TLS/SSL protocol error',
  'net::ERR_CERT_AUTHORITY_INVALID': 'the server certificate is not trusted (unknown authority)',
  'net::ERR_CERT_COMMON_NAME_INVALID': 'the server certificate does not match the host',
  'net::ERR_CERT_DATE_INVALID': 'the server certificate is expired or not yet valid',
};
function humanizeNetError(code) {
  if (!code) return null;
  const name = String(code);
  const h = NET_ERROR_HUMAN[name];
  return h ? (h + ' [' + name + ']') : name;
}

// Ring-buffer request tracking (always-on, non-blocking). No requestBody here; the
// ring buffer needs only metadata; the monitor-capture listeners below carry the
// heavy extraInfoSpec.
browser.webRequest.onBeforeRequest.addListener(d => {
  if (d.tabId < 0) return;
  if (!QUIET_IGNORED_TYPES.has(d.type)) {
    const net = TAB_NET.get(d.tabId) || { inflight: 0, lastActivity: 0 };
    net.inflight++;
    net.lastActivity = Date.now();
    TAB_NET.set(d.tabId, net);
  }
  let log = TAB_REQUESTS.get(d.tabId);
  if (!log) { log = []; TAB_REQUESTS.set(d.tabId, log); }
  log.push({ id: d.requestId, url: d.url, method: d.method, type: d.type, start: d.timeStamp });
  if (log.length > REQ_LOG_MAX) log.splice(0, log.length - REQ_LOG_MAX);
}, { urls: ['<all_urls>'] });

function finishTrackedRequest(d, status, error) {
  if (d.tabId < 0) return;
  const net = QUIET_IGNORED_TYPES.has(d.type) ? null : TAB_NET.get(d.tabId);
  if (net) { net.inflight = Math.max(0, net.inflight - 1); net.lastActivity = Date.now(); }
  const log = TAB_REQUESTS.get(d.tabId);
  if (log) {
    for (let i = log.length - 1; i >= 0; i--) {
      if (log[i].id === d.requestId) {
        log[i].status = status;
        log[i].end = d.timeStamp;
        if (error) log[i].error = error;
        if (d.ip) log[i].ip = d.ip;
        if (d.fromCache) log[i].fromCache = true;
        break;
      }
    }
  }
  // A request that ends moves the network-quiet deadline a settle may be sleeping on.
  wakeSettle(d.tabId);
}
browser.webRequest.onCompleted.addListener(d => finishTrackedRequest(d, d.statusCode), { urls: ['<all_urls>'] });
browser.webRequest.onErrorOccurred.addListener(d => finishTrackedRequest(d, null, d.error), { urls: ['<all_urls>'] });

// Stamp the dialed peer address as soon as the browser knows it (first response byte).
browser.webRequest.onResponseStarted.addListener(d => {
  if (d.tabId < 0 || !d.ip) return;
  const log = TAB_REQUESTS.get(d.tabId);
  if (!log) return;
  for (let i = log.length - 1; i >= 0; i--) {
    if (log[i].id === d.requestId) { log[i].ip = d.ip; break; }
  }
}, { urls: ['<all_urls>'] });

// Record redirect hops on the originating request. Chromium keeps ONE requestId across
// redirects (unlike FF152 channel-replacement), so onCompleted still fires on the same
// id; we do NOT mark the hop finished here (that would end the entry prematurely); we
// only record the hop + follow the url.
browser.webRequest.onBeforeRedirect.addListener(d => {
  if (d.tabId < 0) return;
  const log = TAB_REQUESTS.get(d.tabId);
  if (!log) return;
  for (let i = log.length - 1; i >= 0; i--) {
    if (log[i].id === d.requestId) {
      (log[i].redirects = log[i].redirects || []).push({ from: log[i].url, to: d.redirectUrl, status: d.statusCode });
      if (d.ip) log[i].ip = d.ip;
      log[i].url = d.redirectUrl; // follow, so a later failure is attributed to the final hop
      break;
    }
  }
}, { urls: ['<all_urls>'] });

function markNavCommitted(d) {
  if (d.frameId !== 0) return;
  const nav = TAB_NAV.get(d.tabId) || {};
  nav.lastCommitted = d.timeStamp || Date.now();
  nav.lastCommittedUrl = d.url;
  nav.lastNavError = null;
  nav.lastNavErrorAt = null;
  TAB_NAV.set(d.tabId, nav);
  noteNavEvent(d, 'commit');
  wakeSettle(d.tabId);
}
browser.webNavigation.onCommitted.addListener(markNavCommitted);
browser.webNavigation.onHistoryStateUpdated.addListener(markNavCommitted);
browser.webNavigation.onReferenceFragmentUpdated.addListener(markNavCommitted);

// A navigation that fails to COMMIT fires this, not onCommitted; record WHY.
browser.webNavigation.onErrorOccurred.addListener(d => {
  if (d.frameId !== 0) return;
  const nav = TAB_NAV.get(d.tabId) || {};
  nav.lastNavError = d.error;
  nav.lastNavErrorUrl = d.url;
  nav.lastNavErrorAt = d.timeStamp || Date.now();
  TAB_NAV.set(d.tabId, nav);
  noteNavEvent(d, 'error');
});

// A main-frame document that finished loading. abortOfCompletedLoad reads these; the echo
// it sets aside is a gecko one, and the record costs Chrome nothing.
browser.webNavigation.onCompleted.addListener(d => {
  if (d.frameId !== 0) return;
  noteNavEvent(d, 'done');
  wakeSettle(d.tabId);
});

// The tab's recent main-frame webNavigation events, oldest first, in arrival order:
// {ev: commit|error|done, url, at, error}.
const NAV_EVENTS_MAX = 12;
function noteNavEvent(d, ev) {
  const nav = TAB_NAV.get(d.tabId) || {};
  const log = nav.events || (nav.events = []);
  const e = { ev, url: d.url, at: d.timeStamp || Date.now() };
  if (d.error) e.error = d.error;
  log.push(e);
  if (log.length > NAV_EVENTS_MAX) log.splice(0, log.length - NAV_EVENTS_MAX);
  TAB_NAV.set(d.tabId, nav);
}

// --- lastAccessed, observed, for a browser that does not keep it ------------------
// The live case for this is Firefox for Android, where `tabs.Tab.lastAccessed` is
// undefined for every tab: ext-android.js reads `lastTouchedAt`, a Fennec property
// the Fenix rewrite dropped (Bugzilla 2032927). The gecko tree's copy of this block
// carries the detail. On Chromium the fallback should never fire. The property landed
// in Chrome 121 and this manifest already demands 138, so the browser always keeps a
// value of its own; the block is here so the two trees answer the same way if that
// ever stops being true, and so both twins exercise the same logic.
//
// Nothing can recover when a tab was last touched before we were watching, so we
// record it from here on. The moments below are the ones desktop Firefox stamps
// lastAccessed on itself (tab creation, and a selection change; session restore is
// the third and no extension API can see it), plus the main-frame navigation
// TAB_NAV already tracks. A tab that has done none of them since this background
// page started stays unknown, which is the honest answer: nothing anywhere wrote
// down when it was last used.
function noteTabAccess(tabId, ms) {
  if (typeof tabId !== 'number') return;
  const nav = TAB_NAV.get(tabId) || {};
  nav.lastAccessObserved = ms || Date.now();
  TAB_NAV.set(tabId, nav);
}
browser.tabs.onActivated.addListener(info => noteTabAccess(info && info.tabId));
browser.tabs.onCreated.addListener(tab => noteTabAccess(tab && tab.id));

// The browser's own number when it keeps one, ours when it does not. Deliberately
// NOT the max of the two: where `lastAccessed` is real it means "last SELECTED",
// so folding a background tab's navigation into it would reorder `tabs list` on a
// browser whose value was already right.
function tabLastAccessedMs(t) {
  if (typeof t.lastAccessed === 'number' && t.lastAccessed > 0) return t.lastAccessed;
  const nav = TAB_NAV.get(t.id) || {};
  const observed = Math.max(nav.lastAccessObserved || 0, nav.lastCommitted || 0);
  return observed > 0 ? observed : undefined;
}

browser.tabs.onRemoved.addListener(tabId => {
  TAB_REQUESTS.delete(tabId);
  TAB_NET.delete(tabId);
  TAB_NAV.delete(tabId);
  TAB_CONSOLE.delete(tabId);
  forgetSnapshotDocs(tabId);   // else the map grows for the life of the service worker
  wakeSettle(tabId);           // a settle sleeping on this tab finds it closed at once
});

// A status or URL change is what a sleeping waitForSettle is waiting for. Registered at
// top level like every listener here, so a worker woken by the event still hears it.
browser.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo && (changeInfo.status || changeInfo.url)) wakeSettle(tabId);
});

// --- Most-recently-focused browser window tracking -----------------------------
let LAST_FOCUSED_WINDOW_ID = null;

function reseedLastFocusedWindow() {
  browser.windows.getLastFocused().then(w => {
    if (w && typeof w.id === 'number') LAST_FOCUSED_WINDOW_ID = w.id;
  }).catch(() => {});
}
reseedLastFocusedWindow();

browser.windows.onFocusChanged.addListener(windowId => {
  if (windowId !== browser.windows.WINDOW_ID_NONE) LAST_FOCUSED_WINDOW_ID = windowId;
});

browser.windows.onRemoved.addListener(windowId => {
  if (windowId === LAST_FOCUSED_WINDOW_ID) {
    LAST_FOCUSED_WINDOW_ID = null;
    reseedLastFocusedWindow();
  }
});

// --- Browser-wide SOCKS proxy (Chromium) ------------------------------------------
// chrome.proxy is a browser-GLOBAL setting with no per-request hook, and a PAC script
// gets no tab identity, so the per-tab routing gecko does cannot exist here. What does
// exist is switching the WHOLE browser to a SOCKS proxy, and that is what a non-direct
// `proxy` on tabs.create / navigation.navigate / tabs.routing does on Chromium, behind
// a consent switch the user turns on in the popup (PROXY_CONSENT_KEY). Every tab then goes
// through it, the ones the user is looking at included, so the server tells the agent
// so on each switch and how to undo it (server/tools/browser_proxy.py). Kept:
//   - fail-closed: a SOCKS fixed_servers proxy has no direct fallback, so an unreachable
//     one errors the request; and if another extension or a policy owns the setting the
//     call is refused (PROXY_NOT_CONTROLLABLE) instead of loading direct;
//   - undoable: "" or "direct" clears it, and it lives only as long as the server
//     connection that asked for it (clearBrowserProxy on socket close, on startup and in
//     the watchdog), so a dead Tor cannot leave the whole browser without a network;
//   - the control socket and the managed Tor stay reachable: loopback and the server's
//     own host bypass the proxy.
const BROWSER_PROXY_KEY = 'browserProxy';
// Chrome does not allow `proxy` as an OPTIONAL permission (it is dropped with a manifest
// warning), so the manifest requires it and the user's consent is this switch instead: off
// until they turn it on in the popup, and only extension pages can write it (no wire action
// does). While it is off every proxy request is refused, so nothing changes by installing.
const PROXY_CONSENT_KEY = 'allowBrowserProxy';
const PROXY_PERM_ERROR =
  'Switching the browser to a proxy is turned off in BrowserPlug, because it changes every ' +
  'tab. The user can turn it on in the BrowserPlug popup (toolbar button, Permissions, ' +
  'SOCKS proxy, Allow), then retry.';

// "Has permission" in the hello means: the API is there AND the user switched it on. The
// test harness bakes the consent into config.js (BUILD_CONFIG), as it does the port.
async function hasProxyPermission() {
  try {
    if (!(browser.proxy && browser.proxy.settings)) return false;
    if (typeof BUILD_CONFIG !== 'undefined' && BUILD_CONFIG && BUILD_CONFIG.allowBrowserProxy === true) {
      return true;
    }
    const stored = await browser.storage.local.get(PROXY_CONSENT_KEY);
    return stored[PROXY_CONSENT_KEY] === true;
  } catch (e) { return false; }
}

// Same grammar as the gecko twin: SOCKS5 and SOCKS4 as host:port, nothing else.
function parseProxyEndpoint(raw) {
  if (raw == null || raw === '' || raw === 'direct') return { ok: true, direct: true };
  const m = /^(socks5h?|socks4a?|socks):\/\/([^\s/:]+):(\d{1,5})\/?$/i.exec(String(raw).trim());
  if (!m) return { ok: false, error: `Invalid proxy endpoint "${raw}". Expected e.g. "socks5://127.0.0.1:9050" (schemes: socks5, socks5h, socks4, socks).` };
  const scheme = m[1].toLowerCase();
  const host = m[2];
  const port = parseInt(m[3], 10);
  if (!(port >= 1 && port <= 65535)) return { ok: false, error: `Invalid proxy port in "${raw}".` };
  // Chrome resolves names at a SOCKS5 proxy; the socks4 scheme here is plain SOCKS4.
  return { ok: true, direct: false, info: {
    scheme: (scheme === 'socks4' || scheme === 'socks4a') ? 'socks4' : 'socks5',
    host, port, endpoint: `${scheme}://${host}:${port}` } };
}

async function readBrowserProxy() {
  try {
    const stored = await browser.storage.local.get(BROWSER_PROXY_KEY);
    return stored[BROWSER_PROXY_KEY] || null;
  } catch (e) { return null; }
}

async function applyBrowserProxy(info) {
  const bypass = ['localhost', '127.0.0.1', '[::1]'];
  const serverHost = CONFIG && CONFIG.hostname;
  if (serverHost && !bypass.includes(serverHost)) bypass.push(serverHost);
  await browser.proxy.settings.set({ scope: 'regular', value: {
    mode: 'fixed_servers',
    rules: { singleProxy: { scheme: info.scheme, host: info.host, port: info.port },
             bypassList: bypass } } });
  // set() resolves even when something outranks us; only levelOfControl says who wins.
  const got = await browser.proxy.settings.get({});
  if (got.levelOfControl !== 'controlled_by_this_extension') {
    await browser.proxy.settings.clear({ scope: 'regular' });
    throw new Error(`the browser's proxy setting is controlled by ${got.levelOfControl}`);
  }
  await browser.storage.local.set({ [BROWSER_PROXY_KEY]: { endpoint: info.endpoint, setAt: Date.now() } });
}

// Whether OUR proxy is the one in force, asked of the browser rather than read from what we
// stored: another extension or a policy can take the setting over after we made it.
async function browserProxyIsInForce() {
  if (!(await readBrowserProxy())) return false;
  try {
    const got = await browser.proxy.settings.get({});
    return got.levelOfControl === 'controlled_by_this_extension' &&
           !!got.value && got.value.mode === 'fixed_servers';
  } catch (e) { return false; }
}

// Returns what was set, or null when there was nothing of ours to clear.
async function clearBrowserProxy() {
  const had = await readBrowserProxy();
  try {
    if (browser.proxy && browser.proxy.settings) await browser.proxy.settings.clear({ scope: 'regular' });
  } catch (e) { console.warn('Could not clear the browser-wide proxy:', e); }
  try { await browser.storage.local.remove(BROWSER_PROXY_KEY); } catch (e) { /* best effort */ }
  return had;
}

// The `proxy` field of a tabs.create / navigation.navigate / tabs.routing call.
//   { error: true }                   the error was already sent; the caller must stop
//   { error: false, change: null }    the call carried no proxy
//   { error: false, change: {...} }   what happened: { off, endpoint, was, scope: 'browser' }
async function applyProxyField(id, raw) {
  if (raw === undefined || raw === null) return { error: false, change: null };
  const parsed = parseProxyEndpoint(raw);
  if (!parsed.ok) { sendError(id, 'INVALID_PROXY', parsed.error); return { error: true }; }
  if (parsed.direct) {
    const was = await clearBrowserProxy();
    return { error: false, change: { off: true, was: was ? was.endpoint : null, scope: 'browser' } };
  }
  if (!(await hasProxyPermission())) {
    sendError(id, 'PROXY_PERMISSION_REQUIRED', PROXY_PERM_ERROR);
    return { error: true };
  }
  const before = await readBrowserProxy();
  try {
    await applyBrowserProxy(parsed.info);
  } catch (e) {
    sendError(id, 'PROXY_NOT_CONTROLLABLE',
      'Could not switch the browser to the proxy: ' + ((e && e.message) || e) +
      '. Nothing was loaded; this call did not fall back to a direct connection.');
    return { error: true };
  }
  return { error: false, change: { off: false, endpoint: parsed.info.endpoint, scope: 'browser',
                                   already: !!(before && before.endpoint === parsed.info.endpoint) } };
}

// Privileged pages where content scripts cannot run; re-derived for Chromium.
// (chrome:// / edge:// / devtools:// / chrome-extension:// / view-source: /
// the Chrome Web Store; about: covers about:blank, handled specially below.)
function isPrivilegedUrl(url) {
  return !url ||
    /^(about|chrome|edge|devtools|chrome-extension|view-source|data|javascript):/.test(url) ||
    url.startsWith('https://chromewebstore.google.com/') ||
    url.startsWith('https://chrome.google.com/webstore');
}

// Resolve the target tab for a request: default to the active tab when no tabId was
// given; turn a stale tabId into a STALE_TAB error; optionally refuse privileged pages
// with a named status.
async function resolveTabId(data, opts = {}) {
  let tabId = data.tabId;
  if (tabId == null) {
    const [active] = await browser.tabs.query({ active: true, lastFocusedWindow: true });
    if (!active) throw new Error('No tab_id given and no active tab found');
    tabId = active.id;
    data.tabId = tabId; // downstream handlers read data.tabId
  } else {
    try {
      await browser.tabs.get(tabId);
    } catch (e) {
      // The bare fact and its code. The server words the next step (open the agent's own
      // tab: response_hints.stale_tab_text) and knows when the id belongs to another
      // connected browser. The user's active tab is not named: pointing an agent at it
      // sends the agent to work on the tab the user is looking at.
      const err = new Error(`Tab ${tabId} no longer exists.`);
      err.code = 'STALE_TAB';
      throw err;
    }
  }
  if (opts.requireContent) {
    const tab = await browser.tabs.get(tabId);
    if (isPrivilegedUrl(tab.url)) {
      const shortUrl = (tab.url || 'unknown').split(/[?#]/)[0];
      const nav = TAB_NAV.get(tabId) || {};
      const isBlank = !tab.url || tab.url === 'about:blank';
      let msg = `Tab ${tabId} is a privileged page (${shortUrl}); content scripts cannot run there, so page_read/page_snapshot/page_act/page_eval are unavailable for it.`;
      if (isBlank && nav.lastAttemptedUrl) msg += ` The last attempted navigation was ${nav.lastAttemptedUrl}.`;
      if (isBlank && nav.lastNavError) msg += ` Last navigation error: ${humanizeNetError(nav.lastNavError)}.`;
      msg += isBlank
        ? ' Navigate it to an http(s) page first.'
        : ' Navigate it to a normal http(s) page first.';
      msg += ' To see this tab anyway: page_screenshot returns the painted pixels and requests returns its network log.';
      const err = new Error(msg);
      err.code = 'CONTENT_UNREACHABLE';
      throw err;
    }
  }
  return tabId;
}

// Wait until a tab "settles": document complete + network quiet + optional selector
// present, bounded by timeoutMs. expectNav additionally requires a navigation commit.
// It does not poll: between checks it sleeps until the next moment the answer can change
// (the quiet deadline, a young request ageing out, the timeout) and wakeSettle cuts the
// sleep short on a commit, a tab status or URL change, a finished load or a request
// ending. result.timing holds when each condition first held, in ms after the start.
async function waitForSettle(tabId, opts = {}) {
  const timeoutMs = Math.min(opts.timeoutMs || 20000, 120000);
  const quietMs = opts.quietMs != null ? opts.quietMs : NAV_QUIET_MS;
  const sinceTs = opts.sinceTs || Date.now();
  const expectNav = !!opts.expectNav;
  const selector = opts.selector || null;
  const start = Date.now();
  const deadline = start + timeoutMs;
  const result = { settled: false, timedOut: false };
  const timing = {};
  const stamp = (key) => { if (timing[key] === undefined) timing[key] = Date.now() - start; };
  let navCommitted = false;
  while (true) {
    if (Date.now() > deadline) { result.timedOut = true; break; }
    let tab;
    try {
      tab = await browser.tabs.get(tabId);
    } catch (e) { result.tabClosed = true; break; }
    const nav = TAB_NAV.get(tabId);
    // A fresh tab's own about:blank load can commit inside that allowance. When a real URL
    // was asked for (opts.ignoreBlankCommit), a tab still showing about:blank (or "", which
    // Chrome reports before the first commit) has not committed it.
    // opts.priorCommitAt is the tab's latest commit when the navigation was issued. It is
    // never that navigation's own, however recent: the allowance above would take it, and
    // with the old document still reporting "complete" a navigation issued just after a
    // load would settle on the page it was leaving.
    navCommitted = !!(nav && nav.lastCommitted > sinceTs - 100)
      && !(opts.ignoreBlankCommit && (!tab.url || tab.url === 'about:blank'))
      && !(opts.priorCommitAt != null && nav.lastCommitted <= opts.priorCommitAt);
    const net = TAB_NET.get(tabId) || { inflight: 0, lastActivity: 0 };
    // Only requests younger than 2.5s count as in flight (SSE and long-poll never end),
    // and a request that cannot put content in the page (a ping) does not count at all.
    const now = Date.now();
    const young = (TAB_REQUESTS.get(tabId) || [])
      .filter(r => !r.end && !r.error && (now - r.start) < 2500 && !QUIET_IGNORED_TYPES.has(r.type));
    const quietAt = Math.max(net.lastActivity, sinceTs) + quietMs;
    // opts.skipNetwork (wait="load" and wait="dom") ends on the load event and the commit alone.
    const quiet = !!opts.skipNetwork || (young.length === 0 && now >= quietAt);
    const loaded = tab.status === 'complete';
    const onNewDoc = !expectNav || navCommitted;
    if (expectNav && navCommitted) stamp('commit');
    if (onNewDoc && loaded) stamp('load');
    if (onNewDoc && loaded && quiet) stamp('quiet');
    let selectorOk = !selector;
    if (selector && loaded) {
      try {
        const res = await browser.scripting.executeScript({
          target: { tabId }, func: (sel) => !!document.querySelector(sel), args: [selector]
        });
        selectorOk = !!(res && res[0] && res[0].result);
      } catch (e) { selectorOk = false; } // mid-navigation; keep polling
    }
    if (loaded && quiet && selectorOk && onNewDoc) {
      result.settled = true;
      stamp('settled');
      break;
    }
    // The next moment this answer can change without an event: when the quiet window
    // closes, or when the oldest young request ages out of the count. A selector can
    // appear at any time, so it alone is polled.
    let wakeAt = deadline + 1;
    if (!quiet) {
      wakeAt = Math.min(wakeAt, young.length ? Math.min(...young.map(r => r.start + 2500)) : quietAt);
    }
    if (selector && loaded && quiet && onNewDoc) wakeAt = Math.min(wakeAt, now + SETTLE_SELECTOR_POLL_MS);
    // A missed wake-up costs at most this long, never the whole timeout.
    wakeAt = Math.min(wakeAt, now + SETTLE_RECHECK_MS);
    await sleepUntilWoken(tabId, wakeAt - now);
  }
  try {
    const tab = await browser.tabs.get(tabId);
    result.url = tab.url;
    result.title = tab.title;
    result.status = tab.status;
  } catch (e) { /* tab gone */ }
  result.waitedMs = Date.now() - start;
  result.navCommitted = navCommitted;
  result.timing = timing;
  if (selector) result.waitedForSelector = selector;
  return result;
}

// navigate's default quiet window: how long the network must stay idle after the load
// before the page counts as settled. wait="load" and wait="dom" use none.
const NAV_QUIET_MS = 250;
const SETTLE_SELECTOR_POLL_MS = 150;
const SETTLE_RECHECK_MS = 1000;

// ---- settle wake-ups ----------------------------------------------------------------
// A sleeping waitForSettle registers here; wakeSettle(tabId) ends every sleep on that tab.
// The timer is not cleared on a wake: it fires later into an already-settled promise.
const SETTLE_SLEEPERS = new Map();   // tabId -> Set<wake function>
function wakeSettle(tabId) {
  const sleepers = SETTLE_SLEEPERS.get(tabId);
  if (!sleepers) return;
  SETTLE_SLEEPERS.delete(tabId);
  for (const wake of sleepers) wake();
}

// Resolves after `ms`, or at the next wakeSettle(tabId), whichever comes first.
function sleepUntilWoken(tabId, ms) {
  return new Promise(resolve => {
    let done = false;
    const wake = () => {
      if (done) return;
      done = true;
      const sleepers = SETTLE_SLEEPERS.get(tabId);
      if (sleepers) {
        sleepers.delete(wake);
        if (!sleepers.size) SETTLE_SLEEPERS.delete(tabId);
      }
      resolve();
    };
    let sleepers = SETTLE_SLEEPERS.get(tabId);
    if (!sleepers) { sleepers = new Set(); SETTLE_SLEEPERS.set(tabId, sleepers); }
    sleepers.add(wake);
    setTimeout(wake, Math.max(0, ms));
  });
}

// Waits until `ready()` holds, re-checking at each wake-up, for at most `ms`. Returns
// whether it held.
async function waitForTabCondition(tabId, ms, ready) {
  const until = Date.now() + ms;
  while (!ready()) {
    const left = until - Date.now();
    if (left <= 0) return false;
    await sleepUntilWoken(tabId, left);
  }
  return true;
}

// Lighter than waitForSettle: wait only until a navigation COMMITS or errors.
async function waitForCommit(tabId, sinceTs, timeoutMs) {
  const start = Date.now();
  const budget = Math.min(timeoutMs || 8000, 30000);
  // Chrome reports an UNCOMMITTED tab's url as "" (the requested URL lives in
  // tab.pendingUrl until commit), whereas Firefox reports "about:blank" for the
  // same state. Normalize the empty url to about:blank so the did-not-commit
  // reporting names the tab's real (blank) location instead of an empty string;
  // the same commit-honesty Firefox gives. Only applied to
  // the NOT-committed branches; a committed tab always has a real url.
  const blankUrl = (u) => (u && u.length) ? u : 'about:blank';
  while (Date.now() - start < budget) {
    let tab;
    try { tab = await browser.tabs.get(tabId); } catch (e) { return { committed: false, tabClosed: true }; }
    const nav = TAB_NAV.get(tabId);
    if (nav && nav.lastNavError && nav.lastNavErrorAt >= sinceTs - 100) {
      return { committed: false, navError: nav.lastNavError, url: blankUrl(tab.url), title: tab.title };
    }
    if (nav && nav.lastCommitted >= sinceTs - 100 && tab.url && tab.url !== 'about:blank') {
      return { committed: true, url: tab.url, title: tab.title, status: tab.status };
    }
    await new Promise(r => setTimeout(r, 100));
  }
  let tab = null; try { tab = await browser.tabs.get(tabId); } catch (e) { /* gone */ }
  return { committed: false, timedOut: true, url: tab ? blankUrl(tab.url) : undefined, title: tab ? tab.title : undefined };
}

// Digest of network activity in a tab since t0.
function netDigestSince(tabId, t0) {
  return netDigestFor(tabId, t0, null);
}

// The digest of a navigation whose document committed at `committedAt`. A sub-resource
// that started before that commit and was aborted belonged to the document the navigation
// left, and the navigation cancelled it, so it is left out. Only an abort is: a refused,
// reset or 4xx request still lists, and so does the main-frame request.
function netDigestFor(tabId, t0, committedAt) {
  const log = TAB_REQUESTS.get(tabId) || [];
  const entries = log.filter(r => r.start >= t0 - 50
    && !(committedAt && r.type !== 'main_frame' && r.start < committedAt && isNavAbort(r.error)));
  const failed = entries.filter(r =>
    ((r.status && r.status >= 400) || r.error) && !/\/favicon\.ico(\?|$)/.test(r.url));
  const digest = {
    requests: entries.length,
    pending: entries.filter(r => !r.end).length
  };
  if (failed.length > 0) {
    digest.failed = failed.slice(0, 10).map(r => ({
      method: r.method,
      url: r.url.slice(0, 200),
      status: r.status || undefined,
      error: r.error || undefined
    }));
  }
  return digest;
}

function consoleErrorsSince(tabId, t0) {
  return consoleErrorDigest(tabId, t0, null);
}

// The console errors of the call that began at t0. rx is when an entry reached
// TAB_CONSOLE, on this side's clock: one that arrived before t0 was there for an earlier
// call to report. ts is the page's clock, which can drift a few ms from this one, so it
// gets CONSOLE_CLOCK_SLACK_MS; it decides only for an entry logged just before t0 that
// arrived after it. `doc` keeps navigate's digest to the document that answered its flush,
// since the document it replaced can still deliver entries after t0 (its batch timer, an
// error while it unloads). The count and the list come from the same filtered set.
const CONSOLE_CLOCK_SLACK_MS = 50;
function consoleErrorDigest(tabId, t0, doc) {
  const buf = TAB_CONSOLE.get(tabId) || [];
  const errs = buf.filter(e => e.level === 'error'
    && (e.rx === undefined || e.rx >= t0)
    && e.ts >= t0 - CONSOLE_CLOCK_SLACK_MS
    && (!doc || e.doc === doc));
  return { errors: errs.length, first: errs.slice(0, 3).map(e => e.text.slice(0, 300)) };
}

// The one way into TAB_CONSOLE: a batch from the relay, or the entries a flush answer
// carries. A batch still in flight when the relay answered arrives both ways, so an entry
// whose doc and n are already held is dropped.
function takeConsoleEntries(tabId, entries) {
  if (!Array.isArray(entries) || entries.length === 0) return;
  let buf = TAB_CONSOLE.get(tabId);
  if (!buf) { buf = []; TAB_CONSOLE.set(tabId, buf); }
  const held = new Set();
  for (const e of buf) if (e.doc) held.add(e.doc + ':' + e.n);
  const rx = Date.now();
  for (const e of entries) {
    if (!e || typeof e !== 'object') continue;
    if (e.doc) {
      const key = e.doc + ':' + e.n;
      if (held.has(key)) continue;
      held.add(key);
    }
    buf.push(Object.assign({}, e, { rx: rx }));
  }
  if (buf.length > CONSOLE_MAX) buf.splice(0, buf.length - CONSOLE_MAX);
}

// ---- console flush --------------------------------------------------------------------
// content.js sends console entries on a 300 ms batch timer, which a background tab's timer
// clamp stretches to about a second, so a navigation can settle while the page's load-time
// errors are still in the tab. Whatever reads TAB_CONSOLE for a result first asks the top
// frame's relay for them ('flushConsole'). The relay answers in the task that receives the
// request, and the answer carries every entry the page emitted before it that TAB_CONSOLE
// may not hold yet, so what is read next does not depend on that timer, and a busy page
// delays the answer only by the task it is running. Resolves 'flushed'; 'unreachable' when
// the tab has no relay to ask (a privileged page, an error page, a document content scripts
// never ran in), which holds nothing back; or 'timeout' when the page did not answer within
// budgetMs (a main thread stuck in one task), and then the read may lack entries and the
// result has to say so. An answer that arrives after the timeout still lands in
// TAB_CONSOLE. `answer`, when given, receives the answering document's id as answer.doc.
const CONSOLE_FLUSH_TIMEOUT_MS = 500;
async function flushConsoleCapture(tabId, budgetMs = CONSOLE_FLUSH_TIMEOUT_MS, answer = null) {
  const budget = Math.min(CONSOLE_FLUSH_TIMEOUT_MS, budgetMs);
  if (!(budget > 0)) return 'timeout';
  let timer = null;
  const expired = new Promise(resolve => { timer = setTimeout(() => resolve('timeout'), budget); });
  let answered;
  try {
    answered = browser.tabs.sendMessage(tabId, { action: 'flushConsole' }, { frameId: 0 })
      .then((reply) => {
        if (reply && typeof reply === 'object') {
          takeConsoleEntries(tabId, reply.entries);
          if (answer && typeof reply.doc === 'string') answer.doc = reply.doc;
        }
        return 'flushed';
      }, () => 'unreachable');
  } catch (e) {
    answered = Promise.resolve('unreachable');
  }
  const outcome = await Promise.race([answered, expired]);
  clearTimeout(timer);
  return outcome;
}

// Background-driven wait used by content.act 'wait' steps: re-injects a ONE-SHOT
// condition check every 250ms. Survives navigations mid-wait.
async function actWait(tabId, step, frameId) {
  const timeoutMs = Math.min(step.timeout_ms || step.timeoutMs || 10000, 120000);
  const start = Date.now();
  const checkArgs = {
    selector: step.selector || step.target,
    textContains: step.text_contains || step.textContains,
    urlContains: step.url_contains || step.urlContains,
    predicate: step.predicate,
    gone: !!step.gone,
    requireVisible: !!(step.require_visible || step.requireVisible),
    within: step.within
  };
  if (!checkArgs.selector && !checkArgs.textContains && !checkArgs.urlContains && !checkArgs.predicate) {
    return { ok: false, error: 'wait step needs at least one of: target (CSS selector), text_contains, url_contains, predicate' };
  }
  let last = null;
  let lastSeenCheck = null;
  let maxMatchCount = 0;
  let nodesFirst = null;        // the element count at the first and last poll that
  let nodesLast = null;         // observed the document; free, every wait_check carries it
  let scopeEverSeen = false;
  let scopeMissingStreak = 0;
  while (Date.now() - start < timeoutMs) {
    try {
      last = await runAutomation(tabId, 'wait_check', checkArgs, frameId);
      if (last && last.error && /no result from injected automation/i.test(last.error)) {
        last = { injectError: last.error };
      }
      if (last && !last.injectError) {
        lastSeenCheck = last;
        if (typeof last.selectorMatchCount === 'number' && last.selectorMatchCount > maxMatchCount) {
          maxMatchCount = last.selectorMatchCount;
        }
        if (typeof last.nodesAtEnd === 'number') {
          if (nodesFirst === null) nodesFirst = last.nodesAtEnd;
          nodesLast = last.nodesAtEnd;
        }
        if (checkArgs.within && !last.scopeMissing) scopeEverSeen = true;
        if (last.scopeMissing) scopeMissingStreak++; else scopeMissingStreak = 0;
      }
      if (last && last.error) {
        return { ok: false, error: last.error, elapsedMs: Date.now() - start };
      }
      if (last && last.matched && !checkArgs.gone) {
        return { ok: true, matched: last.matched, elapsedMs: Date.now() - start, url: last.url };
      }
      const hasScopedCond = !!(checkArgs.selector || checkArgs.textContains);
      const goneSatisfied = last && checkArgs.gone &&
        (last.matched ||
         (last.scopeMissing && scopeEverSeen && hasScopedCond && scopeMissingStreak >= 2));
      if (goneSatisfied) {
        let tabLoading = false;
        try { tabLoading = (await browser.tabs.get(tabId)).status === 'loading'; } catch (e) {}
        if (!tabLoading) {
          return last.scopeMissing
            ? { ok: true, matched: 'scope-gone', scopeMissing: last.scopeMissing,
                elapsedMs: Date.now() - start, url: last.url }
            : { ok: true, matched: last.matched, elapsedMs: Date.now() - start, url: last.url };
        }
      }
    } catch (e) {
      last = { injectError: e.message };
    }
    const remaining = timeoutMs - (Date.now() - start);
    if (remaining <= 0) break;
    await new Promise(r => setTimeout(r, Math.min(250, remaining)));
  }
  const obs = lastSeenCheck || {};
  const out = {
    ok: false,
    timedOut: true,
    elapsedMs: Date.now() - start,
    requestedTimeoutMs: timeoutMs,
    predicateError: obs.predicateError || undefined,
    injectError: (last && last.injectError) || undefined,
    waitedFor: {
      selector: checkArgs.selector, text_contains: checkArgs.textContains,
      url_contains: checkArgs.urlContains, predicate: checkArgs.predicate, gone: checkArgs.gone,
      within: checkArgs.within
    }
  };
  if (checkArgs.within && !scopeEverSeen && obs.scopeMissing) {
    out.lastSeen = 'scope-never-appeared';
    out.scopeMissing = obs.scopeMissing;
  } else if (checkArgs.selector && typeof obs.selectorMatchCount === 'number') {
    out.matchCount = obs.selectorMatchCount;
    if (obs.scopeMissing) out.scopeMissing = obs.scopeMissing;
    if (checkArgs.gone) {
      out.lastSeen = obs.selectorVisible === false ? 'still-present-but-hidden'
        : obs.selectorVisible === true ? 'still-present-and-visible'
        : 'still-present';
    } else if (maxMatchCount === 0) {
      out.lastSeen = 'never-matched';
    } else if (obs.selectorMatchCount === 0) {
      out.lastSeen = 'appeared-then-vanished';
      out.matchCount = maxMatchCount;
    } else if (checkArgs.requireVisible) {
      out.lastSeen = 'present-but-hidden';
    }
  }
  if (obs.scopeMissing && out.scopeMissing === undefined && out.lastSeen !== 'scope-never-appeared') {
    out.scopeMissing = obs.scopeMissing;
  }
  // A wait that timed out on a page that was BUSY the whole time is a different
  // story from one that timed out on a page that never moved; and the polls already
  // measured it.
  if (nodesFirst !== null && nodesLast !== null) {
    out.nodesBefore = nodesFirst;
    out.nodesAfter = nodesLast;
    out.nodeDelta = nodesLast - nodesFirst;
  }
  return out;
}

// Console buffer reader (console.read action)
async function handleConsoleAction(id, action, data) {
  try {
    switch (action) {
      case 'console.read': {
        const tabId = await resolveTabId(data);
        const flushed = await flushConsoleCapture(tabId);
        const buf = TAB_CONSOLE.get(tabId) || [];
        const nav = TAB_NAV.get(tabId);
        let entries = buf;
        if ((data.since === 'nav' || data.since == null) && nav) {
          entries = buf.filter(e => e.ts >= nav.lastCommitted - 1000);
        } else if (typeof data.sinceTs === 'number') {
          entries = buf.filter(e => e.ts >= data.sinceTs);
        }
        const levelRank = { debug: 0, log: 1, info: 1, warn: 2, error: 3 };
        const minLevel = data.level === 'all' ? 0 : (levelRank[data.level] != null ? levelRank[data.level] : 2);
        entries = entries.filter(e => (levelRank[e.level] != null ? levelRank[e.level] : 1) >= minLevel);
        const total = entries.length;
        entries = entries.slice(-(data.limit || 50));
        if (data.clear) TAB_CONSOLE.set(tabId, []);
        const reply = { tabId: tabId, total: total, entries: entries, bufferedTotal: buf.length };
        if (flushed === 'timeout') reply.consoleFlushTimedOut = true;
        sendResponse(id, action, reply);
        break;
      }
      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown console action: ${action}`);
    }
  } catch (error) {
    sendError(id, error.code || 'API_ERROR', `Console error: ${error.message}`);
  }
}

// --- Screenshot capture ------------------------------------------------------
// Chromium has NO tabs.captureTab(tabId) (focus-free per-tab capture) and NO
// captureTab({rect}) one-shot region capture; the ONLY primitive is
// tabs.captureVisibleTab, which captures the ACTIVE tab of a window, viewport only,
// rate-limited to ~2/s. So EVERY capture goes through activate+captureVisibleTab
// (+restore); region/element/full-page are the scroll-and-stitch loop (gecko's
// legacy fallback, promoted to the primary path). The SW has no Image/canvas, so
// bitmaps go through createImageBitmap + OffscreenCanvas. Focus-stealing where
// gecko was focus-free is a documented parity cost (design D6). The honesty layer
// (paint-state probe, pageWarning, imageIsUniform, WINDOW_NOT_PAINTED race,
// forceEagerImagesInRegion) is preserved.

// Decode a dataURL to an ImageBitmap (SW has no Image()).
async function loadBitmap(dataUrl) {
  const resp = await fetch(dataUrl);
  const blob = await resp.blob();
  return await createImageBitmap(blob);
}

// Encode a Blob to a dataURL without FileReader (arrayBuffer + base64FromBytes).
async function blobToDataURL(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const b64 = base64FromBytes(bytes);
  return 'data:' + (blob.type || 'application/octet-stream') + ';base64,' + b64;
}

// Decode a captured image and report whether it is ONE uniform color; the signature
// of a blank frame. Uses OffscreenCanvas + createImageBitmap (both available in a
// worker). Returns the color ("rgb(…)") when uniform, else null.
async function imageIsUniform(dataUrl) {
  const bmp = await loadBitmap(dataUrl);
  const w = Math.max(1, Math.min(64, bmp.width || 1));
  const h = Math.max(1, Math.min(64, bmp.height || 1));
  const canvas = new OffscreenCanvas(w, h);
  const ctx = canvas.getContext('2d');
  ctx.drawImage(bmp, 0, 0, w, h);
  const px = ctx.getImageData(0, 0, w, h).data;
  const r0 = px[0], g0 = px[1], b0 = px[2];
  const TOL = 3;
  for (let i = 0; i < px.length; i += 4) {
    if (Math.abs(px[i] - r0) > TOL || Math.abs(px[i + 1] - g0) > TOL ||
        Math.abs(px[i + 2] - b0) > TOL) return null;
  }
  return 'rgb(' + r0 + ',' + g0 + ',' + b0 + ')';
}

// Wait (bounded) for the visible viewport to be paint-stable before a capture.
// Injected as a func into the ISOLATED world (shared DOM). settle_ms <= 0 disables.
async function settleForCapture(tabId, capMs, region) {
  const cap = (capMs == null) ? 1200 : Math.max(0, capMs);
  if (cap === 0) return { settled: true, skipped: true };
  const fn = async function (cap, region) {
    var t0 = Date.now();
    var deadline = t0 + cap;
    var remain = function () { return Math.max(0, deadline - Date.now()); };
    var timeout = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
    try {
      if (document.fonts && document.fonts.ready) await Promise.race([document.fonts.ready, timeout(remain())]);
    } catch (e) {}
    try {
      var iw = window.innerWidth || 0, ih = window.innerHeight || 0;
      var imgs = Array.prototype.slice.call(document.images || []).filter(function (im) {
        var r = im.getBoundingClientRect();
        if (!(r.width > 0 && r.height > 0)) return false;
        if (region) {
          var top = r.top + window.scrollY, left = r.left + window.scrollX;
          return top + r.height > region.y && top < region.y + region.h &&
                 left + r.width > region.x && left < region.x + region.w;
        }
        return r.bottom > 0 && r.top < ih && r.right > 0 && r.left < iw;
      });
      var waits = imgs.map(function (im) {
        if (im.complete && im.naturalWidth > 0) return im.decode ? im.decode().catch(function () {}) : Promise.resolve();
        return new Promise(function (res) {
          var done = function () { res(); };
          im.addEventListener('load', done, { once: true });
          im.addEventListener('error', done, { once: true });
        });
      });
      await Promise.race([Promise.all(waits), timeout(remain())]);
    } catch (e) {}
    await Promise.race([
      new Promise(function (r) {
        if (typeof requestAnimationFrame === 'function') requestAnimationFrame(function () { requestAnimationFrame(r); });
        else setTimeout(r, 16);
      }),
      timeout(Math.min(remain(), 350))
    ]);
    return { settled: Date.now() <= deadline, waitedMs: Date.now() - t0 };
  };
  try {
    const res = await browser.scripting.executeScript({ target: { tabId }, func: fn, args: [cap, region || null] });
    return (res && res[0] && res[0].result) || { settled: false };
  } catch (e) {
    return { settled: false, error: String((e && e.message) || e) };
  }
}

// A captureVisibleTab promise never settles for a window the compositor is not
// painting (behind another app / minimized); race it against a short timeout.
const VISIBLE_CAPTURE_TIMEOUT_MS = 4000;
function raceCapture(promise, what) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => {
      const err = new Error(what + ' timed out after ' + VISIBLE_CAPTURE_TIMEOUT_MS + 'ms; ' +
        'the window is not being painted (the browser is likely behind another app or minimized). ' +
        'Use a plain viewport screenshot (no selector/full_page), or bring the browser window ' +
        'to the front (tabs action="activate"), then retry.');
      err.code = 'WINDOW_NOT_PAINTED';
      reject(err);
    }, VISIBLE_CAPTURE_TIMEOUT_MS);
    promise.then(v => { clearTimeout(t); resolve(v); }, e => { clearTimeout(t); reject(e); });
  });
}

// tabs.captureVisibleTab is rate-limited to MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND
// = 2/s; the scroll-and-stitch loop fires many captures back-to-back, so pace them
// >500ms apart or the browser throws a quota error mid-stitch.
let _lastVisibleCaptureAt = 0;
async function captureVisiblePaced(windowId, opts) {
  const MIN_GAP = 550;
  const wait = _lastVisibleCaptureAt + MIN_GAP - Date.now();
  if (wait > 0) await new Promise(r => setTimeout(r, wait));
  _lastVisibleCaptureAt = Date.now();
  return browser.tabs.captureVisibleTab(windowId, opts);
}

// Ensure a tab is the ACTIVE tab of its window (recording the previously-active tab
// to restore), then capture the painted viewport raced against WINDOW_NOT_PAINTED.
async function grabVisibleViewport(tabId, format, quality, windowIdHint) {
  let windowId = windowIdHint || null;
  let restoreTabId = null;
  if (tabId != null) {
    const tab = await browser.tabs.get(tabId);
    windowId = tab.windowId;
    if (!tab.active) {
      const [prev] = await browser.tabs.query({ active: true, windowId });
      if (prev) restoreTabId = prev.id;
      await browser.tabs.update(tabId, { active: true });
      await new Promise(r => setTimeout(r, 120));
    }
  }
  try {
    const opts = { format };
    if (format === 'jpeg') opts.quality = quality;
    const dataUrl = await raceCapture(captureVisiblePaced(windowId, opts), 'captureVisibleTab');
    return { dataUrl, windowId };
  } finally {
    if (restoreTabId != null) {
      try { await browser.tabs.update(restoreTabId, { active: true }); } catch (e) { /* ignore */ }
    }
  }
}

async function captureScreenshot(data) {
  const format = data.format || 'png';
  const quality = data.quality || 90;

  // Element-only capture (by selector/ref/text): resolve, scroll, settle, crop.
  if (data.selector && data.tabId != null) {
    return await captureElement(data.tabId, data.selector, format, quality, data.settleMs);
  }

  // Full-page: scroll-and-stitch (no black bands below the fold).
  if (data.fullPage && data.tabId != null) {
    return await captureRegion(data.tabId, null, format, quality, data.settleMs);
  }

  // Settle paint BEFORE a viewport capture.
  let settleWarning;
  if (data.tabId != null) {
    const s = await settleForCapture(data.tabId, data.settleMs);
    if (s && s.settled === false && !s.skipped && !s.error) {
      settleWarning = 'capture may not have fully settled (lazy media/animation still in progress)';
    }
  }

  const { dataUrl, windowId } = await grabVisibleViewport(data.tabId, format, quality, data.windowId);
  return { dataUrl, format, quality, tabId: data.tabId != null ? data.tabId : null, windowId,
           method: 'captureVisibleTab', settleWarning };
}

// Native lazy images in an INACTIVE tab never load; flip pending lazy images inside
// the capture region to eager so their loads start.
async function forceEagerImagesInRegion(tabId, rx, ry, rw, rh) {
  const fn = function (region) {
    var n = 0;
    var imgs = document.querySelectorAll('img[loading="lazy" i]');
    for (var i = 0; i < imgs.length; i++) {
      var im = imgs[i];
      if (im.complete && im.naturalWidth > 0) continue;
      var r = im.getBoundingClientRect();
      var top = r.top + window.scrollY, left = r.left + window.scrollX;
      if (top + r.height > region.y && top < region.y + region.h &&
          left + r.width > region.x && left < region.x + region.w) {
        im.loading = 'eager'; n++;
      }
    }
    return n;
  };
  try {
    await browser.scripting.executeScript({ target: { tabId }, func: fn, args: [{ x: rx, y: ry, w: rw, h: rh }] });
  } catch (e) { /* best-effort (privileged page) */ }
}

// Region capture (an element's box, or clip=null for full page). Chromium path:
// activate + scroll-and-stitch captureVisibleTab (needs a painted window; each shot
// fails fast via raceCapture). Lazy content is force-eager'd first; each band is
// settled before capture, preserving the no-black-bands guarantee.
async function captureRegion(tabId, clip, format, quality, settleMs) {
  const tab = await browser.tabs.get(tabId);
  const windowId = tab.windowId;

  let metrics = null;
  try {
    const res = await browser.scripting.executeScript({
      target: { tabId },
      func: () => ({
        sh: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0),
        cw: document.documentElement.clientWidth, ch: document.documentElement.clientHeight,
        dpr: window.devicePixelRatio || 1, sx: window.scrollX, sy: window.scrollY
      })
    });
    metrics = res && res[0] && res[0].result;
  } catch (e) {
    // Content scripts blocked (chrome://, chrome-extension://, Web Store, …). Degrade
    // a full-page request to a plain viewport captureVisibleTab with a note instead of
    // erroring "Missing host permission".
    if (!clip) {
      const { dataUrl, windowId: wid } = await grabVisibleViewport(tabId, format, quality, windowId);
      return { dataUrl, format, quality, tabId, windowId: wid, method: 'captureVisibleTab', pages: 1,
               pageWarning: 'full_page is unavailable on this page (content scripts are blocked on ' +
                 'privileged pages like chrome:// or chrome-extension://, and full-page capture needs ' +
                 'in-page scroll metrics); captured the visible viewport instead.' };
    }
    const err = new Error('cannot measure this page for an element capture: content scripts are ' +
      'blocked on privileged pages (' + e.message + '). A plain viewport screenshot (no ' +
      'full_page/selector) works here.');
    err.code = 'PRIVILEGED_PAGE';
    throw err;
  }
  if (!metrics) throw new Error('could not measure page for capture');
  const dpr = metrics.dpr;
  const vw = metrics.cw, vh = metrics.ch;
  const MAX_HEIGHT = 20000; // safety cap (canvas + image-size limits)

  const regionOf = () => {
    let rx = clip ? Math.max(0, clip.x) : 0;
    let ry = clip ? Math.max(0, clip.y) : 0;
    let rw = clip ? clip.w : metrics.cw;
    let rh = clip ? clip.h : metrics.sh;
    let truncated = false;
    if (rh > MAX_HEIGHT) { rh = MAX_HEIGHT; truncated = true; }
    return { rx: Math.round(rx), ry: Math.round(ry),
             rw: Math.max(1, Math.round(rw)), rh: Math.max(1, Math.round(rh)), truncated };
  };

  // Activate + scroll-and-stitch (needs a painted, frontmost window).
  let restoreTabId = null;
  if (!tab.active) {
    const [prev] = await browser.tabs.query({ active: true, windowId: windowId });
    if (prev) restoreTabId = prev.id;
    await browser.tabs.update(tabId, { active: true });
    await new Promise(r => setTimeout(r, 150));
  }
  let couldNotSettle = false;
  try {
    const region = regionOf();
    const rx = region.rx, ry = region.ry, truncated = region.truncated;
    let rw = region.rw;
    const rh = region.rh;
    let clippedWidth = false;
    if (rw > vw) { rw = vw; clippedWidth = true; } // one viewport wide (we scroll x to rx)

    if (settleMs !== 0) {
      await forceEagerImagesInRegion(tabId, rx, ry, rw, rh);
    }

    const canvas = new OffscreenCanvas(Math.max(1, Math.round(rw * dpr)), Math.max(1, Math.round(rh * dpr)));
    const ctx = canvas.getContext('2d');

    const step = Math.max(vh, 100);
    let pages = 0;
    for (let y = ry; y < ry + rh; y += step) {
      // scroll, then read back the ACTUAL offset; the browser clamps the last step.
      const posRes = await browser.scripting.executeScript({
        target: { tabId },
        func: (x, yy) => { window.scrollTo(x, yy); return { x: window.scrollX, y: window.scrollY }; },
        args: [rx, y]
      });
      const pos = posRes && posRes[0] && posRes[0].result;
      const asx = pos && typeof pos.x === 'number' ? pos.x : rx;
      const asy = pos && typeof pos.y === 'number' ? pos.y : y;
      const s = await settleForCapture(tabId, settleMs);
      if (s && s.settled === false && !s.skipped && !s.error) couldNotSettle = true;
      const opts = { format: format };
      if (format === 'jpeg') opts.quality = quality;
      const dataUrl = await raceCapture(captureVisiblePaced(windowId, opts), 'captureVisibleTab');
      const bmp = await loadBitmap(dataUrl);
      pages++;
      const rowTop = Math.max(ry, asy);
      const rowBot = Math.min(ry + rh, asy + vh);
      if (rowBot <= rowTop) continue;
      const sxSrc = Math.round((rx - asx) * dpr);
      const sySrc = Math.round((rowTop - asy) * dpr);
      const swSrc = Math.round(rw * dpr);
      const shSrc = Math.round((rowBot - rowTop) * dpr);
      const dy = Math.round((rowTop - ry) * dpr);
      if (swSrc > 0 && shSrc > 0) {
        try { ctx.drawImage(bmp, sxSrc, sySrc, swSrc, shSrc, 0, dy, swSrc, shSrc); } catch (e) { /* skip a bad band */ }
      }
    }
    await browser.scripting.executeScript({
      target: { tabId }, func: (x, y) => window.scrollTo(x, y), args: [metrics.sx, metrics.sy]
    });

    const mime = format === 'jpeg' ? 'image/jpeg' : 'image/png';
    const blob = await canvas.convertToBlob({ type: mime, quality: format === 'jpeg' ? quality / 100 : undefined });
    const stitched = await blobToDataURL(blob);
    const out = { dataUrl: stitched, format, quality, tabId, windowId,
      method: clip ? 'element' : 'fullPage', pages: pages, truncated: truncated };
    if (couldNotSettle) out.settleWarning = 'capture may not have fully settled (lazy media/animation still in progress)';
    if (clippedWidth) out.clippedWidth = true;
    return out;
  } finally {
    if (restoreTabId != null) {
      try { await browser.tabs.update(restoreTabId, { active: true }); } catch (e) { /* ignore */ }
    }
  }
}

// Element-only screenshot: resolve + scroll + measure via the automation toolkit,
// then capture the element's page-coords box via captureRegion.
async function captureElement(tabId, selector, format, quality, settleMs) {
  const r = await runAutomation(tabId, 'rect', { target: selector });
  if (!r || !r.ok) {
    const err = new Error('element screenshot: ' + ((r && r.error) || ('could not resolve "' + selector + '"')));
    err.code = (r && r.matchCount > 1) ? 'AMBIGUOUS_SELECTOR' : 'NO_ELEMENT';
    throw err;
  }
  const box = r.page || {};
  if (!(box.w >= 1) || !(box.h >= 1)) {
    const err = new Error('element screenshot: "' + selector + '" has a zero-size box (display:none or not laid out)');
    err.code = 'NO_ELEMENT';
    throw err;
  }
  const out = await captureRegion(tabId, box, format, quality, settleMs);
  out.method = 'element';
  out.selector = selector;
  out.elementBox = { w: Math.round(box.w), h: Math.round(box.h) };
  return out;
}

// Tabs handlers
// ---- file:// open/navigate refusal -----------------------------------------
// A file:// open used to come back as a bare "Illegal URL"; the browser's own words,
// which answer NONE of the three questions the agent then has: did a tab get created
// anyway, is this a setting I can change, and what does work instead? An earlier
// session had an agent spend five calls working around a local file that had ALREADY
// opened. Re-measured on Firefox 153 the tab is NOT created. Both observations are
// real, so neither outcome may be hard-coded: CHECK the tab list, then say what
// actually happened.
//
// The second half of the old message's damage was implying the whole capability is
// missing. It is not: READING an already-open file:// tab is a separate, user-grantable
// permission, and this defers to the existing getFileAccessState / FILE_ACCESS_DISABLED
// vocabulary for that half rather than inventing a second one.
//
// Best-effort by construction; an unanswerable tabs.query must never replace a real
// platform error with a worse one, so a failed probe simply omits the tab clause.
// Chrome names the switch on the extension's details page.
const FILE_ACCESS_TOGGLE = 'chrome://extensions > BrowserPlug > Details > "Allow access to file URLs"';

async function fileUrlRefusal(url, verb, rawMessage) {
  const wanted = String(url || '').split(/[?#]/)[0];
  if (!/^file:/i.test(wanted)) return null;
  let landed = null;
  try {
    const all = await browser.tabs.query({});
    landed = (all || []).find(t => t.url && t.url.split(/[?#]/)[0] === wanted) || null;
  } catch (e) { /* best-effort */ }

  let msg = 'the browser refused to ' + verb + ' ' + wanted + '; it rejected the URL itself ("' +
    String(rawMessage || 'Illegal URL') + '"). ';
  if (landed) {
    msg += 'A TAB IS NEVERTHELESS OPEN ON THAT FILE: tab ' + landed.id +
      (landed.title ? ' ("' + String(landed.title).slice(0, 60) + '")' : '') +
      '; the refusal was PARTIAL. Do not retry and do not work around it: read that tab ' +
      'directly (page_read / page_snapshot / page_act with tab_id=' + landed.id + '). ';
  } else {
    msg += 'NO tab was created and nothing was navigated; the tab you targeted is exactly ' +
      'where it was. Opening a local file is a browser platform rule, NOT a BrowserPlug setting ' +
      'and not the file-access permission: the user has to open it by hand (Ctrl+O, or drag ' +
      'the file into the window). ';
  }

  let state = null;
  try { state = await getFileAccessState(); } catch (e) { /* unknown is a valid answer */ }
  const granted = state ? state.can_read_files : null;
  msg += 'READING an already-open file:// tab is a SEPARATE, user-grantable permission: ';
  if (granted === true) {
    msg += 'it is currently GRANTED for this browser (server_info > ' +
      'extension_file_access.can_read_opened_file_tab), so every page_* tool works on such a tab.';
  } else if (granted === false) {
    msg += 'it is currently OFF; turn it on at ' + FILE_ACCESS_TOGGLE + ', then page_read / ' +
      'page_snapshot / page_act / page_eval all work on that tab.';
  } else {
    msg += 'its state could not be read here; check server_info > ' +
      'extension_file_access.can_read_opened_file_tab; the switch is ' + FILE_ACCESS_TOGGLE + '.';
  }
  return msg;
}

async function handleTabsAction(id, action, data) {
  try {
    switch (action) {
      case 'tabs.list': {
        const tabs = await browser.tabs.query(data.currentWindow ? { currentWindow: true } : {});
        let windows = [];
        try {
          windows = (await browser.windows.getAll({})).map(w => ({
            id: w.id, focused: w.focused, state: w.state, type: w.type,
            incognito: w.incognito, width: w.width, height: w.height
          }));
        } catch (e) { /* window info is best-effort */ }
        sendResponse(id, action, {
          tabs: tabs.map(tab => ({
            url: tab.url, id: tab.id, title: tab.title, active: tab.active,
            windowId: tab.windowId, pinned: tab.pinned, index: tab.index,
            status: tab.status, discarded: tab.discarded, hidden: tab.hidden,
            audible: tab.audible, muted: !!(tab.mutedInfo && tab.mutedInfo.muted),
            isInReaderMode: tab.isInReaderMode, lastAccessed: tabLastAccessedMs(tab)
          })),
          windows: windows,
          // The one proxy Chromium has is browser-wide: null, or what we set.
          browserProxy: await readBrowserProxy(),
          lastFocusedWindowId: LAST_FOCUSED_WINDOW_ID,
          debug: {
            totalFound: tabs.length
          }
        });
        break;
      }

      case 'tabs.active':
        const [activeTab] = await browser.tabs.query({ active: true, lastFocusedWindow: true });
        sendResponse(id, action, { tab: activeTab });
        break;

      // Read back the routing. Chromium has no per-tab proxy, so a tab is never `routed`;
      // the browser-wide one is reported beside it. A `proxy` here is the cheap way to
      // switch it off ("" or "direct") without opening or navigating anything.
      case 'tabs.routing': {
        const applied = await applyProxyField(id, data.proxy);
        if (applied.error) break;
        const rTabId = await resolveTabId(data);
        sendResponse(id, action, { tabId: rTabId, routed: false, type: 'direct',
                                   // A tab is behind a proxy exactly when the browser-wide one is.
                                   isProxy: await browserProxyIsInForce(),
                                   browserProxy: await readBrowserProxy(),
                                   browserProxyChange: applied.change });
        break;
      }

      case 'tabs.create': {
        // Switch the whole browser BEFORE the tab opens, so its first request already goes
        // through the proxy. A refusal (bad value, no permission, setting owned by someone
        // else) stops here: no tab is opened on a direct path.
        const applied = await applyProxyField(id, data.proxy);
        if (applied.error) break;

        const createTabOptions = { active: data.active || false };
        if (data.windowId) createTabOptions.windowId = data.windowId;
        if (data.pinned !== undefined) createTabOptions.pinned = data.pinned;
        createTabOptions.url = data.url;

        const t0commit = Date.now();
        const newTab = await browser.tabs.create(createTabOptions);
        // Atomic open-and-load: await the commit so we never hand back a tab still
        // parked on about:blank with the URL "not applied yet".
        if (data.url && data.url !== 'about:blank') {
          const c = await waitForCommit(newTab.id, t0commit, 8000);
          if (c.url) newTab.url = c.url;
          if (c.title) newTab.title = c.title;
          if (!c.committed) {
            newTab.navPending = data.url;
            if (c.navError) newTab.navError = humanizeNetError(c.navError);
            else if (c.timedOut) newTab.navTimedOut = true;
          }
        }
        sendResponse(id, action, { tab: newTab, browserProxyChange: applied.change });
        break;
      }

      case 'tabs.close':
        await browser.tabs.remove(data.tabId);
        sendResponse(id, action, { success: true });
        break;

      case 'tabs.update':
        const updatedTab = await browser.tabs.update(data.tabId, {
          url: data.url,
          active: data.active
        });
        sendResponse(id, action, { tab: updatedTab });
        break;

      case 'tabs.switch': {
        const switchTabId = await resolveTabId(data);
        await browser.tabs.update(switchTabId, { active: true });
        let focusedWindow = false;
        if (data.focusWindow !== false) {
          try {
            const t = await browser.tabs.get(switchTabId);
            await browser.windows.update(t.windowId, { focused: true });
            focusedWindow = true;
          } catch (e) { /* best-effort */ }
        }
        sendResponse(id, action, { success: true, tabId: switchTabId, focusedWindow: focusedWindow });
        break;
      }

      case 'tabs.screenshot':
      case 'tabs.captureVisibleTab':
        try {
          await resolveTabId(data); // resolve active tab when omitted + stale-tab enrichment
          const shot = await captureScreenshot(data);
          // Paint-state probe: a tab in an unfocused/minimized window is not laid out
          // and captures blank; say so. Never OVERWRITE a warning already attached.
          if (data.tabId != null && !shot.pageWarning) {
            try {
              const stateRes = await browser.scripting.executeScript({
                target: { tabId: data.tabId },
                func: () => {
                  var b = document.body, r = b ? b.getBoundingClientRect() : null;
                  var w = r ? Math.round(r.width) : 0, h = r ? Math.round(r.height) : 0;
                  var hid = false;
                  try { hid = b && getComputedStyle(b).display === 'none'; } catch (e) {}
                  return { painted: (w > 0 && h > 0 && !hid), hiddenByCss: !!hid, focused: document.hasFocus() };
                }
              });
              const state = stateRes && stateRes[0] && stateRes[0].result;
              if (state && !state.painted) {
                shot.pageWarning = state.hiddenByCss
                  ? 'Page content is hidden by CSS (display:none on body); the screenshot may be blank, but the DOM is intact.'
                  : 'Page is not laid out (its window is in the background), so this image is likely blank. ' +
                    'page_snapshot and page_read read the page without focus. Last resort for a ' +
                    'picture: tabs action="activate", then capture again; it brings the tab to the ' +
                    'front of the user\'s window.';
              }
            } catch (e) { /* privileged page or race; skip the probe */ }
          }
          // Last-resort blank detection: check the pixels themselves.
          if (!shot.pageWarning && shot.dataUrl) {
            try {
              const uniform = await imageIsUniform(shot.dataUrl);
              if (uniform) {
                shot.pageWarning = 'The captured image is a single uniform color (' + uniform + '); ' +
                  'likely a blank frame, not real page content. If its window is in the background, ' +
                  'page_snapshot and page_read read the page without focus. Last resort for a ' +
                  'picture: tabs action="activate", then capture again; it brings the tab to the ' +
                  'front of the user\'s window.';
              }
            } catch (e) { /* best-effort pixel probe */ }
          }
          sendResponse(id, action, shot);
        } catch (captureError) {
          sendError(id, captureError.code || 'CAPTURE_ERROR', `Failed to capture screenshot: ${captureError.message}`);
        }
        break;

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown tabs action: ${action}`);
    }
  } catch (error) {
    if (error.code) { sendError(id, error.code, error.message); return; }
    // A file:// open fails with the browser's own "Illegal URL", which says
    // nothing about whether a tab appeared anyway. Measure it before answering.
    const fileMsg = await fileUrlRefusal(data && data.url, 'open a tab on', error.message);
    if (fileMsg) sendError(id, 'FILE_URL_OPEN_REFUSED', fileMsg);
    else sendError(id, 'API_ERROR', `Tabs API error: ${error.message}`);
  }
}

// Helper function to get current tab URL
async function getCurrentTabUrl(tabId) {
  try {
    const tab = await browser.tabs.get(tabId);
    return tab.url;
  } catch (error) {
    return "Unknown URL";
  }
}

// Helper function to send message with retry logic for content script
async function sendMessageWithRetry(tabId, message, maxRetries = 5, delayMs = 2000) {
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      const tab = await browser.tabs.get(tabId);
      if (!tab) {
        throw new Error('Tab not found');
      }
      if (tab.url && isPrivilegedUrl(tab.url)) {
        throw new Error('Cannot access content script on system pages');
      }
      const result = await browser.tabs.sendMessage(tabId, message);
      return result;
    } catch (error) {
      if (attempt === maxRetries) {
        throw new Error(`Content script not available after ${maxRetries} attempts: ${error.message}`);
      }
      await new Promise(resolve => setTimeout(resolve, delayMs));
    }
  }
}

// --- Page automation helpers (extension-chromium/automation.js) --------------
// The automation toolkit is a SHARED, platform-agnostic file (byte-identical to the
// gecko copy). MV3 scripting.executeScript cannot run code STRINGS, so the gecko
// "fetch automation.js text + concat an invoke() call + tabs.executeScript({code})"
// pattern is replaced by a two-step, string-free injection (design D4):
//   1. inject automation.js as a FILE into the ISOLATED world (self-guarding, so
//      re-injection is idempotent; files-injection needs no web_accessible_resources);
//   2. invoke via a func: (m,a)=>window.__browserplugAuto.invoke(m,a). The Promise returned
//      by invoke() IS awaited, so async methods come back in one call; same as gecko.
// Refs persist because __browserplugAuto lives on the ISOLATED-world window for the frame's
// lifetime, exactly like gecko's isolated content-script context.

// Inject with a few quick retries (transient mid-navigation frame absence). Hard
// failures (bad tab/frame, privileged URL) break early. Final failure carries a
// distinct INJECT_FAILED status. `injection` is a full scripting.executeScript arg.
// A file:// tab is NOT a privileged page; content scripts can run there, but
// only while the per-extension local-file opt-in is granted. Without this branch
// that failure comes back wearing the generic injection wording, whose own
// suggested diagnosis ("the tab may be a privileged page") is simply WRONG for an
// ordinary local HTML file; and the agent concludes the capability is absent and
// builds a workaround for something that was one toggle away. Returns the
// specific message, or null when file access is on (or unknown), so the generic
// message still covers the genuinely-privileged and mid-navigation cases.
async function fileAccessInjectHint(tabId) {
  if (tabId === undefined || tabId === null) return null;
  let url = '';
  try {
    const tab = await browser.tabs.get(tabId);
    url = (tab && tab.url) || '';
  } catch (e) {
    return null;
  }
  if (!/^file:\/\//i.test(url)) return null;
  let state = null;
  try { state = await getFileAccessState(); } catch (e) { return null; }
  if (!state || state.can_read_files !== false) return null;
  return 'local file (file://) access is DISABLED for this extension, so no content script could be ' +
    'injected into ' + url.split(/[?#]/)[0] + '. This is a per-user SETTING, not a missing capability: ' +
    'turn it on at chrome://extensions > BrowserPlug > Details > "Allow access to file URLs" (the BrowserPlug ' +
    'popup shows the same switch under "Local file (file://) access"), then retry; page_read / ' +
    'page_snapshot / page_act all work on a file:// tab once it is granted. To check before trying: ' +
    'server_info reports it as extension_file_access.can_read_opened_file_tab. (Chrome DOES let BrowserPlug ' +
    'OPEN a file:// URL with tabs/navigate; measured 2026-08-03; so only READING the page is gated ' +
    'by this toggle; on Firefox the opening half is a hard platform rule instead.)';
}

async function executeScriptWithRetry(injection, opts = {}) {
  const attempts = opts.attempts || 4;
  const delayMs = opts.delayMs || 150;
  let lastErr = null;
  let used = 0;
  for (let i = 1; i <= attempts; i++) {
    try {
      return await browser.scripting.executeScript(injection);
    } catch (e) {
      lastErr = e;
      used = i;
      const msg = String((e && e.message) || e);
      // Chromium hard-failure strings; don't burn retries on them.
      if (/No tab with id|Invalid tab ID|Cannot access|chrome:\/\/|extensions gallery|cannot be scripted|Missing host permission|No frame with id|Frame with id .* not found|The extensions gallery cannot be scripted/i.test(msg)) break;
      if (i < attempts) await new Promise(r => setTimeout(r, delayMs * i));
    }
  }
  const fileHint = await fileAccessInjectHint(injection && injection.target && injection.target.tabId);
  if (fileHint) {
    const fe = new Error(fileHint);
    fe.code = 'FILE_ACCESS_DISABLED';
    throw fe;
  }
  const lastMsg = String((lastErr && lastErr.message) || lastErr);
  // A refused host permission is a permanent answer for this frame, never a navigation
  // race: the old "retry" text sent agents back to a call that cannot succeed.
  const tail = /Missing host permission/i.test(lastMsg)
    ? '. The extension is not allowed into that frame (it is from an origin the extension cannot ' +
      'reach, or the tab may be a privileged page), so retrying cannot succeed. Target another ' +
      'frame_id (page_snapshot lists them) or the top document (omit frame_id). ' +
      '(page_screenshot / requests work on a privileged page; page_read / page_eval do not).'
    : '. The tab may have been navigating; retry; if it persists the tab may be a privileged page ' +
      '(page_screenshot / requests work there; page_read / page_eval do not).';
  const e = new Error('content script injection failed after ' + (used || attempts) + ' attempt(s): ' +
    lastMsg + tail);
  e.code = 'INJECT_FAILED';
  throw e;
}

// Inject the toolkit (idempotent) then invoke a method.
// ---- snapshot ref staleness (documentId) ------------------------------------------
// A ref like "e5" is minted by snapshot into the toolkit's per-document map, and the
// toolkit dies with its document. After a navigation the ref is simply absent, and the
// failure came back as an ordinary "element not found" — byte-identical to the report for
// a button that was removed from a page that never navigated. The right next move differs
// completely between those two (re-snapshot, versus stop looking for that element), so
// collapsing them costs the agent the one fact it needed.
//
// documentId is what separates them; frameId cannot, because ids are reused and a
// navigation swaps the document underneath the same frameId. Record the document each
// snapshot was taken in, and compare ONLY when a ref-targeted call has already failed:
// the diagnosis costs nothing on the path where the click worked, the same rule the
// click-state confirmation follows.
//
// SW-memory only, like the observability ring buffers: an eviction loses the baseline and
// the next stale ref falls back to the generic "not found". Degraded, never wrong.
const SNAPSHOT_DOCS = new Map();   // `${tabId}:${frameId}` -> documentId at snapshot time

// Only a real toolkit envelope counts. A transport-level failure carries no nodesAtStart
// and says nothing about API availability; treating it as "unsupported" would latch a
// false negative.
function noteContentDocIdSupport(out) {
  if (out && typeof out === 'object' && out.nodesAtStart !== undefined) {
    CONTENT_DOC_ID_SUPPORT = !!out.docId;
  }
}

function snapshotDocKey(tabId, frameId) {
  return tabId + ':' + (frameId === undefined || frameId === null ? 0 : frameId);
}

function targetsARef(methodArgs) {
  return /^e\d+$/.test(String((methodArgs && methodArgs.target) || ''));
}

// Rewrite a failed ref lookup into a staleness report, but only when the document
// demonstrably changed. A missing docId on either side means UNKNOWN and leaves the
// original error alone; claiming staleness on a page that never navigated would send an
// agent re-snapshotting in a loop while the real problem went unnamed.
function annotateStaleRef(result, methodArgs, prevDocId) {
  if (!result || result.ok !== false || !targetsARef(methodArgs)) return result;
  if (!prevDocId || !result.docId || result.docId === prevDocId) return result;
  result.code = 'REFS_STALE';
  result.staleRef = String(methodArgs.target);
  // Keep the literal phrase "stale ref": it is the agent-facing contract for this
  // failure, and test_stale_element_ref_after_reload_is_named asserts
  // it. This message SHARPENS that contract (documentId proves the document changed, where
  // the older message inferred it) and must not quietly drop its wording.
  result.error = 'stale ref "' + result.staleRef + '": it was minted by page_snapshot in a ' +
    'document that no longer exists — the page navigated or reloaded since. Element refs do ' +
    'not survive a navigation. Re-run page_snapshot for fresh refs; a CSS selector or ' +
    'accessible-name target survives one.';
  return result;
}

function forgetSnapshotDocs(tabId) {
  const prefix = tabId + ':';
  for (const key of Array.from(SNAPSHOT_DOCS.keys())) {
    if (key.startsWith(prefix)) SNAPSHOT_DOCS.delete(key);
  }
}

// Read a frame's current documentId from the BACKGROUND, via webNavigation.
//
// This is the whole Blink divergence in one function. Gecko reads the id inside the
// injected script, because Firefox 153 gives content scripts runtime.getDocumentId. Chrome
// has no such method in ANY context reachable from a page: the ISOLATED world lacks it
// (measured — the hello probe reports documentId:false on Chrome Canary 150), MAIN is the
// page realm, and USER_SCRIPT gets no extension APIs unless configureWorld enables
// messaging. What Chrome DOES expose is documentId as a field on webNavigation frames,
// which only the background can ask for. Hence this, rather than an injection.
//
// Called LAZILY — on a snapshot, and on a ref-targeted failure — so an ordinary click pays
// nothing. Any failure is null = unknown, which callers must never read as "changed".
async function documentIdForFrame(tabId, frameId) {
  try {
    const frames = await browser.webNavigation.getAllFrames({ tabId });
    const want = (frameId === undefined || frameId === null) ? 0 : frameId;
    const hit = (frames || []).find(f => f.frameId === want);
    return (hit && hit.documentId) || null;
  } catch (e) {
    return null;
  }
}

// Did the document change WHILE the script ran? Both ids must be known; one missing means
// unknown, and unknown must not be reported as a change. This is strictly sharper than the
// tab.status probe next to it: status catches an in-flight load, but a navigation that
// starts and finishes inside the eval's own window leaves status "complete" on both sides
// while the result describes a document that no longer exists.
function evalDocumentChanged(before, after) {
  return !!(before && after && before !== after);
}

// ---- wait predicates on Blink -------------------------------------------------------
// The toolkit's ISOLATED world runs under the extension's own CSP (script-src 'self'),
// so eval there is refused ("Evaluating a string as JavaScript violates the following
// Content Security Policy directive"). That was as true of the eval the shared toolkit
// used to carry, so a wait predicate never worked on Chromium: it reported the CSP text
// at the deadline. The one route that runs a string is the one page_eval takes,
// userScripts.execute in the USER_SCRIPT world (configured with 'unsafe-eval' below),
// behind the same "Allow user scripts" toggle. So the predicate is evaluated THERE, just
// before the toolkit's own check, and the outcome rides into the invoking func, which
// hands the toolkit a predicateFn that replays it. The toolkit keeps the policy (match,
// gone, unknown, parse failure) in one place for both engines; the (0, eval) text lives
// in this code string as it does in page_eval's wrapper. With the toggle off the outcome
// names the toggle, reported as predicateError while the wait's other conditions still
// decide. Not headlessly testable past the toggle, like page_eval; the refstale twin
// drives it with the transport faked.
function waitPredicateToggleMessage() {
  return 'wait predicates need the "Allow user scripts" toggle enabled for BrowserPlug at ' +
    'chrome://extensions/?id=' + browser.runtime.id + ' (the one-time switch page_eval ' +
    'needs; it cannot be enabled programmatically). A wait on a CSS target, text or url ' +
    'needs no toggle.';
}

async function evalWaitPredicate(tabId, predicate, frameId) {
  if (!userScriptsAvailable()) return { error: waitPredicateToggleMessage() };
  await ensureUserScriptWorld();
  const target = { tabId };
  if (frameId !== undefined && frameId !== null) target.frameIds = [frameId];
  const code = '(function(){try{return {truth: !!(0, eval)(' + JSON.stringify(predicate) + ')};}' +
    'catch(e){return (e && e.name === "SyntaxError") ? {syntaxError: String(e.message || e)} : ' +
    '{error: String((e && e.message) || e)};}})()';
  try {
    const results = await browser.userScripts.execute({ target, js: [{ code }], world: 'USER_SCRIPT' });
    const r = results && results[0];
    if (r && r.error) return { error: String(r.error) };
    if (r && r.result && typeof r.result === 'object') return r.result;
    return { error: 'no result from the predicate injection' };
  } catch (e) {
    return { error: String((e && e.message) || e) };
  }
}

async function runAutomation(tabId, method, methodArgs, frameId) {
  const target = { tabId };
  if (frameId !== undefined && frameId !== null) target.frameIds = [frameId];
  // 1. ensure the toolkit is present (self-guards; cheap idempotent re-injection).
  await executeScriptWithRetry({ target, world: 'ISOLATED', files: ['automation.js'] });
  // 2. invoke.
  const key = snapshotDocKey(tabId, frameId);
  const prevDocId = SNAPSHOT_DOCS.get(key);
  // A wait predicate is evaluated beforehand (above) and its outcome replayed to the
  // toolkit through predicateFn; scripting.executeScript cannot carry a function in
  // args, and the shared toolkit never evaluates a string (addons-linter DANGEROUS_EVAL
  // on the gecko submission). gecko inlines the evaluation into its code string instead
  // (compiledArgs).
  const outcome = (methodArgs && typeof methodArgs.predicate === 'string')
    ? await evalWaitPredicate(tabId, methodArgs.predicate, frameId) : null;
  const results = await executeScriptWithRetry({
    target, world: 'ISOLATED',
    func: (m, a, o) => {
      if (o) {
        a.predicateFn = function () {
          if (o.syntaxError) throw new SyntaxError(o.syntaxError);
          if (o.error) throw new Error(o.error);
          return o.truth;
        };
      }
      return window.__browserplugAuto.invoke(m, a);
    },
    args: [method, methodArgs || {}, outcome]
  });
  const out = (results && results.length > 0 && results[0])
    ? results[0].result
    : { ok: false, error: 'no result from injected automation' };
  // A snapshot re-bases the record; anything else is a candidate for the staleness
  // diagnosis. The baseline is NOT updated on failure, so every stale ref in a batch
  // gets the same clear answer until a fresh snapshot replaces it.
  noteContentDocIdSupport(out);
  // On Blink the envelope never carries a docId (no content-script API), so fall back to
  // the background reader. Both branches stay lazy: a snapshot arms the baseline, and only
  // an already-failed ref lookup pays for the diagnosis.
  if (method === 'snapshot') {
    const docId = (out && out.docId) || await documentIdForFrame(tabId, frameId);
    if (docId) SNAPSHOT_DOCS.set(key, docId);
  } else if (out && out.ok === false && prevDocId && targetsARef(methodArgs)) {
    if (!out.docId) out.docId = await documentIdForFrame(tabId, frameId);
    annotateStaleRef(out, methodArgs, prevDocId);
  }
  return out;
}
// ---- end automation invoke path ----------------------------------------------------
// Slice anchor. refstale.test.mjs takes everything from the staleness header down to here
// so it can drive the real runAutomation, not just the helpers it calls. That distinction
// is the point: the helpers were green while the ARMING code around them was untested,
// which is exactly how the StreamFilter capture path stayed dead through 343 passing JS
// tests. Invariant E checks this anchor in BOTH trees.

// --- page_eval (content.execute) via chrome.userScripts ----------------------
// scripting.executeScript cannot run arbitrary code STRINGS. The User Scripts API can:
// world:'USER_SCRIPT' (CSP-exempt isolated world, matches gecko's (0,eval) in the
// isolated CS) or world:'MAIN' (page realm, page-CSP-bound, matches gecko's
// wrappedJSObject.eval). It is GATED on the per-extension "Allow user scripts" toggle
// (Chrome 138+) and accessing chrome.userScripts THROWS when the toggle is off; so we
// feature-detect with try/catch and return a structured EVAL_UNAVAILABLE naming the
// toggle. NO debugger fallback in v1 (design D4).
function userScriptsAvailable() {
  try {
    return !!(chrome.userScripts && typeof chrome.userScripts.execute === 'function');
  } catch (e) {
    return false; // accessing the namespace throws when the toggle is disabled
  }
}

// The USER_SCRIPT world needs 'unsafe-eval' so the isolated wrapper's (0,eval) runs.
// Configured once, lazily; best-effort (the default world may already permit eval).
let _userScriptsWorldConfigured = false;
async function ensureUserScriptWorld() {
  if (_userScriptsWorldConfigured) return;
  try {
    await chrome.userScripts.configureWorld({
      csp: "script-src 'self' 'unsafe-eval' 'unsafe-inline'",
      messaging: false
    });
    _userScriptsWorldConfigured = true;
  } catch (e) { /* best-effort */ }
}

// Capture the current site's Service Worker registrations for the loaded top-level
// document. Chromium ISOLATED world does not see page globals, so this reads
// navigator.serviceWorker from the page's MAIN world via a func (no userScripts
// needed; a fixed func, not a code string). Best-effort + time-boxed; never throws.
async function captureServiceWorkers(tabId) {
  const fn = async function () {
    try {
      const swc = navigator.serviceWorker;
      if (!swc) return null;
      const snap = o => o ? { scriptURL: o.scriptURL, state: o.state } : null;
      const regs = await Promise.race([
        swc.getRegistrations(),
        new Promise(res => setTimeout(() => res(null), 1000))
      ]);
      if (!regs) return { timedOut: true };
      const out = [];
      for (let i = 0; i < regs.length; i++) {
        const r = regs[i];
        out.push({
          scope: r.scope,
          active: snap(r.active),
          waiting: snap(r.waiting),
          installing: snap(r.installing),
          updateViaCache: r.updateViaCache
        });
      }
      const ctrl = swc.controller;
      return { controlled: !!ctrl, controller: snap(ctrl), registrations: out };
    } catch (e) { return { error: String((e && e.message) || e) }; }
  };
  try {
    const res = await browser.scripting.executeScript({ target: { tabId }, world: 'MAIN', func: fn });
    return (res && res[0] && res[0].result) || null;
  } catch (e) {
    return null; // privileged page / injection blocked → nothing to report
  }
}

// ---- content.read window + diagnostics --------------------------------------
// Two failures, one code path.
//
// The slice was never clamped. page_read(offset=3500) on a 149-char page
// answered `[markdown] chars 3500-3500 of 149 (no content extracted — try
// format="text" or a scope selector)`: the response ALREADY knew the length, still
// rendered a range starting past its own stated end, and then blamed the format. An
// out-of-range offset now returns the FINAL window and SAYS it did, and maxChars<=0
// is a LENGTH PROBE; sizing a document before reading it costs no body.
//
// A 290-char read of a live-chat modal on a page of 400 product cards and a
// 7-char read of a genuinely 7-char page were byte-identical in the response. So
// every read now carries the DOCUMENT's own measurements alongside the extraction;
// element count and visible-text length; and the server flags the discrepancy
// instead of returning a near-empty result as a clean success.
//
// Lifted OUT of the switch so it stays testable: this arming glue (the probe flag,
// the clamp, the metrics threading) is exactly the shape that has gone silently dead
// in this codebase before while every helper it called stayed green.
//
// The in-page halves cannot be ONE shared source string across the trees: here
// scripting.executeScript takes a FUNCTION, and an MV3 ISOLATED-world script is bound
// by the extension CSP, which forbids 'unsafe-eval'; so new Function(sharedSrc)
// throws. Duplication is forced; the twin JS tests are the compensating control.

// Extracted chars below which a read is "thin" and worth the extra document probes.
const READ_THIN_CHARS = 1000;

// The extraction cache. A markdown read of a long article spends about 2 s in Defuddle,
// and paging through it used to extract the whole document again for every offset. The
// cache lives in the page's isolated world for the life of the document; an entry is
// keyed by format, scope and URL (each frame has its own world) and any DOM mutation, a
// stylesheet load, a resize or READ_CACHE_TTL_MS drops it. A page that changed is
// extracted again, live, never served from the old copy; when the new length differs from
// the last extraction under the key, the read carries contentChanged, so an agent paging
// with offsets learns the text under them moved.
//
// Nothing watches the page while an extraction runs. Defuddle rewrites srcset on the live
// page's images, and an observer still attached for an older entry (another key, or one
// past the TTL) would take those writes for a page change and drop the entry being made.
// pause() first hands the records already queued to the handler, so a change that landed
// before the read still drops what it invalidates.
//
// One kind of mutation spares some entries: a childList record that only adds or removes
// inert scripts (__fxScriptChurn) keeps the whole-page markdown entries (scriptBlind) and
// drops the rest. Other extensions and tag managers append such scripts long after load.
// The rule is narrow because scripts do reach the output: html includes them, innerText
// follows the page's CSS, and a scope selector can match one. Defuddle reads script text
// (JSON-LD metadata, math/* scripts, MathJax and KaTeX detection, React's $RC streaming
// calls, site extractors' inline data), looks scripts up by id, type and src, counts
// elements by class for the author, and walks the elements around the first h1 before it
// removes scripts. Check that list again when vendor/defuddle.js changes.
//
// An inert script (__fxInertScript) is external, empty and executable, carries no
// attribute Defuddle selects on and is not MathJax or KaTeX. Outside head it must end its
// parent, so it never comes between two elements: Defuddle looks at the elements just
// before and after a heading. The helpers capture window and document because the
// observer and listeners call back after the driver returns.
const READ_CACHE_TTL_MS = 120000;
const READ_CACHE_MAX_CHARS = 2000000;

// maxChars <= 0 means LENGTH PROBE, not "give me the default". undefined/null keeps
// the 20000 default, so protocol-level callers are untouched; 1..49 still floors at
// 50. That makes the parameter non-monotone at the boundary on purpose: 0 is a mode
// flag, not the bottom of a range.
function readWindowParams(data) {
  const rawMax = data.maxChars;
  const probeOnly = rawMax !== undefined && rawMax !== null && Number(rawMax) <= 0;
  const maxChars = probeOnly ? 0 : Math.max(50, Number(rawMax) > 0 ? Number(rawMax) : 20000);
  return { probeOnly: probeOnly, maxChars: maxChars, offset: Math.max(0, Number(data.offset) || 0) };
}

async function runContentRead(readTabId, data) {
  const format = data.format || 'markdown';
  const p = readWindowParams(data);
  const target = { tabId: readTabId };
  if (data.frameId !== undefined && data.frameId !== null) target.frameIds = [data.frameId];

  if (format === 'markdown') {
    // One injection when the page already has Defuddle (or a cached extraction): the driver
    // answers needsDefuddle instead of a read when it has neither, and only then is the
    // bundle injected and the driver run again without that exit.
    // A probe still RUNS Defuddle: what it saves is context, not CPU; the length
    // has to be the length of the very extraction the next call would return.
    const markdownDriver = function (scope, offset, maxChars, probeOnly, thin, needLib, cacheTtl, cacheMax) {
      // The extraction cache (READ_CACHE_TTL_MS has the rules). A func cannot close over
      // anything, so these helpers are written out in each driver, identically: whichever
      // driver runs first in a document builds the object both use. Bump rev when the
      // object's methods change: a read then replaces the object an older build left in
      // the same world, which outlives a reload of the extension.
      function __fxReadCache() {
        var w = window, d = document, c = w.__browserplugReadCache;
        if (c && c.doc === d) {
          if (c.rev === 1) return c;
          try { c.drop(); } catch (e) {}
        }
        c = { doc: d, rev: 1, entries: {}, totals: {}, watching: false };
        c.unwatch = function () {
          c.watching = false;
          try { c.obs.disconnect(); } catch (e) {}
          w.removeEventListener('resize', c.drop);
          d.removeEventListener('load', c.onLoad, true);
        };
        c.drop = function () {
          c.entries = {};
          if (c.watching) c.unwatch();
        };
        c.onLoad = function (ev) { var t = ev && ev.target; if (t && t.tagName === 'LINK') c.drop(); };
        c.onMutations = function (recs) {
          for (var i = 0; i < recs.length; i++) if (!__fxScriptChurn(recs[i], d)) { c.drop(); return; }
          for (var k in c.entries) if (!c.entries[k].scriptBlind) delete c.entries[k];
        };
        c.watch = function () {
          if (c.watching) return;
          try {
            c.obs = c.obs || new MutationObserver(c.onMutations);
            c.obs.observe(d, { subtree: true, childList: true, attributes: true, characterData: true });
            w.addEventListener('resize', c.drop);
            d.addEventListener('load', c.onLoad, true);
            c.watching = true;
          } catch (e) { c.entries = {}; }
        };
        c.pause = function () {
          if (!c.watching) return;
          var queued = [];
          try { queued = c.obs.takeRecords(); } catch (e) {}
          c.unwatch();
          if (queued.length) c.onMutations(queued);
        };
        c.resume = function () { if (Object.keys(c.entries).length) c.watch(); };
        w.__browserplugReadCache = c;
        return c;
      }
      function __fxInertScript(n) {
        if (!n || n.nodeName !== 'SCRIPT' || n.firstChild) return false;
        var ok = ['src', 'type', 'async', 'defer', 'crossorigin', 'integrity', 'nonce', 'referrerpolicy',
                  'charset', 'nomodule', 'fetchpriority', 'blocking'];
        for (var i = 0; i < n.attributes.length; i++) if (ok.indexOf(n.attributes[i].name) < 0) return false;
        var src = n.getAttribute('src') || '', type = (n.getAttribute('type') || '').trim().toLowerCase();
        if (!src || /mathjax|katex/i.test(src)) return false;
        return !type || type === 'module' || /^(text|application)\/(x-)?(java|ecma)script$/.test(type);
      }
      function __fxScriptChurn(rec, d) {
        if (rec.type !== 'childList') return false;
        var p = rec.target, i;
        if (p !== d.head && p !== d.body && p !== d.documentElement) return false;
        if (p !== d.head) for (var s = rec.nextSibling; s; s = s.nextSibling) if (s.nodeType === 1) return false;
        for (i = 0; i < rec.addedNodes.length; i++) if (!__fxInertScript(rec.addedNodes[i])) return false;
        for (i = 0; i < rec.removedNodes.length; i++) if (!__fxInertScript(rec.removedNodes[i])) return false;
        return true;
      }
      function __fxCacheGet(c, key) {
        var e = c.entries[key];
        if (e && Date.now() - e.at < cacheTtl) return e;
        delete c.entries[key];
        return null;
      }
      function __fxCachePut(c, key, entry) {
        var prev = c.totals[key];
        c.totals[key] = entry.content.length;
        if (entry.content.length <= cacheMax) {
          entry.at = Date.now();
          c.entries[key] = entry;
          c.watch();
        }
        return (prev !== undefined && prev !== entry.content.length) ? { previousTotal: prev } : undefined;
      }
      function __fxPageState() {
        var b = document.body, r = b ? b.getBoundingClientRect() : null;
        var w = r ? Math.round(r.width) : 0, h = r ? Math.round(r.height) : 0;
        var hid = false;
        try {
          var bcs = b ? getComputedStyle(b) : null, hcs = getComputedStyle(document.documentElement);
          hid = !!((bcs && bcs.display === 'none') || (hcs && hcs.display === 'none') || (bcs && bcs.visibility === 'hidden'));
        } catch (e) {}
        return { focused: document.hasFocus(), visibility: document.visibilityState, bodyW: w, bodyH: h,
                 hiddenByCss: hid, painted: (w > 0 && h > 0 && !hid), readyState: document.readyState };
      }
      // Clamp semantics, exactly:
      //   offset >= totalChars (and both > 0) -> return the FINAL window, flagged.
      //   offset === totalChars DOES clamp. A legitimate paginator never asks for it
      //     (the server only offers `continue with offset=end` while end < total), so
      //     this cannot break paging; and "exactly at the end" is the case an agent
      //     reaches by guessing, which is precisely when an empty window misleads.
      //   totalChars === 0 -> NO clamp. There is no tail to return, and the
      //     empty-read diagnosis owns that case.
      function __fxReadWindow(content, offset, maxChars, probe) {
        var total = content.length;
        var out = { totalChars: total, offset: offset, content: '' };
        if (probe) { out.probe = true; out.offset = 0; return out; }
        var eff = offset;
        if (offset > 0 && total > 0 && offset >= total) {
          eff = Math.max(0, total - maxChars);
          out.offsetClamped = true;
          out.requestedOffset = offset;
        }
        out.offset = eff;
        out.content = content.slice(eff, eff + maxChars);
        return out;
      }
      // What the DOCUMENT holds, as opposed to what the extraction returned. nodes
      // and bodyChars ride every read (~40 bytes); the three expensive fields only
      // ride a thin or probe read. Every probe is try/caught: a measurement must
      // never be able to fail the read it is measuring.
      function __fxDocMetrics(bodyText, wantDetail) {
        var m = { nodes: 0, bodyChars: 0 };
        try { m.nodes = document.getElementsByTagName('*').length; } catch (e) {}
        var t = bodyText;
        if (t === null || t === undefined) {
          try { t = (document.body && document.body.innerText) || ''; } catch (e) { t = ''; }
        }
        m.bodyChars = t.length;
        if (wantDetail) {
          try { m.iframes = document.getElementsByTagName('iframe').length; } catch (e) {}
          try { m.dialogs = document.querySelectorAll('[aria-modal="true"],dialog[open]').length; } catch (e) {}
          m.sample = t.replace(/\s+/g, ' ').trim().slice(0, 300);
        }
        return m;
      }
      try {
        // Defuddle treats a contentSelector matching NOTHING as "no selector" and
        // falls back to whole-document extraction; refuse an unresolvable scope
        // rather than silently returning the whole page as the scope.
        if (scope) {
          var __fxHit = null;
          try { __fxHit = document.querySelector(scope); }
          catch (e) { return { ok: false, error: 'invalid CSS selector for scope: ' + scope + ' — ' + (e && e.message || e), pageState: __fxPageState() }; }
          if (!__fxHit) { return { ok: false, error: 'scope selector matched nothing: ' + scope + ' — the page was NOT read whole instead; re-run page_snapshot to find the current container, or drop scope to read the page.', pageState: __fxPageState() }; }
        }
        var __fxC = __fxReadCache(), __fxKey = 'markdown|' + (scope || '') + '|' + location.href;
        var r = __fxCacheGet(__fxC, __fxKey), cached = !!r, changed;
        if (!r) {
          if (needLib && typeof Defuddle === 'undefined') return { needsDefuddle: true };
          // useAsync:false keeps Defuddle's async extractors off: they fetch() third-party
          // APIs (some with credentials) from inside the user's logged-in page. parse() is
          // synchronous today; the flag still holds if this ever moves to parseAsync(), and
          // resume() would then have to wait for it.
          var opts = { url: location.href, markdown: true, useAsync: false };
          if (scope) opts.contentSelector = scope;
          __fxC.pause();
          try { var d = new Defuddle(document, opts).parse(); } finally { __fxC.resume(); }
          r = { content: d.content || '', title: d.title, site: d.site, author: d.author,
                published: d.published, wordCount: d.wordCount, scriptBlind: !scope };
          changed = __fxCachePut(__fxC, __fxKey, r);
        }
        var content = r.content;
        var w = __fxReadWindow(content, offset, maxChars, probeOnly);
        var m = __fxDocMetrics(null, probeOnly || w.totalChars < thin);
        return { ok: true, format: 'markdown', url: location.href, title: r.title || document.title,
                 site: r.site || undefined, author: r.author || undefined, published: r.published || undefined,
                 wordCount: r.wordCount || undefined, totalChars: w.totalChars, content: w.content,
                 offset: w.offset, requestedOffset: w.requestedOffset, offsetClamped: w.offsetClamped,
                 probe: w.probe, docMetrics: m, pageState: __fxPageState(),
                 cached: cached || undefined, contentChanged: changed };
      } catch (e) {
        // The Defuddle-failure fallback is the branch a CSP or a bundle crash lands
        // on, so it needs the clamp and the metrics just as much; and it gets
        // bodyChars for free, having already computed the text.
        var t = (document.body && document.body.innerText) || '';
        var w2 = __fxReadWindow(t, offset, maxChars, probeOnly);
        var m2 = __fxDocMetrics(t, probeOnly || w2.totalChars < thin);
        return { ok: true, format: 'text', fallback: 'defuddle failed: ' + String(e && e.message || e),
                 url: location.href, title: document.title, totalChars: w2.totalChars, content: w2.content,
                 offset: w2.offset, requestedOffset: w2.requestedOffset, offsetClamped: w2.offsetClamped,
                 probe: w2.probe, docMetrics: m2, pageState: __fxPageState() };
      }
    };
    const run = async (needLib) => {
      const res = await browser.scripting.executeScript({
        target, world: 'ISOLATED', func: markdownDriver,
        args: [data.scope ? String(data.scope) : null, p.offset, p.maxChars, p.probeOnly, READ_THIN_CHARS,
               needLib, READ_CACHE_TTL_MS, READ_CACHE_MAX_CHARS]
      });
      return res && res[0] && res[0].result;
    };
    let r = await run(true);
    if (r && r.needsDefuddle) {
      // defuddle.js (UMD -> global Defuddle) injected into the ISOLATED world.
      await browser.scripting.executeScript({ target, world: 'ISOLATED', files: ['vendor/defuddle.js'] });
      r = await run(false);
    }
    return r || { ok: false, error: 'no result from read driver' };
  }

  const textHtmlDriver = function (scope, offset, maxChars, format, probeOnly, thin, cacheTtl, cacheMax) {
    // The same cache helpers as markdownDriver's, line for line.
    function __fxReadCache() {
      var w = window, d = document, c = w.__browserplugReadCache;
      if (c && c.doc === d) {
        if (c.rev === 1) return c;
        try { c.drop(); } catch (e) {}
      }
      c = { doc: d, rev: 1, entries: {}, totals: {}, watching: false };
      c.unwatch = function () {
        c.watching = false;
        try { c.obs.disconnect(); } catch (e) {}
        w.removeEventListener('resize', c.drop);
        d.removeEventListener('load', c.onLoad, true);
      };
      c.drop = function () {
        c.entries = {};
        if (c.watching) c.unwatch();
      };
      c.onLoad = function (ev) { var t = ev && ev.target; if (t && t.tagName === 'LINK') c.drop(); };
      c.onMutations = function (recs) {
        for (var i = 0; i < recs.length; i++) if (!__fxScriptChurn(recs[i], d)) { c.drop(); return; }
        for (var k in c.entries) if (!c.entries[k].scriptBlind) delete c.entries[k];
      };
      c.watch = function () {
        if (c.watching) return;
        try {
          c.obs = c.obs || new MutationObserver(c.onMutations);
          c.obs.observe(d, { subtree: true, childList: true, attributes: true, characterData: true });
          w.addEventListener('resize', c.drop);
          d.addEventListener('load', c.onLoad, true);
          c.watching = true;
        } catch (e) { c.entries = {}; }
      };
      c.pause = function () {
        if (!c.watching) return;
        var queued = [];
        try { queued = c.obs.takeRecords(); } catch (e) {}
        c.unwatch();
        if (queued.length) c.onMutations(queued);
      };
      c.resume = function () { if (Object.keys(c.entries).length) c.watch(); };
      w.__browserplugReadCache = c;
      return c;
    }
    function __fxInertScript(n) {
      if (!n || n.nodeName !== 'SCRIPT' || n.firstChild) return false;
      var ok = ['src', 'type', 'async', 'defer', 'crossorigin', 'integrity', 'nonce', 'referrerpolicy',
                'charset', 'nomodule', 'fetchpriority', 'blocking'];
      for (var i = 0; i < n.attributes.length; i++) if (ok.indexOf(n.attributes[i].name) < 0) return false;
      var src = n.getAttribute('src') || '', type = (n.getAttribute('type') || '').trim().toLowerCase();
      if (!src || /mathjax|katex/i.test(src)) return false;
      return !type || type === 'module' || /^(text|application)\/(x-)?(java|ecma)script$/.test(type);
    }
    function __fxScriptChurn(rec, d) {
      if (rec.type !== 'childList') return false;
      var p = rec.target, i;
      if (p !== d.head && p !== d.body && p !== d.documentElement) return false;
      if (p !== d.head) for (var s = rec.nextSibling; s; s = s.nextSibling) if (s.nodeType === 1) return false;
      for (i = 0; i < rec.addedNodes.length; i++) if (!__fxInertScript(rec.addedNodes[i])) return false;
      for (i = 0; i < rec.removedNodes.length; i++) if (!__fxInertScript(rec.removedNodes[i])) return false;
      return true;
    }
    function __fxCacheGet(c, key) {
      var e = c.entries[key];
      if (e && Date.now() - e.at < cacheTtl) return e;
      delete c.entries[key];
      return null;
    }
    function __fxCachePut(c, key, entry) {
      var prev = c.totals[key];
      c.totals[key] = entry.content.length;
      if (entry.content.length <= cacheMax) {
        entry.at = Date.now();
        c.entries[key] = entry;
        c.watch();
      }
      return (prev !== undefined && prev !== entry.content.length) ? { previousTotal: prev } : undefined;
    }
    function __fxPageState() {
      var b = document.body, r = b ? b.getBoundingClientRect() : null;
      var w = r ? Math.round(r.width) : 0, h = r ? Math.round(r.height) : 0;
      var hid = false;
      try {
        var bcs = b ? getComputedStyle(b) : null, hcs = getComputedStyle(document.documentElement);
        hid = !!((bcs && bcs.display === 'none') || (hcs && hcs.display === 'none') || (bcs && bcs.visibility === 'hidden'));
      } catch (e) {}
      return { focused: document.hasFocus(), visibility: document.visibilityState, bodyW: w, bodyH: h,
               hiddenByCss: hid, painted: (w > 0 && h > 0 && !hid), readyState: document.readyState };
    }
    function __fxReadWindow(content, offset, maxChars, probe) {
      var total = content.length;
      var out = { totalChars: total, offset: offset, content: '' };
      if (probe) { out.probe = true; out.offset = 0; return out; }
      var eff = offset;
      if (offset > 0 && total > 0 && offset >= total) {
        eff = Math.max(0, total - maxChars);
        out.offsetClamped = true;
        out.requestedOffset = offset;
      }
      out.offset = eff;
      out.content = content.slice(eff, eff + maxChars);
      return out;
    }
    function __fxDocMetrics(bodyText, wantDetail) {
      var m = { nodes: 0, bodyChars: 0 };
      try { m.nodes = document.getElementsByTagName('*').length; } catch (e) {}
      var t = bodyText;
      if (t === null || t === undefined) {
        try { t = (document.body && document.body.innerText) || ''; } catch (e) { t = ''; }
      }
      m.bodyChars = t.length;
      if (wantDetail) {
        try { m.iframes = document.getElementsByTagName('iframe').length; } catch (e) {}
        try { m.dialogs = document.querySelectorAll('[aria-modal="true"],dialog[open]').length; } catch (e) {}
        m.sample = t.replace(/\s+/g, ' ').trim().slice(0, 300);
      }
      return m;
    }
    var el = scope ? document.querySelector(scope) : (format === 'html' ? document.documentElement : document.body);
    if (!el) return { ok: false, error: 'scope selector matched nothing: ' + (scope || ''), pageState: __fxPageState() };
    var __fxC = __fxReadCache(), __fxKey = format + '|' + (scope || '') + '|' + location.href;
    var hit = __fxCacheGet(__fxC, __fxKey), cached = !!hit, changed;
    if (!hit) {
      __fxC.pause();
      try { hit = { content: format === 'html' ? el.outerHTML : (el.innerText || '') }; } finally { __fxC.resume(); }
      changed = __fxCachePut(__fxC, __fxKey, hit);
    }
    var content = hit.content;
    var w = __fxReadWindow(content, offset, maxChars, probeOnly);
    // Reuse the string we already have as bodyChars ONLY when it really is the
    // body's own text: with a scope it is a subtree's, and with html it is markup.
    var reuse = (format === 'text' && !scope) ? content : null;
    var m = __fxDocMetrics(reuse, probeOnly || w.totalChars < thin);
    return { ok: true, format: format, url: location.href, title: document.title,
             totalChars: w.totalChars, content: w.content, offset: w.offset,
             requestedOffset: w.requestedOffset, offsetClamped: w.offsetClamped, probe: w.probe,
             docMetrics: m, pageState: __fxPageState(), cached: cached || undefined,
             contentChanged: changed };
  };
  const res = await browser.scripting.executeScript({
    target, world: 'ISOLATED', func: textHtmlDriver,
    args: [data.scope ? String(data.scope) : null, p.offset, p.maxChars, format, p.probeOnly, READ_THIN_CHARS,
           READ_CACHE_TTL_MS, READ_CACHE_MAX_CHARS]
  });
  return (res && res[0] && res[0].result) || { ok: false, error: 'no result from read driver' };
}

// ---- content.act step deadline ----------------------------------------------
// A click that navigates can leave scripting.executeScript's promise UNRESOLVED;
// the document the toolkit was injected into is torn down mid-call, so nothing
// ever comes back. Without a deadline the whole batch then blows through the
// SERVER's request budget and the agent gets a bare "Request timed out after 22.1
// seconds": indistinguishable from "nothing happened", when in fact the click may
// well have worked (a lazy "load more" that landed a beat late), or navigated;
// once, to the WRONG page entirely. So every step races a deadline, and a step
// that overruns it still reports what the tab looks like NOW: url/title, whether a
// navigation committed since the step began, and the network/console digest. The
// batch always answers; "timed out" stops meaning "no state change".
const ACT_STEP_TIMEOUT_MS = 12000;   // per non-wait step, unless the step names its own
const ACT_MIN_STEP_MS = 1200;        // below this there is no point starting a step
const ACT_TIMED_OUT = { __actTimedOut: true };

// The after-measure's quiescence poll. waitForSettle ends on NETWORK quiet, and
// a "load more" that appends via a plain setTimeout fires no request at all; so the
// settle can end several hundred ms before the rows land, and a number read there is
// the number from BEFORE the click did its work. Poll until two consecutive readings
// agree, bounded, and never inside the last ACT_MEASURE_MIN_BUDGET_MS of the batch:
// the measurement must never be what blows the budget.
const ACT_MEASURE_QUIET_MS = 200;
const ACT_MEASURE_MAX_MS = 1500;
const ACT_MEASURE_MIN_BUDGET_MS = 2000;

// Budget for one step: its own timeout_ms (or the default), never more than what
// is left of the whole-batch deadline the server handed down.
function actStepBudget(deadlineAt, requestedMs) {
  const asked = Number(requestedMs);
  let budget = (isFinite(asked) && asked > 0) ? asked : ACT_STEP_TIMEOUT_MS;
  if (deadlineAt !== null && deadlineAt !== undefined) {
    budget = Math.min(budget, deadlineAt - Date.now());
  }
  return budget;
}

// actWait honours its own timeout, but it must not honour it PAST the batch
// deadline; a 60s wait inside a 20s budget would starve the report.
function actClampWait(step, deadlineAt) {
  if (deadlineAt === null || deadlineAt === undefined) return step;
  const requested = Number(step.timeout_ms || step.timeoutMs || 10000);
  const remaining = deadlineAt - Date.now();
  if (!(remaining < requested)) return step;
  const clamped = Object.assign({}, step);
  clamped.timeout_ms = Math.max(1000, remaining);
  delete clamped.timeoutMs;
  return clamped;
}

async function actTimeoutReport(tabId, t0, budgetMs, ctx) {
  const kind = (ctx && ctx.kind) || 'step';
  const out = {
    ok: false,
    timedOut: true,
    actionTimedOut: true,
    elapsedMs: Date.now() - t0,
    requestedTimeoutMs: Math.round(budgetMs)
  };
  let tab = null;
  try { tab = await browser.tabs.get(tabId); } catch (e) { out.tabClosed = true; }
  if (tab) {
    out.urlAfter = tab.url;
    out.titleAfter = tab.title;
    out.tabStatus = tab.status;
  }
  const nav = TAB_NAV.get(tabId) || {};
  out.navigated = !!(nav.lastCommitted && nav.lastCommitted > t0 - 100);
  if (ctx && ctx.urlBefore != null) {
    out.urlBefore = ctx.urlBefore;
    out.urlChanged = !!(tab && tab.url !== ctx.urlBefore);
  }
  const net = netDigestSince(tabId, t0);
  if (net.requests > 0) out.network = net;
  // Through ctx for the same reason as ctx.measure below. A page stuck enough to overrun
  // the step will not answer either; consoleFlushTimedOut then says so.
  if (ctx && typeof ctx.flushConsole === 'function') {
    let flushed = 'timeout';
    try { flushed = await ctx.flushConsole(); } catch (e) { flushed = 'timeout'; }
    if (flushed === 'timeout') out.consoleFlushTimedOut = true;
  }
  const cons = consoleErrorsSince(tabId, t0);
  if (cons.errors > 0) out.consoleErrors = cons;

  // "timed out" must not also mean "and I have no idea whether it worked".
  // Reached through ctx, NEVER by calling the page directly: the twin suites stub this
  // function with no runAutomation in scope, and a report that can throw is no report.
  if (ctx && typeof ctx.measure === 'function' && typeof ctx.nodesBefore === 'number') {
    let after = null;
    try { after = await ctx.measure(); } catch (e) { after = null; }
    if (after && typeof after.nodes === 'number') {
      out.nodesBefore = ctx.nodesBefore;
      out.nodesAfter = after.nodes;
      out.nodeDelta = after.nodes - ctx.nodesBefore;
      const zipped = actZipCounts(ctx.countsBefore, after.counts);
      if (zipped) out.counts = zipped;
    } else {
      out.nodeProbeFailed = true;
    }
  }

  let verdict;
  if (out.tabClosed) {
    verdict = 'The tab was closed while the step was running.';
  } else if (out.navigated || out.urlChanged) {
    verdict = 'The page DID navigate while it ran (now at ' + (out.urlAfter || '?') +
      (out.titleAfter ? ' - "' + String(out.titleAfter).slice(0, 80) + '"' : '') +
      '), so the action almost certainly took effect: re-read the page rather than retrying blindly, ' +
      'and CHECK WHERE YOU LANDED - a timed-out click can commit a navigation you did not intend.';
  } else if (typeof out.nodeDelta === 'number' && out.nodeDelta !== 0) {
    verdict = 'No navigation, but the page changed by ' + out.nodeDelta +
      ' element node(s) while it ran, so the action DID take effect. Re-read the page ' +
      'rather than retrying - a retry would apply it twice.';
  } else if (net.requests > 0) {
    verdict = 'No navigation, but ' + net.requests + ' network request(s) started while it ran, so it ' +
      'may still have taken effect (a lazy "load more" often lands just after the settle window). ' +
      'Re-read the page before retrying.';
  } else {
    verdict = 'No navigation and no network activity was observed, so it probably had no effect - ' +
      'retrying is reasonable.';
    if (typeof out.nodeDelta === 'number') {
      verdict += ' The DOM did not grow or shrink either (0 element nodes).';
    }
  }
  out.error = 'the ' + kind + ' step did not report back within ' +
    (Math.round(budgetMs / 100) / 10) + 's. ' + verdict;
  return out;
}

// Race one step against its budget. The step's own promise is NOT cancellable
// (there is no such thing for executeScript), so it is left to settle on its own
// with its rejection swallowed; a late failure belongs to a call nobody is
// waiting for any more.
async function runActStepWithDeadline(tabId, run, budgetMs, t0, ctx) {
  let timer = null;
  const guard = new Promise(function (resolve) {
    timer = setTimeout(function () { resolve(ACT_TIMED_OUT); }, Math.max(500, budgetMs));
  });
  let outcome;
  try {
    const running = run();
    if (running && typeof running.catch === 'function') running.catch(function () { /* orphaned */ });
    outcome = await Promise.race([running, guard]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
  if (outcome !== ACT_TIMED_OUT) return outcome;
  return await actTimeoutReport(tabId, t0, budgetMs, ctx);
}

// Batched interaction steps, orchestrated HERE (not in-page) so a step that triggers
// navigation doesn't strand the rest of the sequence, and waits always evaluate
// against the current document. Each state-changing step gets a network/console
// outcome digest. Lifted OUT of the switch on purpose: the arming glue; budget
// threading, notRunSteps, the urlBefore capture; is exactly the kind of code that
// has gone silently dead in this codebase before while every helper it calls stayed
// green, so it needs to be sliceable and unit-testable in its own right.
const ACT_METHOD_BY_KIND = {
  click: 'click', fill: 'fill', type: 'fill', select: 'select', check: 'fill',
  press: 'press', hover: 'hover', scroll: 'scroll'
};

// "OK: 1/1 steps completed" answers "did the mechanism run", which is never the
// question; 64 of 101 page_act calls in the corpus were followed by a probe whose only
// purpose was to discover what the action had done. So each step now reports a QUANTITY.
// The split of responsibility is forced by the injection boundary: the PAGE can only say
// "how many elements are here right now", it cannot pair a before with an after across
// two injections. The pairing and the TIMING live here.
//
// One measurement of what the agent said it cares about. Best-effort by construction; a
// privileged page, a mid-navigation document or a torn-down frame must cost the step
// NOTHING, so every failure is a null and the step simply carries no delta. A measurement
// must never be able to fail a step.
async function actMeasure(tabId, want, frameId) {
  try {
    const args = {};
    if (want && want.count) args.count = want.count;
    if (want && want.within !== undefined && want.within !== null) args.within = want.within;
    if (want && want.watch) {
      // inspect scopes COUNTS by args.within but resolves each WATCH entry on its own
      // args, so the scope has to be pushed down per entry; exactly as the after-watch
      // call does. Without it a scoped step's before/after watch rows would describe
      // elements from two different panels.
      args.watch = (want.within === undefined || want.within === null) ? want.watch
        : (Array.isArray(want.watch) ? want.watch : [want.watch]).map(function (w) {
            return typeof w === 'string' ? { target: w, within: want.within }
              : Object.assign({ within: want.within }, w);
          });
    }
    const r = await runAutomation(tabId, 'inspect', args, frameId);
    if (!r || typeof r !== 'object') return null;
    if (typeof r.nodesAtEnd !== 'number' && !r.counts) return null;
    return { nodes: r.nodesAtEnd, counts: r.counts, watched: r.watched };
  } catch (e) {
    return null;
  }
}

function actCountsSame(a, b) {
  if (!a && !b) return true;
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i].count !== b[i].count) return false;
  }
  return true;
}

// Poll until two consecutive readings agree, and say plainly when they never did: a
// lower bound the agent KNOWS is a lower bound beats a confident wrong exact.
function actMeasureMoved(before, after) {
  if (!before || !after) return true;
  if (typeof before.nodes === 'number' && typeof after.nodes === 'number' &&
      before.nodes !== after.nodes) return true;
  return !actCountsSame(before.counts, after.counts);
}

async function actMeasureSettled(tabId, want, frameId, deadlineAt, baseline) {
  let last = await actMeasure(tabId, want, frameId);
  if (!last) return { measure: null, settled: false };
  const until = Date.now() + ACT_MEASURE_MAX_MS;
  let ranOut = true;
  let quiet = false;
  while (Date.now() < until) {
    if (deadlineAt !== null && deadlineAt !== undefined &&
        deadlineAt - Date.now() < ACT_MEASURE_MIN_BUDGET_MS) { ranOut = false; break; }
    await new Promise(r => setTimeout(r, ACT_MEASURE_QUIET_MS));
    const next = await actMeasure(tabId, want, frameId);
    if (!next) { ranOut = false; break; }
    quiet = next.nodes === last.nodes && actCountsSame(next.counts, last.counts);
    last = next;
    // Two equal samples prove the page is STABLE, not that the effect has landed yet.
    // Stopping on them while the page still matches the PRE-step baseline is how a click
    // whose rows arrive 900ms later gets reported as "+0, settled"; a confident wrong
    // answer, and the very defect the step delta exists to remove. So a page that has
    // not moved is watched for the whole window; only one that already MOVED may stop
    // early.
    if (quiet && actMeasureMoved(baseline, last)) return { measure: last, settled: true };
  }
  // Out of window. Settled ONLY if the last two readings agreed: a page that never stopped
  // moving (a carousel, a ticker) has given a LOWER BOUND, not an answer, and must say so.
  // A page that sat still for the whole window genuinely did not change; also an answer.
  return { measure: last, settled: ranOut && quiet };
}

// Pair the two readings BY SELECTOR STRING, never by index: two lists built from the
// same input array cannot drift today, but the records extractor's own bug was exactly
// an index zip that was safe until it wasn't.
function actZipCounts(beforeCounts, afterCounts) {
  if (!afterCounts) return undefined;
  const prior = new Map();
  (beforeCounts || []).forEach(function (c) { prior.set(c.selector, c); });
  return afterCounts.map(function (c) {
    const out = { selector: c.selector };
    const b = prior.get(c.selector);
    if (b && typeof b.count === 'number') out.before = b.count;
    if (typeof c.count === 'number') out.after = c.count;
    if (c.error) out.error = c.error;
    else if (b && b.error) out.error = b.error;
    if (typeof out.before === 'number' && typeof out.after === 'number') {
      out.delta = out.after - out.before;
    }
    return out;
  });
}

// The three outcomes that have to be distinguishable, as ONE token. "matched nothing" is
// already ok:false upstream; this classifies what happened once something DID match.
// 'rerendered' is the case domChanged could never express: the same rows replaced, no
// content gained; which on a listing page reads identically to a successful load-more.
function actEffect(res) {
  if (res.urlChanged || res.dialogOpened || res.expandedChanged || res.navigated) return 'changed';
  if (res.counts && res.counts.some(function (c) {
    return typeof c.delta === 'number' && c.delta !== 0;
  })) return 'changed';
  if (typeof res.nodeDelta === 'number') {
    if (res.nodeDelta !== 0) return 'changed';
    if (res.nodesAdded > 0 || res.nodesRemoved > 0) return 'rerendered';
    return 'none';
  }
  if (res.domChanged) return 'changed';
  return 'unmeasured';
}

async function runActSteps(actTabId, data, frameId) {
  const steps = Array.isArray(data.steps) ? data.steps : [];
  const results = [];
  let allOk = true;
  // The server hands down the budget it will itself wait for, so this loop can
  // always answer BEFORE that budget expires. Absent (an older server), the per-step
  // defaults still bound every step.
  const askedBudget = Number(data.budgetMs);
  const batchDeadline = (isFinite(askedBudget) && askedBudget > 0)
    ? Date.now() + askedBudget : null;
  let batchTimedOut = false;
  const notRunSteps = [];
  for (let i = 0; i < steps.length; i++) {
    const step = steps[i] || {};
    const kind = step.action || 'click';
    const t0 = Date.now();
    const stepBudget = actStepBudget(batchDeadline, step.timeout_ms || step.timeoutMs);
    if (batchDeadline !== null && stepBudget < ACT_MIN_STEP_MS) {
      // Out of budget: say which steps never ran rather than starting one we
      // cannot wait for (which would come back as a bare transport timeout).
      for (let j = i; j < steps.length; j++) {
        notRunSteps.push({ step: j, stepAction: (steps[j] || {}).action || 'click' });
      }
      batchTimedOut = true;
      allOk = false;
      break;
    }
    let urlBefore = null;
    try { urlBefore = (await browser.tabs.get(actTabId)).url; } catch (e) { /* best-effort */ }
    // Only `count`/`watch` need a BEFORE reading of their own. A plain step does
    // not, because its own automation result already carries nodesAtStart; so the extra
    // injection is paid only by the steps that asked for it.
    const measureWant = (step.count || step.watch)
      ? { count: step.count, watch: step.watch, within: step.within } : null;
    let beforeMeasure = null;
    if (measureWant) beforeMeasure = await actMeasure(actTabId, measureWant, frameId);
    // Capped by what is left of the batch, so the flush is never what overruns it.
    const flushConsole = () => flushConsoleCapture(actTabId,
      actStepBudget(batchDeadline, CONSOLE_FLUSH_TIMEOUT_MS));
    let res;
    try {
      if (kind === 'wait') {
        const clamped = actClampWait(step, batchDeadline);
        res = await actWait(actTabId, clamped, frameId);
        if (clamped !== step && res && res.ok === false) res.timeoutClampedToMs = clamped.timeout_ms;
      } else if (ACT_METHOD_BY_KIND[kind]) {
        res = await runActStepWithDeadline(
          actTabId,
          () => runAutomation(actTabId, ACT_METHOD_BY_KIND[kind], step, frameId),
          stepBudget, t0, {
            kind: kind,
            urlBefore: urlBefore,
            nodesBefore: beforeMeasure ? beforeMeasure.nodes : undefined,
            countsBefore: beforeMeasure ? beforeMeasure.counts : undefined,
            measure: () => actMeasure(actTabId, measureWant || {}, frameId),
            flushConsole: flushConsole
          });
      } else {
        res = { ok: false, error: 'unknown step action: ' + kind + ' (use click|fill|select|check|press|hover|scroll|wait)' };
      }
    } catch (e) {
      res = { ok: false, error: e.message };
    }
    res = res || { ok: false, error: 'no result' };
    if (res.ok && (kind === 'click' || step.settle_ms != null)) {
      const settleMs = step.settle_ms != null ? step.settle_ms : 600;
      if (settleMs > 0) {
        const settle = await waitForSettle(actTabId, {
          timeoutMs: settleMs + 1500, quietMs: Math.min(settleMs, 500), sinceTs: t0
        });
        res.settled = settle.settled;
        if (settle.url) res.urlAfter = settle.url;
        if (settle.navCommitted) res.navigated = true;
      }
    }
    if (res.ok && ACT_METHOD_BY_KIND[kind]) {
      const net = netDigestSince(actTabId, t0);
      if (net.requests > 0) res.network = net;
      // An error the step's own handler raised is still in the tab's batch at this point.
      if ((await flushConsole()) === 'timeout') res.consoleFlushTimedOut = true;
      const cons = consoleErrorsSince(actTabId, t0);
      if (cons.errors > 0) res.consoleErrors = cons;
    }
    // What did this step actually CHANGE? Measured AFTER the settle window,
    // because that is where a lazy "load more" lands. A `wait` step is excluded unless
    // it named a count of its own: it has already polled the page to a known state, and
    // charging it two more injections to re-learn that would be pure latency.
    if (res.ok !== false && (ACT_METHOD_BY_KIND[kind] || measureWant)) {
      const nodesBefore = (beforeMeasure && typeof beforeMeasure.nodes === 'number')
        ? beforeMeasure.nodes
        : (typeof res.nodesAtStart === 'number' ? res.nodesAtStart : undefined);
      const m = await actMeasureSettled(actTabId, measureWant || {}, frameId, batchDeadline,
                                        beforeMeasure);
      if (m.measure) {
        if (typeof nodesBefore === 'number' && typeof m.measure.nodes === 'number') {
          res.nodesBefore = nodesBefore;
          res.nodesAfter = m.measure.nodes;
          res.nodeDelta = m.measure.nodes - nodesBefore;
        }
        const zipped = actZipCounts(beforeMeasure ? beforeMeasure.counts : null, m.measure.counts);
        if (zipped) res.counts = zipped;
        res.measureSettled = m.settled;
      } else if (measureWant || kind === 'click') {
        res.measureFailed = true;
      }
      if (beforeMeasure && beforeMeasure.watched && beforeMeasure.watched.length) {
        res.watchedBefore = beforeMeasure.watched;
      }
      res.effect = actEffect(res);
    }
    // Plumbing, not contract: the raw carriers stay off the wire.
    delete res.nodesAtStart;
    delete res.nodesAtEnd;
    if (step.watch && res.ok !== false) {
      try {
        const watch = step.within == null ? step.watch
          : (Array.isArray(step.watch) ? step.watch : [step.watch]).map(w =>
              typeof w === 'string'
                ? { target: w, within: step.within }
                : Object.assign({ within: step.within }, w));
        const w = await runAutomation(actTabId, 'inspect', { watch: watch }, frameId);
        if (w && w.watched) res.watched = w.watched;
      } catch (e) { /* best-effort */ }
    }
    res.step = i;
    res.stepAction = kind;
    results.push(res);
    if (res.ok === false && !step.continue_on_error) { allOk = false; break; }
    if (res.ok === false) allOk = false;
  }
  return {
    ok: allOk,
    completedSteps: results.filter(r => r.ok !== false).length,
    totalSteps: steps.length,
    batchTimedOut: batchTimedOut || undefined,
    notRunSteps: notRunSteps.length ? notRunSteps : undefined,
    results: results
  };
}

// Content handlers
async function handleContentAction(id, action, data) {
  try {
    switch (action) {
      case 'content.text':
      case 'content.get_text':
        const textResult = await sendMessageWithRetry(data.tabId, {
          action: 'extractText'
        });
        sendResponse(id, action, {
          text: textResult.text,
          title: textResult.title,
          url: textResult.url
        });
        break;

      case 'content.html':
      case 'content.get_html':
        const htmlResult = await sendMessageWithRetry(data.tabId, {
          action: 'extractHTML'
        });
        sendResponse(id, action, {
          html: htmlResult.html,
          title: htmlResult.title,
          url: htmlResult.url
        });
        break;

      case 'content.execute':
      case 'content.execute_script':
        try {
          const execTabId = await resolveTabId(data, { requireContent: true });
          // page_eval runs arbitrary code STRINGS, which scripting.executeScript cannot
          // do; it needs chrome.userScripts, gated on the "Allow user scripts" toggle.
          if (!userScriptsAvailable()) {
            const e = new Error('page_eval needs the "Allow user scripts" toggle enabled for ' +
              'BrowserPlug at chrome://extensions/?id=' + browser.runtime.id + ' — Chrome 138+ shows a ' +
              'per-extension "Allow user scripts" switch on the extension details page. Enable it, ' +
              'then retry. (This is a one-time user action Chrome requires before an extension may ' +
              'run arbitrary page code; it cannot be enabled programmatically.)');
            e.code = 'EVAL_UNAVAILABLE';
            throw e;
          }

          const world = data.world === 'main' ? 'MAIN' : 'USER_SCRIPT';
          if (world === 'USER_SCRIPT') await ensureUserScriptWorld();

          let wrapped;
          if (data.world === 'main') {
            // Page realm (page-CSP-bound), matching gecko's wrappedJSObject.eval. A strict
            // page CSP blocks eval → EvalError, reported distinctly.
            wrapped = '(function(){' +
              'try {' +
              '  var completion = (0, eval)(' + JSON.stringify(data.script) + ');' +
              '  return Promise.resolve(completion).then(function(v){' +
              '    var out = { readyState: document.readyState };' +
              '    if (v === undefined) { out.result = null; return out; }' +
              '    try { out.result = JSON.parse(JSON.stringify(v)); }' +
              '    catch (e) { out.result = String(v); }' +
              '    return out;' +
              '  });' +
              '} catch (e) {' +
              '  var msg = String(e && e.message || e);' +
              '  if (/call to eval|unsafe-eval|EvalError|CSP|Content Security Policy/i.test(msg)) {' +
              '    return { error: "main-world eval blocked by the page Content-Security-Policy: " + msg + ". Fallback: run with world=isolated and read page globals via a page_eval that returns them." };' +
              '  }' +
              '  return { error: msg };' +
              '}' +
              '})()';
          } else {
            // USER_SCRIPT world (CSP-exempt), matching gecko's isolated (0,eval). Indirect
            // eval keeps global-scope var persistence + completion-value semantics.
            wrapped = '(function(){' +
              'var completion = (0, eval)(' + JSON.stringify(data.script) + ');' +
              'return Promise.resolve(completion).then(function(v){' +
              '  return { __browserplugEval: 1, value: v === undefined ? null : v, readyState: document.readyState };' +
              '});' +
              '})()';
          }

          const target = { tabId: execTabId };
          if (data.frameId !== undefined && data.frameId !== null) target.frameIds = [data.frameId];
          else if (data.allFrames) target.allFrames = true;

          // Mid-navigation the injection binds to a transient placeholder document
          // (right URL, readyState "complete", but empty). tab.status reads "loading"
          // for the whole in-flight window; check on both sides of the eval.
          let tabLoadingBefore = false;
          try { tabLoadingBefore = (await browser.tabs.get(execTabId)).status === 'loading'; } catch (e) {}
          // Out-of-band, because neither eval world can read its own documentId here.
          const evalDocBefore = await documentIdForFrame(execTabId, data.frameId);
          const injResults = await browser.userScripts.execute({ target, js: [{ code: wrapped }], world });
          const evalDocAfter = await documentIdForFrame(execTabId, data.frameId);
          let tabLoadingAfter = false;
          try { tabLoadingAfter = (await browser.tabs.get(execTabId)).status === 'loading'; } catch (e) {}

          // A raw InjectionResult.error is an uncaught throw (the isolated wrapper does
          // not catch; the main wrapper returns {error} in result instead).
          const errored = (injResults || []).find(r => r && r.error);
          if (errored && !data.allFrames) {
            sendError(id, 'SCRIPT_ERROR', String(errored.error));
            break;
          }

          let docReadyState = null;
          const unwrapEval = (r) => {
            if (r && typeof r === 'object' && r.__browserplugEval) {
              if (!docReadyState) docReadyState = r.readyState;
              return r.value;
            }
            return r;
          };
          let result = data.allFrames
            ? (injResults || []).map(r => unwrapEval(r.result))
            : unwrapEval(injResults && injResults.length > 0 && injResults[0] ? injResults[0].result : null);
          if (data.world === 'main' && result && typeof result === 'object' && !data.allFrames) {
            if (result.error) {
              sendError(id, 'SCRIPT_ERROR', result.error);
              break;
            }
            if (result.readyState && !docReadyState) docReadyState = result.readyState;
            result = result.result;
          }
          sendResponse(id, action, {
            result: result,
            url: await getCurrentTabUrl(execTabId),
            readyState: docReadyState || undefined,
            tabLoading: (tabLoadingBefore || tabLoadingAfter) || undefined,
            // The document was REPLACED while the script ran, so the value describes a
            // page that is already gone. Omitted (not false) when either id is unknown.
            documentChanged: evalDocumentChanged(evalDocBefore, evalDocAfter) || undefined
          });
        } catch (scriptError) {
          sendError(id, scriptError.code || 'SCRIPT_ERROR', `Script execution failed: ${scriptError.message}`);
        }
        break;

      case 'content.read': {
        // Unified page reading: markdown (defuddle main-content extraction),
        // text (innerText), html (outerHTML). Injection-based (executeScript),
        // NOT content-script messaging; so it cannot hit "Receiving end does
        // not exist" and works on tabs loaded before the extension.
        const readTabId = await resolveTabId(data, { requireContent: true });
        try {
          sendResponse(id, action, await runContentRead(readTabId, data));
        } catch (readErr) {
          // content.read injects directly (not through executeScriptWithRetry), so
          // it needs its own file:// diagnosis or it reports the raw platform error.
          const fileHint = await fileAccessInjectHint(readTabId);
          if (fileHint) sendError(id, 'FILE_ACCESS_DISABLED', fileHint);
          else sendError(id, readErr.code || 'SCRIPT_ERROR', `page read failed: ${readErr.message}`);
        }
        break;
      }

      case 'content.act': {
        const actTabId = await resolveTabId(data, { requireContent: true });
        sendResponse(id, action, await runActSteps(actTabId, data, data.frameId));
        break;
      }

      case 'content.snapshot':
      case 'content.click':
      case 'content.fill':
      case 'content.select':
      case 'content.wait_for':
      case 'content.page_state': {
        const method = action.split('.')[1]; // snapshot|click|fill|select|wait_for|page_state
        try {
          const autoTabId = await resolveTabId(data, { requireContent: true });
          const result = await runAutomation(autoTabId, method, data, data.frameId);
          sendResponse(id, action, result || { ok: false, error: 'no result' });
        } catch (autoErr) {
          sendError(id, autoErr.code || 'AUTOMATION_ERROR', `page ${method} failed: ${autoErr.message}`);
        }
        break;
      }

      case 'content.frames': {
        try {
          const framesTabId = await resolveTabId(data);
          const frames = await browser.webNavigation.getAllFrames({ tabId: framesTabId });
          sendResponse(id, action, {
            frames: (frames || []).map(f => ({
              frameId: f.frameId,
              parentFrameId: f.parentFrameId,
              url: f.url,
              errorOccurred: f.errorOccurred || false
            }))
          });
        } catch (framesErr) {
          sendError(id, 'API_ERROR', `Could not list frames: ${framesErr.message} (requires webNavigation permission)`);
        }
        break;
      }

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown content action: ${action}`);
    }
  } catch (error) {
    if (error.code) {
      sendError(id, error.code, error.message);
    } else {
      sendError(id, 'API_ERROR', `Content API error: ${error.message}`);
    }
  }
}

// ---- wait="dom": the DOM stops changing ---------------------------------------------
// navigate(wait="dom") returns once the document has gone DOM_STABLE_MS without a
// mutation, for at most DOM_STABLE_MAX_MS after the load event: the rule
// chrome-devtools-mcp applies after its own load event (a MutationObserver on the body:
// childList, subtree, attributes; 100 ms; 3 s). The observer only records when the last
// mutation happened. The clock runs here, because a background tab throttles its own
// timers to about one a second and the page's setTimeout would stretch every 100 ms wait.
// The observer starts at the commit (armDomWatch), so the quiet time already behind the
// load event counts; DOM_POST_LOAD_MIN_MS is the least the wait runs after the load
// event, for a fetch the page starts there.
// The probe is a function, not gecko's code string: MV3 scripting.executeScript runs
// files and functions only.
const DOM_STABLE_MS = 100;
const DOM_POST_LOAD_MIN_MS = 50;
const DOM_STABLE_MAX_MS = 3000;
const DOM_ARM_MS = 5000;
// (stop, stableMs, notBefore, early): reads the watch, installing it first when the
// document has none. It disconnects itself when asked to stop, on an error, and once the
// DOM has been quiet for stableMs at or after notBefore, so the last probe is also the
// stop. `early` only installs, and does nothing before the document has a root element.
function domWatchProbe(stop, stableMs, notBefore, early) {
  var s = window.__browserplugDomWatch;
  if (early && !(document.body || document.documentElement)) return null;
  if (!s || s.doc !== document) {
    s = { doc: document, last: Date.now(), records: 0 };
    try {
      s.obs = new MutationObserver(function (list) { s.records += list.length; s.last = Date.now(); });
      s.obs.observe(document.body || document.documentElement,
        { childList: true, subtree: true, attributes: true });
    } catch (e) { s.error = String((e && e.message) || e); }
    window.__browserplugDomWatch = s;
  }
  var out = { sinceLast: Date.now() - s.last, records: s.records, error: s.error };
  if (early) return out;
  out.stable = !out.error && stableMs != null && out.sinceLast >= stableMs && Date.now() >= notBefore;
  if (stop || out.stable || out.error) {
    try { if (s.obs) s.obs.disconnect(); } catch (e) {}
    window.__browserplugDomWatch = null;
  }
  return out;
}

async function probeDomWatch(tabId, stop, stableMs, notBefore, early) {
  const res = await browser.scripting.executeScript({
    target: { tabId, frameIds: [0] }, world: 'ISOLATED', func: domWatchProbe,
    args: [!!stop, stableMs == null ? null : stableMs, notBefore || 0, !!early],
    injectImmediately: !!early
  });
  return (res && res[0] && res[0].result) || null;
}

// A wait="dom" navigation arms its tab; the tab's next top-level commit, within DOM_ARM_MS,
// starts the observer. A failed injection costs nothing: the first probe installs it late.
const DOM_ARMED = new Map(); // tabId -> time the arming lapses
function armDomWatch(d) {
  if (d.frameId !== 0) return;
  const until = DOM_ARMED.get(d.tabId);
  if (!until || Date.now() > until) { DOM_ARMED.delete(d.tabId); return; }
  probeDomWatch(d.tabId, false, null, 0, true).catch(() => { /* privileged page or gone */ });
}
browser.webNavigation.onCommitted.addListener(armDomWatch);

// {stable, ms, records, error}: stable once DOM_STABLE_MS passed with no mutation; not
// stable when budgetMs ran out first, or when the page could not be observed (`error`).
// The probe that finds the DOM stable, runs out the budget or fails also removes the watch.
async function waitForStableDom(tabId, budgetMs) {
  const start = Date.now();
  const until = start + Math.max(0, budgetMs);
  const notBefore = start + DOM_POST_LOAD_MIN_MS;
  let probe = null;
  while (true) {
    const last = Date.now() >= until;
    try { probe = await probeDomWatch(tabId, last, DOM_STABLE_MS, notBefore); } catch (e) {
      return { stable: false, ms: Date.now() - start, error: String((e && e.message) || e) };
    }
    if (!probe || probe.error) {
      return { stable: false, ms: Date.now() - start,
               error: (probe && probe.error) || 'no result from the DOM probe' };
    }
    if (probe.stable || last) {
      return { stable: !!probe.stable, ms: Date.now() - start, records: probe.records };
    }
    const now = Date.now();
    await new Promise(r => setTimeout(r, Math.min(until - now,
      Math.max(DOM_STABLE_MS - probe.sinceLast, notBefore - now, 1))));
  }
}

// ---- navigation landing diagnostics -----------------------------------------
// The requested-vs-landed check itself lives in the SERVER (it is pure string
// work over two URLs). Two of the facts it needs cannot be derived from tab.url,
// so they are collected here:
//   httpStatus; the MAIN DOCUMENT's response status. A 404 or a 403 with a full
//     HTML body renders as a perfectly normal page, and the status was previously
//     buried in the request digest among every failed sub-resource. Scanning
//     BACKWARDS over main_frame entries yields the FINAL status under either
//     redirect model (see the engine note below).
//   urlBefore; where the tab was before the navigation was issued, so "the
//     navigation never took effect and the tab is still where it was" stops being
//     indistinguishable from "it arrived at exactly that URL".
// chromium: ONE requestId spans the whole redirect chain, so the hops sit on a
// single entry and onCompleted supplies that entry's final status.
// Sub-resources are excluded by the type check on purpose: a 500 from an analytics
// beacon is not the document's status, and reporting it as one would be worse than
// reporting nothing.
// Lifted OUT of the switch so it is sliceable and unit-testable; this file has been
// bitten before by arming glue with no unit coverage, which stays dead while every
// helper it calls is green.
function mainFrameOutcome(tabId, t0) {
  const lg = TAB_REQUESTS.get(tabId) || [];
  const mainReq = latestMainFrameRequest(tabId, t0);
  let errReq = null;
  const hops = [];
  for (let i = lg.length - 1; i >= 0; i--) {
    const rq = lg[i];
    if (rq.type !== 'main_frame' || rq.start < t0 - 50) continue;
    if (rq.error && !errReq) errReq = rq;
  }
  for (let i = 0; i < lg.length; i++) {
    const rq = lg[i];
    if (rq.type !== 'main_frame' || rq.start < t0 - 50) continue;
    if (rq.redirects && rq.redirects.length) hops.push(...rq.redirects);
  }
  const out = {};
  if (hops.length) out.redirects = hops.map(h => ({ to: h.to, status: h.status }));
  if (mainReq && mainReq.ip) out.dialedAddress = mainReq.ip;
  if (mainReq && typeof mainReq.status === 'number') out.httpStatus = mainReq.status;
  if (errReq && errReq.error) out.reqError = errReq.error;
  return out;
}

// The latest main-frame request since t0: the one that decides where the tab landed.
function latestMainFrameRequest(tabId, t0) {
  const lg = TAB_REQUESTS.get(tabId) || [];
  for (let i = lg.length - 1; i >= 0; i--) {
    if (lg[i].type === 'main_frame' && lg[i].start >= t0 - 50) return lg[i];
  }
  return null;
}

// Absent rather than wrong: a tab that closed, or an id that never existed, yields
// null and the server simply says nothing about where the tab was.
async function navUrlBefore(tabId) {
  try { return (await browser.tabs.get(tabId)).url || null; } catch (e) { return null; }
}

// A load aborted, in any spelling: gecko's webRequest names NS_BINDING_ABORTED, gecko's
// webNavigation gives its decimal nsresult, Chrome says net::ERR_ABORTED.
function isNavAbort(err) {
  return ['NS_BINDING_ABORTED', 'Error code ' + 0x804B0002, 'net::ERR_ABORTED'].includes(String(err));
}

// The URL identity the abort rules compare: canonical form, fragment dropped.
function navUrlKey(u) {
  try { const x = new URL(u); x.hash = ''; return x.href; } catch (e) { return String(u); }
}

// When a navigation replaces a load still in progress (a fresh tab's about:blank, a page
// still loading), webNavigation can report that load's abort after the new document has
// committed, so the abort outlives the commit that clears lastNavError. It names the
// replaced load's URL. It counts as the navigation's own only while it names one of
// `ownUrls`: the URL asked for, a redirect followed, the document committed. With none of
// those known nothing tells the two apart, and the abort stands.
// chromium: the late abort was measured on Firefox; here the same check covers net::ERR_ABORTED.
function abortOfReplacedLoad(navError, errorUrl, ownUrls) {
  const urls = (ownUrls || []).filter(Boolean);
  if (!isNavAbort(navError) || !errorUrl || !urls.length) return false;
  return !urls.map(navUrlKey).includes(navUrlKey(errorUrl));
}

// Firefox reports a navigation twice when it moves the tab to another content process:
// both processes send their web-progress events (WebNavigationContent.cpp: "This can fire
// twice when process switches are involved", Bugzilla 1732564). The process the tab leaves
// ends its own attempt at the SAME URL with NS_BINDING_ABORTED, and because each process
// has its own channel to the parent, that abort can arrive after the new process committed
// the document, even after the load finished. A document at the URL the abort names that
// finished loading since t0 (webNavigation.onCompleted) answers it: the navigation's load
// completed. A load that was stopped or replaced never completes (the abort is how it
// ends), so that abort is still reported.
// chromium: Chrome's webNavigation events come from the browser process alone, so the echo
// is not expected here; the rule is kept identical so both engines judge an abort alike.
function abortOfCompletedLoad(navError, errorUrl, nav, t0) {
  if (!isNavAbort(navError) || !errorUrl) return false;
  const key = navUrlKey(errorUrl);
  return (nav.events || []).some(e => e.ev === 'done' && e.at >= t0 - 50 && navUrlKey(e.url) === key);
}

// An abort of the very document the tab committed since t0, whose load has not been seen
// to complete yet. Only this abort can still turn out to be the echo above, so only this
// one is worth waiting a moment for (ABORT_ECHO_GRACE_MS).
function abortAwaitingCompletion(nav, t0) {
  return !!(nav.lastNavError && nav.lastNavErrorAt >= t0 - 50 && isNavAbort(nav.lastNavError)
    && nav.lastNavErrorUrl && nav.lastCommitted >= t0
    && navUrlKey(nav.lastNavErrorUrl) === navUrlKey(nav.lastCommittedUrl)
    && !abortOfCompletedLoad(nav.lastNavError, nav.lastNavErrorUrl, nav, t0));
}

// How long a settled navigate waits for that completion. The tab already reports the load
// complete, so the event is normally milliseconds away; without it the abort is reported.
const ABORT_ECHO_GRACE_MS = 1000;

// The net error of a navigation issued at t0. `target` is the URL it asked for: null for
// back and forward, which do not know theirs beforehand.
function navigationError(nav, t0, outcome, target) {
  const own = [target, nav.lastCommitted >= t0 ? nav.lastCommittedUrl : null,
    ...(outcome.redirects || []).map(h => h.to)];
  const err = nav.lastNavError;
  const fromNav = err && nav.lastNavErrorAt >= t0 - 50
    && !abortOfReplacedLoad(err, nav.lastNavErrorUrl, own)
    && !abortOfCompletedLoad(err, nav.lastNavErrorUrl, nav, t0) ? err : null;
  return fromNav || outcome.reqError || null;
}

// The tab's main-frame webNavigation events since t0, as ms after t0, for a report that
// carries a net error: the order they arrived in tells an echo from an abort that ended the load.
function navEventsSince(nav, t0) {
  return (nav.events || []).filter(e => e.at >= t0 - 50).map(e => {
    const out = { ev: e.ev, url: e.url, ms: e.at - t0 };
    if (e.error) out.error = e.error;
    return out;
  });
}

async function handleNavigationAction(id, action, data) {
  try {
    switch (action) {
      // Unified navigation with settle-by-default.
      case 'navigation.navigate': {
        const navTabId = await resolveTabId(data);
        const op = data.op || 'goto';
        // Refused before anything changes, the browser-wide proxy included.
        if (op === 'goto' && !data.url) { sendError(id, 'INVALID_PARAMETER', 'url is required for goto'); return; }
        // Chromium cannot route one tab: a non-direct proxy switches the whole browser,
        // before the navigation starts, and a refusal stops here (applyProxyField).
        const applied = await applyProxyField(id, data.proxy);
        if (applied.error) return;
        const urlBefore = await navUrlBefore(navTabId);
        const t0 = Date.now();
        const commitBefore = (TAB_NAV.get(navTabId) || {}).lastCommitted;
        { const ns = TAB_NAV.get(navTabId) || {};
          ns.lastNavIssuedAt = t0;
          ns.lastAttemptedUrl = (op === 'goto' ? data.url : op);
          TAB_NAV.set(navTabId, ns); }
        if (data.wait === 'dom') DOM_ARMED.set(navTabId, t0 + DOM_ARM_MS);
        if (op === 'goto') {
          await browser.tabs.update(navTabId, { url: data.url });
        } else if (op === 'back') {
          await browser.tabs.goBack(navTabId);
        } else if (op === 'forward') {
          await browser.tabs.goForward(navTabId);
        } else if (op === 'reload') {
          await browser.tabs.reload(navTabId, { bypassCache: !!data.bypassCache });
        } else {
          sendError(id, 'INVALID_PARAMETER', `Unknown navigation op: ${op}`);
          return;
        }
        let result = { ok: true, op: op, tabId: navTabId, browserProxyChange: applied.change };
        if (data.wait === 'none') {
          try {
            const tab = await browser.tabs.get(navTabId);
            result.url = tab.url;
            result.title = tab.title;
          } catch (e) { /* ignore */ }
        } else {
          // "settled": load, commit and NAV_QUIET_MS of network quiet. "load": load and
          // commit, whatever the network is doing; the result's network digest counts the
          // requests still in flight. "dom": load and commit, then a DOM that stopped
          // changing.
          const waitMode = (data.wait === 'load' || data.wait === 'dom') ? data.wait : 'settled';
          const timeoutMs = Math.min(data.timeoutMs || 20000, 120000);
          const settle = await waitForSettle(navTabId, {
            timeoutMs: timeoutMs,
            quietMs: waitMode === 'settled' ? NAV_QUIET_MS : 0,
            skipNetwork: waitMode !== 'settled',
            sinceTs: t0,
            expectNav: true,
            ignoreBlankCommit: op === 'goto' && !!data.url && data.url !== 'about:blank',
            priorCommitAt: commitBefore,
            selector: data.waitFor || null
          });
          Object.assign(result, settle);
          result.waitMode = waitMode;
          DOM_ARMED.delete(navTabId);
          if (waitMode === 'dom' && !settle.settled) {
            probeDomWatch(navTabId, true).catch(() => { /* the document is gone */ });
          }
          if (waitMode === 'dom' && settle.settled) {
            const dom = await waitForStableDom(navTabId,
              Math.min(DOM_STABLE_MAX_MS, Math.max(0, timeoutMs - settle.waitedMs)));
            result.domStable = dom.stable;
            if (dom.error) result.domError = dom.error;
            result.waitedMs = settle.waitedMs + dom.ms;
            if (dom.stable) result.timing.dom = result.waitedMs;
            // A client-rendered page often sets its title after the load event.
            try {
              const tab = await browser.tabs.get(navTabId);
              result.url = tab.url;
              result.title = tab.title;
            } catch (e) { /* the tab closed; the settle's answer stands */ }
          }
          // An abort that names the document just committed can be the echo of a process
          // switch (abortOfCompletedLoad); its load gets a moment to report completion.
          if (settle.settled && abortAwaitingCompletion(TAB_NAV.get(navTabId) || {}, t0)) {
            await waitForTabCondition(navTabId, ABORT_ECHO_GRACE_MS,
              () => !abortAwaitingCompletion(TAB_NAV.get(navTabId) || {}, t0));
          }
          // Only on the settle path: with wait:"none" the tab has not moved yet, so
          // landed === urlBefore always and "the navigation did not take effect"
          // would fire on every single call.
          if (urlBefore) result.urlBefore = urlBefore;
          const committedAt = (TAB_NAV.get(navTabId) || {}).lastCommitted;
          const net = netDigestFor(navTabId, t0, committedAt >= t0 ? committedAt : null);
          if ((net.failed && net.failed.length > 0) || net.pending > 0) result.network = net;
          // Any wait mode can settle before the page's batched console entries arrive.
          const flushedBy = {};
          if ((await flushConsoleCapture(navTabId, CONSOLE_FLUSH_TIMEOUT_MS, flushedBy)) === 'timeout') {
            result.consoleFlushTimedOut = true;
          }
          const cons = consoleErrorDigest(navTabId, t0, flushedBy.doc || null);
          if (cons.errors > 0) result.consoleErrors = cons;

          if (typeof result.url === 'string' && /^https?:/i.test(result.url)) {
            const sw = await captureServiceWorkers(navTabId);
            if (sw && (sw.controlled || (sw.registrations && sw.registrations.length > 0))) {
              result.serviceWorkers = sw;
            }
          }

          // --- failure diagnostics: WHY didn't it load? ---
          const navState = TAB_NAV.get(navTabId) || {};
          // Most-recent main-frame request since we started, its redirect hops, its
          // dialed peer, its HTTP status and the most-recent errored one; ONE
          // implementation, in mainFrameOutcome above. (A sibling navigation may have
          // added a later, successful request, which must not mask our abort.)
          const outcome = mainFrameOutcome(navTabId, t0);
          if (outcome.redirects) result.redirects = outcome.redirects;
          if (outcome.dialedAddress) result.dialedAddress = outcome.dialedAddress; // absent on a pure hang
          if (outcome.httpStatus != null) result.httpStatus = outcome.httpStatus;
          const rawErr = navigationError(navState, t0, outcome,
            op === 'goto' ? data.url : (op === 'reload' ? urlBefore : null));
          if (rawErr) {
            result.netError = humanizeNetError(rawErr);
            result.navEvents = navEventsSince(navState, t0);
          }
          // An abort caused by a NEWER same-tab navigation is self-inflicted, not a page error.
          if (isNavAbort(rawErr) && navState.lastNavIssuedAt && navState.lastNavIssuedAt > t0) {
            result.superseded = true;
          }
          if (op === 'goto' && (!result.url || result.url === 'about:blank') && data.url && data.url !== 'about:blank') {
            result.aboutBlank = true;
            result.pendingUrl = data.url;
          }
        }
        sendResponse(id, action, result);
        break;
      }

      case 'navigation.go':
      case 'navigation.go_to_url':
        await browser.tabs.update(data.tabId, { url: data.url });
        sendResponse(id, action, { success: true });
        break;

      case 'navigation.back':
        await browser.tabs.goBack(data.tabId);
        sendResponse(id, action, { success: true });
        break;

      case 'navigation.forward':
        await browser.tabs.goForward(data.tabId);
        sendResponse(id, action, { success: true });
        break;

      case 'navigation.reload':
        await browser.tabs.reload(data.tabId, { bypassCache: data.bypassCache || false });
        sendResponse(id, action, { success: true });
        break;

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown navigation action: ${action}`);
    }
  } catch (error) {
    if (error.code) { sendError(id, error.code, error.message); return; }
    // Same measured refusal as the tabs path; a navigate to file:// must say
    // whether the tab moved, not just repeat "Illegal URL".
    const fileMsg = await fileUrlRefusal(data && data.url, 'navigate a tab to', error.message);
    if (fileMsg) sendError(id, 'FILE_URL_OPEN_REFUSED', fileMsg);
    else sendError(id, 'API_ERROR', `Navigation API error: ${error.message}`);
  }
}

// Windows handlers
async function handleWindowsAction(id, action, data) {
  try {
    switch (action) {
      case 'windows.list':
        const windows = await browser.windows.getAll({
          populate: data.populate !== false, // default to true
          windowTypes: ['normal', 'popup', 'panel', 'devtools']
        });
        sendResponse(id, action, { windows });
        break;

      case 'windows.get':
        if (!data.windowId) {
          sendError(id, 'INVALID_PARAMETER', 'windowId is required for windows.get');
          return;
        }
        const window = await browser.windows.get(data.windowId, {
          populate: data.populate !== false
        });
        sendResponse(id, action, { window });
        break;

      case 'windows.get_current':
        const currentWindow = await browser.windows.getCurrent({
          populate: data.populate !== false
        });
        sendResponse(id, action, { window: currentWindow });
        break;

      case 'windows.get_last_focused':
        const lastFocusedWindow = await browser.windows.getLastFocused({
          populate: data.populate !== false
        });
        sendResponse(id, action, { window: lastFocusedWindow });
        break;

      case 'windows.create':
        const createOptions = {};
        if (data.url) createOptions.url = data.url;
        if (data.type) createOptions.type = data.type;
        if (data.state) createOptions.state = data.state;
        if (data.focused !== undefined) createOptions.focused = data.focused;
        if (data.width) createOptions.width = data.width;
        if (data.height) createOptions.height = data.height;
        if (data.top) createOptions.top = data.top;
        if (data.left) createOptions.left = data.left;
        if (data.incognito !== undefined) createOptions.incognito = data.incognito;

        const t0winCommit = Date.now();
        const newWindow = await browser.windows.create(createOptions);
        // Same commit honesty as tabs.create: never echo the REQUESTED url while the
        // tab is actually parked on about:blank.
        if (data.url && data.url !== 'about:blank') {
          const wtab = (newWindow.tabs && newWindow.tabs[0]) || null;
          if (wtab) {
            const c = await waitForCommit(wtab.id, t0winCommit, 8000);
            if (c.url) wtab.url = c.url;
            if (c.title) wtab.title = c.title;
            if (!c.committed) {
              newWindow.navPending = data.url;
              if (c.navError) newWindow.navError = humanizeNetError(c.navError);
              else if (c.timedOut) newWindow.navTimedOut = true;
            }
          }
        }
        sendResponse(id, action, { window: newWindow });
        break;

      case 'windows.close':
        if (!data.windowId) {
          sendError(id, 'INVALID_PARAMETER', 'windowId is required for windows.close');
          return;
        }
        await browser.windows.remove(data.windowId);
        sendResponse(id, action, { success: true, windowId: data.windowId });
        break;

      case 'windows.focus':
        if (!data.windowId) {
          sendError(id, 'INVALID_PARAMETER', 'windowId is required for windows.focus');
          return;
        }
        await browser.windows.update(data.windowId, { focused: true });
        sendResponse(id, action, { success: true, windowId: data.windowId });
        break;

      case 'windows.update':
        if (!data.windowId) {
          sendError(id, 'INVALID_PARAMETER', 'windowId is required for windows.update');
          return;
        }
        const updateOptions = {};
        if (data.state) updateOptions.state = data.state;
        if (data.focused !== undefined) updateOptions.focused = data.focused;
        if (data.width) updateOptions.width = data.width;
        if (data.height) updateOptions.height = data.height;
        if (data.top !== undefined) updateOptions.top = data.top;
        if (data.left !== undefined) updateOptions.left = data.left;

        const updatedWindow = await browser.windows.update(data.windowId, updateOptions);
        sendResponse(id, action, { window: updatedWindow });
        break;

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown windows action: ${action}`);
    }
  } catch (error) {
    if (error.message && error.message.includes('No window with id')) {
      sendError(id, 'WINDOW_NOT_FOUND', `Window with ID ${data.windowId} not found`);
    } else if (error.message && error.message.includes('Invalid window state')) {
      sendError(id, 'INVALID_WINDOW_STATE', error.message);
    } else if (error.message && error.message.includes('Invalid window type')) {
      sendError(id, 'INVALID_WINDOW_TYPE', error.message);
    } else {
      sendError(id, 'API_ERROR', `Windows API error: ${error.message}`);
    }
  }
}

// Request monitoring state
const activeMonitors = new Map();          // monitor_id -> monitor config
const retiredMonitors = new Map();         // monitor_id -> { at, reason, monitor }
const evictedMonitors = new Map();         // monitor_id -> { at, reason }
const capturedRequests = new Map();        // monitor_id -> array of requests
const requestDetails = new Map();          // request_id -> full request details
const capturedResponseBodies = new Map();  // request_id -> response body data
let monitorIdSeq = 0;

// Monitor lifetimes (identical to gecko). NB: on Chromium, response BODY capture is
// via the page fetch/XHR wrapper (design D5a), NOT a StreamFilter; so a body-capturing
// monitor does NOT force uncompressed downloads or hold blocking listeners. The
// metadata-capture webRequest listeners are non-blocking and always-on (guarded).
const MONITOR_TTL_DEFAULT_MS = 10 * 60 * 1000;
const MONITOR_TTL_MIN_MS = 10 * 1000;
const MONITOR_TTL_MAX_MS = 60 * 60 * 1000;
const MONITOR_RETENTION_MS = 5 * 60 * 1000;
const MONITOR_DISCONNECT_GRACE_MS = 60 * 1000;
const MONITOR_SWEEP_INTERVAL_MS = 15 * 1000;
const REQUEST_DETAILS_MAX = 500;
const CAPTURED_PER_MONITOR_MAX = 300;
const CAPTURED_BODIES_MAX = 100;
const MAX_BODY_SIZE_DEFAULT = 50000;
const MAX_BODY_SIZE_LIMIT = 2 * 1024 * 1024;
const CAPTURED_BODIES_MAX_BYTES = 20 * 1024 * 1024;
const EVICTED_MONITORS_MAX = 50;

let monitorSweepTimer = null;
let monitorsLastServedAt = Date.now();

// Armed monitor DEFINITIONS are mirrored to chrome.storage.session so an SW eviction
// mid-session doesn't silently disarm capture: on SW restart restoreMonitorDefs()
// re-populates activeMonitors and re-broadcasts arm state (design D1). Captured DATA
// (bodies, request details, ring buffers) is SW memory and does NOT survive; a
// documented, bounded parity gap.
const MONITOR_DEFS_KEY = 'browserplugMonitorDefs';
function persistMonitorDefs() {
  try {
    if (!chrome.storage || !chrome.storage.session) return;
    const defs = [];
    for (const m of activeMonitors.values()) {
      defs.push({ id: m.id, url_patterns: m.url_patterns, options: m.options,
                  tab_id: m.tab_id, started_at: m.started_at, expires_at: m.expires_at });
    }
    chrome.storage.session.set({ [MONITOR_DEFS_KEY]: defs }).catch(() => {});
  } catch (e) { /* best-effort */ }
}
async function restoreMonitorDefs() {
  try {
    if (!chrome.storage || !chrome.storage.session) return;
    const got = await chrome.storage.session.get({ [MONITOR_DEFS_KEY]: [] });
    const defs = got && got[MONITOR_DEFS_KEY];
    if (!Array.isArray(defs) || defs.length === 0) return;
    const now = Date.now();
    let restored = 0;
    for (const d of defs) {
      if (!d || !d.id || activeMonitors.has(d.id)) continue;
      if (typeof d.expires_at === 'number' && now >= d.expires_at) continue; // already expired
      activeMonitors.set(d.id, {
        id: d.id, url_patterns: d.url_patterns || [], options: d.options || {},
        tab_id: d.tab_id || null, started_at: d.started_at, expires_at: d.expires_at, status: 'active'
      });
      if (!capturedRequests.has(d.id)) capturedRequests.set(d.id, []);
      restored++;
    }
    if (restored > 0) {
      ensureMonitorSweeper();
      broadcastMonitorState();
      console.log('Restored ' + restored + ' monitor definition(s) after SW restart (captured DATA does not survive an SW eviction; definitions do)');
    }
  } catch (e) { /* best-effort */ }
}

function monitorTtlMs(options) {
  const secs = options && Number(options.ttl_seconds);
  if (!secs || !isFinite(secs)) return MONITOR_TTL_DEFAULT_MS;
  return Math.min(MONITOR_TTL_MAX_MS, Math.max(MONITOR_TTL_MIN_MS, secs * 1000));
}

function clampMaxBodySize(value) {
  const n = Number(value);
  if (!n || !isFinite(n) || n <= 0) return MAX_BODY_SIZE_DEFAULT;
  return Math.min(MAX_BODY_SIZE_LIMIT, Math.floor(n));
}

function monitorState(monitorId) {
  if (activeMonitors.has(monitorId)) return 'active';
  if (retiredMonitors.has(monitorId)) return 'retired';
  if (evictedMonitors.has(monitorId)) return 'expired';
  return 'unknown';
}

function monitorConfig(monitorId) {
  const active = activeMonitors.get(monitorId);
  if (active) return active;
  const retired = retiredMonitors.get(monitorId);
  return (retired && retired.monitor) || null;
}

// The ONE teardown path; explicit stop, TTL expiry and disconnect cleanup all go
// through it. Body capture disarms itself: the fetch-wrap reads bodyCaptureArmed()
// state via broadcasts, so removing the monitor + re-broadcasting stops further capture.
function retireMonitor(monitorId, reason) {
  const monitor = activeMonitors.get(monitorId);
  if (!monitor) return null;
  activeMonitors.delete(monitorId);
  retiredMonitors.set(monitorId, { at: Date.now(), reason: reason, monitor: monitor });
  monitor.status = 'stopped';
  persistMonitorDefs();
  broadcastMonitorState(); // disarm the fetch-wrap in pages if body capture just ended
  console.log(`🛑 Monitor ${monitorId} retired (${reason})`);
  return monitor;
}

function evictMonitorData(monitorId, reason) {
  capturedRequests.delete(monitorId);
  for (const [requestId, detail] of requestDetails) {
    const ids = detail.monitor_ids || [detail.monitor_id];
    if (ids.indexOf(monitorId) === -1) continue;
    detail.monitor_ids = ids.filter(m => m !== monitorId);
    if (Array.isArray(detail.listed_in)) {
      detail.listed_in = detail.listed_in.filter(m => m !== monitorId);
    }
    if (detail.monitor_ids.some(m => activeMonitors.has(m) || retiredMonitors.has(m))) continue;
    requestDetails.delete(requestId);
    capturedResponseBodies.delete(requestId);
  }
  evictedMonitors.set(monitorId, { at: Date.now(), reason: reason || 'retention window passed' });
  while (evictedMonitors.size > EVICTED_MONITORS_MAX) {
    evictedMonitors.delete(evictedMonitors.keys().next().value);
  }
}

function bodyBytes(record) {
  if (!record) return 0;
  if (typeof record.content_length === 'number') return record.content_length;
  return (record.response_body || '').length;
}

function dropFromMonitorList(requestId, monitorId) {
  const list = capturedRequests.get(monitorId);
  if (!list) return;
  const i = list.findIndex(r => r.request_id === requestId);
  if (i >= 0) list.splice(i, 1);
}

function forgetRequestForMonitor(requestId, monitorId) {
  dropFromMonitorList(requestId, monitorId);
  const detail = requestDetails.get(requestId);
  if (detail && Array.isArray(detail.listed_in)) {
    detail.listed_in = detail.listed_in.filter(m => m !== monitorId);
    if (detail.listed_in.some(m => activeMonitors.has(m) || retiredMonitors.has(m))) return;
  }
  requestDetails.delete(requestId);
  capturedResponseBodies.delete(requestId);
}

function requestBelongsToMonitor(detail, monitorId) {
  if (!monitorId) return true;
  const ids = detail.monitor_ids || [detail.monitor_id];
  return ids.indexOf(monitorId) !== -1;
}

function enforceCaptureCaps() {
  while (requestDetails.size > REQUEST_DETAILS_MAX) {
    const oldest = requestDetails.keys().next().value;
    const detail = requestDetails.get(oldest);
    requestDetails.delete(oldest);
    capturedResponseBodies.delete(oldest);
    if (detail) {
      for (const m of (detail.listed_in || [detail.monitor_id])) dropFromMonitorList(oldest, m);
    }
  }
  let bytes = 0;
  for (const record of capturedResponseBodies.values()) bytes += bodyBytes(record);
  while (capturedResponseBodies.size > CAPTURED_BODIES_MAX || bytes > CAPTURED_BODIES_MAX_BYTES) {
    const oldest = capturedResponseBodies.keys().next().value;
    if (oldest === undefined) break;
    bytes -= bodyBytes(capturedResponseBodies.get(oldest));
    capturedResponseBodies.delete(oldest);
    const detail = requestDetails.get(oldest);
    if (detail) detail.body_evicted = true;
  }
}

function sweepMonitors() {
  const now = Date.now();
  if (isConnected) monitorsLastServedAt = now;
  const serverGone = !isConnected && (now - monitorsLastServedAt) > MONITOR_DISCONNECT_GRACE_MS;

  for (const [monitorId, monitor] of activeMonitors) {
    if (serverGone) {
      retireMonitor(monitorId, 'server connection lost; nothing left to consume the captures');
    } else if (now >= monitor.expires_at) {
      retireMonitor(monitorId, 'TTL expired');
    }
  }

  for (const [monitorId, retired] of retiredMonitors) {
    if (now - retired.at > MONITOR_RETENTION_MS) {
      retiredMonitors.delete(monitorId);
      evictMonitorData(monitorId, `${retired.reason}; captures aged out after the ${Math.round(MONITOR_RETENTION_MS / 60000)} min retention window`);
    }
  }

  enforceCaptureCaps();
  if (activeMonitors.size === 0 && retiredMonitors.size === 0) stopMonitorSweeper();
}

function ensureMonitorSweeper() {
  monitorsLastServedAt = Date.now();
  if (!monitorSweepTimer) monitorSweepTimer = setInterval(sweepMonitors, MONITOR_SWEEP_INTERVAL_MS);
}

function stopMonitorSweeper() {
  if (monitorSweepTimer) { clearInterval(monitorSweepTimer); monitorSweepTimer = null; }
}

// --- D5a body capture: page fetch/XHR wrapper correlation --------------------
// The MAIN-world wrapper (content-main.js) captures fetch/XHR response text while a
// body-capture monitor is armed and relays it via content.js. Here we correlate each
// relayed body to its webRequest log record (by tabId, url, method, nearest ts) and
// store it shape-identically to gecko's finalizeStreamCapture result, so
// requests.get_content is unchanged. PARTIAL parity: fetch/XHR bodies only;
// document/image/media/beacon bodies are NOT capturable on Chromium without
// chrome.debugger (not used in v1). No Accept-Encoding forcing (the wrapper reads
// decoded text); size/metadata still come from webRequest.
function bodyCaptureArmed() {
  for (const m of activeMonitors.values()) {
    if (m.options && m.options.capture_response_bodies) return true;
  }
  return false;
}
function bodyCaptureUrlPatterns() {
  const set = new Set();
  for (const m of activeMonitors.values()) {
    if (m.options && m.options.capture_response_bodies) {
      for (const p of (m.url_patterns || [])) set.add(p);
    }
  }
  return Array.from(set);
}
async function broadcastMonitorState() {
  const msg = { action: 'browserplug_monitor_state', armed: bodyCaptureArmed(), urlPatterns: bodyCaptureUrlPatterns() };
  try {
    const tabs = await browser.tabs.query({});
    for (const t of tabs) {
      if (t.id == null) continue;
      try { browser.tabs.sendMessage(t.id, msg).catch(() => {}); } catch (e) { /* no CS in this tab */ }
    }
  } catch (e) { /* best-effort */ }
}

// Find the most recent captured requestDetails matching (tabId, url, method) near ts.
function correlateNetBody(tabId, entry) {
  let best = null, bestDelta = Infinity;
  const wantMethod = (entry.method || 'GET');
  const wantTs = entry.ts || Date.now();
  for (const [rid, detail] of requestDetails) {
    if (detail.tab_id !== tabId) continue;
    if (detail.url !== entry.url) continue;
    if ((detail.method || 'GET') !== wantMethod) continue;
    let dt = Infinity;
    try { dt = Math.abs(new Date(detail.timestamp).getTime() - wantTs); } catch (e) {}
    if (dt < bestDelta) { bestDelta = dt; best = rid; }
  }
  return best;
}

function storeNetBody(tabId, entry) {
  if (!entry || !entry.url) return;
  const requestId = correlateNetBody(tabId, entry);
  if (!requestId) return; // couldn't correlate; get_content honestly says "not captured"
  const detail = requestDetails.get(requestId);
  const ids = detail ? (detail.monitor_ids || [detail.monitor_id]) : [];
  // Respect content_types_to_capture + max_body_size of a matching body-capture monitor.
  let ctOk = false;
  let maxSize = MAX_BODY_SIZE_DEFAULT;
  for (const mid of ids) {
    const m = monitorConfig(mid);
    if (m && m.options && m.options.capture_response_bodies) {
      if (shouldCaptureContentTypeBG(entry.contentType, m.options.content_types_to_capture)) {
        ctOk = true;
        maxSize = clampMaxBodySize(m.options.max_body_size);
        break;
      }
    }
  }
  if (!ctOk) return;

  let body = String(entry.body == null ? '' : entry.body);
  let truncated = !!entry.truncated;
  if (body.length > maxSize) { body = body.slice(0, maxSize); truncated = true; }
  const contentType = (detail && detail.response_content_type) || entry.contentType || null;
  const fullSize = detail && typeof detail.response_content_length === 'number' &&
                   detail.response_content_length > 0 ? detail.response_content_length : undefined;
  capturedResponseBodies.set(requestId, {
    request_id: requestId,
    url: detail ? detail.url : entry.url,
    method: detail ? detail.method : (entry.method || 'GET'),
    status_code: detail ? detail.status_code : entry.status,
    content_type: contentType,
    content_length: body.length,
    full_size_bytes: fullSize,
    response_body: body,
    encoding: 'utf-8',           // the wrapper relays decoded text
    truncated: truncated,
    timestamp: new Date().toISOString(),
    source: 'fetch_wrap'
  });
  enforceCaptureCaps();
}

// Same semantics as gecko: empty list = capture everything; substring match; "type/*"
// by family.
function shouldCaptureContentTypeBG(contentType, captureTypes) {
  if (!captureTypes || captureTypes.length === 0) return true;
  if (!contentType) return false;
  return captureTypes.some(type => {
    if (type.includes('*')) {
      const baseType = type.split('/')[0];
      return contentType.startsWith(baseType);
    }
    return contentType.includes(type);
  });
}

// Byte helpers shared by request-body describe + screenshot base64 encoding.
function decodeTextBytes(bytes, truncated) {
  const strict = (b) => {
    try { return new TextDecoder('utf-8', { fatal: true }).decode(b); }
    catch (e) { return null; }
  };
  let text = strict(bytes);
  if (text === null && truncated) {
    for (let trim = 1; trim <= 3 && text === null; trim++) {
      text = strict(bytes.subarray(0, bytes.length - trim));
    }
  }
  return text;
}

function mergeChunks(chunks) {
  let total = 0;
  for (const c of chunks) total += c.byteLength;
  const merged = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) { merged.set(new Uint8Array(c), off); off += c.byteLength; }
  return merged;
}

function base64FromBytes(bytes) {
  let binary = '';
  const SLICE = 0x8000;
  for (let i = 0; i < bytes.length; i += SLICE) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + SLICE));
  }
  return btoa(binary);
}

// --- Always-on, NON-BLOCKING monitor-capture webRequest listeners ------------
// MV3: every listener is registered SYNCHRONOUSLY at top level; each is a no-op while
// no monitor is armed (activeMonitors.size guard) so the gecko dynamic add/remove
// dance is gone. Chromium has no filterResponseData and no blocking webRequest for
// non-policy extensions, so these are observational only (metadata + request/response
// headers + request body). Response BODIES come from the page fetch/XHR wrapper (D5a).
// The gecko "onSendHeaders + requestHeaders breaks filterResponseData" landmine does
// NOT exist here (no StreamFilter), so request headers ride onSendHeaders honestly.
// `extraHeaders` is specified on the header events because Chrome 72+ withholds
// Cookie (request) and Set-Cookie (response) from listeners without it; gecko sees
// them, so we capture them here for FULL request-log parity (D5); both are redacted
// by redactHeaders() before anything is stored (ALWAYS_REDACTED_HEADERS).
browser.webRequest.onBeforeRequest.addListener(d => {
  if (activeMonitors.size === 0) return;
  handleWebRequestEvent('onBeforeRequest', d);
}, { urls: ['<all_urls>'] }, ['requestBody']);

browser.webRequest.onSendHeaders.addListener(d => {
  if (activeMonitors.size === 0) return;
  handleWebRequestEvent('onSendHeaders', d);
}, { urls: ['<all_urls>'] }, ['requestHeaders', 'extraHeaders']);

browser.webRequest.onHeadersReceived.addListener(d => {
  if (activeMonitors.size === 0) return;
  handleWebRequestEvent('onHeadersReceived', d);
}, { urls: ['<all_urls>'] }, ['responseHeaders', 'extraHeaders']);

browser.webRequest.onCompleted.addListener(d => {
  if (activeMonitors.size === 0) return;
  handleWebRequestEvent('onCompleted', d);
}, { urls: ['<all_urls>'] });

browser.webRequest.onErrorOccurred.addListener(d => {
  if (activeMonitors.size === 0) return;
  handleWebRequestEvent('onErrorOccurred', d);
}, { urls: ['<all_urls>'] });

function handleWebRequestEvent(eventType, details) {
  for (const [monitorId, monitor] of activeMonitors) {
    if (shouldCaptureRequest(monitor, details)) {
      captureRequestEvent(monitorId, eventType, details);
    }
  }
}

function shouldCaptureRequest(monitor, details) {
  if (monitor.tab_id && details.tabId !== monitor.tab_id) {
    return false;
  }
  if (monitor.url_patterns && monitor.url_patterns.length > 0) {
    const url = details.url;
    return monitor.url_patterns.some(pattern => {
      if (pattern === '*') return true;
      const regexPattern = pattern
        .replace(/\*/g, '.*')
        .replace(/\?/g, '.');
      try {
        return new RegExp(regexPattern).test(url);
      } catch (e) {
        console.warn(`Invalid URL pattern: ${pattern}`, e);
        return false;
      }
    });
  }
  return true;
}

function captureRequestEvent(monitorId, eventType, details) {
  const requestId = details.requestId;
  const timestamp = new Date().toISOString();

  let request = requestDetails.get(requestId);
  if (request) {
    if (request.monitor_ids.indexOf(monitorId) === -1) request.monitor_ids.push(monitorId);
  }
  if (!request) {
    request = {
      request_id: requestId,
      monitor_id: monitorId,
      monitor_ids: [monitorId],
      listed_in: [],
      url: details.url,
      method: details.method || 'GET',
      tab_id: details.tabId,
      frame_id: details.frameId,
      type: details.type,
      timestamp: timestamp,
      events: []
    };
    requestDetails.set(requestId, request);
    enforceCaptureCaps();
  }

  const event = {
    type: eventType,
    timestamp: timestamp,
    timeStamp: details.timeStamp
  };

  switch (eventType) {
    case 'onBeforeRequest':
      event.url = details.url;
      event.method = details.method;
      event.requestBody = details.requestBody;
      break;

    // The request's outgoing headers as the page composed them (Cookie included). On
    // Chromium these ride onSendHeaders (observational, non-blocking); no
    // filterResponseData conflict and no Accept-Encoding override to hide, so no COPY
    // dance is needed (we never mutate these).
    case 'onSendHeaders':
      event.requestHeaders = (details.requestHeaders || [])
        .map(h => ({ name: h.name, value: h.value }));
      break;

    case 'onHeadersReceived':
      event.responseHeaders = details.responseHeaders;
      event.statusCode = details.statusCode;
      event.statusLine = details.statusLine;
      request.status_code = details.statusCode;
      request.response_headers = details.responseHeaders;

      if (details.responseHeaders) {
        for (const header of details.responseHeaders) {
          if (header.name.toLowerCase() === 'content-length') {
            request.response_content_length = parseInt(header.value) || 0;
          }
          if (header.name.toLowerCase() === 'content-type') {
            request.response_content_type = header.value;
          }
        }
      }
      break;

    case 'onCompleted':
      event.statusCode = details.statusCode;
      request.status_code = details.statusCode;
      request.completed = true;
      request.duration_ms = details.timeStamp - (request.events[0]?.timeStamp || details.timeStamp);
      break;

    case 'onErrorOccurred':
      event.error = details.error;
      request.error = details.error;
      request.completed = true;
      break;
  }

  request.events.push(event);

  if (request.completed && request.listed_in.indexOf(monitorId) === -1) {
    const captured = capturedRequests.get(monitorId) || [];
    captured.push({
      request_id: requestId,
      url: request.url,
      method: request.method,
      status_code: request.status_code,
      duration_ms: request.duration_ms || 0,
      timestamp: request.timestamp,
      tab_id: request.tab_id,
      type: request.type,
      error: request.error,
      response_size_bytes: request.response_content_length || 0,
      response_content_type: request.response_content_type || null
    });
    if (captured.length > CAPTURED_PER_MONITOR_MAX) {
      for (const dropped of captured.splice(0, captured.length - CAPTURED_PER_MONITOR_MAX)) {
        forgetRequestForMonitor(dropped.request_id, monitorId);
      }
    }
    capturedRequests.set(monitorId, captured);
    request.listed_in.push(monitorId);
  }
}

// Credentials must never land in an agent's context. sensitive_headers ADDS to this
// floor and can never subtract from it.
const ALWAYS_REDACTED_HEADERS = [
  'authorization', 'proxy-authorization', 'cookie', 'set-cookie',
  'x-api-key', 'api-key', 'apikey', 'x-auth-token', 'auth-token',
  'x-access-token', 'access-token', 'x-session-token', 'x-session-id',
  'x-csrf-token', 'x-xsrf-token', 'csrf-token', 'xsrf-token',
  'x-amz-security-token', 'x-goog-api-key', 'x-functions-key',
];

function redactHeaders(headers, monitorId) {
  const names = new Set(ALWAYS_REDACTED_HEADERS);
  const cfg = monitorConfig(monitorId);
  const extra = cfg && cfg.options && cfg.options.sensitive_headers;
  if (Array.isArray(extra)) for (const n of extra) names.add(String(n).toLowerCase());
  const out = {};
  for (const name of Object.keys(headers || {})) {
    out[name] = names.has(name.toLowerCase()) ? '<redacted>' : headers[name];
  }
  return out;
}

function pickHeader(headers, name) {
  const key = Object.keys(headers || {}).find(k => k.toLowerCase() === name);
  return key ? headers[key] : null;
}

// webRequest hands the request body over as `formData` (already decoded) OR as `raw`
// ArrayBuffers; decode the bytes (same shape/handling as gecko).
function describeRequestBody(requestBody, contentType, includeBinary) {
  const none = (note) => ({ included: false, content: null, content_type: contentType || null,
                            encoding: null, size_bytes: 0, truncated: false, note: note });
  if (!requestBody) return none('No request body was sent (or the browser exposed none for this request type)');
  if (requestBody.formData) {
    const json = JSON.stringify(requestBody.formData);
    return {
      included: true, content: json, content_type: contentType || null, encoding: 'utf-8',
      size_bytes: json.length, truncated: false,
      note: 'Form fields as JSON — webRequest decodes them; the raw encoded body is not exposed'
    };
  }
  const raw = Array.isArray(requestBody.raw) ? requestBody.raw.filter(p => p && p.bytes) : [];
  if (raw.length > 0) {
    const bytes = mergeChunks(raw.map(p => p.bytes));
    const text = decodeTextBytes(bytes, false);
    if (text !== null) {
      return { included: true, content: text, content_type: contentType || null,
               encoding: 'utf-8', size_bytes: bytes.length, truncated: false };
    }
    if (!includeBinary) {
      return { included: false, content: null, content_type: contentType || null,
               encoding: 'base64', size_bytes: bytes.length, truncated: false,
               note: 'Binary request body — re-request this request_id with include_binary=true to receive it' };
    }
    return { included: true, content: base64FromBytes(bytes), content_type: contentType || null,
             encoding: 'base64', size_bytes: bytes.length, truncated: false };
  }
  if (requestBody.error) return none(`The browser could not read the request body: ${requestBody.error}`);
  return none('A request body was sent but the browser exposed no readable bytes for it (e.g. a file/blob upload)');
}

// ---- body censorship glue (Options > General; the logic is body-censor.js) ----
// requests.get_content returns bodies raw unless the user opted in: the "censor secrets"
// toggle and/or custom patterns. The settings are read on EVERY get, so a change applies
// to traffic that is already captured. Neither failure answers raw while censoring may be
// on: a missing module with censoring switched on is refused, and so is a get whose
// settings cannot be read and were never read before.
//
// The settings as last read; null when unknown. A settings change clears it, so a later
// failed read never falls back to a toggle position the user has since changed.
let lastCensorSettings = null;
if (browser.storage && browser.storage.onChanged) {
  browser.storage.onChanged.addListener((changes, area) => {
    if (area === 'sync' && changes && (changes.censorSecrets || changes.censorPatterns)) {
      lastCensorSettings = null;
    }
  });
}

function censorUnavailable(message) {
  const err = new Error(message);
  err.code = 'CENSOR_UNAVAILABLE';
  return err;
}

// A failed read falls back to the last known settings and, with none, refuses, since
// censoring may be on.
async function readCensorSettings(defaults) {
  try {
    lastCensorSettings = await browser.storage.sync.get(defaults);
  } catch (e) {
    if (!lastCensorSettings) {
      throw censorUnavailable('could not read the body censoring settings '
        + `(${(e && e.message) || e}), so raw bodies are not returned; retry`);
    }
  }
  return lastCensorSettings;
}

async function censorRequestBodies(result) {
  const C = globalThis.BrowserplugBodyCensor;
  if (!C) {
    const raw = await readCensorSettings({ censorSecrets: false, censorPatterns: [] });
    if (raw.censorSecrets === true) {
      throw censorUnavailable('body censoring is switched on in Options but its module did not load; raw bodies are not returned');
    }
    return result;
  }
  C.censorResult(result, C.prepareSettings(await readCensorSettings(C.SETTINGS_DEFAULTS)));
  return result;
}

// Request monitoring handlers
async function handleRequestsAction(id, action, data) {
  try {
    switch (action) {
      case 'requests.recent': {
        const recentTabId = await resolveTabId(data);
        let log = (TAB_REQUESTS.get(recentTabId) || []).slice();
        if (!data.includeStatic) {
          log = log.filter(r => !STATIC_TYPES.has(r.type) || (r.status && r.status >= 400) || r.error);
        }
        if (data.filter) {
          try {
            const re = new RegExp(data.filter, 'i');
            log = log.filter(r => re.test(r.url));
          } catch (e) {
            sendError(id, 'INVALID_PARAMETER', `invalid filter regex: ${e.message}`);
            return;
          }
        }
        if (typeof data.sinceMs === 'number') {
          const cutoff = Date.now() - data.sinceMs;
          log = log.filter(r => r.start >= cutoff);
        }
        const total = log.length;
        log = log.slice(-(data.limit || 30));
        sendResponse(id, action, {
          tabId: recentTabId,
          total: total,
          returned: log.length,
          requests: log.map(r => ({
            method: r.method,
            url: r.url,
            type: r.type,
            status: r.status != null ? r.status : (r.error ? null : 'pending'),
            error: r.error,
            durMs: r.end ? Math.round(r.end - r.start) : undefined,
            fromCache: r.fromCache,
            ip: r.ip,
            ageMs: Math.round(Date.now() - r.start)
          }))
        });
        break;
      }

      case 'requests.start_monitoring':
        const monitor_id = `mon_${Date.now()}_${++monitorIdSeq}`;
        const monitorTtl = monitorTtlMs(data.options);
        const monitor = {
          id: monitor_id,
          url_patterns: data.url_patterns || [],
          options: data.options || {},
          tab_id: data.tab_id || null,
          started_at: new Date().toISOString(),
          expires_at: Date.now() + monitorTtl,
          status: 'active'
        };

        activeMonitors.set(monitor_id, monitor);
        capturedRequests.set(monitor_id, []);
        ensureMonitorSweeper();
        persistMonitorDefs();
        // Arm the page fetch/XHR wrapper for body capture (D5a). No-op for a
        // metadata-only monitor (bodyCaptureArmed stays whatever it was).
        broadcastMonitorState();

        sendResponse(id, action, {
          monitor_id: monitor_id,
          status: 'active',
          started_at: monitor.started_at,
          expires_at: new Date(monitor.expires_at).toISOString(),
          ttl_seconds: Math.round(monitorTtl / 1000),
          url_patterns: monitor.url_patterns,
          options: monitor.options
        });
        break;

      case 'requests.stop_monitoring':
        const monitorToStop = activeMonitors.get(data.monitor_id);
        if (!monitorToStop) {
          const stopState = monitorState(data.monitor_id);
          if (stopState === 'retired') {
            sendError(id, 'MONITOR_NOT_FOUND',
              `Monitor ${data.monitor_id} already ended (${retiredMonitors.get(data.monitor_id).reason}); its captures stay readable until they age out`);
          } else if (stopState === 'expired') {
            sendError(id, 'MONITOR_EXPIRED',
              `Monitor ${data.monitor_id} already ended (${evictedMonitors.get(data.monitor_id).reason}) and its captures have since been evicted`);
          } else {
            sendError(id, 'MONITOR_NOT_FOUND', `Monitor ${data.monitor_id} not found`);
          }
          break;
        }

        const captured = capturedRequests.get(data.monitor_id) || [];
        const startTime = new Date(monitorToStop.started_at).getTime();
        const stopTime = Date.now();
        const durationSeconds = (stopTime - startTime) / 1000;

        retireMonitor(data.monitor_id, 'stopped by the client');

        sendResponse(id, action, {
          monitor_id: data.monitor_id,
          status: 'stopped',
          stopped_at: new Date().toISOString(),
          total_requests_captured: captured.length,
          statistics: {
            duration_seconds: durationSeconds,
            requests_per_second: captured.length / Math.max(durationSeconds, 1),
            total_data_size: captured.reduce((sum, req) => sum + (req.response_size_bytes || 0), 0)
          }
        });
        break;

      case 'requests.list_captured': {
        const listState = monitorState(data.monitor_id);
        if (listState === 'unknown') {
          sendError(id, 'MONITOR_NOT_FOUND',
            `Monitor ${data.monitor_id} not found — it never existed (or the extension was reloaded and lost it)`);
          break;
        }
        if (listState === 'expired') {
          const ev = evictedMonitors.get(data.monitor_id);
          sendError(id, 'MONITOR_EXPIRED',
            `Monitor ${data.monitor_id} ended (${ev.reason}) and its captures have since been evicted — start a new monitor and reproduce the traffic`);
          break;
        }
        const listRetired = retiredMonitors.get(data.monitor_id);
        const monitorRequests = capturedRequests.get(data.monitor_id) || [];

        sendResponse(id, action, Object.assign({
          monitor_id: data.monitor_id,
          status: listState === 'active' ? 'active' : 'stopped',
          total_requests: monitorRequests.length,
          requests: monitorRequests.map(req => ({
            request_id: req.request_id,
            url: req.url,
            method: req.method,
            status_code: req.status_code,
            duration_ms: req.duration_ms,
            timestamp: req.timestamp,
            tab_id: req.tab_id,
            type: req.type,
            error: req.error
          }))
        }, listRetired ? {
          ended_reason: listRetired.reason,
          readable_for_seconds: Math.max(0,
            Math.round((MONITOR_RETENTION_MS - (Date.now() - listRetired.at)) / 1000))
        } : {}));
        break;
      }

      case 'requests.get_content': {
        if (data.monitor_id) {
          const getState = monitorState(data.monitor_id);
          if (getState === 'unknown') {
            sendError(id, 'MONITOR_NOT_FOUND',
              `Monitor ${data.monitor_id} not found — it never existed (or the extension was reloaded and lost it)`);
            break;
          }
          if (getState === 'expired') {
            const ev = evictedMonitors.get(data.monitor_id);
            sendError(id, 'MONITOR_EXPIRED',
              `Monitor ${data.monitor_id} ended (${ev.reason}) and its captures have since been evicted — start a new monitor and reproduce the traffic`);
            break;
          }
        }

        const requestDetail = requestDetails.get(data.request_id);
        if (!requestDetail) {
          sendError(id, 'REQUEST_NOT_FOUND',
            `Request ${data.request_id} not found — it was never captured, or it aged out of the bounded capture buffer (the ${REQUEST_DETAILS_MAX} most recent requests)`);
          break;
        }
        if (!requestBelongsToMonitor(requestDetail, data.monitor_id)) {
          sendError(id, 'REQUEST_NOT_FOUND',
            `Request ${data.request_id} was not captured by monitor ${data.monitor_id} ` +
            `(it belongs to ${(requestDetail.monitor_ids || [requestDetail.monitor_id]).join(', ')})`);
          break;
        }

        let requestHeaders = null;
        let responseHeaders = {};
        let requestBody = null;

        for (const event of requestDetail.events) {
          if (event.type === 'onBeforeRequest' && event.requestBody) {
            requestBody = event.requestBody;
          }
          // FIRST hop wins (headers + url must describe the same hop).
          if (event.type === 'onSendHeaders' && event.requestHeaders
              && requestHeaders === null) {
            requestHeaders = event.requestHeaders.reduce((acc, header) => {
              acc[header.name] = header.value;
              return acc;
            }, {});
          }
          if (event.type === 'onHeadersReceived' && event.responseHeaders) {
            responseHeaders = event.responseHeaders.reduce((acc, header) => {
              acc[header.name] = header.value;
              return acc;
            }, {});
          }
        }
        const responseContentType = pickHeader(responseHeaders, 'content-type') ||
                                    requestDetail.response_content_type || null;
        const monitorOfRequest = data.monitor_id || requestDetail.monitor_id;

        const headersNote = requestHeaders === null
          ? 'Request headers were not captured for this request (no onSendHeaders '
            + 'fired; typically a response served from cache, or a monitor started '
            + 'after the request was already in flight). This is NOT "the request sent '
            + 'no headers".'
          : undefined;

        const contentResult = {
          request_id: data.request_id,
          monitor_id: monitorOfRequest,
          request_headers: redactHeaders(requestHeaders, monitorOfRequest),
          request_headers_note: headersNote,
          response_headers: redactHeaders(responseHeaders, monitorOfRequest),
          request_body: describeRequestBody(
            requestBody, pickHeader(requestHeaders, 'content-type'), !!data.include_binary),
          response_body: (() => {
            // Direct lookup by requestId. On Chromium the body (if any) was stored by the
            // page fetch/XHR wrapper correlated to this webRequest record; a miss means no
            // capturable body for this request on this engine.
            const rec = capturedResponseBodies.get(data.request_id);
            if (!rec) {
              return {
                included: false,
                content: null,
                content_type: responseContentType,
                encoding: null,
                size_bytes: requestDetail.response_content_length || 0,
                truncated: false,
                note: requestDetail.body_evicted
                  ? 'The captured body was EVICTED to keep the capture buffers bounded (newer/larger bodies since). The request itself is still listed; re-run it under a fresh monitor to capture the body again.'
                  : 'No response body was captured for this request. On Chromium only fetch/XHR response bodies are capturable (via the page fetch/XHR wrapper) — document/image/media/beacon bodies are not; the monitor may also not have been capturing bodies, or its content_types_to_capture may exclude this type. size_bytes is the Content-Length the server reported.'
              };
            }

            const bodyEncoding = rec.encoding || 'utf-8';
            const returned = typeof rec.content_length === 'number'
              ? rec.content_length
              : (rec.response_body || '').length;
            const full = typeof rec.full_size_bytes === 'number' ? rec.full_size_bytes : undefined;
            const sizeNote = rec.truncated
              ? ` Truncated at max_body_size: ${returned} bytes returned${full ? ` of ${full}` : ' (the full size is unknown — the server sent no Content-Length)'}.`
              : '';
            const sourceNote = 'Response body captured via the page fetch/XHR wrapper (fetch/XHR only on this engine).';

            if (bodyEncoding === 'base64' && !data.include_binary) {
              return Object.assign({
                included: false,
                content: null,
                content_type: rec.content_type || null,
                encoding: 'base64',
                size_bytes: returned,
                truncated: !!rec.truncated,
                note: 'Binary response body captured (base64) — re-request this request_id with include_binary=true to receive it.' + sizeNote
              }, full ? { full_size_bytes: full } : {});
            }

            return Object.assign({
              included: true,
              content: rec.response_body,
              content_type: rec.content_type || null,
              encoding: bodyEncoding,
              size_bytes: returned,
              truncated: !!rec.truncated,
              note: sourceNote + sizeNote
            }, full ? { full_size_bytes: full } : {});
          })()
        };
        await censorRequestBodies(contentResult);
        sendResponse(id, action, contentResult);
        break;
      }

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown requests action: ${action}`);
    }
  } catch (error) {
    if (error.code) sendError(id, error.code, error.message);
    else sendError(id, 'API_ERROR', `Requests API error: ${error.message}`);
  }
}

// Test helper action handler
//
// The eight test.* actions read popup/options state and open tabs on request. Production
// ships config.js with BUILD_CONFIG = null; only the chromium harness bakes
// testMode: true into its per-session copy of the extension, so a Web Store install
// refuses them and a WebSocket peer cannot change that (nothing on the wire writes
// config.js). The gecko twin cannot use a baked file (its harness installs the shipped
// .xpi) and gates on the seeded test storage instead; both refuse identically.
async function testHelpersEnabled() {
  return typeof BUILD_CONFIG !== 'undefined' && !!BUILD_CONFIG &&
    typeof BUILD_CONFIG === 'object' && BUILD_CONFIG.testMode === true;
}

const TEST_ACTIONS_REFUSAL = 'test.* actions are test-harness helpers and this build does not have them ' +
  'enabled. They exist only for the integration suite, which starts the browser in a test profile; ' +
  'a normal install refuses them by design. Use the tabs, navigate and page_* tools instead.';

async function handleTestAction(id, action, data) {
  // Fail closed BEFORE the switch: a refusal here means no test.* handler can run, so a new
  // case added below is gated without anyone remembering to gate it.
  if (!(await testHelpersEnabled())) {
    sendError(id, 'TEST_ACTIONS_UNSUPPORTED', `${action}: ${TEST_ACTIONS_REFUSAL}`);
    return;
  }
  try {
    switch (action) {
      case 'test.get_popup_state':
        await handleGetPopupState(id, data);
        break;

      case 'test.get_options_state':
        await handleGetOptionsState(id, data);
        break;

      case 'test.get_storage_values':
        await handleGetStorageValues(id, data);
        break;

      case 'test.validate_ui_sync':
        await handleValidateUISync(id, data);
        break;

      case 'test.refresh_ui_state':
        await handleRefreshUIState(id, data);
        break;

      case 'test.visit_url':
        await handleVisitURL(id, data);
        break;

      case 'test.visit_multiple_urls':
        await handleVisitMultipleURLs(id, data);
        break;

      case 'test.create_test_tabs':
        await handleCreateTestTabs(id, data);
        break;

      default:
        sendError(id, 'UNKNOWN_ACTION', `Unknown test action: ${action}`);
    }
  } catch (error) {
    console.error(`Error handling test action ${action}:`, error);
    sendError(id, 'TEST_ERROR', `Test action failed: ${error.message}`, { action, error: error.toString() });
  }
}

// Get current popup display state
async function handleGetPopupState(id, data) {
  try {
    const storageConfig = await browser.storage.sync.get({
      hostname: 'localhost',
      port: 8772,
      retryInterval: 5000,
      maxRetries: -1,
      pingTimeout: 5000,
      testPort: null,
      testHostname: null
    });

    const effectiveHostname = storageConfig.testHostname || storageConfig.hostname || 'localhost';
    const effectivePort = storageConfig.testPort || storageConfig.port || 8772;
    const serverUrl = `ws://${effectiveHostname}:${effectivePort}`;
    const hasTestOverrides = storageConfig.testPort !== null || storageConfig.testHostname !== null;

    sendResponse(id, 'test.get_popup_state', {
      serverUrl: serverUrl,
      retryInterval: storageConfig.retryInterval,
      maxRetries: storageConfig.maxRetries,
      pingTimeout: storageConfig.pingTimeout,
      hasTestOverrides: hasTestOverrides,
      effectiveHostname: effectiveHostname,
      effectivePort: effectivePort,
      testIndicatorShown: hasTestOverrides,
      storageValues: storageConfig
    });
  } catch (error) {
    sendError(id, 'STORAGE_ERROR', `Failed to get popup state: ${error.message}`);
  }
}

// Get current options page display state
async function handleGetOptionsState(id, data) {
  try {
    const storageConfig = await browser.storage.sync.get({
      hostname: 'localhost',
      port: 8772,
      retryInterval: 5000,
      maxRetries: -1,
      pingTimeout: 5000,
      testPort: null,
      testHostname: null
    });

    const displayHostname = storageConfig.testHostname || storageConfig.hostname;
    const displayPort = storageConfig.testPort || storageConfig.port;
    const webSocketUrl = `ws://${displayHostname}:${displayPort}`;
    const hasTestOverrides = storageConfig.testPort !== null || storageConfig.testHostname !== null;

    sendResponse(id, 'test.get_options_state', {
      displayHostname: displayHostname,
      displayPort: displayPort,
      retryInterval: storageConfig.retryInterval,
      maxRetries: storageConfig.maxRetries,
      pingTimeout: storageConfig.pingTimeout,
      webSocketUrl: webSocketUrl,
      hasTestOverrides: hasTestOverrides,
      testOverrideWarningShown: hasTestOverrides,
      storageValues: storageConfig
    });
  } catch (error) {
    sendError(id, 'STORAGE_ERROR', `Failed to get options state: ${error.message}`);
  }
}

// Get raw storage values
async function handleGetStorageValues(id, data) {
  try {
    const storageConfig = await browser.storage.sync.get();
    sendResponse(id, 'test.get_storage_values', storageConfig);
  } catch (error) {
    sendError(id, 'STORAGE_ERROR', `Failed to get storage values: ${error.message}`);
  }
}

// Validate UI-storage synchronization
async function handleValidateUISync(id, data) {
  try {
    const { expectedValues } = data;

    const storageConfig = await browser.storage.sync.get();
    const popupState = await getPopupStateForValidation(storageConfig);
    const optionsState = await getOptionsStateForValidation(storageConfig);

    let storageMatches = true;
    const issues = [];

    if (expectedValues) {
      for (const [key, expectedValue] of Object.entries(expectedValues)) {
        if (storageConfig[key] !== expectedValue) {
          storageMatches = false;
          issues.push(`Storage ${key}: expected ${expectedValue}, got ${storageConfig[key]}`);
        }
      }
    }

    const effectiveHostname = storageConfig.testHostname || storageConfig.hostname || 'localhost';
    const effectivePort = storageConfig.testPort || storageConfig.port || 8772;

    const popupSyncValid = popupState.effectiveHostname === effectiveHostname &&
                          popupState.effectivePort === effectivePort;

    const optionsSyncValid = optionsState.displayHostname === effectiveHostname &&
                            optionsState.displayPort === effectivePort;

    if (!popupSyncValid) {
      issues.push(`Popup sync invalid: expected ${effectiveHostname}:${effectivePort}, got ${popupState.effectiveHostname}:${popupState.effectivePort}`);
    }

    if (!optionsSyncValid) {
      issues.push(`Options sync invalid: expected ${effectiveHostname}:${effectivePort}, got ${optionsState.displayHostname}:${optionsState.displayPort}`);
    }

    sendResponse(id, 'test.validate_ui_sync', {
      popupSyncValid: popupSyncValid,
      optionsSyncValid: optionsSyncValid,
      storageMatches: storageMatches,
      effectiveValues: {
        hostname: effectiveHostname,
        port: effectivePort
      },
      issues: issues
    });
  } catch (error) {
    sendError(id, 'VALIDATION_ERROR', `Failed to validate UI sync: ${error.message}`);
  }
}

// Trigger UI state refresh
async function handleRefreshUIState(id, data) {
  try {
    sendResponse(id, 'test.refresh_ui_state', {
      refreshed: true,
      popupStateUpdated: true,
      optionsStateUpdated: true,
      timestamp: new Date().toISOString()
    });
  } catch (error) {
    sendError(id, 'REFRESH_ERROR', `Failed to refresh UI state: ${error.message}`);
  }
}

async function getPopupStateForValidation(storageConfig) {
  const effectiveHostname = storageConfig.testHostname || storageConfig.hostname || 'localhost';
  const effectivePort = storageConfig.testPort || storageConfig.port || 8772;

  return {
    effectiveHostname,
    effectivePort,
    hasTestOverrides: storageConfig.testPort !== null || storageConfig.testHostname !== null
  };
}

async function getOptionsStateForValidation(storageConfig) {
  const displayHostname = storageConfig.testHostname || storageConfig.hostname;
  const displayPort = storageConfig.testPort || storageConfig.port;

  return {
    displayHostname,
    displayPort,
    hasTestOverrides: storageConfig.testPort !== null || storageConfig.testHostname !== null
  };
}

// Test Helper: Visit a URL to create browser history
async function handleVisitURL(id, data) {
  try {
    const url = data.url;
    const waitTime = data.waitTime || 8000;

    if (!url) {
      sendError(id, 'INVALID_PARAMETERS', 'URL is required for test.visit_url');
      return;
    }

    console.log(`[BrowserPlug] Starting visit to URL: ${url}`);

    const tab = await browser.tabs.create({
      url: url,
      active: false
    });

    console.log(`[BrowserPlug] Created tab ${tab.id} for URL: ${url}`);

    let tabLoaded = false;
    let loadStartTime = Date.now();
    const maxWaitTime = Math.max(waitTime, 10000);

    const tabUpdateListener = (tabId, changeInfo, updatedTab) => {
      if (tabId === tab.id && changeInfo.status === 'complete') {
        console.log(`[BrowserPlug] Tab ${tab.id} finished loading`);
        tabLoaded = true;
      }
    };

    browser.tabs.onUpdated.addListener(tabUpdateListener);

    try {
      while (!tabLoaded && (Date.now() - loadStartTime) < maxWaitTime) {
        await new Promise(resolve => setTimeout(resolve, 500));
      }

      if (tabLoaded) {
        console.log(`[BrowserPlug] Tab loaded in ${Date.now() - loadStartTime}ms`);
        console.log(`[BrowserPlug] Waiting additional time for history recording...`);
        await new Promise(resolve => setTimeout(resolve, 3000));
      } else {
        console.log(`[BrowserPlug] Tab did not complete loading within ${maxWaitTime}ms, proceeding anyway`);
        await new Promise(resolve => setTimeout(resolve, waitTime));
      }

    } finally {
      browser.tabs.onUpdated.removeListener(tabUpdateListener);
    }

    console.log(`[BrowserPlug] Closing tab ${tab.id}`);

    await browser.tabs.remove(tab.id);

    console.log(`[BrowserPlug] Successfully visited and closed: ${url}`);

    sendResponse(id, 'test.visit_url', {
      success: true,
      url: url,
      tabId: tab.id,
      visitTime: new Date().toISOString(),
      loadTime: Date.now() - loadStartTime,
      tabLoaded: tabLoaded,
      message: `Successfully visited ${url} (loaded: ${tabLoaded})`
    });

  } catch (error) {
    sendError(id, 'VISIT_URL_ERROR', `Failed to visit URL: ${error.message}`);
  }
}

// Test Helper: Visit multiple URLs to create test history
async function handleVisitMultipleURLs(id, data) {
  try {
    const urls = data.urls || [];
    const waitTime = data.waitTime || 8000;
    const delayBetween = data.delayBetween || 3000;

    if (!Array.isArray(urls) || urls.length === 0) {
      sendError(id, 'INVALID_PARAMETERS', 'urls array is required for test.visit_multiple_urls');
      return;
    }

    console.log(`[BrowserPlug] Starting visit to ${urls.length} URLs`);
    const results = [];

    for (let i = 0; i < urls.length; i++) {
      const url = urls[i];
      console.log(`[BrowserPlug] Visiting URL ${i + 1}/${urls.length}: ${url}`);

      try {
        const tab = await browser.tabs.create({
          url: url,
          active: false
        });

        let tabLoaded = false;
        let loadStartTime = Date.now();
        const maxWaitTime = Math.max(waitTime, 10000);

        const tabUpdateListener = (tabId, changeInfo, updatedTab) => {
          if (tabId === tab.id && changeInfo.status === 'complete') {
            console.log(`[BrowserPlug] Tab ${tab.id} finished loading URL ${i + 1}`);
            tabLoaded = true;
          }
        };

        browser.tabs.onUpdated.addListener(tabUpdateListener);

        try {
          while (!tabLoaded && (Date.now() - loadStartTime) < maxWaitTime) {
            await new Promise(resolve => setTimeout(resolve, 500));
          }

          if (tabLoaded) {
            console.log(`[BrowserPlug] URL ${i + 1} loaded in ${Date.now() - loadStartTime}ms`);
            await new Promise(resolve => setTimeout(resolve, 2000));
          } else {
            console.log(`[BrowserPlug] URL ${i + 1} did not complete loading within ${maxWaitTime}ms`);
            await new Promise(resolve => setTimeout(resolve, waitTime));
          }

        } finally {
          browser.tabs.onUpdated.removeListener(tabUpdateListener);
        }

        await browser.tabs.remove(tab.id);

        results.push({
          url: url,
          success: true,
          tabId: tab.id,
          visitTime: new Date().toISOString(),
          loadTime: Date.now() - loadStartTime,
          tabLoaded: tabLoaded
        });

        if (i < urls.length - 1) {
          await new Promise(resolve => setTimeout(resolve, delayBetween));
        }

      } catch (error) {
        results.push({
          url: url,
          success: false,
          error: error.message
        });
      }
    }

    const successCount = results.filter(r => r.success).length;

    sendResponse(id, 'test.visit_multiple_urls', {
      success: true,
      totalUrls: urls.length,
      successfulVisits: successCount,
      failedVisits: urls.length - successCount,
      results: results,
      message: `Visited ${successCount}/${urls.length} URLs successfully`
    });

  } catch (error) {
    sendError(id, 'VISIT_MULTIPLE_URLS_ERROR', `Failed to visit multiple URLs: ${error.message}`);
  }
}

// Test Helper: Create test tabs for testing tabs.list functionality
async function handleCreateTestTabs(id, data) {
  try {
    const count = data.count || 3;
    const baseUrls = data.urls || [
      'https://example.com',
      'https://httpbin.org/html',
      'https://httpbin.org/json'
    ];
    const closeExisting = data.closeExisting || false;

    if (closeExisting) {
      const existingTabs = await browser.tabs.query({});
      const tabsToClose = existingTabs.filter(tab => !tab.pinned && tab.url !== 'about:blank');

      if (tabsToClose.length > 0) {
        await browser.tabs.remove(tabsToClose.map(tab => tab.id));
      }
    }

    const createdTabs = [];

    for (let i = 0; i < count; i++) {
      const url = baseUrls[i % baseUrls.length];
      const testUrl = `${url}?test=tab${i + 1}&timestamp=${Date.now()}`;

      try {
        const tab = await browser.tabs.create({
          url: testUrl,
          active: i === 0
        });

        createdTabs.push({
          id: tab.id,
          url: testUrl,
          title: `Test Tab ${i + 1}`,
          active: tab.active,
          index: tab.index
        });

        if (i < count - 1) {
          await new Promise(resolve => setTimeout(resolve, 500));
        }
      } catch (error) {
        console.error(`Failed to create test tab ${i + 1}:`, error);
      }
    }

    await new Promise(resolve => setTimeout(resolve, 1000));

    const allTabs = await browser.tabs.query({});

    sendResponse(id, 'test.create_test_tabs', {
      success: true,
      message: `Successfully created ${createdTabs.length} test tabs`,
      createdTabs: createdTabs,
      totalTabsAfter: allTabs.length,
      tabsCreated: createdTabs.length,
      timestamp: new Date().toISOString()
    });

  } catch (error) {
    sendError(id, 'CREATE_TABS_ERROR', `Failed to create test tabs: ${error.message}`);
  }
}

// ✅ INITIALIZATION: Load config and connect. Runs on EVERY service-worker start
// (initial install, browser start, and every SW wake after an eviction). All chrome.*
// event listeners above are registered SYNCHRONOUSLY at the top level (MV3 requirement)
// BEFORE this async work begins, so a wake triggered by any event has its listeners in
// place. connectToMCPServer() guards against a concurrent connect on rapid wake-ups.
console.log('🚀 Extension starting - loading configuration...');

async function initializeExtension() {
  try {
    console.log('🚀 Initializing extension...');
    updateActionIndicator(); // show an initial toolbar state immediately
    console.log('📍 BEFORE loadConfig() - CONFIG:', JSON.stringify(CONFIG, null, 2));

    await loadConfig();

    console.log('✅ Configuration loaded successfully');
    console.log('📍 AFTER loadConfig() - CONFIG:', JSON.stringify(CONFIG, null, 2));

    // Re-arm any monitor definitions that survived an SW eviction (design D1).
    await restoreMonitorDefs();

    console.log('🔌 Connecting to MCP server...');
    connectToMCPServer();

  } catch (error) {
    console.error('❌ Failed to initialize extension:', error);
    console.log('🔄 Will retry initialization in 1 second...');
    setTimeout(() => {
      initializeExtension();
    }, 1000);
  }
}

// Start initialization.
initializeExtension();

// SW wake sources on browser start / install (the top-level initializeExtension()
// above does the real work when the resulting SW start evaluates this script; these
// listeners exist so those events actually WAKE the SW). Kept side-effect-light to
// avoid the config-race the gecko onStartup handler was removed for.
if (browser.runtime && browser.runtime.onStartup) {
  browser.runtime.onStartup.addListener(() => {
    console.log('⏰ runtime.onStartup; service worker waking on browser start');
    // The setting outlives the browser; the Tor it pointed at did not.
    clearBrowserProxy();
  });
}
if (browser.runtime && browser.runtime.onInstalled) {
  browser.runtime.onInstalled.addListener(() => {
    console.log('📦 runtime.onInstalled');
    clearBrowserProxy();
    setupContextMenu();
  });
}

// Self-heal watchdog: chrome.alarms is the canonical MV3 keep-alive/wake. Period 0.5
// min (the MV3 minimum accepted at our Chrome floor). On alarm: reconnect if down, or
// refresh diagnostic state + ensure the health probe is running if up.
try {
  browser.alarms.create('browserplug-reconnect-watchdog', { periodInMinutes: 0.5 });
  browser.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name !== 'browserplug-reconnect-watchdog') return;
    if (stoppedByUser) return; // user explicitly stopped; don't auto-revive
    if (!websocket || websocket.readyState !== WebSocket.OPEN) {
      // A service worker that died with its socket never saw onclose: finish that here.
      clearBrowserProxy();
      console.log('⏰ Watchdog: not connected; re-initiating connection');
      journalNote('reconnect', { reason: 'watchdog' });
      retryAttempts = 0;
      connectToMCPServer();
    } else {
      sendExtensionHello();
      if (!healthProbeTimer) startHealthProbe();
    }
  });
} catch (e) {
  console.error('Could not set up reconnect watchdog:', e);
}

// --- UI surface routing ----------------------------------------------------
// The toolbar button opens the BrowserPlug interface (popup/popup.html) in one of four
// surfaces (uiSurface in storage.sync):
//   popup    : the default action panel (closes on blur) (default)
//   detached : a standalone popup window that stays open on blur
//   sidebar  : Chromium side panel (chrome.sidePanel; gecko used sidebar_action))
//   tab      : the interface opened as an ordinary tab
// For every surface EXCEPT 'popup' we clear the action popup so action.onClicked fires.
let uiSurface = 'popup';
let detachedWindowId = null;

function applyUiSurface() {
  const ba = browser.action;
  if (!ba || !ba.setPopup) return;
  try {
    ba.setPopup({ popup: uiSurface === 'popup' ? 'popup/popup.html' : '' });
  } catch (e) {
    console.error('applyUiSurface failed:', e);
  }
}

async function openDetachedInterface() {
  const url = browser.runtime.getURL('popup/popup.html');
  if (detachedWindowId != null) {
    try {
      await browser.windows.update(detachedWindowId, { focused: true });
      return;
    } catch (e) {
      detachedWindowId = null; // it was closed since we last saw it
    }
  }
  try {
    const win = await browser.windows.create({ url, type: 'popup', width: 400, height: 640 });
    detachedWindowId = win.id;
  } catch (e) {
    console.error('openDetachedInterface failed:', e);
  }
}

async function openInterfaceTab() {
  const url = browser.runtime.getURL('popup/popup.html');
  try {
    let existing = [];
    try { existing = await browser.tabs.query({ url }); } catch (e) { /* fall back to create */ }
    if (existing && existing.length) {
      await browser.tabs.update(existing[0].id, { active: true });
      if (existing[0].windowId != null) {
        await browser.windows.update(existing[0].windowId, { focused: true });
      }
      return;
    }
    await browser.tabs.create({ url });
  } catch (e) {
    console.error('openInterfaceTab failed:', e);
  }
}

if (browser.action && browser.action.onClicked) {
  browser.action.onClicked.addListener((tab) => {
    // Only fires when the popup is cleared (uiSurface !== 'popup').
    if (uiSurface === 'sidebar') {
      // chrome.sidePanel.open() must run in the click gesture; needs a windowId.
      try {
        if (browser.sidePanel && browser.sidePanel.open) {
          browser.sidePanel.open({ windowId: tab && tab.windowId }).catch((e) =>
            console.error('sidePanel.open failed:', e));
        }
      } catch (e) {
        console.error('sidePanel.open failed:', e);
      }
      return;
    }
    if (uiSurface === 'tab') { openInterfaceTab(); return; }
    openDetachedInterface(); // 'detached'
  });
}

if (browser.windows && browser.windows.onRemoved) {
  browser.windows.onRemoved.addListener((id) => {
    if (id === detachedWindowId) detachedWindowId = null;
  });
}

browser.storage.onChanged.addListener((changes, area) => {
  if (area === 'sync' && changes.uiSurface) {
    uiSurface = changes.uiSurface.newValue || 'popup';
    applyUiSurface();
  }
});

(async () => {
  try {
    const { uiSurface: s } = await browser.storage.sync.get({ uiSurface: 'popup' });
    uiSurface = s || 'popup';
  } catch (e) {
    console.error('Loading uiSurface failed:', e);
  }
  applyUiSurface();
})();

// --- Toolbar-button context menu -------------------------------------------
// An "Options" item on the toolbar button's right-click menu (MV3: menus ->
// contextMenus; context 'browser_action' -> 'action'). Context menus don't persist
// across SW restarts, so recreate on startup/install; removeAll first avoids the
// duplicate-id warning on a re-wake.
const OPTIONS_MENU_ID = 'browserplug-open-options';
function setupContextMenu() {
  if (!(browser.contextMenus && browser.contextMenus.create)) return;
  try {
    browser.contextMenus.removeAll(() => {
      try {
        browser.contextMenus.create({
          id: OPTIONS_MENU_ID,
          title: (browser.i18n && browser.i18n.getMessage('toolbarMenuOptions')) || 'Options',
          contexts: ['action'],
        });
      } catch (e) {
        console.error('Creating toolbar context menu failed:', e);
      }
    });
  } catch (e) {
    console.error('contextMenus.removeAll failed:', e);
  }
}
if (browser.contextMenus && browser.contextMenus.onClicked) {
  browser.contextMenus.onClicked.addListener((info) => {
    if (info.menuItemId === OPTIONS_MENU_ID) {
      browser.runtime.openOptionsPage().catch((e) =>
        console.error('openOptionsPage failed:', e));
    }
  });
}
// Also create it now (covers a plain SW wake that isn't onInstalled/onStartup).
setupContextMenu();
