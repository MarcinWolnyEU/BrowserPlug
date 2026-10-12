/*
 * Unit tests for extension-gecko/toolbar-icon.js — the toolbar button's picture — plus the
 * glue in extension-gecko/background.js that arms it.
 *
 * Two hazards, and the second is why this file slices background.js at all.
 *
 * 1. The PATH. setIcon() takes strings; a wrong one is not an exception, it is an icon that
 *    silently never changes, which looks exactly like a connection state that never moved.
 *    Firefox ships two glyph sets and the theme decides which; get the segment wrong and
 *    every path names a directory the XPI does not contain. So the paths are asserted
 *    literally here, and tests/unit/test_icon_assets.py checks the same strings against the
 *    files on disk.
 *
 * 2. The ARMING. This codebase's recurring failure is arming code staying dead behind
 *    hundreds of green helper tests (see the refstale twins, and parity.toml's note on
 *    them). Testing pathsFor() alone would pass with the apply() call deleted from
 *    updateActionIndicator(), so the last block here runs the REAL updateActionIndicator,
 *    sliced out of background.js on the anchors popup.test.mjs already relies on, and
 *    asserts each of the six connection phases reaches apply() with the right state.
 *
 * The chromium twin is tests/js-chromium/toolbar-icon.test.mjs: same shared module, an MV3
 * worker's globals (no theme API, no matchMedia; the theme is handed in through setTheme()),
 * and its own background.js.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const ICON_SRC = fs.readFileSync(
  new URL('../../extension-gecko/toolbar-icon.js', import.meta.url), 'utf-8');
const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

// toolbar-icon.js is an IIFE that reads and writes `globalThis`. Passing a fake object
// under that name SHADOWS the real global inside the module, so each case gets its own
// browser namespace, its own matchMedia and its own module instance — no reset seam in
// the production file, and no state carried between tests.
// `console` is passed separately: shadowing globalThis does NOT shadow a bare `console`
// inside the module, which would otherwise resolve to node's own and go unrecorded.
function loadModule(fakeGlobal, consoleStub = { warn() {}, error() {} }) {
  // eslint-disable-next-line no-new-func
  const factory = new Function('globalThis', 'console',
    `${ICON_SRC}\nreturn globalThis.browserplugToolbarIcon;`);
  return factory(fakeGlobal, consoleStub);
}

// A browserAction that records what was painted. setIcon resolves, as Firefox's does.
function recordingAction() {
  const calls = [];
  return {
    calls,
    setIcon: (details) => { calls.push(details); return Promise.resolve(); },
    get lastPaths() { return calls.length ? calls[calls.length - 1].path : null; },
  };
}

// A Firefox background page: browser.theme present, matchMedia present.
function firefoxGlobal({ theme = {}, prefersDark = false, action = recordingAction() } = {}) {
  const themeListeners = [];
  const mediaListeners = [];
  const mql = {
    matches: prefersDark,
    addEventListener: (_type, fn) => mediaListeners.push(fn),
  };
  return {
    browser: {
      browserAction: action,
      theme: {
        getCurrent: () => Promise.resolve(theme),
        onUpdated: { addListener: (fn) => themeListeners.push(fn) },
      },
    },
    matchMedia: () => mql,
    // test handles
    action, mql, themeListeners, mediaListeners,
  };
}

const DARK_THEME = { colors: { toolbar: '#1c1b22', toolbar_text: '#fbfbfe' } };
const LIGHT_THEME = { colors: { toolbar: '#f9f9fb', toolbar_text: '#15141a' } };

// A promise chain settles in microtasks; nothing here uses a timer.
const settled = () => new Promise((resolve) => setImmediate(resolve));

// ---------------------------------------------------------------------------- paths

test('pathsFor names one file per shipped size, under the resolved theme', () => {
  const g = firefoxGlobal();
  const icon = loadModule(g);
  assert.deepEqual(icon.SIZES, [16, 24, 32, 48, 64]);
  assert.deepEqual(icon.pathsFor('connected'), {
    16: 'icons/toolbar/light/connected/icon-16.png',
    24: 'icons/toolbar/light/connected/icon-24.png',
    32: 'icons/toolbar/light/connected/icon-32.png',
    48: 'icons/toolbar/light/connected/icon-48.png',
    64: 'icons/toolbar/light/connected/icon-64.png',
  });
});

test('every state has its own directory', () => {
  const icon = loadModule(firefoxGlobal());
  assert.deepEqual(icon.STATES, ['connected', 'unhealthy', 'connecting', 'disconnected']);
  for (const state of icon.STATES) {
    assert.equal(icon.pathsFor(state)[16], `icons/toolbar/light/${state}/icon-16.png`);
  }
});

test('an unknown state paints disconnected rather than a path that does not exist', () => {
  const icon = loadModule(firefoxGlobal());
  assert.equal(icon.pathsFor('wat')[32], 'icons/toolbar/light/disconnected/icon-32.png');
  assert.equal(icon.pathsFor(undefined)[32], 'icons/toolbar/light/disconnected/icon-32.png');
});

// ------------------------------------------------------------------- theme resolution

test('the FIRST paint is light, before any await can have resolved', () => {
  // Firefox ships two sets, and a path is needed synchronously. Guessing the tree's OTHER
  // set here would 404 for as long as getCurrent() takes to answer.
  const g = firefoxGlobal({ theme: DARK_THEME, prefersDark: true });
  const icon = loadModule(g);
  assert.equal(icon.theme(), 'light');
});

test('a dark theme resolves to the dark glyph set and repaints', async () => {
  const g = firefoxGlobal({ theme: DARK_THEME });
  const icon = loadModule(g);
  await settled();
  assert.equal(icon.theme(), 'dark');
  assert.equal(icon.pathsFor('connected')[16], 'icons/toolbar/dark/connected/icon-16.png');
  // The load-time resolve repainted on its own, without waiting for a phase change.
  assert.equal(g.action.lastPaths[16], 'icons/toolbar/dark/connecting/icon-16.png');
});

test('a light theme stays on the light set and does NOT repaint', async () => {
  const g = firefoxGlobal({ theme: LIGHT_THEME });
  const icon = loadModule(g);
  await settled();
  assert.equal(icon.theme(), 'light');
  assert.equal(g.action.calls.length, 0, 'nothing changed, so nothing should have been painted');
});

test('theme colours: the toolbar INK decides, and it outranks the surface', async () => {
  // A theme that styles its toolbar icons has already answered the question. This one
  // contradicts itself (light ink on a light surface); the ink is what the glyph sits next to.
  const g = firefoxGlobal({ theme: { colors: { toolbar: '#ffffff', icons: '#f0f0f4' } } });
  const icon = loadModule(g);
  await settled();
  assert.equal(icon.theme(), 'dark');
});

test('theme colours: surface luminance decides when the theme sets no ink', async () => {
  for (const [toolbar, expected] of [['#2b2a33', 'dark'], ['rgb(20, 20, 20)', 'dark'],
    ['#f0f0f4', 'light'], ['rgba(255, 255, 255, 1)', 'light'], [[28, 27, 34], 'dark']]) {
    const g = firefoxGlobal({ theme: { colors: { toolbar } } });
    const icon = loadModule(g);
    await settled();
    assert.equal(icon.theme(), expected, `toolbar ${JSON.stringify(toolbar)}`);
  }
});

test('theme colours: frame is read when there is no toolbar colour', async () => {
  const g = firefoxGlobal({ theme: { colors: { frame: '#000000' } } });
  const icon = loadModule(g);
  await settled();
  assert.equal(icon.theme(), 'dark');
});

test('the DEFAULT theme returns no colors, so prefers-color-scheme answers', async () => {
  // This is the case that matters most: "System theme — auto" is the setting that actually
  // follows the OS, and getCurrent() says nothing about it.
  for (const prefersDark of [true, false]) {
    const g = firefoxGlobal({ theme: {}, prefersDark });
    const icon = loadModule(g);
    await settled();
    assert.equal(icon.theme(), prefersDark ? 'dark' : 'light');
  }
});

test('a theme whose colours say nothing useable also falls back to the media query', async () => {
  const g = firefoxGlobal({ theme: { colors: { tab_line: 'not-a-colour' } }, prefersDark: true });
  const icon = loadModule(g);
  await settled();
  assert.equal(icon.theme(), 'dark');
});

test('a getCurrent that throws or rejects falls back rather than leaving the icon wrong', async () => {
  for (const theme of ['throw', 'reject']) {
    const g = firefoxGlobal({ prefersDark: true });
    g.browser.theme.getCurrent = () => {
      if (theme === 'throw') throw new Error('no theme permission');
      return Promise.reject(new Error('gone'));
    };
    const icon = loadModule(g);
    await settled();
    assert.equal(icon.theme(), 'dark', theme);
  }
});

// ------------------------------------------------------------------------ re-picking

test('theme.onUpdated re-picks the set and repaints the state already showing', async () => {
  const g = firefoxGlobal({ theme: LIGHT_THEME });
  const icon = loadModule(g);
  await settled();
  icon.apply('connected');
  assert.equal(g.action.lastPaths[16], 'icons/toolbar/light/connected/icon-16.png');

  g.browser.theme.getCurrent = () => Promise.resolve(DARK_THEME);
  assert.equal(g.themeListeners.length, 1, 'theme.onUpdated listener was never registered');
  g.themeListeners[0]({ theme: DARK_THEME });
  await settled();

  assert.equal(icon.theme(), 'dark');
  assert.equal(g.action.lastPaths[16], 'icons/toolbar/dark/connected/icon-16.png',
    'the repaint has to carry the CURRENT state, not restart at connecting');
});

test('the media query change re-picks too — the OS flipping under "System theme — auto"', async () => {
  const g = firefoxGlobal({ theme: {}, prefersDark: false });
  const icon = loadModule(g);
  await settled();
  assert.equal(icon.theme(), 'light');

  assert.equal(g.mediaListeners.length, 1, 'no change listener on the media query');
  g.mql.matches = true;
  g.mediaListeners[0]();
  await settled();
  assert.equal(icon.theme(), 'dark');
});

test('a browser that can answer nothing stays on the light set instead of naming no directory', async () => {
  const g = firefoxGlobal();
  delete g.browser.theme;
  g.matchMedia = undefined;
  const icon = loadModule(g);
  await settled();
  assert.equal(icon.theme(), 'light');
  assert.equal(icon.pathsFor('connected')[16], 'icons/toolbar/light/connected/icon-16.png');
});

test('setTheme adopts a theme worked out elsewhere, and ignores nonsense', () => {
  const g = firefoxGlobal();
  const icon = loadModule(g);
  assert.equal(icon.setTheme('dark'), 'dark');
  assert.equal(icon.setTheme('purple'), 'dark');
  assert.equal(icon.setTheme(null), 'dark');
});

// ---------------------------------------------------------------------------- apply

test('apply paints all five sizes through browserAction.setIcon', () => {
  const g = firefoxGlobal();
  const icon = loadModule(g);
  assert.equal(icon.apply('connected'), true);
  assert.deepEqual(Object.keys(g.action.lastPaths), ['16', '24', '32', '48', '64']);
});

test('apply survives a browserAction that is gone (teardown) without throwing', () => {
  const g = firefoxGlobal();
  delete g.browser.browserAction;
  const icon = loadModule(g);
  assert.equal(icon.apply('connected'), false);
});

// ------------------------------------------------------- the arming glue in background.js

// The real updateActionIndicator(), sliced out on the same anchors popup.test.mjs uses
// (invariant E checks they exist in BOTH trees). Its globals become parameters.
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

// Run the real function for one phase and report which icon state reached apply().
function iconStateFor(phase, { serverHealthy = true, retryAttempts = 0 } = {}) {
  const painted = [];
  const fakeGlobal = { browserplugToolbarIcon: { apply: (state) => painted.push(state) } };
  const noopAction = {
    setBadgeText() {}, setBadgeBackgroundColor() {}, setTitle() {},
  };
  realUpdateActionIndicator(fakeGlobal, { browserAction: noopAction },
    { hostname: 'localhost', port: 8772, maxRetries: -1 },
    phase, serverHealthy, retryAttempts, null);
  assert.equal(painted.length, 1,
    `updateActionIndicator did not paint an icon for phase "${phase}" — the apply() call is `
    + 'missing from background.js, and every helper test above would still pass');
  return painted[0];
}

test('every connection phase paints an icon, and the six collapse onto four states', () => {
  assert.equal(iconStateFor('connected'), 'connected');
  // The first probe is still in flight: indistinguishable from connecting, and over in
  // under a second. Only a probe that ANSWERED with a failure earns the unhealthy plug.
  assert.equal(iconStateFor('connected', { serverHealthy: null }), 'connecting');
  assert.equal(iconStateFor('connected', { serverHealthy: false }), 'unhealthy');
  assert.equal(iconStateFor('connecting'), 'connecting');
  assert.equal(iconStateFor('waiting', { retryAttempts: 3 }), 'connecting');
  assert.equal(iconStateFor('stopped'), 'disconnected');
  assert.equal(iconStateFor('gaveup', { retryAttempts: 9 }), 'disconnected');
  assert.equal(iconStateFor('anything-else'), 'disconnected');
});

test('every state background.js paints is a state toolbar-icon.js ships art for', () => {
  const icon = loadModule(firefoxGlobal());
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
      { browserAction: recording }, { hostname: 'localhost', port: 8772, maxRetries: -1 },
      phase, serverHealthy, 7, Date.now() + 5000);
  }
  assert.equal(seen.length, 8, 'every phase writes the badge text, none skips it');
  assert.deepEqual([...new Set(seen)], [''],
    `updateActionIndicator wrote badge text ${JSON.stringify(seen)}`);
});

test('the badge COLOUR and the icon agree: green iff the connected icon', () => {
  // Nothing paints the badge colour any more (the badge has no text), but the popup
  // reads it back as its whole connected/disconnected verdict, so it and the icon are
  // still two renderings of one decision. A phase that reads green on one and amber on
  // the other is the bug this pins — and now it is the popup that shows it.
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
      { browserAction: recording }, { hostname: 'localhost', port: 8772, maxRetries: -1 },
      phase, serverHealthy, 1, null);
    assert.equal(wrote.color === '#2e7d32', painted[0] === 'connected',
      `phase "${phase}" (serverHealthy=${serverHealthy}) paints badge ${wrote.color} with `
      + `icon "${painted[0]}"`);
  }
});
