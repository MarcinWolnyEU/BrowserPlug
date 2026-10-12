// Unit tests for extension-gecko/options.js settings round-trip (jsdom, no Firefox).
// Focused on the Kilo-session options; also covers the Anthropic provider settings.
// Run: node --test tests/js/options.test.mjs   (or: npm run test:js)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash, randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import { domContentLoaded } from './dom-ready.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OPTIONS_HTML = readFileSync(path.join(__dirname, '../../extension-gecko/options.html'), 'utf8');
const OPTIONS_JS = readFileSync(path.join(__dirname, '../../extension-gecko/options.js'), 'utf8');
// options.html loads it before options.js: it signs requests to the server's own port.
const WS_CRYPTO_JS = readFileSync(path.join(__dirname, '../../extension-gecko/ws-crypto.js'), 'utf8');
// options.html loads it too, before options.js: the Captured bodies card validates its
// patterns with the same module the background applies them with.
const BODY_CENSOR_JS = readFileSync(path.join(__dirname, '../../extension-gecko/body-censor.js'), 'utf8');
// ...and the card itself, which options.js only hooks.
const CENSOR_PANEL_JS = readFileSync(path.join(__dirname, '../../extension-gecko/censor-panel.js'), 'utf8');
// The Copilot card's sign-in wording; options.html loads it before options.js, and
// refreshCopilotAuth does nothing without it.
const COPILOT_VIEW_JS = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/copilot-view.js'), 'utf8');

// jsdom has no SubtleCrypto; lend the page Node's, then load ws-crypto.js the way the
// page's <script> tag would.
function loadWsCrypto(window) {
  if (!(window.crypto && window.crypto.subtle)) {
    Object.defineProperty(window, 'crypto', { value: globalThis.crypto, configurable: true });
  }
  window.eval(WS_CRYPTO_JS);
}

// Yield to the Node macrotask queue so the async DOMContentLoaded init and the
// fire-and-forget auto-save handlers finish before we assert.
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

// Minimal in-memory `browser.storage` area matching webextension semantics:
// get(defaults-object) returns each default overridden by any stored value.
function makeStorageArea() {
  const store = {};
  const area = {
    store,
    get(query) {
      if (query === null || query === undefined) return Promise.resolve({ ...store });
      if (typeof query === 'string') {
        return Promise.resolve(query in store ? { [query]: store[query] } : {});
      }
      if (Array.isArray(query)) {
        const out = {};
        for (const k of query) if (k in store) out[k] = store[k];
        return Promise.resolve(out);
      }
      // Object form: defaults merged with any stored overrides.
      const out = { ...query };
      for (const k of Object.keys(query)) if (k in store) out[k] = store[k];
      return Promise.resolve(out);
    },
    set(obj) {
      Object.assign(store, obj);
      return Promise.resolve();
    },
    remove(keys) {
      for (const k of [].concat(keys)) delete store[k];
      return Promise.resolve();
    },
    clear() {
      for (const k of Object.keys(store)) delete store[k];
      return Promise.resolve();
    },
  };
  return area;
}

function makeBrowserMock(opts = {}) {
  const sync = makeStorageArea();
  const local = makeStorageArea();
  const openPopupCalls = [];
  const tabsCreated = [];
  const browser = {
    browserAction: {
      openPopup: async () => {
        openPopupCalls.push(Date.now());
        if (opts.openPopupFails) throw new Error('refused');
      },
    },
    runtime: {
      lastError: null,
      getManifest: () => ({ version: '0.0.0-test' }),
      openOptionsPage: () => {},
      ...(opts.platformOs
        ? { getPlatformInfo: async () => ({ os: opts.platformOs, arch: 'arm64' }) }
        : {}),
      sendMessage: (msg, cb) => {
        const reply = { connected: false };
        if (typeof cb === 'function') cb(reply);
        return Promise.resolve(reply);
      },
    },
    storage: { sync, local },
    tabs: {
      query: () => Promise.resolve([]),
      create: async (info) => { tabsCreated.push(info); return { id: 1, ...info }; },
    },
  };
  browser.runtime.getURL = (path) => `moz-extension://test/${path}`;
  return { browser, sync, local, openPopupCalls, tabsCreated };
}

// Harness approach "A": load the real options.html DOM, install the browser
// mock, run the real options.js as global code in the jsdom realm (so its
// top-level functions + the DOMContentLoaded listener attach to `window`), then
// wait for jsdom's own DOMContentLoaded (dom-ready.mjs) and let async init settle.
// This exercises the page's own event wiring end-to-end — a true round-trip —
// rather than poking the functions directly.
//
// opts (all optional):
//   seed          — values pre-loaded into storage.sync before the page initialises
//   seedLocal     — the same for storage.local
//   sendMessage   — replaces browser.runtime.sendMessage (e.g. to report a live MCP port)
//   fetch         — replaces the default always-ok fetch stub; every requested URL is
//                   recorded in the returned `fetches` array either way
//   clampTimeouts — cap every window.setTimeout delay at this many ms, so code paths
//                   built on long real-time waits (the 6s fetch-abort guards, poll
//                   backoffs) can be driven to completion without waiting them out
async function initOptions(opts = {}) {
  // runScripts:"outside-only" binds window.eval/Function to the jsdom realm
  // (so eval'd code sees `document`/`window` globals) WITHOUT executing the
  // page's own <script> tags — we inject options.js ourselves, exactly once.
  const dom = new JSDOM(OPTIONS_HTML, { pretendToBeVisual: true, runScripts: 'outside-only' });
  const { window } = dom;
  const pageReady = domContentLoaded(window);
  if (opts.clampTimeouts) {
    const realSetTimeout = window.setTimeout.bind(window);
    window.setTimeout = (fn, ms, ...args) =>
      realSetTimeout(fn, Math.min(ms || 0, opts.clampTimeouts), ...args);
  }
  const { browser, sync, local, openPopupCalls, tabsCreated } = makeBrowserMock(opts);
  if (opts.seed) Object.assign(sync.store, opts.seed);
  if (opts.seedLocal) Object.assign(local.store, opts.seedLocal);
  if (opts.sendMessage) browser.runtime.sendMessage = opts.sendMessage;
  window.browser = browser;
  // Stub the Health-pane fetch (only invoked on the Health tab / user action).
  const fetches = [];
  window.fetch = async (url, init) => {
    fetches.push(String(url));
    if (opts.fetch) return opts.fetch(String(url), init);
    return { ok: true, json: async () => ({}), text: async () => '' };
  };
  window.confirm = () => true; // resetToDefaults() guards on confirm()
  loadWsCrypto(window);
  window.eval(COPILOT_VIEW_JS);
  if (!opts.noBodyCensor) window.eval(BODY_CENSOR_JS);
  window.eval(CENSOR_PANEL_JS);
  // Indirect eval → options.js runs in global scope; declarations become window
  // globals and `document.addEventListener('DOMContentLoaded', …)` registers.
  window.eval(OPTIONS_JS);
  await pageReady;
  await flush();
  await flush();
  return { dom, window, document: window.document, store: sync.store, local: local.store,
           fetches, openPopupCalls, tabsCreated };
}

// Flip a checkbox / set a value and fire the same `change` event the browser
// would, driving the auto-save handlers options.js wired up.
function fireChange(window, id) {
  window.document.getElementById(id).dispatchEvent(new window.Event('change'));
}
function setChecked(window, id, value) {
  window.document.getElementById(id).checked = value;
  fireChange(window, id);
}

test('options: Kilo checkboxes load checked from storage defaults', async () => {
  const { window } = await initOptions();
  try {
    assert.equal(window.document.getElementById('kiloLoadDetails').checked, true);
    assert.equal(window.document.getElementById('kiloClarifyNames').checked, true);
  } finally {
    window.close();
  }
});

test('options: Kilo sub-option follows the master toggle (disabled/enabled)', async () => {
  const { window } = await initOptions();
  try {
    const sub = window.document.getElementById('kiloClarifyNames');
    assert.equal(sub.disabled, false);           // master on by default → sub enabled

    setChecked(window, 'kiloLoadDetails', false); // uncheck master
    await flush();
    assert.equal(sub.disabled, true);            // sub greyed out

    setChecked(window, 'kiloLoadDetails', true);  // re-check master
    await flush();
    assert.equal(sub.disabled, false);           // sub enabled again
  } finally {
    window.close();
  }
});

test('options: Kilo master + sub toggles persist to storage.sync', async () => {
  const { window, store } = await initOptions();
  try {
    // Uncheck master → auto-saved false; the (still-checked) sub is written true.
    setChecked(window, 'kiloLoadDetails', false);
    await flush();
    assert.equal(store.kiloLoadDetails, false);
    assert.equal(store.kiloClarifyNames, true);

    // Re-enable master so the sub can be toggled, then flip the sub off.
    setChecked(window, 'kiloLoadDetails', true);
    await flush();
    setChecked(window, 'kiloClarifyNames', false);
    await flush();
    assert.equal(store.kiloLoadDetails, true);
    assert.equal(store.kiloClarifyNames, false);
  } finally {
    window.close();
  }
});

test('options: Anthropic provider settings persist and clamp on save', async () => {
  const { window, store } = await initOptions();
  try {
    const refresh = window.document.getElementById('anthropicUsageRefreshMinutes');
    refresh.value = '999';                            // above the 1–60 range
    setChecked(window, 'anthropicShowWeekly', true);  // fires saveProviderSettings
    await flush();
    assert.equal(store.anthropicShowWeekly, true);
    assert.equal(store.anthropicUsageRefreshMinutes, 60); // clamped to the max
    assert.equal(refresh.value, '60');                    // clamp reflected in the field
  } finally {
    window.close();
  }
});

test('options: the DeepSeek peak lead is its own setting, clamped like the Anthropic one', async () => {
  // Two providers, two leads: DeepSeek's peak doubles the BILL while Anthropic's slows
  // responses, so a lead that suits one need not suit the other. Saving one must not
  // move the other.
  const { window, store } = await initOptions();
  try {
    const lead = window.document.getElementById('deepseekPeakLeadMinutes');
    lead.value = '9000';                                  // above the 0–240 range
    lead.dispatchEvent(new window.Event('change', { bubbles: true }));
    await flush();
    assert.equal(store.deepseekPeakLeadMinutes, 240);
    assert.equal(lead.value, '240');
    assert.equal(store.anthropicPeakLeadMinutes, 0, 'the Anthropic lead is untouched');
  } finally {
    window.close();
  }
});

// --- Provider display modes -------------------------------------------------
// One control per provider card, three answers each: nothing at all, on the Usage tab
// only, or on the Usage tab and the client's row. The popup reads these same keys, so
// what this file is really pinning is the spelling of the values and the defaults.

const MODE_SELECTS = ['anthropicDisplayMode', 'openaiDisplayMode', 'copilotDisplayMode',
  'deepseekDisplayMode'];

function setSelect(window, id, value) {
  const el = window.document.getElementById(id);
  el.value = value;
  el.dispatchEvent(new window.Event('change', { bubbles: true }));
  return el;
}

test('options: every provider card offers the same three display modes', async () => {
  // Same three everywhere on purpose: a provider whose panel behaved differently from
  // its neighbour would need explaining on every card.
  const { window } = await initOptions();
  try {
    for (const id of MODE_SELECTS) {
      const el = window.document.getElementById(id);
      assert.ok(el, `${id} is missing from options.html`);
      assert.deepEqual(Array.from(el.options).map((o) => o.value),
        ['disabled', 'tab', 'client'], id);
    }
  } finally {
    window.close();
  }
});

test('options: Anthropic and Copilot default to the row, OpenAI and DeepSeek to off', async () => {
  // OpenAI needs a signed-in chatgpt.com profile, and a panel that can only say "not
  // signed in" is not a default worth having. DeepSeek's price clock is opt-in.
  const { window } = await initOptions();
  try {
    assert.equal(window.document.getElementById('anthropicDisplayMode').value, 'client');
    assert.equal(window.document.getElementById('deepseekDisplayMode').value, 'disabled');
    assert.equal(window.document.getElementById('openaiDisplayMode').value, 'disabled');
    assert.equal(window.document.getElementById('copilotDisplayMode').value, 'client');
  } finally {
    window.close();
  }
});

test('options: a display mode persists under the key the popup reads', async () => {
  const { window, store } = await initOptions();
  try {
    setSelect(window, 'anthropicDisplayMode', 'tab');
    await flush();
    assert.equal(store.anthropicDisplayMode, 'tab');
    assert.notEqual(store.deepseekDisplayMode, 'tab', 'one card does not move another');

    setSelect(window, 'openaiDisplayMode', 'client');
    await flush();
    assert.equal(store.openaiDisplayMode, 'client');
  } finally {
    window.close();
  }
});

test('options: turning a panel off hides the settings that only mean something while it is on', async () => {
  const { window } = await initOptions();
  try {
    const field = (id) => window.document.getElementById(id).closest('.field');
    const ids = ['anthropicBackgroundRefresh', 'anthropicPeakLeadMinutes',
      'anthropicUsageRefreshMinutes', 'anthropicShowWeekly'];
    for (const id of ids) assert.equal(field(id).hidden, false, `${id} shows while the panel is on`);

    setSelect(window, 'anthropicDisplayMode', 'disabled');
    await flush();
    for (const id of ids) {
      assert.equal(field(id).hidden, true, `${id}: label and help text go with the control`);
      assert.equal(window.document.getElementById(id).disabled, true, `${id} is inert too`);
    }
    // DeepSeek ships disabled, so its lead is hidden on load; it is a separate panel.
    assert.equal(field('deepseekPeakLeadMinutes').hidden, true);
    setSelect(window, 'deepseekDisplayMode', 'tab');
    await flush();
    assert.equal(field('deepseekPeakLeadMinutes').hidden, false);
    assert.equal(field('anthropicShowWeekly').hidden, true, 'and does not bring the Anthropic ones back');

    assert.equal(field('openaiShowWeekly').hidden, true, 'OpenAI ships disabled');
    setSelect(window, 'openaiDisplayMode', 'tab');
    await flush();
    assert.equal(field('openaiShowWeekly').hidden, false);

    setSelect(window, 'anthropicDisplayMode', 'tab');
    await flush();
    for (const id of ids) {
      assert.equal(field(id).hidden, false, `${id} is back when the panel is shown again`);
      assert.equal(window.document.getElementById(id).disabled, false);
    }
  } finally {
    window.close();
  }
});

test('options: the advanced connection settings sit on one line with no help text', async () => {
  const { window } = await initOptions();
  try {
    const doc = window.document;
    assert.ok([...doc.querySelectorAll('h2')].some((h) => h.textContent === 'Advanced connection settings'));
    assert.ok(![...doc.querySelectorAll('h2')].some((h) => /connection tuning/i.test(h.textContent)));
    const row = doc.getElementById('retryInterval').closest('.field-row');
    assert.deepEqual([...row.querySelectorAll('input')].map((i) => i.id),
      ['retryInterval', 'maxRetries', 'pingTimeout']);
    assert.equal(row.querySelectorAll('.help-text').length, 0);
  } finally {
    window.close();
  }
});

test('options: the background query box ships off and persists under the key the background reads', async () => {
  const { window, store } = await initOptions();
  try {
    for (const id of ['anthropicBackgroundRefresh', 'openaiBackgroundRefresh']) {
      assert.equal(window.document.getElementById(id).checked, false, `${id} ships unticked`);
    }
    assert.equal(window.document.getElementById('deepseekBackgroundRefresh'), null,
      'DeepSeek fetches nothing, so it has no box');

    const box = window.document.getElementById('anthropicBackgroundRefresh');
    box.checked = true;
    box.dispatchEvent(new window.Event('change', { bubbles: true }));
    await flush();
    assert.equal(store.anthropicBackgroundRefresh, true);
    assert.equal(store.openaiBackgroundRefresh, false, 'one card does not move another');
  } finally {
    window.close();
  }
});

test('options: the box label quotes the refresh rate as it is typed', async () => {
  const { window } = await initOptions({ seed: { anthropicUsageRefreshMinutes: 7 } });
  try {
    const label = () => window.document.querySelector('label[for="anthropicBackgroundRefresh"]')
      .textContent.replace(/\s+/g, ' ').trim();
    assert.equal(label(), 'Query every 7 min in background to update the usage progress bar', 'the stored value on load');

    const field = window.document.getElementById('anthropicUsageRefreshMinutes');
    const type = (v) => { field.value = v; field.dispatchEvent(new window.Event('input', { bubbles: true })); };
    type('15');
    assert.match(label(), /every 15 min/, 'every keystroke, not only on save');
    type('900');
    assert.match(label(), /every 60 min/, 'the value a save would store, not the raw text');
    type('');
    assert.match(label(), /every 5 min/);
    assert.match(window.document.querySelector('label[for="openaiBackgroundRefresh"]').textContent,
      /every 5 min/, 'each card quotes its own field');
    const copilotField = window.document.getElementById('copilotUsageRefreshMinutes');
    copilotField.value = '12';
    copilotField.dispatchEvent(new window.Event('input', { bubbles: true }));
    assert.match(window.document.querySelector('label[for="copilotBackgroundRefresh"]').textContent,
      /every 12 min/, 'the Copilot card is wired the same way');
  } finally {
    window.close();
  }
});

test('options: the Copilot card saves its box, rate and completions toggle, and greys them when off', async () => {
  const { window, store } = await initOptions();
  try {
    const $ = (id) => window.document.getElementById(id);
    for (const [id, v] of [['copilotBackgroundRefresh', true], ['copilotShowCompletions', true]]) {
      $(id).checked = v;
      $(id).dispatchEvent(new window.Event('change', { bubbles: true }));
    }
    $('copilotUsageRefreshMinutes').value = '9';
    $('copilotUsageRefreshMinutes').dispatchEvent(new window.Event('change', { bubbles: true }));
    await flush();
    assert.deepEqual([store.copilotBackgroundRefresh, store.copilotShowCompletions, store.copilotUsageRefreshMinutes],
      [true, true, 9]);

    setSelect(window, 'copilotDisplayMode', 'disabled');
    await flush();
    assert.equal(store.copilotDisplayMode, 'disabled');
    for (const id of ['copilotBackgroundRefresh', 'copilotUsageRefreshMinutes', 'copilotShowCompletions']) {
      assert.equal($(id).disabled, true, id);
    }
  } finally {
    window.close();
  }
});

test('options: a stored mode this build does not know falls back to the card default', async () => {
  // A value from a newer build, or a hand-edited profile. A select showing nothing is
  // worse than a select showing the shipped answer.
  // The popup falls back to the same defaults (popup.js PROVIDER_MODE_DEFAULTS), so both
  // pages draw the card the same way.
  const { window } = await initOptions({ seed: {
    anthropicDisplayMode: 'somewhere-else', copilotDisplayMode: 'always', openaiDisplayMode: 'x' } });
  try {
    const value = (id) => window.document.getElementById(id).value;
    assert.equal(value('anthropicDisplayMode'), 'client', 'Anthropic ships on the row');
    assert.equal(value('copilotDisplayMode'), 'client', 'Copilot ships on the row');
    assert.equal(value('openaiDisplayMode'), 'disabled', 'OpenAI ships off');
  } finally {
    window.close();
  }
});

test('options: saving another provider setting does not turn an unknown mode into Disabled', async () => {
  const { window, store } = await initOptions({ seed: { anthropicDisplayMode: 'somewhere-else' } });
  try {
    window.document.getElementById('openaiUsageRefreshMinutes').value = '7';
    fireChange(window, 'openaiUsageRefreshMinutes');
    await flush();
    assert.equal(store.openaiUsageRefreshMinutes, 7, 'the save ran');
    assert.equal(store.anthropicDisplayMode, 'client',
      'an unrelated save switched claude.ai usage off');
  } finally {
    window.close();
  }
});

// --- Connection token (remote servers) --------------------------------------
// The shared secret a server that accepts connections beyond localhost requires.
// background.js reads it straight from storage.sync (`connectionToken`, trimmed) and
// OMITS it from the hello when unset — so an empty field must leave the key ABSENT,
// never ''. It is also a secret: it must not ride along in the `configUpdated` config
// payload, which becomes the background's CONFIG (console-logged, handed to the popup).

// A token exactly as gen_ws_token.py would mint one — generated at runtime, never a
// literal (project rule: token values are never written down anywhere).
function mintToken() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return Buffer.from(bytes).toString('base64url');
}

// Submit the connection form the way the browser would.
function submitConnectionForm(window) {
  window.document.getElementById('optionsForm')
    .dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
}

test('options: token field is a password input and starts empty', async () => {
  const { window, store } = await initOptions();
  try {
    const field = window.document.getElementById('connectionToken');
    assert.ok(field, 'the options page must expose a connection-token field');
    assert.equal(field.type, 'password', 'a secret must not be shown in the clear');
    assert.equal(field.value, '');
    assert.ok(!('connectionToken' in store), 'no token stored on a fresh profile');
  } finally {
    window.close();
  }
});

test('options: token round-trips to storage.sync under the key background.js reads', async () => {
  const token = mintToken();
  const { window, store } = await initOptions();
  try {
    window.document.getElementById('connectionToken').value = token;
    submitConnectionForm(window);
    await flush();
    // The exact key getConnectionToken() reads from storage.sync.
    assert.equal(store.connectionToken, token);
  } finally {
    window.close();
  }
});

test('options: a saved token is loaded back into the field', async () => {
  const token = mintToken();
  const { window, store } = await initOptions();
  try {
    window.document.getElementById('connectionToken').value = token;
    submitConnectionForm(window);
    await flush();
    assert.equal(store.connectionToken, token);
  } finally {
    window.close();
  }

  // Re-open the page against the same storage → the field is repopulated.
  const dom2 = await initOptions();
  try {
    dom2.store.connectionToken = token; // seed the fresh mock store
    await dom2.window.loadSettings();
    assert.equal(dom2.window.document.getElementById('connectionToken').value, token);
  } finally {
    dom2.window.close();
  }
});

test('options: a blank token field leaves the key UNSET (never an empty string)', async () => {
  const { window, store } = await initOptions();
  try {
    // Save a token, then clear the field and save again.
    window.document.getElementById('connectionToken').value = mintToken();
    submitConnectionForm(window);
    await flush();
    assert.ok('connectionToken' in store);

    window.document.getElementById('connectionToken').value = '   '; // blank (whitespace)
    submitConnectionForm(window);
    await flush();
    assert.ok(!('connectionToken' in store),
      "an empty field must REMOVE the key — background.js must never send token:''");
  } finally {
    window.close();
  }
});

// A device token as the server mints one, generated at runtime (project rule: no token
// value is ever written down).
const mintDeviceToken = () =>
  `bp1.${randomBytes(8).toString('base64url')}.${randomBytes(32).toString('base64url')}`;

test('options: the token hint knows a device token from a pre-1.9.1 shared one', async () => {
  const device = mintDeviceToken();
  const ok = await initOptions({ seed: { connectionToken: device } });
  try {
    const hint = ok.document.getElementById('tokenHint');
    assert.equal(hint.className, 'field-note ok');
    assert.match(hint.textContent, /device token/);
  } finally {
    ok.window.close();
  }
  const legacy = await initOptions({ seed: { connectionToken: randomBytes(32).toString('base64url') } });
  try {
    const hint = legacy.document.getElementById('tokenHint');
    assert.equal(hint.className, 'field-note err');
    assert.match(hint.textContent, /older than 1\.9\.1/);
  } finally {
    legacy.window.close();
  }
});

test('options: Copy hands out a NEW device token, never this browser\'s own', async () => {
  const own = mintDeviceToken();
  const fresh = mintDeviceToken();
  const copied = [];
  const { window, document, fetches } = await initOptions({
    seed: { connectionToken: own },
    fetch: async (url) => url.endsWith('/ws-token')
      ? { ok: true, json: async () => ({ token: fresh }) }
      : { ok: true, json: async () => ({}), text: async () => '' },
  });
  Object.defineProperty(window.navigator, 'clipboard', {
    value: { writeText: async (t) => { copied.push(t); } }, configurable: true });
  try {
    document.getElementById('tokenCopy').click();
    for (let i = 0; i < 5 && !copied.length; i++) await flush();
    assert.deepEqual(copied, [fresh], 'the copied token is the fresh one');
    assert.ok(fetches.some(u => /^http:\/\/127\.0\.0\.1:\d+\/ws-token$/.test(u)),
      'fetched from the loopback token endpoint');
    assert.equal(document.getElementById('connectionToken').value, own,
      'this browser keeps its own token');
  } finally {
    window.close();
  }
});

test('options: the token is never handed to the background CONFIG', async () => {
  const token = mintToken();
  const dom = new JSDOM(OPTIONS_HTML, { pretendToBeVisual: true, runScripts: 'outside-only' });
  const { window } = dom;
  const pageReady = domContentLoaded(window);
  const { browser, sync } = makeBrowserMock();
  const messages = [];
  browser.runtime.sendMessage = (msg) => {
    messages.push(msg);
    return Promise.resolve({ connected: false });
  };
  window.browser = browser;
  window.fetch = async () => ({ ok: true, json: async () => ({}), text: async () => '' });
  window.eval(OPTIONS_JS);
  await pageReady;
  await flush();
  await flush();

  try {
    window.document.getElementById('connectionToken').value = token;
    submitConnectionForm(window);
    await flush();

    assert.equal(sync.store.connectionToken, token, 'stored…');
    const configUpdates = messages.filter((m) => m && m.type === 'configUpdated');
    assert.equal(configUpdates.length, 1, '…and the background is told to reconnect');
    assert.ok(!JSON.stringify(configUpdates[0]).includes(token),
      'the secret must not travel in the configUpdated payload (CONFIG is console-logged)');
  } finally {
    window.close();
  }
});

// --- MCP HTTP port and the connection status row -----------------------------
// Both ports are things you TYPE, so both live in the .conn-grid row (hostname, WebSocket
// port, WebSocket URL, HTTP port). The connection STATE they produce is read, not edited,
// so it sits in the field directly below — which is also where the one Refresh/Reconnect
// button lives. This is the reverse of the earlier arrangement (HTTP port below the row,
// status inside it), changed 2026-09-02 for readability.
//
// The port is still a CONNECTION setting, so it stays in General ▸ Server connection and
// out of Health, which keeps only diagnostics and actions. The configured value is a
// starting point: the server prefers 2830 but walks upward when that port is taken, and
// the extension learns the real one over the WebSocket (`serverMcpPort`).

test('options: both ports share the connection row, with the status below it', async () => {
  const { window, document } = await initOptions();
  try {
    const mcpPort = document.getElementById('mcpPort');
    const wsUrl = document.getElementById('wsUrl');
    assert.ok(mcpPort, 'the options page must expose an MCP HTTP port input');
    assert.equal(document.querySelectorAll('#mcpPort').length, 1, 'exactly one #mcpPort — no duplicate id');

    // It is a setting, so it belongs to the General pane…
    assert.equal(mcpPort.closest('section.pane').id, 'pane-general',
      'the MCP port is a connection setting, not a diagnostic — it belongs in General');
    // …in the same "Server connection" card as the WebSocket URL…
    assert.equal(mcpPort.closest('.card'), wsUrl.closest('.card'),
      'it must sit in the Server connection card, with the WebSocket URL');
    // …after it in document order…
    assert.ok(wsUrl.compareDocumentPosition(mcpPort) & window.Node.DOCUMENT_POSITION_FOLLOWING,
      'the MCP port must come AFTER the WebSocket URL field');
    // …and IN the row with it, not stacked underneath.
    const row = wsUrl.closest('.conn-grid');
    assert.ok(row, 'the WebSocket URL belongs in the .conn-grid row');
    assert.ok(row.contains(mcpPort), 'both ports share the connection row');

    // The connection status is the field directly below that row, and carries BOTH dots.
    const status = document.getElementById('connectionIndicator').closest('.field');
    assert.ok(!row.contains(status), 'the status is read, not typed — it sits below the row');
    const below = Array.from(mcpPort.closest('.card').querySelectorAll('.field'))
      .filter((f) => !row.contains(f));
    assert.equal(below.indexOf(status), 0,
      'the connection status must be the field DIRECTLY below the ports row');
    assert.ok(status.contains(document.getElementById('httpIndicator')),
      'the HTTP reachability dot belongs beside the WebSocket one');
    assert.ok(status.contains(document.getElementById('refreshStatus')),
      'the one Refresh/Reconnect button belongs with the state it acts on');

    // Its label travels with it (no orphan label left behind anywhere else).
    const labels = document.querySelectorAll('label[for="mcpPort"]');
    assert.equal(labels.length, 1);
    assert.equal(labels[0].closest('section.pane').id, 'pane-general');
  } finally {
    window.close();
  }
});

// --- the one control beside the status -------------------------------------
// It re-reads when the link is up and DIALS when it is not. Before 2026-09-02 it only ever
// re-read, so the single control next to a red light repainted the red — the state it was
// pressed to clear. Either way the same press re-tests the HTTP port, because the two dots
// fail independently and showing both would be theatre if one button refreshed one of them.

async function settle(ms) {
  await flush();
  await new Promise((resolve) => setTimeout(resolve, ms || 40));
  await flush();
}

test('options: a down socket turns the button into Reconnect, and pressing it dials', async () => {
  const sent = [];
  let connected = false;
  const { window, document } = await initOptions({
    clampTimeouts: 5,
    sendMessage: async (msg) => {
      sent.push(msg);
      if (msg && msg.action === 'forceReconnect') { connected = true; return { success: true }; }
      if (msg && msg.type === 'getConnectionStatus') {
        return { connected: connected, serverHealthy: connected };
      }
      return {};
    },
    fetch: async () => ({ ok: true, json: async () => ({ service: 'browserplug' }) })
  });
  try {
    const btn = document.getElementById('refreshStatus');
    assert.equal(btn.textContent, 'Reconnect', 'a down socket turns the button into a dial');
    assert.equal(sent.filter((m) => m && m.action === 'forceReconnect').length, 0,
      'the INITIAL render must never dial: the page opened itself, nobody pressed anything');

    btn.dispatchEvent(new window.Event('click'));
    await settle();

    assert.equal(sent.filter((m) => m && m.action === 'forceReconnect').length, 1,
      'pressing Reconnect must send exactly one forceReconnect');
    assert.equal(document.getElementById('connectionText').textContent, 'Connected');
    assert.equal(btn.textContent, 'Refresh', 'once connected it goes back to re-reading');
  } finally {
    window.close();
  }
});

test('options: a live socket keeps the button at Refresh and does NOT dial', async () => {
  const sent = [];
  const { window, document } = await initOptions({
    clampTimeouts: 5,
    sendMessage: async (msg) => {
      sent.push(msg);
      if (msg && msg.type === 'getConnectionStatus') {
        return { connected: true, serverHealthy: true };
      }
      return {};
    },
    fetch: async () => ({ ok: true, json: async () => ({ service: 'browserplug' }) })
  });
  try {
    const btn = document.getElementById('refreshStatus');
    assert.equal(btn.textContent, 'Refresh');
    btn.dispatchEvent(new window.Event('click'));
    await settle();
    assert.equal(sent.filter((m) => m && m.action === 'forceReconnect').length, 0,
      'a healthy link must not be torn down and redialled just to refresh it');
  } finally {
    window.close();
  }
});

test('options: the HTTP dot is its own verdict, not a detail of the WebSocket', async () => {
  // The case this exists for: a firewall rule covering the WS port and not the HTTP one,
  // which leaves a remote browser connected and unable to load this page's own data.
  const { window, document } = await initOptions({
    clampTimeouts: 5,
    sendMessage: async (msg) => {
      if (msg && msg.type === 'getConnectionStatus') {
        return { connected: true, serverHealthy: true };
      }
      return {};
    },
    fetch: async (url) => {
      if (url.indexOf('/health') !== -1) throw new Error('blocked by the firewall');
      return { ok: true, json: async () => ({}) };
    }
  });
  try {
    document.getElementById('refreshStatus').dispatchEvent(new window.Event('click'));
    await settle();
    assert.equal(document.getElementById('connectionText').textContent, 'Connected');
    assert.equal(document.getElementById('httpText').textContent, 'Unreachable',
      'the HTTP port must be reported on its own evidence');
    assert.ok(document.getElementById('httpIndicator').classList.contains('disconnected'));
  } finally {
    window.close();
  }
});

// --- the button dials what was SAVED ----------------------------------------------------
// The failure this guards: the HTTP dot probes the value in the hostname BOX, while the
// socket dials the value in storage.sync. Type a new host, press Reconnect without
// saving, and the page reads "Reachable" next to "Disconnected" with nothing on screen
// to explain it. That cost a real debugging session on a phone.

test('options: pressing Reconnect with an unsaved hostname says so', async () => {
  const { window, document } = await initOptions({ seed: { hostname: 'localhost', port: 8772 } });
  try {
    const note = document.getElementById('connDirtyNote');
    assert.equal(note.hidden, true, 'an untouched form is not scolded');

    document.getElementById('hostname').value = '192.168.0.114';
    document.getElementById('refreshStatus').dispatchEvent(new window.Event('click'));
    await settle();

    assert.equal(note.hidden, false);
    assert.match(note.textContent, /hostname/);
    assert.match(note.textContent, /Save/, 'it must name the action that fixes it');
  } finally {
    window.close();
  }
});

test('options: the warning names every unsaved field, not just the first', async () => {
  const { window, document } = await initOptions({ seed: { hostname: 'localhost', port: 8772 } });
  try {
    document.getElementById('hostname').value = '192.168.0.114';
    document.getElementById('port').value = '9999';
    document.getElementById('refreshStatus').dispatchEvent(new window.Event('click'));
    await settle();
    const text = document.getElementById('connDirtyNote').textContent;
    assert.match(text, /hostname/);
    assert.match(text, /port/);
  } finally {
    window.close();
  }
});

test('options: a form matching what is stored draws no warning', async () => {
  const { window, document } = await initOptions({ seed: { hostname: '10.0.0.5', port: 8772 } });
  try {
    // loadSettings filled the fields from the same store, so nothing is unsaved.
    assert.equal(document.getElementById('hostname').value, '10.0.0.5');
    document.getElementById('refreshStatus').dispatchEvent(new window.Event('click'));
    await settle();
    assert.equal(document.getElementById('connDirtyNote').hidden, true);
  } finally {
    window.close();
  }
});

test('options: saving clears the warning', async () => {
  const { window, document } = await initOptions({ seed: { hostname: 'localhost', port: 8772 } });
  try {
    document.getElementById('hostname').value = '192.168.0.114';
    document.getElementById('refreshStatus').dispatchEvent(new window.Event('click'));
    await settle();
    assert.equal(document.getElementById('connDirtyNote').hidden, false);

    document.getElementById('optionsForm').dispatchEvent(new window.Event('submit'));
    await settle();
    assert.equal(document.getElementById('connDirtyNote').hidden, true,
      'the note must not outlive the condition it describes');
  } finally {
    window.close();
  }
});

// --- Android: one surface, and a way to open it ----------------------------------------
// Firefox for Android has exactly one place the interface can appear, and it is the
// popup — tapping the extension covers the page with it. So the picker shows one card
// there, named and drawn for what a phone actually does, and the four desktop surfaces
// (a detached window, a docked sidebar, a tab of its own) disappear: none of them exists
// on a phone. The platform comes from getPlatformInfo, never from a build flag, because
// options.html is a shared file and the chromium tree would inherit anything baked in.

test('options: the Overlay card stores the same surface value as Popup', async () => {
  // storage.sync carries uiSurface to the user's DESKTOP Firefox. If the phone wrote a
  // value of its own, the desktop would read a surface it has no card for.
  const { window, document } = await initOptions();
  try {
    const overlay = document.querySelector('.surface-card.mobile-only');
    assert.ok(overlay, 'there must be a mobile-only surface card');
    assert.equal(overlay.dataset.surface, 'popup',
      'Overlay IS the popup; a new stored value would not survive syncing to desktop');
    assert.equal(overlay.querySelector('input').value, 'popup');
    assert.equal(overlay.querySelector('.surface-name').textContent, 'Overlay');
  } finally {
    window.close();
  }
});

test('options: an Android page stamps the platform; a desktop page does not', async () => {
  const mobile = await initOptions({ platformOs: 'android' });
  try {
    assert.equal(mobile.document.body.dataset.platform, 'android');
  } finally {
    mobile.window.close();
  }

  const desktop = await initOptions({ platformOs: 'win' });
  try {
    assert.equal(desktop.document.body.dataset.platform, undefined,
      'only Android is special-cased; every other OS keeps the four desktop surfaces');
  } finally {
    desktop.window.close();
  }
});

test('options: a build with no getPlatformInfo falls back to the desktop layout', async () => {
  // The safe direction: a phone showing four surfaces is untidy, a desktop showing only
  // Overlay would have silently lost three working options.
  const { window, document } = await initOptions();
  try {
    assert.equal(document.body.dataset.platform, undefined);
  } finally {
    window.close();
  }
});

test('options: on ANDROID the overlay button never calls openPopup — Fenix ignores it', async () => {
  // Bugzilla 1817809: GeckoView implements openPopup, Fenix never hooked it up, and the
  // call resolves without opening anything OR throwing (Bugzilla 2045535 from the other
  // end). So there is no failure to catch and report — calling it is silence, which is
  // exactly what the user saw. MDN claims firefox_android 79 for it; that is wrong.
  const { window, document, openPopupCalls, tabsCreated } =
    await initOptions({ platformOs: 'android' });
  try {
    const btn = document.getElementById('openOverlay');
    assert.ok(btn, 'the button must exist');
    assert.equal(openPopupCalls.length, 0, 'nothing may open the overlay on load');
    btn.dispatchEvent(new window.Event('click'));
    await flush();
    await flush();
    assert.equal(openPopupCalls.length, 0,
      'openPopup does nothing on Fenix; calling it is how this button silently failed');
    assert.equal(tabsCreated.length, 1, 'the interface opens as a tab instead');
    assert.match(tabsCreated[0].url, /popup\/popup\.html$/);
  } finally {
    window.close();
  }
});

test('options: on DESKTOP the overlay button still uses openPopup', async () => {
  const { window, document, openPopupCalls, tabsCreated } =
    await initOptions({ platformOs: 'win' });
  try {
    document.getElementById('openOverlay').dispatchEvent(new window.Event('click'));
    await flush();
    await flush();
    assert.equal(openPopupCalls.length, 1, 'the real anchored panel, where it works');
    assert.equal(tabsCreated.length, 0, 'and no stray tab');
  } finally {
    window.close();
  }
});

test('options: a refused openPopup tells the user where to go instead', async () => {
  const { window, document } = await initOptions({ platformOs: 'win', openPopupFails: true });
  try {
    document.getElementById('openOverlay').dispatchEvent(new window.Event('click'));
    await flush();
    await flush();
    const note = document.getElementById('openOverlayNote');
    assert.equal(note.hidden, false);
    assert.match(note.textContent, /browser menu/);
  } finally {
    window.close();
  }
});

test('options: ANDROID shows Overlay selected even when the SYNCED surface is a desktop one', async () => {
  // uiSurface rides storage.sync, so a phone inherits the desktop's pick. 'tab' has no
  // card on Android, which left the picker with nothing selected and the background
  // clearing the popup — the phone lost its only interface.
  const { window, document } =
    await initOptions({ platformOs: 'android', seed: { uiSurface: 'tab' } });
  try {
    const overlay = document.querySelector('.surface-card.mobile-only[data-surface="popup"]');
    assert.ok(overlay, 'the Overlay card must exist');
    assert.ok(overlay.classList.contains('selected'), 'Overlay is the only surface a phone has');
    assert.equal(overlay.querySelector('input').checked, true);
  } finally {
    window.close();
  }
});

test('options: a DESKTOP surface choice is still honoured', async () => {
  const { window, document } = await initOptions({ platformOs: 'win', seed: { uiSurface: 'tab' } });
  try {
    const tab = document.querySelector('.surface-card.desktop-only[data-surface="tab"]');
    assert.ok(tab.classList.contains('selected'), 'the stored choice wins on desktop');
  } finally {
    window.close();
  }
});

test('options: Refresh and Restart stay in the Health section', async () => {
  const { window, document } = await initOptions();
  try {
    assert.equal(document.getElementById('healthRefresh').closest('section.pane').id, 'pane-health',
      'Refresh is a diagnostic action — it stays in Health');
    assert.equal(document.getElementById('healthRestart').closest('section.pane').id, 'pane-health',
      'Restart is a diagnostic action — it stays in Health');
    // The port input (and its label) must NOT have been left behind in Health.
    assert.equal(document.querySelector('#pane-health #mcpPort'), null);
    assert.equal(document.querySelector('#pane-health label[for="mcpPort"]'), null);
    // The Health pane still shows which URL it polls.
    assert.ok(document.querySelector('#pane-health #healthUrlDisplay'));
  } finally {
    window.close();
  }
});

test('options: the MCP port round-trips to storage.sync and rejects garbage', async () => {
  const { window, document, store } = await initOptions();
  try {
    const input = document.getElementById('mcpPort');
    assert.equal(input.value, '2830', 'a fresh profile starts on the server\'s preferred port');

    input.value = '2831';                 // the server walked up one
    fireChange(window, 'mcpPort');
    await flush();
    assert.equal(store.mcpPort, 2831, 'the port persists under the key loadSettings() reads');

    // Garbage: a number input sanitizes non-numeric text away, so this reaches the
    // isNaN guard in saveMcpPort() — which must refuse it, not store NaN.
    input.value = 'not-a-port';
    fireChange(window, 'mcpPort');
    await flush();
    assert.equal(store.mcpPort, 2831, 'garbage must not overwrite the saved port');
    assert.ok(document.getElementById('status').className.includes('error'),
      'and the user must be told it was rejected');

    // Out of range is refused the same way.
    input.value = '70000';
    fireChange(window, 'mcpPort');
    await flush();
    assert.equal(store.mcpPort, 2831, 'a port above 65535 must not be saved');
  } finally {
    window.close();
  }
});

// --- Numeric validation: NaN must never persist ------------------------------
// An emptied (or garbage-filled) number input reads back as '' → parseInt gives NaN,
// and NaN fails every range comparison — so without an explicit isNaN guard it slips
// through "v < min || v > max" validation and gets SAVED. For retryInterval that is
// not cosmetic: background.js runs setTimeout(reconnect, CONFIG.retryInterval), and
// setTimeout treats NaN as 0ms — a reconnect-hammering loop that survives browser
// restarts because the value is loaded back from storage on startup.

test('options: the WebSocket port refuses a non-numeric value (no NaN saved)', async () => {
  const { window, document, store } = await initOptions();
  try {
    document.getElementById('port').value = '';   // what a number input yields for garbage
    submitConnectionForm(window);
    await flush();
    assert.ok(!('port' in store),
      'a NaN port must never be written — the background would dial ws://host:NaN');
    assert.ok(document.getElementById('status').className.includes('error'),
      'and the user must be told it was rejected');

    document.getElementById('port').value = '8773';
    submitConnectionForm(window);
    await flush();
    assert.equal(store.port, 8773, 'a valid port still saves after the rejection');
  } finally {
    window.close();
  }
});

test('options: Advanced settings refuse non-numeric values (NaN would reach setTimeout as 0ms)', async () => {
  const { window, document, store } = await initOptions();
  const clickSave = async () => { document.getElementById('saveAdvanced').click(); await flush(); };
  try {
    document.getElementById('retryInterval').value = '';
    await clickSave();
    assert.ok(!('retryInterval' in store),
      'NaN retryInterval must be refused: setTimeout(reconnect, NaN) fires at 0ms and hammers the server');
    assert.ok(document.getElementById('status').className.includes('error'));

    document.getElementById('retryInterval').value = '2000';
    document.getElementById('maxRetries').value = '';
    await clickSave();
    assert.ok(!('maxRetries' in store) && !('retryInterval' in store),
      'a rejected save must persist nothing — not even the fields that were valid');

    document.getElementById('maxRetries').value = '3';
    document.getElementById('pingTimeout').value = '';
    await clickSave();
    assert.ok(!('pingTimeout' in store));

    document.getElementById('pingTimeout').value = '4000';
    await clickSave();
    assert.equal(store.retryInterval, 2000);
    assert.equal(store.maxRetries, 3);
    assert.equal(store.pingTimeout, 4000);
  } finally {
    window.close();
  }
});

// --- Post-restart health poll must not wedge ---------------------------------
// After Restart the page polls /health until the replacement server answers. A
// half-up server can accept the TCP connection and never respond; a fetch with no
// abort guard then never settles, the poll loop never advances, and the restart
// spinner spins forever. The fetch stub below settles ONLY via AbortSignal — with
// the guard the poll completes, without it the loop hangs on attempt one.

test('options: the post-restart health poll cannot wedge on a fetch that never settles', async () => {
  const pollSignals = [];
  const { window } = await initOptions({
    clampTimeouts: 25,   // the 6s abort guard fires in 25ms so the test stays fast
    fetch: (url, init) => new Promise((resolve, reject) => {
      pollSignals.push(Boolean(init && init.signal));
      if (init && init.signal) {
        const abort = () => { const e = new Error('aborted'); e.name = 'AbortError'; reject(e); };
        if (init.signal.aborted) abort();
        else init.signal.addEventListener('abort', abort);
      }
      // No signal → this promise never settles: exactly the wedge the guard is for.
    }),
  });
  try {
    let settled = false;
    const poll = window.pollHealthUntilBack(2, 1).then(() => { settled = true; });
    await Promise.race([poll, new Promise((r) => setTimeout(r, 2000))]);
    assert.equal(settled, true,
      'pollHealthUntilBack must settle even when /health never answers — otherwise the restart spinner spins forever');
    assert.ok(pollSignals.length >= 2 && pollSignals.every(Boolean),
      'every poll fetch must carry an AbortSignal so it can be timed out');
  } finally {
    window.close();
  }
});

// --- The restart wait must not declare victory against the dying server ------
// The server no longer exits 0.3s after acking a restart: it drains in-flight tool
// calls, closes the browser sockets and stops its HTTP listener first, and answers
// /health perfectly well for that whole teardown. So "ok" alone proves nothing —
// only a DIFFERENT server_pid does. The old poll took the first ok response and got
// away with it only because its first attempt came 1.5s after a 0.3s exit.

test('options: the restart wait ignores the outgoing server and waits for a new pid', async () => {
  const seen = [];
  const { window } = await initOptions({
    clampTimeouts: 5,
    fetch: (url) => {
      if (!url.includes('/health')) return { ok: true, json: async () => ({}) };
      seen.push(1);
      // The dying server keeps answering for the first few polls, then the
      // replacement binds and reports its own pid.
      const pid = seen.length < 3 ? 4242 : 5150;
      return { ok: true, json: async () => ({ status: 'ok', server_pid: pid }) };
    },
  });
  try {
    await window.pollHealthUntilBack(20, 1, 4242);
    assert.ok(seen.length >= 3,
      'the poll must keep going while /health still reports the pid that was told to restart');
    // The rendered report is the observable: renderHealth prints the pid it accepted.
    assert.match(window.document.getElementById('healthOutput').textContent, /5150/,
      'it must settle on the replacement, not on the server that is still tearing down');
  } finally {
    window.close();
  }
});

test('options: a restart that never completes is reported as a failure, not a refresh', async () => {
  // Falling back to a plain refresh was the old ending, and a refresh that happens to
  // reach the OLD server paints a healthy report for a restart that never happened.
  const { window, document } = await initOptions({
    clampTimeouts: 5,
    fetch: (url) => {
      if (!url.includes('/health')) return { ok: true, json: async () => ({}) };
      return { ok: true, json: async () => ({ status: 'ok', server_pid: 4242 }) };
    },
  });
  try {
    await window.pollHealthUntilBack(3, 1, 4242);
    const text = document.getElementById('healthOutput').textContent;
    assert.match(text, /did not|No replacement/i,
      `a wait that ran out must SAY so; got: ${text.slice(0, 200)}`);
  } finally {
    window.close();
  }
});

// --- Test configuration overrides must not leak into the real config ---------
// The test harness seeds storage.sync with testHostname/testPort, and background.js
// dials `test* || real` while they are set. loadSettings mirrors that: the form
// DISPLAYS the override values. Saving the form untouched must NOT promote those
// temporary values into the real hostname/port keys (same provenance rule as the
// auto-fetched token vs. autoTokenValue). And while overrides are active no config
// message may be sent at all: background updateConfig() persists its running CONFIG
// — whose hostname/port ARE the override values — back into the real keys on every
// config message, which would clobber the preserved values from the outside.

test('options: Save does not promote an untouched test override into the real config', async () => {
  const messages = [];
  const { window, document, store } = await initOptions({
    seed: { hostname: 'realhost', port: 1234, testHostname: 'testhost', testPort: 9999 },
    sendMessage: async (msg) => { messages.push(msg); return { connected: false }; },
  });
  try {
    // The form shows the override — the address the extension is actually dialing…
    assert.equal(document.getElementById('hostname').value, 'testhost');
    assert.equal(document.getElementById('port').value, '9999');

    submitConnectionForm(window);
    await flush();
    await flush();

    // …but Save must keep the REAL config: the user never typed these values.
    assert.equal(store.hostname, 'realhost', 'the real hostname must survive an untouched Save');
    assert.equal(store.port, 1234, 'the real port must survive an untouched Save');
    assert.equal(store.testHostname, 'testhost', 'the override itself stays in force');
    assert.equal(store.testPort, 9999);
    assert.ok(!messages.some((m) => m && (m.type === 'configUpdated' || m.type === 'advancedConfigUpdated')),
      'no config message while overrides are active — the background would persist its override CONFIG over the real keys');
    assert.match(document.getElementById('status').textContent, /overrides/i,
      'Save must say the overrides are active rather than reporting a plain success');
  } finally {
    window.close();
  }
});

test('options: a value the user actually typed still saves while an override is active', async () => {
  const { window, document, store } = await initOptions({
    seed: { hostname: 'realhost', port: 1234, testHostname: 'testhost', testPort: 9999 },
  });
  try {
    document.getElementById('port').value = '4321';   // explicit intent beats the override rule
    submitConnectionForm(window);
    await flush();
    await flush();
    assert.equal(store.port, 4321, 'a typed port is the user\'s real intent — it saves');
    assert.equal(store.hostname, 'realhost', 'the untouched override hostname is still not promoted');
    assert.equal(store.testPort, 9999, 'the override keys themselves are untouched either way');
    assert.equal(store.testHostname, 'testhost');
  } finally {
    window.close();
  }
});

test('options: Advanced save sends no config message while test overrides are active', async () => {
  const messages = [];
  const { window, document, store } = await initOptions({
    seed: { hostname: 'realhost', port: 1234, testPort: 9999 },
    sendMessage: async (msg) => { messages.push(msg); return { connected: false }; },
  });
  try {
    document.getElementById('retryInterval').value = '2000';
    document.getElementById('saveAdvanced').click();
    await flush();
    assert.equal(store.retryInterval, 2000, 'the advanced values themselves still save');
    assert.equal(store.hostname, 'realhost', 'and the preserved real hostname/port keys are kept');
    assert.equal(store.port, 1234);
    assert.ok(!messages.some((m) => m && m.type === 'advancedConfigUpdated'),
      'no advancedConfigUpdated while overrides are active — background updateConfig() would '
      + 'persist its override hostname/port back over the real keys');
  } finally {
    window.close();
  }
});

test('options: Health uses the port the server REPORTS, not a stale configured one', async () => {
  const { window, document, fetches } = await initOptions({
    seed: { mcpPort: 2830 },              // what the user configured (now stale)
    sendMessage: async (msg) => (msg && msg.type === 'getConnectionStatus')
      ? { connected: true, serverHealthy: true, serverMcpPort: 2833 }  // where it really landed
      : { success: true },
  });
  try {
    assert.equal(document.getElementById('mcpPort').value, '2830', 'the configured value is kept as typed');

    await window.refreshHealth();
    await flush();

    const health = fetches.filter((u) => u.endsWith('/health'));
    assert.ok(health.length, 'the Health pane must have probed something');
    assert.equal(health[health.length - 1], 'http://localhost:2833/health',
      'it must probe the port the server reports, not the stale 2830');

    // …and the page says so, rather than letting the field read as the truth.
    assert.equal(document.getElementById('healthUrlDisplay').textContent, 'http://localhost:2833/health');
    assert.equal(document.getElementById('restartUrlDisplay').textContent, 'http://localhost:2833/restart');
    assert.match(document.getElementById('mcpPortLive').textContent, /2833/);
  } finally {
    window.close();
  }
});

// --- Network mode: the Local / LAN / WAN panels -------------------------------
// Three VIEWS over one server-side fact (/health's ws_mode). Picking a panel
// persists nothing; the Set button POSTs /ws-bind (token in a custom header) and
// then waits for the RESTARTED server — mode match alone is not enough, because
// the old process reports the new mode in the moment before it exits.

function fakeHealth(overrides = {}) {
  return {
    status: 'ok', service: 'browserplug',
    server_pid: 111,
    ws_port: 8772,
    ws_host: '0.0.0.0',
    ws_host_source: 'file',
    ws_host_env: 'BROWSERPLUG_WS_HOST',
    ws_accepts_remote: true,
    ws_mode: 'lan',
    ws_lan_urls: ['ws://192.168.1.42:8772', 'ws://172.23.0.5:8772'],
    ws_lan_addresses: [
      { ip: '192.168.1.42', interface: 'Ethernet', recommended: true },
      { ip: '172.23.0.5', interface: 'vEthernet (WSL)', recommended: false },
    ],
    ...overrides,
  };
}

const jsonResponse = (obj) => ({ ok: true, json: async () => obj, text: async () => '' });

// Bounded real-time poll — the async flows under test span several (clamped) timers,
// so a fixed number of flush() calls is a bet; this waits for the actual edge.
async function waitFor(predicate, what, timeout = 2000) {
  const deadline = Date.now() + timeout;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 10));
  }
}

function selectNetMode(window, mode) {
  const radio = window.document.querySelector(`#netModeOptions input[value="${mode}"]`);
  radio.checked = true;
  radio.dispatchEvent(new window.Event('change', { bubbles: true }));
}

test('options: network mode auto-selects the panel matching the server\'s mode', async () => {
  const { window, document } = await initOptions({
    fetch: (url) => url.includes('/health') ? jsonResponse(fakeHealth({ ws_mode: 'lan' }))
                                            : jsonResponse({}),
  });
  try {
    await waitFor(() => document.querySelector('.surface-card[data-mode="lan"].selected'),
      'the LAN card to be selected');
    assert.equal(document.getElementById('netPanelLan').hidden, false);
    assert.equal(document.getElementById('netPanelLocal').hidden, true);
    assert.equal(document.getElementById('netPanelWan').hidden, true);
    assert.match(document.getElementById('netModeState').textContent, /configured as: LAN/);
    assert.equal(document.getElementById('netModeApply').hidden, true,
      'viewed mode == server mode -> nothing to set');
    assert.equal(document.getElementById('wsCleartextWarning').hidden, false,
      'the cleartext warning shows on every non-local panel');
  } finally {
    window.close();
  }
});

test('options: clicking a panel switches the view and persists NOTHING', async () => {
  const { window, document, store } = await initOptions({
    fetch: (url) => url.includes('/health') ? jsonResponse(fakeHealth({ ws_mode: 'local', ws_host: 'localhost', ws_accepts_remote: false }))
                                            : jsonResponse({}),
  });
  try {
    await waitFor(() => document.querySelector('.surface-card[data-mode="local"].selected'),
      'the Local card to be selected');
    const keysBefore = Object.keys(store).sort().join();

    selectNetMode(window, 'wan');
    await flush();

    assert.equal(document.getElementById('netPanelWan').hidden, false);
    assert.equal(document.getElementById('netPanelLocal').hidden, true);
    const btn = document.getElementById('netModeApply');
    assert.equal(btn.hidden, false, 'server is Local, viewing WAN -> the Set button appears');
    assert.equal(btn.textContent, 'Set to WAN (online) & Restart');
    assert.equal(Object.keys(store).sort().join(), keysBefore,
      'panel selection is a view — storage must be untouched');
  } finally {
    window.close();
  }
});

test('options: Set & Restart posts /ws-bind with the mode and only believes a NEW pid', async () => {
  const wsBinds = [];
  const servedPostBindPids = [];
  let postBindHealth = [];
  const { window, document } = await initOptions({
    clampTimeouts: 5,   // the 500ms restart-wait beats and abort guards run in 5ms
    seed: { connectionToken: 'x'.repeat(43) },
    fetch: (url, init) => {
      if (url.includes('/ws-bind')) {
        wsBinds.push({ headers: (init && init.headers) || {}, body: JSON.parse(init.body), raw: init.body });
        // The restart begins: from here /health walks the scripted sequence.
        postBindHealth = [
          fakeHealth({ ws_mode: 'wan', server_pid: 111 }),  // OLD process, new mode — a trap
          fakeHealth({ ws_mode: 'wan', server_pid: 222 }),  // the actual replacement
        ];
        return jsonResponse({ status: 'restarting', ws_mode: 'wan' });
      }
      if (url.includes('/health')) {
        if (wsBinds.length === 0) {
          return jsonResponse(fakeHealth({ ws_mode: 'local', ws_host: 'localhost', ws_accepts_remote: false }));
        }
        const h = postBindHealth.length > 1 ? postBindHealth.shift() : postBindHealth[0];
        servedPostBindPids.push(h.server_pid);
        return jsonResponse(h);
      }
      return jsonResponse({});
    },
  });
  try {
    await waitFor(() => document.querySelector('.surface-card[data-mode="local"].selected'),
      'the Local card to be selected');
    selectNetMode(window, 'wan');
    await flush();

    document.getElementById('netModeApply').click();
    await waitFor(() => document.getElementById('netModeApply').hidden,
      'the Set button to disappear once the replacement reports WAN');

    assert.equal(wsBinds.length, 1);
    assert.deepEqual(wsBinds[0].body, { host: '0.0.0.0', mode: 'wan' });
    // A custom header is what makes the endpoint page-proof (it forces a CORS
    // preflight), and a signature is what keeps the token itself off the network.
    // v2 also signs a hash of the body, so the body cannot be swapped in transit (S7).
    assert.match(wsBinds[0].headers['x-browserplug-auth'], /^v2 [A-Za-z0-9_-]+ \d+ [A-Za-z0-9_-]{22} [A-Za-z0-9_-]{43} [A-Za-z0-9_-]{43}$/);
    assert.equal(wsBinds[0].headers['x-browserplug-auth'].split(' ')[4],
      createHash('sha256').update(wsBinds[0].raw).digest('base64url'),
      'the header names the exact bytes that were sent');
    assert.equal(JSON.stringify(wsBinds[0].headers).includes('x'.repeat(43)), false,
      'the raw token is never sent');
    // The pin that matters: success must have come from the pid-222 poll, with the
    // pid-111/new-mode trap served AND rejected first. (Counting leftovers is not
    // enough — a mode-only check would also consume exactly one entry.)
    assert.deepEqual(servedPostBindPids, [111, 222],
      'the old-pid/new-mode answer must be polled first and rejected; a mode-only '
      + 'check would stop at 111 and never poll again');
    assert.match(document.getElementById('netModeState').textContent, /configured as: WAN/);
    assert.match(document.getElementById('status').textContent, /back up as WAN/);
  } finally {
    window.close();
  }
});

test('options: an env-sourced bind disables the Set flow and shows the env value', async () => {
  const { window, document } = await initOptions({
    fetch: (url) => url.includes('/health')
      ? jsonResponse(fakeHealth({ ws_host_source: 'env', ws_host: '192.168.7.7', ws_mode: 'lan' }))
      : jsonResponse({}),
  });
  try {
    await waitFor(() => document.querySelector('.surface-card[data-mode="lan"].selected'),
      'the LAN card to be selected');
    const bindInput = document.getElementById('lanBindHost');
    assert.equal(bindInput.disabled, true, 'the env owns the bind — the input is locked');
    assert.equal(bindInput.value, '192.168.7.7', 'and it SHOWS the env value, not a dead default');
    assert.match(document.getElementById('netModeState').textContent, /BROWSERPLUG_WS_HOST/);
    assert.equal(document.getElementById('netModeApply').hidden, true);

    selectNetMode(window, 'wan');
    await flush();
    assert.equal(document.getElementById('netModeApply').hidden, true,
      'no panel offers a Set button while the env pins the bind');
    assert.equal(document.getElementById('wanBindHost').disabled, true);
  } finally {
    window.close();
  }
});

test('options: a remote browser (no /health here) gets the degraded note on every panel', async () => {
  const { window, document } = await initOptions({
    fetch: (url) => {
      if (url.includes('/health')) throw new Error('connection refused');
      return jsonResponse({});
    },
  });
  try {
    await waitFor(() => !document.getElementById('netModeState').hidden, 'the status note');
    assert.match(document.getElementById('netModeState').textContent, /these steps are for a browser/);
    assert.ok(document.querySelector('.surface-card[data-mode="lan"].selected'),
      'no server here -> LAN is the likeliest reason this section is open');
    assert.equal(document.getElementById('netModeApply').hidden, true);
    assert.match(document.getElementById('lanAddrList').textContent, /no server here/);

    selectNetMode(window, 'wan');
    await flush();
    assert.match(document.getElementById('netModeState').textContent, /these steps are for a browser/,
      'switching panels keeps the degraded note — the fact does not change with the view');

    // And no auto-detection: this browser's own public IP would be the WRONG machine's.
    await waitFor(() => /server machine/.test(document.getElementById('wanPublicAddr').placeholder),
      'the manual-entry placeholder');
    assert.equal(document.getElementById('wanPublicAddr').value, '');
  } finally {
    window.close();
  }
});

test('options: LAN addresses render adapter-named, recommended (192.168.*) first', async () => {
  const { window, document } = await initOptions({
    fetch: (url) => url.includes('/health') ? jsonResponse(fakeHealth()) : jsonResponse({}),
  });
  try {
    await waitFor(() => document.querySelector('#lanAddrList .lan-addr'), 'the address rows');
    const rows = document.querySelectorAll('#lanAddrList .lan-addr');
    assert.equal(rows.length, 2);
    assert.ok(rows[0].classList.contains('recommended'));
    assert.equal(rows[0].querySelector('.lan-if').textContent, 'Ethernet —');
    assert.equal(rows[0].querySelector('code').textContent, 'ws://192.168.1.42:8772');
    assert.ok(!rows[1].classList.contains('recommended'));
    assert.equal(rows[1].querySelector('.lan-if').textContent, 'vEthernet (WSL) —');
  } finally {
    window.close();
  }
});

test('options: the WAN checker walks the check-host two-step and reports Available online', async () => {
  let resultPolls = 0;
  const { window, document, fetches } = await initOptions({
    clampTimeouts: 5,
    fetch: (url) => {
      if (url.includes('/health')) return jsonResponse(fakeHealth({ ws_mode: 'wan' }));
      if (url.includes('ident.me')) return jsonResponse({ ip: '203.0.113.7' });
      if (url.startsWith('https://check-host.net/check-http')) {
        return jsonResponse({ ok: 1, request_id: 'abc123',
          permanent_link: 'https://check-host.net/check-report/abc123' });
      }
      if (url.includes('/check-result/')) {
        resultPolls += 1;
        return jsonResponse(resultPolls === 1
          ? { 'us1.node': null }                                     // still working
          : { 'us1.node': [[1, 0.21, 'OK', '200', '203.0.113.7']] }); // saw our 200
      }
      return jsonResponse({});
    },
  });
  try {
    await waitFor(() => document.getElementById('wanReachText').textContent === 'Available online',
      'the Available online verdict');
    assert.ok(document.getElementById('wanReachIndicator').classList.contains('connected'));

    const checkUrl = fetches.find((u) => u.startsWith('https://check-host.net/check-http'));
    assert.ok(checkUrl.includes(encodeURIComponent('http://203.0.113.7:8772/reachable')),
      'the probed target is the token-free /reachable on the WS port — never anything else');
    assert.ok(resultPolls >= 2, 'a null node result must be polled again, not judged');

    const link = document.getElementById('wanReachLink');
    assert.equal(link.hidden, false);
    assert.equal(link.href, 'https://check-host.net/check-report/abc123');
  } finally {
    window.close();
  }
});

test('options: every node failing reads Not reachable', async () => {
  const { window, document } = await initOptions({
    clampTimeouts: 5,
    fetch: (url) => {
      if (url.includes('/health')) return jsonResponse(fakeHealth({ ws_mode: 'wan' }));
      if (url.includes('ident.me')) return jsonResponse({ ip: '203.0.113.7' });
      if (url.startsWith('https://check-host.net/check-http')) {
        return jsonResponse({ ok: 1, request_id: 'abc123' });
      }
      if (url.includes('/check-result/')) {
        return jsonResponse({
          'us1.node': [[0, 0.05, 'Connection refused', null, '203.0.113.7']],
          'eu1.node': [[0, null, 'timed out', null, null]],
        });
      }
      return jsonResponse({});
    },
  });
  try {
    await waitFor(() => /Not reachable/.test(document.getElementById('wanReachText').textContent),
      'the Not reachable verdict');
    assert.ok(document.getElementById('wanReachIndicator').classList.contains('disconnected'));
    assert.match(document.getElementById('wanReachText').textContent, /port-forward/);
  } finally {
    window.close();
  }
});

test('options: a custom check template auto-saves and re-runs against its own origin', async () => {
  const { window, document, store, fetches } = await initOptions({
    clampTimeouts: 5,
    fetch: (url) => {
      if (url.includes('/health')) return jsonResponse(fakeHealth({ ws_mode: 'wan' }));
      if (url.includes('ident.me')) return jsonResponse({ ip: '203.0.113.7' });
      if (url.startsWith('https://check-host.net/check-http')) {
        return jsonResponse({ ok: 1, request_id: 'abc123' });
      }
      if (url.startsWith('https://example.org/probe')) {
        return jsonResponse({ ok: 1, request_id: 'zzz9' });
      }
      if (url.includes('/check-result/')) {
        return jsonResponse({ n1: [[1, 0.1, 'OK', '200', '203.0.113.7']] });
      }
      return jsonResponse({});
    },
  });
  try {
    await waitFor(() => document.getElementById('wanReachText').textContent === 'Available online',
      'the default-template check to finish');

    document.getElementById('wanCheckTemplate').value = 'https://example.org/probe?u={target}';
    fireChange(window, 'wanCheckTemplate');
    await waitFor(() => fetches.some((u) => u.startsWith('https://example.org/check-result/')),
      'the re-run to poll the custom origin');

    assert.equal(store.wanCheckTemplate, 'https://example.org/probe?u={target}');
    assert.ok(fetches.some((u) => u.startsWith('https://example.org/probe?u=')));

    // A template without {target} is refused, not saved — the checker could never fill it.
    document.getElementById('wanCheckTemplate').value = 'https://example.org/no-placeholder';
    fireChange(window, 'wanCheckTemplate');
    await flush();
    assert.equal(store.wanCheckTemplate, 'https://example.org/probe?u={target}',
      'the stored template must be one the checker can use');
    assert.match(document.getElementById('wanReachText').textContent, /must contain \{target\}/);
  } finally {
    window.close();
  }
});

test('options: an edited bind with a matching mode offers Apply bind & Restart', async () => {
  const wsBinds = [];
  const { window, document } = await initOptions({
    clampTimeouts: 5,
    seed: { connectionToken: 'x'.repeat(43) },
    fetch: (url, init) => {
      if (url.includes('/ws-bind')) {
        wsBinds.push(JSON.parse(init.body));
        return jsonResponse({ status: 'restarting' });
      }
      if (url.includes('/health')) {
        return jsonResponse(wsBinds.length === 0
          ? fakeHealth({ ws_mode: 'lan', ws_host: '0.0.0.0', server_pid: 111 })
          : fakeHealth({ ws_mode: 'lan', ws_host: '192.168.1.42', server_pid: 222 }));
      }
      return jsonResponse({});
    },
  });
  try {
    await waitFor(() => document.querySelector('.surface-card[data-mode="lan"].selected'),
      'the LAN card to be selected');
    assert.equal(document.getElementById('netModeApply').hidden, true,
      'mode matches and the bind is untouched -> nothing to apply');

    const bind = document.getElementById('lanBindHost');
    bind.value = '192.168.1.42';
    bind.dispatchEvent(new window.Event('input', { bubbles: true }));
    await flush();
    const btn = document.getElementById('netModeApply');
    assert.equal(btn.hidden, false, 'an edited bind is applyable even with the mode already matching');
    assert.equal(btn.textContent, 'Apply bind & Restart');

    btn.click();
    await waitFor(() => btn.hidden, 'the applied bind to be confirmed');
    assert.deepEqual(wsBinds, [{ host: '192.168.1.42', mode: 'lan' }],
      'the edited bind ships with the panel\'s mode');
  } finally {
    window.close();
  }
});

test('options: LAN address list falls back to bare URLs, then to the tunnel line', async () => {
  // An older server (or one whose adapter lookup failed): ws_lan_urls only.
  const first = await initOptions({
    fetch: (url) => url.includes('/health')
      ? jsonResponse(fakeHealth({ ws_lan_addresses: undefined, ws_lan_urls: ['ws://10.0.0.9:8772'] }))
      : jsonResponse({}),
  });
  try {
    await waitFor(() => first.document.querySelector('#lanAddrList code'), 'the bare URL row');
    assert.equal(first.document.querySelector('#lanAddrList code').textContent, 'ws://10.0.0.9:8772');
    assert.equal(first.document.querySelectorAll('#lanAddrList .lan-addr').length, 0,
      'no adapter rows without adapter data');
  } finally {
    first.window.close();
  }

  // No addresses at all -> the tunnel suggestion, not an empty box.
  const second = await initOptions({
    fetch: (url) => url.includes('/health')
      ? jsonResponse(fakeHealth({ ws_lan_addresses: [], ws_lan_urls: [] }))
      : jsonResponse({}),
  });
  try {
    await waitFor(() => /tunnel/.test(second.document.getElementById('lanAddrList').textContent),
      'the tunnel line');
  } finally {
    second.window.close();
  }
});

test('options: switching away and back to WAN does not re-run an identical check', async () => {
  const { window, document, fetches } = await initOptions({
    clampTimeouts: 5,
    fetch: (url) => {
      if (url.includes('/health')) return jsonResponse(fakeHealth({ ws_mode: 'wan' }));
      if (url.includes('ident.me')) return jsonResponse({ ip: '203.0.113.7' });
      if (url.startsWith('https://check-host.net/check-http')) return jsonResponse({ ok: 1, request_id: 'abc123' });
      if (url.includes('/check-result/')) return jsonResponse({ n1: [[1, 0.1, 'OK', '200', '203.0.113.7']] });
      return jsonResponse({});
    },
  });
  try {
    await waitFor(() => document.getElementById('wanReachText').textContent === 'Available online',
      'the first check');
    assert.equal(fetches.filter((u) => u.startsWith('https://check-host.net/check-http')).length, 1);

    selectNetMode(window, 'local');
    await flush();
    selectNetMode(window, 'wan');
    await flush();
    await flush();
    assert.equal(fetches.filter((u) => u.startsWith('https://check-host.net/check-http')).length, 1,
      'same target -> no new check on a panel round-trip; the Refresh button exists for that');
  } finally {
    window.close();
  }
});

test('options: editing the WS port updates the firewall notes and the WAN URL', async () => {
  const { window, document } = await initOptions({
    seed: { wanPublicAddress: 'myhome.example.net' },
    fetch: (url) => url.includes('/health') ? jsonResponse(fakeHealth({ ws_mode: 'wan' })) : jsonResponse({}),
  });
  try {
    await waitFor(() => document.getElementById('wanPublicAddr').value === 'myhome.example.net',
      'the override to land');
    const port = document.getElementById('port');
    port.value = '9001';
    port.dispatchEvent(new window.Event('input', { bubbles: true }));
    await flush();
    assert.equal(document.getElementById('lanFirewallPort').textContent, '9001');
    assert.equal(document.getElementById('wanFirewallPort').textContent, '9001');
    assert.equal(document.getElementById('wanPublicUrl').textContent, 'ws://myhome.example.net:9001');
  } finally {
    window.close();
  }
});

test('options: public-address detection falls back to the tnedi.me mirror', async () => {
  const { window, document, fetches } = await initOptions({
    fetch: (url) => {
      if (url.includes('/health')) return jsonResponse(fakeHealth({ ws_mode: 'lan' }));
      if (url.includes('4.ident.me')) return jsonResponse({});           // primary answers garbage
      if (url.includes('4.tnedi.me')) return jsonResponse({ ip: '198.51.100.4' });
      return jsonResponse({});
    },
  });
  try {
    await waitFor(() => document.getElementById('wanPublicAddr').value === '198.51.100.4',
      'the mirror answer to land');
    const primary = fetches.findIndex((u) => u.includes('4.ident.me'));
    const mirror = fetches.findIndex((u) => u.includes('4.tnedi.me'));
    assert.ok(primary !== -1 && mirror > primary, 'the mirror is tried only after the primary fails');
  } finally {
    window.close();
  }
});

test('options: a stored public-address override beats auto-detect, and clearing re-detects', async () => {
  const { window, document, store, fetches } = await initOptions({
    clampTimeouts: 5,
    seed: { wanPublicAddress: 'myhome.example.net' },
    fetch: (url) => {
      if (url.includes('/health')) return jsonResponse(fakeHealth({ ws_mode: 'wan' }));
      if (url.includes('ident.me')) return jsonResponse({ ip: '203.0.113.7' });
      if (url.startsWith('https://check-host.net/check-http')) {
        return jsonResponse({ ok: 1, request_id: 'abc123' });
      }
      if (url.includes('/check-result/')) {
        return jsonResponse({ n1: [[1, 0.1, 'OK', '200', '198.51.100.9']] });
      }
      return jsonResponse({});
    },
  });
  try {
    await waitFor(() => document.getElementById('wanPublicAddr').value === 'myhome.example.net',
      'the override to land in the field');
    assert.ok(!fetches.some((u) => u.includes('ident.me')),
      'an override means NO detection call at all');
    assert.equal(document.getElementById('wanPublicUrl').textContent, 'ws://myhome.example.net:8772');
    await waitFor(() => document.getElementById('wanReachText').textContent === 'Available online',
      'the check against the override');
    const checkUrl = fetches.find((u) => u.startsWith('https://check-host.net/check-http'));
    assert.ok(checkUrl.includes(encodeURIComponent('http://myhome.example.net:8772/reachable')));

    document.getElementById('wanPublicAddr').value = '';
    fireChange(window, 'wanPublicAddr');
    await waitFor(() => fetches.some((u) => u.includes('ident.me')), 'the re-detection call');
    assert.equal(store.wanPublicAddress, undefined, 'clearing removes the override key');
    assert.equal(document.getElementById('wanPublicAddr').value, '203.0.113.7',
      'and the detected address takes its place');
  } finally {
    window.close();
  }
});

test('options: the map help does not promise hover or scroll-zoom on a phone', async () => {
  // The map reads `mousemove` for its tooltips and `wheel` for zoom; neither fires from
  // touch. Panning is `pointerdown`, which does. The copy has to match that.
  const mobile = await initOptions({ platformOs: 'android' });
  try {
    const p = mobile.document.querySelector('.conn-map-help, #pane-health p');
    const desktopSpan = mobile.document.querySelector('#pane-health span.desktop-only');
    const mobileSpan = mobile.document.querySelector('#pane-health span.mobile-only');
    assert.ok(desktopSpan && /Hover/.test(desktopSpan.textContent),
      'the desktop wording still exists for desktop');
    assert.ok(mobileSpan && /double-tap/.test(mobileSpan.textContent),
      'and a touch version exists beside it');
    assert.doesNotMatch(mobileSpan.textContent, /Hover|Scroll to zoom/,
      'the phone must not be told to hover or scroll-zoom');
  } finally {
    mobile.window.close();
  }
});

test('options: the remote-setup block is desktop-only', async () => {
  // Every word of it configures the machine RUNNING the server, which a phone is not.
  const { window, document } = await initOptions({ platformOs: 'android' });
  try {
    const block = document.getElementById('remoteSetup');
    assert.ok(block, 'the block still exists for desktop');
    assert.ok(block.classList.contains('desktop-only'),
      'hidden on Android: "Configure local server", "This machine", "Remote machine"');
    // The fields a phone DOES need must not be inside it.
    assert.equal(block.querySelector('#hostname'), null);
    assert.equal(block.querySelector('#connectionToken'), null);
  } finally {
    window.close();
  }
});


// ---- Captured bodies (General): censor secrets + custom patterns --------------------------

const feedbackLines = (window) => [...window.document.querySelectorAll('#censorPatternFeedback .field-note')]
  .map((n) => ({ cls: n.className, text: n.textContent }));

function typePatterns(window, text) {
  const el = window.document.getElementById('censorPatterns');
  el.value = text;
  el.dispatchEvent(new window.Event('input'));
  el.dispatchEvent(new window.Event('change'));
}

test('options: the Captured bodies card sits in General and ships raw (toggle off, no patterns)', async () => {
  const { window } = await initOptions();
  try {
    const general = window.document.getElementById('pane-general');
    for (const id of ['censorSecrets', 'censorPatterns', 'censorTestInput']) {
      assert.ok(general.querySelector('#' + id), `${id} is not in the General pane`);
    }
    assert.equal(window.document.getElementById('censorSecrets').checked, false);
    assert.equal(window.document.getElementById('censorPatternsIgnoreCase'), null, 'case is the pattern\'s business');
    assert.equal(window.document.getElementById('censorPatterns').value, '');
    assert.deepEqual(feedbackLines(window), []);
  } finally {
    window.close();
  }
});

test('options: the censor toggle persists to storage.sync under the key the background reads', async () => {
  const { window, store } = await initOptions();
  try {
    setChecked(window, 'censorSecrets', true);
    await flush();
    assert.equal(store.censorSecrets, true);
    setChecked(window, 'censorSecrets', false);
    await flush();
    assert.equal(store.censorSecrets, false);
    assert.equal('censorPatternsIgnoreCase' in store, false);
  } finally {
    window.close();
  }
});

test('options: the patterns row (patterns + Test) shows only while censoring is on', async () => {
  const { window } = await initOptions();
  try {
    const row = window.document.getElementById('censorPatternsField');
    assert.equal(row.hidden, true, 'ships off, so nothing about patterns is shown');
    setChecked(window, 'censorSecrets', true);
    await flush();
    assert.equal(row.hidden, false);
    assert.ok(row.contains(window.document.getElementById('censorPatterns')));
    assert.ok(row.contains(window.document.getElementById('censorTestInput')));
    setChecked(window, 'censorSecrets', false);
    await flush();
    assert.equal(row.hidden, true);
  } finally {
    window.close();
  }
  const on = await initOptions({ seed: { censorSecrets: true } });
  try {
    assert.equal(on.document.getElementById('censorPatternsField').hidden, false, 'shown on load when on');
  } finally {
    on.window.close();
  }
});

const testBox = (window, sample) => {
  const el = window.document.getElementById('censorTestInput');
  el.value = sample;
  el.dispatchEvent(new window.Event('input'));
  return {
    out: window.document.getElementById('censorTestOutput').textContent,
    note: window.document.getElementById('censorTestNote').textContent,
  };
};

test('options: the Test box shows the sample with the built-in masking and the typed patterns applied', async () => {
  const { window, store } = await initOptions({ seed: { censorSecrets: true } });
  try {
    assert.equal(testBox(window, '').note, 'Paste a sample body to see it censored.');
    // Built-in masking alone.
    let r = testBox(window, '{"user":"bob","password":"hunter2","note":"sk-abcdef123456"}');
    assert.equal(r.out, '{"user":"bob","password":"<redacted>","note":"sk-abcdef123456"}');
    assert.equal(r.note, '1 value censored.');
    // A pattern typed but not saved yet applies at once.
    const pat = window.document.getElementById('censorPatterns');
    pat.value = 'sk-[a-z0-9]+';
    pat.dispatchEvent(new window.Event('input'));
    r = testBox(window, '{"user":"bob","password":"hunter2","note":"sk-abcdef123456"}');
    assert.equal(r.out, '{"user":"bob","password":"<redacted>","note":"<redacted>"}');
    assert.equal(r.note, '2 values censored.');
    assert.equal('censorPatterns' in store, false, 'testing saves nothing');
    // No match says so.
    assert.equal(testBox(window, 'plain text').note, 'Nothing matched.');
    // Case is the pattern\'s business.
    pat.value = 'plain';
    pat.dispatchEvent(new window.Event('input'));
    assert.equal(testBox(window, 'Plain plain').out, 'Plain <redacted>');
    pat.value = '(?i:plain)';
    pat.dispatchEvent(new window.Event('input'));
    assert.equal(testBox(window, 'Plain plain').out, '<redacted> <redacted>');
    // An invalid pattern is ignored, the rest still applies.
    pat.value = '[\n(?i:plain)';
    pat.dispatchEvent(new window.Event('input'));
    assert.equal(testBox(window, 'Plain').out, '<redacted>');
    // The output is text, never markup.
    assert.equal(testBox(window, '<b>x</b>').out, '<b>x</b>');
    assert.equal(window.document.getElementById('censorTestOutput').children.length, 0);
  } finally {
    window.close();
  }
});

test('options: the Test sample is saved on change to storage.LOCAL, never sync, and loaded back', async () => {
  const { window, store, local } = await initOptions({ seed: { censorSecrets: true } });
  try {
    const el = window.document.getElementById('censorTestInput');
    el.value = '{"password":"hunter2"}';
    el.dispatchEvent(new window.Event('input'));
    assert.equal('censorTestText' in local, false, 'typing alone must not save');
    el.dispatchEvent(new window.Event('change'));
    await flush();
    assert.equal(local.censorTestText, '{"password":"hunter2"}');
    // Firefox Sync would copy a pasted secret to the user's other devices.
    assert.equal('censorTestText' in store, false, 'the sample reached storage.sync');
    el.value = 'x'.repeat(5000);
    el.dispatchEvent(new window.Event('change'));
    await flush();
    assert.equal(local.censorTestText.length, 3000);
  } finally {
    window.close();
  }
  const again = await initOptions({ seed: { censorSecrets: true }, seedLocal: { censorTestText: 'sk-abc123' } });
  try {
    assert.equal(again.document.getElementById('censorTestInput').value, 'sk-abc123');
    assert.equal(again.document.getElementById('censorTestOutput').textContent, 'sk-abc123',
      'and the output is drawn from it on load');
  } finally {
    again.window.close();
  }
});

test('options: a sample an older build put in storage.sync is moved to storage.local on load', async () => {
  const { window, store, local } = await initOptions({ seed: { censorSecrets: true, censorTestText: 'sk-old' } });
  try {
    assert.equal('censorTestText' in store, false, 'the synced copy was not deleted');
    assert.equal(local.censorTestText, 'sk-old');
    assert.equal(window.document.getElementById('censorTestInput').value, 'sk-old');
  } finally {
    window.close();
  }
  // A local sample already there wins; the synced one is still deleted.
  const both = await initOptions({ seed: { censorTestText: 'sk-old' }, seedLocal: { censorTestText: 'sk-new' } });
  try {
    assert.equal('censorTestText' in both.store, false);
    assert.equal(both.local.censorTestText, 'sk-new');
  } finally {
    both.window.close();
  }
});

test('options: the toggle still saves when the patterns write fails (storage.sync item quota)', async () => {
  const { window, store } = await initOptions({ seed: { censorSecrets: true, censorPatterns: ['abc'] } });
  try {
    const sync = window.browser.storage.sync;
    const realSet = sync.set.bind(sync);
    sync.set = (obj) => ('censorPatterns' in obj
      ? Promise.reject(new Error('QUOTA_BYTES_PER_ITEM quota exceeded')) : realSet(obj));
    typePatterns(window, 'é'.repeat(150));
    await flush();
    assert.deepEqual([...store.censorPatterns], ['abc'], 'the failed write changed nothing');
    setChecked(window, 'censorSecrets', false);
    await flush();
    assert.equal(store.censorSecrets, false, 'censoring could not be switched off');
  } finally {
    window.close();
  }
});

test('options: patterns are saved one per entry and loaded back one per line', async () => {
  const { window, store } = await initOptions();
  try {
    typePatterns(window, 'sk-[a-z0-9]{20,}\n\n  eyJ[\\w-]+  \n');
    await flush();
    assert.deepEqual([...store.censorPatterns], ['sk-[a-z0-9]{20,}', 'eyJ[\\w-]+']);
    assert.deepEqual(feedbackLines(window), [{ cls: 'field-note ok', text: '2 patterns active' }]);
  } finally {
    window.close();
  }
  const again = await initOptions({ seed: { censorPatterns: ['a+b', 'c'], censorSecrets: true } });
  try {
    assert.equal(again.document.getElementById('censorPatterns').value, 'a+b\nc');
    assert.equal(again.document.getElementById('censorSecrets').checked, true);
  } finally {
    again.window.close();
  }
});

test('options: an invalid or dangerous pattern is flagged with its reason, and still saved for fixing', async () => {
  const { window, store } = await initOptions();
  try {
    typePatterns(window, 'fine\n[\n(a+)+$');
    await flush();
    const lines = feedbackLines(window);
    assert.equal(lines.length, 3);
    assert.match(lines[0].text, /^Pattern 2 \(\[\) is ignored: invalid regular expression/);
    assert.match(lines[1].text, /^Pattern 3 \(\(a\+\)\+\$\) is ignored: rejected: .*can hang the browser/);
    assert.equal(lines[0].cls, 'field-note err');
    assert.deepEqual(lines[2], { cls: 'field-note ok', text: '1 pattern active' });
    assert.deepEqual([...store.censorPatterns], ['fine', '[', '(a+)+$']);
  } finally {
    window.close();
  }
});

test('options: the 21st pattern and an over-long one are flagged and NOT saved', async () => {
  const { window, store } = await initOptions();
  try {
    const twenty = Array.from({ length: 20 }, (_, i) => `p${i}x`);
    typePatterns(window, [...twenty, 'extra', 'z'.repeat(201)].join('\n'));
    await flush();
    assert.deepEqual([...store.censorPatterns], twenty);
    const texts = feedbackLines(window).map((l) => l.text);
    assert.ok(texts.some((x) => /Pattern 21 \(extra\) is ignored: over the limit of 20 patterns/.test(x)), texts.join('|'));
    assert.ok(texts.some((x) => /Pattern 22 .*is ignored: too long \(201 characters, the limit is 200\)/.test(x)), texts.join('|'));
    assert.ok(texts.includes('20 patterns active'));
  } finally {
    window.close();
  }
});

test('options: the feedback list follows typing before anything is saved', async () => {
  const { window, store } = await initOptions();
  try {
    const el = window.document.getElementById('censorPatterns');
    el.value = '(';
    el.dispatchEvent(new window.Event('input'));
    assert.equal(feedbackLines(window).length, 1);
    assert.equal('censorPatterns' in store, false, 'typing alone must not save');
  } finally {
    window.close();
  }
});

test('options: Reset to defaults puts the censor settings back to raw', async () => {
  const { window, store, local } = await initOptions({ seed: {
    censorSecrets: true, censorPatterns: ['abc'] }, seedLocal: { censorTestText: 'abc' } });
  try {
    assert.equal(window.document.getElementById('censorSecrets').checked, true);
    window.document.getElementById('resetDefaults').click();
    await flush();
    await flush();
    assert.equal('censorSecrets' in store, false);
    assert.equal(window.document.getElementById('censorSecrets').checked, false);
    assert.equal(window.document.getElementById('censorPatterns').value, '');
    assert.equal(window.document.getElementById('censorPatternsField').hidden, true);
    assert.equal(window.document.getElementById('censorTestInput').value, '');
    assert.equal('censorTestText' in local, false, 'the sample in storage.local survived the reset');
  } finally {
    window.close();
  }
});

test('options: without the module the page says so and never overwrites stored patterns', async () => {
  const { window, store } = await initOptions({ noBodyCensor: true, seed: { censorPatterns: ['keep-me'] } });
  try {
    assert.match(feedbackLines(window)[0].text, /did not load/);
    setChecked(window, 'censorSecrets', true);
    await flush();
    assert.equal(store.censorSecrets, true);
    assert.deepEqual([...store.censorPatterns], ['keep-me']);
  } finally {
    window.close();
  }
});

// --- Provider sign-in checks: Disabled means no request to the site -----------
// Each card's help text promises that Disabled stops the requests to its site, and the
// sign-in line is drawn from a forced usage read, which is such a request.

const USAGE_ACTIONS = ['getClaudeUsage', 'getOpenAIUsage', 'getCopilotUsage'];

async function initRecordingUsage(seed) {
  const sent = [];
  const opened = await initOptions({
    seed,
    sendMessage: async (msg) => {
      if (msg && USAGE_ACTIONS.includes(msg.action)) sent.push(msg.action);
      return { success: false, error: 'stub' };
    },
  });
  await flush();
  return { ...opened, sent };
}

test('options: a Disabled provider card sends nothing to its site when the page opens', async () => {
  const { window, document, sent } = await initRecordingUsage({
    anthropicDisplayMode: 'disabled', openaiDisplayMode: 'disabled', copilotDisplayMode: 'disabled' });
  try {
    assert.deepEqual(sent, [], 'a Disabled card still checked its sign-in');
    assert.match(document.getElementById('copilotAuthText').textContent, /Disabled/);
    assert.equal(document.getElementById('copilotSignIn').hidden, true);

    setSelect(window, 'copilotDisplayMode', 'tab');
    await flush();
    assert.deepEqual(sent, ['getCopilotUsage'], 'switching the card on checks it, once');

    document.getElementById('openaiAuthRecheck').click();
    await flush();
    assert.deepEqual(sent, ['getCopilotUsage', 'getOpenAIUsage'], 'Re-check asks whatever the mode');
  } finally {
    window.close();
  }
});

test('options: the cards that ship on check their sign-in when the page opens', async () => {
  const { window, sent } = await initRecordingUsage();
  try {
    // Exactly once each: the page's init runs once (dom-ready.mjs).
    assert.deepEqual([...sent].sort(), ['getClaudeUsage', 'getCopilotUsage'],
      'Anthropic and Copilot ship on; OpenAI ships off');
  } finally {
    window.close();
  }
});

// --- /ws-bind against a server that predates v2 signatures --------------------

const refused = (error) => ({ ok: false, status: 403, json: async () => ({ error }), text: async () => '' });
const TOKEN_REQUIRED = 'the connection token is required to change the bind address';
const V1_BODY_REFUSED = 'this request has a body, but its X-BrowserPlug-Auth signature (v1) does '
  + 'not cover the body; update the BrowserPlug extension to a build that signs it (v2)';

async function setWanAgainst(bindReply) {
  const binds = [];
  let rebound = false;
  const opened = await initOptions({
    clampTimeouts: 5,
    seed: { connectionToken: 'x'.repeat(43) },
    fetch: (url, init) => {
      if (url.includes('/ws-bind')) {
        const scheme = init.headers['x-browserplug-auth'].split(' ')[0];
        binds.push({ scheme, body: init.body });
        const reply = bindReply(scheme);
        if (reply.ok) rebound = true;
        return reply;
      }
      if (url.includes('/health')) {
        return jsonResponse(rebound ? fakeHealth({ ws_mode: 'wan', server_pid: 222 })
          : fakeHealth({ ws_mode: 'local', ws_host: 'localhost', ws_accepts_remote: false }));
      }
      return jsonResponse({});
    },
  });
  const { window, document } = opened;
  await waitFor(() => document.querySelector('.surface-card[data-mode="local"].selected'),
    'the Local card to be selected');
  selectNetMode(window, 'wan');
  await flush();
  document.getElementById('netModeApply').click();
  return { ...opened, binds };
}

test('options: a server that predates v2 gets the bind again, signed v1', async () => {
  const { window, document, binds } = await setWanAgainst((scheme) => (scheme === 'v1'
    ? jsonResponse({ status: 'restarting', ws_mode: 'wan' }) : refused(TOKEN_REQUIRED)));
  try {
    await waitFor(() => document.getElementById('netModeApply').hidden,
      'the Set button to disappear once the replacement reports WAN');
    assert.deepEqual(binds.map((b) => b.scheme), ['v2', 'v1']);
    assert.equal(binds[1].body, binds[0].body, 'the same bytes; only the signature changes');
    assert.match(document.getElementById('netModeState').textContent, /configured as: WAN/);
  } finally {
    window.close();
  }
});

test('options: a v2 server that refuses the bind reports its own reason, not the retry\'s', async () => {
  const { window, document, binds } = await setWanAgainst((scheme) => (scheme === 'v1'
    ? refused(V1_BODY_REFUSED) : refused(TOKEN_REQUIRED)));
  try {
    await waitFor(() => /connection token|update the BrowserPlug/.test(
      document.getElementById('netModeState').textContent), 'the refusal to be shown');
    assert.deepEqual(binds.map((b) => b.scheme), ['v2', 'v1']);
    assert.match(document.getElementById('netModeState').textContent, /connection token is required/);
    assert.doesNotMatch(document.getElementById('netModeState').textContent, /update the BrowserPlug/);
  } finally {
    window.close();
  }
});
