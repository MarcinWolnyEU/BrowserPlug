/*
 * page_screenshot on Firefox: when both capture paths fail, the error names both.
 *
 * Eval of 2026-10-08: three captures of a background Quill tab in a row came back as
 * "Failed to capture screenshot: An unexpected error occurred". captureTab had failed
 * first and the captureVisibleTab fallback failed after it, but only the fallback's bare
 * text was thrown, so the agent had no cause to act on and activated the user's tab. The
 * server adds the cause it can find (an unlaid-out page, server/tools/page.py
 * capture_failure_cause); this pins that the extension no longer drops the first failure,
 * and that the fallback still puts the user's own tab back.
 *
 * Gecko only: Chromium has no captureTab, so its capture has one path and one error.
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

function loadCapture({ captureTabError, visibleError, visibleResult }) {
  const block = slice('// --- Screenshot capture', 'function loadImageEl(');
  // eslint-disable-next-line no-new-func
  return new Function('opts', `
    const updates = [];
    const browser = { tabs: {
      captureTab: async () => { if (opts.captureTabError) throw new Error(opts.captureTabError); return 'data:image/png;base64,AAAA'; },
      captureVisibleTab: async () => {
        if (opts.visibleError) { const e = new Error(opts.visibleError); e.code = 'CAPTURE_X'; throw e; }
        return opts.visibleResult;
      },
      get: async (id) => ({ id, windowId: 1, active: false }),
      query: async () => [{ id: 9 }],
      update: async (id, props) => { updates.push([id, props.active]); },
    } };
    async function settleForCapture() { return { settled: true }; }
    function raceCapture(p) { return p; }
    async function captureElement() { throw new Error('not under test'); }
    async function captureRegion() { throw new Error('not under test'); }
    ${block}
    return { captureScreenshot, bothCapturesFailed, updates };
  `)({ captureTabError, visibleError, visibleResult });
}

test('both paths failing: the error carries captureTab\'s failure, not only the fallback\'s', async () => {
  const m = loadCapture({ captureTabError: 'An unexpected error occurred',
                          visibleError: 'An unexpected error occurred' });
  await assert.rejects(() => m.captureScreenshot({ tabId: 5, format: 'png' }), (err) => {
    assert.match(err.message, /^captureTab failed \(An unexpected error occurred\), and the captureVisibleTab fallback failed too: An unexpected error occurred$/);
    assert.equal(err.code, 'CAPTURE_X', 'the fallback error code survives');
    return true;
  });
  assert.deepEqual(m.updates, [[5, true], [9, true]], 'the user\'s own tab is put back');
});

test('a fallback that works still reports the captureTab failure as a note', async () => {
  const m = loadCapture({ captureTabError: 'boom', visibleResult: 'data:image/png;base64,BBBB' });
  const shot = await m.captureScreenshot({ tabId: 5, format: 'png' });
  assert.equal(shot.method, 'captureVisibleTab');
  assert.equal(shot.captureTabError, 'boom');
});

test('with no captureTab failure the fallback error passes through unchanged', () => {
  const m = loadCapture({});
  const e = new Error('only this');
  assert.equal(m.bothCapturesFailed(null, e), e);
});
