/*
 * Click confirms the control's STATE changed, not just that an event fired.
 *
 * dispatchEvent always comes back, so "the filter applied" and "the click landed on
 * nothing" are indistinguishable at the event layer. In a filter sidebar that gap is
 * a correctness bug, not an inconvenience: a silently-no-op capacity filter means the
 * agent reads an UNFILTERED result set and reports the wrong "cheapest".
 *
 * The contract this pins, mirroring fill()'s existing valueReverted rule (a control
 * that did not take the change is ok:false, not a success with a footnote):
 *   - a click on a checkbox/label/aria-toggle reports the control's post-settle state;
 *   - a toggle that did NOT move, with nothing else to show for it, is ok:false;
 *   - a <label for="..."> naming an id that is not in the document is called out —
 *     that is the virtualized filter-sidebar shape, a guaranteed no-op;
 *   - ordinary buttons/links are unaffected (no new latency, no new failure mode).
 *
 * automation.js is a [shared] file, so this suite is scope="shared" in parity.toml.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from 'jsdom';

const AUTOMATION_SRC = fs.readFileSync(
  new URL('../../extension-gecko/automation.js', import.meta.url), 'utf-8');

function makeAuto(html, { headStyle = '' } = {}) {
  const dom = new JSDOM(
    `<!DOCTYPE html><html><head><style>${headStyle}</style></head><body>${html}</body></html>`,
    { runScripts: 'outside-only', url: 'https://shop.example/c/dyski-hdd' });
  const win = dom.window;
  function rects() {
    let n = this;
    while (n && n.nodeType === 1) {
      const cs = win.getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return [];
      n = n.parentElement;
    }
    return [{ width: 10, height: 10, top: 0, left: 0, right: 10, bottom: 10, x: 0, y: 0 }];
  }
  win.Element.prototype.getClientRects = rects;
  win.Element.prototype.getBoundingClientRect = function () {
    const r = rects.call(this);
    return r[0] || { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0 };
  };
  win.HTMLElement.prototype.scrollIntoView = function () {};
  win.eval(AUTOMATION_SRC);
  return { auto: win.__browserplugAuto, win };
}

test('a working label->checkbox click reports the resulting checked state', async () => {
  const { auto } = makeAuto(`<label for="cb">uzywane (49)</label><input id="cb" type="checkbox">`);
  const r = await auto.invoke('click', { target: 'label[for="cb"]' });
  assert.equal(r.ok, true);
  assert.equal(r.control.kind, 'native');
  assert.equal(r.control.via, 'label');
  assert.equal(r.control.checkedBefore, false);
  assert.equal(r.control.checked, true, 'the POST-settle state, not "an event was dispatched"');
  assert.equal(r.control.checkedChanged, true);
});

test('unchecking is a change too (the state is read, not assumed)', async () => {
  const { auto } = makeAuto(`<input id="cb" type="checkbox" checked>`);
  const r = await auto.invoke('click', { target: '#cb' });
  assert.equal(r.ok, true);
  assert.equal(r.control.checkedBefore, true);
  assert.equal(r.control.checked, false);
  assert.equal(r.control.checkedChanged, true);
});

test('a toggle whose state does NOT move is ok:false, not a bare "ok"', async () => {
  // A div carrying role=checkbox whose handler never updates aria-checked — the
  // x-kom shape: nine clicks, nine "ok", zero filters applied.
  const { auto } = makeAuto(`<div role="checkbox" aria-checked="false" tabindex="0">4000 GB (13)</div>`);
  const r = await auto.invoke('click', { target: '[role=checkbox]' });
  assert.equal(r.ok, false, 'a filter that did not apply must not read as success');
  assert.equal(r.checkedUnchanged, true);
  assert.equal(r.control.kind, 'aria');
  assert.equal(r.control.checked, false);
  assert.equal(r.control.checkedChanged, false);
  assert.match(r.error, /did not change state/);
  assert.match(r.error, /aria-checked is still false/);
  assert.match(r.error, /action="check"/, 'names a concrete alternative, not just the failure');
});

test('a working aria toggle (handler flips aria-checked) still reports ok', async () => {
  const { auto, win } = makeAuto(`<div id="t" role="checkbox" aria-checked="false" tabindex="0">Filtr</div>`);
  const el = win.document.getElementById('t');
  el.addEventListener('click', () => {
    el.setAttribute('aria-checked', el.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
  });
  const r = await auto.invoke('click', { target: '#t' });
  assert.equal(r.ok, true);
  assert.equal(r.control.checked, true);
  assert.equal(r.control.checkedChanged, true);
});

test('a toggle that commits LATE (framework tick) is still seen as changed', async () => {
  const { auto, win } = makeAuto(`<div id="t" role="checkbox" aria-checked="false" tabindex="0">Filtr</div>`);
  const el = win.document.getElementById('t');
  // 150ms is past click()'s first read and inside its confirm-poll window; a single
  // synchronous read right after dispatch would have called this a no-op.
  el.addEventListener('click', () => {
    win.setTimeout(() => el.setAttribute('aria-checked', 'true'), 150);
  });
  const r = await auto.invoke('click', { target: '#t' });
  assert.equal(r.ok, true, 'the confirm poll waits for the reactive flush');
  assert.equal(r.control.checked, true);
});

test('a <label for=...> naming a missing id is called out as a guaranteed no-op', async () => {
  // The virtualized filter sidebar: labels are painted, their inputs are only
  // rendered once the section expands. Clicking the label can never toggle anything.
  const { auto } = makeAuto(`<div class="filters">
      <button id="expand">Pojemnosc</button>
      <label for="cap-4000">4000 GB (13)</label>
    </div>`);
  const r = await auto.invoke('click', { target: 'label[for="cap-4000"]' });
  assert.equal(r.ok, false);
  assert.equal(r.labelForMissing, 'cap-4000');
  assert.match(r.error, /no element with that id exists/);
  assert.match(r.error, /expand/i, 'says what to do next');
});

test('a dangling label whose click DID something else is not called a no-op', async () => {
  const { auto, win } = makeAuto(`<label for="gone">Sort by price</label><ul id="list"></ul>`);
  const lab = win.document.querySelector('label');
  lab.addEventListener('click', () => {
    win.document.getElementById('list').appendChild(win.document.createElement('li'));
  });
  const r = await auto.invoke('click', { target: 'label[for="gone"]' });
  assert.equal(r.ok, true, 'the page re-rendered, so the click plainly landed');
  assert.equal(r.labelForMissing, 'gone', 'still reported — it is worth knowing');
  assert.equal(r.structuralChange, true);
});

test('a toggle whose click navigates is trusted even if .checked reads stale', async () => {
  // The live Allegro case: cb.checked still read false in the same tick in which the
  // URL had ALREADY gained "&stan-grupa=uzywane". Something observable happened, so
  // the click is not a no-op even though the control's own state looks unmoved.
  const { auto, win } = makeAuto(`<label for="cb">uzywane (49)</label>
    <input id="cb" type="checkbox">`);
  const cb = win.document.getElementById('cb');
  cb.addEventListener('click', (e) => {
    e.preventDefault();                       // the framework refuses the native toggle
    win.history.pushState({}, '', '/listing?stan-grupa=uzywane');
  });
  const r = await auto.invoke('click', { target: 'label[for="cb"]' });
  assert.equal(r.ok, true, 'urlChanged is enough evidence the click landed');
  assert.equal(r.urlChanged, true);
  assert.equal(r.control.checkedChanged, false, 'and the honest control state is still reported');
});

test('an INFERRED control (a row that merely contains a checkbox) is judged loosely', async () => {
  // Clicking a row that happens to hold one checkbox, where the row expands instead
  // of toggling, is a legitimate interaction. Only EXPLICIT associations (the input
  // itself, or a <label> that names it) are held to "the toggle must move" — the
  // x-kom failure was explicit, and a re-render elsewhere is no excuse there.
  const { auto, win } = makeAuto(`<div id="row"><span>Western Digital</span>
      <input id="cb" type="checkbox"><span>(21)</span></div><div id="detail"></div>`);
  win.document.getElementById('cb').addEventListener('click', (e) => e.preventDefault());
  win.document.getElementById('row').addEventListener('click', () => {
    win.document.getElementById('detail').appendChild(win.document.createElement('p'));
  });
  const r = await auto.invoke('click', { target: '#row' });
  assert.equal(r.control.via, 'descendant');
  assert.equal(r.control.checkedChanged, false);
  assert.equal(r.structuralChange, true);
  assert.equal(r.ok, true, 'the row DID expand — that is what the click was for');
});

test('an ordinary button click is unchanged — no control block, no new failure', async () => {
  const { auto } = makeAuto(`<button id="b">Pokaz wiecej produktow</button>`);
  const r = await auto.invoke('click', { target: '#b' });
  assert.equal(r.ok, true);
  assert.equal(r.control, undefined, 'nothing toggle-shaped => no confirm poll, no latency');
  assert.equal(r.clicked, true);
});

test('mutations are split into structural vs attribute-only churn', async () => {
  const { auto, win } = makeAuto(`<button id="b">Go</button><div id="out"></div>`);
  win.document.getElementById('b').addEventListener('click', () => {
    win.document.getElementById('out').appendChild(win.document.createElement('p'));
  });
  const r = await auto.invoke('click', { target: '#b' });
  assert.equal(r.structuralChange, true);
  assert.ok(r.mutationBreakdown.childList >= 1);

  const { auto: auto2, win: win2 } = makeAuto(`<button id="c">Go</button>`);
  win2.document.getElementById('c').addEventListener('click', function () {
    this.setAttribute('data-touched', '1');   // a focus-ring-class-style no-op
  });
  const r2 = await auto2.invoke('click', { target: '#c' });
  assert.equal(r2.structuralChange, false,
    'attribute churn alone is not evidence the click accomplished anything');
  assert.ok(r2.mutationBreakdown.attributes >= 1);
});

test('a hidden target still fails BEFORE dispatch (existing contract preserved)', async () => {
  const { auto } = makeAuto(
    `<div class="section"><label for="cb">4000 GB</label><input id="cb" type="checkbox"></div>`,
    { headStyle: '.section { display: none; }' });
  const r = await auto.invoke('click', { target: 'label[for="cb"]' });
  assert.equal(r.ok, false);
  assert.match(r.error, /not visible/);
});

test('a collapsed ancestor is named as the probable cause of a dead click', async () => {
  const { auto, win } = makeAuto(`<details id="acc">
      <summary>Pojemnosc</summary>
      <div role="checkbox" aria-checked="false" id="chk">4000 GB (13)</div>
    </details>`);
  // jsdom renders <details> content regardless of `open`, which is exactly the shape
  // we want: the label is "visible" yet its section is closed, so the site's handler
  // is not wired up and the click accomplishes nothing.
  assert.equal(win.document.getElementById('acc').open, false);
  const r = await auto.invoke('click', { target: '#chk' });
  assert.equal(r.ok, false);
  assert.equal(r.reachability.collapsedAncestor.reason, 'inside a closed <details> section');
  assert.match(r.error, /closed <details>/);
  assert.match(r.error, /Expand that section first/);
});

// ---------------------------------------------------------------------------
// The control node is REPLACED, not mutated — what a framework does on nearly every
// state change. These drive tests/fixtures/rerendered-checkbox.html directly (jsdom
// runs its inline scripts), so the fixture's header comment and this suite cannot
// drift apart. The same page is exercised through a live browser by
// tests/integration/test_agent_experience.py.
// ---------------------------------------------------------------------------

function makeAutoFromFixture(name) {
  const html = fs.readFileSync(
    new URL('../fixtures/' + name, import.meta.url), 'utf-8');
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',                 // the fixture's own handlers must run
    url: 'https://shop.example/c/dyski-hdd'
  });
  const win = dom.window;
  function rects() {
    let n = this;
    while (n && n.nodeType === 1) {
      const cs = win.getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return [];
      n = n.parentElement;
    }
    return [{ width: 10, height: 10, top: 0, left: 0, right: 10, bottom: 10, x: 0, y: 0 }];
  }
  win.Element.prototype.getClientRects = rects;
  win.Element.prototype.getBoundingClientRect = function () {
    const r = rects.call(this);
    return r[0] || { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0 };
  };
  win.HTMLElement.prototype.scrollIntoView = function () {};
  win.eval(AUTOMATION_SRC);
  return { auto: win.__browserplugAuto, win };
}

test('a control replaced by a re-render is re-resolved, not read as a detached corpse', async () => {
  // The bug this pins: the confirm poll captured the control BEFORE dispatch and
  // re-read that same node for ~400ms. A re-render detaches it, so .checked could
  // never move again and a click that worked perfectly was reported ok:false —
  // the false negative this whole check exists to prevent, inverted.
  const { auto, win } = makeAutoFromFixture('rerendered-checkbox.html');
  const before = win.document.getElementById('rr-toggle');
  const r = await auto.invoke('click', { target: '#rr-toggle' });
  const after = win.document.getElementById('rr-toggle');

  assert.notEqual(before, after, 'the fixture really did replace the node');
  assert.equal(before.isConnected, false, '...and the original really is detached');
  assert.equal(r.ok, true, 'the click worked; it must not be reported as a no-op');
  assert.equal(r.control.checked, true);
  assert.equal(r.control.checkedChanged, true);
  assert.equal(r.control.reresolved, 'id', 'says HOW it recovered, so the report is auditable');
});

test('re-resolving does not become a way to LOSE the no-op detection', async () => {
  // Same re-render, but the fresh node carries the OLD value — a controlled
  // component discarding an untrusted edit. Still a genuine failure.
  const { auto } = makeAutoFromFixture('rerendered-checkbox.html');
  const r = await auto.invoke('click', { target: '#rr-dead' });
  assert.equal(r.ok, false);
  assert.equal(r.checkedUnchanged, true);
  assert.equal(r.control.checked, false);
  assert.equal(r.control.reresolved, 'id', 'it read the FRESH node and still saw no change');
});

test('a control that is removed outright reports "detached", never "it did not move"', async () => {
  // Nothing can be re-resolved. Claiming the toggle never moved would be a
  // confident statement about a node that no longer exists — and the removal is
  // itself evidence the click did something.
  const { auto } = makeAutoFromFixture('rerendered-checkbox.html');
  const r = await auto.invoke('click', { target: '#rr-gone' });
  assert.equal(r.controlDetached, true);
  assert.equal(r.control.detached, true);
  assert.equal(r.control.checked, undefined, 'no claim is made about a vanished control');
  assert.equal(r.control.checkedChanged, false);
  assert.equal(r.checkedUnchanged, undefined, 'and it is NOT reported as a dead click');
  assert.equal(r.ok, true);
});

test('re-resolution falls back to the click target when the control has no id', async () => {
  const { auto, win } = makeAuto(`<div id="slot">
      <label id="lab">uzywane (49)<input type="checkbox"></label>
    </div>`);
  const lab = win.document.getElementById('lab');
  lab.addEventListener('click', () => {
    // Replace ONLY the anonymous input. The label (our click target) survives, so
    // re-running resolution from it is the only route to the new node — there is no
    // id to look up.
    setTimeout(() => {
      const fresh = win.document.createElement('input');
      fresh.type = 'checkbox';
      fresh.checked = true;
      lab.replaceChild(fresh, lab.querySelector('input'));
    }, 30);
  });
  const r = await auto.invoke('click', { target: '#lab' });
  assert.equal(r.control.reresolved, 'target');
  assert.equal(r.control.checked, true);
  assert.equal(r.ok, true);
});
