/*
 * CHROMIUM-ONLY test (no gecko counterpart): the two-realm content-script relay in
 * extension-chromium/content.js (ISOLATED world) + content-main.js (MAIN world).
 *
 * Gecko does console capture + a (dead) fetch wrapper from ONE content script via an
 * inline-<script> injection. Chromium splits it across two declared worlds joined only
 * through the DOM (design D3/D5a):
 *   - content-main.js (MAIN world, CSP-immune): wraps console.* and fetch/XHR, serializes
 *     IN the page, hands each console call and page error to content.js in a synchronous
 *     '__browserplugConsole' event (detail "level:text"), and postMessages
 *     {__browserplugNetBody,...} out.
 *   - content.js (ISOLATED world): batches console entries to the SW (20-entry / 300ms
 *     flush, 500/load cap — the SAME constants as gecko), pulls the armed monitor state at
 *     document_start and forwards SW arm/disarm broadcasts into the MAIN world, and relays
 *     captured bodies up as `net_body`. It also owns the CSP-immune error listeners and the
 *     legacy extractText/HTML/Title/URL messages.
 *
 * Both files run in a jsdom window; message events are dispatched with source===window to
 * model the shared window.postMessage bus, console handovers are dispatched as the event
 * content-main.js sends, and the ISOLATED batch/flush timer is made controllable so the
 * flush cadence is asserted deterministically.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from 'jsdom';

const CONTENT_JS = fs.readFileSync(
  new URL('../../extension-chromium/content.js', import.meta.url), 'utf-8');
const CONTENT_MAIN_JS = fs.readFileSync(
  new URL('../../extension-chromium/content-main.js', import.meta.url), 'utf-8');

function newDom() {
  return new JSDOM('<!DOCTYPE html><html><head><title>Relay Page</title></head><body><p>hi there</p></body></html>',
    { runScripts: 'outside-only', url: 'https://x.test/page' });
}

// Dispatch a window 'message' event whose source IS the window — the check both scripts use
// to accept only same-window postMessages.
function postToWindow(window, data) {
  window.dispatchEvent(new window.MessageEvent('message', { data, source: window }));
}

// What content-main.js dispatches for one console call or page error.
function handOver(window, level, text) {
  window.dispatchEvent(new window.CustomEvent('__browserplugConsole', { detail: level + ':' + text }));
}

// =============================================================================
// content-main.js — the MAIN-world hooks (console + fetch/XHR body wrapper)
// =============================================================================

function loadMainWorld(opts = {}) {
  const dom = newDom();
  const { window } = dom;
  const posted = [];
  window.postMessage = (data) => { posted.push(data); };
  const handed = [];
  window.addEventListener('__browserplugConsole', (ev) => { handed.push(ev.detail); });
  // Silence the ORIGINAL console the hook wraps (it still calls through) so the test's own
  // console.log/error don't print to stdout — the hook captures these as `orig` at wrap time.
  for (const lvl of ['log', 'info', 'warn', 'error', 'debug']) window.console[lvl] = () => {};
  // fetch must be a function BEFORE eval or the wrapper won't install.
  if (opts.fetch !== null) window.fetch = opts.fetch || (async () => ({}));
  if (opts.XHR) window.XMLHttpRequest = opts.XHR;
  window.eval(CONTENT_MAIN_JS);
  return { window, posted, handed, arm: (armed, urlPatterns = []) =>
    postToWindow(window, { __browserplugMonitorState: true, armed, urlPatterns }) };
}

test('the MAIN-world console hook serializes args and hands them over in a __browserplugConsole event', () => {
  const { window, handed, posted } = loadMainWorld();
  window.console.log('hello', { a: 1 }, 42);

  assert.deepEqual(handed, ['log:hello {"a":1} 42'],
    'strings pass through, objects JSON.stringify, primitives String()');
  assert.equal(posted.length, 0, 'the console bus does not use postMessage');
});

test('an Error argument is serialized to its message, not "[object Object]"', () => {
  const { window, handed } = loadMainWorld();
  window.console.error('boom:', new window.Error('it failed'));
  assert.match(handed[0], /^error:boom: it failed/);
});

test('an uncaught page error is handed over as an error-level entry with its location', () => {
  const { window, handed } = loadMainWorld();
  window.dispatchEvent(new window.ErrorEvent('error',
    { message: 'ReferenceError: x is not defined', filename: 'app.js', lineno: 12 }));
  assert.deepEqual(handed, ['error:Uncaught: ReferenceError: x is not defined (app.js:12)']);
});

test('the console hook is installed once — a second injection is a no-op (guard)', () => {
  const dom = newDom();
  const { window } = dom;
  const handed = [];
  window.addEventListener('__browserplugConsole', (ev) => { handed.push(ev.detail); });
  for (const lvl of ['log', 'info', 'warn', 'error', 'debug']) window.console[lvl] = () => {};
  window.fetch = async () => ({});
  window.eval(CONTENT_MAIN_JS);
  window.eval(CONTENT_MAIN_JS); // second injection — __browserplugConsoleHooked guards it
  window.console.log('once');
  assert.equal(handed.length, 1,
    'console.log is handed over exactly once, not twice — the hook did not double-wrap');
});

test('the fetch wrapper captures a matching response body and returns the page fetch UNTOUCHED', async () => {
  const pageResponse = {
    url: 'https://x.test/api', status: 200,
    headers: { get: () => 'application/json' },
    clone: () => ({ text: async () => '{"ok":true}' })
  };
  const origFetch = async () => pageResponse;
  const { window, posted, arm } = loadMainWorld({ fetch: origFetch });
  arm(true, []);

  const returned = await window.fetch('https://x.test/api');
  assert.equal(returned, pageResponse, 'the page gets its own Response object back, untouched');

  await new Promise(r => setTimeout(r, 0)); // let the capture branch resolve
  const body = posted.find(p => p && p.__browserplugNetBody);
  assert.ok(body, 'a body was posted out for the SW to correlate');
  assert.equal(body.url, 'https://x.test/api');
  assert.equal(body.status, 200);
  assert.equal(body.body, '{"ok":true}');
  assert.equal(body.truncated, false);
});

test('the fetch wrapper does NOTHING while disarmed', async () => {
  const origFetch = async () => ({ url: 'https://x.test/api', status: 200,
    headers: { get: () => 'application/json' }, clone: () => ({ text: async () => 'x' }) });
  const { window, posted } = loadMainWorld({ fetch: origFetch });
  // never armed
  await window.fetch('https://x.test/api');
  await new Promise(r => setTimeout(r, 0));
  assert.equal(posted.filter(p => p && p.__browserplugNetBody).length, 0, 'no capture until a monitor arms it');
});

test('the fetch wrapper honours urlPatterns (a non-matching url is not captured)', async () => {
  const make = (url) => ({ url, status: 200, headers: { get: () => 'text/plain' },
                           clone: () => ({ text: async () => 'body' }) });
  const { window, posted, arm } = loadMainWorld({ fetch: async (u) => make(u) });
  arm(true, ['https://x.test/*']);

  await window.fetch('https://x.test/keep');
  await window.fetch('https://other.test/skip');
  await new Promise(r => setTimeout(r, 0));

  const bodies = posted.filter(p => p && p.__browserplugNetBody);
  assert.equal(bodies.length, 1, 'only the matching url is captured');
  assert.equal(bodies[0].url, 'https://x.test/keep');
});

test('a response body over the 1 MB cap is truncated and flagged (MAX_BODY parity)', async () => {
  assert.match(CONTENT_MAIN_JS, /MAX_BODY = 1048576/, 'the 1 MB cap constant is present');
  const big = 'x'.repeat(1048576 + 50);
  const origFetch = async () => ({ url: 'https://x.test/big', status: 200,
    headers: { get: () => 'text/plain' }, clone: () => ({ text: async () => big }) });
  const { window, posted, arm } = loadMainWorld({ fetch: origFetch });
  arm(true, []);

  await window.fetch('https://x.test/big');
  await new Promise(r => setTimeout(r, 0));
  const body = posted.find(p => p && p.__browserplugNetBody);
  assert.equal(body.body.length, 1048576, 'sliced to the 1 MB cap');
  assert.equal(body.truncated, true);
});

test('the XHR wrapper captures a text response on load and posts {__browserplugNetBody,...}', async () => {
  const loadListeners = [];
  class FakeXHR {
    constructor() { this.responseType = ''; this.status = 200; this.responseText = ''; this.responseURL = ''; }
    open() {}
    send() {}
    addEventListener(type, fn) { if (type === 'load') loadListeners.push(fn); }
    getResponseHeader() { return 'application/json'; }
  }
  const { window, posted, arm } = loadMainWorld({ XHR: FakeXHR });
  arm(true, []);

  const xhr = new window.XMLHttpRequest();
  xhr.open('POST', 'https://x.test/xhr');
  assert.equal(xhr.__browserplugMethod, 'POST', 'open() records the method for the capture');
  assert.equal(xhr.__browserplugUrl, 'https://x.test/xhr');
  xhr.responseText = '{"xhr":1}';
  xhr.responseURL = 'https://x.test/xhr';
  xhr.send();
  loadListeners.forEach(fn => fn.call(xhr)); // simulate the response arriving

  const body = posted.find(p => p && p.__browserplugNetBody);
  assert.ok(body, 'the XHR body was posted');
  assert.equal(body.method, 'POST');
  assert.equal(body.body, '{"xhr":1}');
  assert.equal(body.url, 'https://x.test/xhr');
});

// =============================================================================
// content.js — the ISOLATED-world relay (batching, monitor-state, net_body, legacy)
// =============================================================================

function loadIsolatedWorld(opts = {}) {
  const dom = newDom();
  const { window } = dom;
  const posted = [];
  const realPost = window.postMessage.bind(window);
  window.postMessage = (data, origin) => { posted.push(data); };

  // Controllable flush timer (content.js uses bare setTimeout(flush, 300)).
  let flushCb = null;
  window.setTimeout = (fn) => { flushCb = fn; return 1; };
  window.clearTimeout = () => {};

  const sent = [];
  let onMessageListener = null;
  const monitorState = opts.monitorState === undefined ? null : opts.monitorState;
  window.browser = {
    runtime: {
      sendMessage: (msg) => {
        sent.push(msg);
        if (msg && msg.type === 'get_monitor_state') return Promise.resolve(monitorState);
        return Promise.resolve();
      },
      onMessage: { addListener: (fn) => { onMessageListener = fn; } }
    }
  };

  window.eval(CONTENT_JS);
  return {
    window, posted, sent,
    flush: () => { if (flushCb) flushCb(); },
    fireOnMessage: (req) => new Promise((resolve) => {
      const ret = onMessageListener(req, { tab: { id: 1 } }, resolve);
      if (ret !== true) resolve(undefined);
    }),
    consoleEntries: () => sent.filter(m => m && m.type === 'console_entries').flatMap(m => m.entries),
  };
}

test('content.js pulls the armed monitor state at document_start and forwards it to the MAIN world', async () => {
  const iso = loadIsolatedWorld({ monitorState: { armed: true, urlPatterns: ['https://x.test/*'] } });
  await new Promise(r => setImmediate(r)); // let the get_monitor_state promise resolve

  assert.ok(iso.sent.some(m => m && m.type === 'get_monitor_state'),
    'it asks the SW for the current arm state at startup');
  const fwd = iso.posted.find(p => p && p.__browserplugMonitorState);
  assert.ok(fwd, 'and forwards it into the MAIN world');
  assert.equal(fwd.armed, true);
  assert.deepEqual(fwd.urlPatterns, ['https://x.test/*']);
});

test('a console entry from the MAIN world flushes to the SW at the 20-entry batch threshold', () => {
  const iso = loadIsolatedWorld();
  for (let i = 0; i < 19; i++) handOver(iso.window, 'log', `line ${i}`);
  assert.equal(iso.sent.filter(m => m && m.type === 'console_entries').length, 0,
    '19 entries stay buffered — the batch flushes at 20');

  handOver(iso.window, 'log', 'line 19');
  const flushed = iso.sent.filter(m => m && m.type === 'console_entries');
  assert.equal(flushed.length, 1, 'the 20th entry triggers an immediate flush');
  assert.equal(flushed[0].entries.length, 20);
});

test('a sub-batch of console entries flushes on the 300ms timer', () => {
  const iso = loadIsolatedWorld();
  for (let i = 0; i < 3; i++) handOver(iso.window, 'warn', `w${i}`);
  assert.equal(iso.consoleEntries().length, 0, 'nothing flushed yet (below the batch threshold)');

  iso.flush(); // the 300ms timer fires
  const entries = iso.consoleEntries();
  assert.equal(entries.length, 3, 'the timer flush delivers the buffered entries');
  assert.equal(entries[0].text, 'w0');
});

test('console capture is capped at 500 entries per page load (plus one cap-notice)', () => {
  const iso = loadIsolatedWorld();
  for (let i = 0; i < 505; i++) handOver(iso.window, 'log', `e${i}`);
  iso.flush();
  const entries = iso.consoleEntries();
  assert.equal(entries.length, 501, '500 captured + 1 "cap reached" notice; the rest are dropped');
  assert.match(entries[500].text, /cap reached/, 'the final entry warns that capture stopped');
});

test('the CSP-immune uncaught-error listener records the error into the console batch', () => {
  const iso = loadIsolatedWorld();
  iso.window.dispatchEvent(new iso.window.ErrorEvent('error',
    { message: 'ReferenceError: x is not defined', filename: 'app.js', lineno: 12 }));
  iso.flush();
  const entries = iso.consoleEntries();
  assert.ok(entries.some(e => /Uncaught: ReferenceError: x is not defined \(app\.js:12\)/.test(e.text)),
    'the uncaught error is captured as an error-level console entry with its location');
});

test('an unhandledrejection is recorded into the console batch', () => {
  const iso = loadIsolatedWorld();
  const ev = new iso.window.Event('unhandledrejection');
  ev.reason = new iso.window.Error('promise blew up');
  iso.window.dispatchEvent(ev);
  iso.flush();
  assert.ok(iso.consoleEntries().some(e => /Unhandled rejection: promise blew up/.test(e.text)));
});

test('a captured body from the MAIN world is relayed to the SW as a net_body message', () => {
  const iso = loadIsolatedWorld();
  postToWindow(iso.window, { __browserplugNetBody: true, url: 'https://x.test/api', method: 'POST',
    status: 201, contentType: 'application/json', body: '{"id":9}', truncated: false, ts: 1234 });

  const relayed = iso.sent.find(m => m && m.type === 'net_body');
  assert.ok(relayed, 'the body is relayed up to the SW for correlation');
  // relayed.entry is built in the jsdom realm, so compare fields (not deepStrictEqual,
  // which also checks prototype identity across realms).
  const e = relayed.entry;
  assert.equal(e.url, 'https://x.test/api');
  assert.equal(e.method, 'POST');
  assert.equal(e.status, 201);
  assert.equal(e.contentType, 'application/json');
  assert.equal(e.body, '{"id":9}');
  assert.equal(e.truncated, false);
  assert.equal(e.ts, 1234);
});

test('an SW browserplug_monitor_state broadcast is forwarded into the MAIN world', async () => {
  const iso = loadIsolatedWorld();
  await iso.fireOnMessage({ action: 'browserplug_monitor_state', armed: true, urlPatterns: ['*'] });
  const fwd = iso.posted.find(p => p && p.__browserplugMonitorState && p.armed === true);
  assert.ok(fwd, 'the arm/disarm broadcast reaches the MAIN-world wrapper');
  assert.deepEqual(fwd.urlPatterns, ['*']);
});

test('the legacy extract messages still answer (getPageTitle / getPageURL / extractHTML)', async () => {
  const iso = loadIsolatedWorld();
  const title = await iso.fireOnMessage({ action: 'getPageTitle' });
  assert.equal(title.title, 'Relay Page');

  const url = await iso.fireOnMessage({ action: 'getPageURL' });
  assert.equal(url.url, 'https://x.test/page');

  const html = await iso.fireOnMessage({ action: 'extractHTML' });
  assert.match(html.html, /<p>hi there<\/p>/);
  assert.equal(html.title, 'Relay Page');
});

test('an unknown content action reports an error rather than silently succeeding', async () => {
  const iso = loadIsolatedWorld();
  const res = await iso.fireOnMessage({ action: 'no_such_action' });
  assert.match(res.error, /Unknown content action: no_such_action/);
});

// content-relay uses only chrome (`globalThis.browser ?? chrome`) in production; the shim
// line must be present so the ISOLATED script runs under Chromium's chrome-only content world.
test('content.js carries the browser||chrome portability shim', () => {
  assert.match(CONTENT_JS, /const browser = globalThis\.browser \?\? chrome;/,
    'the shim lets the same relay logic run in a chrome-only content world');
});
