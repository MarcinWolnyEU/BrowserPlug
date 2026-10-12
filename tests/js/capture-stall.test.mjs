/*
 * page_screenshot on Firefox: a stalled await ends the request on the extension's own
 * deadline, with the step named and the user's tab put back.
 *
 * Evals of 2026-10-08: the first full-page capture of a background tab got no reply for
 * 60 s (the client's timeout and the server's fired first) and the error only arrived
 * when the harness closed the tab. The tab was left in front: the activating fallback
 * had run and never reached its restore. Nothing but the 4 s captureVisibleTab race was
 * bounded. These tests pin the contract of the bounds added in background.js:
 *   - a captureTab that never answers does NOT fall through to the activating fallback;
 *   - a fallback that stalls after activating is undone by the deadline, and the error
 *     says the previous tab was put back;
 *   - a page that never answers the measurement gives a viewport shot (full_page) or an
 *     error (element), never a hang;
 *   - the settle script is bounded by the background page's timer, not the page's;
 *   - a stall error carries the captureTab error that sent the capture to the fallback;
 *   - the fallback's own 4 s captureVisibleTab race and its restore of the user's tab are
 *     bounded, so a tab switch that never answers cannot turn the fallback's error into a
 *     40 s stall that names the wrong step (the 2026-10-09 eval's "captureVisibleTab").
 *
 * The step and deadline constants are shrunk by rewriting their declarations, so the
 * test also fails loudly if the declarations are renamed.
 * Gecko only: Chromium activates the tab first and has no captureTab.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

function slice(startAnchor, endAnchor) {
  const start = BG_SRC.indexOf(startAnchor);
  const end = BG_SRC.indexOf(endAnchor, start);
  assert.ok(start >= 0 && end > start, `block ${startAnchor} not found in background.js`);
  return BG_SRC.slice(start, end);
}

function shrink(block) {
  let out = block;
  for (const [name, ms] of [['CAPTURE_DEADLINE_MS', 400], ['CAPTURE_STEP_MS', 80],
                            ['CAPTURE_GRAB_MS', 80], ['CAPTURE_RESTORE_MS', 60],
                            ['CAPTURE_SETTLE_MARGIN_MS', 30]]) {
    const re = new RegExp(`const ${name} = \\d+;`);
    assert.match(out, re, `${name} declaration not found`);
    out = out.replace(re, `const ${name} = ${ms};`);
  }
  return out;
}

const NEVER = () => new Promise(() => {});

// captureScreenshot + the run helpers + captureWithDeadline, with the other capture paths stubbed.
function loadCapture(opts) {
  const block = shrink(slice('// --- Screenshot capture', 'function loadImageEl('));
  // eslint-disable-next-line no-new-func
  return new Function('opts', 'NEVER', `
    const updates = [];
    const browser = { tabs: {
      captureTab: async () => opts.captureTab(),
      captureVisibleTab: async () => opts.captureVisibleTab(),
      get: async (id) => ({ id, windowId: 1, active: false }),
      query: async () => [{ id: 9 }],
      update: async (id, props) => { updates.push([id, props.active]); },
    } };
    async function settleForCapture() { return { settled: true }; }
    function raceCapture(p) { return p; }
    async function captureElement() { throw new Error('not under test'); }
    async function captureRegion() { throw new Error('not under test'); }
    ${block}
    return { captureWithDeadline, updates };
  `)(opts, NEVER);
}

test('a captureTab that never answers stalls out and never activates the tab', async () => {
  const m = loadCapture({ captureTab: NEVER, captureVisibleTab: NEVER });
  await assert.rejects(() => m.captureWithDeadline({ tabId: 5, format: 'png' }), (err) => {
    assert.equal(err.code, 'CAPTURE_STALLED');
    assert.match(err.message, /^the screenshot stalled: captureTab did not answer within \d+ s \(step "captureTab"\)\. The tab was not brought to the front\./);
    return true;
  });
  assert.deepEqual(m.updates, [], 'the user\'s window was not touched');
});

test('a fallback that stalls after activating is undone by the deadline, and says so', async () => {
  const m = loadCapture({
    captureTab: async () => { throw new Error('An unexpected error occurred'); },
    captureVisibleTab: NEVER,
  });
  await assert.rejects(() => m.captureWithDeadline({ tabId: 5, format: 'png' }), (err) => {
    assert.equal(err.code, 'CAPTURE_STALLED');
    assert.match(err.message, /the capture did not answer/);
    assert.match(err.message, /step "captureVisibleTab"/);
    assert.match(err.message, /brought the tab to the front; the previous tab was put back/);
    return true;
  });
  assert.deepEqual(m.updates, [[5, true], [9, true]], 'tab 5 raised, tab 9 restored exactly once');
});

test('a stall after a captureTab failure says what the captureTab error was', async () => {
  const m = loadCapture({
    captureTab: async () => { throw new Error('image readback failed'); },
    captureVisibleTab: NEVER,
  });
  await assert.rejects(() => m.captureWithDeadline({ tabId: 5, format: 'png' }), (err) => {
    assert.equal(err.code, 'CAPTURE_STALLED');
    assert.match(err.message, /\(step "captureVisibleTab", after captureTab failed: image readback failed\)\. /);
    return true;
  });
});

test('a stall with no captureTab failure names no cause', async () => {
  const m = loadCapture({ captureTab: NEVER });
  await assert.rejects(() => m.captureWithDeadline({ tabId: 5, format: 'png' }),
    (err) => err.code === 'CAPTURE_STALLED' && !/after captureTab failed/.test(err.message));
});

// The viewport fallback with the REAL raceCapture (the other tests stub it away) and a
// restore that can hang, as a tab switch does in a window that is not painted.
function loadFallback(opts) {
  const block = shrink(slice('// --- Screenshot capture', 'function loadImageEl('));
  const race = slice('const VISIBLE_CAPTURE_TIMEOUT_MS = 4000;', '// Native lazy images');
  // eslint-disable-next-line no-new-func
  return new Function('opts', 'NEVER', `
    const updates = [];
    const browser = { tabs: {
      captureTab: async () => { throw new Error('image readback failed'); },
      captureVisibleTab: () => NEVER(),
      get: async (id) => ({ id, windowId: 1, active: false }),
      query: async () => [{ id: 9 }],
      update: async (id, props) => { updates.push([id, props.active]); return id === 9 ? opts.restore() : undefined; },
    } };
    async function settleForCapture() { return { settled: true }; }
    async function captureElement() { throw new Error('not under test'); }
    async function captureRegion() { throw new Error('not under test'); }
    ${block}
    ${race.replace('= 4000;', '= 50;')}
    return { captureWithDeadline, updates };
  `)(opts, NEVER);
}

test('a captureVisibleTab that never answers fails on its own 4 s race, naming both errors', async () => {
  const m = loadFallback({ restore: async () => {} });
  const t0 = Date.now();
  await assert.rejects(() => m.captureWithDeadline({ tabId: 5, format: 'png' }), (err) => {
    assert.equal(err.code, 'WINDOW_NOT_PAINTED', 'the race error, not a stall at the deadline');
    assert.match(err.message, /captureTab failed \(image readback failed\)/);
    assert.match(err.message, /captureVisibleTab timed out/);
    return true;
  });
  assert.ok(Date.now() - t0 < 300, 'well inside the (shrunk) deadline');
  assert.deepEqual(m.updates, [[5, true], [9, true]]);
});

test('a restore that never answers does not hide the error of the fallback behind the deadline', async () => {
  const m = loadFallback({ restore: NEVER });
  const t0 = Date.now();
  await assert.rejects(() => m.captureWithDeadline({ tabId: 5, format: 'png' }), (err) => {
    assert.equal(err.code, 'WINDOW_NOT_PAINTED');
    assert.match(err.message, /captureVisibleTab timed out/);
    return true;
  });
  assert.ok(Date.now() - t0 < 300, 'the restore is bounded by CAPTURE_RESTORE_MS, not the deadline');
});

test('a deadline that hits while the restore hangs still names the stalled step and says so', async () => {
  // The deadline path of captureWithDeadline: restore the tab, then build the stall error.
  const block = shrink(slice('// --- Screenshot capture', 'function loadImageEl('));
  // eslint-disable-next-line no-new-func
  const { captureStalled, restoreCaptureRun, newCaptureRun } = new Function('NEVER', `
    const browser = { tabs: { update: () => NEVER() } };
    async function captureElement() {} async function captureRegion() {}
    async function settleForCapture() {}
    ${block}
    return { captureStalled, restoreCaptureRun, newCaptureRun };
  `)(NEVER);
  const r = newCaptureRun();
  r.stage = 'captureVisibleTab'; r.activated = true; r.restoreTabId = 9;
  await restoreCaptureRun(r);
  assert.equal(r.stage, 'captureVisibleTab', 'the restore leaves the stalled step alone');
  const err = captureStalled(r, 'the capture', 40000);
  assert.match(err.message, /step "captureVisibleTab"/);
  assert.match(err.message, /putting the previous tab back did not answer, so the tab may still be in front/);
});

test('a working capture is not delayed or altered by the deadline', async () => {
  const m = loadCapture({ captureTab: async () => 'data:image/png;base64,AAAA' });
  const shot = await m.captureWithDeadline({ tabId: 5, format: 'png' });
  assert.equal(shot.method, 'captureTab');
  assert.deepEqual(m.updates, []);
});

// captureRegion on its own, with the page-side helpers stubbed.
function loadRegion(opts) {
  const helpers = shrink(slice('// --- Screenshot capture', 'function loadImageEl('));
  const region = slice('// Region capture (an element', '// Element-only screenshot');
  // eslint-disable-next-line no-new-func
  return new Function('opts', 'NEVER', `
    const updates = [];
    const browser = { tabs: {
      get: async (id) => ({ id, windowId: 1, active: false }),
      executeScript: async () => opts.executeScript(),
      captureTab: async (id, o) => opts.captureTab(o),
      captureVisibleTab: NEVER,
      query: async () => [{ id: 9 }],
      update: async (id, props) => { updates.push([id, props.active]); },
    } };
    const document = { createElement: () => ({ getContext: () => ({}) }) };
    async function settleForCapture() { return { settled: true }; }
    async function forceEagerImagesInRegion() {}
    async function prescrollThroughRegion() {}
    function loadImageEl() { return Promise.resolve({ naturalWidth: 100, naturalHeight: 200 }); }
    function raceCapture(p) { return p; }
    async function captureElement() { throw new Error('not under test'); }
    async function captureScreenshot() { throw new Error('not under test'); }
    ${helpers}
    ${region}
    return { captureRegion, updates, newCaptureRun, restoreCaptureRun };
  `)(opts, NEVER);
}

test('a page that never answers the measurement: full_page degrades to the viewport', async () => {
  const m = loadRegion({ executeScript: NEVER, captureTab: async () => 'data:image/png;base64,AAAA' });
  const shot = await m.captureRegion(5, null, 'png', 90, undefined);
  assert.equal(shot.method, 'captureTab');
  assert.match(shot.pageWarning, /did not answer the in-page measurement/);
  assert.deepEqual(m.updates, []);
});

test('a page that never answers the measurement: an element capture stalls out', async () => {
  const m = loadRegion({ executeScript: NEVER, captureTab: async () => 'data:image/png;base64,AAAA' });
  await assert.rejects(() => m.captureRegion(5, { x: 0, y: 0, w: 10, h: 10 }, 'png', 90, undefined),
    (err) => err.code === 'CAPTURE_STALLED' && /step "measure the page"/.test(err.message));
});

test('a region captureTab that never answers does not fall through to the activating fallback', async () => {
  const m = loadRegion({
    executeScript: async () => [{ sh: 200, cw: 100, ch: 100, dpr: 1, sx: 0, sy: 0 }],
    captureTab: NEVER,
  });
  await assert.rejects(() => m.captureRegion(5, null, 'png', 90, 0), (err) => {
    assert.equal(err.code, 'CAPTURE_STALLED');
    assert.match(err.message, /step "captureTab \(region\)"/);
    assert.match(err.message, /The tab was not brought to the front\./);
    return true;
  });
  assert.deepEqual(m.updates, [], 'no activation, so nothing to restore');
});

test('a region captureTab that throws takes the fallback; the deadline then restores the tab', async () => {
  const m = loadRegion({
    executeScript: async () => [{ sh: 200, cw: 100, ch: 100, dpr: 1, sx: 0, sy: 0 }],
    captureTab: async () => { throw new Error('An unexpected error occurred'); },
  });
  const run = m.newCaptureRun();
  const p = m.captureRegion(5, null, 'png', 90, 0, run);
  p.catch(() => {});
  await new Promise((r) => setTimeout(r, 200));
  assert.deepEqual(m.updates, [[5, true]], 'activated; captureVisibleTab never answers');
  assert.equal(run.activated, true);
  // What captureWithDeadline does when its timer fires.
  run.aborted = true;
  await m.restoreCaptureRun(run);
  assert.deepEqual(m.updates, [[5, true], [9, true]], 'the previous tab is put back, once');
  await m.restoreCaptureRun(run);
  assert.equal(m.updates.length, 2, 'a second restore is a no-op');
});

test('settleForCapture is bounded by the background page\'s timer', async () => {
  const block = shrink(slice('// --- Screenshot capture', 'function loadImageEl(')) +
    slice('async function settleForCapture(', '// Decode a captured image');
  // eslint-disable-next-line no-new-func
  const { settleForCapture } = new Function('NEVER', `
    const browser = { tabs: { executeScript: async () => NEVER() } };
    function captureElement() {} function captureRegion() {}
    ${block}
    return { settleForCapture };
  `)(NEVER);
  const t0 = Date.now();
  const res = await settleForCapture(5, 20, null);
  assert.equal(res.settled, false);
  assert.equal(res.stalled, true);
  assert.equal(res.error, undefined, 'no error key, so the caller\'s "may not have settled" note still shows');
  assert.ok(Date.now() - t0 < 1000);
});
