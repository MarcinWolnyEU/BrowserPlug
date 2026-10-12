/*
 * CHROMIUM-ONLY test (no gecko counterpart): the browser-wide SOCKS proxy in
 * extension-chromium/background.js.
 *
 * Gecko routes ONE tab (proxy.test.mjs). Chromium cannot: chrome.proxy is a browser-global
 * setting with no per-request hook, and a PAC script gets no tab identity. So on Chromium a
 * non-direct `proxy` on tabs.create / navigation.navigate / tabs.routing switches the WHOLE
 * browser, behind a consent switch in the popup (Chrome allows no OPTIONAL `proxy`
 * permission, so the manifest requires it), and the server tells the agent so. What
 * keeps that safe is tested here, against the real code:
 *   - the switch happens BEFORE the tab opens or navigates, and any refusal (bad value, no
 *     permission, a setting someone else controls) stops there, so nothing loads direct;
 *   - the loopback and the server's own host bypass the proxy (the control socket and the
 *     managed Tor must stay reachable);
 *   - "" or "direct" undoes it, and the glue that undoes it by itself (socket close,
 *     watchdog, browser start, install) is still armed. That glue is where this code would
 *     silently rot, so each call site is checked, not just the helper.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');

function slice(startAnchor, endAnchor, what) {
  const start = BG_SRC.indexOf(startAnchor);
  const end = BG_SRC.indexOf(endAnchor, start);
  assert.ok(start >= 0 && end > start,
    `${what} block not found in background.js, anchors moved. ` +
    `${JSON.stringify(startAnchor)} .. ${JSON.stringify(endAnchor)}`);
  return BG_SRC.slice(start, end);
}

const PROXY_BLOCK = slice('const BROWSER_PROXY_KEY', '// Privileged pages where content scripts',
  'browser-wide proxy');

// A WebExtension `browser` with just what the proxy block touches, and a log of calls.
function makeEnv(opts = {}) {
  const log = [];
  const store = {};
  const granted = opts.granted !== false;
  if (granted) store.allowBrowserProxy = true;      // the popup switch, turned on
  const control = opts.levelOfControl || 'controlled_by_this_extension';
  return { log, store, errors: [], source: `
    const console = { log() {}, warn() {}, error() {} };
    const CONFIG = { hostname: ${JSON.stringify(opts.serverHost || 'localhost')} };
    const errors = [];
    const sendError = (id, code, message) => errors.push({ id, code, message });
    const browser = {
      storage: { local: {
        get: async (k) => (k in store ? { [k]: store[k] } : {}),
        set: async (o) => { Object.assign(store, o); log.push('storage.set'); },
        remove: async (k) => { delete store[k]; log.push('storage.remove'); },
      } },
      proxy: { settings: {
        set: async (a) => { log.push('proxy.set'); settings.push(a); },
        get: async () => ({ levelOfControl: ctl, value: { mode: 'fixed_servers' } }),
        clear: async (a) => { log.push('proxy.clear'); cleared.push(a); },
      } },
    };
    const settings = [];
    const cleared = [];
    let ctl = ${JSON.stringify(control)};
    const setControl = (v) => { ctl = v; };
  ` };
}

function loadProxy(opts) {
  const env = makeEnv(opts);
  const factory = new Function('log', 'store', `
    ${env.source}
    ${PROXY_BLOCK}
    return { parseProxyEndpoint, applyProxyField, clearBrowserProxy, readBrowserProxy,
             browserProxyIsInForce, setControl, errors, settings, cleared };
  `);
  return { ...factory(env.log, env.store), log: env.log, store: env.store };
}

// ---- the grammar ------------------------------------------------------------

test('parseProxyEndpoint maps the schemes and rejects everything else', () => {
  const { parseProxyEndpoint } = loadProxy();
  for (const v of [undefined, null, '', 'direct']) assert.equal(parseProxyEndpoint(v).direct, true);
  assert.deepEqual(parseProxyEndpoint('socks5://127.0.0.1:9050').info,
    { scheme: 'socks5', host: '127.0.0.1', port: 9050, endpoint: 'socks5://127.0.0.1:9050' });
  assert.equal(parseProxyEndpoint('socks5h://tor:9150').info.scheme, 'socks5');
  assert.equal(parseProxyEndpoint('socks4a://tor:9150').info.scheme, 'socks4');
  for (const bad of ['127.0.0.1:9050', 'http://p:8080', 'socks5://nohost', 'socks5://h:0',
                     'socks5://h:70000']) {
    assert.equal(parseProxyEndpoint(bad).ok, false, bad);
  }
});

// ---- applyProxyField --------------------------------------------------------

test('a call with no proxy field changes nothing and touches no setting', async () => {
  const m = loadProxy();
  assert.deepEqual(await m.applyProxyField('i', undefined), { error: false, change: null });
  assert.deepEqual(m.log, []);
});

test('a SOCKS endpoint switches the whole browser, with loopback and the server bypassed', async () => {
  const m = loadProxy({ serverHost: '192.168.0.5' });
  const r = await m.applyProxyField('i', 'socks5://127.0.0.1:50767');
  assert.deepEqual(r.change, { off: false, endpoint: 'socks5://127.0.0.1:50767',
                               scope: 'browser', already: false });
  const set = m.settings[0];
  assert.equal(set.scope, 'regular');
  assert.equal(set.value.mode, 'fixed_servers');
  assert.deepEqual(set.value.rules.singleProxy, { scheme: 'socks5', host: '127.0.0.1', port: 50767 });
  assert.deepEqual(set.value.rules.bypassList, ['localhost', '127.0.0.1', '[::1]', '192.168.0.5']);
  assert.equal(m.store.browserProxy.endpoint, 'socks5://127.0.0.1:50767');
});

test('switching to the endpoint already set is idempotent and says so', async () => {
  const m = loadProxy();
  await m.applyProxyField('i', 'socks5://127.0.0.1:50767');
  const again = await m.applyProxyField('i', 'socks5://127.0.0.1:50767');
  assert.equal(again.change.already, true);
  const other = await m.applyProxyField('i', 'socks5://127.0.0.1:50768');
  assert.equal(other.change.already, false);
});

test('"" and "direct" switch it off and report what was on', async () => {
  const m = loadProxy();
  await m.applyProxyField('i', 'socks5://127.0.0.1:50767');
  for (const off of ['', 'direct']) {
    const r = await m.applyProxyField('i', off);
    assert.equal(r.change.off, true);
  }
  assert.equal(m.cleared.length, 2);
  assert.equal(m.store.browserProxy, undefined, 'the stored state is gone');
  const none = loadProxy();
  const r = await none.applyProxyField('i', '');
  assert.equal(r.change.was, null, 'switching off when nothing was on is harmless');
});

test('with the consent switch off the call is refused and no setting is touched', async () => {
  const m = loadProxy({ granted: false });
  const r = await m.applyProxyField('i', 'socks5://127.0.0.1:50767');
  assert.equal(r.error, true);
  assert.equal(m.errors[0].code, 'PROXY_PERMISSION_REQUIRED');
  assert.match(m.errors[0].message, /popup/);
  assert.match(m.errors[0].message, /Allow/);
  assert.equal(m.settings.length, 0);
});

test('the consent is read from storage, and a missing proxy API means no permission', async () => {
  const on = loadProxy();
  assert.equal((await on.applyProxyField('i', 'socks5://127.0.0.1:50767')).error, false);
  const off = loadProxy({ granted: false });
  assert.equal((await off.applyProxyField('i', 'socks5://127.0.0.1:50767')).error, true);
  // No chrome.proxy at all (the permission was withdrawn): refused, not an exception.
  const noApi = new Function('store', `
    const console = { log() {}, warn() {}, error() {} };
    const CONFIG = { hostname: 'localhost' };
    const errors = [];
    const sendError = (id, code, message) => errors.push({ id, code, message });
    const browser = { storage: { local: { get: async () => ({ allowBrowserProxy: true }) } } };
    ${PROXY_BLOCK}
    return { applyProxyField, errors };`)({});
  assert.equal((await noApi.applyProxyField('i', 'socks5://127.0.0.1:50767')).error, true);
  assert.equal(noApi.errors[0].code, 'PROXY_PERMISSION_REQUIRED');
});

test('a baked consent (the test harness) counts, as the baked port does', async () => {
  const baked = new Function('store', `
    const console = { log() {}, warn() {}, error() {} };
    const BUILD_CONFIG = { allowBrowserProxy: true };
    const CONFIG = { hostname: 'localhost' };
    const errors = [];
    const sendError = (id, code, message) => errors.push({ id, code, message });
    const browser = {
      storage: { local: { get: async () => ({}), set: async () => {}, remove: async () => {} } },
      proxy: { settings: { set: async () => {}, get: async () => ({ levelOfControl:
        'controlled_by_this_extension' }), clear: async () => {} } } };
    ${PROXY_BLOCK}
    return { applyProxyField, errors };`)({});
  assert.equal((await baked.applyProxyField('i', 'socks5://127.0.0.1:50767')).error, false);
});

test('a malformed endpoint is refused and no setting is touched', async () => {
  const m = loadProxy();
  const r = await m.applyProxyField('i', 'tor');
  assert.equal(r.error, true);
  assert.equal(m.errors[0].code, 'INVALID_PROXY');
  assert.equal(m.settings.length, 0);
});

test('a setting another extension or a policy controls is refused, undone and not stored', async () => {
  const m = loadProxy({ levelOfControl: 'controlled_by_other_extensions' });
  const r = await m.applyProxyField('i', 'socks5://127.0.0.1:50767');
  assert.equal(r.error, true);
  assert.equal(m.errors[0].code, 'PROXY_NOT_CONTROLLABLE');
  assert.match(m.errors[0].message, /did not fall back to a direct connection/);
  assert.equal(m.cleared.length, 1, 'our attempt was withdrawn');
  assert.equal(m.store.browserProxy, undefined);
});

// ---- the handlers: order, refusal, report -----------------------------------

function loadHandlers() {
  const tabsHandler = slice('async function handleTabsAction(id, action, data) {',
    'async function getCurrentTabUrl', 'handleTabsAction');
  const navHandler = slice('async function handleNavigationAction(id, action, data) {',
    'async function handleWindowsAction', 'handleNavigationAction');
  const env = makeEnv();
  const factory = new Function('log', 'store', `
    ${env.source}
    const responses = [];
    const sendResponse = (id, action, data) => responses.push({ id, action, data });
    const humanizeNetError = (c) => c ? 'H(' + c + ')' : null;
    const netDigestSince = () => ({ requests: 0, pending: 0 });
    const consoleErrorsSince = () => ({ errors: 0, first: [] });
    const captureServiceWorkers = async () => null;
    const waitForSettle = async () => ({ settled: true, url: 'https://x.test/', title: 'X' });
    const waitForCommit = async () => ({ committed: true, url: 'https://x.test/', title: 'X' });
    const navUrlBefore = async () => 'https://x.test/before';
    const mainFrameOutcome = () => ({});
    const TAB_NAV = new Map();
    let LAST_FOCUSED_WINDOW_ID = 1;
    const resolveTabId = async (data) => (data && data.tabId) || 1;
    browser.tabs = {
      create: async (o) => { log.push('tabs.create'); return { id: 123, url: o.url }; },
      update: async () => { log.push('tabs.update'); },
      get: async (id) => ({ id, url: 'https://x.test/', title: 'X' }),
      query: async () => [],
      goBack: async () => {}, goForward: async () => {}, reload: async () => {},
    };
    browser.windows = { getAll: async () => [] };
    ${PROXY_BLOCK}
    ${tabsHandler}
    ${navHandler}
    return { handleTabsAction, handleNavigationAction, setControl, responses, errors };
  `);
  return { ...factory(env.log, env.store), log: env.log, store: env.store };
}

test('tabs.create switches the browser BEFORE the tab opens and reports the change', async () => {
  const m = loadHandlers();
  await m.handleTabsAction('r1', 'tabs.create',
    { url: 'http://x.onion/', proxy: 'socks5://127.0.0.1:50767' });
  assert.ok(m.log.indexOf('proxy.set') >= 0 && m.log.indexOf('proxy.set') < m.log.indexOf('tabs.create'),
    `the proxy must be set before the first request: ${m.log.join(', ')}`);
  assert.equal(m.responses[0].data.browserProxyChange.endpoint, 'socks5://127.0.0.1:50767');
});

test('tabs.create refused by the proxy layer opens NO tab', async () => {
  const m = loadHandlers();
  await m.handleTabsAction('r1', 'tabs.create', { url: 'http://x.onion/', proxy: 'bogus' });
  assert.ok(!m.log.includes('tabs.create'), 'fail-closed: no tab on a direct path');
  assert.equal(m.errors[0].code, 'INVALID_PROXY');
  assert.equal(m.responses.length, 0);
});

test('tabs.create without a proxy never touches the setting', async () => {
  const m = loadHandlers();
  await m.handleTabsAction('r1', 'tabs.create', { url: 'https://x.test/' });
  assert.deepEqual(m.log, ['tabs.create']);
  assert.equal(m.responses[0].data.browserProxyChange, null);
});

test('navigation.navigate switches the browser BEFORE navigating', async () => {
  const m = loadHandlers();
  await m.handleNavigationAction('r2', 'navigation.navigate',
    { tabId: 5, op: 'goto', url: 'http://x.onion/', proxy: 'socks5://127.0.0.1:50767', wait: 'none' });
  assert.ok(m.log.indexOf('proxy.set') >= 0 && m.log.indexOf('proxy.set') < m.log.indexOf('tabs.update'),
    m.log.join(', '));
  assert.equal(m.responses[0].data.browserProxyChange.off, false);
});

test('navigation.navigate refused by the proxy layer navigates NOTHING', async () => {
  const m = loadHandlers();
  await m.handleNavigationAction('r2', 'navigation.navigate',
    { tabId: 5, op: 'goto', url: 'http://x.onion/', proxy: 'nope' });
  assert.ok(!m.log.includes('tabs.update'));
  assert.equal(m.errors[0].code, 'INVALID_PROXY');
});

test('isProxy follows the browser, not just what we stored', async () => {
  const m = loadProxy();
  assert.equal(await m.browserProxyIsInForce(), false, 'nothing set yet');
  await m.applyProxyField('i', 'socks5://127.0.0.1:50767');
  assert.equal(await m.browserProxyIsInForce(), true);
  m.setControl('controlled_by_other_extensions');       // someone else took the setting over
  assert.equal(await m.browserProxyIsInForce(), false, 'our note says on, the browser says no');
  m.setControl('controlled_by_this_extension');
  await m.applyProxyField('i', '');
  assert.equal(await m.browserProxyIsInForce(), false, 'switched off');
});

test('tabs.routing reports isProxy true while on and false once off', async () => {
  const m = loadHandlers();
  await m.handleTabsAction('r1', 'tabs.create',
    { url: 'http://x.onion/', proxy: 'socks5://127.0.0.1:50767' });
  m.responses.length = 0;
  await m.handleTabsAction('r2', 'tabs.routing', { tabId: 5 });
  assert.equal(m.responses[0].data.isProxy, true);
  await m.handleTabsAction('r3', 'tabs.routing', { tabId: 5, proxy: '' });
  assert.equal(m.responses[1].data.isProxy, false);
});

test('tabs.routing with proxy "" switches it off and reports the state', async () => {
  const m = loadHandlers();
  await m.handleTabsAction('r1', 'tabs.create',
    { url: 'http://x.onion/', proxy: 'socks5://127.0.0.1:50767' });
  m.responses.length = 0;
  await m.handleTabsAction('r3', 'tabs.routing', { tabId: 5, proxy: '' });
  const r = m.responses[0].data;
  assert.equal(r.routed, false);
  assert.equal(r.browserProxy, null, 'it is off now');
  assert.equal(r.browserProxyChange.off, true);
  assert.equal(r.browserProxyChange.was, 'socks5://127.0.0.1:50767');
});

// ---- the glue that undoes it by itself must stay armed ----------------------

test('the socket close, the watchdog, browser start and install each clear the proxy', () => {
  const onclose = slice('ws.onclose = (event) => {', 'ws.onerror', 'ws.onclose');
  const watchdog = slice("if (alarm.name !== 'browserplug-reconnect-watchdog') return;",
    'connectToMCPServer();', 'watchdog');
  const startup = slice('browser.runtime.onStartup.addListener', 'browser.runtime.onInstalled',
    'onStartup');
  const installed = slice('browser.runtime.onInstalled.addListener', 'setupContextMenu();',
    'onInstalled');
  for (const [name, block] of Object.entries({ onclose, watchdog, startup, installed })) {
    assert.match(block, /clearBrowserProxy\(\)/, `${name} must clear a browser-wide proxy`);
  }
});

test('turning the consent off ends a proxy that is running', () => {
  // The listener is declared before the proxy block, so it is checked by shape: the branch
  // for the consent key must invalidate the hello and call clearBrowserProxy on a non-true value.
  const listener = slice("else if (area === 'local' && changes.allowBrowserProxy) {",
    '  });', 'storage.onChanged consent branch');
  assert.match(listener, /invalidateHelloCache\(\)/);
  assert.match(listener, /sendExtensionHello\(\)/);
  assert.match(listener, /newValue !== true\) clearBrowserProxy\(\)/);
});

test('the hello reports the grant and that the scope is the whole browser', () => {
  const block = slice('async function getProxyPermissionState() {', '\n}\n', 'proxy state');
  assert.match(block, /has_proxy_permission: await hasProxyPermission\(\)/);
  assert.match(block, /proxy_supported: true/);
  assert.match(block, /proxy_scope: 'browser'/);
});
