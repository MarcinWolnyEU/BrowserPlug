/*
 * Unit tests for two positive paths that had no coverage (jsdom, no Firefox):
 *
 *   1. AnthropicView.isClaudeCode  — the gate for the popup's usage-bar feature.
 *      Its twin, KiloCode.isKiloCli, has been pinned since it was written; this one
 *      never was, even though popup.js branches on it in five places (the bars, the
 *      chart row, the expand toggle, the inline summary). A gate that silently
 *      returns false everywhere would hide the feature, not break it — nothing else
 *      in the suite would notice.
 *
 *   2. automation.js hover — covered only via its scope/ref REFUSALS
 *      (automation.test.mjs); the success path (events dispatched, envelope shape)
 *      was untested.
 *
 * Load idioms mirror the existing suites: kilocode-view.test.mjs / anthropic-view.test.mjs
 * for the vendor modules, automation.test.mjs for the jsdom + layout-stub harness.
 * Run with: node --test tests/js/perception-extras.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ===========================================================================
// 1. AnthropicView.isClaudeCode
// ===========================================================================

const VIEW_SRC = readFileSync(
  path.join(__dirname, '../../extension-gecko/vendor/anthropic-view.js'), 'utf8');

// Same shape as kilocode-view.test.mjs / anthropic-view.test.mjs.
function loadView() {
  const dom = new JSDOM('<!doctype html><body></body>');
  const g = dom.window;
  g.globalThis = g;
  // eslint-disable-next-line no-new-func
  new Function('window', 'document', 'globalThis', VIEW_SRC)(g, g.document, g);
  return g.AnthropicView;
}

test('isClaudeCode is published on AnthropicView (popup.js reads it off the module)', () => {
  const AV = loadView();
  // popup.js:191 — `AnthropicView.isClaudeCode || (() => false)`. If the export is
  // ever dropped, that fallback makes the whole usage feature vanish silently.
  assert.equal(typeof AV.isClaudeCode, 'function');
});

test('isClaudeCode matches a Claude Code mcp_client by friendly name', () => {
  const AV = loadView();
  assert.equal(AV.isClaudeCode({ kind: 'mcp_client', friendly: 'Claude Code' }), true);
});

test('isClaudeCode matches on a label PREFIX (rows carry "Claude Code — <cwd>")', () => {
  const AV = loadView();
  // The live rows are labelled with the client's cwd appended, so an equality test
  // on `label` would never fire — the match has to be a prefix.
  assert.equal(
    AV.isClaudeCode({ kind: 'mcp_client', label: 'Claude Code — C:\\dr\\browserplug' }), true);
  assert.equal(AV.isClaudeCode({ kind: 'mcp_client', label: 'Claude Code' }), true);
});

test('isClaudeCode: friendly wins even when the label says something else', () => {
  const AV = loadView();
  assert.equal(
    AV.isClaudeCode({ kind: 'mcp_client', friendly: 'Claude Code', label: 'Kilo CLI — /tmp' }),
    true, 'the two conditions are OR-ed, not AND-ed');
});

test('isClaudeCode rejects other clients, other kinds, and junk', () => {
  const AV = loadView();
  // A different client on the same kind — this is the discrimination that matters:
  // Kilo CLI rows must not sprout Anthropic usage bars.
  assert.equal(AV.isClaudeCode({ kind: 'mcp_client', friendly: 'Kilo CLI' }), false);
  assert.equal(AV.isClaudeCode({ kind: 'mcp_client', label: 'Kilo CLI — C:\\dr\\browserplug' }), false);
  // Right name, wrong kind: browsers/server rows never get usage bars.
  assert.equal(AV.isClaudeCode({ kind: 'browser', friendly: 'Claude Code' }), false);
  assert.equal(AV.isClaudeCode({ kind: 'server', label: 'Claude Code — /x' }), false);
  // Absent row / empty row: popup.js calls this straight out of `conns.some(...)`.
  assert.equal(AV.isClaudeCode(null), false);
  assert.equal(AV.isClaudeCode(undefined), false);
  assert.equal(AV.isClaudeCode({}), false);
  assert.equal(AV.isClaudeCode({ kind: 'mcp_client' }), false);
});

test('isClaudeCode is prefix-anchored and case-sensitive (no loose contains-match)', () => {
  const AV = loadView();
  // `startsWith`, not `includes` — a row that merely mentions Claude Code mid-string
  // is a different client and must not be claimed.
  assert.equal(
    AV.isClaudeCode({ kind: 'mcp_client', label: 'proxy for Claude Code — /x' }), false);
  assert.equal(AV.isClaudeCode({ kind: 'mcp_client', friendly: 'claude code' }), false);
  assert.equal(AV.isClaudeCode({ kind: 'mcp_client', label: 'claude code — /x' }), false);
});

test('isClaudeCode returns a real boolean, never a truthy string', () => {
  const AV = loadView();
  // `conns.some(isClaudeCode)` tolerates truthiness, but the value is also handed
  // around popup.js; the `!!` in the implementation is load-bearing for `=== true`
  // style callers and for anything that serialises it.
  assert.strictEqual(AV.isClaudeCode({ kind: 'mcp_client', friendly: 'Claude Code' }), true);
  assert.strictEqual(AV.isClaudeCode(''), false, 'a falsy non-object must not leak through as ""');
  assert.strictEqual(AV.isClaudeCode(0), false);
});

// ===========================================================================
// 2. automation.js — the hover positive path
// ===========================================================================

const AUTOMATION_SRC = fs.readFileSync(
  new URL('../../extension-gecko/automation.js', import.meta.url), 'utf-8');

// Mirrors makeAuto() in automation.test.mjs: jsdom has no layout engine, so
// getClientRects()/getBoundingClientRect() are stubbed to honour display:none /
// visibility:hidden (what isVisible() reads) and otherwise report a real rect.
// hover() reads that rect for clientX/clientY, so the stub returns a rect with a
// distinct origin/size — a hard-coded 0x0 would let a broken midpoint calculation pass.
function makeAuto(html) {
  const dom = new JSDOM(`<!DOCTYPE html><html><body>${html}</body></html>`,
    { runScripts: 'outside-only', url: 'http://localhost/test' });
  const win = dom.window;

  const RECT = { width: 40, height: 20, top: 100, left: 60, right: 100, bottom: 120, x: 60, y: 100 };
  function rects() {
    let n = this;
    while (n && n.nodeType === 1) {
      const cs = win.getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return [];
      n = n.parentElement;
    }
    return [RECT];
  }
  win.Element.prototype.getClientRects = rects;
  win.Element.prototype.getBoundingClientRect = function () {
    const r = rects.call(this);
    return r[0] || { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0 };
  };
  // Record scrollIntoView so we can assert hover brought the element into view.
  win.__scrollIntoViewCalls = [];
  win.HTMLElement.prototype.scrollIntoView = function (opts) {
    win.__scrollIntoViewCalls.push({ el: this, opts });
  };
  win.scrollTo = () => {};
  win.scrollBy = () => {};

  win.eval(AUTOMATION_SRC);
  return { auto: win.__browserplugAuto, win, RECT };
}

// --- hover ------------------------------------------------------------------

test('hover on a resolvable element returns {ok:true, hovered:true, target}', async () => {
  const { auto } = makeAuto('<button id="b" type="button">Go</button>');

  const res = await auto.invoke('hover', { target: '#b' });

  assert.equal(res.ok, true);
  assert.equal(res.hovered, true);
  assert.ok(res.target, 'the result describes what was hovered');
  assert.equal(res.target.selector, '#b');
  assert.equal(res.target.tag, 'button');
  assert.equal(res.target.role, 'button');
  assert.equal(res.target.name, 'Go', 'described by accessible name');
  assert.match(res.target.ref, /^e\d+$/, 'a reusable ref is minted for the hovered element');
  assert.equal('error' in res, false);
});

test('hover dispatches the pointer/mouse enter sequence, in order, on the target', async () => {
  const { auto, win } = makeAuto('<button id="b" type="button">Go</button>');
  const el = win.document.querySelector('#b');
  const seen = [];
  for (const t of ['pointerover', 'pointerenter', 'pointermove', 'mouseover', 'mouseenter', 'mousemove']) {
    el.addEventListener(t, (e) => seen.push([e.type, e.clientX, e.clientY, e.bubbles]));
  }

  const res = await auto.invoke('hover', { target: '#b' });
  assert.equal(res.ok, true);

  // The order is the contract: a menu that opens on pointerover but closes on a
  // mousemove it never saw first is exactly the hover bug this locks down.
  assert.deepEqual(seen.map(s => s[0]),
    ['pointerover', 'pointerenter', 'pointermove', 'mouseover', 'mouseenter', 'mousemove']);
});

test('hover aims the events at the CENTRE of the element rect', async () => {
  const { auto, win, RECT } = makeAuto('<button id="b" type="button">Go</button>');
  const el = win.document.querySelector('#b');
  const pts = [];
  el.addEventListener('mousemove', (e) => pts.push([e.clientX, e.clientY]));

  await auto.invoke('hover', { target: '#b' });

  // left + width/2, top + height/2 — hit-testing a corner would land on a
  // neighbour once elements abut.
  assert.deepEqual(pts, [[RECT.left + RECT.width / 2, RECT.top + RECT.height / 2]]);
  assert.deepEqual(pts, [[80, 110]], 'and that is a real midpoint, not a 0,0 default');
});

test('hover events bubble and are cancelable (delegated listeners must see them)', async () => {
  const { auto, win } = makeAuto('<div id="wrap"><button id="b" type="button">Go</button></div>');
  const onParent = [];
  win.document.querySelector('#wrap')
    .addEventListener('mouseover', (e) => onParent.push([e.type, e.target.id, e.cancelable]));

  await auto.invoke('hover', { target: '#b' });

  assert.deepEqual(onParent, [['mouseover', 'b', true]],
    'a listener on the ancestor saw the event, retargeted to the button');
});

test('hover scrolls the element into view before dispatching', async () => {
  const { auto, win } = makeAuto('<button id="b" type="button">Go</button>');

  const res = await auto.invoke('hover', { target: '#b' });
  assert.equal(res.ok, true);

  assert.equal(win.__scrollIntoViewCalls.length, 1);
  assert.equal(win.__scrollIntoViewCalls[0].el, win.document.querySelector('#b'));
  assert.deepEqual(
    { block: win.__scrollIntoViewCalls[0].opts.block, inline: win.__scrollIntoViewCalls[0].opts.inline },
    { block: 'center', inline: 'center' });
});

test('hover accepts a ref minted by snapshot, and by accessible-name text', async () => {
  const { auto } = makeAuto('<button id="b" type="button">Go</button>');
  const snap = await auto.invoke('snapshot', {});
  const ref = snap.elements.find(e => e.selector === '#b').ref;

  const byRef = await auto.invoke('hover', { ref });
  assert.equal(byRef.ok, true);
  assert.equal(byRef.target.selector, '#b');

  const byText = await auto.invoke('hover', { target: 'Go' });
  assert.equal(byText.ok, true, 'accessible-name resolution reaches hover too');
  assert.equal(byText.target.selector, '#b');
});

test('hover honours `nth` and `within` when several elements share a name', async () => {
  const { auto } = makeAuto(`
    <div id="a"><button type="button">Open</button></div>
    <div id="z"><button type="button">Open</button></div>`);

  const second = await auto.invoke('hover', { target: 'Open', nth: 1 });
  assert.equal(second.ok, true);
  assert.equal(second.target.selector, '#z > button', 'nth:1 picked the second match');

  const scoped = await auto.invoke('hover', { target: 'Open', within: '#a' });
  assert.equal(scoped.ok, true);
  assert.equal(scoped.target.selector, '#a > button', 'within scoped the same name to the first');
});

test('hover refuses an invisible element rather than reporting a phantom hover', async () => {
  const { auto, win } = makeAuto('<button id="b" type="button" style="display:none">Go</button>');
  const el = win.document.querySelector('#b');
  let fired = 0;
  el.addEventListener('mouseover', () => fired++);

  const res = await auto.invoke('hover', { target: '#b' });

  assert.equal(res.ok, false);
  assert.match(res.error, /not visible/);
  assert.equal(res.hovered, undefined, 'no hovered:true on a refusal');
  assert.equal(fired, 0, 'and nothing was dispatched');
});
