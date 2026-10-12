/*
 * Tests for extension-chromium/scheme-probe.js, the worker half of the colour-scheme probe
 * that lets the toolbar icon follow the browser's light/dark mode, and for offscreen.js,
 * the page half.
 *
 * The hazards are all arming ones: the listener not registered at load (so the offscreen
 * document's first report lands on a worker that is not listening), the document not
 * created, or a report from anywhere but this extension repainting the icon.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const PROBE_SRC = fs.readFileSync(
  new URL('../../extension-chromium/scheme-probe.js', import.meta.url), 'utf-8');
const PAGE_SRC = fs.readFileSync(
  new URL('../../extension-chromium/offscreen.js', import.meta.url), 'utf-8');

const settled = () => new Promise((resolve) => setImmediate(resolve));

function loadProbe({ documents = 0, offscreen = true, createFails = false } = {}) {
  const listeners = [];
  const created = [];
  const sent = [];
  const themes = [];
  const chrome = {
    runtime: {
      id: 'ext-id',
      getURL: (p) => `chrome-extension://ext-id/${p}`,
      getContexts: async () => Array.from({ length: documents }, () => ({})),
      sendMessage: async (m) => { sent.push(m); },
      onMessage: { addListener: (fn) => listeners.push(fn) },
    },
  };
  if (offscreen) {
    chrome.offscreen = {
      createDocument: async (details) => {
        if (createFails) throw new Error('nope');
        created.push(details);
      },
    };
  }
  const g = { chrome, browserplugToolbarIcon: { setTheme: (t) => themes.push(t) } };
  const warned = [];
  // eslint-disable-next-line no-new-func
  new Function('globalThis', 'console', PROBE_SRC)(g, { warn: (...a) => warned.push(a) });
  return { g, listeners, created, sent, themes, warned };
}

test('the message listener is registered synchronously at load', () => {
  const { listeners } = loadProbe();
  assert.equal(listeners.length, 1);
});

test('a missing document is created for MATCH_MEDIA, then asked for its answer', async () => {
  const { created, sent } = loadProbe({ documents: 0 });
  await settled();
  assert.equal(created.length, 1);
  assert.equal(created[0].url, 'offscreen.html');
  assert.deepEqual(created[0].reasons, ['MATCH_MEDIA']);
  assert.ok(created[0].justification);
  assert.deepEqual(sent, [{ type: 'browserplug.colorScheme.query' }]);
});

test('an existing document is not created twice, only queried', async () => {
  const { created, sent } = loadProbe({ documents: 1 });
  await settled();
  assert.equal(created.length, 0);
  assert.equal(sent.length, 1);
});

test('a report from the extension repaints through setTheme', () => {
  const { listeners, themes } = loadProbe();
  listeners[0]({ type: 'browserplug.colorScheme', dark: true }, { id: 'ext-id' });
  listeners[0]({ type: 'browserplug.colorScheme', dark: false }, { id: 'ext-id' });
  assert.deepEqual(themes, ['dark', 'light']);
});

test('a report from another extension, or an unrelated message, is ignored', () => {
  const { listeners, themes } = loadProbe();
  listeners[0]({ type: 'browserplug.colorScheme', dark: true }, { id: 'someone-else' });
  listeners[0]({ type: 'something.else', dark: true }, { id: 'ext-id' });
  listeners[0](undefined, { id: 'ext-id' });
  assert.deepEqual(themes, []);
});

test('no offscreen API: nothing throws and the icon is left on its light seed', async () => {
  const { created, themes } = loadProbe({ offscreen: false });
  await settled();
  assert.deepEqual([created, themes], [[], []]);
});

test('a document that cannot be created is a warning, not an unhandled rejection', async () => {
  const { warned, themes } = loadProbe({ createFails: true });
  await settled();
  assert.equal(warned.length, 1);
  assert.deepEqual(themes, []);
});

// ------------------------------------------------------------------ the offscreen page half

function loadPage(matches) {
  const messages = [];
  const listeners = [];
  const changeListeners = [];
  const mql = { matches, addEventListener: (_t, fn) => changeListeners.push(fn) };
  const window = { matchMedia: (q) => { mql.query = q; return mql; } };
  const chrome = {
    runtime: {
      sendMessage: (m) => { messages.push(m); return Promise.resolve(); },
      onMessage: { addListener: (fn) => listeners.push(fn) },
    },
  };
  // eslint-disable-next-line no-new-func
  new Function('window', 'chrome', PAGE_SRC)(window, chrome);
  return { messages, listeners, changeListeners, mql };
}

test('the page reports on load, on a scheme change, and when asked', () => {
  const { messages, listeners, changeListeners, mql } = loadPage(false);
  assert.equal(mql.query, '(prefers-color-scheme: dark)');
  assert.deepEqual(messages, [{ type: 'browserplug.colorScheme', dark: false }]);

  mql.matches = true;
  changeListeners[0]();
  assert.deepEqual(messages[1], { type: 'browserplug.colorScheme', dark: true });

  listeners[0]({ type: 'browserplug.colorScheme.query' });
  assert.equal(messages.length, 3);
  listeners[0]({ type: 'unrelated' });
  assert.equal(messages.length, 3);
});

test('the two halves agree on the message names', () => {
  for (const name of ['browserplug.colorScheme', 'browserplug.colorScheme.query']) {
    assert.ok(PROBE_SRC.includes(`'${name}'`), `scheme-probe.js lost ${name}`);
    assert.ok(PAGE_SRC.includes(`'${name}'`), `offscreen.js lost ${name}`);
  }
});
