/*
 * The GLUE between requests(action="get") and body-censor.js, in
 * extension-gecko/background.js: censorRequestBodies() and the call to it inside
 * handleRequestsAction's get_content case.
 *
 * tests/js/body-censor.test.mjs proves the module. This proves it is still ARMED:
 * that a get_content answer really goes through it, reads the settings on every get,
 * and refuses rather than answers raw when storage or the module fails. Deleting the
 * `await censorRequestBodies(contentResult)` line leaves every module test green and
 * turns the tests below red.
 *
 * Like requests-capture.test.mjs, it slices the monitoring block out of background.js
 * (the glue sits inside it) and instantiates it with stubbed globals.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import '../../extension-gecko/body-censor.js';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');
const CENSOR = globalThis.BrowserplugBodyCensor;

function loadModule() {
  const start = BG_SRC.indexOf('// Request monitoring state');
  const end = BG_SRC.indexOf('// Test helper action handler', start);
  assert.ok(start >= 0 && end > start, 'request-monitoring block not found in background.js');
  const block = BG_SRC.slice(start, end);
  assert.ok(block.includes('async function censorRequestBodies('),
    'the censorship glue is no longer inside the sliced monitoring block');

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const console = { log() {}, warn() {}, error() {} };
    const responses = [];
    const errors = [];
    let isConnected = true;
    const TAB_REQUESTS = new Map();
    const STATIC_TYPES = new Set();
    const resolveTabId = async (data) => (data && data.tabId) || 1;
    const sendResponse = (id, action, data) => responses.push({ id, action, data });
    const sendError = (id, code, message) => errors.push({ id, code, message });
    const installed = new Map();
    const evt = (name) => ({
      addListener: (fn) => installed.set(name, fn),
      removeListener: (fn) => { if (installed.get(name) === fn) installed.delete(name); }
    });
    const syncStore = {};
    const syncState = { fail: false, reads: 0 };
    const storageListeners = [];
    const browser = {
      webRequest: {
        onBeforeRequest: evt('onBeforeRequest'),
        onBeforeSendHeaders: evt('onBeforeSendHeaders'),
        onHeadersReceived: evt('onHeadersReceived'),
        onCompleted: evt('onCompleted'),
        onErrorOccurred: evt('onErrorOccurred'),
        filterResponseData: () => ({ write() {}, disconnect() {}, close() {} })
      },
      storage: { sync: { async get(defaults) {
        syncState.reads++;
        if (syncState.fail) throw new Error('storage unavailable');
        const out = {};
        for (const k of Object.keys(defaults)) out[k] = k in syncStore ? syncStore[k] : defaults[k];
        return out;
      } },
      onChanged: { addListener: (fn) => storageListeners.push(fn) } }
    };

    ${block}

    return { handleRequestsAction, captureRequestEvent, capturedResponseBodies, installed,
             stopMonitorSweeper, responses, errors, syncStore, syncState,
             // What Options does: write the key and fire storage.onChanged.
             setSetting: (key, value) => {
               syncStore[key] = value;
               for (const fn of storageListeners) fn({ [key]: { newValue: value } }, 'sync');
             } };
  `);
  return factory();
}

let seq = 0;
const bytesOf = (s) => new TextEncoder().encode(s).buffer;

// One captured request through the real event path: a JSON login POST whose answer is
// a token document. Returns the monitor and request ids.
async function capture(mod, t, { requestBody, responseBody, requestType = 'application/json' } = {}) {
  t.after(() => mod.stopMonitorSweeper());
  const startId = `s${seq++}`;
  await mod.handleRequestsAction(startId, 'requests.start_monitoring',
    { url_patterns: ['*'], options: { capture_response_bodies: true, max_body_size: 50000 } });
  const monitorId = mod.responses.find((r) => r.id === startId).data.monitor_id;
  const requestId = `r${seq++}`;
  const base = { requestId, url: 'https://idp.example/login', method: 'POST', tabId: 1,
                 frameId: 0, type: 'xmlhttprequest', timeStamp: Date.now() };
  mod.captureRequestEvent(monitorId, 'onBeforeRequest', { ...base,
    requestBody: { raw: [{ bytes: bytesOf(requestBody) }] } });
  mod.installed.get('onBeforeSendHeaders')({ ...base, requestHeaders: [
    { name: 'Content-Type', value: requestType },
    { name: 'Authorization', value: 'Bearer live-key' }] });
  mod.captureRequestEvent(monitorId, 'onHeadersReceived', { ...base, statusCode: 200,
    responseHeaders: [{ name: 'Content-Type', value: 'application/json' }] });
  mod.captureRequestEvent(monitorId, 'onCompleted', { ...base, statusCode: 200 });
  mod.capturedResponseBodies.set(requestId, {
    response_body: responseBody, content_type: 'application/json', encoding: 'utf-8',
    content_length: responseBody.length, truncated: false });
  return { monitorId, requestId };
}

async function get(mod, ids) {
  const id = `g${seq++}`;
  await mod.handleRequestsAction(id, 'requests.get_content',
    { monitor_id: ids.monitorId, request_id: ids.requestId });
  return {
    response: mod.responses.find((r) => r.id === id),
    error: mod.errors.find((r) => r.id === id)
  };
}

const LOGIN = '{"user":"bob","password":"hunter2"}';
const TOKENS = '{"access_token":"tok-1","refresh_token":"tok-2","expires_in":3600}';

test('glue: by default get_content returns bodies RAW and carries no marker', async (t) => {
  const mod = loadModule();
  const ids = await capture(mod, t, { requestBody: LOGIN, responseBody: TOKENS });
  const { response } = await get(mod, ids);
  assert.equal(response.data.request_body.content, LOGIN);
  assert.equal(response.data.response_body.content, TOKENS);
  assert.equal(response.data.values_censored, undefined);
  assert.equal(response.data.values_censored_note, undefined);
});

test('glue: the toggle sends BOTH bodies through the censor and adds the marker', async (t) => {
  const mod = loadModule();
  const ids = await capture(mod, t, { requestBody: LOGIN, responseBody: TOKENS });
  mod.syncStore.censorSecrets = true;
  const { response } = await get(mod, ids);
  const d = response.data;
  assert.equal(d.request_body.content, '{"user":"bob","password":"<redacted>"}');
  assert.equal(d.response_body.content,
    '{"access_token":"<redacted>","refresh_token":"<redacted>","expires_in":3600}');
  assert.equal(d.values_censored, 3);
  assert.equal(d.values_censored_note, '3 value(s) censored by your Options > General setting');
  assert.match(d.response_body.note, /2 value\(s\) censored by your Options > General setting/);
  // Header redaction is a separate mechanism and is untouched.
  assert.equal(d.request_headers.Authorization, '<redacted>');
  assert.equal(d.request_headers['Content-Type'], 'application/json');
});

test('glue: a stored custom pattern does nothing while the toggle is OFF', async (t) => {
  const mod = loadModule();
  const ids = await capture(mod, t, { requestBody: LOGIN, responseBody: TOKENS });
  mod.syncStore.censorPatterns = ['tok-\\d'];
  const { response } = await get(mod, ids);
  assert.equal(response.data.request_body.content, LOGIN);
  assert.equal(response.data.response_body.content, TOKENS);
  assert.equal(response.data.values_censored, undefined);
});

test('glue: a custom pattern applies once the toggle is ON', async (t) => {
  const mod = loadModule();
  const ids = await capture(mod, t, { requestBody: LOGIN, responseBody: TOKENS });
  mod.syncStore.censorSecrets = true;
  mod.syncStore.censorPatterns = ['tok-\\d'];
  const { response } = await get(mod, ids);
  assert.equal(response.data.request_body.content, '{"user":"bob","password":"<redacted>"}');
  assert.equal(response.data.response_body.content,
    '{"access_token":"<redacted>","refresh_token":"<redacted>","expires_in":3600}');
});

test('glue: the setting is read on every get, so it covers traffic already captured', async (t) => {
  const mod = loadModule();
  const ids = await capture(mod, t, { requestBody: LOGIN, responseBody: TOKENS });
  const first = (await get(mod, ids)).response.data;
  assert.equal(first.request_body.content, LOGIN);
  mod.syncStore.censorSecrets = true;
  const second = (await get(mod, ids)).response.data;
  assert.equal(second.request_body.content, '{"user":"bob","password":"<redacted>"}');
  mod.syncStore.censorSecrets = false;
  const third = (await get(mod, ids)).response.data;
  assert.equal(third.request_body.content, LOGIN);
  // The stored capture was never rewritten.
  assert.equal(mod.capturedResponseBodies.get(ids.requestId).response_body, TOKENS);
});

test('glue: an invalid or dangerous stored pattern is ignored, the rest still applies', async (t) => {
  const mod = loadModule();
  const ids = await capture(mod, t, { requestBody: LOGIN, responseBody: TOKENS });
  mod.syncStore.censorSecrets = true;
  mod.syncStore.censorPatterns = ['[', '(a+)+$', 'bo[b]'];
  const { response, error } = await get(mod, ids);
  assert.equal(error, undefined);
  // The password is the built-in masking's, "bob" the surviving pattern's.
  assert.equal(response.data.request_body.content, '{"user":"<redacted>","password":"<redacted>"}');
  assert.equal(response.data.values_censored, 4);
});

test('glue: a storage read that fails with nothing read before refuses, never answers raw', async (t) => {
  const mod = loadModule();
  const ids = await capture(mod, t, { requestBody: LOGIN, responseBody: TOKENS });
  mod.syncStore.censorSecrets = true;
  mod.syncState.fail = true;
  const { response, error } = await get(mod, ids);
  assert.equal(response, undefined, 'a raw body was sent although censoring may be on');
  assert.equal(error.code, 'CENSOR_UNAVAILABLE');
  assert.match(error.message, /could not read the body censoring settings/);
});

test('glue: a failed read after a good one uses the settings last read', async (t) => {
  const mod = loadModule();
  const ids = await capture(mod, t, { requestBody: LOGIN, responseBody: TOKENS });
  mod.syncStore.censorSecrets = true;
  assert.equal((await get(mod, ids)).response.data.values_censored > 0, true);
  mod.syncState.fail = true;
  const { response, error } = await get(mod, ids);
  assert.equal(error, undefined);
  assert.equal(response.data.request_body.content, '{"user":"bob","password":"<redacted>"}');
});

test('glue: a settings change forgets the last read, so a failed read after it refuses', async (t) => {
  const mod = loadModule();
  const ids = await capture(mod, t, { requestBody: LOGIN, responseBody: TOKENS });
  await get(mod, ids);                       // censoring off, remembered
  mod.setSetting('censorSecrets', true);     // the user switches it on
  mod.syncState.fail = true;
  const { response, error } = await get(mod, ids);
  assert.equal(response, undefined, 'the remembered "off" outlived the switch to on');
  assert.equal(error.code, 'CENSOR_UNAVAILABLE');
});

test('glue: censoring switched on but the module missing refuses, never answers raw', async (t) => {
  const mod = loadModule();
  const ids = await capture(mod, t, { requestBody: LOGIN, responseBody: TOKENS });
  mod.syncStore.censorSecrets = true;
  globalThis.BrowserplugBodyCensor = undefined;
  t.after(() => { globalThis.BrowserplugBodyCensor = CENSOR; });
  const { response, error } = await get(mod, ids);
  assert.equal(response, undefined, 'a raw body was sent although censoring is on');
  assert.equal(error.code, 'CENSOR_UNAVAILABLE');
});

test('glue: module missing with censoring OFF is harmless', async (t) => {
  const mod = loadModule();
  const ids = await capture(mod, t, { requestBody: LOGIN, responseBody: TOKENS });
  globalThis.BrowserplugBodyCensor = undefined;
  t.after(() => { globalThis.BrowserplugBodyCensor = CENSOR; });
  const { response } = await get(mod, ids);
  assert.equal(response.data.request_body.content, LOGIN);
});
