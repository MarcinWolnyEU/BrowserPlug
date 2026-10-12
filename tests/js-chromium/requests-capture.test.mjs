/*
 * CHROMIUM TWIN of tests/js/requests-capture.test.mjs — the request-monitoring subsystem
 * in extension-chromium/background.js. The MONITOR-LIFECYCLE half is shared logic, but the
 * BODY-CAPTURE half is an entirely different mechanism and is what this twin exercises:
 *
 *   design D5a — MV3 removed blocking webRequest + filterResponseData, so there is no
 *   StreamFilter. Instead the page's MAIN-world fetch/XHR wrapper (content-main.js)
 *   captures response text while a monitor is armed and relays it via content.js as a
 *   `net_body` message. The SW CORRELATES that body to its webRequest log record by
 *   (tabId, url, method, nearest ts) and stores it shape-identically to gecko's
 *   finalizeStreamCapture result — so requests.get_content is unchanged.
 *
 * What this twin pins: the arm/disarm broadcast (browserplug_monitor_state), bodyCaptureArmed /
 * urlPatterns derivation, correlateNetBody, storeNetBody (content-type gating + max_body_size
 * truncation + storage shape), the honest "not capturable on this engine" get_content note,
 * the redaction FLOOR (credentials never reach an agent; sensitive_headers only ADDS), and
 * describeRequestBody.
 *
 * Like the gecko original: slice the contiguous monitoring block and instantiate it with
 * stubbed browser/chrome globals. Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');

function loadRequestsModule() {
  const start = BG_SRC.indexOf('// Request monitoring state');
  const end = BG_SRC.indexOf('// Test helper action handler', start);
  assert.ok(start >= 0 && end > start, 'request-monitoring block not found in background.js');
  const block = BG_SRC.slice(start, end);

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const console = { log() {}, warn() {}, error() {} };
    const responses = [];
    const errors = [];
    const broadcasts = [];   // browser.tabs.sendMessage(tabId, {action:'browserplug_monitor_state', ...})
    let isConnected = true;

    // Symbols the block reaches OUTSIDE itself.
    const TAB_REQUESTS = new Map();
    const STATIC_TYPES = new Set();
    const resolveTabId = async (data) => (data && data.tabId) || 1;
    const sendResponse = (id, action, data) => responses.push({ id, action, data });
    const sendError = (id, code, message) => errors.push({ id, code, message });
    // chrome.storage.session is guarded (if unavailable, persist/restore no-op) — model
    // "unavailable" so monitor DEFINITIONS aren't touched by these unit tests.
    const chrome = {};
    function setInterval() { return 1; }        // the sweeper never actually fires here
    function clearInterval() {}

    // webRequest stub: record each registered listener so tests can fire the REAL wiring.
    const installed = new Map();
    const evt = (name) => ({
      addListener: (fn) => installed.set(name, fn),
      removeListener: (fn) => { if (installed.get(name) === fn) installed.delete(name); }
    });
    const browser = {
      webRequest: {
        onBeforeRequest: evt('onBeforeRequest'),
        onSendHeaders: evt('onSendHeaders'),
        onHeadersReceived: evt('onHeadersReceived'),
        onCompleted: evt('onCompleted'),
        onErrorOccurred: evt('onErrorOccurred'),
      },
      tabs: {
        query: async () => [{ id: 1 }, { id: 2 }],
        sendMessage: (tabId, msg) => { broadcasts.push({ tabId, msg }); return Promise.resolve(); },
      },
      // get_content reads the censoring settings on every call; an empty store answers the
      // defaults (censoring off), as a fresh profile does.
      storage: { sync: { get: async (defaults) => ({ ...defaults }) } }
    };

    ${block}

    return {
      activeMonitors, retiredMonitors, evictedMonitors, capturedRequests, requestDetails,
      capturedResponseBodies,
      monitorConfig, monitorState, retireMonitor,
      captureRequestEvent, handleRequestsAction,
      bodyCaptureArmed, bodyCaptureUrlPatterns, broadcastMonitorState,
      shouldCaptureRequest, correlateNetBody, storeNetBody, shouldCaptureContentTypeBG,
      redactHeaders, describeRequestBody, ALWAYS_REDACTED_HEADERS,
      installed, responses, errors, broadcasts,
      consts: { MAX_BODY_SIZE_DEFAULT, MAX_BODY_SIZE_LIMIT, REQUEST_DETAILS_MAX },
      stopMonitorSweeper,
      setConnected: (v) => { isConnected = v; }
    };
  `);
  return factory();
}

// ---- helpers -----------------------------------------------------------------

let seq = 0;
async function startMonitor(mod, options = {}, patterns = ['*']) {
  const id = `call_${seq++}`;
  await mod.handleRequestsAction(id, 'requests.start_monitoring',
    { url_patterns: patterns, options });
  const r = mod.responses.find(x => x.id === id);
  assert.ok(r, 'start_monitoring sent no response');
  return r.data;
}

async function callAction(mod, action, data) {
  const id = `call_${seq++}`;
  await mod.handleRequestsAction(id, action, data);
  return {
    response: mod.responses.find(x => x.id === id),
    error: mod.errors.find(x => x.id === id)
  };
}

// Drive the REAL registered webRequest listeners so requestDetails is populated exactly
// the way Chromium populates it (monitor must be active for the guard to pass).
function seedRequest(mod, { requestId, url, contentType = 'application/json', length = 100,
                           status = 200, method = 'GET', tabId = 1, requestBody,
                           requestHeaders, extraResponseHeaders = [] }) {
  const base = { requestId, url, method, tabId, frameId: 0,
                 type: 'xmlhttprequest', timeStamp: Date.now() };
  mod.installed.get('onBeforeRequest')({ ...base, requestBody });
  if (requestHeaders) mod.installed.get('onSendHeaders')({ ...base, requestHeaders });
  mod.installed.get('onHeadersReceived')({ ...base,
    statusCode: status,
    responseHeaders: [{ name: 'Content-Type', value: contentType },
                      { name: 'Content-Length', value: String(length) },
                      ...extraResponseHeaders] });
  mod.installed.get('onCompleted')({ ...base, statusCode: status });
}

// Relay a captured body exactly as content.js does (D5a `net_body`).
function relayBody(mod, tabId, { url, method = 'GET', status = 200, contentType = 'application/json',
                                body = '', truncated = false, ts }) {
  mod.storeNetBody(tabId, { url, method, status, contentType, body, truncated,
                            ts: ts == null ? Date.now() : ts });
}

const BODY_OPTS = { capture_response_bodies: true, max_body_size: 50000 };
const bytesOf = (s) => new TextEncoder().encode(s).buffer;

// ---- arm/disarm broadcast (D5a) ---------------------------------------------

test('a body-capture monitor ARMS the page wrapper; a metadata-only monitor does not', async () => {
  const meta = loadRequestsModule();
  await startMonitor(meta, {}, ['https://x.test/*']);
  assert.equal(meta.bodyCaptureArmed(), false, 'a metadata-only monitor does not arm body capture');

  const body = loadRequestsModule();
  await startMonitor(body, BODY_OPTS, ['https://x.test/*']);
  assert.equal(body.bodyCaptureArmed(), true, 'a capture_response_bodies monitor arms it');
  assert.deepEqual(body.bodyCaptureUrlPatterns(), ['https://x.test/*'],
    'and its url patterns are what the wrapper filters on');
});

test('start_monitoring BROADCASTS the armed state to every tab (browserplug_monitor_state)', async () => {
  const mod = loadRequestsModule();
  await startMonitor(mod, BODY_OPTS, ['https://x.test/*']);
  await mod.broadcastMonitorState();

  assert.ok(mod.broadcasts.length >= 2, 'every open tab is told the new arm state');
  const msg = mod.broadcasts[0].msg;
  assert.equal(msg.action, 'browserplug_monitor_state');
  assert.equal(msg.armed, true);
  assert.deepEqual(msg.urlPatterns, ['https://x.test/*']);
});

test('bodyCaptureUrlPatterns unions the patterns of ONLY the body-capturing monitors', async () => {
  const mod = loadRequestsModule();
  await startMonitor(mod, {}, ['https://meta.test/*']);            // metadata-only — excluded
  await startMonitor(mod, BODY_OPTS, ['https://a.test/*']);
  await startMonitor(mod, BODY_OPTS, ['https://b.test/*']);
  const patterns = mod.bodyCaptureUrlPatterns().sort();
  assert.deepEqual(patterns, ['https://a.test/*', 'https://b.test/*'],
    'the metadata-only monitor\'s pattern is not armed for body capture');
});

// ---- shouldCaptureRequest (glob) --------------------------------------------

test('shouldCaptureRequest matches glob url patterns and the optional tab filter', () => {
  const mod = loadRequestsModule();
  const m = { url_patterns: ['https://x.test/*'], tab_id: null };
  assert.equal(mod.shouldCaptureRequest(m, { url: 'https://x.test/api', tabId: 1 }), true);
  assert.equal(mod.shouldCaptureRequest(m, { url: 'https://other.test/', tabId: 1 }), false);
  assert.equal(mod.shouldCaptureRequest({ url_patterns: ['*'] }, { url: 'https://anything/' }), true);

  const tabScoped = { url_patterns: ['*'], tab_id: 7 };
  assert.equal(mod.shouldCaptureRequest(tabScoped, { url: 'https://x/', tabId: 7 }), true);
  assert.equal(mod.shouldCaptureRequest(tabScoped, { url: 'https://x/', tabId: 9 }), false,
    'a tab-scoped monitor ignores other tabs');
});

// ---- correlateNetBody --------------------------------------------------------

test('correlateNetBody matches a relayed body to its request by (tab, url, method)', async () => {
  const mod = loadRequestsModule();
  const mon = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, { requestId: 'req-1', url: 'https://x.test/api', method: 'POST', tabId: 3 });

  const hit = mod.correlateNetBody(3, { url: 'https://x.test/api', method: 'POST', ts: Date.now() });
  assert.equal(hit, 'req-1', 'the matching request_id is found');

  assert.equal(mod.correlateNetBody(3, { url: 'https://x.test/api', method: 'GET' }), null,
    'a different method does not correlate');
  assert.equal(mod.correlateNetBody(9, { url: 'https://x.test/api', method: 'POST' }), null,
    'a different tab does not correlate');
  assert.equal(mod.correlateNetBody(3, { url: 'https://x.test/other', method: 'POST' }), null,
    'a different url does not correlate');
  assert.ok(mon.monitor_id);
});

test('correlateNetBody picks the NEAREST-in-time request when a url is polled repeatedly', async () => {
  const mod = loadRequestsModule();
  await startMonitor(mod, BODY_OPTS);
  // Two captures of the same url; timestamps come from Date.now() at capture time.
  seedRequest(mod, { requestId: 'early', url: 'https://x.test/poll', tabId: 1 });
  await new Promise(r => setTimeout(r, 5));
  seedRequest(mod, { requestId: 'late', url: 'https://x.test/poll', tabId: 1 });

  const lateTs = mod.requestDetails.get('late');
  const hit = mod.correlateNetBody(1, { url: 'https://x.test/poll', method: 'GET',
                                        ts: new Date(lateTs.timestamp).getTime() });
  assert.equal(hit, 'late', 'the body is attributed to the nearest request in time, not a stale poll');
});

// ---- storeNetBody: end-to-end capture ---------------------------------------

test('a relayed fetch body is correlated, stored, and served by get_content as TEXT', async () => {
  const mod = loadRequestsModule();
  const mon = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, { requestId: 'r1', url: 'https://x.test/api', contentType: 'application/json' });
  relayBody(mod, 1, { url: 'https://x.test/api', contentType: 'application/json',
                      body: '{"ok":true}' });

  const rec = mod.capturedResponseBodies.get('r1');
  assert.ok(rec, 'the body was stored under the correlated request_id');
  assert.equal(rec.response_body, '{"ok":true}');
  assert.equal(rec.encoding, 'utf-8', 'the wrapper relays DECODED text — no gzip to undo');
  assert.equal(rec.source, 'fetch_wrap', 'the storage record records where the body came from');
  assert.equal(rec.truncated, false);

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id: mon.monitor_id, request_id: 'r1' });
  assert.equal(response.data.response_body.included, true);
  assert.equal(response.data.response_body.content, '{"ok":true}');
  assert.match(response.data.response_body.note, /fetch\/XHR wrapper/,
    'the note is honest about how the body was obtained on this engine');
});

test('storeNetBody respects content_types_to_capture — an excluded type is NOT stored', async () => {
  const mod = loadRequestsModule();
  await startMonitor(mod, { capture_response_bodies: true, content_types_to_capture: ['application/json'] });
  seedRequest(mod, { requestId: 'html-1', url: 'https://x.test/page', contentType: 'text/html' });
  relayBody(mod, 1, { url: 'https://x.test/page', contentType: 'text/html', body: '<html></html>' });

  assert.equal(mod.capturedResponseBodies.has('html-1'), false,
    'a body of an excluded content-type is dropped, not stored');
});

test('storeNetBody truncates a body over max_body_size and flags it', async () => {
  const mod = loadRequestsModule();
  await startMonitor(mod, { capture_response_bodies: true, max_body_size: 10 });
  seedRequest(mod, { requestId: 'big-1', url: 'https://x.test/big', contentType: 'text/plain' });
  relayBody(mod, 1, { url: 'https://x.test/big', contentType: 'text/plain',
                      body: 'abcdefghijKLMNOPQRST' /* 20 chars */ });

  const rec = mod.capturedResponseBodies.get('big-1');
  assert.ok(rec);
  assert.equal(rec.response_body.length, 10, 'sliced to max_body_size');
  assert.equal(rec.response_body, 'abcdefghij');
  assert.equal(rec.truncated, true, 'and flagged truncated');
});

test('a net_body that correlates to no request is dropped (get_content honestly says so)', async () => {
  const mod = loadRequestsModule();
  const mon = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, { requestId: 'known', url: 'https://x.test/known' });
  // A body for a url that was never a captured request — cannot correlate.
  relayBody(mod, 1, { url: 'https://x.test/never', body: 'orphan' });
  assert.equal(mod.capturedResponseBodies.size, 0, 'an uncorrelatable body is not stored anywhere');

  // And get_content for a request WITHOUT a captured body gives the honest engine note.
  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id: mon.monitor_id, request_id: 'known' });
  const rb = response.data.response_body;
  assert.equal(rb.included, false);
  assert.equal(rb.content, null);
  assert.match(rb.note, /On Chromium only fetch\/XHR response bodies are capturable/,
    'the note names the engine limitation instead of pretending no body existed');
});

// ---- redaction FLOOR ---------------------------------------------------------

test('redactHeaders redacts the credential floor even with no monitor config', () => {
  const mod = loadRequestsModule();
  const out = mod.redactHeaders({
    'Authorization': 'Bearer secret', 'Cookie': 'sid=abc', 'Set-Cookie': 'sid=abc',
    'X-Api-Key': 'k', 'Content-Type': 'application/json'
  }, 'no-such-monitor');
  assert.equal(out['Authorization'], '<redacted>');
  assert.equal(out['Cookie'], '<redacted>');
  assert.equal(out['Set-Cookie'], '<redacted>');
  assert.equal(out['X-Api-Key'], '<redacted>');
  assert.equal(out['Content-Type'], 'application/json', 'a non-credential header is untouched');
});

test('the redaction floor covers the api-key / token family, not just authorization+cookie', () => {
  const mod = loadRequestsModule();
  const names = mod.ALWAYS_REDACTED_HEADERS;
  for (const n of ['authorization', 'cookie', 'set-cookie', 'x-api-key', 'x-auth-token',
                   'x-csrf-token', 'x-amz-security-token']) {
    assert.ok(names.includes(n), `${n} must be in the always-redacted floor`);
  }
});

test('sensitive_headers ADDS to the floor and can never SUBTRACT from it', async () => {
  const mod = loadRequestsModule();
  const mon = await startMonitor(mod, { sensitive_headers: ['x-custom-secret'] });
  const headers = { 'Authorization': 'Bearer s', 'X-Custom-Secret': 'v', 'Content-Type': 'text/plain' };
  const out = mod.redactHeaders(headers, mon.monitor_id);
  assert.equal(out['X-Custom-Secret'], '<redacted>', 'the caller-named header is added to redaction');
  assert.equal(out['Authorization'], '<redacted>', 'the floor still applies');
  assert.equal(out['Content-Type'], 'text/plain');

  // Even an explicitly EMPTY sensitive_headers cannot expose a floor header.
  const empty = await startMonitor(mod, { sensitive_headers: [] });
  const out2 = mod.redactHeaders({ 'Cookie': 'sid=1' }, empty.monitor_id);
  assert.equal(out2['Cookie'], '<redacted>', 'sensitive_headers:[] never unredacts the floor');
});

test('get_content redacts request AND response headers; Set-Cookie never leaks', async () => {
  const mod = loadRequestsModule();
  const mon = await startMonitor(mod, {});
  seedRequest(mod, { requestId: 'h1', url: 'https://x.test/secure',
    requestHeaders: [{ name: 'Authorization', value: 'Bearer top-secret' },
                     { name: 'Cookie', value: 'sid=abc' },
                     { name: 'Accept', value: 'application/json' }],
    extraResponseHeaders: [{ name: 'Set-Cookie', value: 'sid=rotated' }] });

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id: mon.monitor_id, request_id: 'h1' });
  assert.equal(response.data.request_headers['Authorization'], '<redacted>');
  assert.equal(response.data.request_headers['Cookie'], '<redacted>');
  assert.equal(response.data.request_headers['Accept'], 'application/json', 'a benign header rides through');
  assert.equal(response.data.response_headers['Set-Cookie'], '<redacted>',
    'a rotated session cookie must never enter an agent\'s context');
});

// ---- describeRequestBody -----------------------------------------------------

test('describeRequestBody returns formData as JSON', () => {
  const mod = loadRequestsModule();
  const out = mod.describeRequestBody({ formData: { a: ['1'], b: ['2'] } }, 'application/x-www-form-urlencoded', false);
  assert.equal(out.included, true);
  assert.deepEqual(JSON.parse(out.content), { a: ['1'], b: ['2'] });
  assert.equal(out.encoding, 'utf-8');
});

test('describeRequestBody decodes a UTF-8 raw body as text', () => {
  const mod = loadRequestsModule();
  const out = mod.describeRequestBody({ raw: [{ bytes: bytesOf('{"hello":"world"}') }] },
    'application/json', false);
  assert.equal(out.included, true);
  assert.equal(out.content, '{"hello":"world"}');
  assert.equal(out.encoding, 'utf-8');
});

test('describeRequestBody WITHHOLDS a binary raw body unless include_binary (and never fakes it)', () => {
  const mod = loadRequestsModule();
  const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x00, 0xff, 0xfe]).buffer;
  const withheld = mod.describeRequestBody({ raw: [{ bytes: png }] }, 'image/png', false);
  assert.equal(withheld.included, false);
  assert.equal(withheld.content, null);
  assert.equal(withheld.encoding, 'base64');
  assert.match(withheld.note, /include_binary=true/, 'it tells the agent how to get it');

  const included = mod.describeRequestBody({ raw: [{ bytes: png }] }, 'image/png', true);
  assert.equal(included.included, true);
  assert.equal(included.encoding, 'base64');
  assert.ok(included.content.length > 0, 'base64 content is returned when asked for');
});

test('describeRequestBody reports "no body" honestly for a request that sent none', () => {
  const mod = loadRequestsModule();
  const out = mod.describeRequestBody(null, null, false);
  assert.equal(out.included, false);
  assert.equal(out.size_bytes, 0);
  assert.match(out.note, /No request body/);
});

// ---- a start -> stop lifecycle sanity (shared logic, chromium broadcast) -----

test('stopping a body-capture monitor DISARMS the page wrapper', async () => {
  const mod = loadRequestsModule();
  const mon = await startMonitor(mod, BODY_OPTS, ['https://x.test/*']);
  assert.equal(mod.bodyCaptureArmed(), true);

  await callAction(mod, 'requests.stop_monitoring', { monitor_id: mon.monitor_id });
  assert.equal(mod.bodyCaptureArmed(), false, 'no active body-capture monitor remains — the wrapper disarms');
  // the retire path re-broadcasts; the last broadcast must be an unarmed state
  const last = mod.broadcasts[mod.broadcasts.length - 1];
  assert.equal(last.msg.armed, false, 'the disarm broadcast tells pages to stop capturing');
});
