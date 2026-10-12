/*
 * The popup's "Allow user scripts" surfaces, Chromium only.
 *
 * Two places show the switch: a card on the Permissions tab, and the status line, which
 * reads "Connected but scripts are blocked" with "scripts are blocked" linking to that
 * tab while the switch is off. Both read one probe (popup/chromium-access.js). The card
 * links to the extension's own chrome://extensions page, whose id differs per install.
 *
 * The popup is shared byte-identical with Firefox, so the other half of the contract is
 * that Firefox shows none of this. Drives the REAL popup.html and popup.js under jsdom
 * with a `chrome` global and no `browser` global, which is how popup.js tells the engines
 * apart. Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import { domContentLoaded } from './dom-ready.mjs';

const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), '../../extension-gecko/popup');
const read = (f) => readFileSync(path.join(dir, f), 'utf8');
const POPUP_HTML = read('popup.html');
const SCRIPTS = ['popup-tabs.js', 'chromium-access.js', 'popup.js'].map(read);
const ACCESS_JS = read('chromium-access.js');

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const EXT_ID = 'abcdefghijklmnopabcdefghijklmnop';

// --- the module -------------------------------------------------------------------

function loadAccess() {
  const g = {};
  // eslint-disable-next-line no-new-func
  new Function('globalThis', ACCESS_JS)(g);
  return g.ChromiumAccess;
}

test('userScriptsEnabled: true only when the API answers, false when reading it throws', () => {
  const { userScriptsEnabled } = loadAccess();
  assert.equal(userScriptsEnabled({ userScripts: { execute() {} } }), true);
  assert.equal(userScriptsEnabled({ userScripts: {} }), false);
  assert.equal(userScriptsEnabled({}), false);
  assert.equal(userScriptsEnabled(undefined), false);
  const off = {};
  Object.defineProperty(off, 'userScripts', { get() { throw new Error('toggle off'); } });
  assert.equal(userScriptsEnabled(off), false, 'the namespace throws while the switch is off');
});

test('extensionsPageUrl: the browser\'s own scheme, the install\'s own id', () => {
  const { extensionsPageUrl } = loadAccess();
  const chromeUa = 'Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36';
  assert.equal(extensionsPageUrl(EXT_ID, { userAgent: chromeUa }),
    `chrome://extensions/?id=${EXT_ID}`);
  assert.equal(extensionsPageUrl(EXT_ID, { userAgent: chromeUa + ' Edg/140.0.0.0' }),
    `edge://extensions/?id=${EXT_ID}`);
  assert.equal(extensionsPageUrl(EXT_ID, { userAgent: chromeUa, brave: true }),
    `brave://extensions/?id=${EXT_ID}`);
  assert.equal(extensionsPageUrl('a b', {}), 'chrome://extensions/?id=a%20b', 'id is encoded');
});

// --- the popup --------------------------------------------------------------------

// `userScripts: true|false` models the switch; `chromium: false` is the Firefox shape.
// `definesBrowser` is a Chrome that also defines the `browser` global, as recent ones do.
async function initPopup({ userScripts = false, chromium = true, encrypted = true,
  userAgent = null, definesBrowser = false, fileAccess = false } = {}) {
  const dom = new JSDOM(POPUP_HTML, { pretendToBeVisual: true, runScripts: 'outside-only' });
  const { window } = dom;
  const pageReady = domContentLoaded(window);
  if (userAgent) Object.defineProperty(window.navigator, 'userAgent', { value: userAgent });
  const created = [];
  const api = {
    runtime: {
      id: EXT_ID,
      lastError: null,
      // The shipped manifests differ here: a service worker on Chromium, a page on Firefox.
      getManifest: () => ({
        version: '0.0.0-test',
        background: chromium ? { service_worker: 'background.js' } : { scripts: ['background.js'] },
      }),
      openOptionsPage: () => {},
      sendMessage: (msg, cb) => {
        const reply = msg.action === 'getConnectionStatus'
          ? { connected: true, phase: 'connected', encrypted }
          : { success: false };
        if (typeof cb === 'function') cb(reply);
        return Promise.resolve(reply);
      },
    },
    action: {
      getBadgeBackgroundColor: () => Promise.resolve([0, 160, 0, 255]),
      getTitle: () => Promise.resolve('BrowserPlug: Connected to the server'),
    },
    storage: {
      sync: { get: (d) => Promise.resolve({ ...d }), set: () => Promise.resolve() },
      local: { get: (d) => Promise.resolve({ ...d }), set: () => Promise.resolve() },
    },
    permissions: { contains: () => Promise.resolve(false), request: () => Promise.resolve(true) },
    extension: { isAllowedFileSchemeAccess: () => Promise.resolve(fileAccess) },
    tabs: {
      query: () => Promise.resolve([]),
      update: () => Promise.resolve(),
      create: (o) => { created.push(o); return Promise.resolve({ id: 1 }); },
    },
  };
  if (userScripts) api.userScripts = { execute() {} };
  else {
    Object.defineProperty(api, 'userScripts', {
      configurable: true, get() { throw new Error('toggle off'); },
    });
  }
  if (chromium) {
    window.chrome = api;
    if (definesBrowser) {
      api.browserAction = api.action;
      window.browser = api;
    }
  } else {
    // Firefox: a `browser` global, and its own action namespace.
    api.browserAction = api.action;
    window.browser = api;
  }
  window.fetch = async () => ({ ok: true, json: async () => ({}), text: async () => '' });
  window.console = Object.assign(Object.create(window.console), { error: () => {} });
  for (const src of SCRIPTS) window.eval(src);
  await pageReady;
  await flush();
  await flush();
  return { window, document: window.document, created };
}

const statusLine = (document) => document.getElementById('status').textContent;
const statusClass = (document) => document.getElementById('status').className;

test('Chromium, switch off: the status line says scripts are blocked, and is amber', async () => {
  const { window, document } = await initPopup({ userScripts: false });
  try {
    assert.equal(statusLine(document), 'Connected but scripts are blocked');
    assert.match(statusClass(document), /\bwarn\b/);
    const link = document.querySelector('#status .msg a');
    assert.ok(link, 'there is a link');
    assert.equal(link.textContent, 'scripts are blocked', 'only those words are the link');
  } finally {
    window.close();
  }
});

test('Chromium, switch off: the link opens the Permissions tab, at the card', async () => {
  const { window, document } = await initPopup({ userScripts: false });
  // jsdom has no layout and no scrollIntoView; record the call instead.
  const scrolled = [];
  window.HTMLElement.prototype.scrollIntoView = function (opts) { scrolled.push([this.id, opts]); };
  try {
    assert.equal(document.getElementById('pane-permissions').hidden, true, 'starts on Status');
    document.querySelector('#status .msg a').dispatchEvent(
      new window.MouseEvent('click', { bubbles: true, cancelable: true }));
    assert.equal(document.getElementById('pane-permissions').hidden, false);
    assert.equal(document.getElementById('pane-status').hidden, true);
    assert.equal(scrolled.length, 1);
    assert.equal(scrolled[0][0], 'userScriptsCard', 'scrolled to the Allow user scripts card');
    assert.equal(document.getElementById('pane-permissions').hidden, false,
      'the pane is shown BEFORE the scroll, or the card has no position yet');
  } finally {
    window.close();
  }
});

test('Chromium, switch off: a refresh does not rebuild the link', async () => {
  // The poll runs every 700 ms; a link replaced under the pointer loses the click.
  const { window, document } = await initPopup({ userScripts: false });
  try {
    const before = document.querySelector('#status .msg a');
    window.refreshStatus();
    await flush();
    await flush();
    assert.equal(document.querySelector('#status .msg a'), before, 'same node');
  } finally {
    window.close();
  }
});

test('Chromium, switch off, no encryption: both facts, one line', async () => {
  const { window, document } = await initPopup({ userScripts: false, encrypted: false });
  try {
    assert.equal(statusLine(document),
      'Connected but scripts are blocked, no encryption \u{1F513}');
    assert.equal(document.querySelector('#status .msg a').textContent, 'scripts are blocked');
  } finally {
    window.close();
  }
});

test('Chromium, switch on: plain Connected, green, no link', async () => {
  const { window, document } = await initPopup({ userScripts: true });
  try {
    assert.equal(statusLine(document), 'Connected');
    assert.match(statusClass(document), /\bok\b/);
    assert.equal(document.querySelector('#status .msg a'), null);
  } finally {
    window.close();
  }
});

test('Chromium: the Permissions card shows the switch off, with the page link', async () => {
  const { window, document, created } = await initPopup({ userScripts: false });
  try {
    assert.equal(document.getElementById('userScriptsCard').hidden, false);
    assert.equal(document.getElementById('userScriptsIndicator').textContent, 'Disabled');
    assert.equal(document.getElementById('userScriptsEnableHelp').hidden, false);
    const link = document.getElementById('openExtensionPage');
    assert.equal(link.textContent, `chrome://extensions/?id=${EXT_ID}`,
      'the id is this install\'s, and the URL is readable');
    link.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
    await flush();
    assert.equal(created.length, 1);
    assert.equal(created[0].url, `chrome://extensions/?id=${EXT_ID}`);
  } finally {
    window.close();
  }
});

test('Chromium: the card uses Edge\'s scheme on Edge', async () => {
  const { window, document, created } = await initPopup({
    userScripts: false, userAgent: 'Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0',
  });
  try {
    document.getElementById('openExtensionPage').dispatchEvent(
      new window.MouseEvent('click', { bubbles: true, cancelable: true }));
    await flush();
    assert.equal(created[0].url, `edge://extensions/?id=${EXT_ID}`);
  } finally {
    window.close();
  }
});

test('Chromium: with the switch on the card says Enabled and offers no steps', async () => {
  const { window, document } = await initPopup({ userScripts: true });
  try {
    assert.equal(document.getElementById('userScriptsCard').hidden, false);
    assert.equal(document.getElementById('userScriptsIndicator').textContent, 'Enabled');
    assert.equal(document.getElementById('userScriptsEnableHelp').hidden, true);
  } finally {
    window.close();
  }
});

test('Chromium: Re-check reads the switch again', async () => {
  const { window, document } = await initPopup({ userScripts: false });
  try {
    // The user flips the switch on the extensions page; the getter stops throwing.
    Object.defineProperty(window.chrome, 'userScripts', {
      configurable: true, value: { execute() {} },
    });
    document.getElementById('userScriptsRecheck').dispatchEvent(new window.Event('click'));
    await flush();
    assert.equal(document.getElementById('userScriptsIndicator').textContent, 'Enabled');
  } finally {
    window.close();
  }
});

test('a Chrome that defines `browser` is still Chromium', async () => {
  // Canary does. The popup used to take a `browser` global to mean Firefox, and showed
  // neither the card nor the blocked line there.
  const { window, document } = await initPopup({ userScripts: false, definesBrowser: true });
  try {
    assert.equal(statusLine(document), 'Connected but scripts are blocked');
    assert.equal(document.getElementById('userScriptsCard').hidden, false);
  } finally {
    window.close();
  }
});

// The file-access card is shared by both engines, but only Firefox has about:addons. It
// used to send a Chrome user there.
test('Chromium, file access off: the card points at the extensions page, not about:addons', async () => {
  const { window, document, created } = await initPopup({ userScripts: true, fileAccess: false });
  try {
    assert.equal(document.getElementById('fileAccessIndicator').textContent, 'Disabled');
    assert.equal(document.getElementById('fileAccessEnableHelp').hidden, true,
      'the about:addons steps do not exist on Chromium');
    assert.equal(document.getElementById('fileAccessChromiumHelp').hidden, false);
    assert.doesNotMatch(document.getElementById('fileAccessDesktopHow').textContent, /Firefox/);
    const link = document.getElementById('openExtensionPageFiles');
    assert.equal(link.textContent, `chrome://extensions/?id=${EXT_ID}`);
    link.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
    await flush();
    assert.equal(created.length, 1);
    assert.equal(created[0].url, `chrome://extensions/?id=${EXT_ID}`);
  } finally {
    window.close();
  }
});

test('Chromium, file access on: Enabled, and no steps', async () => {
  const { window, document } = await initPopup({ userScripts: true, fileAccess: true });
  try {
    assert.equal(document.getElementById('fileAccessIndicator').textContent, 'Enabled');
    assert.equal(document.getElementById('fileAccessEnableHelp').hidden, true);
    assert.equal(document.getElementById('fileAccessChromiumHelp').hidden, true);
  } finally {
    window.close();
  }
});

test('Firefox, file access off: the about:addons steps, never the Chromium ones', async () => {
  const { window, document } = await initPopup({ chromium: false, fileAccess: false });
  try {
    assert.equal(document.getElementById('fileAccessIndicator').textContent, 'Disabled');
    assert.equal(document.getElementById('fileAccessEnableHelp').hidden, false);
    assert.equal(document.getElementById('fileAccessChromiumHelp').hidden, true);
  } finally {
    window.close();
  }
});

test('Firefox: no card, and the status line is untouched', async () => {
  const { window, document } = await initPopup({ chromium: false, userScripts: false });
  try {
    assert.equal(document.getElementById('userScriptsCard').hidden, true);
    assert.equal(statusLine(document), 'Connected');
    assert.equal(document.querySelector('#status .msg a'), null);
    assert.match(statusClass(document), /\bok\b/);
  } finally {
    window.close();
  }
});
