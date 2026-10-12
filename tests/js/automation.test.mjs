/*
 * Deterministic unit tests for extension-gecko/automation.js v2 (window.__browserplugAuto,
 * __v === 2), run under jsdom so they need no browser. jsdom has no layout
 * engine, so we stub getClientRects()/getBoundingClientRect() to honour
 * display:none / visibility:hidden (what isVisible() actually depends on) and
 * otherwise report a non-empty rect. window.scrollTo/scrollBy are jsdom
 * "not implemented" stubs, so makeAuto replaces them with recording stubs.
 * Run with: npm run test:js  (== node --test "tests/js/*.test.mjs")
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from 'jsdom';

// Arrays produced INSIDE automation.js run in the jsdom realm, so their
// Array.prototype is not reference-equal to this (Node) realm's — and
// assert.deepStrictEqual rejects "same structure, different prototype". For
// plain primitive structures, compare by JSON shape to stay realm-agnostic.
function sameShape(actual, expected, msg) {
  assert.equal(JSON.stringify(actual), JSON.stringify(expected), msg);
}

const AUTOMATION_SRC = fs.readFileSync(
  new URL('../../extension-gecko/automation.js', import.meta.url), 'utf-8');

function makeAuto(html) {
  const dom = new JSDOM(`<!DOCTYPE html><html><body>${html}</body></html>`,
    { runScripts: 'outside-only', url: 'http://localhost/test' });
  const win = dom.window;

  // Stub layout: empty rects when display:none / visibility:hidden (self or an
  // ancestor), otherwise a 10x10 rect — enough for isVisible() and pageState().
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
  // jsdom's scrollTo/scrollBy print "Not implemented" noise — replace with
  // recording stubs so the scroll tests can assert what was requested.
  win.__scrollCalls = [];
  win.scrollTo = (x, y) => { win.__scrollCalls.push(['to', x, y]); };
  win.scrollBy = (x, y) => { win.__scrollCalls.push(['by', x, y]); };

  win.eval(AUTOMATION_SRC);
  return win.__browserplugAuto;
}

// __browserplugAuto is created inside the jsdom window; reach its document via an
// element ref (the refs map holds elements whose ownerDocument is the jsdom doc).
function globalThisDoc(auto) {
  const el = auto.resolveAll({ selector: '*' })[0];
  return el ? el.ownerDocument : null;
}

function winOf(auto) {
  return globalThisDoc(auto).defaultView;
}

// A wait predicate reaches the toolkit COMPILED: the background inlines it as a function
// beside the invoke() call (gecko) or builds it inside the invoking func (chromium), so
// automation.js never evaluates a string. Compile here the way the sandbox sees it, in
// the jsdom realm so `document` is the fixture's.
function compiled(auto, src) {
  return winOf(auto).eval('(function () { return (\n' + src + '\n); })');
}

// ---- version guard ----------------------------------------------------------

test('toolkit is v2 and re-injection does not replace the live instance', async () => {
  const auto = makeAuto(`<button id="b">Go</button>`);
  assert.equal(auto.__v, 2);
  const win = winOf(auto);
  win.eval(AUTOMATION_SRC); // background re-injects SRC before every invoke
  assert.equal(win.__browserplugAuto, auto, 'refs minted earlier must survive re-injection');
});

// ---- snapshot (interactive mode) ---------------------------------------------

test('snapshot lists interactive elements with refs, names, roles, values', async () => {
  const auto = makeAuto(`
    <label for="name">Full name</label><input id="name" type="text" value="Ada">
    <label for="color">Colour</label>
    <select id="color"><option value="">--</option><option value="g">Green</option></select>
    <button id="real" type="button">Save</button>
    <button id="ghost" type="button" style="display:none">Save</button>
    <button id="dis" type="button" disabled>Nope</button>
  `);
  const snap = await auto.invoke('snapshot', {});
  assert.equal(snap.ok, true);
  assert.ok(snap.pageState, 'snapshot carries pageState diagnostics');
  const bySel = Object.fromEntries(snap.elements.map(e => [e.selector, e]));
  assert.equal(bySel['#name'].role, 'textbox');
  assert.equal(bySel['#name'].name, 'Full name');
  assert.equal(bySel['#name'].value, 'Ada');
  assert.equal(bySel['#color'].role, 'combobox');
  assert.equal(bySel['#real'].role, 'button');
  assert.ok(snap.elements.every(e => e.ref), 'every element has a ref');
  // hidden twin excluded by default; disabled button present but enabled:false
  assert.ok(!bySel['#ghost'], 'display:none element excluded by default');
  assert.equal(bySel['#dis'].enabled, false);
});

test('snapshot dedupe collapses identical-name runs without starving unique controls', async () => {
  const dups = Array.from({ length: 30 }, () => `<button type="button">Item</button>`).join('');
  const auto = makeAuto(`
    ${dups}
    <label for="u1">Email</label><input id="u1" type="text">
    <button id="u2" type="button">Save</button>
  `);
  const snap = await auto.invoke('snapshot', { max: 10 });
  assert.equal(snap.ok, true);
  // 30 dups collapse to one entry, so the cap of 10 leaves room for the uniques
  assert.equal(snap.count, 3);
  const names = snap.elements.map(e => e.name);
  assert.ok(names.includes('Email'), 'unique input survives the cap');
  assert.ok(names.includes('Save'), 'unique button survives the cap');
  const first = snap.elements.find(e => e.name === 'Item');
  assert.equal(first.dupCount, 30, 'first duplicate carries the total run count');
  assert.equal(snap.dedupedDuplicates, 29);

  // dedupe:false returns the raw stream, capped at max
  const raw = await auto.invoke('snapshot', { max: 10, dedupe: false });
  assert.equal(raw.ok, true);
  assert.equal(raw.elements.length, 10);
  assert.ok(raw.elements.every(e => e.name === 'Item'), 'cap eaten by duplicates');
  assert.equal(raw.dedupedDuplicates, 0);
  assert.equal(raw.truncated, true);
});

test('snapshot mode:forms groups radios by name and reports inputs/selects/buttons', async () => {
  const auto = makeAuto(`
    <label><input type="radio" name="size" value="s">Small</label>
    <label><input type="radio" name="size" value="m" checked>Medium</label>
    <label><input type="radio" name="size" value="l">Large</label>
    <input type="checkbox" name="opts" value="x" id="cb1"><label for="cb1">Extras</label>
    <label for="em">Email</label><input id="em" type="email" value="a@b.c" required>
    <select id="fs"><option value="a" selected>Alpha</option><option value="b">Beta</option></select>
    <button id="go" type="button">Go</button>
    <button id="no" type="button" disabled>No</button>
  `);
  const snap = await auto.invoke('snapshot', { mode: 'forms' });
  assert.equal(snap.ok, true);
  assert.equal(snap.mode, 'forms');

  // radio group collapsed by name, with per-option label/checked and a selected list
  assert.equal(snap.radioGroups.length, 1);
  const size = snap.radioGroups[0];
  assert.equal(size.name, 'size');
  sameShape(size.options.map(o => o.value), ['s', 'm', 'l']);
  sameShape(size.options.map(o => o.label), ['Small', 'Medium', 'Large']);
  sameShape(size.options.map(o => o.checked), [false, true, false]);
  sameShape(size.selected, ['m']);
  assert.ok(size.options.every(o => o.ref), 'radio options carry refs');

  const opts = snap.checkboxGroups.find(g => g.name === 'opts');
  assert.ok(opts, 'checkbox group collapsed by name');
  sameShape(opts.selected, []);

  const em = snap.inputs.find(i => i.label === 'Email');
  assert.equal(em.value, 'a@b.c');
  assert.equal(em.required, true);
  assert.equal(em.enabled, true);

  const sel = snap.selects.find(s => s.ref);
  assert.equal(sel.selected, 'a');
  sameShape(sel.options, ['Alpha', 'Beta']);

  const byLabel = Object.fromEntries(snap.buttons.map(b => [b.label, b]));
  assert.equal(byLabel['Go'].enabled, true);
  assert.equal(byLabel['No'].enabled, false);
});

// ---- unified target resolution (target: ref -> CSS -> accessible name) -------

test('click resolves target as a snapshot-minted ref', async () => {
  const auto = makeAuto(`<button id="b" type="button">Hit</button><span id="out"></span>`);
  const snap = await auto.invoke('snapshot', {});
  const ref = snap.elements.find(e => e.selector === '#b').ref;
  assert.match(ref, /^e\d+$/);
  const doc = globalThisDoc(auto);
  doc.getElementById('b').addEventListener('click', () => { doc.getElementById('out').textContent = 'hit'; });
  const res = await auto.invoke('click', { target: ref });
  assert.equal(res.ok, true);
  assert.equal(res.target.ref, ref);
  assert.equal(doc.getElementById('out').textContent, 'hit');
});

test('click resolves target as a CSS selector', async () => {
  const auto = makeAuto(`<button id="b" type="button">Hit</button>`);
  const res = await auto.invoke('click', { target: '#b' });
  assert.equal(res.ok, true);
  assert.equal(res.target.selector, '#b');
});

test('click resolves target as accessible-name text when CSS yields nothing', async () => {
  // "Hit" parses as a (type) selector but matches no element -> falls to name match
  const auto = makeAuto(`<button id="b" type="button">Hit</button>`);
  const res = await auto.invoke('click', { target: 'Hit' });
  assert.equal(res.ok, true);
  assert.equal(res.target.selector, '#b');
});

test('nth picks the right match (0-based)', async () => {
  const auto = makeAuto(`
    <button id="a1" type="button">Add</button>
    <button id="a2" type="button">Add</button>
    <button id="a3" type="button">Add</button>
    <span id="out"></span>`);
  const doc = globalThisDoc(auto);
  doc.getElementById('a2').addEventListener('click', () => { doc.getElementById('out').textContent = 'second'; });
  const res = await auto.invoke('click', { target: 'Add', nth: 1 });
  assert.equal(res.ok, true);
  assert.equal(res.target.selector, '#a2');
  assert.equal(doc.getElementById('out').textContent, 'second');
  // out-of-range nth matches nothing
  const miss = await auto.invoke('click', { target: 'Add', nth: 9 });
  assert.equal(miss.ok, false);
  assert.match(miss.error, /no element/i);
});

test('within scopes resolution to a container', async () => {
  const auto = makeAuto(`
    <div id="c1"><button id="g1" type="button">Go</button></div>
    <div id="c2"><button id="g2" type="button">Go</button></div>`);
  const res = await auto.invoke('click', { target: 'Go', within: '#c2' });
  assert.equal(res.ok, true);
  assert.equal(res.target.selector, '#g2');
});

// SAFETY: the container named by `within` has closed (modal dismissed, row
// re-rendered). Widening the search back to the document would click a DIFFERENT
// same-named control on the live authenticated page.
test('a within that matches nothing yields ZERO matches — never a document-wide click', async () => {
  const auto = makeAuto(`
    <div id="page"><button id="danger" type="button">Delete</button></div>
    <span id="out"></span>`);
  const doc = globalThisDoc(auto);
  doc.getElementById('danger').addEventListener('click', () => { doc.getElementById('out').textContent = 'BOOM'; });

  // accessible-name path (byText -> collect): "Delete" is no CSS match
  const byText = await auto.invoke('click', { target: 'Delete', within: '#confirm-modal' });
  assert.equal(byText.ok, false);
  assert.equal(doc.getElementById('out').textContent, '', 'the page-wide twin must not receive the click');
  assert.match(byText.error, /scope not found/i);
  assert.match(byText.error, /not widened/i);
  assert.match(byText.error, /page_snapshot/i);

  // CSS path (scope.querySelectorAll)
  const byCss = await auto.invoke('click', { target: '#danger', within: '#confirm-modal' });
  assert.equal(byCss.ok, false);
  assert.match(byCss.error, /scope not found/i);
  assert.equal(doc.getElementById('out').textContent, '');

  // resolution itself is empty on every input shape — not merely refused at the click layer
  assert.equal(auto.resolveAll({ target: 'Delete', within: '#confirm-modal' }).length, 0);
  assert.equal(auto.resolveAll({ selector: '#danger', within: '#confirm-modal' }).length, 0);
  assert.equal(auto.resolveAll({ text: 'Delete', within: '#confirm-modal' }).length, 0);
  // ...and with no `within` at all the whole document is still searched
  assert.equal(auto.resolveAll({ target: 'Delete' }).length, 1);
});

test('a ref-shaped within that no longer resolves reports a stale scope', async () => {
  const auto = makeAuto(`<button id="del" type="button">Delete</button><span id="out"></span>`);
  const doc = globalThisDoc(auto);
  doc.getElementById('del').addEventListener('click', () => { doc.getElementById('out').textContent = 'BOOM'; });
  const res = await auto.invoke('click', { target: 'Delete', within: 'e999' });
  assert.equal(res.ok, false);
  assert.match(res.error, /scope not found/i);
  assert.match(res.error, /stale/i);
  assert.equal(doc.getElementById('out').textContent, '');
});

test('the unresolvable-scope hint reaches every action (shared staleRefHint channel)', async () => {
  const auto = makeAuto(`
    <input id="q" type="text" value="keep">
    <select id="s"><option value="1">Red</option><option value="2">Green</option></select>
    <button id="b" type="button">Go</button>`);
  const doc = globalThisDoc(auto);
  for (const [method, args] of [
    ['fill', { target: '#q', value: 'typed', within: '#gone' }],
    ['select', { target: '#s', value: 'Green', within: '#gone' }],
    ['press', { target: '#b', key: 'Enter', within: '#gone' }],
    ['hover', { target: '#b', within: '#gone' }],
    ['scroll', { target: '#b', within: '#gone' }],
    ['rect', { target: '#b', within: '#gone' }]
  ]) {
    const res = await auto.invoke(method, args);
    assert.equal(res.ok, false, method + ' must fail on an unresolvable scope');
    assert.match(res.error, /scope not found/i, method + ' must say the scope was not found');
  }
  assert.equal(doc.getElementById('q').value, 'keep', 'the out-of-scope input was not filled');
  assert.equal(doc.getElementById('s').value, '1', 'the out-of-scope select was not changed');
});

test('snapshot with an unresolvable root SAYS SO (no whole-document fallback, no silent empty list)', async () => {
  const auto = makeAuto(`<button id="b" type="button">Go</button>`);
  const miss = await auto.invoke('snapshot', { root: '#not-there' });
  // an empty element list would read as "nothing on the page" and the server would
  // misdiagnose it as "page may still be loading" — the root miss must be explicit
  assert.equal(miss.ok, false);
  assert.match(miss.error, /root not found/i);
  assert.equal(miss.elements, undefined);
  // a root that DOES resolve still scopes normally
  const hit = await auto.invoke('snapshot', { root: 'body' });
  assert.equal(hit.ok, true);
  assert.equal(hit.count, 1);
});

// The target-optional actions (press keys whatever has focus; scroll moves the
// window) never reach resolveAll when no target is given, so the scope refusal has
// to be checked in the method itself — otherwise "press Enter inside the login
// modal" submits whatever form holds focus once that modal is gone.
test('press/scroll with a vanished within and NO target are refused, not run document-wide', async () => {
  const auto = makeAuto(`<form id="f"><input id="q" type="text"></form>`);
  const doc = globalThisDoc(auto);
  const win = doc.defaultView;
  let keys = 0;
  doc.getElementById('f').addEventListener('keydown', () => { keys++; });
  doc.getElementById('q').focus();

  const pressed = await auto.invoke('press', { key: 'Enter', within: '#login-modal' });
  assert.equal(pressed.ok, false, 'press must refuse an unresolvable scope');
  assert.match(pressed.error, /scope not found/i);
  assert.equal(keys, 0, 'no key reached the focused form outside the (missing) scope');

  const scrolled = await auto.invoke('scroll', { to: 'bottom', within: '#login-modal' });
  assert.equal(scrolled.ok, false, 'scroll must refuse an unresolvable scope');
  assert.equal(win.__scrollCalls.length, 0, 'the window was not scrolled');
});

test('press with a RESOLVED within refuses when focus sits outside that container', async () => {
  const auto = makeAuto(`
    <form id="outer"><input id="q" type="text"></form>
    <div id="modal"><input id="m" type="text"></div>`);
  const doc = globalThisDoc(auto);
  let outerKeys = 0;
  doc.getElementById('outer').addEventListener('keydown', () => { outerKeys++; });
  doc.getElementById('q').focus(); // focus is OUTSIDE #modal

  const res = await auto.invoke('press', { key: 'Enter', within: '#modal' });
  assert.equal(res.ok, false);
  assert.match(res.error, /outside within/i);
  assert.equal(outerKeys, 0, 'the outer form was not submitted');

  // focus inside the scope -> the key goes through
  doc.getElementById('m').focus();
  const ok = await auto.invoke('press', { key: 'Enter', within: '#modal' });
  assert.equal(ok.ok, true);
});

test('a within that RESOLVES still constrains a ref target (refs span the whole document)', async () => {
  const auto = makeAuto(`
    <div id="row"><button id="rowdel" type="button">Delete</button></div>
    <div id="confirm-modal"><button id="okdel" type="button">Delete</button></div>
    <span id="out"></span>`);
  const doc = globalThisDoc(auto);
  doc.getElementById('rowdel').addEventListener('click', () => { doc.getElementById('out').textContent = 'ROW-DELETED'; });

  const snap = await auto.invoke('snapshot', {});
  const rowRef = snap.elements.find(e => e.selector === '#rowdel').ref;
  // the ref names the ROW's Delete; the scope names the modal -> no match, no click
  const res = await auto.invoke('click', { target: rowRef, within: '#confirm-modal' });
  assert.equal(res.ok, false, 'a ref outside the scope must not resolve');
  assert.equal(doc.getElementById('out').textContent, '', 'the row Delete was NOT clicked');
  assert.equal(auto.resolveAll({ ref: rowRef, within: '#confirm-modal' }).length, 0);
  // the same ref with no scope, or scoped to its own container, still works
  assert.equal(auto.resolveAll({ ref: rowRef }).length, 1);
  assert.equal(auto.resolveAll({ ref: rowRef, within: '#row' }).length, 1);
});

// E: scope RESOLVES, ref RESOLVES, ref is simply not inside it. That is the one miss the
// agent can act on immediately — a generic "No element matched" would send it
// re-snapshotting a ref that is perfectly good, or dropping `within` and clicking the twin.
test('a live ref OUTSIDE a resolved within says exactly that (not "no element matched")', async () => {
  const auto = makeAuto(`
    <div id="row"><button id="rowdel" type="button">Delete</button></div>
    <div id="confirm-modal"><button id="okdel" type="button">Delete</button></div>`);
  const snap = await auto.invoke('snapshot', {});
  const rowRef = snap.elements.find(e => e.selector === '#rowdel').ref;

  const res = await auto.invoke('click', { target: rowRef, within: '#confirm-modal' });
  assert.equal(res.ok, false);
  assert.match(res.error, /not inside within/i, 'names the real reason: the ref is out of scope');
  assert.match(res.error, /#confirm-modal/, 'names the scope it is outside of');
  assert.doesNotMatch(res.error, /stale/i, 'the ref is live — do not send the agent re-snapshotting');
  assert.doesNotMatch(res.error, /No element matched/i);

  // shared hint channel: every target-resolving action gets it
  const hov = await auto.invoke('hover', { ref: rowRef, within: '#confirm-modal' });
  assert.match(hov.error, /not inside within/i);

  // a ref INSIDE the scope still resolves normally
  const ok = await auto.invoke('click', { target: rowRef, within: '#row' });
  assert.equal(ok.ok, true);
});

test('a malformed within selector is reported as malformed, not as a closed container', async () => {
  const auto = makeAuto(`<button id="b" type="button">Go</button>`);
  const res = await auto.invoke('click', { target: 'Go', within: 'div[class=' });
  assert.equal(res.ok, false);
  assert.match(res.error, /invalid CSS selector/i);
  // must NOT steer the agent into a re-snapshot loop over a selector that can never parse
  assert.doesNotMatch(res.error, /re-run page_snapshot/i);
});

test('a batch fill inherits the top-level within (scope is not dropped per field)', async () => {
  const auto = makeAuto(`
    <input id="outside" type="text" value="keep">
    <div id="form"><input id="inside" type="text" value=""></div>`);
  const doc = globalThisDoc(auto);
  // scope gone -> the whole batch is refused, and the same-named field outside it is untouched
  const miss = await auto.invoke('fill', { within: '#gone', fields: [{ target: '#outside', value: 'LEAKED' }] });
  assert.equal(miss.ok, false);
  assert.equal(doc.getElementById('outside').value, 'keep', 'the out-of-scope field was not filled');
  // scope present -> fields resolve inside it
  const hit = await auto.invoke('fill', { within: '#form', fields: [{ target: 'input', value: 'typed' }] });
  assert.equal(hit.ok, true);
  assert.equal(doc.getElementById('inside').value, 'typed');
});

// F: `within: null` means "no scope given" everywhere else in the toolkit
// (resolveScopeInfo treats null and undefined alike), so a field carrying it must
// INHERIT the batch scope — not quietly resolve document-wide and fill the twin outside.
test('a batch fill treats a field within:null as "inherit", not as document-wide', async () => {
  const auto = makeAuto(`
    <input id="outside" type="text" value="keep">
    <div id="form"><input id="inside" type="text" value=""></div>`);
  const doc = globalThisDoc(auto);
  const res = await auto.invoke('fill', {
    within: '#form',
    fields: [{ target: 'input', value: 'typed', within: null }]
  });
  assert.equal(res.ok, true);
  assert.equal(doc.getElementById('inside').value, 'typed', 'the field resolved inside the batch scope');
  assert.equal(doc.getElementById('outside').value, 'keep', 'the document-wide twin was NOT filled');
});

test('wait_check honors within: a missing scope is zero matches, never document-wide', async () => {
  const auto = makeAuto(`
    <div id="panel-1"><span class="spinner">…</span></div>
    <div id="panel-2"></div>`);
  // panel-1's spinner must not satisfy a wait scoped to panel-2
  const scoped = await auto.invoke('wait_check', { selector: '.spinner', within: '#panel-2' });
  assert.equal(scoped.matched, null);
  assert.equal(scoped.selectorMatchCount, 0);
  // an absent container: the wait POLLS, so this is a transient state, not an error
  const absent = await auto.invoke('wait_check', { selector: '.spinner', within: '#gone' });
  assert.equal(absent.matched, null);
  assert.equal(absent.scopeMissing, '#gone');
  assert.equal(absent.error, undefined, 'a not-yet-rendered container is not an error');
  // a gone-wait scoped to the container that still HAS the spinner must not pass
  const stillThere = await auto.invoke('wait_check', { selector: '.spinner', within: '#panel-1', gone: true });
  assert.equal(stillThere.matched, null);
});

// SAFETY (A): "click Submit, then wait for the spinner INSIDE the results panel to be
// gone" must NOT pass before the results panel has even rendered — a vacuous "nothing is
// visible inside a container that isn't there" would hand the next act step a page that
// is still loading (and would make every gone-wait with a typo'd container pass at once).
// waitCheck is ONE-SHOT and has no history, so it reports the miss and background's
// actWait — which knows whether the scope was ever seen during this wait — decides.
test('wait_check: a gone-wait whose scope has NOT RENDERED YET does not pass (no vacuous truth)', async () => {
  const auto = makeAuto(`<button id="go" type="button">Submit</button>`);
  const gone = await auto.invoke('wait_check', { selector: '.spinner', within: '#results', gone: true });
  assert.equal(gone.matched, null, 'a scope that never rendered can never satisfy gone');
  assert.equal(gone.scopeMissing, '#results', 'the missing scope is named for background/the timeout report');
  assert.equal(gone.selectorMatchCount, 0);
  assert.equal(gone.selectorVisible, false);
  assert.equal(gone.error, undefined, 'not an error — the container may still be about to render');

  // the text-shaped gone-wait had the same hole
  const goneText = await auto.invoke('wait_check', { textContains: 'Loading', within: '#results', gone: true });
  assert.equal(goneText.matched, null);
  assert.equal(goneText.scopeMissing, '#results');

  // and the positive (non-gone) wait is unchanged: still unmatched, still polling
  const present = await auto.invoke('wait_check', { selector: '.spinner', within: '#results' });
  assert.equal(present.matched, null);
  assert.equal(present.scopeMissing, '#results');
});

// B: gone was applied to selector/text/url but NOT to the predicate, so a gone-wait
// written as a predicate matched when the predicate was TRUE — the exact inverse.
test('wait_check applies gone to the predicate too', async () => {
  const auto = makeAuto(`<div id="ready" data-state="loading"></div>`);
  const doc = globalThisDoc(auto);
  const pred = 'document.getElementById("ready").dataset.state === "loading"';
  const predicateFn = compiled(auto, pred);

  let r = await auto.invoke('wait_check', { predicate: pred, predicateFn, gone: true });
  assert.equal(r.matched, null, 'predicate still TRUE => the gone-wait must NOT pass');

  doc.getElementById('ready').dataset.state = 'done';
  r = await auto.invoke('wait_check', { predicate: pred, predicateFn, gone: true });
  assert.equal(r.matched, 'predicate', 'predicate now false => gone is satisfied');

  // a throwing predicate is an unknown, not a "gone"
  r = await auto.invoke('wait_check', { predicate: 'nope.not.defined',
                                        predicateFn: compiled(auto, 'nope.not.defined'), gone: true });
  assert.equal(r.matched, null);
  assert.equal(typeof r.predicateError, 'string');
});

// F3 (the contract the waitCheck comment now states): `within` scopes the SELECTOR and
// TEXT conditions only — urlContains/predicate are page-global and cannot be scoped.
test('within scopes the selector/text conditions only — url and predicate stay page-global', async () => {
  const auto = makeAuto(`<div id="panel"></div><div id="ready" data-state="done"></div>`);
  // #ready is OUTSIDE #panel, yet a scoped predicate/url wait still sees the whole page
  const pred = 'document.getElementById("ready").dataset.state === "done"';
  let r = await auto.invoke('wait_check', {
    predicate: pred, predicateFn: compiled(auto, pred), within: '#panel'
  });
  assert.equal(r.matched, 'predicate');
  r = await auto.invoke('wait_check', { urlContains: '/test', within: '#panel' });
  assert.equal(r.matched, 'url');
});

// Finding 2: a wait target given as a ref or accessible-name must track the real node,
// not become a valid-but-unmatched <e7>/<Saving> type selector that a gone-wait passes on.
test('wait_check gone: a ref target tracks the ref, not a bogus <e7> selector', async () => {
  const auto = makeAuto(`<button id="save" type="button">Save</button>`);
  const snap = await auto.invoke('snapshot', {});
  const ref = snap.elements.find(e => e.selector === '#save').ref;
  const still = await auto.invoke('wait_check', { selector: ref, gone: true });
  assert.equal(still.matched, null, 'the ref is live — a gone-wait must not vacuously pass');
  assert.equal(still.selectorMatchCount, 1);
  const doc = globalThisDoc(auto);
  doc.getElementById('save').remove();
  const gone = await auto.invoke('wait_check', { selector: ref, gone: true });
  assert.equal(gone.matched, 'selector', 'once the ref is detached, gone is satisfied');
});

test('wait_check gone: an accessible-name target tracks that node, not a bogus type selector', async () => {
  const auto = makeAuto(`<button id="b" type="button">Saving changes</button>`);
  const still = await auto.invoke('wait_check', { selector: 'Saving changes', gone: true });
  assert.equal(still.matched, null, 'the named button is present — gone must not pass');
  assert.equal(still.selectorMatchCount, 1);
});

// Finding 3b: within scopes selector/text only; url/predicate are page-global and must
// STILL be evaluated when the scoped container is absent.
test('wait_check: a missing within does not suppress the page-global url/predicate conditions', async () => {
  const auto = makeAuto(`<div id="present"></div>`);
  const url = await auto.invoke('wait_check', { urlContains: '/test', within: '#missing' });
  assert.equal(url.matched, 'url', 'a page-global url wait is not suppressed by a missing scope');
  assert.equal(url.scopeMissing, '#missing', 'the missing scope is still reported alongside');
  const pred = await auto.invoke('wait_check', {
    predicate: '1 + 1 === 2', predicateFn: compiled(auto, '1 + 1 === 2'), within: '#missing'
  });
  assert.equal(pred.matched, 'predicate');
});

test('disabled twin: click prefers the actionable match; enabled_only excludes disabled', async () => {
  const auto = makeAuto(`
    <button id="d" type="button" disabled>Submit</button>
    <button id="en" type="button">Submit</button>`);
  // even WITHOUT enabled_only, click picks the enabled candidate among matches
  const res = await auto.invoke('click', { target: 'Submit' });
  assert.equal(res.ok, true);
  assert.equal(res.target.selector, '#en');
  // WITH enabled_only, resolution itself must drop the disabled twin
  const els = auto.resolveAll({ target: 'Submit', enabled_only: true });
  assert.equal(els.length, 1);
  assert.equal(els[0].id, 'en');
  // targeting the disabled one directly reports a structured error
  const dis = await auto.invoke('click', { target: '#d' });
  assert.equal(dis.ok, false);
  assert.match(dis.error, /disabled/i);
});

// ---- click result envelope ----------------------------------------------------

test('click dispatches a real click and reports the outcome envelope', async () => {
  const auto = makeAuto(`<button id="b" type="button">Go</button><span id="out"></span>`);
  const doc = globalThisDoc(auto);
  doc.getElementById('b').addEventListener('click', () => {
    doc.getElementById('out').textContent = 'clicked';
  });
  const res = await auto.invoke('click', { selector: '#b' });
  assert.equal(res.ok, true);
  assert.equal(res.clicked, true);
  assert.equal(doc.getElementById('out').textContent, 'clicked');
  assert.equal(res.domChanged, true, 'mutation observer saw the text change');
  assert.equal(res.dialogOpened, false);
  assert.equal(res.urlChanged, false);
});

test('click reports dialogOpened when a modal appears', async () => {
  const auto = makeAuto(`<button id="b" type="button">Open</button><div id="host"></div>`);
  const doc = globalThisDoc(auto);
  doc.getElementById('b').addEventListener('click', () => {
    const d = doc.createElement('div');
    d.setAttribute('role', 'dialog');
    doc.getElementById('host').appendChild(d);
  });
  const res = await auto.invoke('click', { target: '#b' });
  assert.equal(res.ok, true);
  assert.equal(res.dialogOpened, true);
  assert.equal(res.domChanged, true);
});

test('click by text+role targets the visible twin, not the hidden one', async () => {
  const auto = makeAuto(`
    <button id="ghost" type="button" style="display:none">Save</button>
    <button id="real" type="button">Save</button>
    <span id="out"></span>`);
  const doc = globalThisDoc(auto);
  doc.getElementById('real').addEventListener('click', () => { doc.getElementById('out').textContent = 'real'; });
  const res = await auto.invoke('click', { text: 'Save', role: 'button' });
  assert.equal(res.ok, true);
  assert.equal(res.target.selector, '#real');
  assert.equal(doc.getElementById('out').textContent, 'real');
});

test('click with empty text matches nothing (no accidental click)', async () => {
  const auto = makeAuto(`<button id="b" type="button">Go</button>`);
  const res = await auto.invoke('click', { text: '' });
  assert.equal(res.ok, false);
  assert.match(res.error, /no element/i);
});

// ---- fill ----------------------------------------------------------------------

test('fill uses the native setter and fires input (reactive mirror updates)', async () => {
  const auto = makeAuto(`<input id="r" type="text"><span id="m"></span>`);
  // emulate a controlled input: mirror updates ONLY on a real 'input' event
  const doc = globalThisDoc(auto);
  const input = doc.getElementById('r');
  input.addEventListener('input', () => { doc.getElementById('m').textContent = input.value; });

  // v2: a single field returns the result object directly (no {results:[...]})
  const res = await auto.invoke('fill', { selector: '#r', value: 'hello' });
  assert.equal(res.ok, true);
  assert.equal(res.confirmedValue, 'hello');
  assert.equal(doc.getElementById('m').textContent, 'hello');
});

test('fill with multiple fields returns the aggregate envelope', async () => {
  const auto = makeAuto(`<input id="a" type="text"><input id="b" type="text">`);
  const res = await auto.invoke('fill', {
    fields: [
      { selector: '#a', value: 'one' },
      { selector: '#b', value: 'two' }
    ]
  });
  assert.equal(res.ok, true);
  assert.equal(res.filled, 2);
  assert.equal(res.failed, 0);
  sameShape(res.results.map(r => r.confirmedValue), ['one', 'two']);
});

test('fill without a value reports an error (no silent "null")', async () => {
  const auto = makeAuto(`<input id="x" type="text">`);
  const res = await auto.invoke('fill', { selector: '#x' });
  assert.equal(res.ok, false);
  assert.match(res.error, /no value/i);
});

test('fill on a <select> routes to select() and fires change', async () => {
  // v1 refused with a "use page_select" hint; v2 just does the right thing
  const auto = makeAuto(`<select id="s"><option value="1">Red</option><option value="2">Green</option></select>`);
  const doc = globalThisDoc(auto);
  let changed = false;
  doc.getElementById('s').addEventListener('change', () => { changed = true; });
  const res = await auto.invoke('fill', { selector: '#s', value: 'Green' });
  assert.equal(res.ok, true);
  assert.equal(res.selectedValue, '2', 'label-matched because "Green" is not an option value');
  assert.equal(res.selectedLabel, 'Green');
  assert.equal(changed, true);
});

test('check (fill alias) toggles a checkbox and confirms the state', async () => {
  const auto = makeAuto(`<input id="cb" type="checkbox">`);
  const res = await auto.invoke('check', { selector: '#cb', value: true });
  assert.equal(res.ok, true);
  assert.equal(res.confirmedValue, true);
  const doc = globalThisDoc(auto);
  assert.equal(doc.getElementById('cb').checked, true);
});

// ---- fill read-back verification: no false success ------------------------------

test('fill reports "value did not take" when the framework reverts the write', async () => {
  const auto = makeAuto(`<input id="cf" type="text" value="orig">`);
  const doc = globalThisDoc(auto);
  const el = doc.getElementById('cf');
  // emulate a Vueform-style controlled input: a synthetic (untrusted) edit is
  // overwritten from the bound model on the next tick
  el.addEventListener('input', (e) => {
    if (!e.isTrusted) setTimeout(() => { el.value = 'orig'; }, 0);
  });
  const res = await auto.invoke('fill', { selector: '#cf', value: 'new value' });
  assert.equal(res.ok, false, 'a reverted write must NOT be reported as success');
  assert.equal(res.valueReverted, true);
  assert.match(res.error, /did not take/i);
  assert.match(res.error, /"orig"/, 'names the value the field reverted to');
});

test('fill stays ok when the page transforms the value (formatter, not a revert)', async () => {
  const auto = makeAuto(`<input id="up" type="text" value="">`);
  const doc = globalThisDoc(auto);
  const el = doc.getElementById('up');
  el.addEventListener('input', () => { el.value = el.value.toUpperCase(); });
  const res = await auto.invoke('fill', { selector: '#up', value: 'abc' });
  assert.equal(res.ok, true, 'a transformed value is a commit, just not verbatim');
  assert.equal(res.confirmedValue, 'ABC', 'confirmedValue reports the post-transform value');
});

test('check reports "value did not take" when a checkbox is forced back', async () => {
  const auto = makeAuto(`<input id="rb" type="checkbox">`);
  const doc = globalThisDoc(auto);
  const el = doc.getElementById('rb');
  el.addEventListener('change', (e) => {
    if (!e.isTrusted) setTimeout(() => { el.checked = false; }, 0);
  });
  const res = await auto.invoke('check', { selector: '#rb', value: true });
  assert.equal(res.ok, false);
  assert.equal(res.valueReverted, true);
});

test('select reports "value did not take" when the selection is reverted', async () => {
  const auto = makeAuto(`<select id="rs"><option value="1" selected>Red</option><option value="2">Green</option></select>`);
  const doc = globalThisDoc(auto);
  const el = doc.getElementById('rs');
  el.addEventListener('change', (e) => {
    if (!e.isTrusted) setTimeout(() => { el.value = '1'; }, 0);
  });
  const res = await auto.invoke('select', { selector: '#rs', value: 'Green' });
  assert.equal(res.ok, false);
  assert.equal(res.valueReverted, true);
  assert.match(res.error, /did not take/i);
});

test('filling a field with its existing value is a commit, not a revert', async () => {
  const auto = makeAuto(`<input id="same" type="text" value="keep">`);
  const res = await auto.invoke('fill', { selector: '#same', value: 'keep' });
  assert.equal(res.ok, true);
  assert.equal(res.confirmedValue, 'keep');
});

// ---- stale element-ref message --------------------------------------------------

test('a ref-shaped target absent from refs returns a DISTINCT stale-ref message', async () => {
  const auto = makeAuto(`<button id="b">Go</button>`);
  // "e999" was never minted in this document (mirrors a ref reused after navigation,
  // which wipes refs). Both click and fill must say so distinctly — not "not found".
  const click = await auto.invoke('click', { target: 'e999' });
  assert.equal(click.ok, false);
  assert.match(click.error, /stale ref/i);
  assert.match(click.error, /page_snapshot|re-run|re-snapshot/i);

  const fill = await auto.invoke('fill', { target: 'e999', value: 'x' });
  assert.equal(fill.ok, false);
  assert.match(fill.error, /stale ref/i);
});

test('a detached minted ref reports "removed from the page" (not generic not-found)', async () => {
  const auto = makeAuto(`<button id="b">Go</button>`);
  const snap = await auto.invoke('snapshot', {});
  const ref = snap.elements.find(e => e.role === 'button').ref;
  // Remove the element: the ref exists in the map but its node is detached.
  const doc = globalThisDoc(auto);
  doc.getElementById('b').remove();
  const res = await auto.invoke('click', { target: ref });
  assert.equal(res.ok, false);
  assert.match(res.error, /stale ref|removed from the page/i);
});

test('a genuinely-wrong CSS selector still returns the generic not-found (not stale-ref)', async () => {
  const auto = makeAuto(`<button id="b">Go</button>`);
  const res = await auto.invoke('click', { target: '#nope' });
  assert.equal(res.ok, false);
  assert.doesNotMatch(res.error, /stale ref/i);
  assert.match(res.error, /no element matched|not found/i);
});

// ---- rect (element-screenshot box) ----------------------------------------------

test('rect resolves one element and returns page/viewport boxes', async () => {
  const auto = makeAuto(`<div id="target">box</div>`);
  const r = await auto.invoke('rect', { target: '#target' });
  assert.equal(r.ok, true);
  assert.equal(r.matchCount, 1);
  assert.equal(typeof r.page.w, 'number');
  assert.equal(typeof r.page.h, 'number');
  assert.ok(r.page.w >= 1 && r.page.h >= 1, 'box has a positive size');
  assert.equal(typeof r.dpr, 'number');
});

test('rect on multiple matches errors with the count (no silent pick)', async () => {
  const auto = makeAuto(`<div class="dup">a</div><div class="dup">b</div>`);
  const r = await auto.invoke('rect', { target: '.dup' });
  assert.equal(r.ok, false);
  assert.equal(r.matchCount, 2);
  assert.match(r.error, /matched 2|more specific|nth/i);
});

test('rect on zero matches errors with matchCount 0', async () => {
  const auto = makeAuto(`<div id="x">a</div>`);
  const r = await auto.invoke('rect', { target: '#nope' });
  assert.equal(r.ok, false);
  assert.equal(r.matchCount, 0);
});

// ---- select --------------------------------------------------------------------

test('select picks an option by label and fires change', async () => {
  const auto = makeAuto(`<select id="s"><option value="">--</option><option value="g">Green</option></select>`);
  const doc = globalThisDoc(auto);
  let changed = false;
  doc.getElementById('s').addEventListener('change', () => { changed = true; });
  const res = await auto.invoke('select', { selector: '#s', label: 'Green' });
  assert.equal(res.ok, true);
  assert.equal(res.selectedValue, 'g');
  assert.equal(changed, true);
});

test('select falls back to label-matching args.value when no label given', async () => {
  const auto = makeAuto(`<select id="s"><option value="1">Red</option><option value="2">Green</option></select>`);
  const res = await auto.invoke('select', { selector: '#s', value: 'Green' });
  assert.equal(res.ok, true);
  assert.equal(res.selectedValue, '2');
  assert.equal(res.selectedLabel, 'Green');
  // a value that matches neither option values nor labels lists what IS available
  const miss = await auto.invoke('select', { selector: '#s', value: 'Purple' });
  assert.equal(miss.ok, false);
  assert.match(miss.error, /option not found/i);
  sameShape(miss.available, ['Red', 'Green']);
});

test('select chooses by exact option value (not label) and echoes the full envelope', async () => {
  const auto = makeAuto(`<select id="s"><option value="r">Red</option><option value="g">Green</option></select>`);
  const res = await auto.invoke('select', { selector: '#s', value: 'g' });
  assert.equal(res.ok, true);
  assert.equal(res.selectedValue, 'g', 'matched the option whose VALUE is "g"');
  assert.equal(res.selectedLabel, 'Green');
  assert.ok(res.target && res.target.ref, 'envelope carries the resolved target');
});

// ---- press ---------------------------------------------------------------------

test('press dispatches keydown/keyup with the requested key', async () => {
  const auto = makeAuto(`<input id="inp" type="text">`);
  const doc = globalThisDoc(auto);
  const events = [];
  const inp = doc.getElementById('inp');
  for (const t of ['keydown', 'keypress', 'keyup']) {
    inp.addEventListener(t, e => events.push([t, e.key]));
  }
  const res = await auto.invoke('press', { target: '#inp', key: 'Enter' });
  assert.equal(res.ok, true);
  assert.equal(res.pressed, 'Enter');
  // multi-char keys get keydown+keyup only (no keypress)
  assert.deepEqual(events, [['keydown', 'Enter'], ['keyup', 'Enter']]);

  events.length = 0;
  const res2 = await auto.invoke('press', { target: '#inp', key: 'a' });
  assert.equal(res2.ok, true);
  assert.deepEqual(events, [['keydown', 'a'], ['keypress', 'a'], ['keyup', 'a']]);
});

test('press without a key reports a structured error', async () => {
  const auto = makeAuto(`<input id="inp">`);
  const res = await auto.invoke('press', {});
  assert.equal(res.ok, false);
  assert.match(res.error, /no key/i);
});

test('press takes the key from args.value (act steps carry it as value)', async () => {
  const auto = makeAuto(`<input id="inp" type="text">`);
  const doc = globalThisDoc(auto);
  const seen = [];
  doc.getElementById('inp').addEventListener('keydown', e => seen.push(e.key));
  const res = await auto.invoke('press', { target: '#inp', value: 'Escape' });
  assert.equal(res.ok, true);
  assert.equal(res.pressed, 'Escape');
  sameShape(seen, ['Escape']);
});

test('press with no target dispatches on the active element (defaults to body)', async () => {
  const auto = makeAuto(`<p>page</p>`);
  const doc = globalThisDoc(auto);
  const seen = [];
  // no focusable target focused -> activeElement is <body>; the keydown bubbles
  doc.addEventListener('keydown', e => seen.push(e.key));
  const res = await auto.invoke('press', { key: 'Enter' });
  assert.equal(res.ok, true);
  assert.equal(res.pressed, 'Enter');
  sameShape(seen, ['Enter']);
});

// ---- scroll --------------------------------------------------------------------
// jsdom has no real scrolling: scrollY stays 0 and scrollTo/scrollBy are stubbed
// in makeAuto. We assert the return SHAPE and which scroll call was requested.

test('scroll handles top/bottom/number and scroll-to-element', async () => {
  const auto = makeAuto(`<div style="height:5000px"></div><button id="end" type="button">End</button>`);
  const win = winOf(auto);

  let res = await auto.invoke('scroll', { value: 'top' });
  assert.equal(res.ok, true);
  assert.equal(typeof res.scrollY, 'number');
  assert.equal(typeof res.maxScrollY, 'number');
  assert.deepEqual(win.__scrollCalls.at(-1), ['to', 0, 0]);

  res = await auto.invoke('scroll', { value: 'bottom' });
  assert.equal(res.ok, true);
  assert.equal(win.__scrollCalls.at(-1)[0], 'to');

  res = await auto.invoke('scroll', { value: 500 });
  assert.equal(res.ok, true);
  assert.deepEqual(win.__scrollCalls.at(-1), ['to', 0, 500]);

  // no value at all: defaults to scrolling one viewport down via scrollBy
  res = await auto.invoke('scroll', {});
  assert.equal(res.ok, true);
  assert.equal(win.__scrollCalls.at(-1)[0], 'by');

  // scroll to a target returns the element description instead
  res = await auto.invoke('scroll', { target: '#end' });
  assert.equal(res.ok, true);
  assert.equal(res.scrolledTo.selector, '#end');
});

test('scroll by a delta uses scrollBy', async () => {
  const auto = makeAuto(`<div style="height:5000px"></div>`);
  const win = winOf(auto);
  const res = await auto.invoke('scroll', { by: 300 });
  assert.equal(res.ok, true);
  assert.equal(typeof res.scrollY, 'number');
  assert.deepEqual(win.__scrollCalls.at(-1), ['by', 0, 300]);
});

// C: a `within` that FAILS to resolve is refused (above), so a `within` that RESOLVES
// must not be silently dropped and the whole window scrolled instead — the caller named
// a scrollable panel/modal, and moving the viewport leaves that panel where it was.
test('scroll with a RESOLVED within and no target scrolls THAT CONTAINER, not the window', async () => {
  const auto = makeAuto(`<div id="panel"><div style="height:5000px"></div></div>`);
  const win = winOf(auto);
  const doc = globalThisDoc(auto);
  const panel = doc.getElementById('panel');
  // jsdom implements no element scrolling: back scrollTop with a real property and give
  // the panel a scrollable geometry (own props shadow jsdom's always-0 prototype getters).
  let top = 0;
  Object.defineProperty(panel, 'scrollTop', { get: () => top, set: (v) => { top = v; }, configurable: true });
  Object.defineProperty(panel, 'scrollHeight', { get: () => 1000, configurable: true });
  Object.defineProperty(panel, 'clientHeight', { get: () => 200, configurable: true });

  let res = await auto.invoke('scroll', { to: 'bottom', within: '#panel' });
  assert.equal(res.ok, true);
  assert.equal(res.scrolledContainer.selector, '#panel', 'reports WHAT it scrolled');
  assert.equal(res.maxScrollTop, 800, 'scrollHeight - clientHeight');
  assert.equal(res.scrollTop, 800);
  assert.equal(panel.scrollTop, 800);
  assert.equal(res.scrollY, undefined, 'the window scroll position is not what moved');
  assert.equal(win.__scrollCalls.length, 0, 'the WINDOW must not be scrolled');

  res = await auto.invoke('scroll', { to: 'top', within: '#panel' });
  assert.equal(panel.scrollTop, 0);

  res = await auto.invoke('scroll', { by: 120, within: '#panel' });
  assert.equal(panel.scrollTop, 120);

  res = await auto.invoke('scroll', { value: 300, within: '#panel' }); // act steps carry `to` as `value`
  assert.equal(panel.scrollTop, 300);

  // no `to`/`by`: one container-viewport step (0.8 * clientHeight), from where it is
  res = await auto.invoke('scroll', { within: '#panel' });
  assert.equal(panel.scrollTop, 460);
  assert.equal(win.__scrollCalls.length, 0, 'still no window scroll on any container path');

  // a target INSIDE the scope keeps the scrollIntoView behavior
  res = await auto.invoke('scroll', { target: 'div', within: '#panel' });
  assert.equal(res.ok, true);
  assert.ok(res.scrolledTo, 'a targeted scroll still reports the element');

  // and with NO within the window path is untouched
  res = await auto.invoke('scroll', { to: 'top' });
  assert.equal(res.ok, true);
  assert.equal(typeof res.scrollY, 'number');
  assert.deepEqual(win.__scrollCalls.at(-1), ['to', 0, 0]);
});

// ---- wait_check (one-shot; background drives the polling loop) ------------------

test('wait_check matches a present selector and reports null when absent', async () => {
  const auto = makeAuto(`<div id="present"></div>`);
  let r = await auto.invoke('wait_check', { selector: '#present' });
  assert.equal(r.matched, 'selector');
  assert.equal(typeof r.url, 'string');
  assert.equal(typeof r.readyState, 'string');

  r = await auto.invoke('wait_check', { selector: '#absent' });
  assert.equal(r.matched, null);
});

test('wait_check gone:true inverts the condition', async () => {
  const auto = makeAuto(`<div id="present"></div>`);
  let r = await auto.invoke('wait_check', { selector: '#absent', gone: true });
  assert.equal(r.matched, 'selector', 'absent + gone => satisfied');
  r = await auto.invoke('wait_check', { selector: '#present', gone: true });
  assert.equal(r.matched, null, 'still present => not gone yet');
});

test('wait_check requireVisible treats a display:none element as absent', async () => {
  const auto = makeAuto(`<div id="spinner" style="display:none"></div>`);
  let r = await auto.invoke('wait_check', { selector: '#spinner', requireVisible: true });
  assert.equal(r.matched, null);
  r = await auto.invoke('wait_check', { selector: '#spinner', requireVisible: true, gone: true });
  assert.equal(r.matched, 'selector', 'hidden counts as gone when visibility is required');
});

// SAFETY: requireVisible is a property of the MATCH SET. A hidden template/stale
// twin ahead of the live element must not time the wait out — nor, inverted with
// gone:true, declare a still-visible modal dismissed (the next act step would then
// fire underneath it). This check is what background's actWait re-injects every 250ms.
test('wait_check requireVisible scans ALL matches, not just the first', async () => {
  const auto = makeAuto(`
    <div class="toast" style="display:none">template</div>
    <div class="toast">Saved</div>`);
  const r = await auto.invoke('wait_check', { selector: '.toast', requireVisible: true });
  assert.equal(r.matched, 'selector', 'a visible match further down the list satisfies the wait');
  assert.equal(r.selectorMatchCount, 2);
  assert.equal(r.selectorVisible, true, 'selectorVisible reports whether ANY match is visible');
});

test('wait_check gone+requireVisible does not pass while a visible match remains', async () => {
  const auto = makeAuto(`
    <div class="modal" style="display:none"><button>ghost OK</button></div>
    <div class="modal"><button>OK</button></div>`);
  const doc = globalThisDoc(auto);
  let r = await auto.invoke('wait_check', { selector: '.modal', requireVisible: true, gone: true });
  assert.equal(r.matched, null, 'the modal is still on screen — the wait must NOT pass');
  assert.equal(r.selectorMatchCount, 2);
  assert.equal(r.selectorVisible, true);

  // dismissed: the hidden twin stays in the DOM, so only "no VISIBLE match" can mean gone
  doc.querySelectorAll('.modal')[1].style.display = 'none';
  r = await auto.invoke('wait_check', { selector: '.modal', requireVisible: true, gone: true });
  assert.equal(r.matched, 'selector', 'no visible match remains => gone');
  assert.equal(r.selectorMatchCount, 2, 'both are still in the DOM — the count stays honest');
  assert.equal(r.selectorVisible, false);
});

test('wait_check invalid selector returns a structured error', async () => {
  const auto = makeAuto(`<div></div>`);
  const r = await auto.invoke('wait_check', { selector: '??bad??' });
  assert.equal(r.matched, null);
  assert.match(r.error, /invalid selector/i);
});

// D: the legacy in-page wait polls waitCheck. A malformed `within`/selector can NEVER
// start matching, so polling it to the deadline turns a fixable, self-describing error
// into a bare TIMEOUT — the one report that tells the agent to just wait longer.
test('wait_for surfaces waitCheck errors at once instead of polling to a timeout', async () => {
  const auto = makeAuto(`<div id="x"></div>`);
  const t0 = Date.now();
  const scope = await auto.invoke('wait_for', { selector: '#x', within: 'div[class=', timeoutMs: 3000 });
  assert.equal(scope.ok, false);
  assert.equal(scope.timedOut, undefined, 'a selector that can never parse is not a timeout');
  assert.match(scope.error, /invalid CSS selector/i);
  assert.ok(Date.now() - t0 < 1500, 'reported immediately, not after polling to the deadline');

  const bad = await auto.invoke('wait_for', { selector: '??bad??', timeoutMs: 3000 });
  assert.equal(bad.ok, false);
  assert.equal(bad.timedOut, undefined);
  assert.match(bad.error, /invalid selector/i);

  // a genuine timeout still reports as a timeout (with no error)
  const t1 = Date.now();
  const out = await auto.invoke('wait_for', { selector: '#never', timeoutMs: 150, intervalMs: 30 });
  assert.equal(out.ok, false);
  assert.equal(out.timedOut, true);
  assert.ok(Date.now() - t1 >= 140, 'a real timeout waits the full deadline');
});

// R2: a selector check reports HOW it failed — match count + visibility of the
// first match — so a timed-out wait can distinguish "never appeared" from
// "present but hidden" without an extra snapshot round trip.
test('wait_check reports selectorMatchCount and selectorVisible diagnostics', async () => {
  const auto = makeAuto(`
    <div class="modal" style="display:none"><button>OK</button></div>
    <button id="shown">Go</button>
  `);
  // zero matches
  let r = await auto.invoke('wait_check', { selector: '#absent' });
  assert.equal(r.matched, null);
  assert.equal(r.selectorMatchCount, 0);
  assert.equal(r.selectorVisible, false);

  // present but hidden, visibility required -> no match, but the count says it exists
  r = await auto.invoke('wait_check', { selector: '.modal button', requireVisible: true });
  assert.equal(r.matched, null);
  assert.equal(r.selectorMatchCount, 1);
  assert.equal(r.selectorVisible, false);

  // present and visible -> matched, diagnostics still attached
  r = await auto.invoke('wait_check', { selector: '#shown', requireVisible: true });
  assert.equal(r.matched, 'selector');
  assert.equal(r.selectorMatchCount, 1);
  assert.equal(r.selectorVisible, true);
});

test('wait_check diagnostics: multiple matches counted; gone-wait keeps the count', async () => {
  const auto = makeAuto(`<li class="row"></li><li class="row"></li><li class="row"></li>`);
  let r = await auto.invoke('wait_check', { selector: '.row' });
  assert.equal(r.matched, 'selector');
  assert.equal(r.selectorMatchCount, 3);

  // gone-wait on a still-present selector: not matched, count says why
  r = await auto.invoke('wait_check', { selector: '.row', gone: true });
  assert.equal(r.matched, null);
  assert.equal(r.selectorMatchCount, 3);
});

// A step can carry BOTH `target` and `text` (the harness/value-alias path) —
// `target` must win, or the accessible-name field hijacks resolution and a
// perfectly good CSS target reports "not found".
test('resolveAll: target outranks text when both are present', async () => {
  const auto = makeAuto(`<input id="reactive" type="text"><button>via-text-alias</button>`);
  const both = auto.resolveAll({ target: '#reactive', text: 'via-text-alias' });
  assert.equal(both.length, 1);
  assert.equal(both[0].id, 'reactive');
  // text-only targeting still resolves by accessible name
  const textOnly = auto.resolveAll({ text: 'via-text-alias' });
  assert.equal(textOnly.length, 1);
  assert.equal(textOnly[0].tagName, 'BUTTON');
});

test('wait_check without a selector carries no selector diagnostics', async () => {
  const auto = makeAuto(`<div></div>`);
  const r = await auto.invoke('wait_check', { urlContains: '/nope' });
  assert.equal(r.matched, null);
  assert.ok(!('selectorMatchCount' in r), 'no selectorMatchCount for non-selector waits');
  assert.ok(!('selectorVisible' in r), 'no selectorVisible for non-selector waits');
});

test('wait_check matches textContains against the body text', async () => {
  // automation.js reads document.body.innerText, which jsdom does NOT implement
  // (it only models textContent). Define innerText on the body so the indexOf
  // matching logic is genuinely exercised here.
  const auto = makeAuto(`<p>placeholder</p>`);
  const doc = globalThisDoc(auto);
  Object.defineProperty(doc.body, 'innerText', {
    get() { return 'Order #42 shipped'; }, configurable: true
  });
  let r = await auto.invoke('wait_check', { textContains: 'shipped' });
  assert.equal(r.matched, 'text');
  r = await auto.invoke('wait_check', { textContains: 'cancelled' });
  assert.equal(r.matched, null);
});

test('wait_check matches urlContains against location.href', async () => {
  // makeAuto sets url http://localhost/test
  const auto = makeAuto(`<div></div>`);
  let r = await auto.invoke('wait_check', { urlContains: '/test' });
  assert.equal(r.matched, 'url');
  r = await auto.invoke('wait_check', { urlContains: '/nope' });
  assert.equal(r.matched, null);
});

test('wait_check runs a compiled predicate and surfaces predicateError', async () => {
  const auto = makeAuto(`<div id="ready" data-state="done"></div>`);
  const pred = 'document.getElementById("ready").dataset.state === "done"';
  let r = await auto.invoke('wait_check', { predicate: pred, predicateFn: compiled(auto, pred) });
  assert.equal(r.matched, 'predicate');
  assert.equal(r.predicateError, null);

  // false predicate -> no match, no error
  r = await auto.invoke('wait_check', { predicate: '1 === 2', predicateFn: compiled(auto, '1 === 2') });
  assert.equal(r.matched, null);
  assert.equal(r.predicateError, null);

  // a throwing predicate is caught and reported, never matched
  r = await auto.invoke('wait_check', { predicate: 'nope.not.defined',
                                        predicateFn: compiled(auto, 'nope.not.defined') });
  assert.equal(r.matched, null);
  assert.equal(typeof r.predicateError, 'string');
});

// The toolkit is a SHARED content file that AMO lints: an eval( call in it is reported as
// DANGEROUS_EVAL on every submission. Predicates arrive compiled by the background, and a
// string that arrives uncompiled is reported, never run.
test('wait_check never evaluates a predicate STRING: an uncompiled one is reported, not run', async () => {
  const auto = makeAuto(`<div></div>`);
  const r = await auto.invoke('wait_check', { predicate: '1 === 1' });
  assert.equal(r.matched, null, 'a true expression with no predicateFn must not match');
  assert.match(r.predicateError, /not compiled/);
  assert.equal(/\beval\s*\(/.test(AUTOMATION_SRC), false,
    'automation.js carries an eval( call again (addons-linter DANGEROUS_EVAL)');
});

test('wait_check fails outright on a predicate that does not parse, when it is the only condition', async () => {
  const auto = makeAuto(`<div id="x"></div>`);
  // Both engines surface a parse failure as a SyntaxError thrown by predicateFn (gecko's
  // inlined (0, eval), chromium's replayed user-script outcome).
  const predicateFn = () => { throw new (winOf(auto).SyntaxError)('unexpected token'); };
  let r = await auto.invoke('wait_check', { predicate: '1 +', predicateFn });
  assert.match(r.error, /^predicate is not valid JavaScript: unexpected token/);
  assert.equal(r.matched, undefined, 'a parse failure is an error, not a poll result');

  // Beside another condition it is reported and the other condition still decides.
  r = await auto.invoke('wait_check', { predicate: '1 +', predicateFn, selector: '#x' });
  assert.equal(r.matched, 'selector');
  r = await auto.invoke('wait_check', { predicate: '1 +', predicateFn, selector: '#absent' });
  assert.equal(r.error, undefined);
  assert.equal(r.matched, null);
  assert.match(r.predicateError, /^predicate is not valid JavaScript/);
});

// ---- inspect (watch targets) -----------------------------------------------------

test('inspect reports found/visible/enabled/value per watch target', async () => {
  const auto = makeAuto(`
    <button id="sub" type="button" disabled>Submit</button>
    <input id="q" type="text" value="hi">`);
  const res = await auto.invoke('inspect', { watch: ['#sub', '#q', '#missing'] });
  assert.equal(res.ok, true);
  assert.equal(res.watched.length, 3);

  const [sub, q, missing] = res.watched;
  assert.equal(sub.found, true);
  assert.equal(sub.enabled, false);
  assert.equal(sub.visible, true);
  assert.equal(q.found, true);
  assert.equal(q.value, 'hi');
  assert.equal(missing.found, false);
  assert.equal(missing.watch, '#missing');
});

test('inspect accepts {target} object watch items (with disambiguators) and resolves by accessible name', async () => {
  const auto = makeAuto(`
    <button id="d" type="button" disabled>Save</button>
    <button id="en" type="button">Save</button>`);
  const res = await auto.invoke('inspect', {
    // object form + enabled_only must pick the enabled twin; plain text uses {target}
    watch: [{ target: 'Save', enabled_only: true }, { target: 'Nothing here' }]
  });
  assert.equal(res.ok, true);
  assert.equal(res.watched.length, 2);
  const [save, none] = res.watched;
  assert.equal(save.found, true);
  assert.equal(save.enabled, true, 'enabled_only dropped the disabled twin');
  assert.equal(save.ref && save.ref.length > 0, true);
  assert.equal(none.found, false);
});

// ---- pageState ---------------------------------------------------------------------
// jsdom returns zero rects only for hidden elements (our stub); a visible body
// measures 10x10, so painted should be true. We assert SHAPE plus the
// hiddenByCss path — real 0x0-unpainted layout can't be reproduced in jsdom.

test('page_state reports paint diagnostics; hiddenByCss reflects display:none body', async () => {
  const auto = makeAuto(`<p>content</p>`);
  let res = await auto.invoke('page_state', {});
  assert.equal(res.ok, true);
  const ps = res.pageState;
  assert.equal(typeof ps.url, 'string');
  assert.equal(typeof ps.title, 'string');
  assert.equal(typeof ps.readyState, 'string');
  assert.equal(typeof ps.focused, 'boolean');
  assert.equal(typeof ps.visibility, 'string');
  assert.equal(ps.hiddenByCss, false);
  assert.equal(ps.painted, true, 'visible body measures non-zero in our layout stub');
  assert.equal(ps.warning, undefined);

  // anti-FOUC guard: DOM intact but body display:none
  globalThisDoc(auto).body.style.display = 'none';
  res = await auto.invoke('page_state', {});
  assert.equal(res.pageState.hiddenByCss, true);
  assert.equal(res.pageState.painted, false);
  assert.match(res.pageState.warning, /hidden by CSS/i);
});

// ---- dispatcher / regressions -----------------------------------------------------

test('unknown method returns a structured error', async () => {
  const auto = makeAuto(`<div></div>`);
  const res = await auto.invoke('nope', {});
  assert.equal(res.ok, false);
  assert.match(res.error, /unknown automation method/);
});

// F: the dispatcher's try/catch is what guarantees the {ok:false,error} envelope the
// whole protocol reads. `return promise` (no await) leaves the try scope before the
// promise settles, so a rejection escapes invoke entirely and background gets a raw
// executeScript failure instead of a structured result.
test('invoke wraps a REJECTED fill/select/check in the {ok:false} envelope', async () => {
  // `fields` is the batch shape; a non-array makes fill() itself throw (async => reject)
  for (const method of ['fill', 'check']) {
    const auto = makeAuto(`<input id="q" type="text">`);
    const res = await auto.invoke(method, { fields: 'not-an-array' });
    assert.equal(res.ok, false, method + ' must come back as a structured error');
    assert.equal(typeof res.error, 'string');
  }

  const auto = makeAuto(`<select id="s"><option value="1">Red</option><option value="2">Green</option></select>`);
  const el = globalThisDoc(auto).getElementById('s');
  Object.defineProperty(el, 'options', { get() { throw new Error('boom'); }, configurable: true });
  const res = await auto.invoke('select', { selector: '#s', label: 'Green' });
  assert.equal(res.ok, false);
  assert.match(res.error, /boom/);
});

test('snapshot does not crash when element.title is non-string (SVG-like)', async () => {
  const auto = makeAuto(`<div id="icon" role="button" title="IconBtn"></div>`);
  const doc = globalThisDoc(auto);
  const el = doc.getElementById('icon');
  // emulate an SVGElement whose .title is an SVGAnimatedString (object, not string)
  Object.defineProperty(el, 'title', { get() { return { animVal: 'IconBtn' }; }, configurable: true });
  const snap = await auto.invoke('snapshot', {});
  assert.equal(snap.ok, true);              // would be false (whole snapshot aborts) before the fix
  const item = snap.elements.find(e => e.selector === '#icon');
  assert.ok(item, 'icon button is listed');
  assert.equal(item.name, 'IconBtn');       // name resolved via getAttribute('title')
});
