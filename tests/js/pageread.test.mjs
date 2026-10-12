/*
 * The content.read window and the extraction-vs-document metrics.
 *
 * The failures being fixed:
 *   - page_read(offset=3500) on a 149-char page answered `[markdown] chars
 *     3500-3500 of 149 (no content extracted — try format="text" or a scope
 *     selector)`. The slice was never clamped, so an out-of-range offset
 *     returned an EMPTY window, and the advice blamed a format that was not the
 *     cause. There was also no way to learn a document's length without paying
 *     for its body.
 *   - a 290-char read of a live-chat modal on a page of 400 product cards and a
 *     7-char read of a genuinely 7-char page were byte-identical in the
 *     response. Nothing described the DOCUMENT, only the extraction.
 *
 * This drives the REAL driver: the block is sliced out of background.js by anchor,
 * runContentRead is called with a stubbed tabs.executeScript that captures the code
 * string it builds, and that string is then evaluated against a jsdom document. So
 * the arming glue (probe flag, clamp, metric threading) is exercised, not just the
 * two in-page helpers — the standing lesson about arming code with no unit coverage.
 *
 * The markdown branch takes its Defuddle-FAILURE path under jsdom for free (no
 * Defuddle global), which is exactly the branch a CSP crash lands on in the wild.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM } from 'jsdom';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

const FIXTURES = new URL('../fixtures/', import.meta.url);

function loadRead() {
  const start = BG_SRC.indexOf('// ---- content.read window + diagnostics');
  const end = BG_SRC.indexOf('// ---- content.act step deadline', start);
  assert.ok(start >= 0 && end > start,
    'content.read window block not found in background.js — the anchors moved');
  const block = BG_SRC.slice(start, end);

  // The markdown driver answers needsDefuddle when the page has no Defuddle, and the
  // extension then injects the bundle as a file and runs the driver again without that
  // exit. jsdom has no Defuddle, so by default the stub plays that page: the first driver
  // run says needsDefuddle, and the LAST captured code is the second run, the one that
  // lands on the extraction-failure branch when the bundle did not take.
  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    const PAGE_STATE_FN = 'function __fxPageState(){ return { painted: true, readyState: "complete" }; }';
    const captured = [];
    const files = [];
    let defuddleMissing = true;
    const browser = { tabs: { executeScript: async (tabId, details) => {
      if (details.file) { files.push(details.file); return [undefined]; }
      captured.push(details.code);
      if (defuddleMissing && details.code.includes('return { needsDefuddle: true }')) {
        return [{ needsDefuddle: true }];
      }
      return [{ __driver: details.code }];
    } } };

    ${block}

    return { runContentRead, readWindowParams, READ_THIN_CHARS, captured, files,
             setDefuddleMissing: (v) => { defuddleMissing = v; } };
  `);
  return factory();
}

// Build the driver code string the extension would inject, then RUN it in jsdom.
async function readIn(html, data, { url = 'http://localhost/test', runScripts = 'outside-only' } = {}) {
  const m = loadRead();
  await m.runContentRead(1, data);
  const code = m.captured[m.captured.length - 1];
  // runScripts:'outside-only' is what makes window.eval run INSIDE the jsdom realm,
  // so the driver's bare `document` / `location` resolve to this page. A fixture
  // whose own inline script IS the subject needs 'dangerously' instead.
  const dom = new JSDOM(html, { url, runScripts });
  // jsdom implements no layout, so innerText is absent; the drivers read it for
  // bodyChars. textContent is the honest stand-in for a laid-out innerText here.
  Object.defineProperty(dom.window.HTMLElement.prototype, 'innerText', {
    get() { return this.textContent; }, configurable: true,
  });
  return { result: dom.window.eval(code), code, module: m };
}

const LONG = 'The quick brown fox. '.repeat(120);   // 2520 chars, > READ_THIN_CHARS
const bodyOf = (text) => `<!DOCTYPE html><html><body><div id="t">${text}</div></body></html>`;

test('an offset INSIDE the document slices exactly that window, unflagged', async () => {
  const { result } = await readIn(bodyOf(LONG), { format: 'text', offset: 100, maxChars: 200 });
  assert.equal(result.ok, true);
  assert.equal(result.totalChars, LONG.length);
  assert.equal(result.offset, 100, 'the effective offset IS the requested one');
  assert.equal(result.content, LONG.slice(100, 300));
  assert.equal(result.offsetClamped, undefined);
  assert.equal(result.requestedOffset, undefined);
  assert.equal(result.probe, undefined);
});

test('an offset EXACTLY at the end returns the final window rather than nothing', async () => {
  // The case an agent reaches by guessing — and the one where an empty window is
  // most misleading, because the page is not empty at all.
  const { result } = await readIn(bodyOf(LONG), { format: 'text', offset: LONG.length, maxChars: 300 });
  assert.equal(result.offsetClamped, true);
  assert.equal(result.requestedOffset, LONG.length);
  assert.equal(result.offset, LONG.length - 300);
  assert.equal(result.content.length, 300);
  assert.ok(LONG.endsWith(result.content), 'the window returned IS the tail');
});

test('an offset FAR beyond the end returns the tail, never the head', async () => {
  const text = 'HEAD_MARKER ' + LONG + ' TAIL_MARKER';
  const { result } = await readIn(bodyOf(text), { format: 'text', offset: 99000, maxChars: 200 });
  assert.equal(result.offsetClamped, true);
  assert.equal(result.requestedOffset, 99000);
  assert.match(result.content, /TAIL_MARKER/);
  assert.doesNotMatch(result.content, /HEAD_MARKER/,
    '"clamped to the tail" and "silently reset to offset 0" must not look alike');
});

test('an EMPTY document is never clamped — there is no tail to return', async () => {
  // The invariant the server-side clamp note depends on: it may assume total > 0.
  for (const offset of [0, 1, 5000]) {
    const { result } = await readIn('<!DOCTYPE html><html><body></body></html>',
      { format: 'text', offset, maxChars: 500 });
    assert.equal(result.totalChars, 0);
    assert.equal(result.offsetClamped, undefined, `offset=${offset} must not clamp an empty page`);
    assert.equal(result.content, '');
  }
});

test('max_chars=0 is a LENGTH PROBE: the count without the body', async () => {
  const { result } = await readIn(bodyOf(LONG), { format: 'text', maxChars: 0 });
  assert.equal(result.probe, true);
  assert.equal(result.content, '', 'empty by DESIGN, and the flag is what says so');
  assert.equal(result.totalChars, LONG.length);
  assert.equal(result.offset, 0);
  assert.ok(result.docMetrics.sample.length > 0, 'a probe always carries the detail metrics');
  assert.equal(typeof result.docMetrics.iframes, 'number');
});

test('maxChars normalisation: absent keeps 20000, 1..49 floors at 50, <=0 probes', () => {
  const m = loadRead();
  assert.deepEqual(m.readWindowParams({}), { probeOnly: false, maxChars: 20000, offset: 0 });
  assert.deepEqual(m.readWindowParams({ maxChars: null }), { probeOnly: false, maxChars: 20000, offset: 0 });
  assert.equal(m.readWindowParams({ maxChars: 10 }).maxChars, 50);
  assert.equal(m.readWindowParams({ maxChars: 0 }).probeOnly, true);
  assert.equal(m.readWindowParams({ maxChars: -1 }).probeOnly, true);
  assert.equal(m.readWindowParams({ offset: -5 }).offset, 0, 'a negative offset floors at 0');
});

test('a healthy read pays only for nodes + bodyChars, never the expensive metrics', async () => {
  const { result } = await readIn(bodyOf(LONG), { format: 'text', maxChars: 20000 });
  assert.ok(result.totalChars >= 1000, 'precondition: this read is not thin');
  assert.equal(typeof result.docMetrics.nodes, 'number');
  assert.equal(typeof result.docMetrics.bodyChars, 'number');
  assert.equal(result.docMetrics.sample, undefined, 'payload economy: no sample on a healthy read');
  assert.equal(result.docMetrics.iframes, undefined);
  assert.equal(result.docMetrics.dialogs, undefined);
});

test('a THIN read carries the full document description, so the emptiness can be explained', async () => {
  const html = '<!DOCTYPE html><html><body>'
    + '<div id="app"></div>'
    + '<iframe src="about:blank"></iframe>'
    + '<div role="dialog" aria-modal="true">Need help? Live chat</div>'
    + '</body></html>';
  const { result } = await readIn(html, { format: 'text' });
  assert.ok(result.totalChars < 1000, 'precondition: this read is thin');
  assert.equal(result.docMetrics.iframes, 1);
  assert.equal(result.docMetrics.dialogs, 1);
  assert.match(result.docMetrics.sample, /Need help\? Live chat/);
  assert.ok(result.docMetrics.sample.length <= 300);
});

test('bodyChars is the BODY\'s text, reused only when the read really is the body', async () => {
  const html = `<!DOCTYPE html><html><body><div id="in">${LONG}</div><p>outside the scope</p></body></html>`;

  const whole = await readIn(html, { format: 'text' });
  assert.equal(whole.result.docMetrics.bodyChars, whole.result.totalChars,
    'format=text with no scope reuses the string it already computed — no second reflow');

  const scoped = await readIn(html, { format: 'text', scope: '#in' });
  assert.ok(scoped.result.docMetrics.bodyChars > scoped.result.totalChars,
    'with a scope, bodyChars must still describe the whole body');

  const asHtml = await readIn(html, { format: 'html' });
  assert.ok(asHtml.result.docMetrics.bodyChars < asHtml.result.totalChars,
    'with html, totalChars is markup — bodyChars must not be confused with it');
});

test('the whitespace-collapsed sample is a readable line, not raw markup indentation', async () => {
  const { result } = await readIn(
    '<!DOCTYPE html><html><body>\n\n   <p>Akceptuję    wszystkie\n pliki cookie</p>\n</body></html>',
    { format: 'text' });
  assert.equal(result.docMetrics.sample, 'Akceptuję wszystkie pliki cookie');
});

test('a scope that matches nothing refuses, and still reports the page state', async () => {
  const { result } = await readIn(bodyOf(LONG), { format: 'text', scope: '#nope' });
  assert.equal(result.ok, false);
  assert.match(result.error, /scope selector matched nothing/);
  assert.ok(result.pageState, 'the refusal describes the page too — the twin trees agree on this');
});

test('the defuddle-FAILURE fallback clamps and measures exactly like the main path', async () => {
  // Under jsdom there is no Defuddle global, so the markdown driver takes its catch —
  // the same branch a page CSP or a bundle crash lands on in the wild.
  const text = 'HEAD_MARKER ' + LONG + ' TAIL_MARKER';
  const { result } = await readIn(bodyOf(text), { format: 'markdown', offset: 99000, maxChars: 200 });
  assert.equal(result.ok, true);
  assert.equal(result.format, 'text');
  assert.match(result.fallback, /defuddle failed/);
  assert.equal(result.offsetClamped, true);
  assert.equal(result.requestedOffset, 99000);
  assert.match(result.content, /TAIL_MARKER/);
  assert.equal(typeof result.docMetrics.nodes, 'number');
  assert.equal(result.docMetrics.bodyChars, result.totalChars, 'the fallback reuses the text it read');
});

test('a probe on the markdown path is a probe on its fallback too', async () => {
  const { result } = await readIn(bodyOf(LONG), { format: 'markdown', maxChars: 0 });
  assert.equal(result.probe, true);
  assert.equal(result.content, '');
  assert.equal(result.totalChars, LONG.length);
});

test('the deferred-render fixture reads as thin against a document that is not', async () => {
  // tests/fixtures/deferred-render.html is the ground truth for that: a large node
  // count with almost no extractable text BEFORE its content mounts. Reading the
  // fixture here means its header comment cannot quietly stop being true.
  // 'dangerously': the fixture's own script is what builds the skeleton. Its
  // content mount is a 1200ms setTimeout, so it has NOT fired yet here — this is
  // deliberately the pre-mount reading.
  const html = fs.readFileSync(new URL('deferred-render.html', FIXTURES), 'utf-8');
  const { result } = await readIn(html, { format: 'text' }, { runScripts: 'dangerously' });
  assert.ok(result.totalChars < 1000,
    `the pre-mount extraction must be thin, got ${result.totalChars}`);
  assert.ok(result.docMetrics.nodes > 250,
    `the skeleton must already be node-rich, got ${result.docMetrics.nodes}`);
  assert.ok(result.docMetrics.sample.length > 0,
    'and the sample is what lets the discrepancy be described rather than guessed');
});

// ---- one injection per warm read, and the bundle as a file -----------------------------

test('a warm markdown read is ONE injection: no probe, no bundle', async () => {
  const m = loadRead();
  m.setDefuddleMissing(false);
  await m.runContentRead(1, { format: 'markdown' });
  assert.equal(m.captured.length, 1, 'the driver checks for Defuddle itself');
  assert.deepEqual(m.files, []);
});

test('a cold markdown read injects the bundle as a file, then runs the driver without the exit', async () => {
  const m = loadRead();
  await m.runContentRead(1, { format: 'markdown' });
  assert.deepEqual(m.files, ['/vendor/defuddle.js'], 'the 766 KB bundle is a file, not a code string');
  assert.equal(m.captured.length, 2);
  assert.ok(m.captured[0].includes('return { needsDefuddle: true }'));
  assert.ok(!m.captured[1].includes('return { needsDefuddle: true }'),
    'the second run must extract (or fall back), never ask again');
});

test('text and html reads never ask for the bundle', async () => {
  for (const format of ['text', 'html']) {
    const m = loadRead();
    await m.runContentRead(1, { format });
    assert.equal(m.captured.length, 1, format);
    assert.deepEqual(m.files, [], format);
  }
});

// ---- the extraction cache --------------------------------------------------------------
// Several reads against ONE jsdom document, the way an agent pages through a tab. A fake
// Defuddle counts its parses; the cache lives in the page, so it is the page that is kept.

function openPage(html, { withDefuddle = true, mutateWhileParsing = false } = {}) {
  const dom = new JSDOM(html, { url: 'http://localhost/article', runScripts: 'outside-only' });
  Object.defineProperty(dom.window.HTMLElement.prototype, 'innerText', {
    get() { return this.textContent; }, configurable: true,
  });
  let parses = 0;
  if (withDefuddle) {
    dom.window.Defuddle = function (doc, opts) {
      this.parse = () => {
        parses++;
        if (mutateWhileParsing) doc.body.setAttribute('data-extracting', String(parses));
        const root = opts.contentSelector ? doc.querySelector(opts.contentSelector) : doc.body;
        return { content: root.textContent, title: 'Article' };
      };
    };
  }
  // prepare() builds the driver and returns a function that runs it, so a test can change
  // the page and read it in one task, before the observer's callback runs.
  const prepare = async (data) => {
    const m = loadRead();
    m.setDefuddleMissing(!withDefuddle);
    await m.runContentRead(1, data);
    const code = m.captured[m.captured.length - 1];
    return () => dom.window.eval(code);
  };
  return {
    dom,
    parses: () => parses,
    prepare,
    async read(data) { return (await prepare(data))(); },
  };
}
const settleObservers = () => new Promise(r => setTimeout(r, 0));
const ARTICLE = bodyOf('<p id="a">' + LONG + '</p><p id="b">' + LONG + '</p>');

test('paging a markdown document extracts it once', async () => {
  const page = openPage(ARTICLE);
  const first = await page.read({ format: 'markdown', offset: 0, maxChars: 1000 });
  const second = await page.read({ format: 'markdown', offset: 1000, maxChars: 1000 });
  assert.equal(page.parses(), 1, 'the second page came from the cache');
  assert.equal(first.cached, undefined);
  assert.equal(second.cached, true);
  assert.equal(second.totalChars, first.totalChars);
  assert.equal(first.content + second.content, page.dom.window.document.body.textContent.slice(0, 2000),
    'the pages join up exactly');
});

test('a DOM change between pages re-extracts the live page and reports the new length', async () => {
  // The drift this guards: 249,500 chars on one page, 248,130 on the next, and nothing
  // said so. The owner's rule: read the live page again and flag it.
  const page = openPage(ARTICLE);
  const first = await page.read({ format: 'markdown', offset: 0, maxChars: 1000 });
  page.dom.window.document.getElementById('b').remove();
  await settleObservers();
  const second = await page.read({ format: 'markdown', offset: 1000, maxChars: 1000 });
  assert.equal(page.parses(), 2, 'never the old copy');
  assert.equal(second.cached, undefined);
  assert.equal(second.contentChanged.previousTotal, first.totalChars);
  assert.ok(second.totalChars < first.totalChars);
});

test('a change that leaves the length alone re-extracts without a flag', async () => {
  const page = openPage(ARTICLE);
  await page.read({ format: 'markdown', maxChars: 1000 });
  page.dom.window.document.getElementById('a').setAttribute('class', 'highlighted');
  await settleObservers();
  const second = await page.read({ format: 'markdown', offset: 1000, maxChars: 1000 });
  assert.equal(page.parses(), 2, 'an attribute change still drops the entry');
  assert.equal(second.contentChanged, undefined);
});

test('the extractor\'s own DOM work does not drop the entry it just made', async () => {
  const page = openPage(ARTICLE, { mutateWhileParsing: true });
  await page.read({ format: 'markdown', maxChars: 1000 });
  await settleObservers();
  const second = await page.read({ format: 'markdown', offset: 1000, maxChars: 1000 });
  assert.equal(second.cached, true, 'the observer starts after the extraction');
  assert.equal(page.parses(), 1);
});

// A miss can come while the observer still watches an older entry: the read is under
// another format or scope, or the entry it replaces outlived the TTL with the observer
// attached. Defuddle rewrites srcset on the live page's images, so an observer left on
// through the extraction would drop the entry being made.
test('a miss under another key keeps the entry being made and the one already watched', async () => {
  const page = openPage(ARTICLE, { mutateWhileParsing: true });
  await page.read({ format: 'markdown', maxChars: 1000 });
  await settleObservers();
  await page.read({ format: 'markdown', scope: '#a', maxChars: 1000 });
  await settleObservers();
  assert.equal((await page.read({ format: 'markdown', scope: '#a', maxChars: 1000 })).cached, true,
    'the scoped entry survived its own extraction');
  assert.equal((await page.read({ format: 'markdown', maxChars: 1000 })).cached, true,
    'the entry already watched survived it too');
  assert.equal(page.parses(), 2);
});

test('a miss past the TTL, with the observer still attached, keeps the fresh entry', async () => {
  const page = openPage(ARTICLE, { mutateWhileParsing: true });
  let now = page.dom.window.Date.now();
  page.dom.window.Date.now = () => now;
  await page.read({ format: 'markdown', maxChars: 1000 });
  await settleObservers();
  now += 121000;
  await page.read({ format: 'markdown', maxChars: 1000 });
  await settleObservers();
  assert.equal((await page.read({ format: 'markdown', maxChars: 1000 })).cached, true);
  assert.equal(page.parses(), 2);
});

test('a change queued just before a miss still drops the entries it invalidates', async () => {
  // Detaching the observer for the extraction must not throw away what it already holds.
  const page = openPage(ARTICLE);
  await page.read({ format: 'markdown', maxChars: 1000 });
  await settleObservers();
  const scoped = await page.prepare({ format: 'markdown', scope: '#a', maxChars: 1000 });
  page.dom.window.document.getElementById('b').remove();
  scoped();
  await settleObservers();
  assert.equal((await page.read({ format: 'markdown', maxChars: 1000 })).cached, undefined,
    'the removal reached the entry that was watched');
  assert.equal((await page.read({ format: 'markdown', scope: '#a', maxChars: 1000 })).cached, true,
    'the entry made after the removal stands');
});

test('a cache object an older build left in the page is replaced, not called', async () => {
  // Chromium keeps an extension's isolated world across a reload of the extension, so a
  // page opened before the update still holds the old object, without pause() or resume().
  const page = openPage(ARTICLE);
  let oldDropped = false;
  page.dom.window.__browserplugReadCache = {
    doc: page.dom.window.document, entries: {}, totals: {}, watching: true,
    drop() { oldDropped = true; },
  };
  const r = await page.read({ format: 'markdown', maxChars: 1000 });
  assert.equal(r.format, 'markdown', 'the read extracted, it did not fall back to text');
  assert.equal(oldDropped, true, 'the old object let go of its observer');
  assert.equal((await page.read({ format: 'markdown', maxChars: 1000 })).cached, true);
});

test('a resize or a stylesheet load drops the cache', async () => {
  const page = openPage(ARTICLE);
  await page.read({ format: 'markdown', maxChars: 1000 });
  page.dom.window.dispatchEvent(new page.dom.window.Event('resize'));
  await page.read({ format: 'markdown', maxChars: 1000 });
  assert.equal(page.parses(), 2, 'a resize can change what is visible');
  const link = page.dom.window.document.createElement('link');
  page.dom.window.document.head.appendChild(link);
  await settleObservers();
  await page.read({ format: 'markdown', maxChars: 1000 });
  assert.equal(page.parses(), 3, 'inserting the link is itself a mutation');
  link.dispatchEvent(new page.dom.window.Event('load'));
  await page.read({ format: 'markdown', maxChars: 1000 });
  assert.equal(page.parses(), 4, 'its stylesheet arriving drops the entry again');
});

test('an entry older than the limit is extracted again', async () => {
  const page = openPage(ARTICLE);
  const realNow = page.dom.window.Date.now;
  let now = realNow();
  page.dom.window.Date.now = () => now;
  await page.read({ format: 'markdown', maxChars: 1000 });
  now += 119000;
  assert.equal((await page.read({ format: 'markdown', maxChars: 1000 })).cached, true);
  now += 2000;
  await page.read({ format: 'markdown', maxChars: 1000 });
  assert.equal(page.parses(), 2, 'past 120 s the entry is not trusted');
});

test('format and scope are part of the key', async () => {
  const page = openPage(ARTICLE);
  const md = await page.read({ format: 'markdown', maxChars: 50 });
  const text = await page.read({ format: 'text', maxChars: 50 });
  const scoped = await page.read({ format: 'markdown', scope: '#a', maxChars: 50 });
  assert.equal(text.cached, undefined, 'a text read never serves a markdown extraction');
  assert.equal(scoped.cached, undefined, 'nor does a scoped read serve the whole page');
  assert.equal(page.parses(), 2);
  assert.equal(md.format, 'markdown');
  assert.equal(text.format, 'text');
});

test('the text path caches and flags a changed length the same way', async () => {
  const page = openPage(ARTICLE, { withDefuddle: false });
  const first = await page.read({ format: 'text', maxChars: 500 });
  assert.equal((await page.read({ format: 'text', offset: 500, maxChars: 500 })).cached, true);
  page.dom.window.document.getElementById('a').textContent = 'short';
  await settleObservers();
  const third = await page.read({ format: 'text', offset: 1000, maxChars: 500 });
  assert.equal(third.cached, undefined);
  assert.equal(third.contentChanged.previousTotal, first.totalChars);
});

// ---- script churn ----------------------------------------------------------------------
// Extensions and tag managers append <script src> to head and body long after load. Such a
// script cannot change a whole-page markdown extraction, so that entry stays; every other
// cached read can show it, and is extracted again.

const TAGGED = '<!DOCTYPE html><html><head><title>Article</title>'
  + '<script src="https://example.org/early.js"></script></head><body>'
  + '<script src="https://example.org/body-early.js"></script>'
  + '<div id="t"><p id="a">' + LONG + '</p><p id="b">' + LONG + '</p></div></body></html>';
const script = (doc, attrs = { src: 'https://example.org/tag.js' }, text = '') => {
  const s = doc.createElement('script');
  for (const [k, v] of Object.entries(attrs)) s.setAttribute(k, v);
  if (text) s.textContent = text;
  return s;
};

test('a script appended to head, body or html, then removed, keeps a markdown entry', async () => {
  const page = openPage(TAGGED);
  const doc = page.dom.window.document;
  await page.read({ format: 'markdown', maxChars: 1000 });
  const injected = doc.body.appendChild(script(doc));
  doc.head.appendChild(script(doc, { src: 'https://example.org/a.js', type: 'text/javascript', async: '' }));
  doc.head.insertBefore(script(doc, { src: 'https://example.org/gtm.js', type: 'module' }), doc.head.firstChild);
  doc.documentElement.appendChild(script(doc, { src: 'https://example.org/late.js', defer: '' }));
  await settleObservers();
  assert.equal((await page.read({ format: 'markdown', maxChars: 1000 })).cached, true);
  injected.remove();
  await settleObservers();
  assert.equal((await page.read({ format: 'markdown', maxChars: 1000 })).cached, true);
  assert.equal(page.parses(), 1);
});

test('script churn drops the text, html and scoped entries, which can show it', async () => {
  const page = openPage(TAGGED);
  const doc = page.dom.window.document;
  const reads = [{ format: 'text' }, { format: 'html' }, { format: 'markdown', scope: '#t' }];
  for (const r of reads) await page.read({ ...r, maxChars: 20000 });
  await page.read({ format: 'markdown', maxChars: 1000 });
  doc.body.appendChild(script(doc));
  await settleObservers();
  for (const r of reads) {
    const again = await page.read({ ...r, maxChars: 20000 });
    assert.equal(again.cached, undefined, JSON.stringify(r));
    if (r.format === 'html') assert.match(again.content, /tag\.js/, 'html carries the new script');
  }
  assert.equal((await page.read({ format: 'markdown', maxChars: 1000 })).cached, true,
    'the whole-page markdown entry outlives the script');
});

test('a record that mixes a script with any other node drops a markdown entry', async () => {
  const others = [
    (doc) => doc.createElement('div'),
    (doc) => doc.createElement('style'),
    (doc) => { const l = doc.createElement('link'); l.setAttribute('rel', 'stylesheet'); return l; },
    (doc) => doc.createTextNode('late text'),
  ];
  for (const make of others) {
    const page = openPage(TAGGED);
    const doc = page.dom.window.document;
    await page.read({ format: 'markdown', maxChars: 1000 });
    const other = make(doc);
    doc.body.append(script(doc), other);   // one record, both nodes
    await settleObservers();
    assert.equal((await page.read({ format: 'markdown', maxChars: 1000 })).cached, undefined, other.nodeName);
  }
});

test('an attribute change on the parent of an added script drops a markdown entry', async () => {
  const page = openPage(TAGGED);
  const doc = page.dom.window.document;
  await page.read({ format: 'markdown', maxChars: 1000 });
  doc.body.appendChild(script(doc));
  doc.body.setAttribute('class', 'consent-shown');
  await settleObservers();
  assert.equal((await page.read({ format: 'markdown', maxChars: 1000 })).cached, undefined);
});

test('a script Defuddle could read, or one between elements, drops a markdown entry', async () => {
  const src = 'https://example.org/x.js';
  const cases = {
    'inline code': (doc) => doc.body.appendChild(script(doc, {}, 'window.x = 1')),
    'src and text': (doc) => doc.body.appendChild(script(doc, { src }, '{}')),
    'JSON-LD': (doc) => doc.head.appendChild(script(doc, { src, type: 'application/ld+json' })),
    'a math type': (doc) => doc.body.appendChild(script(doc, { src, type: 'math/tex' })),
    'MathJax': (doc) => doc.head.appendChild(script(doc, { src: 'https://cdn.example.org/mathjax/tex-chtml.js' })),
    'an id': (doc) => doc.head.appendChild(script(doc, { src, id: 'initial-state' })),
    'a class': (doc) => doc.body.appendChild(script(doc, { src, class: 'author' })),
    'before an element': (doc) => doc.body.insertBefore(script(doc), doc.getElementById('t')),
    'inside the content': (doc) => doc.getElementById('t').appendChild(script(doc)),
    'removed from before an element': (doc) => doc.querySelector('body > script').remove(),
    'a changed src': (doc) => doc.querySelector('head > script').setAttribute('src', src),
  };
  for (const [name, change] of Object.entries(cases)) {
    const page = openPage(TAGGED);
    await page.read({ format: 'markdown', maxChars: 1000 });
    change(page.dom.window.document);
    await settleObservers();
    assert.equal((await page.read({ format: 'markdown', maxChars: 1000 })).cached, undefined, name);
  }
});
