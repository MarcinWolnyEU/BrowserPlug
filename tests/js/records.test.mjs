/*
 * page_snapshot mode="records": structured extraction from repeated cards.
 *
 * The failure this replaces is quiet and expensive: two querySelectorAll results
 * zipped by index look plausible right up until one card contributes a different
 * number of matches than its neighbours, and then a price belonging to a variant
 * (or to the next card entirely) is reported next to the wrong title. Every test
 * here is about that class of mistake:
 *   - a field must come from INSIDE its own card, never a sibling;
 *   - a card holding several LIVE prices must be reported AMBIGUOUS, not guessed;
 *   - a struck-through "was" price must not manufacture ambiguity;
 *   - a page with no repeated pattern must REFUSE rather than return noise.
 *
 * automation.js is a [shared] file (byte-identical in both extension trees), so this
 * suite is scope="shared" in tests/parity/parity.toml — one run covers gecko and
 * chromium. jsdom has no layout engine, so getClientRects/getBoundingClientRect are
 * stubbed exactly as tests/js/automation.test.mjs does.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from 'jsdom';

// Arrays built INSIDE automation.js live in the jsdom realm, so their prototype is
// not reference-equal to Node's and assert.deepEqual rejects them. Compare by JSON
// shape, exactly as tests/js/automation.test.mjs does.
function sameShape(actual, expected, msg) {
  assert.equal(JSON.stringify(actual), JSON.stringify(expected), msg);
}

const AUTOMATION_SRC = fs.readFileSync(
  new URL('../../extension-gecko/automation.js', import.meta.url), 'utf-8');

const ZL = 'zł';           // "zł" — kept as an escape so the file stays ASCII-safe
const NBSP = ' ';

function makeAuto(html, url = 'https://shop.example/listing') {
  const dom = new JSDOM(`<!DOCTYPE html><html><body>${html}</body></html>`,
    { runScripts: 'outside-only', url });
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
  return win.__browserplugAuto;
}

function plainCard(i, name, price) {
  return `<article class="card">
    <a href="/oferta/${i}-item"><h2>${name}</h2></a>
    <div class="prices"><span class="now">${price} ${ZL}</span></div>
  </article>`;
}

// A grid whose middle card embeds a capacity switcher — the live Allegro shape
// that produced "WD My Book 14TB @ 1199,00" when the real price was 1829,74.
const VARIANT_GRID = `<div class="grid">
  ${plainCard(0, 'Seagate Expansion 8TB', '799,00')}
  <article class="card">
    <a href="/oferta/1-mybook"><h2>WD My Book 14TB</h2></a>
    <div class="variants"><button>8TB</button><button>14TB</button></div>
    <div class="prices"><span class="now">1829,74 ${ZL}</span></div>
    <div class="other-variant"><span>1199,00 ${ZL}</span></div>
  </article>
  ${plainCard(2, 'Toshiba Canvio 6TB', '649,99')}
</div>`;

test('records finds the repeated card pattern with no selector supplied', async () => {
  const auto = makeAuto(VARIANT_GRID);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ok, true, r.error);
  assert.equal(r.mode, 'records');
  assert.equal(r.count, 3, 'three cards');
  assert.equal(r.detection.method, 'auto');
  assert.equal(r.detection.itemSelector, 'article.card',
    'reports the selector it found, so the agent can reuse/verify it');
});

test('every field comes from its OWN card — no index drift across siblings', async () => {
  const auto = makeAuto(`<div class="grid">
    ${plainCard(0, 'Alpha 2TB', '100,00')}
    ${plainCard(1, 'Beta 4TB', '200,00')}
    ${plainCard(2, 'Gamma 6TB', '300,00')}
  </div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  sameShape(r.records.map(x => x.title), ['Alpha 2TB', 'Beta 4TB', 'Gamma 6TB']);
  sameShape(r.records.map(x => x.price.value), [100, 200, 300]);
  sameShape(r.records.map(x => x.link.replace(/^.*\/oferta\//, '')),
    ['0-item', '1-item', '2-item']);
});

test('a card with TWO live prices is reported ambiguous instead of picking one', async () => {
  const auto = makeAuto(VARIANT_GRID);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ambiguousCount, 1);
  const mybook = r.records.find(x => x.title === 'WD My Book 14TB');
  assert.equal(mybook.ambiguous, true);
  assert.equal(mybook.price, null, 'NO price is attributed — that is the whole point');
  const values = mybook.prices.filter(p => !p.struck).map(p => p.value).sort((a, b) => a - b);
  sameShape(values, [1199, 1829.74], 'both candidates are surfaced verbatim');
  assert.match(mybook.ambiguousReason, /2 different prices/);
  assert.match(mybook.ambiguousReason, /oferta\/1-mybook/, 'points at the item page for the truth');
  // the unambiguous neighbours are unaffected
  assert.equal(r.records.find(x => x.title === 'Seagate Expansion 8TB').price.value, 799);
});

test('a struck-through "was" price does not manufacture ambiguity', async () => {
  const auto = makeAuto(`<div class="grid">
    ${[0, 1, 2].map(i => `<article class="card">
      <a href="/p/${i}"><h2>Drive ${i}</h2></a>
      <div class="prices"><del>1${NBSP}999,00 ${ZL}</del><span>1199,00 ${ZL}</span></div>
    </article>`).join('')}
  </div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ambiguousCount, 0, 'old-price/new-price is not a variant conflict');
  const rec = r.records[0];
  assert.equal(rec.price.value, 1199);
  assert.equal(rec.priceWas.value, 1999, 'the crossed-out price is kept, but as priceWas');
  assert.equal(rec.priceWas.struck, true);
});

test('prices parse across locale separators and are also returned verbatim', async () => {
  const auto = makeAuto(`<div class="grid">
    <article class="card"><a href="/p/0"><h2>PL space grouping</h2></a>
      <span>1${NBSP}199,00 ${ZL}</span></article>
    <article class="card"><a href="/p/1"><h2>US grouping</h2></a>
      <span>$1,199.00</span></article>
    <article class="card"><a href="/p/2"><h2>DE grouping</h2></a>
      <span>1.199,00 EUR</span></article>
  </div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  sameShape(r.records.map(x => x.price.value), [1199, 1199, 1199]);
  assert.equal(r.records[0].price.text.includes('1'), true);
  assert.equal(r.records[1].price.currency, '$');
});

test('a bare number is never harvested as a price (capacity, counts, ranks)', async () => {
  const auto = makeAuto(`<div class="grid">
    ${[0, 1, 2].map(i => `<article class="card">
      <a href="/p/${i}"><h2>Drive 14TB</h2></a>
      <span class="reviews">128 reviews</span><span class="rank">#${i + 1}</span>
    </article>`).join('')}
  </div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ok, true, r.error);
  assert.equal(r.records[0].price, null);
  assert.equal(r.records[0].prices, undefined, 'no price nodes at all — not a "14" from 14TB');
  assert.equal(r.withPrice, 0);
});

test('a tracking-redirect link is unwrapped to the real target', async () => {
  const real = 'https://allegro.pl/oferta/dysk-wd-elements-14tb-17984469865?bi_s=ads';
  const auto = makeAuto(`<div class="grid">
    ${[0, 1, 2].map(i => `<article class="card">
      <a href="/events/clicks?emission_unit_id=u${i}&redirect=${encodeURIComponent(real)}">
        <h2>WD Elements Desktop 14TB</h2></a>
      <span>99${i},00 ${ZL}</span></article>`).join('')}
  </div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.records[0].link, real, 'the canonical product URL, not the click counter');
  assert.match(r.records[0].linkUnwrappedFrom, /events\/clicks/,
    'the wrapper is still reported, so the unwrap is auditable');
});

test('a redirector whose target is SITE-ROOT-RELATIVE is unwrapped too', async () => {
  // Trackers wrap absolute and root-relative targets about equally often; only the
  // absolute form was covered until a live fixture caught the other one.
  const auto = makeAuto(`<div class="grid">
    ${[0, 1, 2].map(i => `<article class="card">
      <a href="/events/clicks?emission_unit_id=u${i}&redirect=%2Foferta%2Fwd-14tb%3Fbi_s%3Dads">
        <h2>WD Elements Desktop 14TB</h2></a>
      <span>99${i},00 ${ZL}</span></article>`).join('')}
  </div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.records[0].link, 'https://shop.example/oferta/wd-14tb?bi_s=ads');
  assert.match(r.records[0].linkUnwrappedFrom, /events\/clicks/);
});

test('a non-URL value in a redirect-shaped param is left alone', async () => {
  // Inventing a link out of "?u=17984469865" would be worse than reporting the
  // wrapper: the wrapper at least resolves.
  const auto = makeAuto(`<div class="grid">
    ${[0, 1, 2].map(i => `<article class="card">
      <a href="/p/${i}?u=1798446986${i}"><h2>Drive ${i}</h2></a>
      <span>99${i},00 ${ZL}</span></article>`).join('')}
  </div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.match(r.records[0].link, /\/p\/0\?u=17984469860$/);
  assert.equal(r.records[0].linkUnwrappedFrom, undefined);
});

test('a short, price-free repeated group is returned but flagged LOW CONFIDENCE', async () => {
  // A related-posts sidebar IS a real repeated pattern, so refusing would be a lie —
  // but on an article page it is not the listing the caller meant. Say so.
  const auto = makeAuto(`<aside><ul>
      <li><a href="/blog/a">Designing Heartbeat Protocols</a></li>
      <li><a href="/blog/b">Exponential Backoff in Practice</a></li>
      <li><a href="/blog/c">Offline-First Architectures</a></li>
    </ul></aside>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ok, true, 'the rows are accurate — do not refuse them');
  assert.equal(r.withPrice, 0);
  assert.match(r.detection.lowConfidence, /related-links/);
  assert.match(r.detection.lowConfidence, /item_selector/);
});

test('a real priced grid is never flagged low-confidence', async () => {
  const auto = makeAuto(`<div class="grid">
    ${plainCard(0, 'Alpha 2TB', '100,00')}${plainCard(1, 'Beta 4TB', '200,00')}${plainCard(2, 'Gamma 6TB', '300,00')}
  </div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.detection.lowConfidence, undefined);
});

test('no confident repeated pattern => a refusal that names what it considered', async () => {
  const auto = makeAuto(`<nav><a href="/a">A</a><a href="/b">B</a><a href="/c">C</a></nav>
    <p>An ordinary article page, not a listing.</p>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ok, false, 'noise dressed as data is worse than an honest miss');
  assert.match(r.error, /no repeated-card pattern could be identified/);
  assert.match(r.error, /item_selector/, 'names the escape hatch');
  assert.ok(Array.isArray(r.rejectedCandidates));
  assert.match(r.error, /nav/, 'says which group it rejected and why');
});

test('item_selector overrides detection; a miss says so instead of falling back', async () => {
  const auto = makeAuto(VARIANT_GRID);
  const good = await auto.invoke('snapshot', { mode: 'records', item_selector: 'article.card' });
  assert.equal(good.ok, true);
  assert.equal(good.detection.method, 'item_selector');
  assert.equal(good.count, 3);

  const miss = await auto.invoke('snapshot', { mode: 'records', item_selector: '.nope' });
  assert.equal(miss.ok, false);
  assert.match(miss.error, /matched no elements/);

  const broken = await auto.invoke('snapshot', { mode: 'records', item_selector: 'div:has-text(x)' });
  assert.equal(broken.ok, false);
  assert.match(broken.error, /invalid item_selector/);
});

test('root scopes the search, and an unresolvable root is refused (never widened)', async () => {
  const auto = makeAuto(`<div id="sponsored">
      ${plainCard(9, 'Sponsored Drive', '111,00')}${plainCard(8, 'Sponsored Two', '222,00')}${plainCard(7, 'Sponsored Three', '333,00')}
    </div>
    <div id="organic">
      ${plainCard(0, 'Real Drive A', '100,00')}${plainCard(1, 'Real Drive B', '200,00')}${plainCard(2, 'Real Drive C', '300,00')}
    </div>`);
  const scoped = await auto.invoke('snapshot', { mode: 'records', root: '#organic' });
  assert.equal(scoped.ok, true, scoped.error);
  sameShape(scoped.records.map(x => x.title), ['Real Drive A', 'Real Drive B', 'Real Drive C']);

  const gone = await auto.invoke('snapshot', { mode: 'records', root: '#does-not-exist' });
  assert.equal(gone.ok, false);
  assert.match(gone.error, /root not found/);
});

test('max caps the rows and says the result was truncated', async () => {
  const many = Array.from({ length: 12 }, (_, i) => plainCard(i, `Drive ${i}`, `${i + 1}00,00`)).join('');
  const auto = makeAuto(`<div class="grid">${many}</div>`);
  const r = await auto.invoke('snapshot', { mode: 'records', max: 5 });
  assert.equal(r.count, 5);
  assert.equal(r.totalItems, 12);
  assert.equal(r.truncated, true);
});

test('each record carries a live ref usable as a page_act `within` scope', async () => {
  const auto = makeAuto(VARIANT_GRID);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  const ref = r.records[1].ref;
  assert.match(ref, /^e\d+$/);
  const scoped = await auto.invoke('snapshot', { root: ref });
  assert.equal(scoped.ok, true, 'the ref resolves as a snapshot root, so refs are not decorative');
});

test('interactive and forms modes are untouched by the records addition', async () => {
  const auto = makeAuto(`<button id="b">Go</button><input id="t" type="text">`);
  const interactive = await auto.invoke('snapshot', {});
  assert.equal(interactive.ok, true);
  assert.equal(interactive.mode, undefined);
  assert.ok(interactive.elements.length >= 2);
  const forms = await auto.invoke('snapshot', { mode: 'forms' });
  assert.equal(forms.mode, 'forms');
});

// ---------------------------------------------------------------------------
// Detection against the REAL fixture files. These same pages are driven through a
// live browser by tests/integration/test_agent_experience.py; running them here too
// keeps the feedback loop at ~1s for the part that is pure DOM reasoning, and means
// a fixture edit cannot quietly stop testing what its header comment claims.
// ---------------------------------------------------------------------------

function makeAutoFromFixture(name, url = 'https://shop.example/listing') {
  const html = fs.readFileSync(
    new URL('../fixtures/' + name, import.meta.url), 'utf-8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url });
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
  return win.__browserplugAuto;
}

test('sponsored strip vs organic results: the ORGANIC grid wins, sponsored is runner-up', async () => {
  // Both are genuine repeated patterns of priced, linked, identical cards. Picking
  // the 3-card promo strip would return a confident, complete-looking table of the
  // wrong three products.
  const auto = makeAutoFromFixture('sponsored-and-organic-grids.html');
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ok, true, r.error);
  assert.equal(r.count, 18, 'the 18 organic offers, not the 3 promos');
  assert.equal(r.records.filter(x => /^PROMO/.test(x.title || '')).length, 0,
    'no sponsored card leaked into the result set');
  assert.ok(r.detection.runnerUp, 'the rejected alternative must be named, not silently dropped');
  assert.equal(r.detection.runnerUp.count, 3,
    'the promo strip is the runner-up, so item_selector can reverse the choice');
});

test('per-card generated class names still group (the structural fallback key)', async () => {
  // Every card carries a DIFFERENT hashed class, so grouping by tag+class puts each
  // in a bucket of one; only the child-tag-signature key recovers the pattern.
  const auto = makeAutoFromFixture('generated-class-cards.html');
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ok, true, r.error);
  assert.equal(r.count, 8);
  assert.equal(r.withPrice, 8, 'every card resolved its own price');
  assert.equal(r.records[0].title, 'Samsung 990 PRO 2TB');
  assert.equal(r.records[7].price.value, 779);
  // The selector guess cannot use a shared class here — it must still be usable.
  assert.ok(r.detection.itemSelector, 'a reusable selector is still reported');
});

test('a 40-link footer does NOT beat a 12-card product grid', async () => {
  // The adversarial case for scoring: the footer is bigger, fully linked and full of
  // real text. Weighting mostly by size handed it the win, which would have returned
  // 40 tidy rows of navigation for a page whose listing was never examined.
  const auto = makeAutoFromFixture('footer-link-matrix.html');
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ok, true, r.error);
  assert.equal(r.count, 12, 'the products won');
  assert.equal(r.withPrice, 12);
  assert.equal(r.records.some(x => /Popularne|kategorie|Routery/.test(x.title || '')), false,
    'no footer link leaked in');
  assert.equal(r.detection.lowConfidence, undefined, 'a priced grid is a confident answer');
});

test('the variant fixture matches the ground truth in its own header comment', async () => {
  const auto = makeAutoFromFixture('variant-grouped-cards.html');
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.count, 6);
  assert.equal(r.ambiguousCount, 1);
  const by = Object.fromEntries(r.records.map(x => [x.title, x]));
  assert.equal(by['Seagate Expansion 8TB'].price.value, 799);
  assert.equal(by['Toshiba Canvio 6TB'].price.value, 649.99);
  assert.equal(by['WD My Book 14TB'].price, null);
  assert.equal(by['WD My Book 14TB'].ambiguous, true);
  assert.equal(by['Seagate IronWolf 4TB'].price.value, 1199, 'NBSP-grouped thousands');
  assert.equal(by['WD Elements 12TB'].price.value, 1099);
  assert.equal(by['WD Elements 12TB'].priceWas.value, 1499);
  assert.match(by['WD Elements Desktop 14TB'].link, /\/oferta\/wd-elements-14tb/);
  // "14TB" and "128 reviews" are bare numbers, never money.
  assert.equal(by['Seagate Expansion 8TB'].prices.length, 1);
});

// ---------------------------------------------------------------------------
// Price parsing table. Currency-anchored matching + separator disambiguation is
// where a silent wrong NUMBER comes from, and a wrong number survives every other
// check in this file: the row looks perfect. One card per format, ground truth in
// the table itself.
// ---------------------------------------------------------------------------

const PRICE_CASES = [
  ['799,00 zł',        799,      'PL decimal comma'],
  ['1 199,00 zł', 1199,     'PL NBSP thousands'],
  ['1 199,00 zł', 1199,     'narrow NBSP thousands'],
  ['1 199,00 zł',      1199,     'plain space thousands'],
  ['1.199,00 EUR',          1199,     'DE dot thousands + comma decimal'],
  ['$1,199.00',             1199,     'US comma thousands + dot decimal'],
  ['1,199 USD',             1199,     'US thousands, no decimals'],
  ['1199 zł',          1199,     'no separators at all'],
  ['0,99 zł',          0.99,     'sub-unit price'],
  ['12 345,67 zł', 12345.67, 'five figures'],
  ['€1.234,50',        1234.5,   'symbol before, DE separators'],
  ['1234,5 zł',        1234.5,   'single decimal digit'],
  ['PLN 249,99',            249.99,   'ISO code before the number'],
  ['249,99 PLN',            249.99,   'ISO code after the number'],
  ['£89.99',           89.99,    'GBP symbol'],
  ['Kč 1 299,00',      1299,     'multi-byte currency token'],
];

test('price formats parse to the right number, and keep their verbatim text', async () => {
  const cards = PRICE_CASES.map(([text], i) =>
    `<article class="card"><a href="/p/${i}"><h2>Drive number ${i}</h2></a>
       <span class="price">${text}</span></article>`).join('');
  const auto = makeAuto(`<div class="grid">${cards}</div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ok, true, r.error);
  assert.equal(r.count, PRICE_CASES.length);
  for (let i = 0; i < PRICE_CASES.length; i++) {
    const [text, expected, why] = PRICE_CASES[i];
    const rec = r.records[i];
    assert.ok(rec.price, `${why}: ${JSON.stringify(text)} produced no price at all`);
    assert.equal(rec.price.value, expected, `${why}: ${JSON.stringify(text)}`);
    assert.equal(rec.price.text.replace(/\s/g, ' ').trim(),
      text.replace(/\s/g, ' ').trim(),
      `${why}: the verbatim text must survive parsing`);
  }
});

const NON_PRICE_CASES = [
  ['14TB',           'a capacity'],
  ['128 reviews',    'a review count'],
  ['#3',             'a rank'],
  ['7200 obr/min',   'a spindle speed'],
  ['2024',           'a year'],
  ['3.5 cala',       'a form factor'],
];

test('bare numbers are never harvested as money, whatever they look like', async () => {
  const cards = NON_PRICE_CASES.map(([text], i) =>
    `<article class="card"><a href="/p/${i}"><h2>Drive number ${i}</h2></a>
       <span class="spec">${text}</span></article>`).join('');
  const auto = makeAuto(`<div class="grid">${cards}</div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ok, true, r.error);
  assert.equal(r.withPrice, 0, 'not one of these is a price');
  for (let i = 0; i < NON_PRICE_CASES.length; i++) {
    assert.equal(r.records[i].prices, undefined,
      `${NON_PRICE_CASES[i][1]} (${NON_PRICE_CASES[i][0]}) was mistaken for money`);
  }
});

// ---- price labels: an ambiguous card says which price is which -------------------

test('each live price of an ambiguous card carries the words beside it', async () => {
  // The marketplace shape: an item price, and the same offer with delivery included.
  const card = (i) => `<article class="card">
    <a href="/oferta/${i}"><h2>Pendrive ${i} 128 GB</h2></a>
    <div class="now"><span>3${i},99${NBSP}${ZL}</span></div>
    <div class="delivery"><span>4${i},48${NBSP}${ZL}</span> <span>z dostawą</span></div>
  </article>`;
  const auto = makeAuto(`<div class="grid">${[0, 1, 2].map(card).join('')}</div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  assert.equal(r.ambiguousCount, 3, 'still ambiguous: no price is picked for the agent');
  const rec = r.records[0];
  assert.equal(rec.price, null);
  const live = rec.prices.filter(p => !p.struck);
  const delivered = live.find(p => p.value === 40.48);
  assert.equal(delivered.label, 'z dostawą');
  const item = live.find(p => p.value === 30.99);
  assert.equal(item.label, undefined, 'a price with no words of its own gets no label');
});

test('a label never comes from the card heading or from a neighbouring price', async () => {
  const auto = makeAuto(`<div class="grid">${[0, 1, 2].map(i => `<article class="card">
    <div class="info"><h2>Drive ${i}</h2> <span>1${i}9,00 ${ZL}</span></div>
    <div class="alt"><span>2${i}9,00 ${ZL}</span><span>3${i}9,00 ${ZL}</span></div>
    <a href="/p/${i}">more</a>
  </article>`).join('')}</div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  for (const p of r.records[0].prices) {
    assert.ok(!p.label || !/Drive/.test(p.label), `the title is not a label: ${JSON.stringify(p)}`);
    assert.ok(!p.label || !/\d/.test(p.label), `no price text inside a label: ${JSON.stringify(p)}`);
  }
});

test('in one run of text, a colon label introduces the price after it', async () => {
  const auto = makeAuto(`<div class="grid">${[0, 1, 2].map(i => `<article class="card">
    <a href="/p/${i}"><h2>Lamp ${i}</h2></a>
    <div class="prices">Cena: 1${i},00 ${ZL} Z dostawą: 2${i},50 ${ZL}</div>
  </article>`).join('')}</div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  const live = r.records[1].prices.filter(p => !p.struck);
  assert.equal(live.find(p => p.value === 11).label, 'Cena');
  assert.equal(live.find(p => p.value === 21.5).label, 'Z dostawą');
});

test('in one run of text, trailing words belong to the last price', async () => {
  const auto = makeAuto(`<div class="grid">${[0, 1, 2].map(i => `<article class="card">
    <a href="/p/${i}"><h2>Hub ${i}</h2></a>
    <div class="prices">1${i},00 ${ZL} 2${i},50 ${ZL} z dostawą</div>
  </article>`).join('')}</div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  const live = r.records[2].prices.filter(p => !p.struck);
  assert.equal(live.find(p => p.value === 12).label, undefined);
  assert.equal(live.find(p => p.value === 22.5).label, 'z dostawą');
});

test('a long run of words is clipped, so a label never becomes a paragraph', async () => {
  const words = 'najniższa cena z ostatnich trzydziestu dni przed obniżką ceny';
  const auto = makeAuto(`<div class="grid">${[0, 1, 2].map(i => `<article class="card">
    <a href="/p/${i}"><h2>Cable ${i}</h2></a>
    <div class="a"><span>9,99 ${ZL}</span></div>
    <div class="b"><span>${words}</span> <span>1${i},99 ${ZL}</span></div>
  </article>`).join('')}</div>`);
  const r = await auto.invoke('snapshot', { mode: 'records' });
  const labelled = r.records[0].prices.find(p => p.label);
  assert.ok(labelled, JSON.stringify(r.records[0].prices));
  assert.ok(labelled.label.length <= 41, labelled.label);
  assert.match(labelled.label, /^najniższa cena/);
});
