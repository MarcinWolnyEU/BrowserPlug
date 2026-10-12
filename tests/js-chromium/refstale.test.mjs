/**
 * Snapshot ref staleness (documentId) — chromium half.
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
 *
 * MV3 note: the baseline map lives in service-worker memory, so an eviction loses it and
 * the next stale ref falls back to the generic "not found". That is the unknown case
 * below, and it is why unknown must never be reported as "changed".
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf8');

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
             evalDocumentChanged };
  `)();
}

// The ARMING glue: the real runAutomation, with only its injection transport faked. The
// helpers above can all be correct while nothing ever records or consults a baseline, so
// this drives the function that does the wiring.
function loadArming(queue, frames) {
  const start = BG_SRC.indexOf('// ---- snapshot ref staleness (documentId)');
  const end = BG_SRC.indexOf('// ---- end automation invoke path', start);
  assert.ok(start >= 0 && end > start, 'arming slice anchors moved');
  const calls = [];
  // eslint-disable-next-line no-new-func
  const mod = new Function('__queue', '__calls', '__frames', `
    // Blink has no content-script getDocumentId, so the id comes from webNavigation.
    // __frames is the queue of getAllFrames answers, one per call.
    const browser = { webNavigation: { getAllFrames: async () => __frames.shift() } };
    // Declared outside the sliced region in the product (it sits next to getApiSupport,
    // its reader). The slice ASSIGNS it, which is the behaviour under test.
    let CONTENT_DOC_ID_SUPPORT = null;
    // Chromium injects TWICE per runAutomation: files= to ensure the toolkit is present,
    // then func= to invoke it. Only the second carries a payload, and it arrives wrapped
    // in an InjectionResult ({result}), unlike gecko's bare value.
    const executeScriptWithRetry = async (opts) => {
      if (opts.files) return [{}];
      __calls.push({ frameId: opts.target.frameIds ? opts.target.frameIds[0] : undefined });
      return [{ result: __queue.shift() }];
    };
    ${BG_SRC.slice(start, end)}
    return { runAutomation, SNAPSHOT_DOCS, getApiSupportContent: () => CONTENT_DOC_ID_SUPPORT };
  `)(queue, calls, frames || []);
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

// ---- Blink: the document id comes from webNavigation, not the envelope --------------

test('with no docId in the envelope, the baseline comes from webNavigation', async () => {
  // The real Blink shape: automation.js cannot stamp a docId, so every envelope lacks one.
  const m = loadArming(
    [envelope({ ok: true }), envelope({ ok: false, error: 'element not found' })],
    [[{ frameId: 0, documentId: OLD }], [{ frameId: 0, documentId: NEW }]]);

  await m.runAutomation(7, 'snapshot', {}, undefined);
  assert.equal(m.SNAPSHOT_DOCS.get('7:0'), OLD,
    'the background fallback never armed the baseline — the Blink path is dead');

  const click = await m.runAutomation(7, 'click', { target: 'e5' }, undefined);
  assert.equal(click.code, 'REFS_STALE');
});

test('the frame id selects the right document, not just the first frame', async () => {
  const m = loadArming(
    [envelope({ ok: true }), envelope({ ok: false, error: 'element not found' })],
    [[{ frameId: 0, documentId: OLD }, { frameId: 3, documentId: 'doc-frame3' }],
     [{ frameId: 0, documentId: OLD }, { frameId: 3, documentId: NEW }]]);

  await m.runAutomation(7, 'snapshot', {}, 3);
  assert.equal(m.SNAPSHOT_DOCS.get('7:3'), 'doc-frame3', 'picked the wrong frame');
  const click = await m.runAutomation(7, 'click', { target: 'e5' }, 3);
  assert.equal(click.code, 'REFS_STALE');
});

test('an ordinary click never pays for a webNavigation round trip', async () => {
  // No frames queued at all: if the lazy rule is broken, getAllFrames returns undefined
  // and the .find() throws, so this fails loudly rather than silently costing a call.
  const m = loadArming([envelope({ ok: true })], []);
  const click = await m.runAutomation(7, 'click', { target: '#ok' }, undefined);
  assert.equal(click.ok, true);
});

test('webNavigation failing is unknown, not a staleness claim', async () => {
  const m = loadArming(
    [envelope({ ok: true }), envelope({ ok: false, error: 'element not found' })],
    [[{ frameId: 0, documentId: OLD }], []]);   // second lookup finds no matching frame
  await m.runAutomation(7, 'snapshot', {}, undefined);
  const click = await m.runAutomation(7, 'click', { target: 'e5' }, undefined);
  assert.equal(click.code, undefined, 'an unknown current id must not be called stale');
});

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

// ---- wait predicates: evaluated in the user-scripts world, replayed to the toolkit --------
// automation.js is shared with gecko and AMO lints it; an eval( call there is
// DANGEROUS_EVAL. The toolkit's ISOLATED world refuses eval under the extension CSP, so
// the predicate is evaluated through userScripts (page_eval's gate) and the invoking func
// hands the toolkit a predicateFn that replays the outcome. These fake the transport and
// run the func the way Chrome would: applied to its serialised args.
function loadCompile({ toggle = true, execute } = {}) {
  const start = BG_SRC.indexOf('// ---- snapshot ref staleness (documentId)');
  const end = BG_SRC.indexOf('// ---- end automation invoke path', start);
  assert.ok(start >= 0 && end > start, 'arming slice anchors moved');
  const calls = [];
  const userScripts = [];
  // Default transport: run the user-script code string here (it carries its own
  // try/catch and (0, eval), so a parse failure comes back as {syntaxError}).
  // eslint-disable-next-line no-eval
  const run = execute || (async ({ js }) => [{ result: (0, eval)(js[0].code) }]);
  // eslint-disable-next-line no-new-func
  const mod = new Function('__calls', '__userScripts', '__toggle', '__run', `
    const browser = {
      runtime: { id: 'ext-id' },
      webNavigation: { getAllFrames: async () => [] },
      userScripts: { execute: async (opts) => { __userScripts.push(opts); return __run(opts); } }
    };
    // Declared past the slice's end anchor in the product (the page_eval section).
    const userScriptsAvailable = () => __toggle;
    const ensureUserScriptWorld = async () => {};
    let CONTENT_DOC_ID_SUPPORT = null;
    const executeScriptWithRetry = async (opts) => {
      if (opts.files) return [{}];
      __calls.push(opts);
      // What the toolkit would answer: the invoke() arguments, handed back unchanged.
      globalThis.window = { __browserplugAuto: { invoke: (method, args) => ({ method, args }) } };
      try { return [{ result: opts.func(...opts.args) }]; }
      finally { delete globalThis.window; }
    };
    ${BG_SRC.slice(start, end)}
    return { runAutomation, evalWaitPredicate };
  `)(calls, userScripts, toggle, run);
  mod.calls = calls;
  mod.userScripts = userScripts;
  return mod;
}

test('a wait predicate is evaluated in the USER_SCRIPT world and replayed to the toolkit', async () => {
  const m = loadCompile();
  const predicate = '1 + 1 === 2';
  const out = await m.runAutomation(1, 'wait_check', { predicate, gone: false }, 3);
  assert.equal(m.userScripts.length, 1, 'one user-script evaluation per invoke');
  assert.equal(m.userScripts[0].world, 'USER_SCRIPT');
  assert.deepEqual(m.userScripts[0].target, { tabId: 1, frameIds: [3] });
  assert.equal(m.calls[0].world, 'ISOLATED');
  assert.deepEqual(m.calls[0].args[2], { truth: true }, 'the outcome rides as the third arg');
  assert.equal(out.method, 'wait_check');
  assert.equal(out.args.predicate, predicate, 'the string still travels, for the report');
  assert.equal(typeof out.args.predicateFn, 'function');
  assert.equal(out.args.predicateFn(), true);
});

test('a predicate that does not parse replays as a SyntaxError; a run-time throw as an Error', async () => {
  const m = loadCompile();
  const bad = await m.runAutomation(1, 'wait_check', { predicate: '1 +' });
  assert.throws(() => bad.args.predicateFn(), (e) => e.name === 'SyntaxError');
  const thrown = await m.runAutomation(1, 'wait_check', { predicate: 'nope.not.defined' });
  assert.throws(() => thrown.args.predicateFn(), (e) => e.name === 'Error' && /nope/.test(e.message));
});

test('with the "Allow user scripts" toggle off the predicate reports the toggle, and nothing is injected for it', async () => {
  const m = loadCompile({ toggle: false });
  const out = await m.runAutomation(1, 'wait_check', { predicate: '1 === 1' });
  assert.equal(m.userScripts.length, 0);
  assert.throws(() => out.args.predicateFn(), /Allow user scripts/);
  assert.throws(() => out.args.predicateFn(), /ext-id/, 'the message names this extension');
});

test('a user-script transport failure is an unknown, not a match and not a parse failure', async () => {
  const m = loadCompile({ execute: async () => { throw new Error('No tab with id: 9'); } });
  const out = await m.runAutomation(9, 'wait_check', { predicate: '1 === 1' });
  assert.throws(() => out.args.predicateFn(), (e) => e.name === 'Error' && /No tab with id/.test(e.message));
});

test('a call without a predicate hands the func null and attaches nothing', async () => {
  const m = loadCompile();
  const out = await m.runAutomation(1, 'click', { target: '#x' });
  assert.equal(m.userScripts.length, 0);
  assert.equal(m.calls[0].args[2], null);
  assert.equal(out.method, 'click');
  assert.deepEqual(out.args, { target: '#x' });
});
