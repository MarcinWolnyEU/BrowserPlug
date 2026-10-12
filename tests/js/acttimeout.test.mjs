/*
 * A timed-out act step still reports what the page looks like.
 *
 * The failure being fixed: a click that navigates tears down the document the
 * automation toolkit was injected into, so tabs.executeScript's promise never
 * settles. The whole content.act batch then overran the SERVER's request budget and
 * the agent got a bare "Request timed out after 22.1 seconds" — which reads as
 * "nothing happened" and was wrong every single time it was checked (the load-more
 * HAD loaded; the search submit HAD navigated; once the click had navigated to a
 * completely different product).
 *
 * So each step races a deadline and an overrun produces a STRUCTURED result: the
 * tab's url/title/status now, whether a navigation committed since the step began,
 * and the network/console digest — enough to tell "it worked, just slowly" from "it
 * genuinely did nothing".
 *
 * Like actwait.test.mjs: background.js is a top-level extension script, so the block
 * is sliced out by anchor and driven with stubs. The gecko/chromium twin pair exists
 * because the two trees inject through different APIs.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

function loadActTimeout() {
  const start = BG_SRC.indexOf('// ---- content.act step deadline');
  const end = BG_SRC.indexOf('// Content handlers', start);
  assert.ok(start >= 0 && end > start, 'act step deadline block not found in background.js');
  const block = BG_SRC.slice(start, end);

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const TAB_NAV = new Map();
    let tabState = { url: 'https://shop.example/listing', title: 'Listing', status: 'complete' };
    let tabGone = false;
    const browser = {
      tabs: {
        get: async () => {
          if (tabGone) throw new Error('No tab with id');
          return tabState;
        }
      }
    };
    let netDigest = { requests: 0, pending: 0 };
    let consoleDigest = { errors: 0, first: [] };
    function netDigestSince() { return netDigest; }
    function consoleErrorsSince() { return consoleDigest; }

    ${block}

    return {
      ACT_STEP_TIMEOUT_MS, ACT_MIN_STEP_MS,
      actStepBudget, actClampWait, actTimeoutReport, runActStepWithDeadline,
      setTab: (t) => { tabState = Object.assign(tabState, t); },
      killTab: () => { tabGone = true; },
      setNav: (v) => { TAB_NAV.set(1, v); },
      setNet: (v) => { netDigest = v; },
      setConsole: (v) => { consoleDigest = v; }
    };
  `);
  return factory();
}

test('a step budget defaults, honours its own timeout, and never outlives the batch', () => {
  const m = loadActTimeout();
  assert.equal(m.actStepBudget(null, undefined), m.ACT_STEP_TIMEOUT_MS);
  assert.equal(m.actStepBudget(null, 3000), 3000);
  assert.equal(m.actStepBudget(null, 0), m.ACT_STEP_TIMEOUT_MS, 'a bogus 0 falls back');
  const tight = m.actStepBudget(Date.now() + 4000, 30000);
  assert.ok(tight > 3000 && tight <= 4000,
    `batch deadline wins over the step's own ask (got ${tight})`);
});

test('a wait is clamped to the batch deadline, not silently allowed to overrun it', () => {
  const m = loadActTimeout();
  const step = { action: 'wait', timeout_ms: 60000, target: '#x' };
  const same = m.actClampWait(step, null);
  assert.equal(same, step, 'no batch deadline => the step is untouched (same object)');

  const clamped = m.actClampWait(step, Date.now() + 5000);
  assert.notEqual(clamped, step, 'a copy, never a mutation of the caller\'s step');
  assert.ok(clamped.timeout_ms <= 5000 && clamped.timeout_ms > 3500);
  assert.equal(clamped.target, '#x', 'the rest of the step survives the clamp');
  assert.equal(step.timeout_ms, 60000, 'the original is not modified');

  const roomy = m.actClampWait({ action: 'wait', timeout_ms: 2000 }, Date.now() + 60000);
  assert.equal(roomy.timeout_ms, 2000, 'plenty of budget => no clamp');
});

test('a step that finishes inside its budget is returned untouched', async () => {
  const m = loadActTimeout();
  const res = await m.runActStepWithDeadline(
    1, async () => ({ ok: true, clicked: true }), 5000, Date.now(), { kind: 'click' });
  assert.equal(res.ok, true);
  assert.equal(res.clicked, true);
  assert.equal(res.actionTimedOut, undefined, 'no timeout machinery leaks into a normal result');
});

test('a step that never settles yields a report carrying the page state', async () => {
  const m = loadActTimeout();
  m.setTab({ url: 'https://shop.example/listing?page=2', title: 'Listing p2', status: 'complete' });
  const res = await m.runActStepWithDeadline(
    1, () => new Promise(() => {}), 100, Date.now(),
    { kind: 'click', urlBefore: 'https://shop.example/listing' });
  assert.equal(res.ok, false);
  assert.equal(res.timedOut, true);
  assert.equal(res.actionTimedOut, true, 'distinct from a wait-step timeout');
  assert.equal(res.urlAfter, 'https://shop.example/listing?page=2');
  assert.equal(res.titleAfter, 'Listing p2');
  assert.equal(res.tabStatus, 'complete');
  assert.equal(res.urlBefore, 'https://shop.example/listing');
  assert.equal(res.urlChanged, true);
  assert.match(res.error, /did not report back/);
});

test('a timed-out step that NAVIGATED says so, and says to check where you landed', async () => {
  const m = loadActTimeout();
  const t0 = Date.now();
  m.setNav({ lastCommitted: t0 + 50 });
  m.setTab({ url: 'https://shop.example/produkt/seagate-exos-16tb', title: 'Seagate Exos 16TB' });
  const res = await m.runActStepWithDeadline(1, () => new Promise(() => {}), 100, t0,
    { kind: 'click', urlBefore: 'https://shop.example/produkt/wd-12tb' });
  assert.equal(res.navigated, true);
  assert.match(res.error, /page DID navigate/);
  assert.match(res.error, /seagate-exos-16tb/, 'names the destination, not just "it moved"');
  assert.match(res.error, /CHECK WHERE YOU LANDED/,
    'the worst observed case was a timed-out click landing on the WRONG product');
});

test('a timed-out step with network activity is distinguished from a dead one', async () => {
  const m = loadActTimeout();
  m.setNet({ requests: 3, pending: 1 });
  const busy = await m.runActStepWithDeadline(1, () => new Promise(() => {}), 100, Date.now(),
    { kind: 'click' });
  assert.match(busy.error, /3 network request\(s\) started/);
  assert.match(busy.error, /may still have taken effect/);
  assert.deepEqual(JSON.parse(JSON.stringify(busy.network)), { requests: 3, pending: 1 });

  const m2 = loadActTimeout();
  const dead = await m2.runActStepWithDeadline(1, () => new Promise(() => {}), 100, Date.now(),
    { kind: 'click' });
  assert.match(dead.error, /probably had no effect/);
  assert.match(dead.error, /retrying is reasonable/);
  assert.equal(dead.network, undefined, 'no digest is attached when there was no traffic');
});

test('console errors raised during the abandoned step ride along', async () => {
  const m = loadActTimeout();
  m.setConsole({ errors: 2, first: ['TypeError: x is not a function'] });
  const res = await m.runActStepWithDeadline(1, () => new Promise(() => {}), 100, Date.now(),
    { kind: 'click' });
  assert.equal(res.consoleErrors.errors, 2);
});

test('a tab closed under the step is reported as such, not as "no effect"', async () => {
  const m = loadActTimeout();
  m.killTab();
  const res = await m.runActStepWithDeadline(1, () => new Promise(() => {}), 100, Date.now(),
    { kind: 'click' });
  assert.equal(res.tabClosed, true);
  assert.match(res.error, /tab was closed/);
  assert.equal(res.urlAfter, undefined);
});

test('a step that REJECTS still propagates (the act loop owns that error)', async () => {
  const m = loadActTimeout();
  await assert.rejects(
    () => m.runActStepWithDeadline(1, async () => { throw new Error('injection blew up'); },
      5000, Date.now(), { kind: 'click' }),
    /injection blew up/);
});

test('a late rejection from an abandoned step does not become an unhandled rejection', async () => {
  const m = loadActTimeout();
  const seen = [];
  const onUnhandled = (e) => seen.push(e);
  process.on('unhandledRejection', onUnhandled);
  try {
    // The guard floors at 500ms, so the rejection must land after that to be "late".
    const res = await m.runActStepWithDeadline(
      1,
      () => new Promise((_res, rej) => setTimeout(() => rej(new Error('late boom')), 700)),
      100, Date.now(), { kind: 'click' });
    assert.equal(res.actionTimedOut, true);
    await new Promise(r => setTimeout(r, 500));
    assert.deepEqual(seen, [], 'the orphaned promise is caught, not left to crash the SW');
  } finally {
    process.off('unhandledRejection', onUnhandled);
  }
});

// ---------------------------------------------------------------------------
// runActSteps — the arming GLUE, not the helpers it calls.
//
// This is the layer that has gone silently dead in this codebase before: the
// StreamFilter body capture stayed broken through 343 green JS tests because the
// code that ARMED it had no unit coverage, only the pieces it called. The loop that
// threads the budget, records notRunSteps and captures urlBefore is the same shape,
// which is why it was lifted out of the switch into a sliceable function.
// ---------------------------------------------------------------------------

function loadActLoop() {
  const start = BG_SRC.indexOf('// ---- content.act step deadline');
  const end = BG_SRC.indexOf('// Content handlers', start);
  assert.ok(start >= 0 && end > start, 'act step block not found in background.js');
  const block = BG_SRC.slice(start, end);

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const TAB_NAV = new Map();
    let tabState = { url: 'https://shop.example/listing', title: 'Listing', status: 'complete' };
    const browser = { tabs: { get: async () => tabState } };
    function netDigestSince() { return { requests: 0, pending: 0 }; }
    function consoleErrorsSince() { return { errors: 0, first: [] }; }
    // The console flush asks the page; this harness has no page (console-flush.test.mjs).
    const CONSOLE_FLUSH_TIMEOUT_MS = 500;
    async function flushConsoleCapture() { return 'flushed'; }

    const calls = [];
    let automation = async () => ({ ok: true, clicked: true });
    async function runAutomation(tabId, method, args, frameId) {
      calls.push({ kind: 'automation', tabId, method, args, frameId });
      return await automation(method, args, calls.length - 1);
    }
    let waitResult = async () => ({ ok: true, matched: 'selector', elapsedMs: 5 });
    async function actWait(tabId, step, frameId) {
      calls.push({ kind: 'wait', tabId, step, frameId });
      return await waitResult(step, calls.length - 1);
    }
    let settleResult = { settled: true, url: 'https://shop.example/listing', navCommitted: false };
    async function waitForSettle() { return settleResult; }

    ${block}

    return {
      runActSteps, ACT_MIN_STEP_MS, ACT_STEP_TIMEOUT_MS, calls,
      setAutomation: (fn) => { automation = fn; },
      setWait: (fn) => { waitResult = fn; },
      setSettle: (v) => { settleResult = v; },
      setTab: (t) => { tabState = Object.assign({}, tabState, t); }
    };
  `);
  return factory();
}

test('runActSteps runs each step in order and reports the tally', async () => {
  const m = loadActLoop();
  const out = await m.runActSteps(1, { steps: [
    { action: 'fill', target: '#a', value: 'x' },
    { action: 'click', target: 'Save' }
  ] }, undefined);
  assert.equal(out.ok, true);
  assert.equal(out.completedSteps, 2);
  assert.equal(out.totalSteps, 2);
  // The delta work added an `inspect` measurement around every step; filter it out so this
  // assertion stays about the step->method MAPPING, which is what it tests.
  assert.deepEqual(m.calls.filter(c => c.kind === 'automation' && c.method !== 'inspect')
    .map(c => c.method), ['fill', 'click']);
  assert.deepEqual(out.results.map(r => r.step), [0, 1]);
  assert.deepEqual(out.results.map(r => r.stepAction), ['fill', 'click']);
});

test('every step kind maps to its automation method (check/type are aliases)', async () => {
  const m = loadActLoop();
  await m.runActSteps(1, { steps: [
    { action: 'type', target: '#a', value: 'x' },
    { action: 'check', target: '#b', value: true },
    { action: 'select', target: '#c', value: 'v' },
    { action: 'press', value: 'Enter' },
    { action: 'hover', target: '#d' },
    { action: 'scroll', to: 'bottom' }
  ] }, undefined);
  assert.deepEqual(m.calls.filter(c => c.kind === 'automation' && c.method !== 'inspect')
    .map(c => c.method), ['fill', 'fill', 'select', 'press', 'hover', 'scroll']);
});

test('an unknown step action is named, and it stops the batch', async () => {
  const m = loadActLoop();
  const out = await m.runActSteps(1, { steps: [
    { action: 'teleport', target: '#a' },
    { action: 'click', target: 'Save' }
  ] }, undefined);
  assert.equal(out.ok, false);
  assert.match(out.results[0].error, /unknown step action: teleport/);
  assert.equal(out.results.length, 1, 'the rest of the sequence is abandoned');
  assert.equal(m.calls.filter(c => c.kind === 'automation' && c.method !== 'inspect').length, 0);
});

test('a failing step stops the batch unless it opts into continue_on_error', async () => {
  const m = loadActLoop();
  m.setAutomation(async (method, args, i) => (i === 0 ? { ok: false, error: 'nope' } : { ok: true }));
  const stop = await m.runActSteps(1, { steps: [
    { action: 'click', target: 'a' }, { action: 'click', target: 'b' }
  ] }, undefined);
  assert.equal(stop.results.length, 1);
  assert.equal(stop.ok, false);

  const m2 = loadActLoop();
  m2.setAutomation(async (method, args, i) => (i === 0 ? { ok: false, error: 'nope' } : { ok: true }));
  const go = await m2.runActSteps(1, { steps: [
    { action: 'click', target: 'a', continue_on_error: true }, { action: 'click', target: 'b' }
  ] }, undefined);
  assert.equal(go.results.length, 2);
  assert.equal(go.ok, false, 'the batch still reports failure overall');
  assert.equal(go.completedSteps, 1);
});

test('the server budget is threaded into per-step deadlines', async () => {
  const m = loadActLoop();
  let sawBudget = null;
  m.setAutomation(async () => {
    // A step that never settles: the deadline is what ends it, so its report's
    // requestedTimeoutMs IS the budget the loop computed.
    return await new Promise(() => {});
  });
  const out = await m.runActSteps(1, {
    budgetMs: 3000,
    steps: [{ action: 'click', target: 'Go' }]
  }, undefined);
  sawBudget = out.results[0].requestedTimeoutMs;
  assert.equal(out.results[0].actionTimedOut, true);
  assert.ok(sawBudget <= 3000 && sawBudget > 1000,
    `the step budget must come from the batch budget, got ${sawBudget}`);
});

test('with no budget from the server, per-step defaults still bound the batch', async () => {
  const m = loadActLoop();
  const out = await m.runActSteps(1, { steps: [{ action: 'click', target: 'Go' }] }, undefined);
  assert.equal(out.ok, true, 'an older server that sends no budgetMs still works');
});

test('steps that never ran are NAMED rather than silently dropped', async () => {
  // Step 0 SUCCEEDS but eats the budget. (A step that FAILS stops the batch by the
  // ordinary rule below — a different mechanism, and conflating them hides this one.)
  const m = loadActLoop();
  m.setAutomation(() => new Promise(res => setTimeout(() => res({ ok: true }), 1500)));
  const out = await m.runActSteps(1, {
    budgetMs: 2500,
    steps: [
      { action: 'fill', target: 'a', value: '1' },
      { action: 'click', target: 'b' },
      { action: 'fill', target: 'c', value: 'd' }
    ]
  }, undefined);
  assert.equal(out.results.length, 1, 'step 0 ran and succeeded');
  assert.equal(out.results[0].ok, true);
  assert.equal(out.batchTimedOut, true);
  assert.equal(out.ok, false, 'an incomplete batch is not a success');
  assert.deepEqual(out.notRunSteps.map(s => s.step), [1, 2]);
  assert.deepEqual(out.notRunSteps.map(s => s.stepAction), ['click', 'fill']);
  assert.equal(out.totalSteps, 3, 'the caller can still see how many it asked for');
});

test('a step that TIMES OUT stops the batch like any other failure', async () => {
  const m = loadActLoop();
  m.setAutomation(async () => await new Promise(() => {}));
  const out = await m.runActSteps(1, {
    budgetMs: 4000,
    steps: [{ action: 'click', target: 'a' }, { action: 'click', target: 'b' }]
  }, undefined);
  assert.equal(out.results.length, 1, 'the timed-out step ends the sequence');
  assert.equal(out.results[0].actionTimedOut, true);
  assert.equal(out.ok, false);
  assert.equal(out.batchTimedOut, undefined,
    'that is a step failure, not budget exhaustion — do not conflate the two');
});

test('a wait step is clamped to the remaining batch budget, and says so', async () => {
  const m = loadActLoop();
  let seen = null;
  m.setWait(async (step) => {
    seen = step;
    return { ok: false, timedOut: true, elapsedMs: 10 };
  });
  const out = await m.runActSteps(1, {
    budgetMs: 4000,
    steps: [{ action: 'wait', target: '#x', timeout_ms: 60000 }]
  }, undefined);
  assert.ok(seen.timeout_ms <= 4000, `wait was not clamped: ${seen.timeout_ms}`);
  assert.equal(out.results[0].timeoutClampedToMs, seen.timeout_ms,
    'the clamp is reported, so a short wait is not mistaken for a real timeout');
});

test('a roomy budget leaves a wait step untouched', async () => {
  const m = loadActLoop();
  let seen = null;
  m.setWait(async (step) => { seen = step; return { ok: true, matched: 'selector' }; });
  const out = await m.runActSteps(1, {
    budgetMs: 120000,
    steps: [{ action: 'wait', target: '#x', timeout_ms: 5000 }]
  }, undefined);
  assert.equal(seen.timeout_ms, 5000);
  assert.equal(out.results[0].timeoutClampedToMs, undefined);
});

test('a click gets a settle window whose outcome rides on the result', async () => {
  const m = loadActLoop();
  m.setSettle({ settled: true, url: 'https://shop.example/listing?page=2', navCommitted: true });
  const out = await m.runActSteps(1, { steps: [{ action: 'click', target: 'Next' }] }, undefined);
  assert.equal(out.results[0].settled, true);
  assert.equal(out.results[0].urlAfter, 'https://shop.example/listing?page=2');
  assert.equal(out.results[0].navigated, true);
});

test('watch targets are inspected after the step, with the step scope pushed down', async () => {
  const m = loadActLoop();
  m.setAutomation(async (method) => (method === 'inspect'
    ? { ok: true, watched: [{ watch: '#err', found: true, visible: true }] }
    : { ok: true, clicked: true }));
  const out = await m.runActSteps(1, { steps: [
    { action: 'click', target: 'Save', within: '#panel', watch: ['#err'] }
  ] }, undefined);
  // The LAST inspect is the watch call: the delta measurement inspects run before the
  // step and again after the settle, and this block is deliberately below them.
  const inspects = m.calls.filter(c => c.method === 'inspect');
  const inspect = inspects[inspects.length - 1];
  assert.ok(inspect, 'inspect was never called');
  assert.equal(inspect.args.within, undefined,
    'the watch call scopes per ENTRY, never at the top level — that is a measurement call');
  assert.deepEqual(JSON.parse(JSON.stringify(inspect.args.watch)),
    [{ target: '#err', within: '#panel' }],
    'a scoped step must not report a same-named element from another panel');
  assert.equal(out.results[0].watched.length, 1);
});

test('frameId is forwarded to every step', async () => {
  const m = loadActLoop();
  await m.runActSteps(1, { steps: [
    { action: 'click', target: 'a' }, { action: 'wait', target: '#x' }
  ] }, 7);
  assert.equal(m.calls.find(c => c.kind === 'automation').frameId, 7);
  assert.equal(m.calls.find(c => c.kind === 'wait').frameId, 7);
});

test('an empty step list is a well-formed no-op, not a crash', async () => {
  const m = loadActLoop();
  const out = await m.runActSteps(1, { steps: [] }, undefined);
  assert.equal(out.ok, true);
  assert.equal(out.totalSteps, 0);
  assert.equal(out.completedSteps, 0);
  assert.equal(out.batchTimedOut, undefined);
  assert.equal(out.notRunSteps, undefined);
});
