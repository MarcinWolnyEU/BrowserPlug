/*
 * Chromium twin of tests/js/toolbar-icon.test.mjs.
 *
 * Same shared toolbar-icon.js, a service worker's globals. The MV3 worker has neither a
 * theme API nor matchMedia, so it cannot read the toolbar's colour itself: scheme-probe.js
 * (tested in scheme-probe.test.mjs) is told by an offscreen document and hands the answer
 * to setTheme(). What is asserted here is the worker's side of that bargain: it starts on
 * the light set, stays there for good when nothing ever answers, and repaints the state
 * already showing when a theme does arrive.
 *
 * The second half slices this tree's OWN background.js, for the reason the gecko twin gives:
 * the apply() call is arming glue, and every path test above would stay green without it.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const ICON_SRC = fs.readFileSync(
  new URL('../../extension-chromium/toolbar-icon.js', import.meta.url), 'utf-8');
const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');

// Shadow `globalThis` with a fake, as the gecko twin does: the module reads and writes it,
// so this gives each case its own instance with no reset seam in the production file.
// `console` is passed separately: shadowing globalThis does NOT shadow a bare `console`
// inside the module, which would otherwise resolve to node's own and go unrecorded.
function loadModule(fakeGlobal, consoleStub = { warn() {}, error() {} }) {
  // eslint-disable-next-line no-new-func
  const factory = new Function('globalThis', 'console',
    `${ICON_SRC}\nreturn globalThis.browserplugToolbarIcon;`);
  return factory(fakeGlobal, consoleStub);
}

function recordingAction() {
  const calls = [];
  return {
    calls,
    setIcon: (details) => { calls.push(details); return Promise.resolve(); },
    get lastPaths() { return calls.length ? calls[calls.length - 1].path : null; },
  };
}

// An MV3 service worker: shim.js has put the namespace on globalThis.browser, chrome.action
// replaces browserAction, and neither `theme` nor `matchMedia` exists at all.
function workerGlobal({ action = recordingAction() } = {}) {
  return { browser: { action }, action };
}

const settled = () => new Promise((resolve) => setImmediate(resolve));

// ---------------------------------------------------------------------------- paths

test('a worker starts on the light set and its own resolve never moves it', async () => {
  const g = workerGlobal();
  const icon = loadModule(g);
  assert.equal(icon.theme(), 'light', 'seeded synchronously, before any await');
  await settled();
  assert.equal(icon.theme(), 'light', 'no theme API and no matchMedia: nothing to learn');
  assert.equal(g.action.calls.length, 0, 'and so nothing to repaint');
});

test('paths name the themed glyph directories both trees ship', () => {
  const icon = loadModule(workerGlobal());
  assert.deepEqual(icon.pathsFor('connected'), {
    16: 'icons/toolbar/light/connected/icon-16.png',
    24: 'icons/toolbar/light/connected/icon-24.png',
    32: 'icons/toolbar/light/connected/icon-32.png',
    48: 'icons/toolbar/light/connected/icon-48.png',
    64: 'icons/toolbar/light/connected/icon-64.png',
  });
});

test('every state has its own directory; an unknown one paints disconnected', () => {
  const icon = loadModule(workerGlobal());
  assert.deepEqual(icon.STATES, ['connected', 'unhealthy', 'connecting', 'disconnected']);
  for (const state of icon.STATES) {
    assert.equal(icon.pathsFor(state)[16], `icons/toolbar/light/${state}/icon-16.png`);
  }
  assert.equal(icon.pathsFor('wat')[16], 'icons/toolbar/light/disconnected/icon-16.png');
});

// ------------------------------------------------------------------- setTheme (the probe)

test('setTheme("dark") switches the set and repaints the state already showing', () => {
  const g = workerGlobal();
  const icon = loadModule(g);
  icon.apply('connected');
  assert.equal(icon.setTheme('dark'), 'dark');
  assert.equal(g.action.lastPaths[16], 'icons/toolbar/dark/connected/icon-16.png',
    'the repaint has to carry the CURRENT state, not restart at connecting');
  assert.equal(icon.setTheme('light'), 'light');
  assert.equal(g.action.lastPaths[16], 'icons/toolbar/light/connected/icon-16.png');
});

test('setTheme to the theme already showing paints nothing', () => {
  const g = workerGlobal();
  const icon = loadModule(g);
  icon.setTheme('light');
  assert.equal(g.action.calls.length, 0);
});

test('setTheme ignores anything but light or dark, so a bad answer cannot name a missing dir', () => {
  const g = workerGlobal();
  const icon = loadModule(g);
  for (const bad of ['', null, undefined, 'Dark', 'auto', 1]) {
    assert.equal(icon.setTheme(bad), 'light', String(bad));
  }
  assert.equal(g.action.calls.length, 0);
});

// ---------------------------------------------------------------------------- apply

test('apply paints all five sizes through action.setIcon (MV3 has no browserAction)', () => {
  const g = workerGlobal();
  const icon = loadModule(g);
  assert.equal(icon.apply('connecting'), true);
  assert.deepEqual(Object.keys(g.action.lastPaths), ['16', '24', '32', '48', '64']);
  assert.equal(g.action.lastPaths[24], 'icons/toolbar/light/connecting/icon-24.png');
});

test('a rejected setIcon is reported, not swallowed', async () => {
  // MV3 rejects when a path does not resolve. An unhandled rejection in a worker would
  // leave a missing file looking exactly like a state that never changed.
  const warned = [];
  const g = workerGlobal();
  g.browser.action.setIcon = () => Promise.reject(new Error('Could not load icon'));
  const icon = loadModule(g, { warn: (...args) => warned.push(args), error() {} });
  assert.equal(icon.apply('connected'), true, 'the call itself did not throw');
  await settled();
  assert.equal(warned.length, 1);
  assert.match(String(warned[0][1]), /icons\/toolbar\/light\/connected\/icon-16\.png/);
});

test('apply survives an action that is gone (worker teardown) without throwing', () => {
  const g = workerGlobal();
  delete g.browser.action;
  const icon = loadModule(g);
  assert.equal(icon.apply('connected'), false);
});

// ------------------------------------------------------- the arming glue in background.js

function loadUpdateActionIndicator() {
  const start = BG_SRC.indexOf('function updateActionIndicator()');
  const end = BG_SRC.indexOf('function setConnectionPhase(', start);
  assert.ok(start >= 0 && end > start,
    'updateActionIndicator() not found in background.js between its anchors '
    + '("function updateActionIndicator()" .. "function setConnectionPhase(")');
  // eslint-disable-next-line no-new-func
  return new Function('globalThis', 'browser', 'CONFIG', 'connectionPhase', 'serverHealthy',
    'retryAttempts', 'nextRetryAt', `${BG_SRC.slice(start, end)}\nreturn updateActionIndicator();`);
}

const realUpdateActionIndicator = loadUpdateActionIndicator();

function iconStateFor(phase, { serverHealthy = true, retryAttempts = 0 } = {}) {
  const painted = [];
  const fakeGlobal = { browserplugToolbarIcon: { apply: (state) => painted.push(state) } };
  // MV3: the indicator reads browser.action, not browser.browserAction.
  const noopAction = { setBadgeText() {}, setBadgeBackgroundColor() {}, setTitle() {} };
  realUpdateActionIndicator(fakeGlobal, { action: noopAction },
    { hostname: 'localhost', port: 8772, maxRetries: -1 },
    phase, serverHealthy, retryAttempts, null);
  assert.equal(painted.length, 1,
    `updateActionIndicator did not paint an icon for phase "${phase}" — the apply() call is `
    + 'missing from the chromium background.js, and every test above would still pass');
  return painted[0];
}

test('every connection phase paints an icon, and the six collapse onto four states', () => {
  assert.equal(iconStateFor('connected'), 'connected');
  // A probe still in flight is not a failed one: only an answer earns the amber plug.
  assert.equal(iconStateFor('connected', { serverHealthy: null }), 'connecting');
  assert.equal(iconStateFor('connected', { serverHealthy: false }), 'unhealthy');
  assert.equal(iconStateFor('connecting'), 'connecting');
  assert.equal(iconStateFor('waiting', { retryAttempts: 3 }), 'connecting');
  assert.equal(iconStateFor('stopped'), 'disconnected');
  assert.equal(iconStateFor('gaveup', { retryAttempts: 9 }), 'disconnected');
  assert.equal(iconStateFor('anything-else'), 'disconnected');
});

test('every state background.js paints is a state toolbar-icon.js ships art for', () => {
  const icon = loadModule(workerGlobal());
  const cases = [['connected', true], ['connected', false], ['connected', null],
    ['connecting', true], ['waiting', true], ['stopped', true], ['gaveup', true],
    ['disconnected', true]];
  for (const [phase, serverHealthy] of cases) {
    assert.ok(icon.STATES.includes(iconStateFor(phase, { serverHealthy })),
      `phase "${phase}" (serverHealthy=${serverHealthy}) paints a state `
      + 'toolbar-icon.js does not know');
  }
});

test('the toolbar button carries no badge TEXT in any phase', () => {
  // The icon says what the glyphs used to say ("…", "●", the retry count, "‖", "!",
  // "×"), so they were removed rather than shown twice on one button. The write is
  // asserted on EVERY phase, not merely found absent: an in-place extension update
  // leaves the previous build's glyph sitting there until the browser restarts, and
  // writing '' on each pass is what clears it.
  const seen = [];
  const recording = {
    setBadgeText: ({ text }) => seen.push(text),
    setBadgeBackgroundColor() {}, setTitle() {},
  };
  for (const [phase, serverHealthy] of [['connected', true], ['connected', false],
    ['connected', null], ['connecting', true], ['waiting', true], ['stopped', true],
    ['gaveup', true], ['disconnected', true]]) {
    realUpdateActionIndicator(
      { browserplugToolbarIcon: { apply: () => {} } },
      { action: recording }, { hostname: 'localhost', port: 8772, maxRetries: -1 },
      phase, serverHealthy, 7, Date.now() + 5000);
  }
  assert.equal(seen.length, 8, 'every phase writes the badge text, none skips it');
  assert.deepEqual([...new Set(seen)], [''],
    `updateActionIndicator wrote badge text ${JSON.stringify(seen)}`);
});

test('the badge COLOUR and the icon agree: green iff the connected icon', () => {
  // Nothing paints the badge colour any more (the badge has no text), but the popup
  // reads it back as its whole connected/disconnected verdict, so it and the icon are
  // still two renderings of one decision.
  const wrote = {};
  const recording = {
    setBadgeText() {}, setTitle() {},
    setBadgeBackgroundColor: ({ color }) => { wrote.color = color; },
  };
  for (const [phase, serverHealthy] of [['connected', true], ['connected', false],
    ['connected', null], ['connecting', true], ['waiting', true], ['stopped', true],
    ['gaveup', true], ['disconnected', true]]) {
    const painted = [];
    realUpdateActionIndicator(
      { browserplugToolbarIcon: { apply: (s) => painted.push(s) } },
      { action: recording }, { hostname: 'localhost', port: 8772, maxRetries: -1 },
      phase, serverHealthy, 1, null);
    assert.equal(wrote.color === '#2e7d32', painted[0] === 'connected',
      `phase "${phase}" (serverHealthy=${serverHealthy}) paints badge ${wrote.color} with `
      + `icon "${painted[0]}"`);
  }
});
