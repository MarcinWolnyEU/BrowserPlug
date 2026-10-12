// Unit tests for extension-gecko/popup/popup.js against the REAL popup.html (jsdom, no
// Firefox). The bug these guard (GATE 1.7): popup.js reads #serverInfo at load and
// loadConfigToUI() wrote to it unconditionally — but the element did not exist in
// popup.html, so every popup open threw a TypeError INSIDE updateDisplay()'s try.
// The catch then repainted the popup with a hardcoded phase:'disconnected', silently
// overriding the correct render that had just happened one line earlier.
//
// The MECHANISM (not just that one throw site) is what's under test here:
//   * updateDisplay's catch must never fabricate a phase — a failed CONFIG read must
//     not downgrade the CONNECTION display (that is what makes such a bug invisible).
//   * the footer must show the URL the background is really dialing (its live CONFIG),
//     not an independently-derived guess from storage.
//   * the test-override indicator must name only what is actually overridden.
// Note the exception is MASKED (swallowed by the catch, only console.error'd), so a
// window 'error' listener can never see it — these tests spy on console.error instead.
//
// Two things here are deliberately NOT hand-copied, because a copy asserts itself:
//   * the toolbar button state comes from the REAL updateActionIndicator() sliced out
//     of background.js (loadUpdateActionIndicator below) — the popup's status line is
//     supposed to BE that button, so a mirror of it could prove nothing;
//   * every roster fixture is validated against tests/fixtures/connections_info_contract.json,
//     the same file tests/unit/test_connections_contract.py holds the real Python
//     get_connections_info() to. A server-side key rename used to leave these tests green.
// Run: node --test tests/js/popup.test.mjs   (or: npm run test:js)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import { domContentLoaded } from './dom-ready.mjs';
import { assertMatchesContract } from './contract.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const POPUP_HTML = readFileSync(path.join(__dirname, '../../extension-gecko/popup/popup.html'), 'utf8');
const POPUP_JS = readFileSync(path.join(__dirname, '../../extension-gecko/popup/popup.js'), 'utf8');
const BG_SRC = readFileSync(path.join(__dirname, '../../extension-gecko/background.js'), 'utf8');
const DEEPSEEK_VIEW = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/deepseek-view.js'), 'utf8');
const POPUP_TABS = readFileSync(path.join(__dirname, '../../extension-gecko/popup/popup-tabs.js'), 'utf8');
const USAGE_PANE = readFileSync(path.join(__dirname, '../../extension-gecko/popup/usage-pane.js'), 'utf8');
const COPILOT_VIEW = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/copilot-view.js'), 'utf8');

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

// The toolbar button is the single source of truth for the status line
// (renderConnection mirrors it, so it can never paint a false "Disconnected"). So what
// this mock serves has to be what background.js really writes to the button — a
// hand-copied phase→text/colour table would just be asserting itself, and would go on
// agreeing with the popup long after updateActionIndicator() had moved on.
//
// background.js is a top-level extension script (no exports), so slice the real function
// out on stable anchors and instantiate it with its globals as parameters — the idiom
// neterror.test.mjs / connection.test.mjs / observability.test.mjs already use.
function loadUpdateActionIndicator() {
  const start = BG_SRC.indexOf('function updateActionIndicator()');
  const end = BG_SRC.indexOf('function setConnectionPhase(', start);
  assert.ok(start >= 0 && end > start,
    'updateActionIndicator() not found in background.js between its anchors '
    + '("function updateActionIndicator()" .. "function setConnectionPhase(") — the '
    + 'anchors moved; re-point them at the real function rather than deleting this test');
  // Its globals, in order: the WebExtension namespace, the live config, and the four
  // pieces of connection state the button is computed from.
  // eslint-disable-next-line no-new-func
  const factory = new Function('browser', 'CONFIG', 'connectionPhase', 'serverHealthy',
    'retryAttempts', 'nextRetryAt',
    `${BG_SRC.slice(start, end)}\nreturn updateActionIndicator();`);
  return (...globals) => factory(...globals);
}

const realUpdateActionIndicator = loadUpdateActionIndicator();

// browser.browserAction.getBadgeBackgroundColor() answers with an [r,g,b,a] ColorArray
// no matter what setBadgeBackgroundColor() was handed (background.js hands it hex). The
// popup's badgeColor() classifies "connected" off the g channel, so a raw hex string
// would read as black == disconnected. This models the BROWSER API's conversion — it is
// not product logic being re-implemented. The colour is invisible on the toolbar now
// (the badge has no text), which is exactly why the popup is the only thing that would
// notice it going wrong.
function hexToColorArray(hex) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
  assert.ok(m, `background.js wrote badge colour "${hex}", which is not the #rrggbb this `
    + 'mock knows how to turn into the ColorArray Firefox returns');
  return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16), 255];
}

// Run the REAL updateActionIndicator() over a recording browserAction, then serve the
// popup exactly what it wrote. `status` is the background's connection state as the popup
// hears it; `config` is the background's LIVE CONFIG (the thing the ws:// URL is built from).
function makeBadge(status, config) {
  const wrote = {};
  const recordingAction = {
    setBadgeText: ({ text }) => { wrote.text = text; },
    setBadgeBackgroundColor: ({ color }) => { wrote.color = color; },
    setTitle: ({ title }) => { wrote.title = title; },
  };
  realUpdateActionIndicator(
    { browserAction: recordingAction },
    // maxRetries -1 == background.js's default (infinite): no "/N" in the tooltip.
    { hostname: config.hostname, port: config.port, maxRetries: -1 },
    status.phase || (status.connected ? 'connected' : 'disconnected'),
    // The health probe's verdict. Absent in a fixture == the settled, healthy case;
    // the tests that care about the other two branches say so explicitly.
    status.serverHealthy === undefined ? true : status.serverHealthy,
    status.retryAttempts || 0,
    status.nextRetryAt === undefined ? null : status.nextRetryAt,
  );
  assert.ok(wrote.color !== undefined && wrote.title !== undefined,
    'updateActionIndicator() wrote nothing — its browserAction guard rejected the mock');
  assert.equal(wrote.text, '',
    'updateActionIndicator() wrote badge TEXT; the toolbar icon carries the state now '
    + '(tests/js/toolbar-icon.test.mjs pins that, this just keeps the mock honest)');
  return {
    getBadgeBackgroundColor: () => Promise.resolve(hexToColorArray(wrote.color)),
    getTitle: () => Promise.resolve(wrote.title),
  };
}

// `failConfigRead` reproduces a throw INSIDE updateDisplay's try that has nothing to
// do with the connection (the storage read of the server config). Only that read is
// failed — the unrelated one in updateConnections keeps working.
// `connections` is the server's get_connections_info() payload (what the background
// relays for 'getServerConnections'); null keeps the old "no server" answer.
function makeBrowserMock(storage, connectionStatus,
                        { failConfigRead = false, connections = null,
                          platformOs = null, proxyGranted = false,
                          permissionRequests = [], localStore = {} } = {}) {
  const live = connectionStatus.config || {};
  const config = {
    hostname: live.hostname || storage.testHostname || storage.hostname || 'localhost',
    port: live.port || storage.testPort || storage.port || 8772,
  };
  return {
    browserAction: makeBadge(connectionStatus, config),
    runtime: {
      lastError: null,
      getManifest: () => ({ version: '0.0.0-test' }),
      openOptionsPage: () => {},
      getBrowserInfo: () => Promise.resolve({ version: '152.0' }),
      // Absent unless a test names a platform: a build without it must fall back to the
      // desktop layout, which is what the no-getPlatformInfo test pins.
      ...(platformOs ? { getPlatformInfo: () => Promise.resolve({ os: platformOs, arch: 'arm64' }) } : {}),
      // Every popup message is callback-style; answer the ones popup.js sends.
      sendMessage: (msg, cb) => {
        let reply = { success: false };
        if (msg.action === 'getConnectionStatus') reply = connectionStatus;
        if (msg.action === 'getServerConnections') {
          reply = connections
            ? { success: true, data: connections }
            : { success: false, error: 'not connected' };
        }
        if (typeof cb === 'function') cb(reply);
        return Promise.resolve(reply);
      },
    },
    storage: {
      sync: {
        get: (defaults) => {
          if (failConfigRead && Object.prototype.hasOwnProperty.call(defaults, 'hostname')) {
            return Promise.reject(new Error('storage.sync.get exploded'));
          }
          return Promise.resolve({ ...defaults, ...storage });
        },
        set: () => Promise.resolve(),
      },
      // Stateful, because the Chromium proxy switch is read and written here. A string key
      // asks for one value; the object form is the usual defaults-plus-stored read.
      local: {
        get: (d) => Promise.resolve(typeof d === 'string' ? { [d]: localStore[d] }
                                                          : { ...d, ...localStore }),
        set: (o) => { Object.assign(localStore, o); return Promise.resolve(); },
      },
    },
    extension: { isAllowedFileSchemeAccess: () => Promise.resolve(false) },
    permissions: {
      contains: () => Promise.resolve(proxyGranted),
      request: (what) => { permissionRequests.push(what); return Promise.resolve(true); },
    },
    tabs: { query: () => Promise.resolve([]), update: () => Promise.resolve() },
    menus: undefined,
  };
}

// Load the real popup.html, install the browser mock, run the real popup.js as
// global code, wait for jsdom's DOMContentLoaded (dom-ready.mjs), let the async init
// settle. `html` lets a test mutate the document first (to reproduce the
// missing-element regression).
//
// `consoleErrors` is the spy that can actually SEE the bug: the failure mode is a
// swallowed exception, so it surfaces only as a console.error from updateDisplay's
// catch — never as an uncaught window 'error' event.
async function initPopup({
  storage = {},
  connectionStatus = { connected: true, phase: 'connected', retryAttempts: 0 },
  html = POPUP_HTML,
  failConfigRead = false,
  connections = null,
  vendors = [],
  now = null,
  platformOs = null,
  proxyGranted = false,
  engine = 'gecko',
  localStore = {},
} = {}) {
  // Every roster fixture goes through the contract on its way in: these rows are frozen
  // copies of what get_connections_info() emits, and tests/unit/test_connections_contract.py
  // holds the REAL emitter to the same file. requireAll:false because several fixtures
  // below are DELIBERATELY partial (a stale / pre-upgrade server's payload) — but an
  // unknown key is always fatal, and that is what catches a server-side rename here.
  // The two retired flip-flop keys (DEAD_FLAGS) are stripped first: the server no longer
  // emits them, so the contract no longer knows them, but a stale server still might.
  if (connections) {
    const { multiple_extensions: _m, extension_warning: _w, ...current } = connections;
    assertMatchesContract(current, 'payload', { requireAll: false });
  }
  const dom = new JSDOM(html, { pretendToBeVisual: true, runScripts: 'outside-only' });
  const { window } = dom;
  const pageReady = domContentLoaded(window);
  const permissionRequests = [];
  const browserMock = makeBrowserMock(storage, connectionStatus,
    { failConfigRead, connections, platformOs, proxyGranted, permissionRequests, localStore });
  if (engine === 'chromium') {
    // Chrome, Edge and Brave define `chrome` and no `browser`; popup.js tells them apart by that.
    window.chrome = browserMock;
    delete window.browser;
  } else {
    window.browser = browserMock;
  }
  window.fetch = async () => ({ ok: true, json: async () => ({}), text: async () => '' });
  const consoleErrors = [];
  window.console = Object.assign(Object.create(window.console), {
    error: (...args) => consoleErrors.push(args.map(String).join(' ')),
  });
  // A view whose output depends on the wall clock (the peak indicators) needs a fixed
  // one, or the test asserts whatever phase the suite happened to run in. Installed on
  // the window before the views are evaluated, so their `new Date()` sees it.
  if (now !== null) {
    const Real = window.Date;
    const at = now instanceof Date ? now.getTime() : now;
    window.Date = class extends Real {
      constructor(...args) { super(...(args.length ? args : [at])); }
      static now() { return at; }
    };
  }
  // popup.js's <script> tags (vendor views) are not executed by runScripts:"outside-only";
  // the panels that use them degrade gracefully, which is all these tests need. A test
  // that is ABOUT one of those views names it in `vendors` and gets it evaluated first,
  // in the order popup.html loads them.
  for (const src of vendors) window.eval(src);
  window.eval(POPUP_JS);
  await pageReady;
  await flush();
  await flush();
  // A controllable clock, installed AFTER init so the page's own start-up timers already
  // ran on the real one. The press-and-hold threshold is 3s; seven tests of it would cost
  // 21 real seconds, and a test that sleeps is a test nobody runs.
  const timers = [];
  let fakeNow = 0;
  window.setTimeout = (fn, ms = 0, ...args) => {
    timers.push({ id: timers.length + 1, at: fakeNow + (ms || 0), fn, args, dead: false });
    return timers.length;
  };
  window.clearTimeout = (id) => {
    const t = timers[id - 1];
    if (t) t.dead = true;
  };
  window.__advance = async (ms) => {
    fakeNow += ms;
    for (const t of [...timers]) {
      if (!t.dead && t.at <= fakeNow) { t.dead = true; t.fn(...t.args); }
    }
    await flush();
  };
  return { window, document: window.document, consoleErrors, permissionRequests };
}

// Did anything get swallowed into updateDisplay's catch? (The catch is the only
// place popup.js logs this string.)
const swallowed = (consoleErrors) => consoleErrors.filter(m => m.includes('Error updating display'));

// --- the status line IS background.js's toolbar button ----------------------
// renderConnection deliberately re-uses the toolbar button rather than deriving text
// from a phase itself, so the popup and the toolbar can never disagree. These pin that
// the popup really carries updateActionIndicator()'s OWN output through — the tooltip
// and the green/not-green verdict — for phases the old hand-copied 4-row table could not
// even express.

// status.textContent == the tooltip minus its "BrowserPlug: " prefix; the class carries
// the tone the strip is painted in (ok / warn / bad), which is the badge colour.
const statusLine = (document) => document.getElementById('status').textContent;
const statusClass = (document) => document.getElementById('status').className;

test('the healthy connection reads "Connected" here, and the BUTTON keeps its sentence', async () => {
  // The one sanctioned divergence from "the status line IS the toolbar button". That
  // tooltip answers "what is this button for?" to somebody hovering the toolbar, which is
  // not a sentence to re-read inside the panel that already answers it. The substitution
  // is keyed on the green badge, so it cannot fire for a phase that merely calls itself
  // connected; every other phase still passes through verbatim (the next test).
  const { window, document } = await initPopup({
    storage: { hostname: 'localhost', port: 8772 },
    connectionStatus: { connected: true, phase: 'connected', retryAttempts: 0 },
  });
  try {
    assert.equal(statusLine(document), 'Connected');
    assert.equal(document.getElementById('status').title,
      'Browser is connected to server which then can be used by AI Agents through MCP',
      'the sentence the line drops is still one hover away, on the line itself');
    assert.equal(await window.browser.browserAction.getTitle({}),
      'BrowserPlug: Browser is connected to server which then can be used by AI Agents through MCP',
      'the TOOLBAR button is untouched: the popup reads that title, it never writes one');
    assert.equal(statusClass(document), 'status ok',
      'the green badge COLOUR is what makes the popup say connected');
    // The same verdict reaches the strip around the line.
    assert.equal(document.getElementById('statusStrip').className, 'pp-status ok');
    // 1.8.8: no dot beside the brand name. It repainted, in a third place, a verdict
    // the line and the strip around it were already showing.
    assert.equal(document.getElementById('liveDot'), null, 'the brand-row dot is gone');
    assert.doesNotMatch(POPUP_JS, /liveDot/, 'and nothing in the popup still paints it');
  } finally {
    window.close();
  }
});

test('a live connection the background reports as NOT encrypted says so', async () => {
  // Only an AUTH_OFF server (no HTTP app, so no token to fetch) still takes a plaintext
  // extension. The badge is green all the same; the line is where the difference shows.
  const { window, document } = await initPopup({
    storage: { hostname: 'localhost', port: 8772 },
    connectionStatus: { connected: true, phase: 'connected', retryAttempts: 0, encrypted: false },
  });
  try {
    assert.equal(statusLine(document), 'Connected, no encryption \u{1F513}');
    assert.equal(statusClass(document), 'status ok');
  } finally {
    window.close();
  }
});

test('an encrypted connection, or one whose state is not known yet, reads plain "Connected"', async () => {
  for (const encrypted of [true, null, undefined]) {
    const { window, document } = await initPopup({
      storage: { hostname: 'localhost', port: 8772 },
      connectionStatus: { connected: true, phase: 'connected', retryAttempts: 0, encrypted },
    });
    try {
      assert.equal(statusLine(document), 'Connected', `encrypted: ${encrypted}`);
    } finally {
      window.close();
    }
  }
});

test('a connected-but-UNHEALTHY server is not painted as connected', async () => {
  // updateActionIndicator's own branch: socket up, /health silent → amber, not green.
  // The old hand-mirror had no row for this at all, so the popup's handling of it was
  // untested — it would have claimed a healthy connection.
  const { window, document } = await initPopup({
    storage: { hostname: 'localhost', port: 8772 },
    connectionStatus: {
      connected: true, phase: 'connected', retryAttempts: 0, serverHealthy: false,
    },
  });
  try {
    assert.match(statusLine(document), /^Connected, but server not responding/);
    assert.equal(statusClass(document), 'status warn',
      'amber is not green — the popup must not report this as a healthy connection');
    assert.equal(document.getElementById('statusStrip').className, 'pp-status warn');
    // The line already SAYS this; a tooltip repeating the words under the cursor is
    // noise, so only the healthy connection (whose line is shortened) gets one.
    assert.equal(document.getElementById('status').title, '');
  } finally {
    window.close();
  }
});

test('every phase background.js can be in reaches the popup as its real tooltip', async () => {
  // One case per switch branch in updateActionIndicator(), asserted against the REAL
  // function's output. A branch added there without a popup thought lands here.
  const cases = [
    // 'connected' is deliberately absent: it is the one phase the line does NOT carry
    // verbatim, and the test above owns it.
    ['connecting', { connected: false, phase: 'connecting' }, /^Connecting to ws:\/\/localhost:8772…/, 'Stop'],
    ['waiting', { connected: false, phase: 'waiting', retryAttempts: 3 }, /^Reconnecting, attempt 3/, 'Stop'],
    ['stopped', { connected: false, phase: 'stopped' }, /^Stopped$/, 'Reconnect'],
    ['gaveup', { connected: false, phase: 'gaveup', retryAttempts: 9 }, /^Cannot connect after 9 attempts/, 'Reconnect'],
    ['disconnected', { connected: false, phase: 'disconnected' }, /^Disconnected$/, 'Reconnect'],
  ];
  for (const [name, status, descRx, link] of cases) {
    const { window, document } = await initPopup({
      storage: { hostname: 'localhost', port: 8772 },
      connectionStatus: { retryAttempts: 0, ...status },
    });
    try {
      const line = statusLine(document);
      assert.ok(line.length > 0, `${name}: the line is a status line (got: ${line})`);
      assert.match(line, descRx, `${name}: the tooltip text is the descriptor`);
      assert.doesNotMatch(line, /^[●…‖×!\d]/,
        `${name}: a badge glyph still sits in front of the words (got: ${line})`);
      assert.doesNotMatch(line, /BrowserPlug:/, `${name}: the redundant app-name prefix is stripped`);
      assert.equal(document.getElementById('connAction').textContent, link,
        `${name}: Stop is offered only while actively trying`);
    } finally {
      window.close();
    }
  }
});

test('the tooltip names the server the background is really dialing', async () => {
  // The ws:// URL is built inside updateActionIndicator from the LIVE CONFIG, so it
  // follows the background — not storage — everywhere it appears. Driven through
  // `connecting` because that is a phase whose tooltip still carries the URL: `waiting`
  // names the attempt only, so it could not tell a right URL from a wrong one.
  const { window, document } = await initPopup({
    storage: { hostname: 'localhost', port: 8772 },
    connectionStatus: {
      connected: false, phase: 'connecting', retryAttempts: 2,
      config: { hostname: '127.0.0.1', port: 9999 },
    },
  });
  try {
    assert.equal(statusLine(document), 'Connecting to ws://127.0.0.1:9999…');
    assert.equal(statusClass(document), 'status warn', 'still trying is amber, not red');
  } finally {
    window.close();
  }
});

test('popup.html actually HAS the #serverInfo element popup.js writes to', () => {
  const dom = new JSDOM(POPUP_HTML);
  assert.ok(dom.window.document.getElementById('serverInfo'),
    '#serverInfo must exist — popup.js reads it at load and writes the server footer to it');
  dom.window.close();
});

test('the popup footer shows the ws:// server it is configured to reach', async () => {
  const { window, document, consoleErrors } = await initPopup({ storage: { hostname: 'localhost', port: 8772 } });
  try {
    assert.equal(document.getElementById('serverInfo').textContent, 'Server: ws://localhost:8772');
    assert.deepEqual(consoleErrors, [], 'a clean open must not swallow any exception');
  } finally {
    window.close();
  }
});

// B: the background's live CONFIG is the thing actually dialed. It applies the test
// overrides at runtime and updateConfig() can change it WITHOUT persisting, so a
// footer derived independently from storage can name a server nobody is dialing.
test('the footer follows the background\'s LIVE config, not storage, when they disagree', async () => {
  const { window, document } = await initPopup({
    // storage still holds the old/default target …
    storage: { hostname: 'localhost', port: 8772 },
    // … while the background is really dialing this one (e.g. updateConfig without persist)
    connectionStatus: {
      connected: true, phase: 'connected', retryAttempts: 0,
      config: { hostname: '127.0.0.1', port: 9999 },
    },
  });
  try {
    assert.equal(document.getElementById('serverInfo').textContent, 'Server: ws://127.0.0.1:9999',
      'the footer must state the URL the background is dialing, not the one in storage');
  } finally {
    window.close();
  }
});

test('a live config still flags the test override it was built from', async () => {
  // The test-env shape: storage holds testPort and the background has folded it into
  // CONFIG.port (it does not report testPort/testHostname separately) — while its
  // hostname has moved on at runtime, so storage alone would name the wrong server.
  const { window, document } = await initPopup({
    storage: { hostname: 'localhost', port: 8772, testPort: 19999 },
    connectionStatus: {
      connected: true, phase: 'connected', retryAttempts: 0,
      config: { hostname: 'test-host', port: 19999 },
    },
  });
  try {
    const info = document.getElementById('serverInfo');
    assert.match(info.textContent, /ws:\/\/test-host:19999/,
      'the live config wins for BOTH parts of the URL');
    assert.match(info.textContent, /Test config active \(19999\)/,
      'preferring the live config must not lose the override flag (it lives in storage)');
  } finally {
    window.close();
  }
});

test('an override storage holds but the background never adopted is not advertised', async () => {
  const { window, document } = await initPopup({
    storage: { hostname: 'localhost', port: 8772, testPort: 19999 },
    // The background is still dialing 8772 — the override is NOT in effect.
    connectionStatus: {
      connected: false, phase: 'connecting', retryAttempts: 1,
      config: { hostname: 'localhost', port: 8772 },
    },
  });
  try {
    const info = document.getElementById('serverInfo');
    assert.equal(info.textContent, 'Server: ws://localhost:8772');
    assert.doesNotMatch(info.textContent, /Test config active/,
      'claiming an override is active while dialing the other port would be a lie');
  } finally {
    window.close();
  }
});

test('a test-config override is flagged in the footer, and repeated renders do not stack indicators', async () => {
  // No `config` in the reply (old background / no answer) → the footer falls back to
  // storage and re-applies the override precedence itself.
  const { window, document } = await initPopup({
    storage: { hostname: 'localhost', port: 8772, testPort: 19999 },
  });
  try {
    const info = document.getElementById('serverInfo');
    // the override — not the stored port — is the one actually connected to
    assert.match(info.textContent, /ws:\/\/localhost:19999/);
    assert.match(info.textContent, /Test config active \(19999\)/);
    // textContent= clears children first, so a second render must not append a 2nd indicator
    const before = info.querySelectorAll('div').length;
    assert.equal(before, 1);
    window.document.dispatchEvent(new window.Event('DOMContentLoaded'));
    await flush();
    assert.equal(info.querySelectorAll('div').length, 1, 'the indicator must not stack');
  } finally {
    window.close();
  }
});

// D: the indicator used to print `effectivePort` unconditionally, so a hostname-only
// override named the port that was NOT overridden, as if it were the override.
test('the test-config indicator names only what is actually overridden', async () => {
  const { window, document } = await initPopup({
    storage: { hostname: 'localhost', port: 8772, testHostname: 'test-host' },
  });
  try {
    const info = document.getElementById('serverInfo');
    assert.match(info.textContent, /ws:\/\/test-host:8772/);
    const indicator = info.querySelector('div');
    assert.ok(indicator, 'an active override must still be flagged');
    assert.equal(indicator.textContent, '⚙️ Test config active (test-host)');
    assert.doesNotMatch(indicator.textContent, /8772/,
      'the port was not overridden — naming it presents an untouched value as the override');
  } finally {
    window.close();
  }
});

// The observable symptom of the masked TypeError: updateDisplay() fell into its
// catch, which re-rendered with a hardcoded phase:'disconnected'. renderConnection
// mirrors the BADGE for the status line (so that stayed right), but the phase is
// what picks the action link — so a popup opened mid-reconnect offered "Reconnect"
// (the disconnected default) instead of "Stop" until the 700ms tick corrected it.
test('a popup opened while CONNECTING offers Stop — the real phase, not the catch default', async () => {
  const { window, document, consoleErrors } = await initPopup({
    connectionStatus: { connected: false, phase: 'connecting', retryAttempts: 1 },
  });
  try {
    assert.equal(document.getElementById('connAction').textContent, 'Stop',
      'the live phase must reach renderConnection — "Reconnect" means updateDisplay threw');
    assert.deepEqual(swallowed(consoleErrors), [],
      'nothing should have been swallowed into updateDisplay\'s catch on a clean open');
  } finally {
    window.close();
  }
});

test('a missing #serverInfo degrades to "no footer" — it must never throw into the catch', async () => {
  // Regression guard for the failure MODE, not just the symptom: if a future HTML edit
  // drops the element again, the popup must still render its real connection state
  // instead of silently repainting itself as disconnected.
  const html = POPUP_HTML.replace('<div id="serverInfo" class="info"></div>', '');
  assert.ok(!html.includes('id="serverInfo"'), 'fixture must really remove the element');
  const { window, document, consoleErrors } = await initPopup({
    html,
    connectionStatus: { connected: false, phase: 'connecting', retryAttempts: 1 },
  });
  try {
    assert.equal(document.getElementById('connAction').textContent, 'Stop',
      'a missing footer element must not cost the popup its real connection phase');
    assert.deepEqual(swallowed(consoleErrors), [],
      'the guard must return early — not throw into updateDisplay\'s catch');
  } finally {
    window.close();
  }
});

// A: the catch is the MECHANISM. Force a throw that has nothing to do with the
// connection (the config read) and prove the catch does NOT invent a phase: the popup
// must keep rendering the phase it really heard. Before the fix the catch re-rendered
// with phase:'disconnected' and the link flipped to "Reconnect".
test('a failed CONFIG read does not downgrade the CONNECTION display', async () => {
  const { window, document, consoleErrors } = await initPopup({
    failConfigRead: true,
    connectionStatus: { connected: false, phase: 'connecting', retryAttempts: 1 },
  });
  try {
    // (jsdom queues its own DOMContentLoaded on top of the one initPopup dispatches,
    // so init — and the throw — happens more than once; only "at least once" matters.)
    assert.ok(swallowed(consoleErrors).length > 0,
      'the fixture must really make updateDisplay throw (otherwise this proves nothing)');
    assert.equal(document.getElementById('connAction').textContent, 'Stop',
      'the catch must re-render the phase it actually heard, not a fabricated "disconnected"');
    assert.equal(document.getElementById('serverInfo').textContent, '',
      'the footer has no data to show — but that is the ONLY thing the failure costs');
  } finally {
    window.close();
  }
});

// --- Connections panel: multiple Firefox browsers are a SUPPORTED state ------
// Multi-Firefox support landed on the server: several browsers now coexist on the one
// server (each gets a registry slot) instead of kicking each other off the single
// extension socket. The popup's old #multiWarn block ("Multiple Firefox instances
// connected — they are fighting over BrowserPlug's single connection slot") is therefore
// not just dead, it is a LIE if it ever fires. It is deleted; these tests hold that.

// Rows exactly as server.py's get_connections_info() emits them (the popup does no
// date math and no label building — it renders what the server sends).
const serverRow = () => ({
  kind: 'server', label: 'BrowserPlug server', pid: 1000,
  exe_path: 'C:\\dr\\browserplug\\.venv\\Scripts\\python.exe',
  activity_hms: '10:00:00', activity_epoch: 1, since_hms: '09:00:00',
  detail: 'ws://localhost:8772  +  http://localhost:3456/mcp/',
});
// The label is the browser's NAME and nothing else: since 1.8.8 the server stops
// folding the slot and the pid into it, because the panel paints the slot on the row's
// icon and the pid under the activity time, each from its own field (get_connections_info).
const firefoxRow = (label, pid, slot) => ({
  kind: 'extension', label, pid, slot,
  exe_path: 'C:\\Program Files\\Firefox\\firefox.exe',
  activity_hms: '10:00:01', activity_epoch: 2, since_hms: '08:55:00',
  detail: 'WebSocket: 8772',
});
const clientRow = () => ({
  kind: 'mcp_client', label: 'Claude Code — C:\\dr\\browserplug', friendly: 'Claude Code',
  cwd: 'C:\\dr\\browserplug', pid: 3001, exe_path: 'C:\\claude\\claude.exe',
  activity_hms: '10:00:02', activity_epoch: 3, since_hms: '09:30:00',
  session_count: 1, detail: '1 session',
  sessions: [{
    id: 'abc12345', last_activity_hms: '10:00:02',
    commands: [{ time_hms: '10:00:02', tool: 'tabs', params: { action: 'list' } }],
  }],
});

// The flip-flop keys the server no longer sets (pinned False/None). The fixtures below
// deliberately send the values that USED to fire the warning: a payload from a stale or
// pre-upgrade server must not be able to resurrect it, and any revert of the deletion
// fails right here instead of shipping the lie.
const DEAD_FLAGS = {
  multiple_extensions: true,
  extension_warning: 'They are fighting over BrowserPlug’s single connection slot.',
  extension_instances: [
    { slot: 1, pid: 2001, friendly: 'Firefox', connect_count: 1, currently_connected: true, last_action: 'tabs.list', last_activity_hms: '10:00:01' },
    { slot: 2, pid: 2002, friendly: 'Firefox', connect_count: 1, currently_connected: true, last_action: null, last_activity_hms: null },
  ],
};

// Read each row by class, not by position: an extension row's first <span> is the icon
// wrapper carrying the slot, and the name sits after it.
const rowTexts = (document, sel) => [...document.querySelectorAll('#connList li')]
  .map((li) => { const el = li.querySelector(sel); return el ? el.textContent : null; });
const rowLabels = (document) => rowTexts(document, '.conn-name');
const rowSlots = (document) => rowTexts(document, '.conn-slot');
const rowPids = (document) => rowTexts(document, '.conn-pid');

test('the popup carries no multi-instance warning block at all (element, renderer, flag readers)', () => {
  assert.doesNotMatch(POPUP_HTML, /multiWarn/,
    'the #multiWarn element is gone — multiple Firefoxes coexist, they do not fight');
  assert.doesNotMatch(POPUP_JS, /renderMultiInstanceWarning|multiWarn/,
    'the renderer and its call site are gone');
  assert.doesNotMatch(POPUP_JS, /multiple_extensions|extension_warning|extension_instances/,
    'nothing in the popup may still read the retired flip-flop flags');
});

test('a multi-browser roster renders ONE row per Firefox and no "fighting over the slot" warning', async () => {
  const { window, document } = await initPopup({
    connections: {
      count: 4,
      generated_hms: '10:00:03',
      connections: [
        serverRow(),
        firefoxRow('Firefox Developer Edition', 2001, 1),
        firefoxRow('Firefox Developer Edition', 2002, 2),
        clientRow(),
      ],
      ...DEAD_FLAGS,   // a stale server still shouting "they are fighting"
    },
  });
  try {
    const labels = rowLabels(document);
    assert.equal(labels.length, 4, 'server + both Firefoxes + the MCP client each get a row');
    assert.deepEqual(labels.slice(0, 3),
      ['BrowserPlug server', 'Firefox Developer Edition', 'Firefox Developer Edition'],
      'each browser renders as its own row, named by the server and nothing else');
    // Two browsers of one edition are the SAME string, which is the normal case (two
    // Operas, two Firefoxes). What tells them apart is the slot painted on the icon and
    // the pid under the activity time - the two fields the name stopped carrying.
    assert.deepEqual(rowSlots(document).slice(1, 3), ['1', '2'],
      'the slot rides the row icon, one per browser');
    assert.deepEqual(rowPids(document).slice(1, 3), ['pid 2001', 'pid 2002']);
    assert.equal(document.getElementById('connCount').textContent, '4');

    // The whole point: coexistence is normal now. No warning may reach the user, even
    // though the payload carries the legacy flag + instance list.
    assert.ok(!document.getElementById('multiWarn'), 'the warning element must not exist');
    const body = document.body.textContent;
    assert.doesNotMatch(body, /fighting/i, 'the browsers no longer fight over a single slot');
    assert.doesNotMatch(body, /Multiple Firefox instances/i);

    // Both browser rows keep the normal row furniture (activity + detail + since).
    const ffRows = [...document.querySelectorAll('#connList li')].slice(1, 3);
    for (const li of ffRows) {
      assert.equal(li.querySelector('.activity').textContent, '10:00:01');
      assert.match(li.textContent, /WebSocket: 8772 · since 08:55:00/);
    }
  } finally {
    window.close();
  }
});

// --- the row's two identity columns ------------------------------------------------
// Both moved out of the name in 1.8.8: the slot onto the icon (it is what
// browsers(target=N) takes, so it belongs to the row, not to a string), the pid under
// the activity time (where every kind of row shows it, once).
test('the slot is painted ON the row icon, and the pid sits under the activity time', async () => {
  const { window, document } = await initPopup({
    connections: {
      count: 3, generated_hms: '10:00:03',
      connections: [serverRow(), firefoxRow('Opera GX', 45216, 6), clientRow()],
      ...DEAD_FLAGS,
    },
  });
  try {
    const [srv, ff, client] = [...document.querySelectorAll('#connList li')];

    // The number is INSIDE the icon wrapper, over the glyph it dims - not a separate
    // column, and not a prefix on the name.
    const wrap = ff.querySelector('.conn-idwrap');
    assert.ok(wrap, 'the browser row wears an icon wrapper');
    assert.ok(wrap.querySelector('svg.conn-ic'), 'the glyph is still there behind it');
    assert.equal(wrap.querySelector('.conn-slot').textContent, '6');
    assert.match(wrap.title, /browsers\(action="use", target=6\)/,
      'hovering it says what the number is for');
    assert.equal(ff.querySelector('.conn-name').textContent, 'Opera GX',
      'and the name is left to be the name');

    // The pid is the last thing in the right-hand column, under the time.
    const right = ff.querySelector('.conn-right');
    assert.deepEqual([...right.children].map(el => el.className), ['activity', 'conn-pid']);
    assert.equal(right.querySelector('.conn-pid').textContent, 'pid 45216');

    // Every kind of row, one place. The server and the client carry no slot: only a
    // browser is selectable, and a number on those rows would mean nothing.
    assert.deepEqual([srv, client].map(li => li.querySelector('.conn-pid').textContent),
      ['pid 1000', 'pid 3001']);
    assert.deepEqual([srv, client].map(li => li.querySelector('.conn-slot')), [null, null]);
    // The client's pid left its detail line when it gained the column.
    assert.equal(client.querySelector('.conn-detail').textContent, '1 session · since 09:30:00');
  } finally {
    window.close();
  }
});

test('a single-browser roster still renders exactly as before (unprefixed Firefox row)', async () => {
  const { window, document, consoleErrors } = await initPopup({
    connections: {
      count: 3,
      generated_hms: '10:00:03',
      connections: [serverRow(), firefoxRow('Firefox extension', 2001, 1), clientRow()],
      // What the server now always sends: multi-instance is not a conflict.
      multiple_extensions: false, extension_warning: null, extension_instances: [],
    },
  });
  try {
    assert.deepEqual(rowLabels(document),
      ['BrowserPlug server', 'Firefox extension', 'Claude Code — C:\\dr\\browserplug'],
      'one browser → the label the server sends, with no [slot] prefix invented by the popup');
    assert.equal(document.getElementById('connCount').textContent, '3');
    assert.ok(!document.getElementById('multiWarn'));
    // The MCP client row still expands its sessions/commands (unchanged behavior).
    const client = document.querySelectorAll('#connList li')[2];
    assert.match(client.textContent, /session abc12345 - last 10:00:02/);
    assert.match(client.textContent, /tabs\s+action: list/);
    assert.deepEqual(swallowed(consoleErrors), [], 'rendering a roster must not throw');
  } finally {
    window.close();
  }
});

test('a client row expands from its head, and the 2s refresh does not fold it back', async () => {
  const { window, document } = await initPopup({
    connections: {
      count: 3, generated_hms: '10:00:03',
      connections: [serverRow(), firefoxRow('Firefox extension', 2001, 1), clientRow()],
      ...DEAD_FLAGS,
    },
  });
  try {
    const rows = () => [...document.querySelectorAll('#connList li')];
    let client = rows()[2];
    assert.ok(client.classList.contains('clickable'), 'a client with sessions is the expandable kind');
    assert.equal(client.getAttribute('aria-expanded'), 'false');
    assert.equal(client.querySelector('.sessions').hidden, true, 'collapsed by default');
    assert.ok(client.querySelector('.chev'), 'and says so with a chevron');
    for (const li of rows().slice(0, 2)) {
      assert.equal(li.getAttribute('aria-expanded'), null, 'a row with nothing to expand has no disclosure');
      assert.equal(li.querySelector('.chev'), null);
    }

    client.querySelector('.conn-head').dispatchEvent(new window.Event('click', { bubbles: true }));
    assert.equal(client.getAttribute('aria-expanded'), 'true');
    assert.equal(client.querySelector('.sessions').hidden, false);

    // The list is rebuilt every 2s; the expansion is keyed by pid and must survive it.
    await window.updateConnections();
    client = rows()[2];
    assert.equal(client.getAttribute('aria-expanded'), 'true');
    assert.equal(client.querySelector('.sessions').hidden, false);
  } finally {
    window.close();
  }
});

// --- the Usage tab ----------------------------------------------------------
// popup/usage-pane.js draws it (its own test covers every card state); what is pinned
// here is popup.js's part: nothing is fetched for the pane while it is hidden, and the
// moment the tab is shown the providers are asked and the pane fills.
test('the Usage pane fetches only once its tab is showing', async () => {
  const { window, document } = await initPopup({
    vendors: [DEEPSEEK_VIEW, POPUP_TABS, USAGE_PANE],
    connections: { count: 1, generated_hms: '10:00:03', connections: [serverRow()], ...DEAD_FLAGS },
  });
  try {
    const sent = [];
    const orig = window.browser.runtime.sendMessage;
    window.browser.runtime.sendMessage = (msg, cb) => { sent.push(msg.action); return orig(msg, cb); };

    assert.equal(document.getElementById('pane-usage').hidden, true, 'the popup opened on Status');
    await window.updateUsagePane();
    assert.deepEqual(sent, [], 'a hidden pane asks nobody for anything');
    assert.equal(document.getElementById('usagePane').childElementCount, 0);

    window.PopupTabs.show('usage');
    await flush();
    await flush();
    assert.equal(document.getElementById('pane-usage').hidden, false);
    assert.ok(sent.includes('getClaudeUsage'), 'showing the tab is what triggers the fetch');
    assert.ok(!sent.includes('getOpenAIUsage'), 'OpenAI ships Disabled, and Disabled means no fetch');
    const titles = [...document.querySelectorAll('#usagePane .card h2')].map(h => h.textContent);
    assert.deepEqual(titles, ['Anthropic'],
      'Disabled draws no card at all — OpenAI and DeepSeek ship Disabled, so they are simply not here');
    assert.equal(document.querySelector('#usagePane .card .state').textContent, 'waiting',
      'no reading yet is said out loud, not left blank');
  } finally {
    window.close();
  }
});

// popup.js's other part: the tab itself. All three providers off and there is nothing
// for the pane to draw, so the tab comes off the strip rather than opening onto an empty
// box. Decided before the strip restores, so a popup remembered on Usage opens on Status
// instead of flinching off it a frame later.
test('the Usage tab disappears when every provider is turned off', async () => {
  const allOff = {
    anthropicDisplayMode: 'disabled', openaiDisplayMode: 'disabled', deepseekDisplayMode: 'disabled',
    copilotDisplayMode: 'disabled',
  };
  const { window, document } = await initPopup({
    vendors: [DEEPSEEK_VIEW, POPUP_TABS, USAGE_PANE],
    storage: { hostname: 'localhost', port: 8772, popupTab: 'usage', ...allOff },
  });
  try {
    const tab = document.querySelector('.tabs [role="tab"][data-tab="usage"]');
    assert.equal(tab.hidden, true, 'no provider to report on means no tab');
    assert.equal(window.PopupTabs.current(), 'status',
      'and the popup does not open on the tab it just took away');

    // One provider back on and the tab returns on the next Usage pane tick, which runs
    // while the pane is hidden too, with no reload.
    window.browser.storage.sync.get = (defaults) => Promise.resolve({
      ...defaults, hostname: 'localhost', port: 8772, deepseekDisplayMode: 'tab',
    });
    await window.updateUsagePane();
    await flush();
    assert.equal(tab.hidden, false, 'turning one back on brings the tab back');
  } finally {
    window.close();
  }
});

// The row is rendered TWICE by two functions that must agree: updateConnections()
// paints the panel, buildConnectionCopyNode() rebuilds it for right-click "Copy
// connection info". A browser row with a [slot] label has to survive both paths.
test('right-click "Copy connection info" renders a slot-labelled Firefox row like the panel', async () => {
  const ff = firefoxRow('Firefox Developer Edition', 2002, 2);
  const { window, document } = await initPopup({
    connections: {
      count: 3, generated_hms: '10:00:03',
      connections: [serverRow(), firefoxRow('Firefox Developer Edition', 2001, 1), ff],
      ...DEAD_FLAGS,
    },
  });
  try {
    const panelRow = document.querySelectorAll('#connList li')[2];
    const copyNode = window.buildConnectionCopyNode(ff, { hideEmptySessions: true, toolCallSort: 'bottom' });
    for (const fragment of ['Firefox Developer Edition', '10:00:01', 'WebSocket: 8772', 'since 08:55:00']) {
      assert.ok(panelRow.textContent.includes(fragment), `panel row shows "${fragment}"`);
      assert.ok(copyNode.textContent.includes(fragment), `copied row shows "${fragment}"`);
    }
    // The panel has three columns the copy does not: the slot on the icon, the pid and
    // the time on the right. A paste keeps all three, spelled out on the one line.
    assert.equal(panelRow.querySelector('.conn-slot').textContent, '2');
    assert.equal(panelRow.querySelector('.conn-pid').textContent, 'pid 2002');
    assert.ok(copyNode.textContent.includes(
      '[2] Firefox Developer Edition pid 2002 (activity: 10:00:01)'),
      `copied header reads: ${copyNode.textContent.slice(0, 80)}`);
    assert.doesNotMatch(copyNode.textContent, /fighting|Multiple Firefox instances/i);
  } finally {
    window.close();
  }
});

// A copied row exists to be pasted somewhere and read by someone else, so it has to say
// what the connection IS. exe_path and cwd alone do not: every node CLI has an exe_path
// of plain node.exe and a cwd of the project, which is exactly how a DeepSeek Harness
// came out as an anonymous "Node" row (2026-08-17).
test('right-click "Copy connection info" carries the command line and the launcher hop', async () => {
  const dsh = {
    ...clientRow(),
    label: 'DSH — C:\\dr\\myproject', friendly: 'DSH',
    cwd: 'C:\\dr\\myproject', pid: 31524, exe_path: 'C:\\nvm\\v24.15.0\\node.exe',
    cmdline: 'node C:\\npm-cache\\_npx\\node_modules\\@deepseek-ai\\dsh\\lib\\bin.js web',
    launcher_pid: 776, launcher_version: '1.7.24',
  };
  const { window, document } = await initPopup({
    connections: {
      count: 2, generated_hms: '10:00:03',
      connections: [serverRow(), dsh], ...DEAD_FLAGS,
    },
  });
  try {
    const copied = window.buildConnectionCopyNode(dsh, { hideEmptySessions: true, toolCallSort: 'bottom' }).textContent;
    assert.ok(copied.includes('exe_path C:\\nvm\\v24.15.0\\node.exe'), 'exe_path still copied');
    assert.ok(copied.includes('cwd C:\\dr\\myproject'), 'cwd still copied');
    assert.ok(copied.includes('cmdline node C:\\npm-cache\\_npx\\node_modules\\@deepseek-ai\\dsh\\lib\\bin.js web'),
      'the argv that names the tool is what makes the copy useful');
    assert.ok(copied.includes('launcher pid 776 · v1.7.24'), 'which launcher the client reached us through');
    // The panel's tooltip is the same information; the two renderings must agree.
    const tip = document.querySelectorAll('#connList li')[1].querySelector('.conn-name').title;
    assert.match(tip, /cmdline: node C:\\npm-cache/);
  } finally {
    window.close();
  }
});

// The DeepSeek price clock is DeepSeek's alone: Anthropic's peak hours are a different
// schedule for a different reason (capacity, not price), so a row must never be handed
// the other client's windows. The wiring is what these three pin — the clock itself is
// tested in deepseek-view.test.mjs. The instants are fixed against DeepSeek's windows
// (peak 01:00-04:00 and 06:00-10:00 UTC).
const DURING_PEAK = new Date(Date.UTC(2026, 7, 17, 2, 0, 0));
const OFF_PEAK = new Date(Date.UTC(2026, 7, 17, 14, 0, 0));
// The row prints the SHORT name (server.py's `_friendly_from_cmdline`): a connection
// row is one line, and "DeepSeek Harness — C:\dr\myproject" spends most of it on a name.
const dshRow = () => ({
  ...clientRow(), label: 'DSH — C:\\dr\\myproject', friendly: 'DSH',
  pid: 31524, detail: '1 session',
});

test('the DeepSeek peak badge rides the DSH row', async () => {
  const dsh = dshRow();
  const { window, document } = await initPopup({
    vendors: [DEEPSEEK_VIEW], now: DURING_PEAK,
    storage: { deepseekDisplayMode: 'client' },
    connections: { count: 2, generated_hms: '10:00:03', connections: [serverRow(), dsh], ...DEAD_FLAGS },
  });
  try {
    const li = document.querySelectorAll('#connList li')[1];
    // The indicator is the same 38x8 bar the Claude Code row draws, so the row is
    // searched for that element rather than for text (the phase lives in its tooltip).
    const bar = [...li.querySelectorAll('span')].find(s => s.style.width === '38px');
    assert.ok(bar, 'the panel row draws the peak bar');
    assert.match(bar.title, /^PEAK (from|starts in)/, 'and it carries the phase in its tooltip');
    assert.match(li.textContent, /· 1 session · since/, 'the bar leads the line, the session count trails it');
    assert.equal(li.querySelector('.conn-pid').textContent, 'pid 31524', 'and the pid is in its own column');
    const copied = window.buildConnectionCopyNode(dsh, {}).textContent;
    assert.match(copied, /PEAK (\d+:\d\d:\d\d left|in \d)/, 'and survives the copy as text');
  } finally {
    window.close();
  }
});

test('outside the peak window the DSH row carries no bar at all', async () => {
  // Default lead is 0, so off-peak the indicator is absent rather than empty — the same
  // rule the Claude Code row follows, and the row must render as if it never existed.
  const dsh = dshRow();
  const { window, document } = await initPopup({
    vendors: [DEEPSEEK_VIEW], now: OFF_PEAK,
    storage: { deepseekDisplayMode: 'client' },
    connections: { count: 2, generated_hms: '10:00:03', connections: [serverRow(), dsh], ...DEAD_FLAGS },
  });
  try {
    const li = document.querySelectorAll('#connList li')[1];
    assert.equal([...li.querySelectorAll('span')].filter(s => s.style.width === '38px').length, 0);
    assert.match(li.textContent, /1 session · since/, 'and the detail line closes up');
    assert.doesNotMatch(window.buildConnectionCopyNode(dsh, {}).textContent, /PEAK/);
  } finally {
    window.close();
  }
});

test('no other client row gets the DeepSeek windows', async () => {
  const { window, document } = await initPopup({
    vendors: [DEEPSEEK_VIEW], now: DURING_PEAK,
    connections: {
      count: 3, generated_hms: '10:00:03',
      connections: [serverRow(), clientRow(),
                    { ...clientRow(), label: 'Kilo CLI — C:\\dr', friendly: 'Kilo CLI', pid: 4242,
                      provider: 'deepseek', model: 'deepseek-chat', detail: '1 session' }],
      ...DEAD_FLAGS,
    },
  });
  try {
    // Note the second row: a Kilo CLI actually TALKING to DeepSeek still does not get the
    // badge. The billing windows belong to the harness's own account, and this row's
    // provider is a model choice, not a bill we can read.
    for (const li of [...document.querySelectorAll('#connList li')].slice(1)) {
      const bars = [...li.querySelectorAll('span')].filter(s => (s.title || '').startsWith('PEAK'));
      assert.equal(bars.length, 0, li.textContent.slice(0, 60));
    }
  } finally {
    window.close();
  }
});

// --- Where an indicator is allowed to appear --------------------------------
// Options ▸ Advanced ▸ <provider> sessions, three answers: 'disabled', 'tab' (the Usage
// tab only) and 'client' (the Usage tab and the client's row, where these have always
// been). The setting decides ONE thing per provider, and getting it wrong is invisible
// (a bar in two places, or in neither), so each mode is pinned end to end.

// The provider cards the Usage tab draws once it is shown.
async function usageCards(window, document) {
  window.PopupTabs.show('usage');
  await flush();
  await flush();
  return [...document.querySelectorAll('#usagePane .card h2')].map(h => h.textContent);
}

const rowBars = (li) => [...li.querySelectorAll('span')].filter(s => s.style.width === '38px');

test('DeepSeek ships disabled; "client" is the Usage tab plus the client row', async () => {
  const off = await initPopup({
    vendors: [DEEPSEEK_VIEW, POPUP_TABS, USAGE_PANE], now: DURING_PEAK,
    connections: { count: 2, generated_hms: '10:00:03', connections: [serverRow(), dshRow()], ...DEAD_FLAGS },
  });
  try {
    assert.equal(rowBars(off.document.querySelectorAll('#connList li')[1]).length, 0, 'no bar on the row');
    assert.ok(!(await usageCards(off.window, off.document)).includes('DeepSeek'), 'and no card');
  } finally {
    off.window.close();
  }
  const { window, document } = await initPopup({
    vendors: [DEEPSEEK_VIEW, POPUP_TABS, USAGE_PANE], now: DURING_PEAK,
    storage: { deepseekDisplayMode: 'client' },
    connections: { count: 2, generated_hms: '10:00:03', connections: [serverRow(), dshRow()], ...DEAD_FLAGS },
  });
  try {
    assert.ok(rowBars(document.querySelectorAll('#connList li')[1]).length, 'bar on the row');
    assert.ok((await usageCards(window, document)).includes('DeepSeek'), 'and a card on the Usage tab');
  } finally {
    window.close();
  }
});

test('"tab" keeps the indicator off the row and on the Usage tab', async () => {
  const { window, document } = await initPopup({
    storage: { deepseekDisplayMode: 'tab' },
    vendors: [DEEPSEEK_VIEW, POPUP_TABS, USAGE_PANE], now: DURING_PEAK,
    connections: { count: 2, generated_hms: '10:00:03', connections: [serverRow(), dshRow()], ...DEAD_FLAGS },
  });
  try {
    const li = document.querySelectorAll('#connList li')[1];
    assert.equal(rowBars(li).length, 0, 'not on the row');
    assert.match(li.textContent, /1 session · since/, 'and the detail line closes up');
    assert.ok((await usageCards(window, document)).includes('DeepSeek'), 'the Usage tab still has it');
  } finally {
    window.close();
  }
});

test('"disabled" removes it from both places', async () => {
  const { window, document } = await initPopup({
    storage: { deepseekDisplayMode: 'disabled' },
    vendors: [DEEPSEEK_VIEW, POPUP_TABS, USAGE_PANE], now: DURING_PEAK,
    connections: { count: 2, generated_hms: '10:00:03', connections: [serverRow(), dshRow()], ...DEAD_FLAGS },
  });
  try {
    assert.equal(rowBars(document.querySelectorAll('#connList li')[1]).length, 0);
    assert.ok(!(await usageCards(window, document)).includes('DeepSeek'));
  } finally {
    window.close();
  }
});

test('a copied row mirrors the panel: nothing inline while the bar is kept to the tab', async () => {
  // buildConnectionCopyNode has its own renderer, and a copied row that disagrees with
  // the screen is worse than one that says less.
  const dsh = dshRow();
  const { window } = await initPopup({
    storage: { deepseekDisplayMode: 'tab' },
    vendors: [DEEPSEEK_VIEW], now: DURING_PEAK,
    connections: { count: 2, generated_hms: '10:00:03', connections: [serverRow(), dsh], ...DEAD_FLAGS },
  });
  try {
    assert.doesNotMatch(window.buildConnectionCopyNode(dsh, {}).textContent, /PEAK/);
  } finally {
    window.close();
  }
});

// --- GitHub Copilot Chat ------------------------------------------------------
// The row shows what VS Code's Copilot is doing (the server's `copilot` block) next to
// the github.com quota this browser reads. The two can be different GitHub accounts;
// the row must then refuse to draw the quota, on screen and in the copied block alike.
const copilotRow = () => ({
  ...clientRow(), friendly: 'GitHub Copilot Chat', vendor: 'Microsoft',
  label: 'GitHub Copilot Chat — c:\\work', cwd: 'c:\\work',
  provider: 'github-copilot', model: 'gpt-6-luna',
  copilot: { model: 'gpt-6-luna', account: 'octo-editor', sku: 'free_limited_copilot', requests: 3,
             last_request_hms: '16:52:12', chat_title: 'fix the parser', chat_turns: 2,
             workspace: 'c:\\work' },
});

async function withCopilotUsage(window, data) {
  const orig = window.browser.runtime.sendMessage;
  window.browser.runtime.sendMessage = (msg, cb) => {
    if (msg.action !== 'getCopilotUsage') return orig(msg, cb);
    const reply = { success: true, data };
    if (typeof cb === 'function') cb(reply);
    return Promise.resolve(reply);
  };
  await window.updateConnections();
}

const copilotQuota = (login) => ({
  ok: true, login, plan: 'free', resetsAt: null, premium: null,
  chat: { percent: 25, used: 50, total: 200, unlimited: false },
  completions: { percent: 5, used: 100, total: 2000, unlimited: false },
});

test('a Copilot Chat row carries the bar and the session badge when the accounts match', async () => {
  const row = copilotRow();
  const { window, document } = await initPopup({
    vendors: [COPILOT_VIEW],
    connections: { count: 2, generated_hms: '10:00:03', connections: [serverRow(), row], ...DEAD_FLAGS },
  });
  try {
    await withCopilotUsage(window, copilotQuota('octo-editor'));
    const li = document.querySelectorAll('#connList li')[1];
    assert.ok([...li.querySelectorAll('span')].some(s => s.style.width === '38px'), 'the quota bar');
    assert.match(li.textContent, /◆ gpt-6-luna/);
    assert.match(window.buildConnectionCopyNode(row, {}).textContent, /Chat 25% \(50 of 200\) · gpt-6-luna · 2 turns/);
  } finally {
    window.close();
  }
});

test('a Copilot Chat row says "other account" instead of another account\'s quota', async () => {
  const row = copilotRow();
  const { window, document } = await initPopup({
    vendors: [COPILOT_VIEW],
    connections: { count: 2, generated_hms: '10:00:03', connections: [serverRow(), row], ...DEAD_FLAGS },
  });
  try {
    await withCopilotUsage(window, copilotQuota('octo-personal'));
    const li = document.querySelectorAll('#connList li')[1];
    assert.equal([...li.querySelectorAll('span')].filter(s => s.style.width === '38px').length, 0);
    assert.match(li.querySelector('.conn-na').textContent, /other account/);
    assert.match(window.buildConnectionCopyNode(row, {}).textContent, /usage is for octo-personal, not octo-editor/);
  } finally {
    window.close();
  }
});

test("Copilot in 'tab' mode: a standing account mismatch still forces a re-read of github.com", async () => {
  // The rows draw nothing in 'tab' mode, so the rows' copy of the reading stays null; the
  // re-check must key on the reading the Usage tab itself received.
  const row = copilotRow();
  const { window } = await initPopup({
    vendors: [COPILOT_VIEW],
    storage: { copilotDisplayMode: 'tab' },
    connections: { count: 2, generated_hms: '10:00:03', connections: [serverRow(), row], ...DEAD_FLAGS },
  });
  try {
    const asked = [];
    const orig = window.browser.runtime.sendMessage;
    window.browser.runtime.sendMessage = (msg, cb) => {
      if (msg.action !== 'getCopilotUsage') return orig(msg, cb);
      asked.push(!!msg.force);
      const reply = { success: true, data: copilotQuota('octo-personal') };
      if (typeof cb === 'function') cb(reply);
      return Promise.resolve(reply);
    };
    await window.updateConnections();
    await window.readCopilotUsage();
    await window.readCopilotUsage();
    assert.deepEqual(asked, [false, true], 'the second read should have bypassed the cache');
  } finally {
    window.close();
  }
});

test('the vendor rides the row tooltip and the copied block', async () => {
  // Who ships a client, named server-side. Null for anything unrecognized, and then it
  // must print nothing at all rather than "unknown".
  const named = { ...clientRow(), friendly: 'GitHub Copilot Chat', vendor: 'Microsoft',
                  label: 'GitHub Copilot Chat — C:\\dr' };
  const { window, document } = await initPopup({
    connections: { count: 2, generated_hms: '10:00:03', connections: [serverRow(), named], ...DEAD_FLAGS },
  });
  try {
    const li = document.querySelectorAll('#connList li')[1];
    assert.match(li.querySelector('.conn-name').title, /vendor: Microsoft/);
    assert.match(window.buildConnectionCopyNode(named, {}).textContent, /vendor Microsoft/);

    const plain = window.buildConnectionCopyNode({ ...clientRow(), vendor: null }, {}).textContent;
    assert.doesNotMatch(plain, /vendor/);
  } finally {
    window.close();
  }
});

test('a direct HTTP client says so instead of leaving the launcher line blank', async () => {
  const duo = { ...clientRow(), label: 'GitLab Duo CLI — C:\\dr', friendly: 'GitLab Duo CLI',
                launcher_pid: null, cmdline: null };
  const { window } = await initPopup({
    connections: { count: 2, generated_hms: '10:00:03',
                   connections: [serverRow(), duo], ...DEAD_FLAGS },
  });
  try {
    const copied = window.buildConnectionCopyNode(duo, {}).textContent;
    assert.ok(copied.includes('launcher none (direct HTTP client)'));
    assert.ok(!copied.includes('cmdline'), 'an unreadable command line prints no empty label');
  } finally {
    window.close();
  }
});

// --- a browser on the other side of the LAN ---------------------------------
// A remote browser has no pid, no exe path and no start time on the server's machine,
// so the only thing that says WHICH browser it is arrives in its hello. Both places a
// person can read a row (the tooltip, and "Copy connection info") print it from the one
// builder, browserFacts, because the two must never disagree about a row.
const phoneRow = () => ({
  kind: 'extension', label: '[Remote] Firefox for Android Nightly', slot: 2,
  is_remote: true, remote_addr: '192.168.30.171', pid: null, exe_path: null,
  engine: null, platform: { os: 'android', arch: 'aarch64' },
  browser_info: { name: 'Firefox Nightly', vendor: 'Mozilla', version: '157.0a1',
                  buildID: '20260901215037' },
  version: '1.7.12',
  activity_hms: '10:00:01', activity_epoch: 2, since_hms: null,
  detail: 'WebSocket: 8772 · from 192.168.30.171',
});

test('a remote browser row names the phone, and both readers of it say the same thing', async () => {
  const phone = phoneRow();
  const { window, document } = await initPopup({
    connections: { count: 3, generated_hms: '10:00:03',
                   connections: [serverRow(), phone, clientRow()], ...DEAD_FLAGS },
  });
  try {
    assert.ok(rowLabels(document).includes('[Remote] Firefox for Android Nightly'),
      'the server-built label is rendered verbatim');
    // Remote stays in the NAME (no column of the panel carries it); the slot still
    // rides the icon, and there is no pid line at all - that machine's processes are
    // not ours to number.
    assert.deepEqual(rowSlots(document).slice(1, 2), ['2']);
    assert.deepEqual(rowPids(document).slice(1, 2), [null]);
    const wanted = [
      'remote address 192.168.30.171',
      'browser Firefox Nightly 157.0a1 · Mozilla · build 20260901215037',
      'platform android aarch64',
      'extension v1.7.12',
    ];
    const tooltip = [...document.querySelectorAll('#connList li span')]
      .map(s => s.title || '').find(t => t.includes('192.168.30.171'));
    assert.ok(tooltip, 'the row a person hovers carries the identification');
    const copied = window.buildConnectionCopyNode(phone, {}).textContent;
    for (const fact of wanted) {
      assert.ok(tooltip.includes(fact), `tooltip is missing: ${fact}`);
      assert.ok(copied.includes(fact), `copied block is missing: ${fact}`);
    }
  } finally {
    window.close();
  }
});

test("a fork's own identity key survives into the copied row", async () => {
  // Zen answers getBrowserInfo() with name:"Firefox" and adds zen:{version}. That extra
  // key is the ONLY thing that outs it from a remote browser, so it must not be dropped
  // on the way to the one place a person pastes from.
  const zen = { ...phoneRow(), label: '[Remote] Firefox', platform: { os: 'linux' },
                browser_info: { name: 'Firefox', vendor: 'Mozilla', version: '152.0.5',
                                zen: { version: '1.21.6b' } } };
  const { window } = await initPopup({
    connections: { count: 2, generated_hms: '10:00:03',
                   connections: [serverRow(), zen], ...DEAD_FLAGS },
  });
  try {
    assert.match(window.buildConnectionCopyNode(zen, {}).textContent, /zen 1\.21\.6b/);
  } finally {
    window.close();
  }
});

test('an ordinary local browser row gains no remote furniture', async () => {
  const local = firefoxRow('Firefox extension', 2001, 1);
  const { window, document } = await initPopup({
    connections: { count: 3, generated_hms: '10:00:03',
                   connections: [serverRow(), local, clientRow()], ...DEAD_FLAGS },
  });
  try {
    const copied = window.buildConnectionCopyNode(local, {}).textContent;
    assert.ok(!copied.includes('remote address'), 'nothing invents an address');
    assert.ok(!copied.includes('platform '), 'a payload with no platform prints no label');
    assert.ok(!document.body.textContent.includes('Remote'));
  } finally {
    window.close();
  }
});

test('the connections list is not bounded on mobile (the overlay scrolls as one page)', () => {
  // The desktop bound exists for Firefox's ~600px browserAction cap, so the panels below
  // the list stay reachable. Android has no such cap: the popup IS the screen, and at
  // zoom 1.5 the bound showed two connection rows out of six.
  assert.match(POPUP_HTML, /#connList\s*\{[^}]*max-height:\s*400px/,
    'desktop keeps its bound');
  const mobile = POPUP_HTML.match(/body\.mobile\s+#connList\s*\{([^}]*)\}/);
  assert.ok(mobile, 'mobile must override the bound');
  assert.match(mobile[1], /max-height:\s*none/);
  // Both axes together: a `visible` axis computes to `auto` while the other is not
  // visible, which would silently keep the scroll container.
  assert.match(mobile[1], /overflow:\s*visible/,
    'release both axes; clearing overflow-y alone leaves the list scrolling');
});

// --- the two capability blocks, told the truth about a phone ---------------------------
// Both used to send a phone to about:addons ▸ Permissions. Firefox for Android has no
// about:addons at all, so the file:// block pointed at a page that does not exist and the
// proxy block named the only route to a permission a phone CAN have (proxy.onRequest is
// supported there since 60; only proxy.settings is not).

test('popup: on mobile the file:// block explains why, instead of desktop steps', async () => {
  const { window, document } = await initPopup({ platformOs: 'android' });
  try {
    assert.equal(document.getElementById('fileAccessText').textContent, 'Unavailable on mobile');
    assert.equal(document.getElementById('fileAccessDesktopHow').hidden, true,
      'Ctrl+O and the address bar mean nothing on a phone');
    const why = document.getElementById('fileAccessMobileWhy');
    assert.equal(why.hidden, false, 'the row says Unavailable; this says why');
    assert.match(why.textContent, /about:addons/, 'it names the desktop control that is missing');
    assert.equal(document.getElementById('fileAccessEnableHelp').hidden, true,
      'and never offers enable steps that cannot work');
  } finally {
    window.close();
  }
});

test('popup: on DESKTOP the file:// block is unchanged', async () => {
  const { window, document } = await initPopup({ platformOs: 'win' });
  try {
    assert.equal(document.getElementById('fileAccessDesktopHow').hidden, false);
    assert.equal(document.getElementById('fileAccessMobileWhy').hidden, true);
  } finally {
    window.close();
  }
});

test('popup: on mobile the proxy block offers a Grant button, not about:addons', async () => {
  const { window, document } = await initPopup({ platformOs: 'android', proxyGranted: false });
  try {
    assert.equal(document.getElementById('proxyAccessEnableHelp').hidden, true,
      'the about:addons route does not exist on a phone');
    assert.equal(document.getElementById('proxyAccessMobileHelp').hidden, false);
    assert.equal(document.getElementById('proxyAccessGrant').hidden, false);
  } finally {
    window.close();
  }
});

test('popup: the Grant button asks Firefox for the proxy permission', async () => {
  const { window, document, permissionRequests } =
    await initPopup({ platformOs: 'android', proxyGranted: false });
  try {
    document.getElementById('proxyAccessGrant').dispatchEvent(new window.Event('click'));
    await flush();
    await flush();
    // Contents, not identity: the object is built in the jsdom realm, so a strict
    // deepEqual against a Node-realm literal fails on the prototype alone.
    assert.equal(permissionRequests.length, 1, 'exactly one request, from the tap');
    assert.deepEqual([...permissionRequests[0].permissions], ['proxy']);
  } finally {
    window.close();
  }
});

test('popup: nothing offers to grant what is already granted', async () => {
  const { window, document } = await initPopup({ platformOs: 'android', proxyGranted: true });
  try {
    assert.equal(document.getElementById('proxyAccessGrant').hidden, true);
    assert.equal(document.getElementById('proxyAccessMobileHelp').hidden, true);
  } finally {
    window.close();
  }
});

test('popup: on DESKTOP the proxy block keeps the about:addons route and no Grant button', async () => {
  const { window, document } = await initPopup({ platformOs: 'win', proxyGranted: false });
  try {
    assert.equal(document.getElementById('proxyAccessEnableHelp').hidden, false);
    assert.equal(document.getElementById('proxyAccessMobileHelp').hidden, true);
    assert.equal(document.getElementById('proxyAccessGrant').hidden, true);
  } finally {
    window.close();
  }
});

// --- press and hold: the only route to Copy connection info on a phone -----------------
// The menu was bound to `contextmenu` alone, which touch never fires, so the block people
// paste into bug reports was unreachable from the browser it is hardest to debug.

// jsdom has no PointerEvent; the handlers only read button/clientX/clientY, so a plain
// Event carrying those is the same thing as far as this code is concerned.
function pointer(window, type, { button = 0, x = 10, y = 10 } = {}) {
  const e = new window.Event(type, { bubbles: true });
  Object.assign(e, { button, clientX: x, clientY: y });
  return e;
}

const firstConnRow = (document) => document.querySelector('#connList li');
const ctxMenu = (document) => document.querySelector('.ctx-menu');
const ctxOpen = (document) => {
  const m = ctxMenu(document);
  return !!m && m.style.display !== 'none';
};

async function popupWithRows(extra = {}) {
  return initPopup({
    connections: { count: 3, generated_hms: '10:00:03',
                   connections: [serverRow(), firefoxRow('Firefox extension', 2001, 1), clientRow()],
                   ...DEAD_FLAGS },
    ...extra,
  });
}

test('popup: holding a connection row for 3s opens the menu as a modal', async () => {
  const { window, document } = await popupWithRows();
  try {
    const row = firstConnRow(document);
    assert.ok(row, 'there is a row to hold');
    assert.equal(ctxOpen(document), false, 'nothing is open before the hold');

    row.dispatchEvent(pointer(window, 'pointerdown'));
    assert.ok(row.classList.contains('holding'), 'the row shows it is being held');
    assert.equal(ctxOpen(document), false, 'and not a moment before the threshold');

    await window.__advance(3000);
    assert.equal(ctxOpen(document), true);
    assert.ok(ctxMenu(document).classList.contains('as-modal'),
      'centred over a backdrop, not pinned to a pointer nobody can see under a thumb');
    assert.notEqual(document.querySelector('.ctx-backdrop').style.display, 'none');
    assert.equal(row.classList.contains('holding'), false, 'the press styling is released');
  } finally {
    window.close();
  }
});

test('popup: letting go early opens nothing', async () => {
  const { window, document } = await popupWithRows();
  try {
    const row = firstConnRow(document);
    row.dispatchEvent(pointer(window, 'pointerdown'));
    await window.__advance(1500);
    row.dispatchEvent(pointer(window, 'pointerup'));
    await window.__advance(5000);
    assert.equal(ctxOpen(document), false, 'a tap is not a hold');
  } finally {
    window.close();
  }
});

test('popup: moving the finger cancels the hold — that gesture is a scroll', async () => {
  const { window, document } = await popupWithRows();
  try {
    const row = firstConnRow(document);
    row.dispatchEvent(pointer(window, 'pointerdown', { x: 10, y: 10 }));
    row.dispatchEvent(pointer(window, 'pointermove', { x: 10, y: 60 }));
    await window.__advance(5000);
    assert.equal(ctxOpen(document), false,
      'a list that scrolls must not sprout menus while you scroll it');
  } finally {
    window.close();
  }
});

test('popup: a small tremor during the hold does NOT cancel it', async () => {
  const { window, document } = await popupWithRows();
  try {
    const row = firstConnRow(document);
    row.dispatchEvent(pointer(window, 'pointerdown', { x: 10, y: 10 }));
    row.dispatchEvent(pointer(window, 'pointermove', { x: 14, y: 13 }));
    await window.__advance(3000);
    assert.equal(ctxOpen(document), true, 'nobody holds a thumb perfectly still');
  } finally {
    window.close();
  }
});

test('popup: a right-click does not also fire a hold three seconds later', async () => {
  const { window, document } = await popupWithRows();
  try {
    const row = firstConnRow(document);
    // The real sequence: pointerdown with the secondary button, then contextmenu.
    row.dispatchEvent(pointer(window, 'pointerdown', { button: 2 }));
    row.dispatchEvent(pointer(window, 'contextmenu'));
    assert.equal(ctxOpen(document), true, 'right-click still opens it immediately');
    assert.equal(ctxMenu(document).classList.contains('as-modal'), false,
      'and still pinned to the pointer, not as a modal');
    await window.__advance(5000);
    assert.equal(ctxMenu(document).classList.contains('as-modal'), false,
      'the hold must not re-open it as a modal on top');
  } finally {
    window.close();
  }
});

test('popup: tapping the backdrop dismisses the modal', async () => {
  const { window, document } = await popupWithRows();
  try {
    firstConnRow(document).dispatchEvent(pointer(window, 'pointerdown'));
    await window.__advance(3000);
    assert.equal(ctxOpen(document), true);

    document.querySelector('.ctx-backdrop').dispatchEvent(new window.Event('click', { bubbles: true }));
    assert.equal(ctxOpen(document), false);
    assert.equal(document.querySelector('.ctx-backdrop').style.display, 'none');
  } finally {
    window.close();
  }
});

test('popup: the held row copies what THAT row was showing', async () => {
  const { window, document } = await popupWithRows();
  try {
    firstConnRow(document).dispatchEvent(pointer(window, 'pointerdown'));
    await window.__advance(3000);
    const items = [...document.querySelectorAll('.ctx-menu-item')].map(i => i.textContent);
    assert.deepEqual(items, ['Copy connection info', 'Copy PID'],
      'the same two actions the right-click menu offers');
  } finally {
    window.close();
  }
});

// S5: a token that once worked and is now refused (4401) is not replaced by the
// background on the server's say-so. The popup is where the user decides: the action
// button becomes "Re-authorize" and sends adoptServerToken.
test('a rejected, adoptable token offers Re-authorize and clicking it sends adoptServerToken', async () => {
  const { window, document } = await initPopup({
    connectionStatus: {
      connected: false, phase: 'waiting', retryAttempts: 2,
      tokenRejected: true, tokenAdoptable: true,
    },
  });
  try {
    const sent = [];
    const orig = window.browser.runtime.sendMessage;
    window.browser.runtime.sendMessage = (msg, cb) => { sent.push(msg.action); return orig(msg, cb); };
    const button = document.getElementById('connAction');
    assert.equal(button.textContent, 'Re-authorize');
    assert.match(statusLine(document), /^Token rejected/);
    assert.doesNotMatch(statusLine(document), /Options/, 'Re-authorize is the way out here');
    assert.equal(statusClass(document), 'status bad');
    button.click();
    assert.deepEqual(sent, ['adoptServerToken']);
  } finally {
    window.close();
  }
});

test('a Re-authorize that failed says why on the status line', async () => {
  const { window, document } = await initPopup({
    connectionStatus: {
      connected: false, phase: 'waiting', retryAttempts: 2,
      tokenRejected: true, tokenAdoptable: true, adoptError: 'could not fetch a token: HTTP 404',
    },
  });
  try {
    assert.match(statusLine(document),
      /^Token rejected: .*\(re-authorize failed: could not fetch a token: HTTP 404\)$/);
    assert.equal(document.getElementById('connAction').textContent, 'Re-authorize', 'it can be tried again');
  } finally {
    window.close();
  }
});

test('a rejected token with nothing to adopt from says so but offers no adoption', async () => {
  const { window, document } = await initPopup({
    connectionStatus: {
      connected: false, phase: 'waiting', retryAttempts: 2,
      tokenRejected: true, tokenAdoptable: false,
    },
  });
  try {
    assert.match(statusLine(document), /^Token rejected/);
    assert.notEqual(document.getElementById('connAction').textContent, 'Re-authorize');
    // Reconnect alone loops on the same 4401; the line says where the way out is.
    assert.match(statusLine(document), /Set the server's MCP port or paste its token in Options\.$/);
  } finally {
    window.close();
  }
});

test('popup (Chromium): the proxy card is a consent switch, off until the user turns it on', async () => {
  const localStore = {};
  const { window, document, permissionRequests } =
    await initPopup({ engine: 'chromium', platformOs: 'win', localStore });
  const pill = () => document.getElementById('proxyAccessIndicator').textContent;
  const button = () => document.getElementById('proxyAccessGrant');
  try {
    assert.equal(pill(), 'Disabled');
    assert.equal(button().hidden, false, 'the switch is always on offer');
    assert.equal(button().textContent, 'Allow the browser-wide proxy');
    assert.equal(document.getElementById('proxyAccessChromiumScope').hidden, false,
      'the whole-browser warning is shown');
    assert.equal(document.getElementById('proxyAccessChromiumHelp').hidden, false);
    assert.equal(document.getElementById('proxyAccessEnableHelp').hidden, true,
      'there is no about:addons on Chrome');

    button().dispatchEvent(new window.Event('click'));
    await flush();
    await flush();
    assert.equal(localStore.allowBrowserProxy, true, 'the click writes the consent');
    assert.equal(permissionRequests.length, 0, 'Chrome cannot grant an optional proxy permission');
    assert.equal(pill(), 'Enabled');
    assert.equal(button().textContent, 'Turn off the browser-wide proxy');
    assert.equal(document.getElementById('proxyAccessChromiumHelp').hidden, true);

    button().dispatchEvent(new window.Event('click'));
    await flush();
    await flush();
    assert.equal(localStore.allowBrowserProxy, false, 'the same button turns it off');
    assert.equal(pill(), 'Disabled');
  } finally {
    window.close();
  }
});

test('popup (Chromium): a consent already given is shown as enabled on open', async () => {
  const { window, document } = await initPopup(
    { engine: 'chromium', platformOs: 'win', localStore: { allowBrowserProxy: true } });
  try {
    assert.equal(document.getElementById('proxyAccessIndicator').textContent, 'Enabled');
    assert.equal(document.getElementById('proxyAccessGrant').textContent,
      'Turn off the browser-wide proxy');
  } finally {
    window.close();
  }
});

test('popup (Firefox): the Chromium wording and switch stay out of the way', async () => {
  const { window, document } = await initPopup({ platformOs: 'win', proxyGranted: false });
  try {
    assert.equal(document.getElementById('proxyAccessChromiumScope').hidden, true);
    assert.equal(document.getElementById('proxyAccessChromiumHelp').hidden, true);
    assert.equal(document.getElementById('proxyAccessGrant').hidden, true);
  } finally {
    window.close();
  }
});
