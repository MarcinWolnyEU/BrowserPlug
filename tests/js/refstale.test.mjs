/**
 * Snapshot ref staleness (documentId) — gecko half.
 *
 * A ref like "e5" lives in the automation toolkit's per-document map, and the toolkit dies
 * with its document. So after a navigation the lookup fails exactly the way it fails for an
 * element that was removed from a page which never navigated — and the agent's correct next
 * move differs completely between those two. documentId is what separates them.
 *
 * What must hold, and what this pins:
 *   - a CHANGED document turns a failed ref lookup into REFS_STALE
 *   - an UNCHANGED document leaves the original error alone
 *   - an unknown docId on EITHER side is unknown, never "changed" (a false staleness claim
 *     sends an agent re-snapshotting a page that was fine)
 *   - a SUCCESSFUL call is never rewritten, whatever the document did
 *   - non-ref targets (CSS, accessible name) are untouched — they survive a navigation
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf8');

// Slice the staleness block out of background.js. Invariant E cross-checks these anchors
// in BOTH trees, so a rename fails the parity suite before this test can go stale.
function loadStaleness() {
  const start = BG_SRC.indexOf('// ---- snapshot ref staleness (documentId)');
  const end = BG_SRC.indexOf('// ---- end automation invoke path', start);
  assert.ok(start >= 0 && end > start,
    'ref-staleness block not found in background.js — the slice anchors moved, so this ' +
    'test is stale (fix the anchors), not the product.');
  // eslint-disable-next-line no-new-func
  return new Function(`
    ${BG_SRC.slice(start, end)}
    return { annotateStaleRef, targetsARef, snapshotDocKey, SNAPSHOT_DOCS, forgetSnapshotDocs,
             evalDocumentChanged, DOC_ID_EXPR };
  `)();
}

// The ARMING glue: the real runAutomation, with only its injection transport faked. The
// helpers above can all be correct while nothing ever records or consults a baseline, so
// this drives the function that does the wiring.
function loadArming(queue) {
  const start = BG_SRC.indexOf('// ---- snapshot ref staleness (documentId)');
  const end = BG_SRC.indexOf('// ---- end automation invoke path', start);
  assert.ok(start >= 0 && end > start, 'arming slice anchors moved');
  const calls = [];
  // eslint-disable-next-line no-new-func
  const mod = new Function('__queue', '__calls', `
    // Declared outside the sliced region in the product (it sits next to getApiSupport,
    // its reader). The slice ASSIGNS it, which is the behaviour under test.
    let CONTENT_DOC_ID_SUPPORT = null;
    const getAutomationSrc = async () => 'AUTOSRC';
    const executeScriptWithRetry = async (tabId, details) => {
      __calls.push({ tabId, frameId: details.frameId });
      return [__queue.shift()];
    };
    ${BG_SRC.slice(start, end)}
    return { runAutomation, SNAPSHOT_DOCS, getApiSupportContent: () => CONTENT_DOC_ID_SUPPORT };
  `)(queue, calls);
  mod.calls = calls;
  return mod;
}

// A toolkit envelope: nodesAtStart is what marks it as one (see noteContentDocIdSupport).
const envelope = (extra) => Object.assign({ nodesAtStart: 10, nodesAtEnd: 10 }, extra);

const OLD = 'doc-aaaa';
const NEW = 'doc-bbbb';
const fail = (extra) => Object.assign({ ok: false, error: 'element not found' }, extra || {});

test('a changed document turns a failed ref lookup into REFS_STALE', () => {
  const m = loadStaleness();
  const out = m.annotateStaleRef(fail({ docId: NEW }), { target: 'e5' }, OLD);

  assert.equal(out.code, 'REFS_STALE');
  assert.equal(out.staleRef, 'e5');
  assert.match(out.error, /e5/, 'the message names the ref that went stale');
  assert.match(out.error, /page_snapshot/, 'and says how to recover');
  // The literal phrase is the agent-facing contract for this failure, and only the
  // 36-minute integration sweep asserted it. Pinned here too, because this message
  // was rewritten once and silently dropped it.
  assert.match(out.error, /stale ref/, 'the "stale ref" wording was dropped');
  assert.equal(/element not found/.test(out.error), false,
    'the generic message is REPLACED — leaving it invites the wrong next move');
});

test('an unchanged document leaves the original error alone', () => {
  const m = loadStaleness();
  const out = m.annotateStaleRef(fail({ docId: OLD }), { target: 'e5' }, OLD);
  assert.equal(out.code, undefined);
  assert.equal(out.error, 'element not found');
});

test('an unknown docId is unknown, never "changed"', () => {
  const m = loadStaleness();
  // No docId in the response (browser too old, or the API was unreachable).
  assert.equal(m.annotateStaleRef(fail(), { target: 'e5' }, OLD).code, undefined);
  // No baseline recorded (no snapshot yet, or the SW dropped it).
  assert.equal(m.annotateStaleRef(fail({ docId: NEW }), { target: 'e5' }, undefined).code,
    undefined);
});

test('a successful call is never rewritten, whatever the document did', () => {
  const m = loadStaleness();
  const ok = { ok: true, docId: NEW };
  const out = m.annotateStaleRef(ok, { target: 'e5' }, OLD);
  assert.equal(out.code, undefined, 'a click that WORKED must not be reported as stale');
  assert.equal(out.ok, true);
});

test('only ref targets are diagnosed; selectors and names survive a navigation', () => {
  const m = loadStaleness();
  for (const target of ['#submit', 'Log in', 'button.primary', '', 'e5x', 'xe5']) {
    const out = m.annotateStaleRef(fail({ docId: NEW }), { target }, OLD);
    assert.equal(out.code, undefined, `${JSON.stringify(target)} is not a ref`);
  }
  assert.equal(m.targetsARef({ target: 'e5' }), true);
  assert.equal(m.targetsARef({ target: 'e12345' }), true);
  assert.equal(m.targetsARef({}), false);
  assert.equal(m.targetsARef(null), false);
});

test('the baseline key separates frames of the same tab, and 0 is the default frame', () => {
  const m = loadStaleness();
  assert.equal(m.snapshotDocKey(7, 0), m.snapshotDocKey(7, undefined));
  assert.equal(m.snapshotDocKey(7, 0), m.snapshotDocKey(7, null));
  assert.notEqual(m.snapshotDocKey(7, 0), m.snapshotDocKey(7, 3));
  assert.notEqual(m.snapshotDocKey(7, 0), m.snapshotDocKey(70, 0));
});

// ---- ARMING: runAutomation records the baseline and consults it ---------------------

test('a snapshot records the baseline, and a later ref failure is diagnosed against it', async () => {
  const m = loadArming([
    envelope({ ok: true, docId: OLD }),                       // snapshot, document A
    envelope({ ok: false, error: 'element not found', docId: NEW }),  // click, document B
  ]);

  const snap = await m.runAutomation(7, 'snapshot', {}, undefined);
  assert.equal(snap.ok, true);
  assert.equal(m.SNAPSHOT_DOCS.get(m2key(m, 7)), OLD, 'the snapshot did not arm the baseline');

  const click = await m.runAutomation(7, 'click', { target: 'e5' }, undefined);
  assert.equal(click.code, 'REFS_STALE',
    'the baseline was recorded but never consulted — the arming glue is dead');
  assert.match(click.error, /e5/);
});

test('without a preceding snapshot there is no baseline, so nothing is claimed', async () => {
  const m = loadArming([envelope({ ok: false, error: 'element not found', docId: NEW })]);
  const click = await m.runAutomation(7, 'click', { target: 'e5' }, undefined);
  assert.equal(click.code, undefined, 'a missing baseline must not produce a staleness claim');
  assert.equal(click.error, 'element not found');
});

test('a failure does not re-base, so every stale ref in a batch gets the same answer', async () => {
  const m = loadArming([
    envelope({ ok: true, docId: OLD }),
    envelope({ ok: false, error: 'element not found', docId: NEW }),
    envelope({ ok: false, error: 'element not found', docId: NEW }),
  ]);
  await m.runAutomation(7, 'snapshot', {}, undefined);
  const first = await m.runAutomation(7, 'click', { target: 'e5' }, undefined);
  const second = await m.runAutomation(7, 'click', { target: 'e6' }, undefined);
  assert.equal(first.code, 'REFS_STALE');
  assert.equal(second.code, 'REFS_STALE', 'the second stale ref lost its diagnosis');
  assert.equal(m.SNAPSHOT_DOCS.get(m2key(m, 7)), OLD, 'a failure must not re-base the baseline');
});

test('a fresh snapshot re-bases, so refs from it are not called stale', async () => {
  const m = loadArming([
    envelope({ ok: true, docId: OLD }),
    envelope({ ok: true, docId: NEW }),                                // re-snapshot after nav
    envelope({ ok: false, error: 'element not found', docId: NEW }),   // genuinely absent
  ]);
  await m.runAutomation(7, 'snapshot', {}, undefined);
  await m.runAutomation(7, 'snapshot', {}, undefined);
  const click = await m.runAutomation(7, 'click', { target: 'e5' }, undefined);
  assert.equal(click.code, undefined,
    'an element missing from the CURRENT document is not a stale ref');
  assert.equal(m.SNAPSHOT_DOCS.get(m2key(m, 7)), NEW);
});

test('baselines are per frame, and the frameId is threaded to the injection', async () => {
  const m = loadArming([
    envelope({ ok: true, docId: OLD }),                                // frame 0
    envelope({ ok: false, error: 'element not found', docId: NEW }),   // frame 3, no baseline
  ]);
  await m.runAutomation(7, 'snapshot', {}, 0);
  const click = await m.runAutomation(7, 'click', { target: 'e5' }, 3);
  assert.equal(click.code, undefined, "frame 0's baseline must not diagnose frame 3");
  assert.deepEqual(m.calls.map(c => c.frameId), [0, 3], 'frameId was not passed through');
});

test('the content-side documentId capability is observed from real envelopes only', async () => {
  const m = loadArming([
    { ok: false, error: 'no result from injected automation' },   // not a toolkit envelope
    envelope({ ok: true, docId: OLD }),
    envelope({ ok: true }),                                       // toolkit ran, no docId
  ]);
  await m.runAutomation(7, 'click', { target: '#x' }, undefined);
  assert.equal(m.getApiSupportContent(), null,
    'a transport failure says nothing about API availability and must not latch false');
  await m.runAutomation(7, 'snapshot', {}, undefined);
  assert.equal(m.getApiSupportContent(), true);
  await m.runAutomation(7, 'snapshot', {}, undefined);
  assert.equal(m.getApiSupportContent(), false, 'an envelope without a docId means absent');
});

// Key helper: the product owns the key shape, so ask it rather than restating it here.
function m2key(m, tabId) {
  return `${tabId}:0`;
}

// ---- page_eval: the document swapped WHILE the script ran ---------------------------

test('a document replaced mid-eval is reported; an unknown id never is', () => {
  const m = loadStaleness();
  assert.equal(m.evalDocumentChanged(OLD, NEW), true, 'a real swap must be reported');
  assert.equal(m.evalDocumentChanged(OLD, OLD), false);
  // Unknown on either side (browser too old, API unreachable, frame already unloaded).
  for (const [a, b] of [[null, NEW], [OLD, null], [null, null], [undefined, undefined]]) {
    assert.equal(m.evalDocumentChanged(a, b), false,
      `unknown (${a} -> ${b}) must not be reported as a change`);
  }
});

test('the injected documentId expression answers null instead of throwing', () => {
  const m = loadStaleness();
  // No extension APIs at all — the shape a page_eval runs in when the browser is old.
  const noApis = new Function(`return ${m.DOC_ID_EXPR};`);
  assert.equal(noApis(), null, 'must not throw into the user script');

  // Present but throwing (the documented behaviour for an unloaded frame).
  const throwing = new Function('browser', `return ${m.DOC_ID_EXPR};`);
  assert.equal(throwing({ runtime: { getDocumentId() { throw new Error('unloaded'); } } }), null);

  // Present and working.
  const working = new Function('browser', 'window', `return ${m.DOC_ID_EXPR};`);
  assert.equal(working({ runtime: { getDocumentId: () => 'doc-xyz' } }, {}), 'doc-xyz');
});

test('closing a tab releases every frame baseline it held, and only its own', () => {
  const m = loadStaleness();
  m.SNAPSHOT_DOCS.set(m.snapshotDocKey(1, 0), OLD);
  m.SNAPSHOT_DOCS.set(m.snapshotDocKey(1, 4), OLD);
  m.SNAPSHOT_DOCS.set(m.snapshotDocKey(2, 0), OLD);
  // The prefix match must not treat tab 1 as a prefix of tab 12.
  m.SNAPSHOT_DOCS.set(m.snapshotDocKey(12, 0), OLD);

  m.forgetSnapshotDocs(1);

  assert.equal(m.SNAPSHOT_DOCS.has(m.snapshotDocKey(1, 0)), false);
  assert.equal(m.SNAPSHOT_DOCS.has(m.snapshotDocKey(1, 4)), false);
  assert.equal(m.SNAPSHOT_DOCS.has(m.snapshotDocKey(2, 0)), true);
  assert.equal(m.SNAPSHOT_DOCS.has(m.snapshotDocKey(12, 0)), true,
    'tab 12 was dropped by a naive "starts with 1" prefix match');
});

// ---- wait predicates: compiled beside invoke(), never eval'd by the toolkit ---------------
// automation.js is a shared content file that AMO lints, and an eval( call in it is
// DANGEROUS_EVAL on every submission. runAutomation puts the evaluation into the
// executeScript code string instead, beside invoke(), where page_eval's wrapper keeps its
// own; these drive that glue with only the transport faked, and run the produced line the
// way the sandbox would.
function loadCompile(transport) {
  const start = BG_SRC.indexOf('// ---- snapshot ref staleness (documentId)');
  const end = BG_SRC.indexOf('// ---- end automation invoke path', start);
  assert.ok(start >= 0 && end > start, 'arming slice anchors moved');
  const calls = [];
  // eslint-disable-next-line no-new-func
  const mod = new Function('__transport', '__calls', `
    let CONTENT_DOC_ID_SUPPORT = null;
    const getAutomationSrc = async () => 'AUTOSRC';
    const executeScriptWithRetry = async (tabId, details) => {
      __calls.push(details);
      return __transport(details);
    };
    ${BG_SRC.slice(start, end)}
    return { runAutomation, compiledArgs };
  `)(transport, calls);
  mod.calls = calls;
  return mod;
}

// Stand-in for the injected toolkit: hands back what invoke() received.
function runInvokeLine(code) {
  assert.ok(code.startsWith('AUTOSRC\n;'), 'the toolkit source goes first');
  // eslint-disable-next-line no-new-func
  return new Function('window', 'return ' + code.slice('AUTOSRC\n;'.length))(
    { __browserplugAuto: { invoke: (method, args) => ({ method, args }) } });
}

test('a wait predicate is compiled beside invoke(); the toolkit gets a function', async () => {
  const m = loadCompile(async () => [{ nodesAtStart: 1, nodesAtEnd: 1, matched: null }]);
  const predicate = '1 + 1 === 2 // a trailing comment must not swallow the wrapper';
  await m.runAutomation(1, 'wait_check', { predicate, gone: false }, 3);
  const code = m.calls[0].code;
  assert.equal(m.calls[0].frameId, 3);
  const seen = runInvokeLine(code);
  assert.equal(seen.method, 'wait_check');
  assert.equal(seen.args.predicate, predicate, 'the string still travels, for the report');
  assert.equal(seen.args.gone, false);
  assert.equal(typeof seen.args.predicateFn, 'function');
  assert.equal(seen.args.predicateFn(), true);
});

test('a predicate that does not parse throws a SyntaxError from predicateFn, not from the injection', async () => {
  // Firefox strips the error name off an injection that fails to parse, so the parse
  // failure has to surface INSIDE the call, where the toolkit classifies it by name.
  const m = loadCompile(async () => [{ nodesAtStart: 1, nodesAtEnd: 1, matched: null }]);
  await m.runAutomation(1, 'wait_check', { predicate: '1 +' });
  const seen = runInvokeLine(m.calls[0].code);
  assert.throws(() => seen.args.predicateFn(), (e) => e.name === 'SyntaxError');
  const thrown = loadCompile(async () => [{ nodesAtStart: 1, nodesAtEnd: 1, matched: null }]);
  await thrown.runAutomation(1, 'wait_check', { predicate: 'nope.not.defined' });
  assert.throws(() => runInvokeLine(thrown.calls[0].code).args.predicateFn(),
    (e) => e.name === 'ReferenceError', 'a run-time throw keeps its own name');
});

test('a call without a predicate injects plain JSON args', async () => {
  const m = loadCompile(async () => [{ nodesAtStart: 1, nodesAtEnd: 1, ok: true }]);
  await m.runAutomation(1, 'click', { target: '#x' });
  const seen = runInvokeLine(m.calls[0].code);
  assert.equal(seen.method, 'click');
  assert.deepEqual(seen.args, { target: '#x' });
  assert.equal(m.compiledArgs(undefined), '{}');
});
