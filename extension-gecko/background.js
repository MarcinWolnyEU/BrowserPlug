/*
 * BrowserPlug Firefox Extension - Background Script
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 */

// SINGLE CONNECTION CONSTRAINT: Only one WebSocket connection to MCP server allowed
let websocket = null;
let isConnected = false;


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
    // Always log to console
    originalConsoleLog(...args);

    const logMessage = createLogMessage("log", args);

    // Try to send immediately, otherwise buffer it
    if (!sendLogMessage(logMessage)) {
      logBuffer.push(logMessage);
      // Keep buffer size reasonable
      if (logBuffer.length > 100) {
        logBuffer = logBuffer.slice(-50);
      }
    }
  }

  function enhancedError(...args) {
    // Always log to console
    originalConsoleError(...args);

    const logMessage = createLogMessage("error", args);

    // Try to send immediately, otherwise buffer it
    if (!sendLogMessage(logMessage)) {
      logBuffer.push(logMessage);
      // Keep buffer size reasonable
      if (logBuffer.length > 100) {
        logBuffer = logBuffer.slice(-50);
      }
    }
  }

  // Replace console methods with enhanced versions
  console.log = enhancedLog;
  console.error = enhancedError;

  // Export flush function to be called when WebSocket connects
  window.flushDebugLogBuffer = flushLogBuffer;
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

// Server health verified OVER the WebSocket, not just "socket is OPEN". A crashed
// or wedged server can leave a half-open socket that still reads OPEN, so we send a
// tiny request and require a timely response before calling the link green.
//   null  = unknown / first probe in flight
//   true  = server answered a health probe recently
//   false = socket claims OPEN but the server is not answering (wedged / half-open)
let serverHealthy = null;
let lastHealthOkAt = null;          // epoch ms of the last successful probe
let healthProbeTimer = null;        // setInterval handle while connected
let healthProbeFailures = 0;        // consecutive failures (drives the auto-reconnect)
let healthOkThisConnection = false; // has any probe succeeded since this socket opened?
// The server's REAL MCP HTTP port, learned over the WS health probe. The WS is on
// the correct port regardless of any HTTP-side drift, so this is authoritative and
// lets the Health pane self-correct a stale/guessed port. null until first learned.
let lastServerMcpPort = null;
const HEALTH_PROBE_INTERVAL_MS = 15000;
const HEALTH_PROBE_FAILURES_BEFORE_RECONNECT = 2;

// --- Live toolbar indicator ------------------------------------------------
// Reflect the connection phase on the toolbar ICON and tooltip so the state is
// visible WITHOUT opening the popup. The six phases collapse onto the four
// icon states here (connected: plug seated, green; unhealthy: seated, amber;
// connecting: pulled back, amber; disconnected: pulled clear, red; see
// assets/icons/README.md), and toolbar-icon.js owns everything downstream of
// that, including which of the two
// Firefox glyph sets reads against the toolbar. The tooltip (hover) carries what
// the icon cannot: which URL, which attempt, why a live socket is not counted
// healthy.
//
// The badge carries no TEXT. Its glyphs ("...", a dot, the retry count) said what
// the icon says now, and a button that says it twice is just noisier. The badge
// COLOUR is still written, and still means what it always meant: with the text
// empty nothing renders it, and the popup reads it back as the one piece of
// connection state it can have synchronously on first paint, with no message
// round-trip that could momentarily paint a false "Disconnected" -- see
// renderConnection() in popup/popup.js.
//
// So toolbar-icon.js is no longer optional in practice: without it the button
// keeps its default picture and only the hover text moves.
function updateActionIndicator() {
  const ba = browser.browserAction;
  if (!ba || !ba.setBadgeText) return; // API unavailable (e.g. during teardown)

  const wsUrl = `ws://${CONFIG.hostname}:${CONFIG.port}`;
  let color, title, icon;
  switch (connectionPhase) {
    case 'connected':
      if (serverHealthy === false) {
        // Socket is up but the server isn't answering health probes. The plug stays
        // SEATED and turns amber: plugged in with nothing answering is a different
        // thing to say than the pulled-back plug's "still dialling".
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

// Single choke point for phase changes so the toolbar indicator always tracks
// the real state. Set nextRetryAt / retryAttempts BEFORE calling this wherever
// the icon or tooltip depends on them.
function setConnectionPhase(phase) {
  connectionPhase = phase;
  updateActionIndicator();
  // While waiting, tick the tooltip every second so the "next try in ~Xs"
  // countdown stays live on the toolbar (and in the popup, which mirrors it).
  if (phase === 'waiting') {
    if (!waitingTicker) waitingTicker = setInterval(updateActionIndicator, 1000);
  } else if (waitingTicker) {
    clearInterval(waitingTicker);
    waitingTicker = null;
  }
}

function connectToMCPServer() {
  try {
    // We ARE the reconnect: any retry still armed (by scheduleReconnect) is now
    // redundant, and letting it fire would race a second attempt against this one.
    // The watchdog and the popup's Reconnect both land here with a timer pending.
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

    // IMPORTANT: Only one WebSocket connection is allowed at a time
    // Disconnect any existing connection first to prevent multiple connections
    if (websocket && websocket.readyState !== WebSocket.CLOSED) {
      console.log('🔌 Disconnecting existing connection before creating new one');
      disconnect();
    }

    // Compute WebSocket URL dynamically using current config
    const WS_URL = `ws://${CONFIG.hostname}:${CONFIG.port}`;
    console.log(`🔗 Connecting to ${WS_URL} (attempt ${retryAttempts + 1})`);

    nextRetryAt = null;
    // Re-verify health from scratch on every (re)connect attempt.
    serverHealthy = null;
    healthProbeFailures = 0;
    healthOkThisConnection = false;
    lastServerMcpPort = null;
    handshakeToken = null;
    setConnectionPhase('connecting');
    websocket = new WebSocket(WS_URL);

    // A socket we replaced still fires its events LATE (close in particular arrives
    // after the successor is already open). Every handler below therefore bails out
    // unless it is still THE current socket; otherwise a dead socket's onclose
    // would clear isConnected, kill the new connection's health probe and schedule
    // a redundant reconnect on top of a perfectly healthy link.
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
      retryAttempts = 0; // Reset retry counter on successful connection
      connectionRetryAttempts = 0; // Reset connection retry counter
      stoppedByUser = false;
      nextRetryAt = null;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      setConnectionPhase('connected');

      // Announce the extension version + diagnostic state (surfaced on /health).
      // The server gives the hello a deadline and drops a socket that misses it, so
      // this sends from the cache when there is one and awaits nothing.
      sendExtensionHelloNow();
      // …and what this browser currently holds (window/tab counts for the map).
      sendBrowserState();

      // Start verifying server health over the WS. The icon stays amber and the
      // tooltip says "verifying" until the first probe answers; then green, or
      // amber still if the server is up at the socket level but not responding.
      startHealthProbe();

      // Send an immediate debug message to test after a small delay
      setTimeout(() => {
        console.log('🔌 WebSocket connection established and ready');
      }, 200);

      // Flush any buffered debug logs
      if (typeof window.flushDebugLogBuffer === 'function') {
        setTimeout(() => {
          window.flushDebugLogBuffer();
        }, 100);
      }
    };

    ws.onmessage = (event) => {
      if (websocket !== ws || !w) return;
      w.receive(event.data);
    };

    ws.onclose = (event) => {
      if (w) w.socketClosed(event && event.code, event && event.reason);
      if (websocket !== ws) return; // superseded socket; its successor owns the state
      console.log('Disconnected from MCP server');
      isConnected = false;
      stopHealthProbe();
      serverHealthy = null;
      // 4401: the server wants a token we didn't present. Its close reason tells a
      // same-machine browser where to fetch the one it minted; handleUnauthorized owns
      // the reconnect from here (it may reconnect at once, or back off like anything else).
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
// a diagnostic file failed to load would be the wedge this exists to explain.
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
// No Firefox lets an extension OPEN file:// itself; this is about reading a
// file:// tab the user opened. Firefox < 153: granted automatically by the
// required <all_urls> host permission. Firefox 153+: gated by the per-extension
// opt-in, read accurately via extension.isAllowedFileSchemeAccess().
// (permissions.contains({origins:['file:///*']}) is deliberately NOT used/reported:
// it just echoes <all_urls> and stays true even when file reading is off.)
async function getFileAccessState() {
  let browserMajor = null;
  try {
    if (browser.runtime.getBrowserInfo) {
      const info = await browser.runtime.getBrowserInfo();
      browserMajor = parseInt(info.version, 10);
    }
  } catch (e) {
    console.error('getBrowserInfo failed:', e);
  }

  let canReadFiles = null;
  if (browserMajor !== null && browserMajor < 153) {
    canReadFiles = true; // <all_urls> (required, always granted) enables it pre-153
  } else {
    try {
      if (browser.extension && browser.extension.isAllowedFileSchemeAccess) {
        canReadFiles = await browser.extension.isAllowedFileSchemeAccess();
      }
    } catch (e) {
      console.error('isAllowedFileSchemeAccess failed:', e);
    }
  }

  return { can_read_files: canReadFiles, browser_major: browserMajor };
}

// --- Browser identity -------------------------------------------------------
// The server keys its browser registry (multiple Firefoxes on one server) on this
// id, so it must be STABLE across a Firefox restart AND an extension reload;
// hence minted once and persisted.
// storage.LOCAL, never storage.sync: Firefox Sync propagates sync'd values to the
// user's OTHER profiles/machines, which would give two live browsers the same
// identity and collapse them into a single registry entry.
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
      instanceIdPromise = null; // never cache a failed mint; the next hello retries it
      throw e;
    });
  }
  return instanceIdPromise;
}

// --- Which browser is this, really? -----------------------------------------
// A Firefox FORK runs this extension unchanged and will NOT admit that it is one:
// Zen answers runtime.getBrowserInfo() with {name: "Firefox", version: "152.0.5"}
// (the Firefox it is BUILT ON; its own version is not exposed to extensions at all)
// and serves a stock Firefox user-agent, with navigator.buildID frozen to Mozilla's
// anti-fingerprinting placeholder. All live-verified on Zen 152.
//
// The browser's own branding (chrome://branding/locale/brand.properties, which does
// say "Zen Browser") is NOT reachable from here: Firefox refuses the load outright --
//   "Content at moz-extension://…/_generated_background_page.html may not load or
//    link to chrome://branding/locale/brand.properties"
// -- and the same for every resource:// path to it. An extension page is "content"
// for that check. Don't try again; it is a platform rule, not a URL we got wrong.
//
// So the hello sends everything the API WILL tell us (vendor and buildID included --
// a fork that fills in `vendor` is the one self-report left), and naming a LOCAL
// browser is the server's job: it probes the process (gecko_brand in server.py).

// Identity + display info for the server's browser roster (a remote browser has no
// PID/profile for the server to probe, so the naming material must come from here).
// Never throws: a missing piece degrades to null rather than costing us the whole
// hello, which also carries the file/proxy access state the server acts on.
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
    if (browser.runtime.getBrowserInfo) {
      const b = await browser.runtime.getBrowserInfo();
      // The WHOLE object, not the two fields we happen to know about. A fork lies on
      // `name` and `vendor` but adds a key of its OWN, and that key is the only thing
      // that outs it; Zen 152 answers:
      //   {name:"Firefox", vendor:"Mozilla", version:"152.0.5",
      //    buildID:"20260708113753", zen:{version:"1.21.6b"}}
      // Hand-picking fields threw that away. The server reads it (fork_from_browser_info).
      identity.browserInfo = Object.assign({}, b);
    }
  } catch (e) {
    console.error('getBrowserInfo failed:', e);
  }
  return identity;
}

// --- The WebSocket token ----------------------------------------------------
// The server always has one (it mints and persists one on first run). Two ways we can
// hold it, in precedence order:
//
//   1. `connectionToken` in storage.SYNC; typed by the user into the options page. It is
//      the ONLY way a browser on ANOTHER machine can be configured: that browser cannot
//      reach the server's localhost-only token endpoint. Explicit user intent wins.
//   2. `autoToken` in storage.LOCAL; pulled from the server itself after it closed us
//      with 4401 (handleUnauthorized). Zero user action for a same-machine browser.
//
// The auto-pulled one lives in storage.LOCAL and never in storage.sync: Firefox Sync
// propagates sync'd values to the user's OTHER machines/profiles, and this is a secret
// belonging to ONE server on ONE machine; syncing it would leak it off this box.
//
// Both are deliberately kept OUT of CONFIG (which is console-logged and handed to the
// popup wholesale) and are never logged.
const AUTO_TOKEN_KEY = 'autoToken';
const WS_CLOSE_UNAUTHORIZED = 4401;          // the server's "you need a token" close code
const TOKEN_ENDPOINT_LOOPBACK_HOSTS = new Set(['127.0.0.1', '::1', 'localhost']);
const MAX_TOKEN_BOOTSTRAP_ATTEMPTS = 3;      // then back off; never a fetch loop
let tokenBootstrapAttempts = 0;              // reset once a health probe answers (= we're in)
let lastAuthError = null;                    // surfaced in the connection-status payload

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

// The 4401 close reason names the server's token endpoint, including its REAL MCP port,
// which we cannot guess (tests and multi-server setups use dynamic ports). Only a LOOPBACK
// http URL is ever honoured: a token fetched from anywhere else would be a stranger's
// token, and the very next thing we would do with it is present it to a server.
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

// The server closed us with 4401: it wants a token we don't have. If the server is on THIS
// machine it will have told us where to get it; fetch it once and come straight back.
// Bounded on every axis: a server that keeps rejecting us must never become a fetch loop.
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
    // The user typed a token and the server said no. Silently replacing it would hide a
    // real misconfiguration (and break "manual wins"), so surface it and back off.
    console.error(`${rejected}. ` +
                  'The token set in Options was rejected by the server. Check it against ' +
                  'the value the server printed, or clear it, and a browser on the same ' +
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
    scheduleReconnect();   // remote server, or an old one with no endpoint to offer
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
    // We already held exactly this token and were rejected anyway; refetching it cannot
    // change the outcome, so stop pulling and let the backoff do its job.
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
  connectToMCPServer();   // immediately: we now have exactly what the server asked for
}

// Which optional platform APIs this browser actually has, probed rather than inferred
// from a version string. A fork reports the Firefox it is BUILT ON, and forks trail that
// build by their own release cadence, so "Firefox 153 has getDocumentId, therefore this
// browser does" is a guess. Feature detection is the only honest answer, and it is what
// the code paths themselves branch on.
//
// Whether a CONTENT script has been observed reporting a documentId. Assigned by
// noteContentDocIdSupport (down by runAutomation) the first time the toolkit answers, and
// declared HERE so it sits inside the same region as its reader - the hello slice and the
// runAutomation slice are taken separately by the JS twins, and a reference that escapes
// its slice breaks the harness rather than the product. null = not yet observed.
let CONTENT_DOC_ID_SUPPORT = null;

// documentId (Firefox 153+, Chromium since MV3): a stable per-document identity.
// frameId is NOT one - ids are reused, and a navigation replaces the document underneath
// the same frameId, which is why a stale snapshot ref cannot currently be told apart
// from one whose element simply went away.
function getApiSupport() {
  // Read through a local alias on purpose. addons-linter checks every literal
  // `browser.runtime.<name>` chain against strict_min_version and reports this one as
  // unsupported on Firefox 140 (INCOMPATIBLE_API) although the call is feature-detected
  // and never made where the API is missing; the alias says the same thing to a reader
  // and nothing to the linter.
  const rt = browser.runtime;
  return {
    // Probed in the BACKGROUND context.
    documentId: !!rt && typeof rt.getDocumentId === 'function',
    // Probed where it is actually USED. The background answer is a necessary condition,
    // not a sufficient one: the features that depend on documentId run in content scripts
    // (automation.js stamping snapshot results, and the wrapper around page_eval), and a
    // context can lack an API the background has. Reporting only the background reading
    // would claim a working capability on a browser where the content side comes back
    // empty - the same shape of confidently-wrong answer this probe exists to avoid.
    // null until a content script has actually reported once; that is UNKNOWN, not false.
    documentIdInContent: CONTENT_DOC_ID_SUPPORT
  };
}

// --- The hello, and why it is cached -----------------------------------------
// Four of the hello's five inputs are IPC or disk round trips: getFileAccessState()
// (runtime.getBrowserInfo + extension.isAllowedFileSchemeAccess), getProxyPermissionState(),
// getBrowserIdentity() (storage.local + getPlatformInfo + getBrowserInfo) and
// getConnectionToken() (storage.sync + storage.local). Awaiting all four before the frame
// goes out costs nothing on a healthy browser and is the whole hello budget on one that is
// thrashing: on 2026-09-21 two reconnects opened their sockets and were dropped for a hello
// the server never received.
//
// So the answers are kept. Once a hello has been built, a reconnect sends from that cache
// synchronously out of onopen and refreshes it afterwards for the next time. The first-ever
// connect has nothing cached and takes the live path, which is also what the watchdog runs
// while connected — that periodic hello exists to re-report a file-access opt-in, so it must
// read the toggle rather than the memory of it.
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
    fileAccess: inputs.fileAccess,
    proxyAccess: inputs.proxyAccess,
    // Read live on every hello: it is synchronous, and a content script may have
    // reported documentId support since the cache was built.
    apiSupport: getApiSupport(),
    instance_id: identity.instance_id,
    platform: identity.platform,
    // What the browser CLAIMS to be. A fork says "Firefox" here (Zen does), so the
    // server only trusts this for a REMOTE browser, where it has no process to probe.
    browserInfo: identity.browserInfo,
    // "Reject me with a /ws-token URL and I will fetch it and come back." The server
    // needs this to tell us apart from an extension too old to bootstrap a token, which
    // it must let in rather than brick (see _authenticate_extension).
    tokenBootstrap: true
  };
  // Omitted entirely when unset; a server that requires one must not see an
  // empty string and take it for an answer.
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
// block against a browser object that carries only the two storage areas.
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
  });
}

function scheduleReconnect() {
  // Never let two retry timers run at once; they would double the attempt rate
  // and each fire its own connectToMCPServer().
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

  if (stoppedByUser) {
    nextRetryAt = null;
    setConnectionPhase('stopped');
    return;
  }
  retryAttempts++;

  // Check configured max retries
  if (CONFIG.maxRetries > 0 && retryAttempts > CONFIG.maxRetries) {
    nextRetryAt = null;
    setConnectionPhase('gaveup');
    console.error(`Configured max retry attempts (${CONFIG.maxRetries}) exceeded. Stopping reconnection attempts.`);
    return;
  }

  // Absolute safety cap only applies when a FINITE retry limit is configured.
  // With the default infinite setting (maxRetries = -1) we retry forever, so the
  // extension always recovers on its own once the server is back up.
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

// Function to update configuration (can be called from popup or other scripts).
// `persist` writes the new values back to storage; the "Reset to Defaults" path
// passes false so storage stays empty (options.js has already cleared it) while
// the running session still adopts the defaults.
function updateConfig(newConfig, persist = true) {
  Object.assign(CONFIG, newConfig);
  console.log('Configuration updated:', CONFIG);
  console.log('New WebSocket URL will be:', `ws://${CONFIG.hostname}:${CONFIG.port}`);

  // Keep the toolbar tooltip's URL fresh after a settings change.
  updateActionIndicator();

  // Save to storage for persistence
  if (persist) {
    browser.storage.sync.set({
      hostname: CONFIG.hostname,
      port: CONFIG.port,
      retryInterval: CONFIG.retryInterval,
      maxRetries: CONFIG.maxRetries,
      pingTimeout: CONFIG.pingTimeout
    });
  }

  // Reconnect with new settings if currently connected
  if (isConnected || websocket) {
    console.log('Reconnecting with new configuration...');
    disconnect();
    connectToMCPServer();
  }
}

// Load configuration from storage on startup
// The MCP HTTP port setting as a usable number, or null (unset, junk, out of range).
function validPort(value) {
  const n = Number(value);
  return Number.isInteger(n) && n >= 1 && n <= 65535 ? n : null;
}

async function loadConfig() {

  // Load from storage - browser.storage.sync.get() always succeeds
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

  // Check if we're in a test environment waiting for configuration
  const isTestEnvironment = result.testPort !== null || result.testHostname !== null;
  const hasValidTestConfig = result.testPort && result.testPort !== 8772;

  // In test environment, wait longer for configuration to be ready
  if (isTestEnvironment && !hasValidTestConfig) {
    throw new Error('Test environment detected but configuration not ready');
  }

  // Apply configuration with test overrides taking priority
  CONFIG.hostname = result.testHostname || result.hostname;
  CONFIG.port = result.testPort || result.port;
  CONFIG.retryInterval = result.retryInterval;
  CONFIG.maxRetries = result.maxRetries;
  CONFIG.pingTimeout = result.pingTimeout;
  CONFIG.mcpPort = validPort(result.mcpPort);

  const overridden = (result.testPort || result.testHostname) ? ' (test overrides active)' : '';
  console.log(`📋 Config loaded: ws://${CONFIG.hostname}:${CONFIG.port}${overridden}`);
}

// Disconnect function
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
// A socket in readyState OPEN does NOT prove the server is alive; a crashed or
// wedged server can leave a half-open socket that still reads OPEN. We send a
// tiny `server.health` request and require a timely response before classifying
// the connection green; otherwise the icon stays amber, and after a couple
// of failures we drop the socket so the reconnect loop can re-establish (or
// honestly show "reconnecting") instead of sitting falsely green.
async function probeServerHealth() {
  // A probe AWAITS a response, so the socket it was launched on can be replaced
  // (reconnect, config change) while it is in flight. Its late answer must not
  // touch the successor's health state; a stale failure would otherwise count
  // toward the NEW connection's "2 failures" and drop a perfectly healthy socket.
  const ws = websocket;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const timeout = Math.max(2000, CONFIG.pingTimeout || 5000);
  try {
    const data = await requestFromServer('server.health', {}, timeout);
    if (websocket !== ws) return; // superseded mid-probe; the successor owns the state
    serverHealthy = true;
    healthProbeFailures = 0;
    healthOkThisConnection = true;
    lastHealthOkAt = Date.now();
    // The server is answering us: this socket is authenticated. Clear the token-bootstrap
    // budget so a LATER 4401 (the user rotated the token) gets a fresh chance to
    // self-configure instead of inheriting an exhausted counter.
    tokenBootstrapAttempts = 0;
    lastAuthError = null;
    // Remember the server's actual MCP HTTP port so the Health pane can reconcile
    // a drifted/guessed port against it.
    if (data && typeof data.mcp_port === 'number') lastServerMcpPort = data.mcp_port;
    markTokenProven(data && data.mcp_port);
    // Refresh the browser's window/tab counts on the same tick; the event
    // listeners cover the common changes, this covers everything else (and any
    // push lost while the socket was down) without a timer of its own.
    sendBrowserState();
    updateActionIndicator();
  } catch (e) {
    if (websocket !== ws) return; // superseded mid-probe; this failure was the old socket's
    healthProbeFailures++;
    serverHealthy = false;
    updateActionIndicator();
    console.warn(`Server health probe failed (${healthProbeFailures}): ${e && e.message}`);
    // Only force a reconnect once we KNOW this server answers health probes and it
    // has since gone silent; that's the half-open/wedged case reconnecting fixes.
    // If no probe ever succeeded on this socket (e.g. a server too old to know the
    // action), stay put and just show amber rather than thrashing the connection.
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
  probeServerHealth(); // verify immediately, don't wait a full interval
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
// to us. Sent on connect, on every health-probe tick (~15s) and (debounced)
// whenever a tab or window comes or goes, so no timer of its own is needed.
const BROWSER_STATE_DEBOUNCE_MS = 800;
let browserStateTimer = null;

// The foreground tab of each window is listed individually (the count alone said
// nothing: with one window it is forever "1 active"). Bounded like every other
// buffer here - a browser with dozens of windows must not push an unbounded list.
const ACTIVE_TAB_LIST_MAX = 12;
const ACTIVE_TAB_TITLE_MAX = 120;   // the tooltip truncates for display; keep some room

// The most recent thing we can honestly say happened in a tab: the last main-frame
// navigation we recorded, or the last time the user focused it, whichever is newer.
// null when neither is known (a tab restored into a fresh session has no history
// here). `lastAccessed` comes from tabLastAccessedMs, so a browser that does not
// keep one of its own gets the same answer here as `tabs list` gives.
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

// Firefox for ANDROID implements no windows API whatever: every member of it reads
// android:false in browser-compat-data, and `browser.windows` is simply undefined there,
// so touching it throws. That matters far beyond windows, because one unguarded touch at
// TOP LEVEL (reseedLastFocusedWindow, below) aborted the whole background script on a
// phone. Every `const` after it was then permanently in the temporal dead zone and every
// listener after it went unregistered; function declarations hoist, so the extension kept
// answering and the damage surfaced hundreds of lines away as unrelated feature bugs (a
// screenshot failing on VISIBLE_CAPTURE_TIMEOUT_MS, the whole uiSurface block never
// running, no reconnect watchdog). Every use is guarded, each site for itself.
async function collectBrowserState() {
  const [wins, tabs] = await Promise.all([
    // null, not [], when there is no windows API: the popup reads null as "this extension
    // never said" and 0 as "none", and a phone has tabs without windows.
    browser.windows ? browser.windows.getAll({ populate: false }) : Promise.resolve(null),
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
    windows: wins ? wins.length : null,
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

// Registered unconditionally at top level (the MV3 twin needs that; here it is
// merely harmless): every handler is a no-op while the socket is down.
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

  // Responses to requests this extension sent to the server (e.g. server.get_connections)
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

  // Route actions to appropriate handlers (all are now async)
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
  // Check WebSocket state directly instead of relying on isConnected flag
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
// The whole store (fetch, cache, rolling per-window history, persistence, and
// the refresh rate) lives in vendor/anthropic-data.js (AnthropicData.createUsageStore),
// mirroring how server/usage_panels/kilocode_data.py owns Kilo's data. Here we just instantiate it
// with this extension's storage, keep the refresh setting in sync (Options ▸
// Advanced), and expose it to the popup via the getClaudeUsage message. (Numbers
// are ACCOUNT-WIDE; fetching rides the <all_urls> claude.ai cookies, no new perm.)
const claudeUsageStore = AnthropicData.createUsageStore({ local: browser.storage.local });
// The same, for OpenAI (vendor/openai-data.js). Its `local` holds only the weekly samples
// behind the Usage tab's weekly chart; there is no 5-hour chart, so no session history. Two calls per
// refresh, not one - the usage endpoint wants a bearer that only the session endpoint
// can mint - and both of them happen in here, so the popup still asks once.
const openaiUsageStore = OpenAIData.createUsageStore({ local: browser.storage.local });
// The same, for GitHub Copilot (vendor/copilot-data.js): the github.com login and the
// entitlement, two calls on this browser's cookies. The login is what lets the popup
// notice when this browser and VS Code are signed in to different GitHub accounts.
const copilotUsageStore = CopilotData.createUsageStore({ local: browser.storage.local });
// Kilo provider/model NAME resolver (models.dev), with a persistent storage.local
// cache; see vendor/kilocode-modelnames-data.js. The popup asks via resolveKiloNames.
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

// Handle popup requests for connection status
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
      // Why the server turned us away, when it did (4401). Never the token itself:
      // CONFIG and this payload both go to the popup and the console.
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

  // Handle options page configuration updates
  if (request.type === 'configUpdated') {
    updateConfig(request.config);
    sendResponse({ success: true });
    return true;
  }

  // Handle advanced configuration updates
  if (request.type === 'advancedConfigUpdated') {
    updateConfig(request.config);
    sendResponse({ success: true });
    return true;
  }

  // Handle "Reset to Defaults" from the options page. options.js has already
  // cleared storage.sync, so mirror the fresh-install connection defaults into
  // the running config WITHOUT re-persisting (keep storage empty) and reconnect.
  // uiSurface reverts on its own via the storage.onChanged listener firing for
  // the cleared key.
  if (request.type === 'resetToDefaults') {
    updateConfig({
      hostname: 'localhost',
      port: 8772,
      retryInterval: 5000,
      maxRetries: -1,
      pingTimeout: 5000
    }, false);
    // Fresh slate: drop the persisted local caches too (Claude usage history +
    // the Kilo model-name cache), both in memory and in storage.local.
    try { claudeUsageStore.clearHistory(); } catch (e) {}
    try { openaiUsageStore.clearHistory(); } catch (e) {}
    try { copilotUsageStore.clearHistory(); } catch (e) {}
    try { kiloNameResolver.clear(); } catch (e) {}
    sendResponse({ success: true });
    return true;
  }


  // Handle connection status request from options page
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
    // Pass the "Load Kilo session details" setting so the server can SKIP the
    // per-Kilo-process log reading when it's off (saves work on the 2s polling).
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
    // Restart over the WebSocket (works even when the MCP HTTP control port is
    // unreachable; the exact failure the health check surfaces. The server acks,
    // then cycles itself; the socket drops and the reconnect loop picks up the
    // replacement. Rejects (→ HTTP fallback in options.js) if we're not connected.
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
  // END console-entries sink (observability.test.mjs slices to this anchor)
});


// --- Per-tab observability: rolling request log, console buffer, nav state ---
// Always-on, bounded. Powers: requests.recent (retroactive inspection without
// pre-arming a monitor), the network/console outcome digests attached to
// content.act and navigation.navigate results, and waitForSettle's
// network-quiet detection.
const TAB_REQUESTS = new Map(); // tabId -> [{id,url,method,type,start,end,status,error,fromCache}]
const TAB_NET = new Map();      // tabId -> {inflight, lastActivity}
const TAB_NAV = new Map();      // tabId -> {lastCommitted,lastAccessObserved}
const TAB_CONSOLE = new Map();  // tabId -> [{level,text,ts,url}]
const TAB_PROXY = new Map();    // tabId -> {type,host,port,proxyDNS,endpoint} (opt-in SOCKS routing)
const REQ_LOG_MAX = 300;
const CONSOLE_MAX = 600;
const STATIC_TYPES = new Set(['stylesheet', 'script', 'image', 'imageset', 'font', 'media', 'object', 'speculative']);
// Request types that cannot put content into the page: a sendBeacon, a hyperlink ping, a
// CSP violation report, a speculative connection. They stay in the request log but not on
// the network-quiet clock, so an analytics beacon cannot hold a navigation open. (Firefox's
// webRequest.ResourceType names; Chromium has no beacon or speculative type.)
const QUIET_IGNORED_TYPES = new Set(['beacon', 'ping', 'csp_report', 'speculative']);

// Map Gecko/necko network error codes to a short plain-English cause, so a failed
// navigation reads as "connection refused", not just an opaque code.
const NET_ERROR_HUMAN = {
  NS_ERROR_CONNECTION_REFUSED: 'connection refused - nothing is listening on that address/port',
  NS_ERROR_PROXY_CONNECTION_REFUSED: 'proxy connection refused - the SOCKS proxy is not accepting connections (is it running?)',
  NS_ERROR_NET_TIMEOUT: 'network timeout - the connection was accepted but no response arrived (or the host is unreachable)',
  NS_ERROR_UNKNOWN_HOST: 'unknown host - DNS could not resolve the domain',
  NS_ERROR_UNKNOWN_PROXY_HOST: 'unknown proxy host - the SOCKS proxy hostname could not be resolved',
  NS_ERROR_NET_RESET: 'connection reset by the server',
  NS_ERROR_NET_INTERRUPT: 'connection interrupted before the response completed',
  NS_ERROR_REDIRECT_LOOP: 'redirect loop',
  NS_BINDING_ABORTED: 'request aborted - usually a newer navigation replaced it, or the tab was stopped/closed',
  NS_ERROR_ABORT: 'request aborted',
  NS_ERROR_OFFLINE: 'browser is offline',
  NS_ERROR_PORT_ACCESS_NOT_ALLOWED: 'port blocked - Firefox refuses to connect to this restricted port',
  NS_ERROR_NET_PARTIAL_TRANSFER: 'connection closed before the full response arrived',
  NS_ERROR_DOM_BAD_URI: 'request blocked - the URI is invalid or access to it is not allowed (often CORS, a disallowed scheme, or a blocked port)',
};
// webNavigation reports an uncommitted load as "Error code <decimal nsresult>"
// while webRequest names the SAME failure symbolically (NS_ERROR_...); decode
// the integer form so both surfaces read alike and an agent can branch on the
// cause without hand-converting a 10-digit number.
// Keys are nsresult values (hex); anything unmapped falls through unchanged.
const NSRESULT_NAMES = {
  0x80004004: 'NS_ERROR_ABORT',
  0x804B0002: 'NS_BINDING_ABORTED',
  0x804B000D: 'NS_ERROR_CONNECTION_REFUSED',
  0x804B000E: 'NS_ERROR_NET_TIMEOUT',
  0x804B0010: 'NS_ERROR_OFFLINE',
  0x804B0013: 'NS_ERROR_PORT_ACCESS_NOT_ALLOWED',
  0x804B0014: 'NS_ERROR_NET_RESET',
  0x804B001E: 'NS_ERROR_UNKNOWN_HOST',
  0x804B001F: 'NS_ERROR_REDIRECT_LOOP',
  0x804B002A: 'NS_ERROR_UNKNOWN_PROXY_HOST',
  0x804B0047: 'NS_ERROR_NET_INTERRUPT',
  0x804B0048: 'NS_ERROR_PROXY_CONNECTION_REFUSED',
  0x804B004C: 'NS_ERROR_NET_PARTIAL_TRANSFER',
  0x805303F4: 'NS_ERROR_DOM_BAD_URI',
};
function humanizeNetError(code) {
  if (!code) return null;
  let name = String(code);
  let raw = null;
  const m = /^Error code\s+(\d+)$/.exec(name);
  if (m) {
    const sym = NSRESULT_NAMES[Number(m[1])];
    if (!sym) return name; // genuinely unmapped; keep the raw integer form
    name = sym;
    raw = m[1];
  }
  const h = NET_ERROR_HUMAN[name];
  const label = raw ? (name + ' = ' + raw) : name;
  return h ? (h + ' [' + label + ']') : (raw ? (name + ' [' + raw + ']') : name);
}

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
        delete log[i].hop;
        if (error) log[i].error = error;
        if (d.ip) log[i].ip = d.ip; // resolved/dialed server address (when the connection got that far)
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

// Stamp the dialed peer address as soon as Firefox knows it (first response
// byte), not only at completion; so a still-pending or errored entry carries
// the IP that diagnoses an address-family split. A pure
// hang (handshake done, server never responds) never reaches this point, so
// its entry stays ip-less; the server marks that absence explicitly.
browser.webRequest.onResponseStarted.addListener(d => {
  if (d.tabId < 0) return;
  // Only the document's response decides an open; any other response matters for its ip.
  const mainFrame = d.type === 'main_frame';
  if (!mainFrame && !d.ip) return;
  const log = TAB_REQUESTS.get(d.tabId);
  if (!log) return;
  for (let i = log.length - 1; i >= 0; i--) {
    if (log[i].id === d.requestId) {
      // A response arrived, so a document commit that follows is that response, not an
      // error page (failedMainFrameError).
      if (mainFrame) log[i].responded = true;
      if (d.ip) log[i].ip = d.ip;
      break;
    }
  }
}, { urls: ['<all_urls>'] });

// Record redirect hops on the originating request so a navigation result can show
// the chain (and where it failed). onBeforeRedirect carries the per-hop ip too.
browser.webRequest.onBeforeRedirect.addListener(d => {
  if (d.tabId < 0) return;
  const log = TAB_REQUESTS.get(d.tabId);
  if (!log) return;
  for (let i = log.length - 1; i >= 0; i--) {
    if (log[i].id === d.requestId) {
      (log[i].redirects = log[i].redirects || []).push({ from: log[i].url, to: d.redirectUrl, status: d.statusCode });
      if (d.ip) log[i].ip = d.ip;
      log[i].url = d.redirectUrl; // follow, so a later failure is attributed to the final hop
      // FF152+ replaces the channel on (some) redirects: the continuation gets a NEW
      // requestId, so THIS id never sees onCompleted and would linger "pending"
      // forever. Mark the hop finished now; if the same id does continue (older
      // behavior), onCompleted simply overwrites status/end.
      log[i].status = d.statusCode;
      log[i].end = d.timeStamp;
      // On the same id the response or error is still to come, so this end is the hop's.
      log[i].hop = true;
      break;
    }
  }
}, { urls: ['<all_urls>'] });

function markNavCommitted(d) {
  if (d.frameId !== 0) return;
  const nav = TAB_NAV.get(d.tabId) || {};
  nav.lastCommitted = d.timeStamp || Date.now();
  nav.lastCommittedUrl = d.url;
  nav.lastNavError = null;     // a successful commit clears the prior failure
  nav.lastNavErrorAt = null;
  TAB_NAV.set(d.tabId, nav);
  noteNavEvent(d, 'commit');
  wakeSettle(d.tabId);
}
browser.webNavigation.onCommitted.addListener(markNavCommitted);
browser.webNavigation.onHistoryStateUpdated.addListener(markNavCommitted);
browser.webNavigation.onReferenceFragmentUpdated.addListener(markNavCommitted);

// A navigation that fails to COMMIT (DNS/connection/abort) fires this, not
// onCommitted; record WHY so perception tools on the resulting about:blank tab,
// and the navigate result, can report the cause instead of going blind.
browser.webNavigation.onErrorOccurred.addListener(d => {
  if (d.frameId !== 0) return;
  const nav = TAB_NAV.get(d.tabId) || {};
  nav.lastNavError = d.error;
  nav.lastNavErrorUrl = d.url;
  nav.lastNavErrorAt = d.timeStamp || Date.now();
  TAB_NAV.set(d.tabId, nav);
  noteNavEvent(d, 'error');
});

// A main-frame document that finished loading. abortOfCompletedLoad reads these to set
// aside the abort Firefox reports from the process a tab leaves.
browser.webNavigation.onCompleted.addListener(d => {
  if (d.frameId !== 0) return;
  noteNavEvent(d, 'done');
  wakeSettle(d.tabId);
});

// The tab's recent main-frame webNavigation events, oldest first, in arrival order:
// {ev: commit|error|done, url, at, error}. Arrival order is the fact the abort rules
// depend on, since gecko delivers a process switch's events on two channels.
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
// `tabs.Tab.lastAccessed` is undefined for EVERY tab on Firefox for Android, and
// always has been. ext-android.js implements it as `nativeTab.lastTouchedAt`, a
// Fennec property that did not survive the Fenix rewrite, so the getter reads a
// field nothing defines any more (Bugzilla 2032927, filed 2026-04-17, still NEW).
// MDN says otherwise: its compat data carries `firefox_android: "mirror"` for this
// property, which means "assume whatever desktop does", not "somebody measured it".
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
  forgetSnapshotDocs(tabId);   // else the map grows for the life of the background page
  deleteTabProxy(tabId);   // persist the drop, so a closed tab's routing can't be restored
  wakeSettle(tabId);       // a settle sleeping on this tab finds it closed at once
});

// A status or URL change is what a sleeping waitForSettle is waiting for.
browser.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo && (changeInfo.status || changeInfo.url)) wakeSettle(tabId);
});

// --- Most-recently-focused Firefox window tracking -----------------------------
// The tabs "list" tool flags the focused window; but when Firefox isn't the
// frontmost OS app (e.g. you're in a terminal) NO window reports focused:true, so
// we track the last Firefox window that HAD focus. Seed from getLastFocused(),
// then follow windows.onFocusChanged, IGNORING WINDOW_ID_NONE (-1): on Windows
// and some Linux WMs a window->window switch emits an intermediate -1, so clearing
// on it would be wrong. Re-seed if the tracked window is closed. (Win/mac/Linux.)
let LAST_FOCUSED_WINDOW_ID = null;

function reseedLastFocusedWindow() {
  if (!browser.windows) return;      // a phone has no windows to track
  browser.windows.getLastFocused().then(w => {
    if (w && typeof w.id === 'number') LAST_FOCUSED_WINDOW_ID = w.id;
  }).catch(() => {});
}
reseedLastFocusedWindow();

if (browser.windows && browser.windows.onFocusChanged) {
  browser.windows.onFocusChanged.addListener(windowId => {
    if (windowId !== browser.windows.WINDOW_ID_NONE) LAST_FOCUSED_WINDOW_ID = windowId;
  });

  browser.windows.onRemoved.addListener(windowId => {
    if (windowId === LAST_FOCUSED_WINDOW_ID) {
      LAST_FOCUSED_WINDOW_ID = null;
      reseedLastFocusedWindow();
    }
  });
}

// --- Per-tab SOCKS proxy routing (opt-in; e.g. open a .onion via a Tor SOCKS5) ---
// A tab opted into a proxy routes ALL its requests (including DNS) through that
// SOCKS endpoint, or fails. It NEVER silently falls back to the direct path
// (fail-closed), so a "routed" tab can't deanonymize via a direct connection or a
// local DNS lookup. Keyed by tabId; non-proxied tabs are completely unaffected.
//
// No host/port/proxy is ever hard-coded; the endpoint comes only from the caller
// (the `proxy` field on tabs.create / navigation.navigate), so the same extension
// works unchanged on any machine/OS.
function parseProxyEndpoint(raw) {
  if (raw == null || raw === '' || raw === 'direct') return { ok: true, direct: true };
  const m = /^(socks5h?|socks4a?|socks):\/\/([^\s/:]+):(\d{1,5})\/?$/i.exec(String(raw).trim());
  if (!m) return { ok: false, error: `Invalid proxy endpoint "${raw}". Expected e.g. "socks5://127.0.0.1:9050" (schemes: socks5, socks5h, socks4, socks).` };
  const scheme = m[1].toLowerCase();
  const host = m[2];
  const port = parseInt(m[3], 10);
  if (!(port >= 1 && port <= 65535)) return { ok: false, error: `Invalid proxy port in "${raw}".` };
  // Firefox proxy API: type 'socks' = SOCKS5, 'socks4' = SOCKS4. proxyDNS keeps DNS
  // resolution AT the proxy (required so .onion resolves remotely with no local leak).
  const type = (scheme === 'socks4' || scheme === 'socks4a') ? 'socks4' : 'socks';
  return { ok: true, direct: false,
    info: { type, host, port, proxyDNS: true, endpoint: `${scheme}://${host}:${port}` } };
}

// --- TAB_PROXY persistence (survives a background-page restart) ----------------
// TAB_PROXY is in memory. A background restart (extension reload/update, background
// crash+revive) empties it while the proxied tabs stay open; so the next request
// from a routed .onion/Tor tab would hit proxyDecision with no config and go DIRECT:
// a silent deanonymization leak. So the map is mirrored into storage.local and
// restored on startup, and proxyDecision FAIL-CLOSES over the async restore window
// (below) rather than answering "direct" before the map is repopulated.
const PROXY_STORAGE_KEY = 'browserplugTabProxy';

function persistTabProxy() {
  try {
    const obj = {};
    for (const [tabId, info] of TAB_PROXY) obj[tabId] = info;
    browser.storage.local.set({ [PROXY_STORAGE_KEY]: obj }).catch(() => {});
  } catch (e) { /* storage unavailable; in-memory routing still enforced */ }
}
// Every TAB_PROXY mutation goes through these so persistence can't be forgotten.
function setTabProxy(tabId, info) { TAB_PROXY.set(tabId, info); persistTabProxy(); }
function deleteTabProxy(tabId) { if (TAB_PROXY.delete(tabId)) persistTabProxy(); }
function clearTabProxy() { TAB_PROXY.clear(); persistTabProxy(); }

// A genuine browser restart (new session) invalidates the persisted tab IDs; session
// restore assigns fresh ones, so an old id either doesn't exist or now names a
// DIFFERENT tab. runtime.onStartup fires ONLY on browser start, never on a background
// reload, so it is the reliable "tab IDs are stale, discard" signal. Fail-closed: on a
// new session we drop the persisted routing rather than risk mis-routing an unrelated
// tab. (The restored .onion tab then loads direct; unsolvable by tab-id persistence;
// but an .onion can't resolve on clearnet so it errors rather than reaching a real
// server, and this is documented.)
let _browserStartedThisLoad = false;
if (browser.runtime && browser.runtime.onStartup) {
  browser.runtime.onStartup.addListener(() => {
    _browserStartedThisLoad = true;
    clearTabProxy();
  });
}

// Repopulate TAB_PROXY from storage after a background restart, keeping only entries
// whose tab is still open. Resolves _proxyRestorePromise so proxyDecision can stop
// fail-closing. Skipped entirely on a fresh browser session (stale ids).
let _proxyRestoreDone = false;
async function restoreProxyState() {
  try {
    const stored = await browser.storage.local.get(PROXY_STORAGE_KEY);
    const saved = stored && stored[PROXY_STORAGE_KEY];
    // The `_browserStartedThisLoad` clause is defense-in-depth: on a browser restart
    // onStartup's clearTabProxy already empties storage, and the post-await recheck in
    // the loop below is the load-bearing guard (pinned by the mid-restore test). This
    // just short-circuits the common case where startup beat us to the read.
    if (_browserStartedThisLoad || !saved || Object.keys(saved).length === 0) return;
    // Without the optional proxy permission the listener is inactive and routing
    // can't be enforced; don't pretend it can; drop the unenforceable state.
    if (!(await hasProxyPermission())) { clearTabProxy(); return; }
    let restored = 0;
    for (const [idStr, info] of Object.entries(saved)) {
      const tabId = parseInt(idStr, 10);
      try {
        await browser.tabs.get(tabId);        // still open?
        // Re-check AFTER the await: onStartup (a browser restart) may have fired while
        // we were suspended on tabs.get. Its handler cleared TAB_PROXY; without this
        // recheck the set below would resurrect a stale entry it just discarded.
        if (_browserStartedThisLoad) return;
        TAB_PROXY.set(tabId, info);
        restored++;
      } catch (e) { /* tab closed while we were down; drop it */ }
    }
    if (restored > 0) ensureProxyListener();
    persistTabProxy();                        // reprune storage to the surviving set
    if (restored > 0) console.log(`proxy routing restored for ${restored} tab(s)`);
  } catch (e) { /* storage/permission probe failed; stay empty (fail-closed) */ }
  finally { _proxyRestoreDone = true; }
}
let _proxyRestorePromise = restoreProxyState();

// Apply an optional `proxy` field to a tab's routing. Returns an error string the
// caller must surface (and abort on; fail-closed: a bad value never becomes a
// silent direct connection), or null on success. '' / 'direct' clears routing;
// undefined leaves it unchanged.
function applyTabProxy(tabId, raw) {
  if (raw === undefined) return null;
  const p = parseProxyEndpoint(raw);
  if (!p.ok) return p.error;
  if (p.direct) deleteTabProxy(tabId);
  else setTabProxy(tabId, p.info);
  return null;
}

// The single global routing decision. Fail-closed: a proxied tab is handed ONLY its
// SOCKS proxy, and the array is null-TERMINATED so Firefox does not fail over to a
// browser-defined (possibly direct) proxy if the SOCKS endpoint is unreachable; the
// request errors instead of leaking to clearnet (MDN: an array ending in null
// suppresses the default browser-proxy failover).
function proxyDecisionSync(details) {
  const cfg = TAB_PROXY.get(details.tabId);
  if (!cfg) return { type: 'direct' };
  return [{ type: cfg.type, host: cfg.host, port: cfg.port, proxyDNS: cfg.proxyDNS }, null];
}
function proxyDecision(details) {
  // Until the post-restart repopulation finishes, we do NOT know whether this tab is
  // routed; answering "direct" now is the leak. proxy.onRequest accepts a Promise
  // (MDN), so hold the request until TAB_PROXY is restored, then decide.
  if (!_proxyRestoreDone && _proxyRestorePromise) {
    return _proxyRestorePromise.then(() => proxyDecisionSync(details));
  }
  return proxyDecisionSync(details);
}
// NOTE: Firefox refuses to proxy loopback (127.0.0.1 / localhost) targets by
// default (pref network.proxy.allow_hijacking_localhost = false), so such requests
// bypass this listener and go direct. Irrelevant to the .onion/Tor use case (those
// hosts are non-loopback); only matters if someone tries to route a localhost dev
// server through a proxy.
//
// `proxy` is an OPTIONAL permission (manifest optional_permissions), so
// `browser.proxy` exists only while the user has it granted (about:addons ▸
// Permissions). Register the routing listener lazily whenever the permission is
// held, and tear it down + drop all routing when it's revoked; never leave a
// tab with a stale proxy config that the (now inactive) listener won't enforce,
// which would silently go direct (a fail-open leak).
let _proxyListenerRegistered = false;
function ensureProxyListener() {
  if (_proxyListenerRegistered) return;
  if (browser.proxy && browser.proxy.onRequest) {
    browser.proxy.onRequest.addListener(proxyDecision, { urls: ['<all_urls>'] });
    _proxyListenerRegistered = true;
  }
}
function removeProxyListener() {
  try {
    if (browser.proxy && browser.proxy.onRequest &&
        browser.proxy.onRequest.hasListener(proxyDecision)) {
      browser.proxy.onRequest.removeListener(proxyDecision);
    }
  } catch (e) { /* namespace may already be gone with the permission */ }
  _proxyListenerRegistered = false;
  clearTabProxy(); // fail-closed: no unenforced routing survives a revoke (and clear
                   // the persisted copy too, or a restart would restore what was revoked)
}
ensureProxyListener();

// Whether the extension currently holds the optional "proxy" permission.
async function hasProxyPermission() {
  try {
    if (browser.permissions && browser.permissions.contains) {
      return await browser.permissions.contains({ permissions: ['proxy'] });
    }
  } catch (e) { /* treat as not granted */ }
  return false;
}

// Reported to the server (extension_hello -> /health) so it can show/hide the
// `proxy` arg on the tabs/navigate MCP tools to match the grant.
async function getProxyPermissionState() {
  let hasProxy = null;
  try {
    if (browser.permissions && browser.permissions.contains) {
      hasProxy = await browser.permissions.contains({ permissions: ['proxy'] });
    }
  } catch (e) {
    console.error('permissions.contains(proxy) failed:', e);
  }
  return { has_proxy_permission: hasProxy };
}

// Shown when a proxy is requested without the permission (fail-closed: the tab
// is NOT opened/navigated on a direct path).
const PROXY_PERM_ERROR =
  'SOCKS proxy routing needs the optional "proxy" permission (Control browser ' +
  'proxy settings). Enable it in about:addons ▸ BrowserPlug ▸ Permissions, then retry.';

// Track runtime grant/revoke of the optional permission: (un)register the
// routing listener and refresh /health (which re-shapes the MCP tool schema).
if (browser.permissions && browser.permissions.onAdded) {
  browser.permissions.onAdded.addListener((perms) => {
    if (perms && Array.isArray(perms.permissions) && perms.permissions.includes('proxy')) {
      ensureProxyListener();
      sendExtensionHello();
    }
  });
}
if (browser.permissions && browser.permissions.onRemoved) {
  browser.permissions.onRemoved.addListener((perms) => {
    if (perms && Array.isArray(perms.permissions) && perms.permissions.includes('proxy')) {
      removeProxyListener();
      sendExtensionHello();
    }
  });
}

function isPrivilegedUrl(url) {
  return !url || /^(about|chrome|moz-extension|resource|view-source|jar|data|javascript):/.test(url) ||
    url.startsWith('https://addons.mozilla.org/');
}

// Resolve the target tab for a request: default to the active tab when no
// tabId was given; turn a stale tabId into a STALE_TAB error; optionally refuse
// privileged pages with a named status.
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
      // about:blank is often a navigation that did not commit; surface what we know.
      if (isBlank && nav.lastAttemptedUrl) msg += ` The last attempted navigation was ${nav.lastAttemptedUrl}.`;
      if (isBlank && nav.lastNavError) msg += ` Last navigation error: ${humanizeNetError(nav.lastNavError)}.`;
      msg += isBlank
        ? ' Navigate it to an http(s) page first.'
        : ' Navigate it to a normal http(s) page first.';
      // Name the privileged-safe alternatives (these do NOT need a content script).
      msg += ' To see this tab anyway: page_screenshot returns the painted pixels and requests returns its network log.';
      const err = new Error(msg);
      err.code = 'CONTENT_UNREACHABLE';
      throw err;
    }
  }
  return tabId;
}

// Wait until a tab "settles": document complete + network quiet (no requests
// in flight and none started for quietMs) + optional selector present, bounded
// by timeoutMs. expectNav additionally requires a navigation commit (real or
// SPA history update) after sinceTs; the guard against matching a stale,
// pre-navigation document.
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
    // was asked for (opts.ignoreBlankCommit), a tab still showing about:blank has not
    // committed it, whatever order the commit events arrived in.
    // opts.priorCommitAt is the tab's latest commit when the navigation was issued. It is
    // never that navigation's own, however recent: the allowance above would take it, and
    // with the old document still reporting "complete" a navigation issued just after a
    // load would settle on the page it was leaving.
    navCommitted = !!(nav && nav.lastCommitted > sinceTs - 100)
      && !(opts.ignoreBlankCommit && (!tab.url || tab.url === 'about:blank'))
      && !(opts.priorCommitAt != null && nav.lastCommitted <= opts.priorCommitAt);
    const net = TAB_NET.get(tabId) || { inflight: 0, lastActivity: 0 };
    // "Network quiet" must not be blocked by long-lived connections (SSE,
    // long-poll, websocket handshakes that never complete): only requests
    // younger than 2.5s count as genuinely in-flight. A request that cannot put
    // content in the page (a beacon, a ping) does not count at all.
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
        const [found] = await browser.tabs.executeScript(tabId, {
          code: '!!document.querySelector(' + JSON.stringify(selector) + ')'
        });
        selectorOk = !!found;
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

// Firefox answers a failed connection (refused, reset, proxy unreachable) by COMMITTING its
// own error page at the requested URL, and onCommitted then wipes lastNavError, so a commit
// alone does not mean the page loaded. The request log decides: the open failed when the
// LATEST main-frame request since t0 ended in an error. An abort is not one (a newer
// navigation replaced the request), and a 4xx/5xx has no error at all. Neither is a body
// cut short by a clean close: Firefox renders what arrived, while a reset after the
// response started gets its error page (both measured on Firefox 158). A redirect hop on
// the same id (before Firefox 152, and internal redirects) ends the hop, not the request.
// Returns the error, null when the request did not fail (or there is none: about:, data:,
// file:), or undefined while it has neither an error nor a response. webRequest and
// webNavigation are separate event streams, so the commit can arrive before
// onErrorOccurred does.
//
// The same holds for the request itself. A network page (`tabUrl`, the URL the tab shows)
// always has a main-frame request, so a commit with none on file means webRequest has not
// reported it yet, not that it never existed: a loaded machine delays one stream against
// the other (a 1.0.11 sweep opened an unresolvable .onion name on a loaded machine and was
// told "Opened tab" with an empty request log). Then the answer is undecided, and a failure
// webNavigation reported before the commit wiped lastNavError is still in the tab's event
// list (navErrorEventSince).
function failedMainFrameError(tabId, t0, tabUrl) {
  const rq = latestMainFrameRequest(tabId, t0);
  if (!rq) {
    const navError = navErrorEventSince(tabId, t0);
    if (navError) return navError;
    return isNetworkUrl(tabUrl) ? undefined : null;
  }
  if (rq.error) {
    if (rq.error === 'NS_BINDING_ABORTED') return null;
    if (rq.error === 'NS_ERROR_NET_PARTIAL_TRANSFER' && rq.responded) return null;
    return rq.error;
  }
  return (rq.responded || (rq.end && !rq.hop)) ? null : undefined;
}

// The latest failure webNavigation reported for the tab's main frame since t0, from the event
// list noteNavEvent keeps (arrival order, not wiped by the commit that follows an error).
// An abort is not one. Null when there is none.
function navErrorEventSince(tabId, t0) {
  const nav = TAB_NAV.get(tabId);
  const events = (nav && nav.events) || [];
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.at < t0 - 100) break;
    if (e.ev === 'error' && e.error && !isNavAbort(e.error)) return e.error;
  }
  return null;
}

// A page fetched over the network, as opposed to about:, data: and file:, which have no
// main-frame request in the log by design.
function isNetworkUrl(u) {
  return /^(https?|ftp):/i.test(String(u || ''));
}

// Firefox reports a body cut short by a clean close through webNavigation as well, after
// it committed and rendered the part that arrived. Such a tab shows that part, so the
// open committed. Headers with no body leave the tab on about:blank: that open failed.
function partialPageShown(tabId, t0, navError, tab) {
  const m = /^Error code\s+(\d+)$/.exec(String(navError));
  const name = m ? NSRESULT_NAMES[Number(m[1])] : String(navError);
  if (name !== 'NS_ERROR_NET_PARTIAL_TRANSFER' || !tab.url || tab.url === 'about:blank') return false;
  const rq = latestMainFrameRequest(tabId, t0);
  return !!(rq && rq.responded);
}

// The URLs an open's own abort can name (abortOfReplacedLoad): the one requested, where its
// request was redirected to, and a document committed since the open began. A proxied open
// starts from about:blank and then replaces that load, so about:blank never counts.
function openOwnUrls(tabId, sinceTs, url, nav) {
  const rq = latestMainFrameRequest(tabId, sinceTs);
  const committed = nav.lastCommitted >= sinceTs ? nav.lastCommittedUrl : null;
  return [url, rq && rq.url, committed === 'about:blank' ? null : committed];
}

// How long a seen commit waits for its main-frame request to resolve one way or the other.
const COMMIT_RESOLVE_GRACE_MS = 1000;

// The same process switch reported BEFORE the new process commits: a proxied open, or any
// open in a blank tab, can see the leaving process's abort of the requested URL while the
// request for that URL is still running (measured on Firefox Developer Edition 158 with a
// local SOCKS proxy: abort +95 to +119 ms, the commit +165 to +210 ms, the page loaded, and
// the open reported "request aborted" with the tab on about:blank in about half the runs).
// A load that was really stopped or replaced ends its request with an error (an abort
// included, failedMainFrameError), so an abort that names the latest main-frame request
// while that request has no error is not the end of the load: still in flight (a slow
// proxy such as Tor can take seconds; the open's budget bounds the wait), or finished less
// than COMMIT_RESOLVE_GRACE_MS ago with its commit not yet delivered through webNavigation.
// Only before the commit: the same abort after it is abortAwaitingCompletion's.
function abortOfLiveRequest(tabId, nav, t0) {
  if (!nav.lastNavError || !nav.lastNavErrorUrl || !(nav.lastNavErrorAt >= t0 - 50)
      || !isNavAbort(nav.lastNavError)) return false;
  // A document the tab already committed at that URL is abortAwaitingCompletion's case.
  if (nav.lastCommitted >= t0 && navUrlKey(nav.lastCommittedUrl) === navUrlKey(nav.lastNavErrorUrl)) return false;
  const rq = latestMainFrameRequest(tabId, t0);
  if (!rq || rq.error || navUrlKey(rq.url) !== navUrlKey(nav.lastNavErrorUrl)) return false;
  return !rq.end || Date.now() - rq.end < COMMIT_RESOLVE_GRACE_MS;
}

// Lighter than waitForSettle: wait only until a navigation COMMITS (a real
// document started loading at a non-about:blank URL) or errors, bounded by
// timeoutMs. Used by tabs.create and windows.create so "open a tab on this URL" doesn't
// hand back a tab still parked on about:blank with the URL not yet applied, or call
// Firefox's error page a loaded one (failedMainFrameError). `url` is the one requested.
async function waitForCommit(tabId, sinceTs, timeoutMs, url) {
  const start = Date.now();
  const budget = Math.min(timeoutMs || 8000, 30000);
  let commitSeenAt = null;
  let echoSeenAt = null;
  let tab = null;
  while (Date.now() - start < budget) {
    try { tab = await browser.tabs.get(tabId); } catch (e) { return { committed: false, tabClosed: true }; }
    const nav = TAB_NAV.get(tabId);
    if (nav && nav.lastNavError && nav.lastNavErrorAt >= sinceTs - 100
        && !partialPageShown(tabId, sinceTs, nav.lastNavError, tab)
        && !abortOfReplacedLoad(nav.lastNavError, nav.lastNavErrorUrl,
          openOwnUrls(tabId, sinceTs, url, nav))
        && !abortOfCompletedLoad(nav.lastNavError, nav.lastNavErrorUrl, nav, sinceTs)
        && !abortOfLiveRequest(tabId, nav, sinceTs)) {
      // An abort of the very document the tab committed can be the echo of a process
      // switch (abortOfCompletedLoad): the answer waits the grace for that load to complete.
      if (abortAwaitingCompletion(nav, sinceTs)) {
        if (echoSeenAt === null) echoSeenAt = Date.now();
        if (commitSeenAt === null) commitSeenAt = echoSeenAt;
        if (Date.now() - echoSeenAt < COMMIT_RESOLVE_GRACE_MS) {
          await new Promise(r => setTimeout(r, 100));
          continue;
        }
      }
      return { committed: false, navError: nav.lastNavError, url: tab.url, title: tab.title };
    }
    if (nav && nav.lastCommitted >= sinceTs - 100 && tab.url && tab.url !== 'about:blank') {
      const failed = failedMainFrameError(tabId, sinceTs, tab.url);
      if (failed) return { committed: false, navError: failed, url: tab.url, title: tab.title };
      if (commitSeenAt === null) commitSeenAt = Date.now();
      if (failed === null || Date.now() - commitSeenAt >= COMMIT_RESOLVE_GRACE_MS) {
        return { committed: true, url: tab.url, title: tab.title, status: tab.status };
      }
    }
    await new Promise(r => setTimeout(r, 100));
  }
  try { tab = await browser.tabs.get(tabId); } catch (e) { tab = null; }
  // The budget ran out inside the grace: it did commit. An error may have landed after
  // the last poll, so look once more before calling it loaded.
  if (commitSeenAt !== null && tab) {
    const failed = failedMainFrameError(tabId, sinceTs, tab.url);
    if (failed) return { committed: false, navError: failed, url: tab.url, title: tab.title };
    // An abort of the committed document that its load never answered still stands.
    const navEnd = TAB_NAV.get(tabId) || {};
    if (abortAwaitingCompletion(navEnd, sinceTs)) {
      return { committed: false, navError: navEnd.lastNavError, url: tab.url, title: tab.title };
    }
    return { committed: true, url: tab.url, title: tab.title, status: tab.status };
  }
  // No commit inside the budget. A main-frame request that ended in a net error is still the
  // answer: under load a slow lookup can fail after most of the budget has gone, and the
  // error page's commit comes later than the open waits ("did not commit within 8s" told an
  // agent nothing about a name that does not resolve).
  if (tab) {
    const failed = failedMainFrameError(tabId, sinceTs);
    if (failed) return { committed: false, navError: failed, url: tab.url, title: tab.title };
  }
  return { committed: false, timedOut: true, url: tab ? tab.url : undefined, title: tab ? tab.title : undefined };
}

// Digest of network activity in a tab since t0; lets an action result carry
// the outcome of the async work it triggered (e.g. a login POST -> 401).
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
  // favicon 404s are ubiquitous noise; never surface them as "failed"
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

// Background-driven wait used by content.act 'wait' steps: re-injects a
// ONE-SHOT condition check every 250ms. Unlike an in-page polling loop, this
// honors the requested timeout exactly and survives navigations mid-wait (a
// failed injection during a navigation is retried, and the next check runs in
// the NEW document). A POSITIVE match can only come from the real document (an
// empty placeholder has nothing to match); a GONE match can be manufactured by
// the mid-navigation placeholder, so a gone success is only trusted once
// tab.status leaves "loading" (see the gone-success gate below).
async function actWait(tabId, step, frameId) {
  const timeoutMs = Math.min(step.timeout_ms || step.timeoutMs || 10000, 120000);
  const start = Date.now();
  const checkArgs = {
    // for a wait step the universal `target` IS the CSS selector to wait for
    selector: step.selector || step.target,
    textContains: step.text_contains || step.textContains,
    urlContains: step.url_contains || step.urlContains,
    predicate: step.predicate,
    gone: !!step.gone,
    requireVisible: !!(step.require_visible || step.requireVisible),
    // `within` is declared on every act step; forward it, or a scoped wait would
    // silently evaluate document-wide (panel 1's spinner satisfying a wait on
    // panel 2's) while every other step kind honors the scope.
    within: step.within
  };
  if (!checkArgs.selector && !checkArgs.textContains && !checkArgs.urlContains && !checkArgs.predicate) {
    return { ok: false, error: 'wait step needs at least one of: target (CSS selector), text_contains, url_contains, predicate' };
  }
  let last = null;              // the most recent poll result, whatever it was
  let lastSeenCheck = null;     // the most recent poll that actually OBSERVED the document
  let maxMatchCount = 0;        // selector matches seen across ALL polls (not just the last)
  let nodesFirst = null;        // the element count at the first and last poll that
  let nodesLast = null;         // observed the document; free, every wait_check carries it
  let scopeEverSeen = false;    // did `within` ever resolve during this wait?
  let scopeMissingStreak = 0;   // consecutive polls the scope was missing (debounce flicker)
  while (Date.now() - start < timeoutMs) {
    try {
      last = await runAutomation(tabId, 'wait_check', checkArgs, frameId);
      // An empty injection result is the mid-navigation transient (the target frame was
      // briefly absent); the SAME condition as a thrown injection and just as recoverable.
      // Treat it as a non-observation (keep polling), not a fatal error, and don't let it
      // pollute the wait's history (scopeEverSeen / lastSeenCheck / streak).
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
        // Debounce a flickering scope: a single missing poll can be a framework
        // unmount/remount (key swap, Suspense fallback), not a permanent teardown. Require
        // the miss to persist across polls before it can end a gone-wait.
        if (last.scopeMissing) scopeMissingStreak++; else scopeMissingStreak = 0;
      }
      if (last && last.error) {
        return { ok: false, error: last.error, elapsedMs: Date.now() - start };
      }
      // A POSITIVE match is self-validating: an empty document cannot produce one,
      // so it is safe to accept the moment it is seen.
      if (last && last.matched && !checkArgs.gone) {
        return { ok: true, matched: last.matched, elapsedMs: Date.now() - start, url: last.url };
      }
      // A `gone` wait is satisfied by ABSENCE; an empty document trivially
      // provides. Two shapes: the scoped container DISAPPEARED (what it held went with
      // it; waitCheck can't decide this from one poll, so the HISTORY does: the scope
      // must have been SEEN earlier this wait), or the condition's own nodes are gone.
      // BUT mid-navigation an executeScript binds to Firefox's transient placeholder
      // document (right URL, readyState "complete", yet EMPTY; no body): there every
      // selector/text/scope reads as absent, so a gone-wait matches VACUOUSLY and would
      // end the wait against a page that has not parsed yet; the round-2 vacuous-truth
      // harm one level up (document-wide, not container-scoped). tab.status is the only
      // signal that exposes the placeholder (content.execute guards the identical way),
      // so a gone success from a poll taken while the tab is still loading is discarded.
      // scope-gone is only meaningful when the wait actually has a SCOPED condition
      // (selector/text); a within carrying only a page-global url/predicate must not be
      // satisfied just because its container vanished (those are evaluated globally in
      // waitCheck's scope-missing branch). And require the miss to persist >= 2 polls so a
      // one-poll unmount/remount flicker does not end the wait mid-load.
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
        // seen on a still-loading tab (likely the empty placeholder); keep polling
        // until the destination document settles before trusting an absence.
      }
      // scopeMissing + never seen: the container may still render; keep polling.
    } catch (e) {
      // injection fails transiently while a navigation is in flight; keep polling
      last = { injectError: e.message };
    }
    const remaining = timeoutMs - (Date.now() - start);
    if (remaining <= 0) break;
    await new Promise(r => setTimeout(r, Math.min(250, remaining)));
  }
  // A failed injection on the FINAL poll (routine mid-navigation) must not erase the
  // report: classify from the last poll that actually observed the document.
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
  // Say WHY the wait timed out: "the scope never appeared", "never matched",
  // "present but hidden", or (for gone-waits) "still visible" / "still present but
  // hidden", so the agent's next move is a decision, not a guess.
  if (checkArgs.within && !scopeEverSeen && obs.scopeMissing) {
    // The scope never showed up in ANY poll, so the condition was never actually
    // evaluated; reporting "never-matched" would send the agent hunting the wrong
    // element. (Judged from the whole wait, not just the last poll.)
    out.lastSeen = 'scope-never-appeared';
    out.scopeMissing = obs.scopeMissing;
  } else if (checkArgs.selector && typeof obs.selectorMatchCount === 'number') {
    out.matchCount = obs.selectorMatchCount;
    if (obs.scopeMissing) out.scopeMissing = obs.scopeMissing; // scope came and went
    if (checkArgs.gone) {
      // selectorVisible tells "still on screen" apart from "still in the DOM but
      // hidden"; the second usually means the agent should have waited on removal
      // of a different node, or used require_visible. (undefined = not reported:
      // claim neither.)
      // All three keep the `still-present` stem: the FACT is that the node the agent
      // waited to disappear is still in the DOM; visibility only qualifies it.
      out.lastSeen = obs.selectorVisible === false ? 'still-present-but-hidden'
        : obs.selectorVisible === true ? 'still-present-and-visible'
        : 'still-present';
    } else if (maxMatchCount === 0) {
      out.lastSeen = 'never-matched';
    } else if (obs.selectorMatchCount === 0) {
      // it matched earlier (maxMatchCount > 0) but is gone from the last observation;
      // for a positive wait that is "appeared then vanished before settling". Report the
      // PEAK count, not the contradictory "present-but-hidden with matchCount 0".
      out.lastSeen = 'appeared-then-vanished';
      out.matchCount = maxMatchCount;
    } else if (checkArgs.requireVisible) {
      out.lastSeen = 'present-but-hidden';
    }
  }
  // A scope that came and went should be reported even for a text-only wait (which
  // never enters the selector branch above).
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
          // small grace window: document_start capture begins around commit
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
// Capture a tab by id without stealing focus when possible. Firefox's
// tabs.captureTab(tabId) (FF59+) captures a specific tab directly; we prefer it
// and fall back to captureVisibleTab + activate/restore only if it is missing
// or throws. `fullPage` does a best-effort scroll-and-stitch.
async function captureScreenshot(data, run = newCaptureRun()) {
  const format = data.format || 'png';
  const quality = data.quality || 90;

  // Element-only capture (by selector/ref/text): resolve, scroll into view, settle,
  // crop to its box. A selector is caller intent, so this path is opt-in.
  if (data.selector && data.tabId != null) {
    return await captureElement(data.tabId, data.selector, format, quality, data.settleMs, run);
  }

  // Full-page: per-band-settled scroll-and-stitch (no black bands below the fold).
  if (data.fullPage && data.tabId != null) {
    return await captureRegion(data.tabId, null, format, quality, data.settleMs, run);
  }

  const options = { format: format };
  if (format === 'jpeg') options.quality = quality;
  if (data.rect) options.rect = data.rect;
  if (data.scale) options.scale = data.scale;

  // Settle paint (web fonts, in-viewport image decode, a quiet frame) BEFORE a
  // viewport capture, so we return the final frame, not a transient/blank one.
  // Best-effort; a privileged page just yields {settled:false,error} and
  // we proceed without a (false) warning.
  let settleWarning;
  if (data.tabId != null) {
    run.stage = 'settle';
    const s = await settleForCapture(data.tabId, data.settleMs);
    if (s && s.settled === false && !s.skipped && !s.error) {
      settleWarning = 'capture may not have fully settled (lazy media/animation still in progress)';
    }
  }

  // Preferred: captureTab(tabId); works for any tab, no focus change. A
  // failure here is RECORDED, not just logged: the fallback needs a painted
  // window and once returned a blank image as a clean success, so the
  // downgrade must be visible in the result.
  let captureTabError = null;
  if (data.tabId != null && browser.tabs.captureTab) {
    try {
      const dataUrl = await boundedStep(run, 'captureTab', browser.tabs.captureTab(data.tabId, options), CAPTURE_GRAB_MS);
      const tab = await browser.tabs.get(data.tabId).catch(() => null);
      return { dataUrl, format, quality, tabId: data.tabId, windowId: tab ? tab.windowId : null, method: 'captureTab', settleWarning };
    } catch (e) {
      // A captureTab that never answers is not a failure the fallback can fix: it needs a
      // painted window and would bring the user's tab to the front to find that out.
      if (e && e.code === 'CAPTURE_STALLED') throw e;
      captureTabError = String((e && e.message) || e);
      run.captureTabError = captureTabError;
      console.log('captureTab failed, falling back to captureVisibleTab: ' + captureTabError);
    }
  }

  // Fallback: captureVisibleTab (active tab of a window). For a non-active
  // target tab, activate it, capture, then restore the previously-active tab.
  let windowId = data.windowId || null;
  try {
    if (data.tabId != null) {
      const tab = await browser.tabs.get(data.tabId);
      windowId = tab.windowId;
      if (!tab.active) {
        const [prev] = await browser.tabs.query({ active: true, windowId: windowId });
        if (prev) run.restoreTabId = prev.id;
        run.activated = true;
        await boundedStep(run, 'activate the tab', browser.tabs.update(data.tabId, { active: true }), CAPTURE_STEP_MS);
        await new Promise(r => setTimeout(r, 120));
      }
    }
    if (run.aborted) throw captureStalled(run, 'the capture', CAPTURE_DEADLINE_MS);
    run.stage = 'captureVisibleTab';
    const dataUrl = await raceCapture(browser.tabs.captureVisibleTab(windowId, options), 'captureVisibleTab');
    return { dataUrl, format, quality, tabId: data.tabId != null ? data.tabId : null, windowId,
             method: 'captureVisibleTab', settleWarning,
             captureTabError: captureTabError || undefined };
  } catch (e) {
    throw (e && e.code === 'CAPTURE_STALLED') ? e : bothCapturesFailed(captureTabError, e);
  } finally {
    await restoreCaptureRun(run);
  }
}

// When the fallback fails too, the error names both failures. Thrown alone, the fallback's
// error hides the first one, and Firefox's bare "An unexpected error occurred" for a tab
// captureTab could not draw leaves an agent no cause to act on.
function bothCapturesFailed(captureTabError, fallbackError) {
  if (!captureTabError) return fallbackError;
  const both = new Error('captureTab failed (' + captureTabError + '), and the ' +
    'captureVisibleTab fallback failed too: ' +
    String((fallbackError && fallbackError.message) || fallbackError));
  if (fallbackError && fallbackError.code) both.code = fallbackError.code;
  return both;
}

// One run of a capture: a deadline, bounded steps, and a record of what it did to the tab.
// Every await on the page or the browser can stall: a background tab's page timers are
// throttled or suspended, executeScript waits for a document that is not idle yet, a
// captureTab promise can stay unresolved. Before this, only the 4 s captureVisibleTab race
// was bounded, so one stalled await held the request open until the tab was closed. In the
// 2026-10-08 evals the server's 60 s timeout and the client's own fired first, the agent saw
// "Request timed out", and a fallback that had already brought the tab to the front never
// put it back. The run knows the step it is in and whether it moved the user's tab, so a
// stall can name its step and undo the move.
const CAPTURE_DEADLINE_MS = 40000;  // under the server's 60 s request timeout
const CAPTURE_STEP_MS = 10000;      // one in-page step: measure, scroll a band, activate
const CAPTURE_GRAB_MS = 15000;      // one captureTab call
const CAPTURE_RESTORE_MS = 4000;    // putting the user's previous tab back in front
const CAPTURE_SETTLE_MARGIN_MS = 3000; // slack over the page-side settle cap

function newCaptureRun() {
  return { stage: 'start', activated: false, restoreTabId: null, restored: false, restoreStalled: false,
           aborted: false, captureTabError: null };
}

// The error for a stall. It says whether the tab was moved, because that is what an agent
// that retries needs to know.
function captureStalled(run, what, ms) {
  const moved = !run.activated
    ? 'The tab was not brought to the front.'
    : run.restoreStalled
      ? 'The fallback had brought the tab to the front and putting the previous tab back did not ' +
        'answer, so the tab may still be in front.'
      : run.restored
        ? 'The fallback had brought the tab to the front; the previous tab was put back.'
        : 'The fallback had brought the tab to the front and no previous tab was found to put back.';
  // The fallback only runs after captureTab failed; without that error the agent sees a
  // stall in captureVisibleTab and never learns what sent the capture there.
  const cause = run.captureTabError ? ', after captureTab failed: ' + run.captureTabError : '';
  const err = new Error('the screenshot stalled: ' + what + ' did not answer within ' +
    Math.round(ms / 1000) + ' s (step "' + run.stage + '"' + cause + '). ' + moved +
    ' The page is probably throttled because its window is in the background. A retry may ' +
    'work; page_snapshot and page_read answer without painting.');
  err.code = 'CAPTURE_STALLED';
  return err;
}

// Race a browser or page call against a timer owned by the background page, whose timers
// keep firing when the page's own are throttled.
function boundedStep(run, stage, promise, ms) {
  run.stage = stage;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(captureStalled(run, stage, ms)), ms);
    promise.then(v => { clearTimeout(timer); resolve(v); },
                 e => { clearTimeout(timer); reject(e); });
  });
}

// A step whose loss costs only polish (lazy-image nudge, prescroll): a stall or an error
// is ignored.
async function bestEffortStep(run, stage, promise, ms) {
  try { return await boundedStep(run, stage, promise, ms); } catch (e) { return undefined; }
}

// Put the tab the fallback replaced back in front. Safe to call twice. It runs in the
// fallback's `finally`, so it is raced like every other await: a tabs.update that never
// answers (a window that is not painted) must not hold back the error the fallback is
// already throwing, which then surfaces only as a stall at the deadline with the wrong
// step. It leaves run.stage alone, so a deadline error still names the step that stalled.
async function restoreCaptureRun(run) {
  const id = run.restoreTabId;
  if (id == null) return;
  run.restoreTabId = null;
  let timer;
  try {
    await Promise.race([
      browser.tabs.update(id, { active: true }),
      new Promise((resolve, reject) => {
        timer = setTimeout(() => { run.restoreStalled = true; reject(new Error('restore timed out')); },
                           CAPTURE_RESTORE_MS);
      }),
    ]);
    run.restored = true;
  } catch (e) {
    if (!run.restoreStalled) run.restored = true;  // the tab is gone: nothing left to restore
  } finally {
    clearTimeout(timer);
  }
}

// The handler's entry point: captureScreenshot under the deadline. On expiry the run is
// aborted (its loops stop), the tab is put back, and the error names the step.
async function captureWithDeadline(data) {
  const run = newCaptureRun();
  let timer;
  const deadline = new Promise((resolve, reject) => {
    timer = setTimeout(() => {
      run.aborted = true;
      restoreCaptureRun(run).then(() => reject(captureStalled(run, 'the capture', CAPTURE_DEADLINE_MS)));
    }, CAPTURE_DEADLINE_MS);
  });
  try {
    return await Promise.race([captureScreenshot(data, run), deadline]);
  } finally {
    clearTimeout(timer);
  }
}

function loadImageEl(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const timer = setTimeout(() => reject(new Error('image load timed out')), 5000);
    img.onload = () => { clearTimeout(timer); resolve(img); };
    img.onerror = () => { clearTimeout(timer); reject(new Error('image load failed')); };
    img.src = src;
  });
}

// Wait (bounded) for the visible viewport to be paint-stable before a capture:
// web fonts ready, in-viewport images decoded, and a couple of quiet animation
// frames. Returns {settled, waitedMs}. Injected into the page; best-effort; a
// privileged page or a race just yields {settled:false, error} and the caller
// proceeds. settle_ms <= 0 disables it (escape hatch / cap).
// `region` ({x,y,w,h} in page CSS px) scopes the image wait to that region
// instead of the viewport; used by the one-shot captureTab({rect}) path,
// which renders the whole region regardless of the current scroll position.
async function settleForCapture(tabId, capMs, region) {
  const cap = (capMs == null) ? 1200 : Math.max(0, capMs);
  if (cap === 0) return { settled: true, skipped: true };
  const fn = function (cap, region) {
    return (async function () {
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
      // Quiet-frame wait, BOUNDED: Firefox throttles (and for fully occluded
      // windows can suspend) rAF, so an unraced double-rAF can hang forever on
      // a background window. Losing the race only costs the "quiet frame"
      // confirmation; captureTab still renders such tabs fine.
      await Promise.race([
        new Promise(function (r) {
          if (typeof requestAnimationFrame === 'function') requestAnimationFrame(function () { requestAnimationFrame(r); });
          else setTimeout(r, 16);
        }),
        timeout(Math.min(remain(), 350))
      ]);
      return { settled: Date.now() <= deadline, waitedMs: Date.now() - t0 };
    })();
  };
  // The page's own timers bound the script, and they are the ones a background tab
  // throttles or suspends; this timer is the background page's. A page that does not
  // answer is reported unsettled (no `error`, so the caller's warning still shows).
  let stallTimer;
  const stalled = new Promise(resolve => {
    stallTimer = setTimeout(() => resolve([{ settled: false, stalled: true }]),
                            cap + CAPTURE_SETTLE_MARGIN_MS);
  });
  try {
    const [res] = await Promise.race([
      browser.tabs.executeScript(tabId, {
        code: '(' + fn.toString() + ')(' + cap + ',' + JSON.stringify(region || null) + ')'
      }),
      stalled
    ]);
    return res || { settled: false };
  } catch (e) {
    return { settled: false, error: String((e && e.message) || e) };
  } finally {
    clearTimeout(stallTimer);
  }
}

// Decode a captured image and report whether it is ONE uniform color; the
// signature of a blank frame (unpainted window, transient white flash). The
// paint-state probe can miss this (the window may be laid out yet composite
// blank), so this is the net that catches every blank
// capture regardless of cause. Samples via a small smoothed canvas: cheap,
// and the tolerance absorbs jpeg/scaling noise. Returns the color ("rgb(…)")
// when uniform, else null.
async function imageIsUniform(dataUrl) {
  const img = await loadImageEl(dataUrl);
  const w = Math.max(1, Math.min(64, img.naturalWidth || 1));
  const h = Math.max(1, Math.min(64, img.naturalHeight || 1));
  const canvas = document.createElement('canvas');
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0, w, h);
  const px = ctx.getImageData(0, 0, w, h).data;
  const r0 = px[0], g0 = px[1], b0 = px[2];
  const TOL = 3;
  for (let i = 0; i < px.length; i += 4) {
    if (Math.abs(px[i] - r0) > TOL || Math.abs(px[i + 1] - g0) > TOL ||
        Math.abs(px[i + 2] - b0) > TOL) return null;
  }
  return 'rgb(' + r0 + ',' + g0 + ',' + b0 + ')';
}

// A captureVisibleTab promise never settles for a window the compositor is
// not painting (Firefox behind another app / minimized); race it against a
// short timeout so the caller gets an actionable error in seconds, not a
// 60s request-timeout stall.
const VISIBLE_CAPTURE_TIMEOUT_MS = 4000;
function raceCapture(promise, what) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => {
      const err = new Error(what + ' timed out after ' + VISIBLE_CAPTURE_TIMEOUT_MS + 'ms. ' +
        'the window is not being painted (Firefox is likely behind another app or minimized). ' +
        'Use a plain viewport screenshot (no selector/full_page), or bring the Firefox window ' +
        'to the front (tabs action="activate"), then retry.');
      err.code = 'WINDOW_NOT_PAINTED';
      reject(err);
    }, VISIBLE_CAPTURE_TIMEOUT_MS);
    promise.then(v => { clearTimeout(t); resolve(v); }, e => { clearTimeout(t); reject(e); });
  });
}

// Native lazy images in an INACTIVE/occluded tab never load no matter how the
// page is scrolled; without refresh ticks the lazy-load intersection never
// fires. (The legacy stitch path only worked because it activated the tab.)
// Flip pending lazy images inside the capture region to eager so their loads
// start immediately; the region-aware settle then waits for the actual bytes.
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
    await browser.tabs.executeScript(tabId, {
      code: '(' + fn.toString() + ')(' + JSON.stringify({ x: rx, y: ry, w: rw, h: rh }) + ')'
    });
  } catch (e) { /* best-effort (privileged page) */ }
}

// Scroll through a region viewport-by-viewport so IntersectionObserver-based
// lazy loaders fire, then restore the original scroll position. The per-band
// pause is deliberately short; the region-aware settle pass afterwards waits
// for the triggered loads themselves.
async function prescrollThroughRegion(tabId, rx, ry, rh, vh, restoreX, restoreY) {
  const step = Math.max(vh, 100);
  const MAX_BANDS = 40;
  const budgetEnd = Date.now() + 8000; // a throttled tab must not spend the whole deadline here
  let bands = 0;
  for (let y = ry; y < ry + rh && bands < MAX_BANDS && Date.now() < budgetEnd; y += step, bands++) {
    try {
      await browser.tabs.executeScript(tabId, { code: 'window.scrollTo(' + rx + ',' + y + ')' });
    } catch (e) { break; }
    await new Promise(r => setTimeout(r, 60));
  }
  try {
    await browser.tabs.executeScript(tabId, { code: 'window.scrollTo(' + restoreX + ',' + restoreY + ')' });
  } catch (e) { /* best-effort */ }
}

// Region capture (an element's box, or clip=null for full page width ×
// scrollHeight). Preferred path: ONE-SHOT tabs.captureTab({rect}) (FF82+);
// it renders any tab without focus or activation, so element/full-page shots
// work on background/unfocused windows exactly like plain viewport shots.
// Lazy below-the-fold content is prescrolled into loading first, and the
// settle pass waits on images inside the REGION, preserving the
// no-black-bands guarantee. Falls back to the legacy activate +
// scroll-and-stitch captureVisibleTab loop only if captureTab is missing or
// its rect option is ignored (verified against the returned bitmap size);
// that path needs a painted window, so each shot fails fast via raceCapture.
async function captureRegion(tabId, clip, format, quality, settleMs, run = newCaptureRun()) {
  const tab = await browser.tabs.get(tabId);
  const windowId = tab.windowId;

  let metrics = null;
  try {
    [metrics] = await boundedStep(run, 'measure the page', browser.tabs.executeScript(tabId, {
      code: '({sh: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0),' +
            ' cw: document.documentElement.clientWidth, ch: document.documentElement.clientHeight,' +
            ' dpr: window.devicePixelRatio || 1, sx: window.scrollX, sy: window.scrollY})'
    }), CAPTURE_STEP_MS);
  } catch (e) {
    // The page did not answer the measurement at all (not the same as a blocked content
    // script): a full-page request degrades to the viewport, an element request has no box.
    const unanswered = !!(e && e.code === 'CAPTURE_STALLED');
    // Content scripts are blocked here (moz-extension://, about:, AMO, …), so
    // the page can't be measured for a full-page capture. Degrade to a plain
    // viewport captureTab with a note instead of erroring "Missing host
    // permission"; which misleads the agent into chasing permissions.
    if (!clip && browser.tabs.captureTab) {
      const opts = { format: format };
      if (format === 'jpeg') opts.quality = quality;
      const dataUrl = await boundedStep(run, 'captureTab (viewport)', browser.tabs.captureTab(tabId, opts), CAPTURE_GRAB_MS);
      return { dataUrl, format, quality, tabId, windowId, method: 'captureTab', pages: 1,
               pageWarning: unanswered
                 ? 'full_page is unavailable right now (the page did not answer the in-page ' +
                   'measurement), captured the visible viewport instead.'
                 : 'full_page is unavailable on this page (content scripts are blocked ' +
                   'on privileged pages like moz-extension:// or about:, and full-page capture ' +
                   'needs in-page scroll metrics), captured the visible viewport instead.' };
    }
    if (unanswered) throw e;
    const err = new Error('cannot measure this page for ' + (clip ? 'an element' : 'a full-page') +
      ' capture: content scripts are blocked on privileged pages (' + e.message + '). ' +
      'A plain viewport screenshot (no full_page/selector) works here.');
    err.code = 'PRIVILEGED_PAGE';
    throw err;
  }
  if (!metrics) throw new Error('could not measure page for capture');
  const dpr = metrics.dpr;
  const vw = metrics.cw, vh = metrics.ch;
  const MAX_HEIGHT = 20000; // safety cap (canvas + Firefox image-size limits)

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

  // --- Preferred: one-shot captureTab({rect}); no focus, no activation.
  let captureTabError = null;
  if (browser.tabs.captureTab) {
    const { rx, ry, rw, rh, truncated } = regionOf();
    try {
      let couldNotSettle = false;
      if (settleMs !== 0) {
        await bestEffortStep(run, 'load lazy images', forceEagerImagesInRegion(tabId, rx, ry, rw, rh), CAPTURE_STEP_MS);
        if (rh > vh) {
          await bestEffortStep(run, 'prescroll', prescrollThroughRegion(tabId, rx, ry, rh, vh, metrics.sx, metrics.sy), 2 * CAPTURE_STEP_MS);
        }
      }
      run.stage = 'settle';
      const s = await settleForCapture(tabId, settleMs, { x: rx, y: ry, w: rw, h: rh });
      if (s && s.settled === false && !s.skipped && !s.error) couldNotSettle = true;
      const opts = { format: format, rect: { x: rx, y: ry, width: rw, height: rh } };
      if (format === 'jpeg') opts.quality = quality;
      const dataUrl = await boundedStep(run, 'captureTab (region)', browser.tabs.captureTab(tabId, opts), CAPTURE_GRAB_MS);
      // An older Firefox ignores `rect` and returns the viewport; verify the
      // bitmap actually covers the requested region before trusting it.
      const img = await loadImageEl(dataUrl);
      const expW = Math.round(rw * dpr), expH = Math.round(rh * dpr);
      const okW = Math.abs(img.naturalWidth - expW) <= Math.max(4, expW * 0.02);
      const okH = Math.abs(img.naturalHeight - expH) <= Math.max(4, expH * 0.02);
      if (okW && okH) {
        const out = { dataUrl, format, quality, tabId, windowId,
          method: clip ? 'element' : 'fullPage', pages: 1, truncated: truncated };
        if (couldNotSettle) out.settleWarning = 'capture may not have fully settled (lazy media/animation still in progress)';
        return out;
      }
      captureTabError = 'rect ignored (got ' + img.naturalWidth + 'x' + img.naturalHeight +
                        ' for a ' + expW + 'x' + expH + ' region)';
      run.captureTabError = captureTabError;
      console.log('captureTab rect returned ' + img.naturalWidth + 'x' + img.naturalHeight +
                  ' for a ' + expW + 'x' + expH + ' region; falling back to scroll-and-stitch');
    } catch (e) {
      // A captureTab that never answers is not something the activating fallback can fix.
      if (e && e.code === 'CAPTURE_STALLED') throw e;
      captureTabError = String((e && e.message) || e);
      run.captureTabError = captureTabError;
      console.log('captureTab region capture failed (' + captureTabError + '); falling back to scroll-and-stitch');
    }
  }

  // --- Fallback: activate + scroll-and-stitch via captureVisibleTab. Needs a
  // painted (frontmost) window; each shot fails fast instead of hanging.
  let couldNotSettle = false;
  try {
    if (!tab.active) {
      const [prev] = await browser.tabs.query({ active: true, windowId: windowId });
      if (prev) run.restoreTabId = prev.id;
      run.activated = true;
      await boundedStep(run, 'activate the tab', browser.tabs.update(tabId, { active: true }), CAPTURE_STEP_MS);
      await new Promise(r => setTimeout(r, 150));
    }
    const region = regionOf();
    const rx = region.rx, ry = region.ry, truncated = region.truncated;
    let rw = region.rw;
    const rh = region.rh;
    let clippedWidth = false;
    if (rw > vw) { rw = vw; clippedWidth = true; } // one viewport wide (we scroll x to rx)

    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(rw * dpr));
    canvas.height = Math.max(1, Math.round(rh * dpr));
    const ctx = canvas.getContext('2d');

    const step = Math.max(vh, 100);
    let pages = 0;
    for (let y = ry; y < ry + rh; y += step) {
      if (run.aborted) throw captureStalled(run, 'the capture', CAPTURE_DEADLINE_MS);
      // scroll, then read back the ACTUAL offset; the browser clamps the last
      // step, so stitching at the requested y would misalign the bottom band.
      const [pos] = await boundedStep(run, 'scroll a band', browser.tabs.executeScript(tabId, {
        code: '(function(){window.scrollTo(' + rx + ',' + y + ');return {x:window.scrollX,y:window.scrollY};})()'
      }), CAPTURE_STEP_MS);
      const asx = pos && typeof pos.x === 'number' ? pos.x : rx;
      const asy = pos && typeof pos.y === 'number' ? pos.y : y;
      const s = await settleForCapture(tabId, settleMs);
      if (s && s.settled === false && !s.skipped && !s.error) couldNotSettle = true;
      const opts = { format: format };
      if (format === 'jpeg') opts.quality = quality;
      run.stage = 'captureVisibleTab';
      const dataUrl = await raceCapture(browser.tabs.captureVisibleTab(windowId, opts), 'captureVisibleTab');
      const img = await loadImageEl(dataUrl);
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
        try { ctx.drawImage(img, sxSrc, sySrc, swSrc, shSrc, 0, dy, swSrc, shSrc); } catch (e) { /* skip a bad band */ }
      }
    }
    await bestEffortStep(run, 'restore scroll', browser.tabs.executeScript(tabId, { code: 'window.scrollTo(' + metrics.sx + ',' + metrics.sy + ');' }), CAPTURE_STEP_MS);

    const mime = format === 'jpeg' ? 'image/jpeg' : 'image/png';
    const stitched = canvas.toDataURL(mime, format === 'jpeg' ? quality / 100 : undefined);
    const out = { dataUrl: stitched, format, quality, tabId, windowId,
      method: clip ? 'element' : 'fullPage', pages: pages, truncated: truncated,
      captureTabError: captureTabError || undefined };
    if (couldNotSettle) out.settleWarning = 'capture may not have fully settled (lazy media/animation still in progress)';
    if (clippedWidth) out.clippedWidth = true;
    return out;
  } catch (e) {
    throw (e && e.code === 'CAPTURE_STALLED') ? e : bothCapturesFailed(captureTabError, e);
  } finally {
    await restoreCaptureRun(run);
  }
}

// Element-only screenshot: resolve + scroll + measure via the automation toolkit
// (reusing its ref/CSS/text resolution + nth disambiguation), then capture the
// element's page-coords box via captureRegion (one-shot captureTab({rect}) on
// current Firefox; works on unfocused windows). Throws a clear error on
// zero/many matches; never a silent viewport fallback.
async function captureElement(tabId, selector, format, quality, settleMs, run = newCaptureRun()) {
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
  const out = await captureRegion(tabId, box, format, quality, settleMs, run);
  out.method = 'element';
  out.selector = selector;
  out.elementBox = { w: Math.round(box.w), h: Math.round(box.h) };
  return out;
}

// Tabs handlers
// ---- file:// open/navigate refusal -----------------------------------------
// A file:// open used to come back as a bare "Illegal URL", the browser's own words,
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
// Firefox names the switch on the add-on's own permissions screen.
const FILE_ACCESS_TOGGLE = 'about:addons > BrowserPlug > Permissions > "Access your data for file:// URLs"';

// Firefox for Android cannot open file:// AT ALL, and not only from an extension: the
// browser itself cannot. Typing file:/// in its address bar does nothing. Fennec could;
// the Fenix rewrite dropped it and it has not returned (Bugzilla 1806171, NEW since
// 2022-12). The trap is that nothing THROWS: tabs.create resolves, the tab never commits,
// and waitForCommit burns the caller's whole budget before reporting a timeout that
// explains nothing. So this refuses BEFORE trying, where fileUrlRefusal below only runs
// after the browser has already rejected the URL (which is the desktop behaviour).
//
// The platform test is the absence of the windows API: on gecko that IS Firefox for
// Android, and it is synchronous, where getPlatformInfo() is async and would leave a
// startup window in which this answers wrongly.
function androidFileUrlRefusal(url, verb) {
  if (browser.windows) return null;                       // has windows => desktop
  if (!/^file:/i.test(String(url || '').trim())) return null;
  return 'cannot ' + verb + ' ' + String(url).split(/[?#]/)[0] + ': Firefox for Android ' +
    'cannot open file:// URLs at all. This is not a BrowserPlug setting and not the ' +
    'file-access permission - the browser itself has no file:// support on Android ' +
    '(Bugzilla 1806171), so there is nothing to enable and no tab to read. Do not retry ' +
    'and do not look for a workaround here: local files need a desktop browser.';
}

async function fileUrlRefusal(url, verb, rawMessage) {
  const wanted = String(url || '').split(/[?#]/)[0];
  if (!/^file:/i.test(wanted)) return null;
  let landed = null;
  try {
    const all = await browser.tabs.query({});
    landed = (all || []).find(t => t.url && t.url.split(/[?#]/)[0] === wanted) || null;
  } catch (e) { /* best-effort */ }

  let msg = 'the browser refused to ' + verb + ' ' + wanted + ': it rejected the URL itself ("' +
    String(rawMessage || 'Illegal URL') + '"). ';
  if (landed) {
    msg += 'A TAB IS NEVERTHELESS OPEN ON THAT FILE: tab ' + landed.id +
      (landed.title ? ' ("' + String(landed.title).slice(0, 60) + '")' : '') +
      ', the refusal was PARTIAL. Do not retry and do not work around it: read that tab ' +
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
        // All windows by default; pass currentWindow:true to scope.
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
            isInReaderMode: tab.isInReaderMode, lastAccessed: tabLastAccessedMs(tab),
            proxy: (TAB_PROXY.get(tab.id) || {}).endpoint
          })),
          windows: windows,
          lastFocusedWindowId: LAST_FOCUSED_WINDOW_ID,
          debug: {
            totalFound: tabs.length
          }
        });
        break;
      }

      case 'tabs.active':
        // lastFocusedWindow (not currentWindow); the exact query resolveTabId
        // uses, so this reports the tab page_* tools target when tab_id is omitted.
        const [activeTab] = await browser.tabs.query({ active: true, lastFocusedWindow: true });
        sendResponse(id, action, { tab: activeTab });
        break;

      // Read back a tab's effective routing (proxied via which endpoint, or direct).
      case 'tabs.routing': {
        const rTabId = await resolveTabId(data);
        const cfg = TAB_PROXY.get(rTabId);
        sendResponse(id, action, cfg
          ? { tabId: rTabId, routed: true, isProxy: true, type: cfg.type, endpoint: cfg.endpoint, proxyDNS: cfg.proxyDNS }
          : { tabId: rTabId, routed: false, isProxy: false, type: 'direct' });
        break;
      }

      case 'tabs.create': {
        // Before anything else: on Android this URL can only produce a tab that never
        // commits, and a 30s timeout that explains nothing.
        const androidFileMsg = androidFileUrlRefusal(data.url, 'open a tab on');
        if (androidFileMsg) { sendError(id, 'FILE_URL_UNSUPPORTED', androidFileMsg); break; }
        // Validate the proxy up front: a bad value must NOT create a tab that
        // then loads over the direct path (fail-closed).
        const proxyParsed = parseProxyEndpoint(data.proxy);
        if (!proxyParsed.ok) { sendError(id, 'INVALID_PROXY', proxyParsed.error); break; }
        const proxyInfo = proxyParsed.direct ? null : proxyParsed.info;
        // A non-direct proxy needs the optional "proxy" permission; without it the
        // routing listener is inactive and the tab would load DIRECT (a leak). Refuse.
        if (proxyInfo && !(await hasProxyPermission())) {
          sendError(id, 'PROXY_PERMISSION_REQUIRED', PROXY_PERM_ERROR); break;
        }
        if (proxyInfo) ensureProxyListener();

        const createTabOptions = { active: data.active || false };
        if (data.windowId) createTabOptions.windowId = data.windowId;
        if (data.pinned !== undefined) createTabOptions.pinned = data.pinned;
        // With a proxy we open about:blank FIRST (no network), install routing for
        // the new tab, then navigate; otherwise the very first request could race
        // the proxy install and leak to the direct path.
        createTabOptions.url = proxyInfo ? 'about:blank' : data.url;

        const t0commit = Date.now();
        const newTab = await browser.tabs.create(createTabOptions);
        if (proxyInfo) {
          setTabProxy(newTab.id, proxyInfo);
          if (data.url) await browser.tabs.update(newTab.id, { url: data.url });
          newTab.proxy = proxyInfo.endpoint;
        }
        // Atomic open-and-load: await the commit so we never hand back a tab still
        // parked on about:blank with the URL "not applied yet". Bounded; a
        // hang/error is reported, not dressed up as success.
        if (data.url && data.url !== 'about:blank') {
          const c = await waitForCommit(newTab.id, t0commit, 8000, data.url);
          if (c.url) newTab.url = c.url;
          if (c.title) newTab.title = c.title;
          if (!c.committed) {
            newTab.navPending = data.url;
            if (c.navError) newTab.navError = humanizeNetError(c.navError);
            else if (c.timedOut) newTab.navTimedOut = true;
          }
        }
        sendResponse(id, action, { tab: newTab });
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
        // Also focus the tab's window (unless told not to): an active tab in an
        // unfocused window stays unpainted, which silently breaks perception.
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
          const shot = await captureWithDeadline(data);
          // Paint-state probe: a tab in an unfocused/minimized window is not
          // laid out and captures as a blank image; say so instead of letting
          // the agent diagnose a healthy page as broken. Never OVERWRITE a
          // warning a capture path already attached (e.g. the privileged-page
          // full_page fallback).
          if (data.tabId != null && !shot.pageWarning) {
            try {
              const [state] = await boundedStep(newCaptureRun(), 'paint probe', browser.tabs.executeScript(data.tabId, {
                code: '(function(){var b=document.body,r=b?b.getBoundingClientRect():null;' +
                      'var w=r?Math.round(r.width):0,h=r?Math.round(r.height):0;' +
                      'var hid=false;try{hid=b&&getComputedStyle(b).display==="none";}catch(e){}' +
                      'return {painted:(w>0&&h>0&&!hid),hiddenByCss:!!hid,focused:document.hasFocus()};})()'
              }), CAPTURE_STEP_MS);
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
          // Last-resort blank detection: the probe above reads LAYOUT, but a
          // window can be laid out and still composite a blank frame (that is
          // how a background claude.ai tab once shipped an all-white capture
          // with no warning). Check the pixels themselves.
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
      // Check if tab exists and is accessible
      const tab = await browser.tabs.get(tabId);
      if (!tab) {
        throw new Error('Tab not found');
      }

      // Skip status check for now and try to send message
      // The content script should be available on all URLs except chrome:// pages
      if (tab.url && (tab.url.startsWith('chrome://') || tab.url.startsWith('moz-extension://'))) {
        throw new Error('Cannot access content script on system pages');
      }

      const result = await browser.tabs.sendMessage(tabId, message);
      return result;
    } catch (error) {
      if (attempt === maxRetries) {
        throw new Error(`Content script not available after ${maxRetries} attempts: ${error.message}`);
      }

      // Wait longer before retry
      await new Promise(resolve => setTimeout(resolve, delayMs));
    }
  }
}

// --- Page automation helpers (extension-gecko/automation.js) ----------------------
// The automation toolkit is injected on demand into a tab/frame via
// tabs.executeScript. We load its source text once and cache it.
let AUTOMATION_SRC = null;
async function getAutomationSrc() {
  if (AUTOMATION_SRC) return AUTOMATION_SRC;
  try {
    const resp = await fetch(browser.runtime.getURL('automation.js'));
    AUTOMATION_SRC = await resp.text();
    return AUTOMATION_SRC;
  } catch (e) {
    console.error('Failed to load automation.js:', e);
    return ''; // don't poison the cache on a transient failure; retry next call
  }
}

// Minimal inline page-state probe shared by the content.read drivers (kept
// independent of automation.js so reads work even if that injection fails).
const PAGE_STATE_FN = `function __fxPageState(){
  var b=document.body, r=b?b.getBoundingClientRect():null;
  var w=r?Math.round(r.width):0, h=r?Math.round(r.height):0;
  var hid=false;
  try {
    var bcs=b?getComputedStyle(b):null, hcs=getComputedStyle(document.documentElement);
    hid=!!((bcs&&bcs.display==='none')||(hcs&&hcs.display==='none')||(bcs&&bcs.visibility==='hidden'));
  } catch(e){}
  return { focused:document.hasFocus(), visibility:document.visibilityState, bodyW:w, bodyH:h,
           hiddenByCss:hid, painted:(w>0&&h>0&&!hid), readyState:document.readyState };
}`;

// Inject with a few quick retries. During a navigation the target frame can be
// momentarily absent ("No window matching" / "Frame not found" / not-yet-injected)
// (transient). On final failure we surface the attempt count + a distinct
// INJECT_FAILED status, so a read failure reads as "retried N times", not a silent
// ~30s hang or an opaque one-shot error.
// A file:// tab is NOT a privileged page; content scripts can run there, but
// only while the per-extension local-file opt-in is granted (Firefox 153+ gates
// it; earlier versions grant it through the required <all_urls>). Without this
// branch that failure comes back wearing the generic injection wording, whose own
// suggested diagnosis ("the tab may be a privileged page") is simply WRONG for an
// ordinary local HTML file; and the agent concludes the capability is absent and
// builds a workaround for something that was one toggle away. Returns the
// specific message, or null when file access is on (or unknown), so the generic
// message still covers the genuinely-privileged and mid-navigation cases.
async function fileAccessInjectHint(tabId) {
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
    'turn it on in about:addons > BrowserPlug > Permissions > "Access your data for file:// URLs" (the ' +
    'BrowserPlug popup shows the same switch under "Local file (file://) access"), then retry; page_read / ' +
    'page_snapshot / page_act / page_eval all work on a file:// tab once it is granted. To check before ' +
    'trying: server_info reports it as extension_file_access.can_read_opened_file_tab. (Note the ' +
    'extension can never OPEN a file:// URL itself; that part is a Firefox platform limit, so the tab ' +
    'has to be opened by hand.)';
}

// executeScript's refusal of a value the script returned, which only a change to the
// script can fix: a DOM node, a function or a window cannot be cloned out of the page.
const NON_CLONABLE_RE = /non-structured-clonable|could not be cloned|DataCloneError/i;

async function executeScriptWithRetry(tabId, details, opts = {}) {
  const attempts = opts.attempts || 4;
  const delayMs = opts.delayMs || 150;
  let lastErr = null;
  let used = 0;
  for (let i = 1; i <= attempts; i++) {
    try {
      return await browser.tabs.executeScript(tabId, details);
    } catch (e) {
      lastErr = e;
      const msg = String((e && e.message) || e);
      used = i;
      // Hard failures won't fix themselves; don't burn retries on them. A result that
      // cannot be cloned means the script already RAN: a retry would run it again.
      if (/No tab with id|Invalid tab ID|Missing host permission|Cannot access/i.test(msg)) break;
      if (NON_CLONABLE_RE.test(msg)) break;
      if (i < attempts) await new Promise(r => setTimeout(r, delayMs * i));
    }
  }
  const fileHint = await fileAccessInjectHint(tabId);
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

// Inject the toolkit + a single invoke() call into the target tab/frame.
// Firefox resolves a Promise returned from executeScript, so async automation
// methods (wait_for / click / fill) come back in one round-trip.
// ---- snapshot ref staleness (documentId) ------------------------------------------
// A ref like "e5" is minted by snapshot into the toolkit's per-document map, and the
// toolkit dies with its document. After a navigation the ref is simply absent, and the
// failure came back as an ordinary "element not found" - byte-identical to the report for
// a button that was removed from a page that never navigated. The right next move differs
// completely between those two (re-snapshot, versus stop looking for that element), so
// collapsing them costs the agent the one fact it needed.
//
// documentId is what separates them; frameId cannot, because ids are reused and a
// navigation swaps the document underneath the same frameId. Record the document each
// snapshot was taken in, and compare ONLY when a ref-targeted call has already failed:
// the diagnosis costs nothing on the path where the click worked, the same rule the
// click-state confirmation follows.
const SNAPSHOT_DOCS = new Map();   // `${tabId}:${frameId}` -> documentId at snapshot time

// Only a real toolkit envelope counts. A transport-level failure carries no nodesAtStart
// and says nothing about API availability; treating it as "unsupported" would latch a
// false negative that survives until the background restarts.
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
    'document that no longer exists - the page navigated or reloaded since. Element refs do ' +
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

// A self-contained expression that reads the current documentId from INSIDE an injected
// script. It is a string because the page_eval path builds code strings; it must never
// throw into the user's script, so every failure answers null (= unknown).
const DOC_ID_EXPR =
  '(function(){try{var r=(typeof browser!=="undefined"&&browser.runtime)||' +
  '(typeof chrome!=="undefined"&&chrome.runtime)||null;' +
  'return (r&&typeof r.getDocumentId==="function")?(r.getDocumentId(window)||null):null;}' +
  'catch(e){return null;}})()';

// Did the document change WHILE the script ran? Both ids must be known; one missing means
// unknown, and unknown must not be reported as a change. This is strictly sharper than the
// tab.status probe next to it: status catches an in-flight load, but a navigation that
// starts and finishes inside the eval's own window leaves status "complete" on both sides
// while the result describes a document that no longer exists.
function evalDocumentChanged(before, after) {
  return !!(before && after && before !== after);
}

// ---- page_eval wrapper ------------------------------------------------------------
// The code page_eval injects around the caller's script. A throw, a syntax error or a
// rejected promise in the caller's own code comes back as an envelope with `error`
// ("SyntaxError: return not in function") instead of rejecting executeScript. A rejection
// would read as an injection failure: executeScriptWithRetry would run the script again
// on every attempt and then report "the tab may have been navigating; retry", sending
// the agent after a navigation that never happened.
const EVAL_ERROR_FN =
  'function(e){var n=e&&e.name,m=e&&e.message;' +
  'm=(m===undefined||m===null)?String(e):String(m);' +
  'return (n&&m.indexOf(String(n))!==0)?String(n)+": "+m:m;}';

function evalWrapperCode(script, world) {
  if (world === 'main') {
    // Evaluate in the PAGE's compartment via wrappedJSObject.eval so page JS globals are
    // visible. This wrapper itself runs in the ISOLATED world (only the inner eval
    // crosses into the page compartment), so browser.runtime is reachable and the
    // document identity is captured atomically here, as in the isolated branch. Page CSP
    // can block eval; that is reported distinctly, with the CSP-immune alternative.
    return '(function(){' +
      'var __d0 = ' + DOC_ID_EXPR + ';' +
      'var __errText = ' + EVAL_ERROR_FN + ';' +
      'try {' +
      '  var completion = window.wrappedJSObject.eval(' + JSON.stringify(script) + ');' +
      '  return Promise.resolve(completion).then(function(v){' +
      '    var out = { readyState: document.readyState, docId: __d0, ' +
      'docIdAfter: ' + DOC_ID_EXPR + ' };' +
      '    if (v === undefined) { out.result = null; return out; }' +
      '    try { out.result = JSON.parse(JSON.stringify(v)); }' +
      '    catch (e) { out.result = String(v); }' +
      '    return out;' +
      '  }, function(e){ return { error: __errText(e) }; });' +
      '} catch (e) {' +
      '  var msg = __errText(e);' +
      '  if (/call to eval|unsafe-eval|EvalError|CSP/i.test(msg)) {' +
      '    return { error: "main-world eval blocked by the page Content-Security-Policy: " + msg + ". Fallback: run with world=isolated and read page globals via window.wrappedJSObject.<name>." };' +
      '  }' +
      '  return { error: msg };' +
      '}' +
      '})()';
  }
  // Isolated world: capture document.readyState ATOMICALLY with the eval (same
  // synchronous tick). An eval issued right after a reload (wait="none") can land in a
  // document whose DOM is still parsing and truthfully return half-state (empty title,
  // missing nodes); a separate probe would race the document swap and miss it.
  // Indirect eval keeps the script's own semantics: global scope (var persistence across
  // calls) and completion-value result.
  return '(function(){' +
    'var __d0 = ' + DOC_ID_EXPR + ';' +
    'var __errText = ' + EVAL_ERROR_FN + ';' +
    'var __fail = function(e){ return { __browserplugEval: 1, error: __errText(e) }; };' +
    'var completion;' +
    'try { completion = (0, eval)(' + JSON.stringify(script) + '); }' +
    'catch (e) { return __fail(e); }' +
    'return Promise.resolve(completion).then(function(v){' +
    '  return { __browserplugEval: 1, value: v === undefined ? null : v, ' +
    'readyState: document.readyState, docId: __d0, docIdAfter: ' + DOC_ID_EXPR + ' };' +
    '}, __fail);' +
    '})()';
}

// page_eval's answer when executeScript refuses the script's result (NON_CLONABLE_RE).
const NON_CLONABLE_HELP = 'the value the script returned cannot be sent back from the page ' +
  '(a DOM element, a function or a window cannot be). Return plain data instead: a string, ' +
  'number, array or object, for example el.outerHTML or el.textContent rather than el.';

// ---- wait predicates are compiled here, never eval'd by the toolkit -----------------
// A `wait` step's predicate is the agent's JavaScript expression. The toolkit used to
// eval() it in the page, which addons-linter reports as DANGEROUS_EVAL on every AMO
// submission and which no reviewer can tell apart from an extension evaluating strings
// it did not write. The evaluation moves into the executeScript code string, beside the
// invoke() call it belongs to and in the same place page_eval's wrapper keeps its own
// (0, eval): the sandbox's global scope, so a predicate sees what page_eval
// world="isolated" sees (`window.wrappedJSObject` included). The toolkit receives a
// function, `predicateFn`, and applies its policy when it calls it; a predicate that
// does not parse throws a SyntaxError from inside that call, which the toolkit tells
// apart from a run-time throw. (Inlining the expression bare would fail the whole
// injection instead, and Firefox strips the error's name on the way back, so the parse
// failure could not be told from a transport failure.)
function compiledArgs(methodArgs) {
  const json = JSON.stringify(methodArgs || {});
  if (!methodArgs || typeof methodArgs.predicate !== 'string') return json;
  return 'Object.assign(' + json + ', {predicateFn: function () { return (0, eval)(' +
    JSON.stringify(methodArgs.predicate) + '); }})';
}

async function runAutomation(tabId, method, methodArgs, frameId) {
  const src = await getAutomationSrc();
  if (!src) throw new Error('automation helpers unavailable');
  const code = src + '\n;window.__browserplugAuto.invoke(' +
    JSON.stringify(method) + ', ' + compiledArgs(methodArgs) + ');';
  const details = { code: code };
  if (frameId !== undefined && frameId !== null) details.frameId = frameId;
  const key = snapshotDocKey(tabId, frameId);
  const prevDocId = SNAPSHOT_DOCS.get(key);
  const results = await executeScriptWithRetry(tabId, details);
  const out = (results && results.length > 0) ? results[0]
    : { ok: false, error: 'no result from injected automation' };
  // A snapshot re-bases the record; anything else is a candidate for the staleness
  // diagnosis. The baseline is NOT updated on failure, so every stale ref in a batch
  // gets the same clear answer until a fresh snapshot replaces it.
  noteContentDocIdSupport(out);
  if (out && out.docId && method === 'snapshot') SNAPSHOT_DOCS.set(key, out.docId);
  else annotateStaleRef(out, methodArgs, prevDocId);
  return out;
}
// ---- end automation invoke path ----------------------------------------------------
// Slice anchor. refstale.test.mjs takes everything from the staleness header down to here
// so it can drive the real runAutomation, not just the helpers it calls. That distinction
// is the point: the helpers were green while the ARMING code around them was untested,
// which is exactly how the StreamFilter capture path stayed dead through 343 passing JS
// tests. Invariant E checks this anchor in BOTH trees.

// ---- content.read window + diagnostics --------------------------------------
// Two failures, one code path.
//
// The slice was never clamped. page_read(offset=3500) on a 149-char page
// answered `[markdown] chars 3500-3500 of 149 (no content extracted; try
// format="text" or a scope selector)`: the response ALREADY knew the length, still
// rendered a range starting past its own stated end, and then blamed the format. An
// out-of-range offset now returns the FINAL window and SAYS it did, and maxChars<=0
// is a LENGTH PROBE; sizing a document before reading it costs no body.
//
// A 290-char read of a live-chat modal on a page of 400 product cards and a
// 7-char read of a genuinely 7-char page were byte-identical in the response. So
// every read now carries the DOCUMENT's own measurements alongside the extraction;
// element count and visible-text length, and the server flags the discrepancy
// instead of returning a near-empty result as a clean success.
//
// Lifted OUT of the switch so it stays testable: this arming glue (the probe flag,
// the clamp, the metrics threading) is exactly the shape that has gone silently dead
// in this codebase before while every helper it called stayed green.
//
// The in-page halves cannot be ONE shared source string across the trees: chromium's
// scripting.executeScript takes a FUNCTION, and an MV3 ISOLATED-world script is bound
// by the extension CSP, which forbids 'unsafe-eval'; so new Function(sharedSrc)
// throws there. Duplication is forced; the twin JS tests are the compensating control.

// Extracted chars below which a read is "thin" and worth the extra document probes.
const READ_THIN_CHARS = 1000;

// Clamp semantics, exactly:
//   offset >= totalChars (and both > 0) -> return the FINAL window, flagged.
//   offset === totalChars DOES clamp. A legitimate paginator never asks for it (the
//     server only offers `continue with offset=end` while end < total), so this
//     cannot break paging; and the "exactly at the end" case is the one an agent
//     reaches by guessing, which is precisely when an empty window misleads.
//   totalChars === 0 -> NO clamp. There is no tail to return, and the empty-read
//     diagnosis owns that case; clamping would invent an offset for a page that has
//     no content at all.
const READ_WINDOW_FN = `function __fxReadWindow(content, offset, maxChars, probe){
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
}`;

// What the DOCUMENT holds, as opposed to what the extraction returned. nodes and
// bodyChars ride every read (~40 bytes); the three expensive fields only ride a thin
// or probe read, so a healthy read is as cheap as it was. Every probe is try/caught:
// a measurement must never be able to fail the read it is measuring.
const DOC_METRICS_FN = `function __fxDocMetrics(bodyText, wantDetail){
  var m = { nodes: 0, bodyChars: 0 };
  try { m.nodes = document.getElementsByTagName('*').length; } catch(e){}
  var t = bodyText;
  if (t === null || t === undefined) {
    try { t = (document.body && document.body.innerText) || ''; } catch(e){ t = ''; }
  }
  m.bodyChars = t.length;
  if (wantDetail) {
    try { m.iframes = document.getElementsByTagName('iframe').length; } catch(e){}
    try { m.dialogs = document.querySelectorAll('[aria-modal="true"],dialog[open]').length; } catch(e){}
    m.sample = t.replace(/\\s+/g, ' ').trim().slice(0, 300);
  }
  return m;
}`;

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

// The extraction cache. A markdown read of a long article spends about 2 s in Defuddle,
// and paging through it used to extract the whole document again for every offset. The
// cache lives in the page's extension sandbox for the life of the document; an entry is
// keyed by format, scope and URL (each frame has its own sandbox) and any DOM mutation, a
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
// observer and listeners call back after the driver returns. Bump rev when the object's
// methods change: a read then replaces the object an older build left in the same world.
const READ_CACHE_TTL_MS = 120000;
const READ_CACHE_MAX_CHARS = 2000000;
const READ_CACHE_FN = `function __fxReadCache(){
  var w = window, d = document, c = w.__browserplugReadCache;
  if (c && c.doc === d) {
    if (c.rev === 1) return c;
    try { c.drop(); } catch (e) {}
  }
  c = { doc: d, rev: 1, entries: {}, totals: {}, watching: false };
  c.unwatch = function(){
    c.watching = false;
    try { c.obs.disconnect(); } catch (e) {}
    w.removeEventListener('resize', c.drop);
    d.removeEventListener('load', c.onLoad, true);
  };
  c.drop = function(){
    c.entries = {};
    if (c.watching) c.unwatch();
  };
  c.onLoad = function(ev){ var t = ev && ev.target; if (t && t.tagName === 'LINK') c.drop(); };
  c.onMutations = function(recs){
    for (var i = 0; i < recs.length; i++) if (!__fxScriptChurn(recs[i], d)) { c.drop(); return; }
    for (var k in c.entries) if (!c.entries[k].scriptBlind) delete c.entries[k];
  };
  c.watch = function(){
    if (c.watching) return;
    try {
      c.obs = c.obs || new MutationObserver(c.onMutations);
      c.obs.observe(d, { subtree: true, childList: true, attributes: true, characterData: true });
      w.addEventListener('resize', c.drop);
      d.addEventListener('load', c.onLoad, true);
      c.watching = true;
    } catch (e) { c.entries = {}; }
  };
  c.pause = function(){
    if (!c.watching) return;
    var queued = [];
    try { queued = c.obs.takeRecords(); } catch (e) {}
    c.unwatch();
    if (queued.length) c.onMutations(queued);
  };
  c.resume = function(){ if (Object.keys(c.entries).length) c.watch(); };
  w.__browserplugReadCache = c;
  return c;
}
function __fxInertScript(n){
  if (!n || n.nodeName !== 'SCRIPT' || n.firstChild) return false;
  var ok = ['src', 'type', 'async', 'defer', 'crossorigin', 'integrity', 'nonce', 'referrerpolicy',
            'charset', 'nomodule', 'fetchpriority', 'blocking'];
  for (var i = 0; i < n.attributes.length; i++) if (ok.indexOf(n.attributes[i].name) < 0) return false;
  var src = n.getAttribute('src') || '', type = (n.getAttribute('type') || '').trim().toLowerCase();
  if (!src || /mathjax|katex/i.test(src)) return false;
  return !type || type === 'module' || /^(text|application)\\/(x-)?(java|ecma)script$/.test(type);
}
function __fxScriptChurn(rec, d){
  if (rec.type !== 'childList') return false;
  var p = rec.target, i;
  if (p !== d.head && p !== d.body && p !== d.documentElement) return false;
  if (p !== d.head) for (var s = rec.nextSibling; s; s = s.nextSibling) if (s.nodeType === 1) return false;
  for (i = 0; i < rec.addedNodes.length; i++) if (!__fxInertScript(rec.addedNodes[i])) return false;
  for (i = 0; i < rec.removedNodes.length; i++) if (!__fxInertScript(rec.removedNodes[i])) return false;
  return true;
}
function __fxCacheGet(c, key){
  var e = c.entries[key];
  if (e && Date.now() - e.at < ${READ_CACHE_TTL_MS}) return e;
  delete c.entries[key];
  return null;
}
function __fxCachePut(c, key, entry){
  var prev = c.totals[key];
  c.totals[key] = entry.content.length;
  if (entry.content.length <= ${READ_CACHE_MAX_CHARS}) {
    entry.at = Date.now();
    c.entries[key] = entry;
    c.watch();
  }
  return (prev !== undefined && prev !== entry.content.length) ? { previousTotal: prev } : undefined;
}`;

async function runContentRead(readTabId, data) {
  const format = data.format || 'markdown';
  const p = readWindowParams(data);
  const N = p.offset, M = p.maxChars, PROBE = p.probeOnly ? 'true' : 'false';
  const details = {};
  if (data.frameId !== undefined && data.frameId !== null) details.frameId = data.frameId;
  const cacheKey = JSON.stringify(format + '|' + (data.scope ? String(data.scope) : '') + '|');

  if (format === 'markdown') {
    // One injection when the page already has Defuddle (or a cached extraction): the driver
    // answers needsDefuddle instead of a read when it has neither, and only then is the
    // bundle injected, as a file, and the driver run again without that exit.
    const run = async (needLib) => {
      const [res] = await browser.tabs.executeScript(readTabId,
        Object.assign({}, details, { code: markdownDriver(needLib) }));
      return res;
    };
    let r = await run(true);
    if (r && r.needsDefuddle) {
      await browser.tabs.executeScript(readTabId,
        Object.assign({}, details, { file: '/vendor/defuddle.js' }));
      r = await run(false);
    }
    return r || { ok: false, error: 'no result from read driver' };
  }

  function markdownDriver(needLib) {
    // A probe still RUNS Defuddle: what it saves is context, not CPU; the length
    // has to be the length of the very extraction the next call would return.
    return '(function(){' + PAGE_STATE_FN + READ_WINDOW_FN + DOC_METRICS_FN + READ_CACHE_FN +
      'try {' +
      // Defuddle treats a contentSelector that matches NOTHING as "no selector"
      // and falls back to whole-document extraction; so an unresolvable scope
      // would silently return the ENTIRE page presented as the scope. Refuse it,
      // exactly as the text/html branch below does. (Same rule as page_act/
      // page_snapshot: a scope that was ASKED FOR never widens to the document.)
      (data.scope
        ? '  var __fxScope = ' + JSON.stringify(String(data.scope)) + ';' +
          '  var __fxHit = null;' +
          '  try { __fxHit = document.querySelector(__fxScope); }' +
          '  catch (e) { return { ok: false, error: "invalid CSS selector for scope: " + __fxScope + "; " + (e && e.message || e), pageState: __fxPageState() }; }' +
          '  if (!__fxHit) { return { ok: false, error: "scope selector matched nothing: " + __fxScope + "; the page was NOT read whole instead; re-run page_snapshot to find the current container, or drop scope to read the page.", pageState: __fxPageState() }; }'
        : '') +
      '  var __fxC = __fxReadCache(), __fxKey = ' + cacheKey + ' + location.href;' +
      '  var r = __fxCacheGet(__fxC, __fxKey), cached = !!r, changed;' +
      '  if (!r) {' +
      (needLib ? '    if (typeof Defuddle === "undefined") return { needsDefuddle: true };' : '') +
      // useAsync:false keeps Defuddle's async extractors off: they fetch() third-party
      // APIs (some with credentials) from inside the user's logged-in page. parse() is
      // synchronous today; the flag still holds if this ever moves to parseAsync(), and
      // resume() would then have to wait for it.
      '    var opts = { url: location.href, markdown: true, useAsync: false };' +
      (data.scope ? '    opts.contentSelector = ' + JSON.stringify(data.scope) + ';' : '') +
      '    __fxC.pause();' +
      '    try { var d = new Defuddle(document, opts).parse(); } finally { __fxC.resume(); }' +
      '    r = { content: d.content || "", title: d.title, site: d.site, author: d.author,' +
      '          published: d.published, wordCount: d.wordCount, scriptBlind: ' + !data.scope + ' };' +
      '    changed = __fxCachePut(__fxC, __fxKey, r);' +
      '  }' +
      '  var content = r.content;' +
      '  var w = __fxReadWindow(content, ' + N + ', ' + M + ', ' + PROBE + ');' +
      '  var m = __fxDocMetrics(null, ' + PROBE + ' || w.totalChars < ' + READ_THIN_CHARS + ');' +
      '  return { ok: true, format: "markdown", url: location.href, title: r.title || document.title,' +
      '           site: r.site || undefined, author: r.author || undefined, published: r.published || undefined,' +
      '           wordCount: r.wordCount || undefined, totalChars: w.totalChars, content: w.content,' +
      '           offset: w.offset, requestedOffset: w.requestedOffset, offsetClamped: w.offsetClamped,' +
      '           probe: w.probe, docMetrics: m, pageState: __fxPageState(),' +
      '           cached: cached || undefined, contentChanged: changed };' +
      '} catch (e) {' +
      // The Defuddle-failure fallback is the branch a CSP or a bundle crash lands
      // on, so it needs the clamp and the metrics just as much; and it gets
      // bodyChars for free, having already computed the text.
      '  var t = (document.body && document.body.innerText) || "";' +
      '  var w2 = __fxReadWindow(t, ' + N + ', ' + M + ', ' + PROBE + ');' +
      '  var m2 = __fxDocMetrics(t, ' + PROBE + ' || w2.totalChars < ' + READ_THIN_CHARS + ');' +
      '  return { ok: true, format: "text", fallback: "defuddle failed: " + String(e && e.message || e),' +
      '           url: location.href, title: document.title, totalChars: w2.totalChars, content: w2.content,' +
      '           offset: w2.offset, requestedOffset: w2.requestedOffset, offsetClamped: w2.offsetClamped,' +
      '           probe: w2.probe, docMetrics: m2, pageState: __fxPageState() };' +
      '}' +
      '})()';
  }

  const scopeExpr = data.scope
    ? 'document.querySelector(' + JSON.stringify(data.scope) + ')'
    : (format === 'html' ? 'document.documentElement' : 'document.body');
  const contentExpr = format === 'html' ? 'el.outerHTML' : '(el.innerText || "")';
  // Reuse the string we already have as bodyChars ONLY when it really is the body's
  // own text: with a scope it is a subtree's, and with html it is markup.
  const bodyTextExpr = (format === 'text' && !data.scope) ? 'content' : 'null';
  const driver = '(function(){' + PAGE_STATE_FN + READ_WINDOW_FN + DOC_METRICS_FN + READ_CACHE_FN +
    'var el = ' + scopeExpr + ';' +
    'if (!el) return { ok: false, error: "scope selector matched nothing: " + ' + JSON.stringify(data.scope || '') + ', pageState: __fxPageState() };' +
    'var __fxC = __fxReadCache(), __fxKey = ' + cacheKey + ' + location.href;' +
    'var hit = __fxCacheGet(__fxC, __fxKey), cached = !!hit, changed;' +
    'if (!hit) {' +
    '  __fxC.pause();' +
    '  try { hit = { content: ' + contentExpr + ' }; } finally { __fxC.resume(); }' +
    '  changed = __fxCachePut(__fxC, __fxKey, hit);' +
    '}' +
    'var content = hit.content;' +
    'var w = __fxReadWindow(content, ' + N + ', ' + M + ', ' + PROBE + ');' +
    'var m = __fxDocMetrics(' + bodyTextExpr + ', ' + PROBE + ' || w.totalChars < ' + READ_THIN_CHARS + ');' +
    'return { ok: true, format: ' + JSON.stringify(format) + ', url: location.href, title: document.title,' +
    '         totalChars: w.totalChars, content: w.content, offset: w.offset,' +
    '         requestedOffset: w.requestedOffset, offsetClamped: w.offsetClamped, probe: w.probe,' +
    '         docMetrics: m, pageState: __fxPageState(), cached: cached || undefined,' +
    '         contentChanged: changed };' +
    '})()';
  const [r] = await browser.tabs.executeScript(readTabId, Object.assign({}, details, { code: driver }));
  return r || { ok: false, error: 'no result from read driver' };
}

// ---- content.act step deadline ----------------------------------------------
// A click that navigates can leave tabs.executeScript's promise UNRESOLVED; the
// document the toolkit was injected into is torn down mid-call, so nothing ever
// comes back. Without a deadline the whole batch then blows through the SERVER's
// request budget and the agent gets a bare "Request timed out after 22.1 seconds":
// indistinguishable from "nothing happened", when in fact the click may well have
// worked (a lazy "load more" that landed a beat late), or navigated; once, to the
// WRONG page entirely. So every step races a deadline, and a step that overruns it
// still reports what the tab looks like NOW: url/title, whether a navigation
// committed since the step began, and the network/console digest. The batch always
// answers; "timed out" stops meaning "no state change".
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
// outcome digest. Lifted OUT of the switch on purpose: the arming glue (budget
// threading, notRunSteps, the urlBefore capture) is exactly the kind of code that
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
    // Post-action observation: clicks get a brief settle window so the
    // digest reflects the requests/navigation the click triggered.
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
        // inspect resolves each watch target on its OWN args, so the step's
        // `within` has to be pushed down into every entry; otherwise a scoped
        // step reports panel 1's state after acting on panel 2. An entry that
        // carries its own `within` keeps it.
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
          const execDetails = { code: evalWrapperCode(data.script, data.world) };
          if (data.frameId !== undefined && data.frameId !== null) execDetails.frameId = data.frameId;
          else if (data.allFrames) execDetails.allFrames = true;
          // Mid-navigation, the injection binds to Firefox's TRANSIENT
          // placeholder document (right URL, readyState "complete", but empty;
          // no title, no body), so the document's own readyState cannot expose
          // the problem. tab.status can: it reads "loading" for the whole
          // in-flight window. Check on both sides of the eval and flag if
          // either saw a load in progress.
          let tabLoadingBefore = false;
          try { tabLoadingBefore = (await browser.tabs.get(execTabId)).status === 'loading'; } catch (e) {}
          let executeResults;
          try {
            executeResults = await executeScriptWithRetry(execTabId, execDetails);
          } catch (injectErr) {
            if (NON_CLONABLE_RE.test(String((injectErr && injectErr.message) || injectErr))) {
              sendError(id, 'SCRIPT_ERROR', NON_CLONABLE_HELP);
              break;
            }
            throw injectErr;
          }
          // The caller's own code threw: report its error, which no retry would change.
          const first = executeResults && executeResults.length > 0 ? executeResults[0] : null;
          if (!data.allFrames && first && typeof first === 'object' && first.__browserplugEval &&
              first.error !== undefined) {
            sendError(id, 'SCRIPT_ERROR', String(first.error));
            break;
          }
          let tabLoadingAfter = false;
          try { tabLoadingAfter = (await browser.tabs.get(execTabId)).status === 'loading'; } catch (e) {}
          let docReadyState = null;
          let evalDocBefore = null;
          let evalDocAfter = null;
          const unwrapEval = (r) => {
            if (r && typeof r === 'object' && r.__browserplugEval) {
              if (!docReadyState) docReadyState = r.readyState;
              if (!evalDocBefore) evalDocBefore = r.docId || null;
              if (!evalDocAfter) evalDocAfter = r.docIdAfter || null;
              return r.value;
            }
            return r;
          };
          // executeScript returns an array of results (one per injected frame)
          let result = data.allFrames
            ? (executeResults || []).map(unwrapEval)
            : unwrapEval(executeResults && executeResults.length > 0 ? executeResults[0] : null);
          if (data.world === 'main' && result && typeof result === 'object' && !data.allFrames) {
            if (result.error) {
              sendError(id, 'SCRIPT_ERROR', result.error);
              break;
            }
            if (result.readyState && !docReadyState) docReadyState = result.readyState;
            // The main-world envelope carries its own document identity (captured in the
            // isolated wrapper around the page-compartment eval), so world="main" gets the
            // same mid-eval replacement check as world="isolated".
            if (!evalDocBefore) evalDocBefore = result.docId || null;
            if (!evalDocAfter) evalDocAfter = result.docIdAfter || null;
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
          // tab_id is optional on every page_* tool, so it is optional here too.
          // Without this, getAllFrames({tabId: undefined}) THREW, the error was
          // swallowed by the caller's try/except, and page_snapshot() silently
          // listed no iframes at all on its most common call path (no tab_id);
          // while its docstring promised them.
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
    // Preserve recoverable codes (STALE_TAB / CONTENT_UNREACHABLE) and pass
    // their already-actionable message through verbatim; no generic prefix.
    if (error.code) {
      sendError(id, error.code, error.message);
    } else {
      sendError(id, 'API_ERROR', `Content API error: ${error.message}`);
    }
  }
}

// Navigation handlers
// Capture the current site's Service Worker registrations for the loaded
// top-level document. The content-script sandbox does NOT expose
// navigator.serviceWorker, so we reach the page's real API through the Xray
// waiver (window.wrappedJSObject.navigator.serviceWorker). That path is
// CSP-immune (unlike a main-world eval, which strict-CSP sites block) and only
// reads primitives back out, so the result structured-clones cleanly.
// Best-effort + time-boxed: a site that registers its SW after settle may be
// missed, and any failure (privileged page, injection blocked) yields null;
// this never throws.
async function captureServiceWorkers(tabId) {
  const code = `(async () => {
    try {
      const nav = window.wrappedJSObject && window.wrappedJSObject.navigator;
      const swc = nav && nav.serviceWorker;
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
  })()`;
  try {
    const [res] = await browser.tabs.executeScript(tabId, { code });
    return res || null;
  } catch (e) {
    return null; // privileged page / injection blocked → nothing to report
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
const DOM_STABLE_MS = 100;
const DOM_POST_LOAD_MIN_MS = 50;
const DOM_STABLE_MAX_MS = 3000;
const DOM_ARM_MS = 5000;
// (stop, stableMs, notBefore, early): reads the watch, installing it first when the
// document has none. It disconnects itself when asked to stop, on an error, and once the
// DOM has been quiet for stableMs at or after notBefore, so the last probe is also the
// stop. `early` only installs, and does nothing before the document has a root element.
const DOM_WATCH_CODE = `(function (stop, stableMs, notBefore, early) {
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
})`;

async function probeDomWatch(tabId, stop, stableMs, notBefore, early) {
  const args = [!!stop, stableMs == null ? 'null' : stableMs, notBefore || 0, !!early].join(',');
  const [r] = await browser.tabs.executeScript(tabId, {
    code: DOM_WATCH_CODE + '(' + args + ')',
    runAt: early ? 'document_start' : 'document_idle', frameId: 0
  });
  return r || null;
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
//   httpStatus: the MAIN DOCUMENT's response status. A 404 or a 403 with a full
//     HTML body renders as a perfectly normal page, and the status was previously
//     buried in the request digest among every failed sub-resource. Scanning
//     BACKWARDS over main_frame entries yields the FINAL status under either
//     redirect model (see the engine note below).
//   urlBefore: where the tab was before the navigation was issued, so "the
//     navigation never took effect and the tab is still where it was" stops being
//     indistinguishable from "it arrived at exactly that URL".
// gecko: FF152+ REPLACES the channel on a redirect, so one navigation's hops
// land on SEVERAL entries and the continuation is the LATEST of them.
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
      // Unified navigation with settle-by-default: performs the op, then waits
      // until the page actually settles (commit + load + network-quiet +
      // optional selector) and reports the POST-settle url/title plus a digest
      // of failed requests / console errors raised during the transition.
      case 'navigation.navigate': {
        const androidNavFileMsg = androidFileUrlRefusal(data.url, 'navigate to');
        if (androidNavFileMsg) {
          sendError(id, 'FILE_URL_UNSUPPORTED', androidNavFileMsg);
          break;
        }
        const navTabId = await resolveTabId(data);
        const op = data.op || 'goto';
        // Refused before anything changes, the tab's routing included.
        if (op === 'goto' && !data.url) { sendError(id, 'INVALID_PARAMETER', 'url is required for goto'); return; }
        // Opt-in routing: set/clear this tab's SOCKS proxy BEFORE navigating so
        // the new navigation (and its DNS) goes through it. Bad value → abort
        // (fail-closed), never a silent direct load.
        if (data.proxy !== undefined) {
          const pp = parseProxyEndpoint(data.proxy);
          // A non-direct proxy needs the optional "proxy" permission; refuse
          // rather than navigate direct with routing that won't be enforced.
          if (pp.ok && !pp.direct && !(await hasProxyPermission())) {
            sendError(id, 'PROXY_PERMISSION_REQUIRED', PROXY_PERM_ERROR); return;
          }
          if (pp.ok && !pp.direct) ensureProxyListener();
          const perr = applyTabProxy(navTabId, data.proxy);
          if (perr) { sendError(id, 'INVALID_PROXY', perr); return; }
        }
        const urlBefore = await navUrlBefore(navTabId);
        const t0 = Date.now();
        const commitBefore = (TAB_NAV.get(navTabId) || {}).lastCommitted;
        // Record the attempt up front so (a) a sibling navigate started after us can
        // be detected as having "superseded" this one, and (b) an about:blank
        // refusal can name the pending URL.
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
        let result = { ok: true, op: op, tabId: navTabId };
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

          // Current-site Service Worker registrations for the loaded document
          // (best-effort, quiet when the site has none). Only http(s) documents
          // can host a SW and accept content-script injection.
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
          // about:blank sentinel: a goto to a real URL that never rendered a document.
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
  // Firefox for Android has no windows API at all, so every one of these would throw a
  // raw TypeError ("can't access property \"create\", browser.windows is undefined") and
  // hand the agent an internal error instead of a fact about the platform. Say what is
  // true and what to use instead, the way the Blink gaps do (PROXY_UNSUPPORTED,
  // EVAL_UNAVAILABLE). A phone shows one tab at a time and has nothing to move, size or
  // focus, so there is no degraded version of these to offer -- only the tabs actions.
  if (!browser.windows) {
    sendError(id, 'WINDOWS_UNSUPPORTED',
      `${action} needs the windows API, which Firefox for Android does not implement. ` +
      'A phone has no window to move, resize, focus or close: it shows one tab at a ' +
      'time. Use the tabs actions instead (list / open / activate / close), which work ' +
      'here; every tab reports windowId, but those ids do not name anything you can act on.');
    return;
  }
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
        // Same commit honesty as tabs.create: never echo the REQUESTED url as the
        // tab's url while the tab is actually parked on about:blank; the
        // incognito/new-window open path must not be the one place a false
        // "loaded at <url>" survives.
        if (data.url && data.url !== 'about:blank') {
          const wtab = (newWindow.tabs && newWindow.tabs[0]) || null;
          if (wtab) {
            const c = await waitForCommit(wtab.id, t0winCommit, 8000, data.url);
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
    // Handle specific window errors
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

// REMOVED: onStartup listener to prevent race conditions during testing
// Extension will only connect on explicit user actions or valid storage events

// Request monitoring state
const activeMonitors = new Map(); // monitor_id -> monitor config
const retiredMonitors = new Map(); // monitor_id -> { at, reason, monitor } (data still readable)
const evictedMonitors = new Map(); // monitor_id -> { at, reason }; data gone, id remembered
const capturedRequests = new Map(); // monitor_id -> array of requests
const requestDetails = new Map(); // request_id -> full request details
const capturedResponseBodies = new Map(); // request_id -> response body data
const streamCaptures = new Map(); // webRequest request_id -> in-flight StreamFilter buffer
const parkedBodies = new Map(); // webRequest request_id -> { cap, at, bytes }: finished, awaiting its headers
let monitorIdSeq = 0; // bumped per start_monitoring so two starts in the same ms still get unique ids

// A body-capturing monitor is EXPENSIVE and invisible: it holds the blocking
// webRequest listeners and forces `Accept-Encoding: identity` on every matching
// request (the user downloads uncompressed) for as long as it lives. An agent that
// forgets its stop_monitoring must not leave that armed for the browser session,
// so every monitor dies on its own three ways:
//   - TTL: expires after ttl_seconds (default 10 min, clamped 10s..1h)
//   - server gone: captures only ever feed the server, so a WebSocket that has been
//     down for the grace period retires them. The grace must comfortably outlast a
//     server /restart (reconnects in seconds); a blip must NOT kill a live monitor.
//   - retention: a stopped/expired monitor KEEPS its captured data for a while, so
//     the normal start -> stop -> list -> get flow still works after the stop; the
//     sweeper evicts it once the window passes.
const MONITOR_TTL_DEFAULT_MS = 10 * 60 * 1000;
const MONITOR_TTL_MIN_MS = 10 * 1000;
const MONITOR_TTL_MAX_MS = 60 * 60 * 1000;
const MONITOR_RETENTION_MS = 5 * 60 * 1000;
const MONITOR_DISCONNECT_GRACE_MS = 60 * 1000;
const MONITOR_SWEEP_INTERVAL_MS = 15 * 1000;
// Hard caps on the capture maps, in the spirit of the always-on ring buffers
// (TAB_REQUESTS 300 / TAB_CONSOLE 600): oldest-first (FIFO) eviction, so a
// long-running monitor on a busy pattern can never grow without bound. The bounded
// maps are requestDetails, capturedRequests (per monitor), capturedResponseBodies
// (count AND total bytes), streamCaptures (each in-flight buffer stops at the
// clamped max_body_size, and the whole map is dropped with the last monitor) and
// parkedBodies (its own count cap, its bytes spend the body byte budget, and an entry
// is pruned after PARKED_BODY_MAX_AGE_MS or once no active monitor holds its request).
// Eviction is MONITOR-AWARE: dropping a requestDetails entry also drops it from its
// monitor's list, so list_captured can never advertise a request get_content has
// already lost.
const REQUEST_DETAILS_MAX = 500;
const CAPTURED_PER_MONITOR_MAX = 300;
const CAPTURED_BODIES_MAX = 100;
// max_body_size is agent-settable, so it is CLAMPED: without a ceiling one monitor
// could ask for a body bigger than the whole byte budget and evict every other body
// (including its own) the moment it landed. The total budget stays a comfortable
// multiple of one clamped body.
const MAX_BODY_SIZE_DEFAULT = 50000;
const MAX_BODY_SIZE_LIMIT = 2 * 1024 * 1024;
const CAPTURED_BODIES_MAX_BYTES = 20 * 1024 * 1024;
const EVICTED_MONITORS_MAX = 50;
// A parked body's headers event is late by Firefox's batching (a 250 ms idle-dispatch
// timeout), seconds at worst under load; past this it is not coming.
const PARKED_BODY_MAX_AGE_MS = 30 * 1000;
// Redirect hops on one requestId (Firefox before 152, and internal redirects): their
// headers describe the hop, not the body that ends the chain.
const REDIRECT_STATUS_CODES = new Set([301, 302, 303, 307, 308]);

let monitorSweepTimer = null;
let monitorsLastServedAt = Date.now(); // last moment the server was there to consume captures

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

// Where a monitor id stands RIGHT NOW; list_captured/get_content answer very
// differently for "still running" vs "ended, data still readable" vs "ended and
// evicted" vs "never existed", and an empty list for any of the last three is a lie.
function monitorState(monitorId) {
  if (activeMonitors.has(monitorId)) return 'active';
  if (retiredMonitors.has(monitorId)) return 'retired';
  if (evictedMonitors.has(monitorId)) return 'expired';
  return 'unknown';
}

// The monitor's config outlives its retirement (its captures are still readable, and
// reading them must honour the policy (e.g. sensitive_headers) it was armed with).
function monitorConfig(monitorId) {
  const active = activeMonitors.get(monitorId);
  if (active) return active;
  const retired = retiredMonitors.get(monitorId);
  return (retired && retired.monitor) || null;
}

// The ONE teardown path; explicit stop, TTL expiry and disconnect cleanup all go
// through it, so the blocking listeners can never outlive the last active monitor.
//
// Body capture needs no separate disarm step: the StreamFilter is attached
// per-request by monitorWantingBody(), which reads activeMonitors live, so
// removing the monitor here already stops any further request from being filtered.
function retireMonitor(monitorId, reason) {
  const monitor = activeMonitors.get(monitorId);
  if (!monitor) return null;
  activeMonitors.delete(monitorId);
  retiredMonitors.set(monitorId, { at: Date.now(), reason: reason, monitor: monitor });
  monitor.status = 'stopped';

  if (activeMonitors.size === 0) {
    stopWebRequestMonitoring();
    // No monitor left to own them. An in-flight filter keeps writing the response
    // through to the page (ondata writes FIRST); its finalize simply finds no buffer
    // and stores nothing.
    streamCaptures.clear();
  }
  pruneParkedBodies(Date.now());
  console.log(`🛑 Monitor ${monitorId} retired (${reason})`);
  return monitor;
}

// Drop the parked bodies that can no longer be committed: older than
// PARKED_BODY_MAX_AGE_MS, or on a request no active monitor holds (its headers event
// reaches the request record only through an active monitor that matched it).
function pruneParkedBodies(now) {
  for (const [requestId, parked] of parkedBodies) {
    const detail = requestDetails.get(requestId);
    const ids = detail ? (detail.monitor_ids || [detail.monitor_id]) : [];
    if (now - parked.at > PARKED_BODY_MAX_AGE_MS || !ids.some(m => activeMonitors.has(m))) {
      parkedBodies.delete(requestId);
    }
  }
}

// Drop everything a retired monitor captured, once its retention window has passed.
function evictMonitorData(monitorId, reason) {
  capturedRequests.delete(monitorId);
  for (const [requestId, detail] of requestDetails) {
    const ids = detail.monitor_ids || [detail.monitor_id];
    if (ids.indexOf(monitorId) === -1) continue;
    // shared with another live monitor? then only this monitor's claim goes
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

// Remove a request from its monitor's list, so what list_captured advertises and
// what get_content can still answer for never drift apart.
function dropFromMonitorList(requestId, monitorId) {
  const list = capturedRequests.get(monitorId);
  if (!list) return;
  const i = list.findIndex(r => r.request_id === requestId);
  if (i >= 0) list.splice(i, 1);
}

// Drop a request from ONE monitor's view. Its detail/body are shared with every other
// monitor that matched the same request, so they may only be deleted once no monitor
// still lists it; otherwise a sibling's list would advertise a request get_content
// could no longer serve.
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

// Every monitor that captured this request may fetch it (overlapping url_patterns are
// legitimate); only a monitor that never saw it is refused.
function requestBelongsToMonitor(detail, monitorId) {
  if (!monitorId) return true; // protocol-level callers may omit it
  const ids = detail.monitor_ids || [detail.monitor_id];
  return ids.indexOf(monitorId) !== -1;
}

function enforceCaptureCaps() {
  while (requestDetails.size > REQUEST_DETAILS_MAX) {
    const oldest = requestDetails.keys().next().value;
    const detail = requestDetails.get(oldest);
    requestDetails.delete(oldest);
    capturedResponseBodies.delete(oldest);
    // un-list it from EVERY monitor that held it, or list_captured would keep
    // advertising a request get_content can no longer serve.
    if (detail) {
      for (const m of (detail.listed_in || [detail.monitor_id])) dropFromMonitorList(oldest, m);
    }
  }
  let bytes = 0;
  for (const record of capturedResponseBodies.values()) bytes += bodyBytes(record);
  for (const parked of parkedBodies.values()) bytes += parked.bytes;
  // Parked bodies go first: they have not been through content_types_to_capture yet, and
  // on a page load most of them are images and scripts the default list rejects, which
  // must not push out bodies that already passed it.
  while (parkedBodies.size > CAPTURED_BODIES_MAX ||
         (parkedBodies.size > 0 && bytes > CAPTURED_BODIES_MAX_BYTES)) {
    const oldest = parkedBodies.keys().next().value;
    bytes -= parkedBodies.get(oldest).bytes;
    parkedBodies.delete(oldest);
    const detail = requestDetails.get(oldest);
    if (detail) detail.body_evicted = true;
  }
  while (capturedResponseBodies.size > CAPTURED_BODIES_MAX || bytes > CAPTURED_BODIES_MAX_BYTES) {
    const oldest = capturedResponseBodies.keys().next().value;
    if (oldest === undefined) break;
    bytes -= bodyBytes(capturedResponseBodies.get(oldest));
    capturedResponseBodies.delete(oldest);
    // The request itself stays listed (its metadata is still true), but get_content
    // must be able to say the BODY was evicted rather than "none was captured".
    const detail = requestDetails.get(oldest);
    if (detail) detail.body_evicted = true;
  }
}

function sweepMonitors() {
  const now = Date.now();
  // Only count time the server was actually unreachable: while connected the clock
  // keeps resetting, so a reconnect within the grace period costs the monitor nothing.
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

  pruneParkedBodies(now);
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

// The PRIMARY body-capture path: webRequest.filterResponseData (StreamFilter)
// attached per monitored request. Network-layer, so it sees page-realm fetch/
// XHR/axios AND document loads, unlike the legacy content-script fetch wrapper,
// which lives in the content-script sandbox where (a) page requests never pass
// and (b) modern Firefox no longer lets `window.fetch =` shadow the sandbox's
// own fetch binding (verified live: the wrapper never installs). Records are
// keyed by the WebRequest requestId, so requests.get_content finds them by
// direct lookup; no URL/method/status correlation needed.
function monitorWantingBody(details) {
  for (const monitor of activeMonitors.values()) {
    if (monitor.options && monitor.options.capture_response_bodies &&
        shouldCaptureRequest(monitor, details)) {
      return monitor;
    }
  }
  return null;
}

// Background-side twin of content.js shouldCaptureContentType (same semantics:
// empty list = capture everything; entries match by substring, "type/*" by family).
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

function attachStreamFilter(details, monitor) {
  const opts = monitor.options || {};
  const maxSize = clampMaxBodySize(opts.max_body_size);
  const filter = browser.webRequest.filterResponseData(details.requestId);
  // Carry the requesting monitor's capture policy on the buffer: the monitor can be
  // retired (stop / TTL) before the response finishes, and finalize still has to
  // honour the policy the capture was armed with.
  const cap = { chunks: [], received: 0, truncated: false, capReached: false,
                finalized: false, maxSize: maxSize, opts: opts, monitorId: monitor.id };
  streamCaptures.set(details.requestId, cap);
  filter.ondata = (event) => {
    // Pass the data through FIRST; a filter that fails to write starves the
    // page of its own response.
    filter.write(event.data);
    if (cap.finalized) return;
    if (cap.capReached) {
      // Data STILL arriving after the buffer filled exactly on the cap: NOW we know
      // bytes were really dropped. (A body ending exactly on max_body_size is
      // complete; flagging it truncated at the moment the cap tripped was a lie.)
      cap.truncated = true;
      finalizeStreamCapture(details.requestId);
      try { filter.disconnect(); } catch (e) { /* already disconnected */ }
      return;
    }
    cap.chunks.push(event.data);
    cap.received += event.data.byteLength;
    if (cap.received >= maxSize) {
      cap.capReached = true;
      if (cap.received > maxSize) {
        // This chunk overshot the cap, so the kept bytes get clamped: truncation is
        // certain. Finalize now and hand the rest of the stream back to Firefox so a
        // large download costs nothing further.
        cap.truncated = true;
        finalizeStreamCapture(details.requestId);
        try { filter.disconnect(); } catch (e) { /* already disconnected */ }
      }
      // Exactly ON the cap: hold the filter one more event; onstop means a perfect
      // fit (not truncated), another ondata means truncated.
    }
  };
  filter.onstop = () => {
    try { filter.close(); } catch (e) { /* already closed */ }
    finalizeStreamCapture(details.requestId);
  };
  filter.onerror = () => {
    // The filter failed (an aborted request, or one Firefox would not filter): drop the
    // partial buffer, and keep filter.error so get_content can say why there is no body.
    if (cap.finalized) return;
    streamCaptures.delete(details.requestId);
    noteBodyOutcome(details.requestId, `filter_error:${filter.error || 'unknown'}`);
  };
}

// Why a request has or lacks a body, for get_content's note: stored, no_bytes,
// type_excluded:<type>, filter_error:<error>, attach_failed:<message>,
// awaiting_headers or request_failed:<error>.
function noteBodyOutcome(requestId, outcome) {
  const detail = requestDetails.get(requestId);
  if (detail) detail.body_outcome = outcome;
}

// Which media types are TEXT (return the string) vs. binary (base64, and withheld
// unless include_binary). Anything JSON-, XML-, JS- or form-shaped is text;
// including the whole `+json`/`+xml` suffix family (application/vnd.api+json …),
// which the old `application/json|text/|application/xml` regex called binary and
// handed back to agents as base64. null = no Content-Type: don't guess, sniff the
// bytes (see decodeTextBytes).
function isTextualContentType(contentType) {
  if (!contentType) return null;
  const type = String(contentType).split(';')[0].trim().toLowerCase();
  if (!type) return null;
  if (type.startsWith('text/')) return true;
  if (/^application\/(json|xml|javascript|ecmascript|x-javascript|x-ecmascript|x-www-form-urlencoded|csv|graphql|x-ndjson|ndjson|yaml|x-yaml|xhtml\+xml)$/.test(type)) return true;
  if (/^application\/[\w.+-]+\+(json|xml)$/.test(type)) return true; // vnd.api+json, atom+xml, soap+xml…
  return false;
}

// A strict (fatal) UTF-8 decode is the honest test for "is this text?": invalid byte
// sequences throw, so a clean decode means text and a throw means binary. A body cut
// at max_body_size can end mid-codepoint; trim the dangling bytes (a UTF-8 sequence
// is at most 4 bytes) before calling it binary.
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

// btoa() needs a binary string, and String.fromCharCode.apply(null, hugeArray)
// blows the call stack; build it in 32K slices.
function base64FromBytes(bytes) {
  let binary = '';
  const SLICE = 0x8000;
  for (let i = 0; i < bytes.length; i += SLICE) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + SLICE));
  }
  return btoa(binary);
}

function finalizeStreamCapture(requestId) {
  const cap = streamCaptures.get(requestId);
  if (!cap || cap.finalized) return;
  cap.finalized = true;
  streamCaptures.delete(requestId);
  // An empty body (a 204, a HEAD, a zero-length response) must not shadow the
  // metadata-only fallback with an empty string.
  if (cap.received === 0) {
    noteBodyOutcome(requestId, 'no_bytes');
    return;
  }
  // Firefox delivers non-blocking webRequest events late and in batches, while the
  // filter's bytes arrive at once, so a small fast response can stop before its
  // onHeadersReceived has been processed. Its Content-Type is unknown until then: park
  // the bytes, and the headers (or onCompleted) commit them.
  const detail = requestDetails.get(requestId);
  if (detail && !detail.headers_seen && !detail.completed) {
    parkedBodies.set(requestId, { cap: cap, at: Date.now(),
                                  bytes: Math.min(cap.received, cap.maxSize) });
    noteBodyOutcome(requestId, 'awaiting_headers');
    enforceCaptureCaps();
    return;
  }
  storeStreamBody(requestId, cap);
}

// Commit a parked body once its request's final headers have been processed, or the
// request completed without them.
function commitParkedBody(requestId) {
  const parked = parkedBodies.get(requestId);
  if (!parked) return;
  const detail = requestDetails.get(requestId);
  if (detail && !detail.headers_seen && !detail.completed) return;
  parkedBodies.delete(requestId);
  storeStreamBody(requestId, parked.cap);
}

// A failed request never gets the onCompleted that would commit its parked body.
function dropParkedBody(requestId, error) {
  if (parkedBodies.delete(requestId)) {
    noteBodyOutcome(requestId, `request_failed:${error || 'unknown'}`);
  }
}

// Judge a finished capture against the content_types_to_capture it was armed with, and
// store it. Called once the response headers are known, or can no longer arrive.
function storeStreamBody(requestId, cap) {
  const detail = requestDetails.get(requestId);
  const opts = cap.opts || {};
  const contentType = (detail && detail.response_content_type) || '';
  if (!shouldCaptureContentTypeBG(contentType, opts.content_types_to_capture)) {
    noteBodyOutcome(requestId, `type_excluded:${contentType || 'none'}`);
    return;
  }
  // The ondata loop stops AT the cap but a chunk can overshoot it; clamp, so the
  // bytes we report are exactly the bytes we hand back.
  let bytes = mergeChunks(cap.chunks);
  let truncated = !!cap.truncated;
  if (bytes.length > cap.maxSize) {
    bytes = bytes.subarray(0, cap.maxSize);
    truncated = true;
  }
  // Text-ish bodies are decoded to a string; anything else is base64 (it only got
  // captured at all because the caller widened content_types_to_capture to admit
  // it). requests.get_content withholds base64 bodies unless include_binary; a
  // binary blob must never land in an agent's context by accident.
  let body = '';
  let encoding = 'utf-8';
  try {
    const textual = isTextualContentType(contentType);
    let text = null;
    if (textual === true) {
      const charsetMatch = /charset=([^;\s]+)/i.exec(contentType);
      let decoder;
      try { decoder = new TextDecoder(charsetMatch ? charsetMatch[1].trim() : 'utf-8'); }
      catch (e) { decoder = new TextDecoder('utf-8'); }
      text = decoder.decode(bytes);
    } else if (textual === null) {
      text = decodeTextBytes(bytes, truncated); // no Content-Type: let the bytes decide
    }
    if (text !== null) {
      body = text;
    } else {
      encoding = 'base64';
      body = base64FromBytes(bytes);
    }
  } catch (e) {
    console.warn('stream capture decode failed:', e.message);
    body = '';
    encoding = 'utf-8';
  }
  // The server's Content-Length (identity encoding is forced on monitored requests,
  // so it is the real body size); the only honest way to say how much a truncated
  // capture left behind.
  const fullSize = detail && typeof detail.response_content_length === 'number' &&
                   detail.response_content_length > 0 ? detail.response_content_length : undefined;
  capturedResponseBodies.set(requestId, {
    request_id: requestId,
    url: detail ? detail.url : undefined,
    method: detail ? detail.method : 'GET',
    status_code: detail ? detail.status_code : undefined,
    content_type: contentType || null,
    // The RAW byte count of what we KEPT; a base64 body's string length is ~4/3 of
    // the truth, and cap.received overshoots the cap by up to one chunk.
    content_length: bytes.length,
    full_size_bytes: fullSize,
    response_body: body,
    encoding: encoding,
    truncated: truncated,
    timestamp: new Date().toISOString(),
    source: 'stream_filter'
  });
  noteBodyOutcome(requestId, 'stored');
  enforceCaptureCaps();
}

// WebRequest listener functions
let onBeforeRequestListener = null;
let onBeforeSendHeadersListener = null;
let onHeadersReceivedListener = null;
let onCompletedListener = null;
let onErrorOccurredListener = null;


function startWebRequestMonitoring(monitor) {
  console.log(`🔍 Starting WebRequest monitoring for monitor ${monitor.id}`);

  if (activeMonitors.size === 0) {
    // First monitor - set up listeners
    setupWebRequestListeners();
  }
}

function setupWebRequestListeners() {
  if (onBeforeRequestListener) {
    // Already set up
    return;
  }

  console.log('🔧 Setting up WebRequest API listeners');

  // Before request - captures initial request data, and attaches the
  // StreamFilter body capture for requests a body-capturing monitor matches.
  // (filterResponseData must be called synchronously inside a BLOCKING
  // onBeforeRequest listener; hence "blocking" + the webRequestBlocking perm.)
  onBeforeRequestListener = function(details) {
    handleWebRequestEvent('onBeforeRequest', details);
    const bodyMonitor = monitorWantingBody(details);
    if (bodyMonitor && !streamCaptures.has(details.requestId)) {
      try {
        attachStreamFilter(details, bodyMonitor);
      } catch (e) {
        console.warn('stream filter attach failed:', e.message);
        noteBodyOutcome(details.requestId, `attach_failed:${e.message}`);
      }
    }
  };

  // Ask for an UNCOMPRESSED response on monitored requests: the stream filter
  // then sees plain text (no gzip/brotli to undo), and servers answering
  // identity requests send a real Content-Length, which also fixes the
  // metadata-only size_bytes for bodies we don't capture.
  onBeforeSendHeadersListener = function(details) {
    // This is also where request_headers come from. It is the only event we register
    // that carries them, and it must NOT be a separate onSendHeaders listener:
    // asking for ["requestHeaders"] extra-info on onSendHeaders breaks
    // filterResponseData on the same request; the StreamFilter then captures
    // nothing (live-verified FF152, twice; the listener alone is harmless, the
    // extra-info flag is what does it). Reading them here is also MORE honest:
    // these are the headers as the PAGE composed them, before the Accept-Encoding
    // override below, so we never report our own mutation back as the page's.
    handleWebRequestEvent('onBeforeSendHeaders', details);
    if (!monitorWantingBody(details)) return;
    const headers = details.requestHeaders || [];
    let found = false;
    for (const h of headers) {
      if (h.name.toLowerCase() === 'accept-encoding') {
        h.value = 'identity';
        found = true;
      }
    }
    if (!found) headers.push({ name: 'Accept-Encoding', value: 'identity' });
    return { requestHeaders: headers };
  };

  // Headers received - captures response headers and status. Non-blocking on purpose:
  // registered for <all_urls>, a blocking listener would hold up every request in the
  // browser while a monitor lives. Its events reach us late and batched, so one can land
  // after the stream filter has stopped; that body waits in parkedBodies until here.
  onHeadersReceivedListener = function(details) {
    handleWebRequestEvent('onHeadersReceived', details);
    commitParkedBody(details.requestId);
  };

  // Completed - captures final timing and status
  onCompletedListener = function(details) {
    handleWebRequestEvent('onCompleted', details);
    commitParkedBody(details.requestId);
  };

  // Error - captures failed requests
  onErrorOccurredListener = function(details) {
    handleWebRequestEvent('onErrorOccurred', details);
    dropParkedBody(details.requestId, details.error);
  };

  // Register listeners with browser
  browser.webRequest.onBeforeRequest.addListener(
    onBeforeRequestListener,
    { urls: ["<all_urls>"] },
    ["requestBody", "blocking"]
  );

  browser.webRequest.onBeforeSendHeaders.addListener(
    onBeforeSendHeadersListener,
    { urls: ["<all_urls>"] },
    ["requestHeaders", "blocking"]
  );

  browser.webRequest.onHeadersReceived.addListener(
    onHeadersReceivedListener,
    { urls: ["<all_urls>"] },
    ["responseHeaders"]
  );

  browser.webRequest.onCompleted.addListener(
    onCompletedListener,
    { urls: ["<all_urls>"] }
  );

  browser.webRequest.onErrorOccurred.addListener(
    onErrorOccurredListener,
    { urls: ["<all_urls>"] }
  );

  console.log('✅ WebRequest listeners registered');
}

function stopWebRequestMonitoring() {
  if (activeMonitors.size > 0) {
    // Still have active monitors
    return;
  }

  console.log('🛑 Stopping WebRequest monitoring - removing listeners');

  if (onBeforeRequestListener) {
    browser.webRequest.onBeforeRequest.removeListener(onBeforeRequestListener);
    onBeforeRequestListener = null;
  }

  if (onBeforeSendHeadersListener) {
    browser.webRequest.onBeforeSendHeaders.removeListener(onBeforeSendHeadersListener);
    onBeforeSendHeadersListener = null;
  }

  if (onHeadersReceivedListener) {
    browser.webRequest.onHeadersReceived.removeListener(onHeadersReceivedListener);
    onHeadersReceivedListener = null;
  }

  if (onCompletedListener) {
    browser.webRequest.onCompleted.removeListener(onCompletedListener);
    onCompletedListener = null;
  }

  if (onErrorOccurredListener) {
    browser.webRequest.onErrorOccurred.removeListener(onErrorOccurredListener);
    onErrorOccurredListener = null;
  }

  console.log('✅ WebRequest listeners removed');
}

function handleWebRequestEvent(eventType, details) {
  // Check if any monitor should capture this request
  for (const [monitorId, monitor] of activeMonitors) {
    if (shouldCaptureRequest(monitor, details)) {
      captureRequestEvent(monitorId, eventType, details);
    }
  }
}

function shouldCaptureRequest(monitor, details) {
  // Check tab filter
  if (monitor.tab_id && details.tabId !== monitor.tab_id) {
    return false;
  }

  // Check URL patterns
  if (monitor.url_patterns && monitor.url_patterns.length > 0) {
    const url = details.url;
    return monitor.url_patterns.some(pattern => {
      if (pattern === '*') return true;

      // Convert glob pattern to regex
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

  return true; // No filters, capture all
}

function captureRequestEvent(monitorId, eventType, details) {
  const requestId = details.requestId;
  const timestamp = new Date().toISOString();

  // Get or create request record. ONE record per requestId, but SEVERAL monitors can
  // match the same request (overlapping url_patterns); so membership is a list, not a
  // single owner. Attributing the request to the first matcher alone would leave every
  // other monitor's list empty and make get_content reject a request that monitor
  // genuinely captured.
  let request = requestDetails.get(requestId);
  if (request) {
    if (request.monitor_ids.indexOf(monitorId) === -1) request.monitor_ids.push(monitorId);
  }
  if (!request) {
    request = {
      request_id: requestId,
      monitor_id: monitorId,       // first matcher; kept for the response's headline
      monitor_ids: [monitorId],    // every monitor that matched it
      listed_in: [],               // monitors whose captured list already holds it
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

  // Add event data
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

    // The request's outgoing headers as the page composed them (Cookie included).
    // The ONLY source of request_headers: get_content reported {} for the whole life
    // of the feature because the listener meant to carry them was declared and never
    // registered. See setupWebRequestListeners for why this rides onBeforeSendHeaders
    // rather than its own onSendHeaders listener.
    case 'onBeforeSendHeaders':
      // COPY, don't alias: this same listener goes on to push/rewrite
      // Accept-Encoding: identity on `details.requestHeaders` in place, so keeping a
      // reference would report OUR override back to the agent as a header the page
      // sent. (The test caught this: expected {Content-Type, X-Trace}, got a third
      // entry `Accept-Encoding: identity` that no page ever wrote.)
      event.requestHeaders = (details.requestHeaders || [])
        .map(h => ({ name: h.name, value: h.value }));
      break;

    case 'onHeadersReceived':
      event.responseHeaders = details.responseHeaders;
      event.statusCode = details.statusCode;
      event.statusLine = details.statusLine;
      request.status_code = details.statusCode;
      request.response_headers = details.responseHeaders;

      // Extract content length and type from headers
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
      // commitParkedBody may judge a finished body by these headers, unless they belong
      // to a redirect hop.
      request.headers_seen = !REDIRECT_STATUS_CODES.has(details.statusCode);
      if (typeof details.fromCache === 'boolean') request.from_cache = details.fromCache;
      break;

    case 'onCompleted':
      event.statusCode = details.statusCode;
      request.status_code = details.statusCode;
      request.completed = true;
      if (typeof details.fromCache === 'boolean') request.from_cache = details.fromCache;
      request.duration_ms = details.timeStamp - (request.events[0]?.timeStamp || details.timeStamp);
      break;

    case 'onErrorOccurred':
      event.error = details.error;
      request.error = details.error;
      request.completed = true;
      break;
  }

  request.events.push(event);

  // If request is complete, add it to THIS monitor's captured list (once per monitor;
  // a request matched by two monitors must appear in both lists).
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
    // FIFO-bound the session's list, and drop the detail/body of anything that falls
    // off it; list_captured would never surface those again anyway.
    if (captured.length > CAPTURED_PER_MONITOR_MAX) {
      for (const dropped of captured.splice(0, captured.length - CAPTURED_PER_MONITOR_MAX)) {
        forgetRequestForMonitor(dropped.request_id, monitorId);
      }
    }
    capturedRequests.set(monitorId, captured);
    request.listed_in.push(monitorId);
  }
}

// Credentials must never land in an agent's context just because it asked for a
// request's headers. The monitor's `sensitive_headers` option ADDS to this floor and
// can never subtract from it; the server's start-time default is a strict subset of
// it, so the floor is what every default-options agent actually gets.
//
// The floor covers BOTH directions, and the two have different credential
// vocabularies; sizing it for one is how you leak the other:
//   response -> set-cookie is how a session is handed out.
//   request  -> authorization/cookie, but ALSO the bearer-token-by-another-name
//               headers that SPAs actually authenticate with. Those were exposed the
//               moment request_headers became real (they were always {} before), so
//               a floor that stops at authorization/cookie leaks a live API key
//               verbatim out of the user's logged-in browser on the very first
//               `requests(action="get")`.
// Anything credential-shaped belongs here. A false positive costs one <redacted> in a
// debug session; a false negative costs the user's key.
const ALWAYS_REDACTED_HEADERS = [
  // both directions
  'authorization', 'proxy-authorization', 'cookie', 'set-cookie',
  // request-side: the common API-key / token / session header spellings
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

// get_content's note for a request it holds no body for, built from the outcome its
// capture recorded. Every variant starts "No response body was captured" (tests match it).
function noBodyNote(detail) {
  const outcome = detail.body_outcome || '';
  const sep = outcome.indexOf(':');
  const kind = sep === -1 ? outcome : outcome.slice(0, sep);
  const arg = sep === -1 ? '' : outcome.slice(sep + 1);
  let why;
  if (kind === 'type_excluded') {
    why = (arg === 'none'
      ? 'the response had no Content-Type, and the monitor\'s content_types_to_capture admits only the types it lists'
      : `its Content-Type (${arg}) is not in the monitor's content_types_to_capture`) +
      '; a monitor whose list admits it would capture it';
  } else if (kind === 'no_bytes') {
    why = 'the response body was empty';
  } else if (kind === 'filter_error') {
    why = `the stream filter failed (${arg})`;
  } else if (kind === 'attach_failed') {
    why = `Firefox would not attach a stream filter to it (${arg})`;
  } else if (kind === 'awaiting_headers') {
    why = 'its response headers have not reached the extension, so its Content-Type could not be checked';
  } else if (kind === 'request_failed') {
    why = `the request failed (${arg}) before its response headers reached the extension`;
  } else {
    why = 'no stream filter was attached to it (the monitor that matched it was not capturing bodies, or the request started before the monitor did)';
  }
  const cache = detail.from_cache ? ' It came from the HTTP cache, which does not stop capture.' : '';
  return `No response body was captured for this request: ${why}.${cache} ` +
    'size_bytes is the Content-Length the server reported.';
}

function pickHeader(headers, name) {
  const key = Object.keys(headers || {}).find(k => k.toLowerCase() === name);
  return key ? headers[key] : null;
}

// webRequest hands the request body over as `formData` (already decoded) OR as
// `raw` ArrayBuffers; JSON.stringify()ing the latter yields "[{}]" and LOSES the
// body, so decode the bytes: UTF-8 when they decode cleanly, base64 (gated by
// include_binary, exactly like the response path) when they don't.
function describeRequestBody(requestBody, contentType, includeBinary) {
  const none = (note) => ({ included: false, content: null, content_type: contentType || null,
                            encoding: null, size_bytes: 0, truncated: false, note: note });
  if (!requestBody) return none('No request body was sent (or Firefox exposed none for this request type)');
  if (requestBody.formData) {
    const json = JSON.stringify(requestBody.formData);
    return {
      included: true, content: json, content_type: contentType || null, encoding: 'utf-8',
      size_bytes: json.length, truncated: false,
      note: 'Form fields as JSON; webRequest decodes them; the raw encoded body is not exposed'
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
               note: 'Binary request body; re-request this request_id with include_binary=true to receive it' };
    }
    return { included: true, content: base64FromBytes(bytes), content_type: contentType || null,
             encoding: 'base64', size_bytes: bytes.length, truncated: false };
  }
  if (requestBody.error) return none(`Firefox could not read the request body: ${requestBody.error}`);
  // e.g. a file/blob upload: webRequest reports the body exists but hands over no bytes.
  return none('A request body was sent but Firefox exposed no readable bytes for it (e.g. a file/blob upload)');
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
      // Retroactive inspection from the always-on per-tab rolling log: no
      // pre-armed monitor needed. Summaries only (no bodies); body capture
      // still requires a start_monitoring session.
      case 'requests.recent': {
        const recentTabId = await resolveTabId(data);
        let log = (TAB_REQUESTS.get(recentTabId) || []).slice();
        if (!data.includeStatic) {
          // keep failed static resources; a 404'd script/stylesheet is signal
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

        // Body capture arms itself: setupWebRequestListeners' onBeforeRequest asks
        // monitorWantingBody() per request, so registering the monitor below is all
        // it takes. Nothing to enable here.

        // Start actual monitoring
        startWebRequestMonitoring(monitor);

        // Store monitor
        activeMonitors.set(monitor_id, monitor);
        capturedRequests.set(monitor_id, []);
        ensureMonitorSweeper();

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

        // Calculate statistics
        const captured = capturedRequests.get(data.monitor_id) || [];
        const startTime = new Date(monitorToStop.started_at).getTime();
        const stopTime = Date.now();
        const durationSeconds = (stopTime - startTime) / 1000;

        // Retires the monitor and removes the listeners if it was the last active
        // one; its captures stay readable (list/get) for the retention window.
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
        // A monitor that expired (TTL), was retired when the server dropped, or whose
        // retention window has passed must SAY so; answering {total_requests: 0} for
        // any of those reads as "the traffic never happened".
        const listState = monitorState(data.monitor_id);
        if (listState === 'unknown') {
          sendError(id, 'MONITOR_NOT_FOUND',
            `Monitor ${data.monitor_id} not found; it never existed (or the extension was reloaded and lost it)`);
          break;
        }
        if (listState === 'expired') {
          const ev = evictedMonitors.get(data.monitor_id);
          sendError(id, 'MONITOR_EXPIRED',
            `Monitor ${data.monitor_id} ended (${ev.reason}) and its captures have since been evicted; start a new monitor and reproduce the traffic`);
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
        // Same lifecycle honesty as list_captured (the server always sends monitor_id;
        // the protocol-level tests don't, and then there is nothing to validate against).
        if (data.monitor_id) {
          const getState = monitorState(data.monitor_id);
          if (getState === 'unknown') {
            sendError(id, 'MONITOR_NOT_FOUND',
              `Monitor ${data.monitor_id} not found; it never existed (or the extension was reloaded and lost it)`);
            break;
          }
          if (getState === 'expired') {
            const ev = evictedMonitors.get(data.monitor_id);
            sendError(id, 'MONITOR_EXPIRED',
              `Monitor ${data.monitor_id} ended (${ev.reason}) and its captures have since been evicted; start a new monitor and reproduce the traffic`);
            break;
          }
        }

        const requestDetail = requestDetails.get(data.request_id);
        if (!requestDetail) {
          sendError(id, 'REQUEST_NOT_FOUND',
            `Request ${data.request_id} not found; it was never captured, or it aged out of the bounded capture buffer (the ${REQUEST_DETAILS_MAX} most recent requests)`);
          break;
        }
        // The request is keyed globally, so without this check monitor B's id could
        // fetch a request only monitor A ever captured. Overlapping patterns are
        // legitimate, though; a request captured by BOTH is fetchable through either.
        if (!requestBelongsToMonitor(requestDetail, data.monitor_id)) {
          sendError(id, 'REQUEST_NOT_FOUND',
            `Request ${data.request_id} was not captured by monitor ${data.monitor_id} ` +
            `(it belongs to ${(requestDetail.monitor_ids || [requestDetail.monitor_id]).join(', ')})`);
          break;
        }

        // Extract headers from events
        let requestHeaders = null;
        let responseHeaders = {};
        let requestBody = null;

        for (const event of requestDetail.events) {
          if (event.type === 'onBeforeRequest' && event.requestBody) {
            requestBody = event.requestBody;
          }
          // FIRST hop wins, not last. A redirect chain keeps ONE requestId (so one
          // record) but fires onBeforeSendHeaders per hop, while `url` is stamped once
          // at record creation from the FIRST hop. Taking the last hop's headers
          // therefore reports the credentials sent to the redirect TARGET under the
          // ORIGINAL url; telling the agent that a request to shop.example carried
          // accounts.example's Authorization. Headers and url must describe the same
          // hop; url is the first, so these are too.
          if (event.type === 'onBeforeSendHeaders' && event.requestHeaders
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
        // Redaction follows the monitor the AGENT asked through (it is that monitor's
        // sensitive_headers policy), falling back to the one that captured it.
        const monitorOfRequest = data.monitor_id || requestDetail.monitor_id;

        // `{}` cannot say WHY it is empty, and "the request sent no headers" is never
        // true; that ambiguity is the whole reason the always-{} bug went unnoticed
        // for the life of the feature. If no hop ever carried them (Firefox does not
        // fire onBeforeSendHeaders for a response served straight from cache), say so
        // rather than implying an empty header set.
        const headersNote = requestHeaders === null
          ? 'Request headers were not captured for this request (no onBeforeSendHeaders '
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
            // Direct lookup ONLY. The StreamFilter keys every record by the webRequest
            // requestId, so a miss means this request has no captured body; the old
            // url+method+status correlation fallback answered with ANOTHER request's
            // body (for a polled endpoint, the oldest one) while claiming included:true.
            const rec = capturedResponseBodies.get(data.request_id);
            if (!rec) {
              return {
                included: false,
                content: null,
                content_type: responseContentType,
                encoding: null,
                // What the server said the body weighed, even though we don't hold it.
                size_bytes: requestDetail.response_content_length || 0,
                truncated: false,
                note: requestDetail.body_evicted
                  ? 'The captured body was EVICTED to keep the capture buffers bounded (newer/larger bodies since). The request itself is still listed; re-run it under a fresh monitor to capture the body again.'
                  : noBodyNote(requestDetail)
              };
            }

            const bodyEncoding = rec.encoding || 'utf-8';
            const returned = typeof rec.content_length === 'number'
              ? rec.content_length
              : (rec.response_body || '').length;
            const full = typeof rec.full_size_bytes === 'number' ? rec.full_size_bytes : undefined;
            // size_bytes is what you GET; full_size_bytes is what the body really
            // weighed (Content-Length) when the capture stopped short of it.
            const sizeNote = rec.truncated
              ? ` Truncated at max_body_size: ${returned} bytes returned${full ? ` of ${full}` : ' (the full size is unknown; the server sent no Content-Length)'}.`
              : '';
            // The stream filter is the ONLY writer of capturedResponseBodies; the
            // content-script fetch/XHR wrapper this used to also describe never
            // installed and is deleted.
            const sourceNote = 'Response body captured at the network layer (stream filter).';

            // A base64 blob is worthless in an agent's context window and costs it
            // dearly; withhold it (with the metadata that lets the caller decide)
            // unless the caller explicitly asked for binary.
            if (bodyEncoding === 'base64' && !data.include_binary) {
              return Object.assign({
                included: false,
                content: null,
                content_type: rec.content_type || null,
                encoding: 'base64',
                size_bytes: returned,
                truncated: !!rec.truncated,
                note: 'Binary response body captured (base64); re-request this request_id with include_binary=true to receive it.' + sizeNote
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
// The eight test.* actions read popup/options state and open tabs on request. They ship in
// the release package because the Firefox harness installs the very .xpi users get, so the
// gate is a runtime one: the harness seeds testPort AND testHostname into storage.sync
// before Firefox first starts (firefox_test_utils._test_storage_config), and a normal
// profile never has either. Only the extension itself writes storage.sync (options.js
// preserves these two keys and never sets them); no WebSocket action does, so a peer
// cannot turn the helpers on. Same predicate as loadConfig()'s valid-test-config check,
// plus the hostname, so a half-seeded profile does not open the gate. The Chromium twin
// gates on the baked BUILD_CONFIG.testMode instead; both refuse identically.
async function testHelpersEnabled() {
  try {
    const { testPort, testHostname } =
      await browser.storage.sync.get({ testPort: null, testHostname: null });
    return Number.isInteger(testPort) && testPort > 0 && testPort !== 8772 &&
      typeof testHostname === 'string' && testHostname !== '';
  } catch (e) {
    return false; // cannot prove it is a test profile: refuse
  }
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
    
    // Calculate effective values (same logic as popup.js)
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
    
    // Calculate display values (same logic as options.js)
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
    
    // Get current storage values
    const storageConfig = await browser.storage.sync.get();
    
    // Get popup state
    const popupState = await getPopupStateForValidation(storageConfig);
    
    // Get options state  
    const optionsState = await getOptionsStateForValidation(storageConfig);
    
    // Check storage matches expected values
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
    
    // Validate popup displays correct effective values
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
    // This simulates what happens when popup/options pages refresh
    // In practice, this would trigger any cached state to be cleared
    // and force re-reading from storage
    
    // For now, we just confirm the action was received
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

// Helper function for validation
async function getPopupStateForValidation(storageConfig) {
  const effectiveHostname = storageConfig.testHostname || storageConfig.hostname || 'localhost';
  const effectivePort = storageConfig.testPort || storageConfig.port || 8772;
  
  return {
    effectiveHostname,
    effectivePort,
    hasTestOverrides: storageConfig.testPort !== null || storageConfig.testHostname !== null
  };
}

// Helper function for validation
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
    const waitTime = data.waitTime || 8000; // Increased default wait time

    if (!url) {
      sendError(id, 'INVALID_PARAMETERS', 'URL is required for test.visit_url');
      return;
    }

    console.log(`[BrowserPlug] Starting visit to URL: ${url}`);

    // Create a new tab with the URL
    const tab = await browser.tabs.create({
      url: url,
      active: false // Don't make it active to avoid disrupting tests
    });

    console.log(`[BrowserPlug] Created tab ${tab.id} for URL: ${url}`);

    // Wait for the tab to actually complete loading
    let tabLoaded = false;
    let loadStartTime = Date.now();
    const maxWaitTime = Math.max(waitTime, 10000); // At least 10 seconds

    // Set up a listener for tab updates
    const tabUpdateListener = (tabId, changeInfo, updatedTab) => {
      if (tabId === tab.id && changeInfo.status === 'complete') {
        console.log(`[BrowserPlug] Tab ${tab.id} finished loading`);
        tabLoaded = true;
      }
    };

    browser.tabs.onUpdated.addListener(tabUpdateListener);

    try {
      // Wait for either tab to load or timeout
      while (!tabLoaded && (Date.now() - loadStartTime) < maxWaitTime) {
        await new Promise(resolve => setTimeout(resolve, 500));
      }

      if (tabLoaded) {
        console.log(`[BrowserPlug] Tab loaded in ${Date.now() - loadStartTime}ms`);
        // Give additional time for history to be recorded
        console.log(`[BrowserPlug] Waiting additional time for history recording...`);
        await new Promise(resolve => setTimeout(resolve, 3000));
      } else {
        console.log(`[BrowserPlug] Tab did not complete loading within ${maxWaitTime}ms, proceeding anyway`);
        // Still wait the original wait time as fallback
        await new Promise(resolve => setTimeout(resolve, waitTime));
      }

    } finally {
      // Clean up the listener
      browser.tabs.onUpdated.removeListener(tabUpdateListener);
    }

    console.log(`[BrowserPlug] Closing tab ${tab.id}`);

    // Close the tab
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
    const waitTime = data.waitTime || 8000; // Increased time to wait at each URL
    const delayBetween = data.delayBetween || 3000; // Increased delay between visits

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
        // Use the same improved logic as single URL visit
        const tab = await browser.tabs.create({
          url: url,
          active: false
        });

        let tabLoaded = false;
        let loadStartTime = Date.now();
        const maxWaitTime = Math.max(waitTime, 10000);

        // Set up tab update listener
        const tabUpdateListener = (tabId, changeInfo, updatedTab) => {
          if (tabId === tab.id && changeInfo.status === 'complete') {
            console.log(`[BrowserPlug] Tab ${tab.id} finished loading URL ${i + 1}`);
            tabLoaded = true;
          }
        };

        browser.tabs.onUpdated.addListener(tabUpdateListener);

        try {
          // Wait for tab to load or timeout
          while (!tabLoaded && (Date.now() - loadStartTime) < maxWaitTime) {
            await new Promise(resolve => setTimeout(resolve, 500));
          }

          if (tabLoaded) {
            console.log(`[BrowserPlug] URL ${i + 1} loaded in ${Date.now() - loadStartTime}ms`);
            // Extra wait for history recording
            await new Promise(resolve => setTimeout(resolve, 2000));
          } else {
            console.log(`[BrowserPlug] URL ${i + 1} did not complete loading within ${maxWaitTime}ms`);
            await new Promise(resolve => setTimeout(resolve, waitTime));
          }

        } finally {
          browser.tabs.onUpdated.removeListener(tabUpdateListener);
        }

        // Close the tab
        await browser.tabs.remove(tab.id);

        results.push({
          url: url,
          success: true,
          tabId: tab.id,
          visitTime: new Date().toISOString(),
          loadTime: Date.now() - loadStartTime,
          tabLoaded: tabLoaded
        });
        
        // Small delay between visits
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
    const count = data.count || 3; // Default to 3 test tabs
    const baseUrls = data.urls || [
      'https://example.com',
      'https://httpbin.org/html',
      'https://httpbin.org/json'
    ];
    const closeExisting = data.closeExisting || false;
    
    // Close existing tabs if requested (except pinned tabs)
    if (closeExisting) {
      const existingTabs = await browser.tabs.query({});
      const tabsToClose = existingTabs.filter(tab => !tab.pinned && tab.url !== 'about:blank');
      
      if (tabsToClose.length > 0) {
        await browser.tabs.remove(tabsToClose.map(tab => tab.id));
      }
    }
    
    const createdTabs = [];
    
    // Create the specified number of test tabs
    for (let i = 0; i < count; i++) {
      const url = baseUrls[i % baseUrls.length];
      const testUrl = `${url}?test=tab${i + 1}&timestamp=${Date.now()}`;
      
      try {
        const tab = await browser.tabs.create({
          url: testUrl,
          active: i === 0 // Make first tab active
        });
        
        createdTabs.push({
          id: tab.id,
          url: testUrl,
          title: `Test Tab ${i + 1}`,
          active: tab.active,
          index: tab.index
        });
        
        // Small delay between tab creation
        if (i < count - 1) {
          await new Promise(resolve => setTimeout(resolve, 500));
        }
      } catch (error) {
        console.error(`Failed to create test tab ${i + 1}:`, error);
      }
    }
    
    // Wait a moment for tabs to load
    await new Promise(resolve => setTimeout(resolve, 1000));
    
    // Get final tab count
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


// ✅ INITIALIZATION: Load config and connect automatically after loading completes
// This ensures storage configuration is fully loaded before connection attempts
console.log('🚀 Extension starting - loading configuration...');

// Initialize extension: load config then connect
async function initializeExtension() {
  try {
    console.log('🚀 Initializing extension...');
    updateActionIndicator(); // show an initial toolbar state immediately
    console.log('📍 BEFORE loadConfig() - CONFIG:', JSON.stringify(CONFIG, null, 2));

    await loadConfig();

    console.log('✅ Configuration loaded successfully');
    console.log('📍 AFTER loadConfig() - CONFIG:', JSON.stringify(CONFIG, null, 2));

    // Connect after config load
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

// Start initialization
initializeExtension();

// Self-heal watchdog: even if the retry loop ever stops, or the background page was
// suspended/revived, periodically ensure we're connected and re-initiate if not. This
// means the extension reconnects on its own after a server restart; no manual Reconnect.
try {
  browser.alarms.create('browserplug-reconnect-watchdog', { periodInMinutes: 1 });
  browser.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name !== 'browserplug-reconnect-watchdog') return;
    if (stoppedByUser) return; // user explicitly stopped; don't auto-revive
    if (!websocket || websocket.readyState !== WebSocket.OPEN) {
      console.log('⏰ Watchdog: not connected; re-initiating connection');
      journalNote('reconnect', { reason: 'watchdog' });
      retryAttempts = 0;
      connectToMCPServer();
    } else {
      // Connected: refresh diagnostic state (e.g. file-access opt-in) on /health
      sendExtensionHello();
      // Safety net: if the health-probe interval was ever lost (e.g. background
      // page revived), make sure it's running again.
      if (!healthProbeTimer) startHealthProbe();
    }
  });
} catch (e) {
  console.error('Could not set up reconnect watchdog:', e);
}

// --- UI surface routing ----------------------------------------------------
// The toolbar button can open the BrowserPlug interface (popup/popup.html) in one of
// four surfaces, chosen in the options page ("uiSurface" in storage.sync):
//   popup    - the default browser_action panel (closes on blur) (default)
//   detached - a standalone popup window that stays open on blur
//   sidebar  - docked into the Firefox window via sidebar_action
//   tab      - the interface opened as an ordinary tab
// For every surface EXCEPT 'popup' we clear the browser_action popup so that
// browserAction.onClicked fires and we can route the click ourselves.
let uiSurface = 'popup';
let detachedWindowId = null;

// Three of the four surfaces cannot exist on a phone: there is no second window to detach
// into, no sidebar, and the overlay IS the browser_action popup. uiSurface is a SYNCED
// key, so a phone inherits whatever the desktop picked, and a stored 'tab' cleared the
// popup and left Android with no interface at all. Decide it locally rather than writing
// 'popup' back to storage: the phone must not overwrite the desktop's choice to fix its
// own platform.
let isAndroidUi = false;
function effectiveUiSurface() {
  return isAndroidUi ? 'popup' : uiSurface;
}

function applyUiSurface() {
  const ba = browser.browserAction;
  if (!ba || !ba.setPopup) return;
  try {
    // '' → onClicked fires; a URL → the browser opens that panel itself.
    ba.setPopup({ popup: effectiveUiSurface() === 'popup' ? 'popup/popup.html' : '' });
  } catch (e) {
    console.error('applyUiSurface failed:', e);
  }
}

async function openDetachedInterface() {
  const url = browser.runtime.getURL('popup/popup.html');
  // Reuse the existing detached window if it's still open.
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

if (browser.browserAction && browser.browserAction.onClicked) {
  browser.browserAction.onClicked.addListener(() => {
    // Only fires when the popup is cleared (the effective surface is not 'popup'), so on
    // Android this never runs at all; it reads the effective value anyway so the routing
    // and the popup registration can never disagree about which surface is in force.
    const surface = effectiveUiSurface();
    if (surface === 'sidebar') {
      // sidebarAction.open() must run synchronously inside the click gesture.
      try {
        browser.sidebarAction.open();
      } catch (e) {
        console.error('sidebarAction.open failed:', e);
      }
      return;
    }
    if (surface === 'tab') { openInterfaceTab(); return; }
    openDetachedInterface(); // 'detached'
  });
}

if (browser.windows && browser.windows.onRemoved) {
  browser.windows.onRemoved.addListener((id) => {
    if (id === detachedWindowId) detachedWindowId = null;
  });
}

// React live when the surface is changed in the options page.
browser.storage.onChanged.addListener((changes, area) => {
  if (area === 'sync' && changes.uiSurface) {
    uiSurface = changes.uiSurface.newValue || 'popup';
    applyUiSurface();
  }
});

// Load the persisted surface and apply it (independent of the MCP connection, so
// the toolbar button behaves correctly even before/without a server).
(async () => {
  try {
    if (browser.runtime.getPlatformInfo) {
      const info = await browser.runtime.getPlatformInfo();
      isAndroidUi = !!(info && info.os === 'android');
    }
  } catch (e) {
    // Desktop is the safe default: a phone briefly offering four surfaces is untidy, a
    // desktop pinned to one would have lost three working ones.
    console.error('getPlatformInfo failed; assuming desktop:', e);
  }
  try {
    const { uiSurface: s } = await browser.storage.sync.get({ uiSurface: 'popup' });
    uiSurface = s || 'popup';
  } catch (e) {
    console.error('Loading uiSurface failed:', e);
  }
  applyUiSurface();
})();

// --- Toolbar-button context menu -------------------------------------------
// Add an "Options" item to the right-click menu of our toolbar button so the
// options page is reachable without going through about:addons. It opens the
// SAME page as Firefox's built-in about:addons "Options" entry (options_ui).
// The label is localized via _locales, mirroring Firefox's own wording
// ("Options" / "Opcje"; the Windows string; other platforms say "Preferences").
if (browser.menus && browser.menus.create) {
  const OPTIONS_MENU_ID = 'browserplug-open-options';
  try {
    browser.menus.create({
      id: OPTIONS_MENU_ID,
      title: browser.i18n.getMessage('toolbarMenuOptions') || 'Options',
      contexts: ['browser_action'],
    });
  } catch (e) {
    console.error('Creating toolbar context menu failed:', e);
  }
  browser.menus.onClicked.addListener((info) => {
    if (info.menuItemId === OPTIONS_MENU_ID) {
      browser.runtime.openOptionsPage().catch((e) =>
        console.error('openOptionsPage failed:', e));
    }
  });
}
