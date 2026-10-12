/*
 * CHROMIUM TWIN of tests/js/actdelta.test.mjs.
 *
 * Background side — pairing the page's measurements into a DELTA, and timing
 * that pairing so it catches content that lands late.
 *
 * The failure being fixed: `OK: 1/1 steps completed` answers "did the mechanism run",
 * never "what changed". Two facts make that hard to fix in the page alone:
 *   1. a count taken inside self.click can only span the ~470ms its own observer is
 *      alive, and the euro.com.pl "load more" this case is built on lands well after;
 *   2. waitForSettle ends on NETWORK quiet, and a setTimeout-driven append fires no
 *      request at all — so the settle can end BEFORE the rows exist.
 * So the background takes a before number, runs the step, runs the settle, then polls
 * the page until the number stops moving, and subtracts. Exactly the split that
 * already exists for actWait (one-shot check in the page, history in the background).
 *
 * On this engine every runAutomation is TWO scripting.executeScript calls (inject the
 * toolkit, then invoke), so a measurement costs roughly double what it does on gecko —
 * which is exactly why the before-measure is skipped unless the step asked for it.
 *
 * Sliced out of background.js by the existing act-deadline anchors (no new anchor), and driven
 * with stubs — the gecko/chromium twin pair exists because this block is a declared
 * [[identical_blocks]] entry and each twin only ever reads its own tree's copy.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');

function block() {
  const start = BG_SRC.indexOf('// ---- content.act step deadline');
  const end = BG_SRC.indexOf('// Content handlers', start);
  assert.ok(start >= 0 && end > start, 'act step block not found in background.js');
  return BG_SRC.slice(start, end);
}

// The act loop with an `inspect`-aware automation stub. `nodes` is a function of the
// call index so a test can make the page keep growing (or settle) at will.
function loadActLoop() {
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
    let nodesSeq = () => 100;
    let countsSeq = () => undefined;
    let inspectFails = false;
    let automation = async () => ({ ok: true, clicked: true, nodesAtStart: 100, nodesAtEnd: 100 });
    let inspectCount = 0;
    async function runAutomation(tabId, method, args, frameId) {
      calls.push({ kind: 'automation', tabId, method, args, frameId });
      if (method === 'inspect') {
        if (inspectFails) throw new Error('injection blocked');
        const i = inspectCount++;
        return { ok: true, watched: [], nodesAtStart: nodesSeq(i), nodesAtEnd: nodesSeq(i),
                 counts: countsSeq(i, args) };
      }
      return await automation(method, args, calls.length - 1);
    }
    let waitResult = async () => ({ ok: true, matched: 'selector', elapsedMs: 5 });
    async function actWait(tabId, step, frameId) {
      calls.push({ kind: 'wait', tabId, step, frameId });
      return await waitResult(step, calls.length - 1);
    }
    let settleResult = { settled: true, url: 'https://shop.example/listing', navCommitted: false };
    async function waitForSettle() { return settleResult; }

    ${block()}

    return {
      runActSteps, actMeasure, actMeasureSettled, actZipCounts, actCountsSame, actEffect,
      actTimeoutReport, runActStepWithDeadline,
      ACT_MEASURE_MAX_MS, ACT_MEASURE_MIN_BUDGET_MS, calls,
      inspects: () => calls.filter(c => c.method === 'inspect'),
      setAutomation: (fn) => { automation = fn; },
      setNodes: (fn) => { nodesSeq = fn; },
      setCounts: (fn) => { countsSeq = fn; },
      failInspect: () => { inspectFails = true; },
      setSettle: (v) => { settleResult = v; }
    };
  `);
  return factory();
}

// ---- the round-trip budget: who pays for a measurement ----------------------

test('a plain step pays for the AFTER measure only — its own result carries the before', async () => {
  const m = loadActLoop();
  m.setNodes(() => 124);
  m.setAutomation(async () => ({ ok: true, clicked: true, nodesAtStart: 100, nodesAtEnd: 100 }));
  const out = await m.runActSteps(1, { steps: [{ action: 'click', target: 'Load more' }] }, undefined);

  const order = m.calls.map(c => c.method);
  assert.equal(order[0], 'click', 'no BEFORE inspect: nodesAtStart already came back with the click');
  assert.deepEqual(m.inspects().length, 2, 'the after-measure is a quiescence PAIR, nothing more');
  assert.equal(out.results[0].nodesBefore, 100);
  assert.equal(out.results[0].nodesAfter, 124);
  assert.equal(out.results[0].nodeDelta, 24);
  assert.equal(out.results[0].effect, 'changed');
});

test('a step naming `count` pays for a BEFORE measure, and it runs before the step', async () => {
  const m = loadActLoop();
  m.setNodes(() => 100);
  m.setCounts((i) => [{ selector: '.product-card', count: i === 0 ? 40 : 48 }]);
  const out = await m.runActSteps(1, {
    steps: [{ action: 'click', target: 'Load more', count: ['.product-card'] }]
  }, undefined);

  assert.equal(m.calls[0].method, 'inspect', 'the before-measure runs FIRST');
  assert.deepEqual(m.calls[0].args.count, ['.product-card']);
  assert.equal(m.calls[1].method, 'click');
  assert.deepEqual(JSON.parse(JSON.stringify(out.results[0].counts)),
    [{ selector: '.product-card', before: 40, after: 48, delta: 8 }]);
});

test('the step scope is pushed down into the measurement, not just into watch', async () => {
  const m = loadActLoop();
  await m.runActSteps(1, {
    steps: [{ action: 'click', target: 'Load more', count: ['.card'], within: '#grid' }]
  }, undefined);
  assert.equal(m.calls[0].args.within, '#grid',
    'a scoped step must not count another panel\'s cards');
});

test('the raw nodesAtStart/nodesAtEnd carriers never reach the wire', async () => {
  const m = loadActLoop();
  const out = await m.runActSteps(1, { steps: [{ action: 'click', target: 'x' }] }, undefined);
  assert.equal('nodesAtStart' in out.results[0], false, 'plumbing, not contract');
  assert.equal('nodesAtEnd' in out.results[0], false);
});

test('a successful WAIT step is not charged for a measurement it does not need', async () => {
  // actWait has already polled the page to a known state; two more injections would be
  // pure latency. A wait that NAMES a count still gets one, because it asked.
  const m = loadActLoop();
  const bare = await m.runActSteps(1, { steps: [{ action: 'wait', target: '#x' }] }, undefined);
  assert.equal(bare.results[0].ok, true);
  assert.equal(m.inspects().length, 0, 'no measurement was taken');
  assert.equal(bare.results[0].nodeDelta, undefined);

  const m2 = loadActLoop();
  m2.setCounts(() => [{ selector: '.p', count: 3 }]);
  await m2.runActSteps(1, { steps: [{ action: 'wait', target: '#x', count: ['.p'] }] }, undefined);
  assert.ok(m2.inspects().length > 0, 'a wait that asked for a count is measured');
});

test('a scoped `watch` is pushed down per entry on the BEFORE measure too', async () => {
  // inspect scopes counts by args.within but resolves each watch entry on its own args,
  // so before/after watch rows would otherwise describe two different panels.
  const m = loadActLoop();
  await m.runActSteps(1, {
    steps: [{ action: 'click', target: 'Save', within: '#panel', watch: ['#err'] }]
  }, undefined);
  const before = m.inspects()[0];
  assert.deepEqual(JSON.parse(JSON.stringify(before.args.watch)),
    [{ target: '#err', within: '#panel' }]);
  assert.equal(before.args.within, '#panel', 'and the COUNT scope still rides at the top level');
});

// ---- quiescence: the delta must span the settle, not end at it --------------

test('the poll keeps reading while the number is still moving, and says it never settled', async () => {
  // A page that grows on every read: the honest answer is a LOWER BOUND, flagged.
  const m = loadActLoop();
  let n = 100;
  m.setNodes(() => (n += 10));
  const out = await m.runActSteps(1, { steps: [{ action: 'click', target: 'x' }] }, undefined);
  assert.equal(out.results[0].measureSettled, false,
    'a carousel/ticker never quiesces — the rendering says LOWER BOUND rather than lying');
  assert.ok(m.inspects().length > 2, 'it really did keep polling');
});

test('the poll stops the moment two consecutive readings agree', async () => {
  const m = loadActLoop();
  const seq = [100, 118, 124, 124, 124];
  m.setNodes((i) => seq[Math.min(i, seq.length - 1)]);
  const out = await m.runActSteps(1, { steps: [{ action: 'click', target: 'x' }] }, undefined);
  assert.equal(out.results[0].measureSettled, true);
  assert.equal(out.results[0].nodesAfter, 124);
  assert.equal(m.inspects().length, 4, 'stopped at the first agreeing pair, not at the cap');
});

test('the poll is skipped entirely when the batch budget is nearly spent', async () => {
  // The measurement must never be what blows the budget — that is the regression this guards.
  const m = loadActLoop();
  let n = 100;
  m.setNodes(() => (n += 10));
  // 1500ms of batch budget left is under ACT_MEASURE_MIN_BUDGET_MS, so the poll is
  // skipped outright — one reading, reported honestly as unsettled.
  const out = await m.runActSteps(1, {
    budgetMs: 1500,
    steps: [{ action: 'click', target: 'x' }]
  }, undefined);
  assert.equal(m.inspects().length, 1, 'one reading, no poll: there was no budget for it');
  assert.equal(out.results[0].measureSettled, false);
});

// ---- the helpers, directly --------------------------------------------------

test('actZipCounts pairs by SELECTOR, never by index', async () => {
  const m = loadActLoop();
  const zipped = m.actZipCounts(
    [{ selector: '.a', count: 4 }, { selector: '.b', count: 9 }],
    [{ selector: '.b', count: 9 }, { selector: '.a', count: 12 }]);
  assert.deepEqual(JSON.parse(JSON.stringify(zipped)), [
    { selector: '.b', before: 9, after: 9, delta: 0 },
    { selector: '.a', before: 4, after: 12, delta: 8 },
  ], 'a reordered after-list must not attribute .a\'s growth to .b');
});

test('actZipCounts carries an error through instead of inventing a delta', async () => {
  const m = loadActLoop();
  const zipped = m.actZipCounts(
    [{ selector: '((', error: 'invalid selector: ((' }],
    [{ selector: '((', error: 'invalid selector: ((' }]);
  assert.equal(zipped[0].error, 'invalid selector: ((');
  assert.equal('delta' in zipped[0], false, 'NOT MEASURED must not read as "no change"');
  assert.equal(m.actZipCounts(null, undefined), undefined, 'nothing measured, nothing reported');
});

test('actCountsSame compares the numbers, and treats a length change as movement', async () => {
  const m = loadActLoop();
  assert.equal(m.actCountsSame(undefined, undefined), true);
  assert.equal(m.actCountsSame([{ selector: 'a', count: 1 }], [{ selector: 'a', count: 1 }]), true);
  assert.equal(m.actCountsSame([{ selector: 'a', count: 1 }], [{ selector: 'a', count: 2 }]), false);
  assert.equal(m.actCountsSame([{ selector: 'a', count: 1 }], []), false);
  assert.equal(m.actCountsSame(null, [{ selector: 'a', count: 1 }]), false);
});

test('actEffect names the four outcomes the agent has to tell apart', async () => {
  const m = loadActLoop();
  assert.equal(m.actEffect({ nodeDelta: 24 }), 'changed');
  assert.equal(m.actEffect({ nodeDelta: 0, nodesAdded: 40, nodesRemoved: 40 }), 'rerendered',
    'the case domChanged could never express: churn without content');
  assert.equal(m.actEffect({ nodeDelta: 0, nodesAdded: 0, nodesRemoved: 0 }), 'none');
  assert.equal(m.actEffect({ domChanged: false }), 'unmeasured',
    'no number at all is its own answer — never a confident "none"');
  assert.equal(m.actEffect({ nodeDelta: 0, counts: [{ selector: '.p', delta: 8 }] }), 'changed',
    'a named count outranks the node total: 8 cards may replace 8 skeletons');
  assert.equal(m.actEffect({ urlChanged: true }), 'changed');
});

test('actMeasure returns null when the page cannot be probed — it never throws', async () => {
  const m = loadActLoop();
  m.failInspect();
  assert.equal(await m.actMeasure(1, {}, undefined), null);
});

test('an unprobeable page leaves the step ok, flagged, and without a made-up delta', async () => {
  const m = loadActLoop();
  m.failInspect();
  const out = await m.runActSteps(1, { steps: [{ action: 'click', target: 'x' }] }, undefined);
  assert.equal(out.results[0].ok, true, 'a measurement must never fail the step it measures');
  assert.equal(out.results[0].measureFailed, true);
  assert.equal(out.results[0].nodeDelta, undefined);
});

// ---- a TIMED-OUT step still reports the delta (acceptance d) ----------------

test('a timed-out step measures through ctx and says the action DID take effect', async () => {
  const m = loadActLoop();
  const res = await m.runActStepWithDeadline(1, () => new Promise(() => {}), 100, Date.now(), {
    kind: 'click', urlBefore: 'https://shop.example/listing',
    nodesBefore: 100, countsBefore: [{ selector: '.p', count: 40 }],
    measure: async () => ({ nodes: 124, counts: [{ selector: '.p', count: 48 }] })
  });
  assert.equal(res.nodeDelta, 24);
  assert.equal(res.nodesBefore, 100);
  assert.equal(res.nodesAfter, 124);
  assert.deepEqual(JSON.parse(JSON.stringify(res.counts)),
    [{ selector: '.p', before: 40, after: 48, delta: 8 }]);
  assert.match(res.error, /DID take effect/);
  assert.match(res.error, /a retry would apply it twice/,
    'the harm of a blind retry on a load-more is a DUPLICATE, not a wasted call');
});

test('a timed-out step whose probe fails says so rather than claiming no effect', async () => {
  const m = loadActLoop();
  const res = await m.runActStepWithDeadline(1, () => new Promise(() => {}), 100, Date.now(), {
    kind: 'click', nodesBefore: 100,
    measure: async () => { throw new Error('tab gone'); }
  });
  assert.equal(res.nodeProbeFailed, true);
  assert.equal(res.nodeDelta, undefined);
  assert.match(res.error, /probably had no effect/, 'the pre-existing verdict is unchanged');
});

test('a timed-out step that measured ZERO says the DOM did not move either', async () => {
  const m = loadActLoop();
  const res = await m.runActStepWithDeadline(1, () => new Promise(() => {}), 100, Date.now(), {
    kind: 'click', nodesBefore: 100, measure: async () => ({ nodes: 100 })
  });
  assert.equal(res.nodeDelta, 0);
  assert.match(res.error, /probably had no effect/);
  assert.match(res.error, /did not grow or shrink either/);
});

test('a ctx WITHOUT a measure hook leaves the old report byte-for-byte intact', async () => {
  // This is why the probe is threaded through ctx rather than calling the page: the
  // acttimeout.test.mjs harness defines no runAutomation at all.
  const m = loadActLoop();
  const res = await m.runActStepWithDeadline(1, () => new Promise(() => {}), 100, Date.now(),
    { kind: 'click', urlBefore: 'https://shop.example/listing' });
  assert.equal(res.nodeDelta, undefined);
  assert.equal(res.nodeProbeFailed, undefined);
  assert.match(res.error, /probably had no effect/);
  assert.match(res.error, /retrying is reasonable/);
  assert.doesNotMatch(res.error, /did not grow or shrink/);
});

// ---- the frozen watch call --------------------------------------------------

test('the watch sub-call is untouched, and still runs BELOW the after-measure', () => {
  // Both actwait.test.mjs twins regex this literal; the whole placement of the
  // after-measure exists to keep it so.
  const b = block();
  assert.ok(/runAutomation\(actTabId, 'inspect', \{ watch: watch \}/.test(b));
  assert.ok(b.indexOf('actMeasureSettled(actTabId') < b.indexOf("if (step.watch && res.ok !== false)"),
    'the after-measure must stay strictly ABOVE the frozen watch block');
});
