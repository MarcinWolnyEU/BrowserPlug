/*
 * Page side — an action must report what it CHANGED, in numbers.
 *
 * The failure being fixed: `OK: 1/1 steps completed` answers "did the mechanism run",
 * which is never the question. On euro.com.pl's "Pokaż więcej produktów" the SAME
 * 96-byte success came back for a click that loaded 8 more products and for a click
 * that matched nothing at all — so 64 of 101 page_act calls in the corpus were
 * followed immediately by a probe whose only purpose was to find out which happened.
 * `domChanged: true` cannot close that gap either: on a lazy listing it is true for
 * skeletons, animations and re-renders as much as for real results.
 *
 * The page owns PRIMITIVE MEASUREMENT only — "how many elements are here right now",
 * "how many match this selector right now", "how many nodes did this click add and
 * remove". It cannot own the before/after pairing, because it cannot see across an
 * injection boundary; the background does that (tests/js{,-chromium}/actdelta).
 *
 * automation.js is a [shared] file, so this suite is scope="shared" in parity.toml —
 * one run covers both trees.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from 'jsdom';

const AUTOMATION_SRC = fs.readFileSync(
  new URL('../../extension-gecko/automation.js', import.meta.url), 'utf-8');

const FIXTURE = fs.readFileSync(
  new URL('../fixtures/counted-load-more.html', import.meta.url), 'utf-8');

function makeAuto(html, { runScripts = 'outside-only', full = false } = {}) {
  const dom = new JSDOM(
    full ? html : `<!DOCTYPE html><html><body>${html}</body></html>`,
    { runScripts, url: 'https://shop.example/listing' });
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
  return { auto: win.__browserplugAuto, win, doc: win.document };
}

// ---- the universal element-count carrier ------------------------------------

test('EVERY invoke result carries the element count before and after the method ran', async () => {
  const { auto } = makeAuto('<p>one</p><p>two</p>');
  for (const method of ['page_state', 'snapshot', 'inspect']) {
    const r = await auto.invoke(method, {});
    assert.equal(typeof r.nodesAtStart, 'number', `${method} must carry nodesAtStart`);
    assert.equal(typeof r.nodesAtEnd, 'number', `${method} must carry nodesAtEnd`);
  }
});

test('the carrier rides a FAILURE envelope too — a delta is most wanted when a step failed', async () => {
  const { auto } = makeAuto('<p>page</p>');
  const r = await auto.invoke('click', { target: '#nope' });
  assert.equal(r.ok, false);
  assert.equal(typeof r.nodesAtStart, 'number');
  assert.equal(typeof r.nodesAtEnd, 'number');
});

test('an unknown method still answers with the envelope AND the carrier', async () => {
  const { auto } = makeAuto('<p>page</p>');
  const r = await auto.invoke('teleport', {});
  assert.equal(r.ok, false);
  assert.match(r.error, /unknown automation method: teleport/);
  assert.equal(typeof r.nodesAtStart, 'number');
});

// ---- the click observer counts NODES, not mutation records ------------------

test('a click that APPENDS rows reports the element nodes it added', async () => {
  const { auto, win } = makeAuto(
    '<div id="grid"></div><button id="go">Load more</button>', { runScripts: 'dangerously' });
  win.eval(`document.getElementById('go').addEventListener('click', function () {
    var g = document.getElementById('grid');
    for (var i = 0; i < 8; i++) {
      var row = document.createElement('div');
      row.className = 'product';
      row.appendChild(document.createElement('a'));
      row.appendChild(document.createElement('span'));
      g.appendChild(row);
    }
  });`);
  const r = await auto.invoke('click', { target: '#go' });
  assert.equal(r.ok, true);
  assert.equal(r.nodesAdded, 8, 'appendChild of 8 rows = 8 element nodes entering the document');
  assert.equal(r.nodesRemoved, 0);
  assert.equal(r.structuralChange, true);
});

test('a click that REPLACES rows is distinguishable from one that adds them', async () => {
  // The discriminator domChanged could never make: same count, high churn, no content
  // gained. On a listing page these two are the difference between "8 more products"
  // and "the same 40 products, re-rendered".
  const rows = Array.from({ length: 10 }, (_, i) => `<span class="price">${i}</span>`).join('');
  const { auto, win } = makeAuto(
    `<div id="grid">${rows}</div><button id="go">Refresh</button>`, { runScripts: 'dangerously' });
  win.eval(`document.getElementById('go').addEventListener('click', function () {
    var g = document.getElementById('grid');
    g.innerHTML = g.innerHTML;
  });`);
  const r = await auto.invoke('click', { target: '#go' });
  assert.equal(r.nodesAdded, 10);
  assert.equal(r.nodesRemoved, 10);
  assert.equal(r.domChanged, true, 'domChanged alone would call this a success');
});

test('a handler-less button reports zero added and zero removed', async () => {
  const { auto } = makeAuto('<button id="dead">Sort</button>');
  const r = await auto.invoke('click', { target: '#dead' });
  assert.equal(r.nodesAdded, 0);
  assert.equal(r.nodesRemoved, 0);
  assert.equal(r.structuralChange, false, 'focus moving to the button is not a page change');
});

test('text-node churn is NOT counted as element nodes', async () => {
  const { auto, win } = makeAuto(
    '<p id="t">old</p><button id="go">Go</button>', { runScripts: 'dangerously' });
  win.eval(`document.getElementById('go').addEventListener('click', function () {
    document.getElementById('t').textContent = 'new';
  });`);
  const r = await auto.invoke('click', { target: '#go' });
  assert.equal(r.nodesAdded, 0, 'a text swap changes no element count');
  assert.equal(r.nodesRemoved, 0);
  assert.equal(r.domChanged, true, 'it DID mutate — the count is what says it gained nothing');
});

test('mutationBreakdown and structuralChange keep their exact existing shape', () => {
  // Guarded because click-state.test.mjs reads both; the count fields are additions,
  // never a reshaping of what was there.
  assert.ok(/mutationBreakdown: \{ childList: childListMutations, attributes: attrMutations \}/
    .test(AUTOMATION_SRC));
  assert.ok(/structuralChange: childListMutations > 0 \|\| urlChanged \|\| dialogOpened/
    .test(AUTOMATION_SRC));
});

// ---- inspect(count:) --------------------------------------------------------

test('inspect returns exactly one entry per requested selector, in input order', async () => {
  const { auto } = makeAuto('<ul><li class="p"></li><li class="p"></li><li class="p"></li></ul>');
  const r = await auto.invoke('inspect', { count: ['.p', '#nope', 'li'] });
  assert.equal(r.ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(r.counts)), [
    { selector: '.p', count: 3 },
    { selector: '#nope', count: 0 },
    { selector: 'li', count: 3 },
  ], 'one row per input, in order — the background pairs before/after off this');
});

test('an unparseable selector reports an ERROR, never a silent 0', async () => {
  // A 0 would read as "the rows disappeared", which is the opposite of the truth.
  const { auto } = makeAuto('<div class="p"></div>');
  const r = await auto.invoke('inspect', { count: ['((', '.p'] });
  assert.match(r.counts[0].error, /invalid selector/);
  assert.equal('count' in r.counts[0], false);
  assert.equal(r.counts[1].count, 1, 'the other selectors are still measured');
});

test('count is scoped by `within`, and a missing scope is an error rather than a 0', async () => {
  const { auto } = makeAuto(
    '<div id="a"><span class="x"></span><span class="x"></span></div>'
    + '<div id="b"><span class="x"></span></div>');
  const scoped = await auto.invoke('inspect', { count: ['.x'], within: '#a' });
  assert.deepEqual(JSON.parse(JSON.stringify(scoped.counts)), [{ selector: '.x', count: 2 }]);

  const gone = await auto.invoke('inspect', { count: ['.x'], within: '#gone' });
  assert.match(gone.counts[0].error, /is not in the document/);
  assert.equal('count' in gone.counts[0], false);
});

test('inspect without count is unchanged: {ok, watched} plus the carriers', async () => {
  const { auto } = makeAuto('<button id="b">Save</button>');
  const bare = await auto.invoke('inspect', {});
  assert.equal(bare.ok, true);
  assert.equal(bare.watched.length, 0);
  assert.equal(bare.counts, undefined, 'no count asked for, no counts key');

  const watched = await auto.invoke('inspect', { watch: ['#b'] });
  assert.equal(watched.watched.length, 1);
  assert.equal(watched.watched[0].found, true);
});

test('count and watch answer the same call', async () => {
  const { auto } = makeAuto('<div class="p"></div><div class="p"></div><button id="b">Save</button>');
  const r = await auto.invoke('inspect', { count: ['.p'], watch: ['#b'] });
  assert.equal(r.counts[0].count, 2);
  assert.equal(r.watched[0].found, true);
});

test('at most 8 selectors are measured — a bounded probe, not an arbitrary query engine', async () => {
  const { auto } = makeAuto('<div class="p"></div>');
  const r = await auto.invoke('inspect', { count: ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j'] });
  assert.equal(r.counts.length, 8);
});

// ---- against the fixture's own ground truth ---------------------------------

test('counted-load-more.html measures exactly what its header claims', async () => {
  // tests/fixtures/counted-load-more.html states: 12 .product rows initially, +8 per
  // #load-more click (after a 900ms delay), and a #no-op button that appends nothing
  // but DOES mutate an attribute. Driving the fixture here means its header cannot
  // quietly stop being true while the live sweep still passes.
  const { auto, win } = makeAuto(FIXTURE, { runScripts: 'dangerously', full: true });

  const initial = await auto.invoke('inspect', { count: ['.product'] });
  assert.equal(initial.counts[0].count, 12, 'GROUND TRUTH: 12 rows before any click');

  const dead = await auto.invoke('click', { target: '#no-op' });
  assert.equal(dead.nodesAdded, 0, 'the decoy appends nothing...');
  assert.equal(dead.domChanged, true, '...but it DOES mutate, so domChanged cannot tell them apart');
  const afterDead = await auto.invoke('inspect', { count: ['.product'] });
  assert.equal(afterDead.counts[0].count, 12, 'only a COUNT distinguishes them');

  const load = await auto.invoke('click', { target: '#load-more' });
  assert.equal(load.ok, true);
  assert.equal(load.nodesAdded, 0,
    'the rows land 900ms later — PAST the click observer, which is why the background '
    + 'owns the after-measure rather than the page');

  await new Promise(r => win.setTimeout(r, 1000));
  const afterLoad = await auto.invoke('inspect', { count: ['.product'] });
  assert.equal(afterLoad.counts[0].count, 20, 'GROUND TRUTH: 12 + 8 once the batch lands');
});
