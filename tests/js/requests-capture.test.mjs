/*
 * Unit tests for the request-monitoring subsystem in extension-gecko/background.js:
 * monitor lifecycle (TTL / disconnect cleanup / retention / bounded maps) and the
 * StreamFilter body capture (text vs. base64, include_binary gating).
 *
 * A leaked capture monitor is invisible and expensive — it holds the BLOCKING
 * webRequest listeners and forces `Accept-Encoding: identity` on all matching
 * traffic — so the teardown assertions below check the listeners are really gone,
 * not just that the monitor left the map.
 *
 * Like proxy.test.mjs / neterror.test.mjs: background.js is a top-level extension
 * script (browser.* listeners run on load), so we slice the contiguous monitoring
 * block out of the source and instantiate it with stubbed browser/socket globals.
 * Run with: npm run test:js  (== node --test "tests/js/*.test.mjs")
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

// Slice the whole monitoring subsystem: the state maps + lifecycle helpers, the
// StreamFilter capture, the webRequest listener wiring and handleRequestsAction.
// Anchors are stable source landmarks.
function loadRequestsModule(t) {
  const start = BG_SRC.indexOf('// Request monitoring state');
  const end = BG_SRC.indexOf('// Test helper action handler', start);
  assert.ok(start >= 0 && end > start, 'request-monitoring block not found in background.js');
  const block = BG_SRC.slice(start, end);

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const console = { log() {}, warn() {}, error() {} };
    const responses = [];
    const errors = [];
    let isConnected = true;

    // Only the symbols the block reaches OUTSIDE itself.
    const TAB_REQUESTS = new Map();
    const STATIC_TYPES = new Set();
    const resolveTabId = async (data) => (data && data.tabId) || 1;
    const sendResponse = (id, action, data) => responses.push({ id, action, data });
    const sendError = (id, code, message) => errors.push({ id, code, message });
    // webRequest stub that tracks which listeners are currently INSTALLED, and
    // hands out fake StreamFilters (the real body-capture path).
    const installed = new Map();
    const evt = (name) => ({
      addListener: (fn) => installed.set(name, fn),
      removeListener: (fn) => { if (installed.get(name) === fn) installed.delete(name); }
    });
    const filters = new Map();
    const browser = { webRequest: {
      onBeforeRequest: evt('onBeforeRequest'),
      onBeforeSendHeaders: evt('onBeforeSendHeaders'),
      onHeadersReceived: evt('onHeadersReceived'),
      onCompleted: evt('onCompleted'),
      onErrorOccurred: evt('onErrorOccurred'),
      filterResponseData: (requestId) => {
        const f = {
          requestId, written: [], disconnected: false, closed: false,
          write(d) { this.written.push(d); },
          disconnect() { this.disconnected = true; },
          close() { this.closed = true; }
        };
        filters.set(requestId, f);
        return f;
      }
    },
      // get_content reads the censoring settings on every call; an empty store answers the
      // defaults (censoring off), as a fresh profile does.
      storage: { sync: { get: async (defaults) => ({ ...defaults }) } }
    };

    ${block}

    return {
      activeMonitors, retiredMonitors, evictedMonitors, capturedRequests, requestDetails,
      capturedResponseBodies, streamCaptures, parkedBodies,
      monitorTtlMs, clampMaxBodySize, monitorState, retireMonitor, evictMonitorData,
      enforceCaptureCaps, sweepMonitors, ensureMonitorSweeper, stopMonitorSweeper,
      captureRequestEvent, attachStreamFilter, finalizeStreamCapture, handleRequestsAction,
      monitorWantingBody,
      isTextualContentType, redactHeaders,
      installed, filters, responses, errors, browser,
      consts: {
        MONITOR_TTL_DEFAULT_MS, MONITOR_TTL_MIN_MS, MONITOR_TTL_MAX_MS,
        MONITOR_RETENTION_MS, MONITOR_DISCONNECT_GRACE_MS,
        REQUEST_DETAILS_MAX, CAPTURED_PER_MONITOR_MAX,
        CAPTURED_BODIES_MAX, CAPTURED_BODIES_MAX_BYTES,
        MAX_BODY_SIZE_DEFAULT, MAX_BODY_SIZE_LIMIT, PARKED_BODY_MAX_AGE_MS
      },
      setConnected: (v) => { isConnected = v; },
      setLastServedAt: (t) => { monitorsLastServedAt = t; }
    };
  `);
  const mod = factory();
  // Starting a monitor arms the sweep interval; leaving one running holds node's
  // event loop open for the whole run.
  if (t) t.after(() => mod.stopMonitorSweeper());
  return mod;
}

// ---- helpers ----------------------------------------------------------------

let seq = 0;
async function startMonitor(mod, options = {}, patterns = ['*']) {
  const id = `call_${seq++}`;
  await mod.handleRequestsAction(id, 'requests.start_monitoring',
    { url_patterns: patterns, options });
  const r = mod.responses.find(x => x.id === id);
  assert.ok(r, 'start_monitoring sent no response');
  return r.data;
}

// Monitor ids used to be `mon_${Date.now()}`, so two starts in the same millisecond
// minted the SAME id and the second overwrote the first. background.js now
// appends a monotonic counter, so ids are unique regardless of the clock (pinned by
// the same-millisecond test above); nextMs() is kept only where a test wants the two
// monitors visibly ordered.
const nextMs = () => new Promise(r => setTimeout(r, 2));

async function callAction(mod, action, data) {
  const id = `call_${seq++}`;
  await mod.handleRequestsAction(id, action, data);
  return {
    response: mod.responses.find(x => x.id === id),
    error: mod.errors.find(x => x.id === id)
  };
}

// Drive the real webRequest event path so requestDetails/capturedRequests are
// populated exactly the way Firefox populates them.
function seedRequest(mod, monitorId, { requestId, url, contentType, length = 100, status = 200,
                                       method = 'GET', requestBody, requestHeaders,
                                       extraResponseHeaders = [] }) {
  const base = { requestId, url, method, tabId: 1, frameId: 0,
                 type: 'xmlhttprequest', timeStamp: Date.now() };
  mod.captureRequestEvent(monitorId, 'onBeforeRequest', { ...base, requestBody });
  if (requestHeaders) {
    // Fire this one through the REGISTERED listener, not captureRequestEvent directly.
    // request_headers was broken precisely because the listener carrying them was
    // never registered, so a test that reaches past registration cannot see the bug
    // it exists to catch.
    const fire = mod.installed.get('onBeforeSendHeaders');
    assert.ok(fire, 'onBeforeSendHeaders is not registered — request_headers cannot be captured');
    fire({ ...base, requestHeaders });
  }
  mod.captureRequestEvent(monitorId, 'onHeadersReceived', { ...base,
    statusCode: status,
    responseHeaders: [{ name: 'Content-Type', value: contentType },
                      { name: 'Content-Length', value: String(length) },
                      ...extraResponseHeaders] });
  mod.captureRequestEvent(monitorId, 'onCompleted', { ...base, statusCode: status });
}

// Emulate what attachStreamFilter's ondata/onstop leave behind for finalize.
function finalizeBody(mod, requestId, bytes, options, maxSize = 50000) {
  mod.streamCaptures.set(requestId, {
    chunks: [bytes.buffer], received: bytes.byteLength, truncated: false,
    finalized: false, maxSize, opts: options
  });
  mod.finalizeStreamCapture(requestId);
}

// Drive the REAL StreamFilter path (attachStreamFilter + its ondata/onstop), which
// is where the cap/truncation semantics live.
function streamBody(mod, monitorId, requestId, chunks) {
  const monitor = mod.activeMonitors.get(monitorId);
  mod.attachStreamFilter({ requestId }, monitor);
  const filter = mod.filters.get(requestId);
  for (const c of chunks) {
    filter.ondata({ data: c.buffer.slice(c.byteOffset, c.byteOffset + c.byteLength) });
  }
  if (!filter.disconnected) filter.onstop();
  return filter;
}

const BODY_OPTS = { capture_response_bodies: true, max_body_size: 50000 };

// ---- monitor id uniqueness (regression) -------------------------------------

test('two monitors started in the SAME millisecond get distinct ids and both survive', async (t) => {
  const mod = loadRequestsModule(t);

  // Freeze the clock so both starts mint their id from the identical Date.now() — the
  // exact same-millisecond collision that used to make the second start overwrite the
  // first in activeMonitors. new Function bodies reference the global Date, so patching
  // Date.now here reaches the sliced start_monitoring code.
  const realNow = Date.now;
  const frozen = Date.now();
  Date.now = () => frozen;
  t.after(() => { Date.now = realNow; });

  const a = await startMonitor(mod, BODY_OPTS);
  const b = await startMonitor(mod, BODY_OPTS);

  // The bug: `mon_${Date.now()}` is non-unique within a ms, so both ids were `mon_<frozen>`.
  assert.notEqual(a.monitor_id, b.monitor_id,
    'a same-ms second start must not reuse the first id (needs a counter suffix, not just Date.now())');
  assert.equal(mod.activeMonitors.size, 2, 'neither monitor overwrote the other');
  assert.ok(mod.activeMonitors.has(a.monitor_id), 'first monitor still registered');
  assert.ok(mod.activeMonitors.has(b.monitor_id), 'second monitor still registered');
  // the overwrite used to clobber the per-monitor capture bucket too
  assert.ok(mod.capturedRequests.has(a.monitor_id) && mod.capturedRequests.has(b.monitor_id),
    'each surviving monitor keeps its own capturedRequests bucket');
});

test('monitorTtlMs: 10 min default, honours ttl_seconds, clamps both ends', (t) => {
  const { monitorTtlMs, consts } = loadRequestsModule(t);
  assert.equal(monitorTtlMs(undefined), consts.MONITOR_TTL_DEFAULT_MS);
  assert.equal(monitorTtlMs({}), consts.MONITOR_TTL_DEFAULT_MS);
  assert.equal(monitorTtlMs({ ttl_seconds: 120 }), 120000);
  assert.equal(monitorTtlMs({ ttl_seconds: 1 }), consts.MONITOR_TTL_MIN_MS);
  assert.equal(monitorTtlMs({ ttl_seconds: 99999 }), consts.MONITOR_TTL_MAX_MS);
  assert.equal(monitorTtlMs({ ttl_seconds: 'nonsense' }), consts.MONITOR_TTL_DEFAULT_MS);
});

test('start_monitoring reports the expiry it armed', async (t) => {
  const mod = loadRequestsModule(t);
  const data = await startMonitor(mod, { ttl_seconds: 60 });
  assert.equal(data.ttl_seconds, 60);
  assert.ok(Date.parse(data.expires_at) > Date.now(), 'expires_at is in the future');
});

test('an expired monitor is retired by the sweeper and its blocking listeners removed', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  assert.ok(mod.installed.has('onBeforeRequest'), 'blocking onBeforeRequest installed');
  assert.ok(mod.installed.has('onBeforeSendHeaders'), 'blocking onBeforeSendHeaders installed');

  mod.activeMonitors.get(monitor_id).expires_at = Date.now() - 1;
  mod.sweepMonitors();

  assert.equal(mod.activeMonitors.size, 0, 'monitor retired');
  assert.ok(mod.retiredMonitors.has(monitor_id), 'retired, not forgotten');
  // The whole point: no listener may outlive the last active monitor, or the
  // forced identity encoding keeps costing the user compression on every request.
  assert.equal(mod.installed.size, 0, `listeners still installed: ${[...mod.installed.keys()]}`);
});

// ---- disconnect cleanup -----------------------------------------------------

test('a brief server outage (< grace) does NOT retire a monitor — /restart reconnects in seconds', async (t) => {
  const mod = loadRequestsModule(t);
  await startMonitor(mod, BODY_OPTS);

  mod.setConnected(false);
  mod.setLastServedAt(Date.now() - (mod.consts.MONITOR_DISCONNECT_GRACE_MS - 5000));
  mod.sweepMonitors();

  assert.equal(mod.activeMonitors.size, 1, 'monitor survives a transient blip');
  assert.ok(mod.installed.has('onBeforeRequest'));
});

test('a server gone longer than the grace period retires every monitor', async (t) => {
  const mod = loadRequestsModule(t);
  await startMonitor(mod, BODY_OPTS);
  await nextMs();
  await startMonitor(mod, BODY_OPTS);
  assert.equal(mod.activeMonitors.size, 2);

  mod.setConnected(false);
  mod.setLastServedAt(Date.now() - (mod.consts.MONITOR_DISCONNECT_GRACE_MS + 1000));
  mod.sweepMonitors();

  assert.equal(mod.activeMonitors.size, 0, 'nothing left to consume the captures');
  assert.equal(mod.installed.size, 0, 'listeners removed');
});

test('a reconnect RESETS the outage clock (the monitor survives an outage longer than the grace)', async (t) => {
  const mod = loadRequestsModule(t);
  await startMonitor(mod, BODY_OPTS);

  // Seed an outage LONGER than the grace — if the reconnect did not reset the clock,
  // the next disconnected sweep would retire the monitor on that stale timestamp.
  mod.setConnected(false);
  mod.setLastServedAt(Date.now() - (mod.consts.MONITOR_DISCONNECT_GRACE_MS + 30000));

  mod.setConnected(true);
  mod.sweepMonitors();               // reconnected: `if (isConnected) monitorsLastServedAt = now`
  assert.equal(mod.activeMonitors.size, 1, 'a connected sweep never retires');

  mod.setConnected(false);
  mod.sweepMonitors();               // the outage clock must start from the RECONNECT

  assert.equal(mod.activeMonitors.size, 1, 'the reconnect saved the monitor');
  assert.ok(mod.installed.has('onBeforeRequest'), 'its listeners are still armed');
});

// ---- teardown / retention ---------------------------------------------------

// Overlapping url_patterns are legitimate (and in a long-lived browser an agent's new
// monitor routinely coexists with someone else's). One request record is shared by all
// the monitors that matched it — so if it were attributed to just the FIRST matcher,
// every later monitor would list nothing and get_content would refuse a request that
// monitor really did capture.
test('a request matched by TWO monitors is listed by both, and fetchable through either', async (t) => {
  const mod = loadRequestsModule(t);
  const a = await startMonitor(mod, {}, ['*']);
  await nextMs();
  const b = await startMonitor(mod, {}, ['*']);
  assert.notEqual(a.monitor_id, b.monitor_id);

  // the real listeners call captureRequestEvent once per MATCHING monitor
  for (const id of [a.monitor_id, b.monitor_id]) {
    seedRequest(mod, id, { requestId: '77', url: 'http://x/article.html', contentType: 'text/html' });
  }

  for (const id of [a.monitor_id, b.monitor_id]) {
    const listed = (await callAction(mod, 'requests.list_captured', { monitor_id: id }))
      .response.data.requests.map(r => r.request_id);
    assert.deepEqual(listed, ['77'], `monitor ${id} must list the request it captured`);
    // and everything a monitor LISTS, it must be able to GET (the two must never disagree)
    const got = await callAction(mod, 'requests.get_content',
      { monitor_id: id, request_id: '77' });
    assert.equal(got.error, undefined, `monitor ${id} could not fetch a request it listed`);
    assert.equal(got.response.data.request_id, '77');
  }

  // a monitor that never matched it is still refused
  await nextMs();
  const c = await startMonitor(mod, {}, ['*/nothing/*']);
  const denied = await callAction(mod, 'requests.get_content',
    { monitor_id: c.monitor_id, request_id: '77' });
  assert.ok(denied.error, 'a monitor that never captured the request must not fetch it');
  assert.match(denied.error.message, /not captured by monitor/);
});

test('stopping one of two monitors keeps the listeners for the survivor', async (t) => {
  const mod = loadRequestsModule(t);
  const a = await startMonitor(mod, BODY_OPTS);
  await nextMs();
  await startMonitor(mod, BODY_OPTS);
  assert.equal(mod.activeMonitors.size, 2);

  await callAction(mod, 'requests.stop_monitoring', { monitor_id: a.monitor_id });

  assert.equal(mod.activeMonitors.size, 1);
  assert.ok(mod.installed.has('onBeforeRequest'), 'listeners stay for the remaining monitor');
});

// Body capture has NO global enable/disable switch: the StreamFilter is armed
// per-request by monitorWantingBody(), which reads activeMonitors live. Retiring a
// monitor therefore disarms exactly its own requests and nobody else's. This used to
// be a spy assertion on a disableResponseBodyCapture() kill switch that drove the
// (dead) content-script fetch/XHR wrapper; both are deleted, so assert the PROPERTY
// the kill switch was standing in for instead of the mechanism.
test('retiring one body monitor does not disarm a surviving one; the last one disarms capture', async (t) => {
  const mod = loadRequestsModule(t);
  const a = await startMonitor(mod, BODY_OPTS, ['*a.test*']);
  await nextMs();
  const b = await startMonitor(mod, BODY_OPTS, ['*b.test*']);

  await callAction(mod, 'requests.stop_monitoring', { monitor_id: a.monitor_id });

  // The survivor still gets bodies.
  assert.ok(mod.monitorWantingBody({ url: 'https://b.test/x.json', tabId: 1 }),
    'retiring monitor A must not disarm monitor B');
  assert.equal(mod.monitorWantingBody({ url: 'https://a.test/x.json', tabId: 1 }), null,
    'the retired monitor A must no longer arm capture');

  await callAction(mod, 'requests.stop_monitoring', { monitor_id: b.monitor_id });
  assert.equal(mod.monitorWantingBody({ url: 'https://b.test/x.json', tabId: 1 }), null,
    'with the last body monitor gone, nothing arms capture');
});

test('retiring the last monitor drops the in-flight stream buffers (and the page still gets its bytes)', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, { requestId: '301', url: 'https://x.test/slow.json', contentType: 'application/json' });
  const monitor = mod.activeMonitors.get(monitor_id);
  mod.attachStreamFilter({ requestId: '301' }, monitor);
  assert.equal(mod.streamCaptures.size, 1, 'a capture is in flight');

  await callAction(mod, 'requests.stop_monitoring', { monitor_id });
  assert.equal(mod.streamCaptures.size, 0, 'streamCaptures is the one map that used to leak');

  // The response is still streaming: the filter must keep writing it through to the
  // page, and its finalize must find no buffer and store nothing.
  const filter = mod.filters.get('301');
  filter.ondata({ data: new TextEncoder().encode('{"late":true}').buffer });
  filter.onstop();
  assert.equal(filter.written.length, 1, 'the page still received its response body');
  assert.equal(mod.capturedResponseBodies.has('301'), false, 'nothing stored for a dead monitor');
  assert.equal(mod.streamCaptures.size, 0);
});

// Named, not counted: a bare `size === 5` says nothing about WHICH listener a future
// change added or forgot to remove, and its failure message ("expected 5, got 6") reads
// as the test being stale rather than a listener leaking.
test('stop_monitoring removes every webRequest listener it installed', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  assert.deepEqual([...mod.installed.keys()].sort(),
    ['onBeforeRequest', 'onBeforeSendHeaders', 'onCompleted', 'onErrorOccurred',
     'onHeadersReceived'],
    'the exact listener set a body monitor arms');

  await callAction(mod, 'requests.stop_monitoring', { monitor_id });

  assert.equal(mod.installed.size, 0, `still installed: ${[...mod.installed.keys()]}`);
});

test('start -> stop -> list -> get still works: a retired monitor keeps its captures', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, { requestId: '11', url: 'https://x.test/api', contentType: 'application/json' });
  finalizeBody(mod, '11', new TextEncoder().encode('{"ok":true}'), BODY_OPTS);

  await callAction(mod, 'requests.stop_monitoring', { monitor_id });

  const listed = await callAction(mod, 'requests.list_captured', { monitor_id });
  assert.equal(listed.response.data.total_requests, 1);
  const got = await callAction(mod, 'requests.get_content', { request_id: '11' });
  assert.equal(got.response.data.response_body.included, true);
  assert.equal(got.response.data.response_body.content, '{"ok":true}');
});

test('the retention window ends: the sweeper evicts a retired monitor\'s captures', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, { requestId: '21', url: 'https://x.test/api', contentType: 'application/json' });
  finalizeBody(mod, '21', new TextEncoder().encode('{"ok":true}'), BODY_OPTS);
  await callAction(mod, 'requests.stop_monitoring', { monitor_id });

  const retired = mod.retiredMonitors.get(monitor_id);
  retired.at = Date.now() - (mod.consts.MONITOR_RETENTION_MS + 1000);
  mod.sweepMonitors();

  assert.equal(mod.retiredMonitors.size, 0);
  assert.equal(mod.capturedRequests.size, 0, 'captured list evicted');
  assert.equal(mod.requestDetails.size, 0, 'request details evicted');
  assert.equal(mod.capturedResponseBodies.size, 0, 'response bodies evicted');

  const got = await callAction(mod, 'requests.get_content', { request_id: '21' });
  assert.equal(got.error.code, 'REQUEST_NOT_FOUND');
});

// ---- lifecycle awareness: list/get must never answer an empty "all good" --------

test('list_captured on a RETIRED monitor still serves its captures, and says it ended', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, { requestId: '201', url: 'https://x.test/api', contentType: 'application/json' });
  mod.activeMonitors.get(monitor_id).expires_at = Date.now() - 1;
  mod.sweepMonitors();

  const { response } = await callAction(mod, 'requests.list_captured', { monitor_id });
  assert.equal(response.data.status, 'stopped');
  assert.equal(response.data.total_requests, 1, 'a retired monitor keeps serving its captures');
  assert.match(response.data.ended_reason, /TTL expired/);
  assert.ok(response.data.readable_for_seconds > 0, 'says how long the data stays readable');
});

test('list_captured on an EXPIRED-and-evicted monitor errors instead of reporting zero requests', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, { requestId: '211', url: 'https://x.test/api', contentType: 'application/json' });
  await callAction(mod, 'requests.stop_monitoring', { monitor_id });
  mod.retiredMonitors.get(monitor_id).at = Date.now() - (mod.consts.MONITOR_RETENTION_MS + 1000);
  mod.sweepMonitors();

  const listed = await callAction(mod, 'requests.list_captured', { monitor_id });
  assert.equal(listed.response, undefined, '{total_requests: 0} would read as "the traffic never happened"');
  assert.equal(listed.error.code, 'MONITOR_EXPIRED');
  assert.match(listed.error.message, /evicted/);

  const got = await callAction(mod, 'requests.get_content', { monitor_id, request_id: '211' });
  assert.equal(got.error.code, 'MONITOR_EXPIRED');
});

test('list_captured / get_content on a monitor that NEVER existed say so', async (t) => {
  const mod = loadRequestsModule(t);
  const listed = await callAction(mod, 'requests.list_captured', { monitor_id: 'mon_nope' });
  assert.equal(listed.response, undefined);
  assert.equal(listed.error.code, 'MONITOR_NOT_FOUND');

  const got = await callAction(mod, 'requests.get_content',
    { monitor_id: 'mon_nope', request_id: '1' });
  assert.equal(got.error.code, 'MONITOR_NOT_FOUND');
});

test('get_content refuses a request that belongs to a DIFFERENT monitor', async (t) => {
  const mod = loadRequestsModule(t);
  const a = await startMonitor(mod, BODY_OPTS);
  await nextMs();
  const b = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, a.monitor_id, { requestId: '221', url: 'https://x.test/api', contentType: 'application/json' });
  finalizeBody(mod, '221', new TextEncoder().encode('{"secret":1}'), BODY_OPTS);

  const { error } = await callAction(mod, 'requests.get_content',
    { monitor_id: b.monitor_id, request_id: '221' });
  assert.equal(error.code, 'REQUEST_NOT_FOUND');
  // names the monitor that asked (it did not capture this) AND the one that did
  assert.match(error.message, new RegExp(`not captured by monitor ${b.monitor_id}`));
  assert.match(error.message, new RegExp(`belongs to ${a.monitor_id}`));
});

test('stopping an already-expired monitor says so instead of a bare "not found"', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  mod.activeMonitors.get(monitor_id).expires_at = Date.now() - 1;
  mod.sweepMonitors();

  const { error } = await callAction(mod, 'requests.stop_monitoring', { monitor_id });
  assert.equal(error.code, 'MONITOR_NOT_FOUND');
  assert.match(error.message, /already ended/);
});

// ---- bounded maps -----------------------------------------------------------

test('requestDetails is FIFO-bounded (oldest evicted, newest kept)', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  const n = mod.consts.REQUEST_DETAILS_MAX + 25;
  for (let i = 0; i < n; i++) {
    mod.captureRequestEvent(monitor_id, 'onBeforeRequest',
      { requestId: `r${i}`, url: `https://x.test/${i}`, method: 'GET', tabId: 1, timeStamp: Date.now() });
  }
  assert.equal(mod.requestDetails.size, mod.consts.REQUEST_DETAILS_MAX);
  assert.equal(mod.requestDetails.has('r0'), false, 'oldest evicted');
  assert.equal(mod.requestDetails.has(`r${n - 1}`), true, 'newest kept');
});

test('a monitor\'s captured list is FIFO-bounded, and the dropped entries\' data goes with it', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  const n = mod.consts.CAPTURED_PER_MONITOR_MAX + 5;
  for (let i = 0; i < n; i++) {
    seedRequest(mod, monitor_id, { requestId: `c${i}`, url: `https://x.test/${i}`, contentType: 'application/json' });
  }
  const captured = mod.capturedRequests.get(monitor_id);
  assert.equal(captured.length, mod.consts.CAPTURED_PER_MONITOR_MAX);
  assert.equal(captured[0].request_id, 'c5', 'oldest five fell off the front');
  assert.equal(mod.requestDetails.has('c0'), false, 'a dropped entry keeps no details');
});

// The map sizes are an implementation detail — what matters is that the two things
// an AGENT sees (the list, and what it can then fetch) never disagree.
test('a GLOBAL requestDetails eviction also un-lists the request: list_captured and get_content stay consistent', async (t) => {
  const mod = loadRequestsModule(t);
  const a = await startMonitor(mod, BODY_OPTS);
  await nextMs();
  const b = await startMonitor(mod, BODY_OPTS);

  // Two monitors, each under the per-monitor cap, together OVER the global one — so
  // the global FIFO evicts A's oldest details while A's own list never trips its cap.
  const perA = mod.consts.CAPTURED_PER_MONITOR_MAX;                       // 300
  const perB = mod.consts.REQUEST_DETAILS_MAX - perA + 50;                // 250 -> 550 > 500
  for (let i = 0; i < perA; i++) {
    seedRequest(mod, a.monitor_id, { requestId: `a${i}`, url: `https://a.test/${i}`, contentType: 'application/json' });
  }
  for (let i = 0; i < perB; i++) {
    seedRequest(mod, b.monitor_id, { requestId: `b${i}`, url: `https://b.test/${i}`, contentType: 'application/json' });
  }
  assert.equal(mod.requestDetails.size, mod.consts.REQUEST_DETAILS_MAX, 'the global cap held');

  const { response } = await callAction(mod, 'requests.list_captured', { monitor_id: a.monitor_id });
  const listed = response.data.requests;
  assert.ok(listed.length < perA, 'the globally evicted requests left A\'s list too');

  // Everything the list still advertises must be fetchable — the failure this guards
  // is list_captured offering a request get_content answers REQUEST_NOT_FOUND for.
  for (const req of listed) {
    const got = await callAction(mod, 'requests.get_content',
      { monitor_id: a.monitor_id, request_id: req.request_id });
    assert.equal(got.error, undefined,
      `list_captured advertised ${req.request_id} but get_content lost it`);
  }
});

test('max_body_size is CLAMPED — one monitor cannot set a body bigger than the whole byte budget', async (t) => {
  const mod = loadRequestsModule(t);
  assert.equal(mod.clampMaxBodySize(undefined), mod.consts.MAX_BODY_SIZE_DEFAULT);
  assert.equal(mod.clampMaxBodySize(0), mod.consts.MAX_BODY_SIZE_DEFAULT);
  assert.equal(mod.clampMaxBodySize(-5), mod.consts.MAX_BODY_SIZE_DEFAULT);
  assert.equal(mod.clampMaxBodySize(1234), 1234);
  assert.equal(mod.clampMaxBodySize(999 * 1024 * 1024), mod.consts.MAX_BODY_SIZE_LIMIT);
  // The byte budget must hold several clamped bodies, or storing one would evict all
  // the others (and, when it lands last, itself).
  assert.ok(mod.consts.CAPTURED_BODIES_MAX_BYTES >= 4 * mod.consts.MAX_BODY_SIZE_LIMIT,
    'the total byte cap must be comfortably larger than one clamped body');

  // …and the capture actually uses the clamp.
  const { monitor_id } = await startMonitor(mod,
    { capture_response_bodies: true, max_body_size: 999 * 1024 * 1024 });
  mod.attachStreamFilter({ requestId: '401' }, mod.activeMonitors.get(monitor_id));
  assert.equal(mod.streamCaptures.get('401').maxSize, mod.consts.MAX_BODY_SIZE_LIMIT);
});

test('an evicted BODY is reported as evicted — never as "no body was captured"', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, { requestId: '411', url: 'https://x.test/api', contentType: 'application/json' });
  finalizeBody(mod, '411', new TextEncoder().encode('{"ok":true}'), BODY_OPTS);

  // Push the byte cap over with newer bodies: the oldest body goes, its request stays.
  const big = Math.round(mod.consts.CAPTURED_BODIES_MAX_BYTES / 2);
  for (let i = 0; i < 3; i++) {
    mod.capturedResponseBodies.set(`filler${i}`, { response_body: '', content_length: big });
  }
  mod.enforceCaptureCaps();
  assert.equal(mod.capturedResponseBodies.has('411'), false, 'the oldest body was evicted');

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id, request_id: '411' });
  const rb = response.data.response_body;
  assert.equal(rb.included, false);
  assert.match(rb.note, /EVICTED/i, 'an evicted body must not be reported as "never captured"');
});

test('captured bodies are bounded by count AND by total bytes', (t) => {
  const mod = loadRequestsModule(t);
  for (let i = 0; i < mod.consts.CAPTURED_BODIES_MAX + 10; i++) {
    mod.capturedResponseBodies.set(`b${i}`, { response_body: 'x', content_length: 1 });
  }
  mod.enforceCaptureCaps();
  assert.equal(mod.capturedResponseBodies.size, mod.consts.CAPTURED_BODIES_MAX);
  assert.equal(mod.capturedResponseBodies.has('b0'), false, 'oldest evicted');

  mod.capturedResponseBodies.clear();
  const big = Math.round(mod.consts.CAPTURED_BODIES_MAX_BYTES / 4);
  for (let i = 0; i < 6; i++) {
    mod.capturedResponseBodies.set(`big${i}`, { response_body: '', content_length: big });
  }
  mod.enforceCaptureCaps();
  let total = 0;
  for (const r of mod.capturedResponseBodies.values()) total += r.content_length;
  assert.ok(total <= mod.consts.CAPTURED_BODIES_MAX_BYTES, `total ${total} over the byte cap`);
  assert.equal(mod.capturedResponseBodies.has('big5'), true, 'newest kept');
});

// ---- body capture: text vs. binary ------------------------------------------

test('a text body is decoded and stored as utf-8', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, { requestId: '31', url: 'https://x.test/a.json', contentType: 'application/json' });
  finalizeBody(mod, '31', new TextEncoder().encode('{"hello":"wörld"}'), BODY_OPTS);

  const rec = mod.capturedResponseBodies.get('31');
  assert.equal(rec.encoding, 'utf-8');
  assert.equal(rec.response_body, '{"hello":"wörld"}');
});

test('a binary body is base64-encoded, sized by its RAW bytes (not the base64 length)', async (t) => {
  const mod = loadRequestsModule(t);
  const opts = { capture_response_bodies: true, max_body_size: 50000,
                 content_types_to_capture: ['image/'] };
  const { monitor_id } = await startMonitor(mod, opts);
  const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0xff]);
  seedRequest(mod, monitor_id, { requestId: '41', url: 'https://x.test/a.png', contentType: 'image/png' });
  finalizeBody(mod, '41', png, opts);

  const rec = mod.capturedResponseBodies.get('41');
  assert.equal(rec.encoding, 'base64');
  assert.equal(rec.content_length, png.byteLength, 'raw byte count, not the base64 string length');
  assert.deepEqual(new Uint8Array(Buffer.from(rec.response_body, 'base64')), png);
});

test('a large binary body base64s without blowing the stack, and round-trips', async (t) => {
  const mod = loadRequestsModule(t);
  const opts = { capture_response_bodies: true, max_body_size: 500000,
                 content_types_to_capture: [] };
  const { monitor_id } = await startMonitor(mod, opts);
  // Comfortably past the ~64K argument limit that String.fromCharCode.apply blows on.
  const blob = new Uint8Array(300000);
  for (let i = 0; i < blob.length; i++) blob[i] = i % 256;
  seedRequest(mod, monitor_id, { requestId: '51', url: 'https://x.test/big.bin', contentType: 'application/octet-stream' });
  finalizeBody(mod, '51', blob, opts, 500000);

  const rec = mod.capturedResponseBodies.get('51');
  assert.equal(rec.encoding, 'base64');
  assert.equal(rec.truncated, false);
  assert.deepEqual(new Uint8Array(Buffer.from(rec.response_body, 'base64')), blob);
});

test('a binary body over max_body_size is truncated to the cap and flagged', async (t) => {
  const mod = loadRequestsModule(t);
  const opts = { capture_response_bodies: true, max_body_size: 1000,
                 content_types_to_capture: [] };
  const { monitor_id } = await startMonitor(mod, opts);
  const blob = new Uint8Array(4096).fill(7);
  seedRequest(mod, monitor_id, { requestId: '61', url: 'https://x.test/big.bin', contentType: 'application/pdf' });
  finalizeBody(mod, '61', blob, opts, 1000);

  const rec = mod.capturedResponseBodies.get('61');
  assert.equal(rec.truncated, true);
  assert.equal(Buffer.from(rec.response_body, 'base64').length, 1000, 'kept exactly max_body_size bytes');
});

test('the default content types admit no binary — a png is captured only on explicit opt-in', async (t) => {
  const mod = loadRequestsModule(t);
  const opts = { capture_response_bodies: true, max_body_size: 50000,
                 content_types_to_capture: ['application/json', 'text/plain'] };
  const { monitor_id } = await startMonitor(mod, opts);
  seedRequest(mod, monitor_id, { requestId: '71', url: 'https://x.test/a.png', contentType: 'image/png' });
  finalizeBody(mod, '71', new Uint8Array([1, 2, 3]), opts);

  assert.equal(mod.capturedResponseBodies.has('71'), false, 'binary must not be captured by default');
});

// ---- get_content: include_binary --------------------------------------------

test('get_content WITHHOLDS a binary body without include_binary, but reports its metadata', async (t) => {
  const mod = loadRequestsModule(t);
  const opts = { capture_response_bodies: true, max_body_size: 50000,
                 content_types_to_capture: ['image/'] };
  const { monitor_id } = await startMonitor(mod, opts);
  const png = new Uint8Array(1234).fill(9);
  seedRequest(mod, monitor_id, { requestId: '81', url: 'https://x.test/a.png',
                                 contentType: 'image/png', length: 1234 });
  finalizeBody(mod, '81', png, opts);

  const { response } = await callAction(mod, 'requests.get_content',
    { request_id: '81', include_binary: false });
  const rb = response.data.response_body;
  assert.equal(rb.included, false, 'a binary blob must never land in an agent context by accident');
  assert.equal(rb.content, null);
  assert.equal(rb.encoding, 'base64');
  assert.equal(rb.content_type, 'image/png');
  assert.equal(rb.size_bytes, 1234);
  assert.match(rb.note, /include_binary=true/);
});

test('get_content RETURNS the base64 body with include_binary=true', async (t) => {
  const mod = loadRequestsModule(t);
  const opts = { capture_response_bodies: true, max_body_size: 50000,
                 content_types_to_capture: ['image/'] };
  const { monitor_id } = await startMonitor(mod, opts);
  const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x01, 0x02]);
  seedRequest(mod, monitor_id, { requestId: '91', url: 'https://x.test/a.png',
                                 contentType: 'image/png', length: 6 });
  finalizeBody(mod, '91', png, opts);

  const { response } = await callAction(mod, 'requests.get_content',
    { request_id: '91', include_binary: true });
  const rb = response.data.response_body;
  assert.equal(rb.included, true);
  assert.equal(rb.encoding, 'base64');
  assert.equal(rb.size_bytes, 6, 'raw bytes, not the base64 string length');
  assert.deepEqual(new Uint8Array(Buffer.from(rb.content, 'base64')), png);
});

test('include_binary is irrelevant to text bodies (both ways return them)', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, { requestId: '101', url: 'https://x.test/a.json',
                                 contentType: 'application/json' });
  finalizeBody(mod, '101', new TextEncoder().encode('{"a":1}'), BODY_OPTS);

  for (const include_binary of [false, true]) {
    const { response } = await callAction(mod, 'requests.get_content',
      { request_id: '101', include_binary });
    const rb = response.data.response_body;
    assert.equal(rb.included, true, `include_binary=${include_binary}`);
    assert.equal(rb.encoding, 'utf-8');
    assert.equal(rb.content, '{"a":1}');
    assert.equal(rb.size_bytes, 7);
  }
});

test('get_content no longer advertises the removed save-to-file escape hatch', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, { requestId: '111', url: 'https://x.test/a.json',
                                 contentType: 'application/json' });
  finalizeBody(mod, '111', new TextEncoder().encode('{}'), BODY_OPTS);

  const { response } = await callAction(mod, 'requests.get_content', { request_id: '111' });
  assert.equal('saved_to_file' in response.data.request_body, false);
  assert.equal('saved_to_file' in response.data.response_body, false);
});

// ---- get_content must NEVER serve another request's body ---------------------

test('get_content never correlates by url+method+status: a polled endpoint\'s bodies do not cross over', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  // The same endpoint polled twice: identical url, method and status — the exact
  // shape the old correlation fallback matched on (and it served the OLDEST body).
  seedRequest(mod, monitor_id, { requestId: '501', url: 'https://x.test/poll', contentType: 'application/json' });
  finalizeBody(mod, '501', new TextEncoder().encode('{"tick":1}'), BODY_OPTS);
  seedRequest(mod, monitor_id, { requestId: '502', url: 'https://x.test/poll', contentType: 'application/json' });
  // …whose own body was never captured (evicted, excluded by type, never filtered: the
  // reason does not matter; what matters is that it is NOT this request's).

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id, request_id: '502' });
  const rb = response.data.response_body;
  assert.equal(rb.included, false, 'answering with request 501\'s body would be silent data corruption');
  assert.equal(rb.content, null);
  assert.match(rb.note, /No response body was captured/);
  assert.match(rb.note, /no stream filter was attached/,
    'seedRequest attaches no filter, and the note must say that rather than guess');
  // …and the miss must not poison the map with a fake record for 502.
  assert.equal(mod.capturedResponseBodies.has('502'), false);
  // The genuine record is untouched.
  const first = await callAction(mod, 'requests.get_content', { monitor_id, request_id: '501' });
  assert.equal(first.response.data.response_body.content, '{"tick":1}');
});

// ---- text vs. binary classification -----------------------------------------

test('every textual media type comes back as TEXT (json/+json/xml/js/form/csv), not base64', async (t) => {
  const mod = loadRequestsModule(t);
  const opts = { capture_response_bodies: true, max_body_size: 50000, content_types_to_capture: [] };
  const { monitor_id } = await startMonitor(mod, opts);
  const types = [
    'application/vnd.api+json',            // the whole +json family
    'application/ld+json',
    'application/problem+json; charset=utf-8',
    'application/atom+xml',
    'application/javascript',
    'application/x-www-form-urlencoded',
    'text/csv',
    'text/html; charset=utf-8'
  ];
  let n = 600;
  for (const contentType of types) {
    const rid = String(n++);
    seedRequest(mod, monitor_id, { requestId: rid, url: `https://x.test/${rid}`, contentType });
    finalizeBody(mod, rid, new TextEncoder().encode('body=1'), opts);
    const rec = mod.capturedResponseBodies.get(rid);
    assert.equal(rec.encoding, 'utf-8', `${contentType} must be text, not base64`);
    assert.equal(rec.response_body, 'body=1');
  }

  // and a genuinely binary type is still base64
  seedRequest(mod, monitor_id, { requestId: '699', url: 'https://x.test/i.png', contentType: 'image/png' });
  finalizeBody(mod, '699', new Uint8Array([0x89, 0x50]), opts);
  assert.equal(mod.capturedResponseBodies.get('699').encoding, 'base64');
});

test('with NO content-type the bytes decide: a clean UTF-8 decode is text, invalid bytes are binary', async (t) => {
  const mod = loadRequestsModule(t);
  const opts = { capture_response_bodies: true, max_body_size: 50000, content_types_to_capture: [] };
  const { monitor_id } = await startMonitor(mod, opts);

  seedRequest(mod, monitor_id, { requestId: '701', url: 'https://x.test/nott', contentType: undefined });
  finalizeBody(mod, '701', new TextEncoder().encode('{"héllo":true}'), opts);
  const text = mod.capturedResponseBodies.get('701');
  assert.equal(text.encoding, 'utf-8', 'a clean strict-UTF-8 decode means text');
  assert.equal(text.response_body, '{"héllo":true}');

  seedRequest(mod, monitor_id, { requestId: '702', url: 'https://x.test/notb', contentType: undefined });
  finalizeBody(mod, '702', new Uint8Array([0xff, 0xfe, 0x00, 0x80]), opts);
  const bin = mod.capturedResponseBodies.get('702');
  assert.equal(bin.encoding, 'base64', 'invalid UTF-8 means binary — do not guess');
  assert.deepEqual(new Uint8Array(Buffer.from(bin.response_body, 'base64')),
                   new Uint8Array([0xff, 0xfe, 0x00, 0x80]));
});

// ---- truncation honesty ------------------------------------------------------

test('a body landing EXACTLY on max_body_size is complete, not truncated', async (t) => {
  const mod = loadRequestsModule(t);
  const opts = { capture_response_bodies: true, max_body_size: 100, content_types_to_capture: [] };
  const { monitor_id } = await startMonitor(mod, opts);
  seedRequest(mod, monitor_id, { requestId: '801', url: 'https://x.test/exact', contentType: 'text/plain', length: 100 });

  streamBody(mod, monitor_id, '801', [new Uint8Array(100).fill(0x61)]); // 'a' x100, then onstop
  const rec = mod.capturedResponseBodies.get('801');
  assert.equal(rec.response_body.length, 100);
  assert.equal(rec.truncated, false, 'the stream ENDED on the cap — nothing was dropped');

  // …but a chunk arriving AFTER the cap was reached proves bytes really were dropped.
  seedRequest(mod, monitor_id, { requestId: '802', url: 'https://x.test/more', contentType: 'text/plain', length: 150 });
  streamBody(mod, monitor_id, '802',
    [new Uint8Array(100).fill(0x61), new Uint8Array(50).fill(0x62)]);
  assert.equal(mod.capturedResponseBodies.get('802').truncated, true);
});

test('a truncated body reports the bytes it RETURNED (and the real size when known)', async (t) => {
  const mod = loadRequestsModule(t);
  const opts = { capture_response_bodies: true, max_body_size: 100, content_types_to_capture: [] };
  const { monitor_id } = await startMonitor(mod, opts);
  seedRequest(mod, monitor_id, { requestId: '811', url: 'https://x.test/big', contentType: 'text/plain', length: 250 });

  // ONE 250-byte chunk overshoots the 100-byte cap: the buffer holds 250, the body we
  // hand back holds 100. Reporting the buffered count would be neither.
  streamBody(mod, monitor_id, '811', [new Uint8Array(250).fill(0x61)]);
  const rec = mod.capturedResponseBodies.get('811');
  assert.equal(rec.truncated, true);
  assert.equal(rec.response_body.length, 100);
  assert.equal(rec.content_length, 100, 'the bytes actually kept — not "whatever was buffered when the cap tripped"');

  const { response } = await callAction(mod, 'requests.get_content', { monitor_id, request_id: '811' });
  const rb = response.data.response_body;
  assert.equal(rb.size_bytes, 100, 'size_bytes = the bytes returned');
  assert.equal(rb.full_size_bytes, 250, 'the real size, from the Content-Length');
  assert.match(rb.note, /100 bytes returned of 250/);
});

// ---- body capture when the headers event lands late ----------------------------
//
// Firefox delivers non-blocking webRequest events to an extension late and in batches,
// while the StreamFilter's bytes arrive at once, so a small fast response can stop
// before its onHeadersReceived has been processed. These tests fire events through the
// listeners the module REGISTERED, in that order. seedRequest calls captureRequestEvent
// directly and always fires the headers first, so it can reach neither the order nor the
// commit glue in the listeners.

function fire(mod, event, details) {
  const listener = mod.installed.get(event);
  assert.ok(listener, `${event} is not registered`);
  return listener(details);
}

// The request starts, and its filter streams the whole body and stops, all before any
// response headers have been processed.
function streamBeforeHeaders(mod, requestId, url, body) {
  const base = { requestId, url, method: 'GET', tabId: 1, frameId: 0,
                 type: 'xmlhttprequest', timeStamp: Date.now() };
  fire(mod, 'onBeforeRequest', base);
  const filter = mod.filters.get(requestId);
  assert.ok(filter, 'onBeforeRequest armed no stream filter');
  filter.ondata({ data: body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength) });
  filter.onstop();
  return base;
}

function headersFor(base, contentType, length, extra = {}) {
  return { ...base, statusCode: 200, ...extra,
           responseHeaders: [{ name: 'Content-Type', value: contentType },
                             { name: 'Content-Length', value: String(length) }] };
}

// What the server sends for every default monitor: a non-empty list, so a body whose
// Content-Type is not known yet does not pass it.
const LISTED_TYPES = { capture_response_bodies: true, max_body_size: 50000,
                       content_types_to_capture: ['text/html', 'application/json'] };

async function bodyOf(mod, monitorId, requestId) {
  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id: monitorId, request_id: requestId });
  return response.data.response_body;
}

test('a body that finishes BEFORE its response headers is stored once they arrive', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, LISTED_TYPES);
  const html = new TextEncoder().encode('<p>early bytes</p>');
  const base = streamBeforeHeaders(mod, '901', 'https://x.test/page.html', html);
  assert.equal(mod.capturedResponseBodies.has('901'), false, 'its Content-Type is not known yet');
  assert.ok(mod.parkedBodies.has('901'), 'the finished body must wait for its headers, not be dropped');

  fire(mod, 'onHeadersReceived', headersFor(base, 'text/html; charset=utf-8', html.byteLength));
  fire(mod, 'onCompleted', { ...base, statusCode: 200 });

  const rb = await bodyOf(mod, monitor_id, '901');
  assert.equal(rb.included, true, `a listed type was thrown away for finishing first: ${rb.note}`);
  assert.equal(rb.content, '<p>early bytes</p>');
  assert.equal(mod.parkedBodies.size, 0);
});

test('a parked body still answers to content_types_to_capture, and the note names its type', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, LISTED_TYPES);
  const base = streamBeforeHeaders(mod, '911', 'https://x.test/logo.png',
                                   new Uint8Array([0x89, 0x50, 0x4e, 0x47]));
  fire(mod, 'onHeadersReceived', headersFor(base, 'image/png', 4, { fromCache: true }));
  fire(mod, 'onCompleted', { ...base, statusCode: 200, fromCache: true });

  assert.equal(mod.capturedResponseBodies.has('911'), false, 'image/png is not on the list');
  assert.equal(mod.parkedBodies.size, 0, 'judged and released, not left parked');
  const rb = await bodyOf(mod, monitor_id, '911');
  assert.equal(rb.included, false);
  assert.match(rb.note, /^No response body was captured/);
  assert.match(rb.note, /Content-Type \(image\/png\) is not in the monitor's content_types_to_capture/);
  // The cache is reported as a fact, and the note says it is not why the body is missing.
  assert.match(rb.note, /HTTP cache, which does not stop capture/);
});

test('a redirect hop\'s headers do not settle the type of the body that ends the chain', async (t) => {
  // Before Firefox 152 a redirect kept its requestId: the 301's headers describe the
  // hop, while the filter's bytes belong to the final response.
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, LISTED_TYPES);
  const json = new TextEncoder().encode('{"moved":true}');
  const base = streamBeforeHeaders(mod, '921', 'https://x.test/old', json);
  fire(mod, 'onHeadersReceived', { ...base, statusCode: 301,
    responseHeaders: [{ name: 'Content-Type', value: 'text/plain' },
                      { name: 'Location', value: '/new' }] });
  assert.ok(mod.parkedBodies.has('921'), 'the 301 must not commit the final body');

  fire(mod, 'onHeadersReceived', headersFor(base, 'application/json', json.byteLength));
  fire(mod, 'onCompleted', { ...base, statusCode: 200 });
  const rb = await bodyOf(mod, monitor_id, '921');
  assert.equal(rb.included, true, rb.note);
  assert.equal(rb.content_type, 'application/json');
});

test('a parked body whose request then FAILS is dropped, and the note says it failed', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, LISTED_TYPES);
  const base = streamBeforeHeaders(mod, '931', 'https://x.test/a.json',
                                   new TextEncoder().encode('{"a":1}'));
  fire(mod, 'onErrorOccurred', { ...base, error: 'NS_BINDING_ABORTED' });

  assert.equal(mod.parkedBodies.size, 0, 'no onCompleted follows an error; nothing may stay parked');
  assert.equal(mod.capturedResponseBodies.has('931'), false);
  const rb = await bodyOf(mod, monitor_id, '931');
  assert.match(rb.note, /the request failed \(NS_BINDING_ABORTED\)/);
});

test('retiring a monitor drops the bodies parked on its requests, even while another monitor lives', async (t) => {
  const mod = loadRequestsModule(t);
  const a = await startMonitor(mod, LISTED_TYPES, ['*a.test*']);
  const b = await startMonitor(mod, LISTED_TYPES, ['*b.test*']);
  streamBeforeHeaders(mod, '941', 'https://a.test/x.json', new TextEncoder().encode('{}'));
  streamBeforeHeaders(mod, '942', 'https://b.test/x.json', new TextEncoder().encode('{}'));
  assert.equal(mod.parkedBodies.size, 2);

  // B keeps the listeners installed, but no active monitor matches a.test any more, so
  // the headers that would commit 941 can never reach its record.
  await callAction(mod, 'requests.stop_monitoring', { monitor_id: a.monitor_id });
  assert.deepEqual([...mod.parkedBodies.keys()], ['942']);

  await callAction(mod, 'requests.stop_monitoring', { monitor_id: b.monitor_id });
  assert.equal(mod.parkedBodies.size, 0, 'the last monitor leaves nothing parked');
});

test('a parked body whose headers never come is aged out by the sweep', async (t) => {
  const mod = loadRequestsModule(t);
  await startMonitor(mod, LISTED_TYPES);
  streamBeforeHeaders(mod, '951', 'https://x.test/a.json', new TextEncoder().encode('{}'));
  streamBeforeHeaders(mod, '952', 'https://x.test/b.json', new TextEncoder().encode('{}'));
  mod.parkedBodies.get('951').at -= mod.consts.PARKED_BODY_MAX_AGE_MS + 1;

  mod.sweepMonitors();
  assert.deepEqual([...mod.parkedBodies.keys()], ['952'], 'only the stale entry goes');
});

test('parked bytes spend the body byte budget, and go before any stored body', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, { requestId: '961', url: 'https://x.test/kept.json', contentType: 'application/json' });
  finalizeBody(mod, '961', new TextEncoder().encode('{"kept":true}'), BODY_OPTS);

  const big = Math.round(mod.consts.CAPTURED_BODIES_MAX_BYTES / 2);
  for (let i = 0; i < 3; i++) mod.parkedBodies.set(`p${i}`, { cap: {}, at: Date.now(), bytes: big });
  mod.enforceCaptureCaps();

  let total = 0;
  for (const r of mod.capturedResponseBodies.values()) total += r.content_length;
  for (const p of mod.parkedBodies.values()) total += p.bytes;
  assert.ok(total <= mod.consts.CAPTURED_BODIES_MAX_BYTES, `total ${total} over the byte cap`);
  assert.ok(mod.capturedResponseBodies.has('961'), 'a body that passed its type check outranks unjudged bytes');
  assert.ok(mod.parkedBodies.has('p2'), 'the newest parked entry is kept');
});

test('a stream filter error and a refused attach are each named in the note', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, LISTED_TYPES);

  const failed = { requestId: '971', url: 'https://x.test/f.json', method: 'GET', tabId: 1,
                   frameId: 0, type: 'xmlhttprequest', timeStamp: Date.now() };
  fire(mod, 'onBeforeRequest', failed);
  const filter = mod.filters.get('971');
  filter.error = 'Channel redirected';
  filter.onerror();
  fire(mod, 'onHeadersReceived', headersFor(failed, 'application/json', 2));
  fire(mod, 'onCompleted', { ...failed, statusCode: 200 });
  assert.match((await bodyOf(mod, monitor_id, '971')).note,
    /the stream filter failed \(Channel redirected\)/);

  mod.browser.webRequest.filterResponseData = () => { throw new Error('Invalid request ID'); };
  const refused = { ...failed, requestId: '972', url: 'https://x.test/r.json' };
  fire(mod, 'onBeforeRequest', refused);
  fire(mod, 'onHeadersReceived', headersFor(refused, 'application/json', 2));
  fire(mod, 'onCompleted', { ...refused, statusCode: 200 });
  assert.match((await bodyOf(mod, monitor_id, '972')).note,
    /Firefox would not attach a stream filter to it \(Invalid request ID\)/);
});

// ---- request bodies ----------------------------------------------------------

test('a RAW (ArrayBuffer) request body is decoded, not JSON.stringify()d into "[{}]"', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  const payload = new TextEncoder().encode('{"q":"search","n":2}');
  seedRequest(mod, monitor_id, {
    requestId: '901', url: 'https://x.test/api', method: 'POST', contentType: 'application/json',
    requestBody: { raw: [{ bytes: payload.buffer.slice(0) }] }
  });

  const { response } = await callAction(mod, 'requests.get_content', { monitor_id, request_id: '901' });
  const body = response.data.request_body;
  assert.equal(body.included, true);
  assert.equal(body.content, '{"q":"search","n":2}');
  assert.equal(body.encoding, 'utf-8');
  assert.equal(body.size_bytes, payload.byteLength, 'the RAW byte size, not the length of a "[{}]" envelope');
});

test('a BINARY request body is withheld unless include_binary (and never faked)', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  const blob = new Uint8Array([0x00, 0xff, 0xfe, 0x01]);
  seedRequest(mod, monitor_id, {
    requestId: '911', url: 'https://x.test/upload', method: 'POST', contentType: 'application/octet-stream',
    requestBody: { raw: [{ bytes: blob.buffer.slice(0) }] }
  });

  const withheld = await callAction(mod, 'requests.get_content', { monitor_id, request_id: '911' });
  assert.equal(withheld.response.data.request_body.included, false);
  assert.equal(withheld.response.data.request_body.size_bytes, 4);
  assert.match(withheld.response.data.request_body.note, /include_binary=true/);

  const given = await callAction(mod, 'requests.get_content',
    { monitor_id, request_id: '911', include_binary: true });
  const body = given.response.data.request_body;
  assert.equal(body.included, true);
  assert.equal(body.encoding, 'base64');
  assert.deepEqual(new Uint8Array(Buffer.from(body.content, 'base64')), blob);
});

test('a formData request body stays JSON; a body Firefox exposed no bytes for says so honestly', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, {
    requestId: '921', url: 'https://x.test/form', method: 'POST',
    contentType: 'application/json', requestBody: { formData: { user: ['bob'] } }
  });
  const form = await callAction(mod, 'requests.get_content', { monitor_id, request_id: '921' });
  assert.equal(form.response.data.request_body.included, true);
  assert.equal(form.response.data.request_body.content, '{"user":["bob"]}');

  // A file upload: webRequest says there is a body but hands over no bytes.
  seedRequest(mod, monitor_id, {
    requestId: '922', url: 'https://x.test/file', method: 'POST',
    contentType: 'application/json', requestBody: { raw: [{ file: 'blob:…' }] }
  });
  const file = await callAction(mod, 'requests.get_content', { monitor_id, request_id: '922' });
  assert.equal(file.response.data.request_body.included, false);
  assert.equal(file.response.data.request_body.content, null, 'never invent a body');
  assert.equal(file.response.data.request_body.size_bytes, 0);
  assert.match(file.response.data.request_body.note, /no readable bytes/i);
});

// ---- sensitive headers -------------------------------------------------------

test('sensitive headers are REDACTED in get_content (the option is honoured, and Set-Cookie never leaks)', async (t) => {
  const mod = loadRequestsModule(t);
  const opts = { capture_response_bodies: true, max_body_size: 50000,
                 sensitive_headers: ['Authorization', 'Cookie', 'X-Api-Key'] };
  const { monitor_id } = await startMonitor(mod, opts);
  seedRequest(mod, monitor_id, {
    requestId: '1001', url: 'https://x.test/api', contentType: 'application/json',
    extraResponseHeaders: [
      { name: 'Set-Cookie', value: 'session=super-secret; HttpOnly' },
      { name: 'x-api-key', value: 'sk-live-123' },
      { name: 'X-Request-Id', value: 'abc-123' }
    ]
  });

  const { response } = await callAction(mod, 'requests.get_content', { monitor_id, request_id: '1001' });
  const headers = response.data.response_headers;
  assert.equal(headers['Set-Cookie'], '<redacted>', 'a session cookie must never land in an agent context');
  assert.equal(headers['x-api-key'], '<redacted>', 'case-insensitive match on the monitor\'s sensitive_headers');
  assert.equal(headers['X-Request-Id'], 'abc-123', 'everything else is passed through');
  assert.equal(headers['Content-Type'], 'application/json');
});

// ---- request headers (C-F5 regression) ---------------------------------------
// request_headers was ALWAYS {}: the listener meant to carry them was declared but
// never registered, so no event carried them and get_content's loop had nothing to
// read. The tool reported an empty object as if the request genuinely sent no
// headers — and request_body.content_type, which is derived from them, was always
// null for the same reason. They now ride onBeforeSendHeaders (which is already
// registered with ["requestHeaders"]); see setupWebRequestListeners for why a
// separate onSendHeaders listener is NOT an option.

test('request_headers are actually captured, and drive request_body.content_type', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, {
    requestId: '1021', url: 'https://x.test/api', method: 'POST',
    contentType: 'application/json',
    requestHeaders: [
      { name: 'Content-Type', value: 'application/json' },
      { name: 'X-Trace', value: 'trace-42' }
    ],
    requestBody: { raw: [{ bytes: new TextEncoder().encode('{"a":1}').buffer }] }
  });

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id, request_id: '1021' });

  assert.deepEqual(response.data.request_headers,
    { 'Content-Type': 'application/json', 'X-Trace': 'trace-42' },
    'the headers the request actually sent — not {}');
  assert.equal(response.data.request_body.content_type, 'application/json',
    'content_type is derived from the request headers, so it was null while they were {}');
});

test('our own Accept-Encoding override never shows up as a header the page sent', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  // A body monitor rewrites Accept-Encoding to `identity` IN PLACE on the same
  // details.requestHeaders array the capture reads — so aliasing it (rather than
  // copying) reports our override back as the page's own header, and the agent
  // debugging "why is this response uncompressed?" is chasing us.
  seedRequest(mod, monitor_id, {
    requestId: '1023', url: 'https://x.test/api', contentType: 'application/json',
    requestHeaders: [{ name: 'Accept-Encoding', value: 'gzip, deflate, br' },
                     { name: 'Accept', value: 'application/json' }]
  });

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id, request_id: '1023' });
  assert.equal(response.data.request_headers['Accept-Encoding'], 'gzip, deflate, br',
    'reported the page\'s real Accept-Encoding, not our identity override');
});

test('a redirect chain reports the FIRST hop\'s headers — the hop `url` names', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  const base = { requestId: '1026', method: 'GET', tabId: 1, frameId: 0,
                 type: 'xmlhttprequest', timeStamp: Date.now() };
  // One requestId, two hops. `url` is stamped at record creation from hop 1 and never
  // moves, but onBeforeSendHeaders fires per hop — so last-hop-wins would report the
  // credentials sent to accounts.test under the shop.test url.
  mod.captureRequestEvent(monitor_id, 'onBeforeRequest',
    { ...base, url: 'https://shop.test/checkout' });
  const fire = mod.installed.get('onBeforeSendHeaders');
  fire({ ...base, url: 'https://shop.test/checkout',
         requestHeaders: [{ name: 'X-Api-Key', value: 'shop-key' },
                          { name: 'X-Hop', value: 'first' }] });
  fire({ ...base, url: 'https://accounts.test/sso',
         requestHeaders: [{ name: 'X-Api-Key', value: 'sso-key-different-origin' },
                          { name: 'X-Hop', value: 'second' }] });
  mod.captureRequestEvent(monitor_id, 'onHeadersReceived', { ...base,
    url: 'https://accounts.test/sso', statusCode: 200,
    responseHeaders: [{ name: 'Content-Type', value: 'application/json' }] });
  mod.captureRequestEvent(monitor_id, 'onCompleted',
    { ...base, url: 'https://accounts.test/sso', statusCode: 200 });

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id, request_id: '1026' });
  assert.equal(response.data.request_headers['X-Hop'], 'first',
    'headers must describe the same hop as the reported url (the first)');
});

test('uncaptured request headers say so instead of posing as "sent none"', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  // No requestHeaders => seedRequest fires no onBeforeSendHeaders, which is what a
  // cache-served response really looks like.
  seedRequest(mod, monitor_id, {
    requestId: '1027', url: 'https://x.test/cached.json', contentType: 'application/json' });

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id, request_id: '1027' });
  assert.deepEqual(response.data.request_headers, {});
  assert.match(response.data.request_headers_note || '', /not captured/i,
    'an empty {} with no note reads as "the request sent no headers", which is never true');
});

test('captured request headers carry NO note (the note means "not captured")', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, {
    requestId: '1028', url: 'https://x.test/api', contentType: 'application/json',
    requestHeaders: [{ name: 'Accept', value: 'application/json' }] });

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id, request_id: '1028' });
  assert.equal(response.data.request_headers_note, undefined,
    'a note on real headers would train agents to ignore it');
});

test('the redaction floor covers api-key/token headers, not just authorization+cookie',
     async (t) => {
  const mod = loadRequestsModule(t);
  // Default options — NO sensitive_headers. The server's own default is a subset of
  // the floor, so this is exactly what an agent calling requests(action="start") with
  // no options gets. request_headers were always {} until now, so these header
  // spellings had never been exposed at all; the floor was sized for RESPONSE headers
  // (set-cookie) and would have handed live API keys to the agent verbatim.
  const { monitor_id } = await startMonitor(mod, { capture_response_bodies: true });
  seedRequest(mod, monitor_id, {
    requestId: '1024', url: 'https://x.test/api', contentType: 'application/json',
    requestHeaders: [
      { name: 'X-Api-Key', value: 'sk-live-REAL' },
      { name: 'X-Auth-Token', value: 'REAL-AUTH' },
      { name: 'X-CSRF-Token', value: 'REAL-CSRF' },
      { name: 'X-Request-Id', value: 'abc-123' },
    ]
  });

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id, request_id: '1024' });
  const h = response.data.request_headers;
  for (const name of ['X-Api-Key', 'X-Auth-Token', 'X-CSRF-Token']) {
    assert.equal(h[name], '<redacted>', `${name} is a live credential and must not leak`);
  }
  assert.equal(h['X-Request-Id'], 'abc-123', 'non-credential headers still pass through');
});

test('the redaction floor is NOT overridable by sensitive_headers: []', async (t) => {
  const mod = loadRequestsModule(t);
  // The docstring used to tell agents "pass [] to see them verbatim". It cannot, and
  // must not — so the docstring changed, and this pins the behaviour it now promises.
  const { monitor_id } = await startMonitor(mod,
    { capture_response_bodies: true, sensitive_headers: [] });
  seedRequest(mod, monitor_id, {
    requestId: '1025', url: 'https://x.test/api', contentType: 'application/json',
    requestHeaders: [{ name: 'Authorization', value: 'Bearer REAL' },
                     { name: 'X-Api-Key', value: 'sk-live-REAL' }]
  });

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id, request_id: '1025' });
  assert.equal(response.data.request_headers['Authorization'], '<redacted>');
  assert.equal(response.data.request_headers['X-Api-Key'], '<redacted>');
});

test('request headers get the same redaction floor as response headers', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, {
    requestId: '1022', url: 'https://x.test/api', contentType: 'application/json',
    requestHeaders: [
      { name: 'Authorization', value: 'Bearer sk-live-super-secret' },
      { name: 'Cookie', value: 'session=super-secret' },
      { name: 'Accept', value: 'application/json' }
    ]
  });

  const { response } = await callAction(mod, 'requests.get_content',
    { monitor_id, request_id: '1022' });
  const h = response.data.request_headers;
  // Now that these are real, they are a live credential-leak path into the agent's
  // context. The floor must hold WITHOUT the monitor opting in to sensitive_headers.
  assert.equal(h['Authorization'], '<redacted>', 'bearer token must never reach the agent');
  assert.equal(h['Cookie'], '<redacted>', 'session cookie must never reach the agent');
  assert.equal(h['Accept'], 'application/json', 'everything else passes through');
});

test('redaction survives the monitor\'s retirement (its captures are still readable)', async (t) => {
  const mod = loadRequestsModule(t);
  const { monitor_id } = await startMonitor(mod, BODY_OPTS);
  seedRequest(mod, monitor_id, {
    requestId: '1011', url: 'https://x.test/api', contentType: 'application/json',
    extraResponseHeaders: [{ name: 'Set-Cookie', value: 'session=secret' }]
  });
  await callAction(mod, 'requests.stop_monitoring', { monitor_id });

  const { response } = await callAction(mod, 'requests.get_content', { monitor_id, request_id: '1011' });
  assert.equal(response.data.response_headers['Set-Cookie'], '<redacted>');
});
