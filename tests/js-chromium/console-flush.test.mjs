/*
 * A console digest is read only after the page has handed over what it emitted (the
 * chromium twin of tests/js/console-flush.test.mjs).
 *
 * content.js sends console entries to the worker on a 300 ms batch timer, and a
 * background tab's timer clamp stretches that to about a second. navigate settles in about
 * 250 ms, so a digest read straight after the settle would miss the page's load-time
 * errors. Each reader of TAB_CONSOLE (navigate in all three waits, a page_act step and its
 * timeout report, console.read) sends 'flushConsole' to the top frame first, and the relay
 * answers with the entries the worker may not hold yet.
 *
 * Two halves, both read from this tree:
 *   - background.js, driven with a stub relay that keeps an entry until it is flushed. A
 *     relay that never answers must not hang the call, and the result must say the digest
 *     may be short. The digest counts only what the call's own window holds: an entry an
 *     earlier call already took, or one from the document a navigation left, is not this
 *     call's.
 *   - content.js (ISOLATED) with content-main.js (MAIN) under jsdom: page console calls
 *     and page errors reach content.js in a synchronous '__browserplugConsole' event, and
 *     the relay answers in the task that receives the request, with what is pending and
 *     what is still in flight, so a busy page delays the answer by one task. No timer
 *     stands in the way.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');
const CONTENT_SRC = fs.readFileSync(
  new URL('../../extension-chromium/content.js', import.meta.url), 'utf-8');
const CONTENT_MAIN_SRC = fs.readFileSync(
  new URL('../../extension-chromium/content-main.js', import.meta.url), 'utf-8');

// A missing anchor must be loud: a slice that finds nothing would make every assertion
// below pass vacuously. Fix the anchors here, not the product.
function slice(startAnchor, endAnchor, what) {
  const start = BG_SRC.indexOf(startAnchor);
  const end = BG_SRC.indexOf(endAnchor, start);
  assert.ok(start >= 0 && end > start,
    `${what} not found in background.js; the slice anchors moved. Anchors: ` +
    `${JSON.stringify(startAnchor)} .. ${JSON.stringify(endAnchor)}`);
  return BG_SRC.slice(start, end);
}

const SINK = slice("// Console entries relayed by the content script's capture layer",
  '// END console-entries sink', 'the console-entries sink');
const FLUSH = slice('function consoleErrorsSince(tabId, t0)',
  '// Background-driven wait used by content.act', 'consoleErrorsSince and the console flush');
const NAVIGATE = slice('async function handleNavigationAction(', '// Windows handlers',
  'the navigation handler');
const ACT = slice('// ---- content.act step deadline', '// Content handlers',
  'the content.act step loop');
const CONSOLE_READ = slice('// Console buffer reader (console.read action)',
  '// --- Screenshot capture', 'the console.read handler');

const PAGE = 'https://x.test/console-noise.html';
const entry = (level, text) => ({ level, text, ts: Date.now(), url: PAGE });
// An entry as content.js stamps it: its document and its number in that document.
let numbered = 0;
const docEntry = (doc, level, text) => Object.assign(entry(level, text), { doc, n: ++numbered });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const loadErrors = () => [
  entry('warn', 'load-warning A'),
  entry('error', 'load-error B'),
  entry('error', 'Uncaught: Error: thrown-error C (https://x.test/console-noise.html:19)'),
];

// The tab's capture relay as the background sees it. `held` is what the page emitted that
// still sits in content.js's batch. 'answers': a flushConsole hands it to the background's
// sink as a batch, then answers. 'replies': the answer itself carries it, as content.js
// does. 'both': a batch still in flight, so it arrives as a batch and in the answer.
// 'silent': a page whose main thread never gets to the message. 'absent': no content
// script in the tab, so sendMessage rejects at once. `doc` names the document that answers.
function makeRelay(mode, held = [], doc) {
  const asked = [];
  const relay = (tabId, message, options, deliver) => {
    asked.push({ tabId, message, options });
    if (mode === 'silent') return new Promise(() => {});
    if (mode === 'absent') {
      return Promise.reject(new Error('Could not establish connection. Receiving end does not exist.'));
    }
    if (mode === 'replies') {
      return new Promise((resolve) => setImmediate(() =>
        resolve({ ok: true, doc, entries: held.splice(0) })));
    }
    return new Promise((resolve) => setImmediate(() => {
      const entries = held.splice(0);
      deliver(entries);
      setImmediate(() => resolve(mode === 'both'
        ? { ok: true, doc, entries: entries.map(e => Object.assign({}, e)) }
        : { ok: true, dispatched: true, doc }));
    }));
  };
  return { relay, asked };
}

// The parts every harness shares: the real sink, the real digest and flush, and a browser
// whose tabs.sendMessage reaches the stub relay.
const COMMON = `
  const TAB_CONSOLE = new Map();
  const CONSOLE_MAX = 600;
  const TAB_NAV = new Map();
  const sink = function (request, sender) {
    ${SINK}
    return undefined;
  };
  const deliverTo = (tabId) => (entries) =>
    sink({ type: 'console_entries', entries }, { tab: { id: tabId } });
  const replies = [];
  const sendResponse = (id, action, data) => { replies.push({ id, action, data }); };
  const sendError = (id, code, message) => { replies.push({ id, code, message }); };
  const resolveTabId = async (data) => data.tabId;
  ${FLUSH}
`;

const SETTLED = {
  settled: true, timedOut: false, url: PAGE, title: 'BrowserPlug Console Noise Fixture',
  status: 'complete', waitedMs: 260, navCommitted: true,
  timing: { commit: 20, load: 40, quiet: 260, settled: 260 },
};

function loadNavigate(relay) {
  // eslint-disable-next-line no-new-func
  return new Function('relay', 'SETTLED', `
    ${COMMON}
    const browser = { tabs: {
      update: async () => ({}),
      get: async () => ({ url: SETTLED.url, title: SETTLED.title, status: 'complete' }),
      sendMessage: (tabId, message, options) => relay(tabId, message, options, deliverTo(tabId)),
    } };
    const androidFileUrlRefusal = () => null;
    const fileUrlRefusal = async () => null;
    const navUrlBefore = async () => 'https://x.test/titled.html';
    async function applyProxyField() { return { change: undefined }; }
    const NAV_QUIET_MS = 250;
    const DOM_STABLE_MAX_MS = 3000;
    const DOM_ARMED = new Map();
    const DOM_ARM_MS = 5000;
    async function probeDomWatch() { return null; }
    const ABORT_ECHO_GRACE_MS = 1000;
    async function waitForSettle() { return JSON.parse(JSON.stringify(SETTLED)); }
    async function waitForStableDom() { return { stable: true, ms: 100 }; }
    function abortAwaitingCompletion() { return false; }
    async function waitForTabCondition() { return true; }
    function netDigestFor() { return { requests: 3, pending: 0 }; }
    async function captureServiceWorkers() { return null; }
    function mainFrameOutcome() { return { httpStatus: 200 }; }
    function navigationError() { return null; }
    function humanizeNetError(e) { return String(e); }
    function navEventsSince() { return []; }
    function isNavAbort() { return false; }
    ${NAVIGATE}
    return {
      navigate: async (data) => {
        await handleNavigationAction('n1', 'navigation.navigate',
          Object.assign({ op: 'goto', url: SETTLED.url, tabId: 7 }, data));
        assert(replies.length === 1, 'one reply per navigate');
        return replies[0].data || replies[0];
      },
      // What an earlier call's flush or the batch timer already put in TAB_CONSOLE.
      deliver: (tabId, entries) => deliverTo(tabId)(entries),
      TAB_CONSOLE
    };
    function assert(ok, msg) { if (!ok) throw new Error(msg); }
  `)(relay, SETTLED);
}

for (const wait of ['settled', 'load', 'dom']) {
  test(`wait="${wait}": errors still in the tab's batch at the settle reach the navigate result`, async () => {
    const { relay, asked } = makeRelay('answers', loadErrors());
    const m = loadNavigate(relay);
    const r = await m.navigate({ wait });
    assert.ok(r.consoleErrors, 'the console digest is empty: it was read before the page handed its entries over');
    assert.equal(r.consoleErrors.errors, 2, 'console.error and the uncaught throw, not the warning');
    assert.match(r.consoleErrors.first.join(' | '), /load-error B/);
    assert.match(r.consoleErrors.first.join(' | '), /thrown-error C/);
    assert.equal(r.consoleFlushTimedOut, undefined);
    assert.equal(asked.length, 1);
    assert.equal(asked[0].tabId, 7);
    assert.deepEqual(asked[0].message, { action: 'flushConsole' });
    assert.deepEqual(asked[0].options, { frameId: 0 }, 'console capture runs in the top frame only');
  });
}

test('a relay that never answers does not hang navigate, and the result says so', async () => {
  const { relay } = makeRelay('silent', loadErrors());
  const m = loadNavigate(relay);
  const started = Date.now();
  const r = await m.navigate({ wait: 'settled' });
  const took = Date.now() - started;
  assert.equal(r.consoleFlushTimedOut, true);
  assert.equal(r.consoleErrors, undefined, 'nothing was handed over');
  assert.equal(r.settled, true, 'the navigation itself still succeeded');
  assert.ok(took >= 450 && took < 2000, `bounded by CONSOLE_FLUSH_TIMEOUT_MS, took ${took} ms`);
});

test('a tab with no relay answers at once and is not reported as a timeout', async () => {
  const { relay, asked } = makeRelay('absent');
  const m = loadNavigate(relay);
  const started = Date.now();
  const r = await m.navigate({ wait: 'load' });
  assert.equal(asked.length, 1);
  assert.equal(r.consoleFlushTimedOut, undefined);
  assert.ok(Date.now() - started < 200, 'a refusal costs no wait');
});

for (const wait of ['settled', 'load', 'dom']) {
  test(`wait="${wait}": entries handed over in the flush answer reach the navigate result`, async () => {
    const { relay } = makeRelay('replies', [
      docEntry('doc-new', 'error', 'load-error B'),
      docEntry('doc-new', 'error', 'Uncaught: Error: thrown-error C'),
    ], 'doc-new');
    const r = await loadNavigate(relay).navigate({ wait });
    assert.ok(r.consoleErrors, 'the entries in the answer were not taken into the digest');
    assert.equal(r.consoleErrors.errors, 2);
    assert.equal(r.consoleFlushTimedOut, undefined);
  });
}

test('an entry that arrives both in a batch and in the flush answer is counted once', async () => {
  const { relay } = makeRelay('both', [
    docEntry('doc-new', 'error', 'load-error B'),
    docEntry('doc-new', 'error', 'Uncaught: Error: thrown-error C'),
  ], 'doc-new');
  const m = loadNavigate(relay);
  const r = await m.navigate({ wait: 'settled' });
  assert.equal(r.consoleErrors.errors, 2);
  assert.equal(m.TAB_CONSOLE.get(7).length, 2, 'the buffer holds one copy of each');
});

// wait="load" returns about 30 ms after the load event, so the next call begins a few ms
// after the earlier document's errors were logged, well inside the 50 ms allowance on ts.
test('navigate leaves out entries an earlier call already took, though they were logged just before t0', async () => {
  const earlier = [docEntry('doc-old', 'error', 'load-error B (old)'),
                   docEntry('doc-old', 'error', 'thrown-error C (old)')];
  const { relay } = makeRelay('answers', [docEntry('doc-new', 'error', 'load-error B (new)'),
                                          docEntry('doc-new', 'error', 'thrown-error C (new)')], 'doc-new');
  const m = loadNavigate(relay);
  m.deliver(7, earlier);
  await sleep(5);
  const r = await m.navigate({ wait: 'dom' });
  assert.equal(r.consoleErrors.errors, 2, 'the previous document\'s errors were counted again');
  assert.ok(r.consoleErrors.first.every(t => /\(new\)/.test(t)),
    `the list must describe the counted set: ${r.consoleErrors.first.join(' | ')}`);
});

test('navigate leaves out what the page it left delivers after the navigation began', async () => {
  const { relay } = makeRelay('answers', [
    docEntry('doc-old', 'error', 'beforeunload error (old)'),
    docEntry('doc-new', 'error', 'load-error B (new)'),
  ], 'doc-new');
  const r = await loadNavigate(relay).navigate({ wait: 'settled' });
  assert.equal(r.consoleErrors.errors, 1);
  assert.match(r.consoleErrors.first[0], /\(new\)/);
});

test('with no answer to name the document, entries taken before t0 still stay out', async () => {
  const { relay } = makeRelay('silent');
  const m = loadNavigate(relay);
  m.deliver(7, [docEntry('doc-old', 'error', 'load-error B (old)')]);
  await sleep(5);
  const r = await m.navigate({ wait: 'load' });
  assert.equal(r.consoleFlushTimedOut, true);
  assert.equal(r.consoleErrors, undefined);
});

test('wait="none" builds no digest, so it does not flush', async () => {
  const { relay, asked } = makeRelay('silent');
  const m = loadNavigate(relay);
  const r = await m.navigate({ wait: 'none' });
  assert.equal(asked.length, 0);
  assert.equal(r.consoleFlushTimedOut, undefined);
});

// ---- page_act: the post-step digest and the timeout report ---------------------------

function loadAct(relay) {
  // eslint-disable-next-line no-new-func
  return new Function('relay', `
    ${COMMON}
    let hang = false;
    const browser = { tabs: {
      get: async () => ({ url: 'https://x.test/form.html', title: 'Form', status: 'complete' }),
      sendMessage: (tabId, message, options) => relay(tabId, message, options, deliverTo(tabId)),
    } };
    function netDigestSince() { return { requests: 0, pending: 0 }; }
    async function runAutomation(tabId, method) {
      if (method === 'inspect') return { ok: true, watched: [], nodesAtStart: 100, nodesAtEnd: 100 };
      if (hang) return new Promise(() => {});
      return { ok: true, clicked: true, filled: true, nodesAtStart: 100, nodesAtEnd: 100 };
    }
    async function actWait() { return { ok: true, matched: 'selector', elapsedMs: 5 }; }
    async function waitForSettle() {
      return { settled: true, url: 'https://x.test/form.html', navCommitted: false };
    }
    ${ACT}
    return { runActSteps, hangSteps: () => { hang = true; },
      deliver: (tabId, entries) => deliverTo(tabId)(entries) };
  `)(relay);
}

for (const step of [{ action: 'click', target: 'Save' }, { action: 'fill', target: '#name', value: 'x' }]) {
  test(`page_act ${step.action}: an error its handler raised reaches the step result`, async () => {
    const { relay, asked } = makeRelay('answers', [entry('error', 'TypeError: save failed')]);
    const m = loadAct(relay);
    const out = await m.runActSteps(7, { steps: [step] }, undefined);
    const res = out.results[0];
    assert.equal(res.ok, true);
    assert.ok(res.consoleErrors, 'the step digest was read before the error was handed over');
    assert.equal(res.consoleErrors.errors, 1);
    assert.match(res.consoleErrors.first[0], /save failed/);
    assert.deepEqual(asked.map(a => a.message.action), ['flushConsole']);
  });
}

test('page_act: an error the page logged before the step began is not the step\'s', async () => {
  // A navigate(wait="load") just reported it; the click that follows did not cause it.
  const { relay } = makeRelay('replies', [], 'doc-1');
  const m = loadAct(relay);
  m.deliver(7, [docEntry('doc-1', 'error', 'load-error B')]);
  await sleep(5);
  const out = await m.runActSteps(7, { steps: [{ action: 'click', target: 'Calm' }] }, undefined);
  assert.equal(out.results[0].ok, true);
  assert.equal(out.results[0].consoleErrors, undefined);
});

test('page_act: a relay that never answers leaves the step ok and says the digest may be short', async () => {
  const { relay } = makeRelay('silent', [entry('error', 'TypeError: save failed')]);
  const m = loadAct(relay);
  const started = Date.now();
  const out = await m.runActSteps(7, { steps: [{ action: 'click', target: 'Save' }] }, undefined);
  assert.equal(out.results[0].ok, true);
  assert.equal(out.results[0].consoleFlushTimedOut, true);
  assert.ok(Date.now() - started < 2000);
});

test('page_act: a step that times out still flushes before its report reads the console', async () => {
  const { relay, asked } = makeRelay('answers', [entry('error', 'Uncaught: RangeError: stuck')]);
  const m = loadAct(relay);
  m.hangSteps();
  const out = await m.runActSteps(7, { steps: [{ action: 'click', target: 'Save', timeout_ms: 1 }] },
    undefined);
  const res = out.results[0];
  assert.equal(res.actionTimedOut, true);
  assert.ok(res.consoleErrors, 'the timeout report read the console before the flush');
  assert.match(res.consoleErrors.first[0], /stuck/);
  assert.equal(asked.length, 1);
});

// ---- console.read ------------------------------------------------------------------------

test('console.read returns entries that were still in the tab\'s batch', async () => {
  const { relay } = makeRelay('answers', loadErrors());
  // eslint-disable-next-line no-new-func
  const m = new Function('relay', `
    ${COMMON}
    const browser = { tabs: {
      sendMessage: (tabId, message, options) => relay(tabId, message, options, deliverTo(tabId)),
    } };
    ${CONSOLE_READ}
    return { handleConsoleAction, replies };
  `)(relay);
  await m.handleConsoleAction('c1', 'console.read', { tabId: 7, since: 'ts', sinceTs: 0, level: 'error' });
  const r = m.replies[0].data;
  assert.equal(r.total, 2);
  assert.equal(r.consoleFlushTimedOut, undefined);
});

// ---- content.js: the relay end ------------------------------------------------------------
// window.postMessage is replaced by an ordered queue that dispatches each message in its
// own later task with source === window, the way a browser delivers posted messages from
// either world; `stalled` never dispatches them, which is a page whose main thread stays
// busy. The batch timer is captured and fires only when a test fires it, which is what a
// background tab's throttled timer looks like to a caller that wants the entries now.

function loadRelay(opts = {}) {
  const dom = new JSDOM('<!DOCTYPE html><html><head><title>Noise</title></head><body></body></html>',
    { runScripts: 'outside-only', url: PAGE, virtualConsole: new VirtualConsole() });
  const { window } = dom;
  const queue = [];
  let draining = false;
  const drain = () => {
    const data = queue.shift();
    window.dispatchEvent(new window.MessageEvent('message', { data, source: window }));
    if (queue.length) setImmediate(drain); else draining = false;
  };
  window.postMessage = (data) => {
    queue.push(data);
    if (!draining && !opts.stalled) { draining = true; setImmediate(drain); }
  };
  const timers = [];
  window.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  window.clearTimeout = () => {};

  const sent = [];
  const takers = [];
  let onMessage = null;
  window.browser = { runtime: {
    sendMessage: (msg) => {
      // content.js also asks for the monitor state at document_start.
      if (!msg || msg.type !== 'console_entries') return Promise.resolve(null);
      const batch = { texts: Array.from(msg.entries || [], e => String(e.text)),
                      entries: msg.entries || [], taken: false };
      sent.push(batch);
      return new Promise((resolve) => {
        const take = () => { batch.taken = true; resolve(undefined); };
        if (opts.manualTake) takers.push(take); else setImmediate(take);
      });
    },
    onMessage: { addListener: (fn) => { onMessage = fn; } },
  } };
  if (opts.mainWorld) window.eval(CONTENT_MAIN_SRC);
  window.eval(CONTENT_SRC);
  return {
    window, sent, timers,
    takeAll: () => { while (takers.length) takers.shift()(); },
    ask: (request) => new Promise((resolve) => {
      const keepOpen = onMessage(request, {}, resolve);
      if (keepOpen !== true) resolve(undefined);
    }),
    // What the relay answered before its listener returned: undefined if it did not.
    askNow: (request) => {
      let answer;
      onMessage(request, {}, (a) => { answer = a; });
      return answer;
    },
  };
}

const ticks = async (n) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };
// The answer is built in the jsdom realm; copy it out before deepEqual compares prototypes.
const texts = (answer) => Array.from(answer.entries, e => String(e.text));
// What content-main.js's error capture dispatches for a page throw.
const handOver = (window, level, text) => window.dispatchEvent(
  new window.CustomEvent('__browserplugConsole', { detail: level + ':' + text }));

test('content.js: a console.error the page made before the request is in the answer', async () => {
  const r = loadRelay({ mainWorld: true });
  assert.equal(r.window.__browserplugConsoleHooked, true, 'the MAIN-world hook is installed');
  r.window.console.error('load-error B');
  const answer = await r.ask({ action: 'flushConsole' });
  assert.equal(answer.ok, true);
  assert.deepEqual(texts(answer), ['load-error B']);
  assert.equal(r.sent.length, 0, 'the answer carries the entry, so no batch is sent for it');
  assert.ok(r.timers.every(t => t.ms === 300), 'only the batch timer was armed; the flush uses none');
});

test('content.js: the answer comes in the task that receives the request, even with the page queue stalled', () => {
  const r = loadRelay({ mainWorld: true, stalled: true });
  r.window.console.error('load-error B');
  const answer = r.askNow({ action: 'flushConsole' });
  assert.ok(answer, 'the relay waited on a task behind the page\'s own before answering');
  assert.deepEqual(texts(answer), ['load-error B'],
    'the page\'s console call was not recorded before it returned');
  assert.equal(typeof answer.doc, 'string');
  assert.ok(answer.entries.every(e => e.doc === answer.doc), 'entries carry their document');
});

test('content.js: a page error handed over from the MAIN world is in the answer', async () => {
  // On Chrome the ISOLATED world never sees the page's own throw; content-main.js hands it
  // over in the same event as a console call.
  const r = loadRelay();
  handOver(r.window, 'error', 'Uncaught: Error: thrown-error C (https://x.test/console-noise.html:19)');
  const answer = await r.ask({ action: 'flushConsole' });
  assert.deepEqual(texts(answer),
    ['Uncaught: Error: thrown-error C (https://x.test/console-noise.html:19)']);
});

test('content.js: a batch the worker has not taken yet rides in the answer', async () => {
  const r = loadRelay({ mainWorld: true, manualTake: true });
  r.window.console.error('early');
  assert.equal(r.timers.length, 1, 'the entry is recorded and the batch timer armed');
  r.timers[0].fn();                       // the timer fires: the batch leaves, not yet taken
  assert.equal(r.sent.length, 1);
  const answer = r.askNow({ action: 'flushConsole' });
  assert.ok(answer, 'the relay waited for the worker to take the batch');
  assert.deepEqual(texts(answer), ['early']);
  assert.equal(answer.entries[0].doc, r.sent[0].entries[0].doc);
  assert.equal(answer.entries[0].n, r.sent[0].entries[0].n, 'the copies share doc and n');
  r.takeAll();
  await ticks(2);
  assert.deepEqual(texts(r.askNow({ action: 'flushConsole' })), [],
    'a batch the worker took is not carried again');
});

test('content.js: a page listener that logs on the handover event does not re-enter the hook', () => {
  const r = loadRelay({ mainWorld: true });
  r.window.addEventListener('__browserplugConsole', () => { r.window.console.log('echo'); });
  r.window.console.error('once');
  assert.deepEqual(texts(r.askNow({ action: 'flushConsole' })), ['once']);
});

test('content.js: a flush with nothing pending answers without sending', async () => {
  const r = loadRelay();
  const answer = await r.ask({ action: 'flushConsole' });
  assert.equal(answer.ok, true);
  assert.equal(r.sent.length, 0);
});
