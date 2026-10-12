// Unit tests for extension-gecko/vendor/deepseek-view.js (jsdom, no Firefox).
// Run: node --test tests/js/deepseek-view.test.mjs   (or: npm run test:js)
//
// The windows are fixed in UTC (01:00-04:00 and 06:00-10:00 peak Monday to Friday,
// everything else off-peak; https://api-docs.deepseek.com/quick_start/pricing/), so each case
// names a FIXED instant: the module has no data source to stub, and a test on the
// ambient clock would only ever check the phase the suite happened to run in.
//
// Load-bearing distinction: these are DeepSeek's windows, not a shared notion of
// "peak". Anthropic's (weekdays 12:00-18:00 UTC, anthropic-view.js) are about
// capacity; these are about price. They now agree on skipping the weekend and on
// nothing else, so the last tests pin that they stay apart.
//
// Calendar for the fixed dates below: 2026-08-16 Sun, 17 Mon, 21 Fri, 22 Sat,
// 23 Sun, 24 Mon.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/deepseek-view.js'), 'utf8');
const ANTHROPIC_SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/anthropic-view.js'), 'utf8');

// Freeze the clock: the builders read the ambient `Date` (as anthropic-view.js's do),
// so overriding the global makes their internal `new Date()` deterministic.
function withFakeNow(iso, fn) {
  const RealDate = Date;
  const fixed = new RealDate(iso).getTime();
  class FakeDate extends RealDate {
    constructor(...args) { if (args.length === 0) super(fixed); else super(...args); }
    static now() { return fixed; }
  }
  globalThis.Date = FakeDate;
  try { return fn(); } finally { globalThis.Date = RealDate; }
}

function loadModule() {
  const dom = new JSDOM('<!doctype html><body></body>');
  const g = dom.window;
  g.globalThis = g;
  // eslint-disable-next-line no-new-func
  new Function('window', 'document', 'globalThis', SRC)(g, g.document, g);
  return g.DeepSeekView;
}

// TZ pinned so the local-clock rendering is reproducible anywhere (Europe/Warsaw is
// UTC+2 in August, where the live check was made).
process.env.TZ = 'Europe/Warsaw';
const utc = (h, m = 0, day = 17) => new Date(Date.UTC(2026, 7, day, h, m, 0));

test('the two peak windows are the ones DeepSeek bills double for', () => {
  // Compared as JSON: the arrays are built in the jsdom realm, so deepEqual fails
  // them on prototype identity alone.
  assert.equal(JSON.stringify(loadModule().PEAK_WINDOWS), '[[60,240],[360,600]]');
});

test('peakInfo: mid-window is inPeak, at the right fraction', () => {
  const DS = loadModule();
  const p = DS.peakInfo(utc(2, 30), 0);
  assert.equal(p.inPeak, true);
  assert.equal(p.visible, true);
  assert.equal(p.start.toISOString(), '2026-08-17T01:00:00.000Z');
  assert.equal(p.end.toISOString(), '2026-08-17T04:00:00.000Z');
  assert.equal(p.msLeft, 90 * 60000);
  assert.ok(Math.abs(p.frac - 0.5) < 1e-9);
});

test('peakInfo: a boundary belongs to the window it opens, not the one it closes', () => {
  const DS = loadModule();
  assert.equal(DS.peakInfo(utc(1, 0)).inPeak, true, '01:00 is already peak');
  assert.equal(DS.peakInfo(utc(4, 0)).inPeak, false, '04:00 is already off-peak');
  assert.equal(DS.peakInfo(utc(6, 0)).inPeak, true);
  assert.equal(DS.peakInfo(utc(10, 0)).inPeak, false);
});

test('peakInfo: outside peak, start/end describe the window that is COMING', () => {
  const DS = loadModule();
  const gap = DS.peakInfo(utc(5, 0));            // the 2h gap between the windows
  assert.equal(gap.inPeak, false);
  assert.equal(gap.start.toISOString(), '2026-08-17T06:00:00.000Z');
  assert.equal(gap.end.toISOString(), '2026-08-17T10:00:00.000Z');
  assert.equal(gap.msUntil, 60 * 60000);
  assert.equal(gap.frac, 0, 'nothing has elapsed of a window that has not started');
});

test('peakInfo: the next window is found across the day wrap', () => {
  // 22:00 must find TOMORROW's 01:00. Today's would make msUntil negative and the
  // indicator would announce a peak that ended 21 hours ago.
  const DS = loadModule();
  const evening = DS.peakInfo(utc(22, 0));
  assert.equal(evening.start.toISOString(), '2026-08-18T01:00:00.000Z');
  assert.equal(evening.end.toISOString(), '2026-08-18T04:00:00.000Z');
  assert.equal(evening.msUntil, 3 * 3600000);
  assert.equal(DS.peakInfo(utc(0, 30, 18)).msUntil, 30 * 60000);
});

test('peakInfo: off-peak hidden with lead 0, shown with a big lead', () => {
  const DS = loadModule();
  const at = utc(4, 15);                          // 105 min before the 06:00 window
  assert.equal(DS.peakInfo(at, 0).visible, false);
  assert.equal(DS.peakInfo(at, 90).visible, false, 'a lead shorter than the wait shows nothing');
  assert.equal(DS.peakInfo(at, 120).visible, true);
  assert.equal(DS.peakInfo(at, 120).inPeak, false);
});

test('peakInfo: the weekend is off-peak even at an hour that bills double on a weekday', () => {
  // 02:00 UTC is the middle of the first window every weekday. On Saturday and Sunday
  // it is not a window at all — DeepSeek stopped billing peak on those two days — and
  // the old rule (clock alone, weekends included) would announce a doubled bill here.
  const DS = loadModule();
  assert.equal(new Date(Date.UTC(2026, 7, 22)).getUTCDay(), 6, '2026-08-22 is a Saturday');
  assert.equal(new Date(Date.UTC(2026, 7, 23)).getUTCDay(), 0, '2026-08-23 is a Sunday');
  assert.equal(DS.peakInfo(utc(2, 0, 22)).inPeak, false);
  assert.equal(DS.peakInfo(utc(8, 0, 23)).inPeak, false);
  assert.equal(DS.peakInfo(utc(2, 0, 24)).inPeak, true, 'and Monday is back on');
});

test('peakInfo: the wait across a weekend is a real 63 hours, not a day wrap', () => {
  // Friday 10:00 closes the last window of the week; the next one opens Monday 01:00.
  // A modulo of the day length answers Saturday 01:00 here, which is 24h too early and
  // a day DeepSeek does not bill for.
  const DS = loadModule();
  const friday = DS.peakInfo(utc(10, 0, 21));
  assert.equal(friday.inPeak, false, '10:00 is the end boundary, and it belongs to what follows');
  assert.equal(friday.start.toISOString(), '2026-08-24T01:00:00.000Z');
  assert.equal(friday.end.toISOString(), '2026-08-24T04:00:00.000Z');
  assert.equal(friday.msUntil, 63 * 3600000);
});

test('peakInfo: mid-weekend the countdown still points at Monday', () => {
  // Nothing in the lead can span this gap, so the bar stays hidden at the maximum
  // lead the options page allows (240 minutes) — the row renders as if the feature
  // were not there, all weekend.
  const DS = loadModule();
  const saturday = DS.peakInfo(utc(12, 0, 22), 240);
  assert.equal(saturday.start.toISOString(), '2026-08-24T01:00:00.000Z');
  assert.equal(saturday.visible, false);
  // Sunday 23:30 is inside the lead: 90 minutes before Monday's first window.
  assert.equal(DS.peakInfo(utc(23, 30, 23), 120).visible, true);
  assert.equal(DS.peakInfo(utc(23, 30, 23), 120).start.toISOString(), '2026-08-24T01:00:00.000Z');
});

test('formatting matches the Anthropic view it sits beside', () => {
  const DS = loadModule();
  assert.equal(DS.fmtHMS(10 * 3600000 + 48 * 60000 + 12000), '10:48:12');
  assert.equal(DS.fmtHMS(-5), '0:00:00');
  assert.equal(DS.fmtLeft(3 * 3600000 + 5 * 60000), '3h 05m');
  assert.equal(DS.fmtLeft(-5), '0s');
});

// ---- the bar ---------------------------------------------------------------
// It IS anthropic-view.js's peak bar. These read the geometry out of that file
// rather than repeating the numbers, so restyling Claude's bar fails here instead of
// leaving the two rows to drift apart unnoticed.

test('buildPeakBars: built to the same spec as the Claude Code peak bar', () => {
  const DS = loadModule();
  assert.ok(ANTHROPIC_SRC.match(/width:38px; height:8px; '\s*\+\s*`border-radius:4px; background:\$\{track\}/),
    'anthropic-view.js still builds a 38x8 peak bar (update both if not)');
  assert.ok(ANTHROPIC_SRC.includes('#e67e22') && ANTHROPIC_SRC.includes('#efe0d0')
    && ANTHROPIC_SRC.includes('#ededed'), 'and still uses these three colors');

  withFakeNow('2026-08-17T02:00:00Z', () => {
    const bar = DS.buildPeakBars({ peakLeadMinutes: 0 }).children[0];
    assert.equal(bar.style.width, '38px');
    assert.equal(bar.style.height, '8px');
    assert.equal(bar.style.borderRadius, '4px');
    assert.equal(bar.style.cursor, 'help');
    assert.equal(bar.firstChild.style.background, 'rgb(230, 126, 34)', '#e67e22, the Claude fill');
  });
});

test('buildPeakBars: during peak the track is tan and the fill tracks the window', () => {
  const DS = loadModule();
  withFakeNow('2026-08-17T02:00:00Z', () => {        // 1h into a 3h window
    const bar = DS.buildPeakBars({}).children[0];
    assert.equal(bar.style.background, 'rgb(239, 224, 208)', '#efe0d0, the peak track');
    // Compared as a number: jsdom re-serializes the percentage and drops a digit.
    assert.ok(Math.abs(parseFloat(bar.firstChild.style.width) - 100 / 3) < 0.01,
      bar.firstChild.style.width);
  });
});

test('buildPeakBars: within the lead, a grey bar with an empty fill', () => {
  const DS = loadModule();
  withFakeNow('2026-08-17T04:31:00Z', () => {        // 89 min before the 06:00 window
    const bar = DS.buildPeakBars({ peakLeadMinutes: 90 }).children[0];
    assert.equal(bar.style.background, 'rgb(237, 237, 237)', '#ededed, the pre-peak track');
    assert.equal(bar.firstChild.style.width, '0%', 'like the Claude bar before peak');
    assert.match(bar.title, /^PEAK starts in 1h 29m \(from .+ till .+\)/);
  });
});

test('buildPeakBars: outside peak and its lead there is no element at all', () => {
  // Not an empty bar: the row must render as if the feature were not there, which is
  // buildUsageBars()'s contract when it has nothing to show.
  const DS = loadModule();
  withFakeNow('2026-08-17T14:00:00Z', () => {
    assert.equal(DS.buildPeakBars({ peakLeadMinutes: 0 }), null);
    assert.equal(DS.peakSummaryText({ peakLeadMinutes: 0 }), '');
  });
});

test('the tooltip opens with the Claude wording, then says what the window costs', () => {
  const DS = loadModule();
  withFakeNow('2026-08-17T02:00:00Z', () => {
    const tip = DS.buildPeakBars({}).children[0].title;
    assert.match(tip, /^PEAK from .+ till .+, 2:00:00 left/);
    assert.match(tip, /Double price until it ends\./);
  });
  withFakeNow('2026-08-17T00:30:00Z', () => {
    const tip = DS.buildPeakBars({ peakLeadMinutes: 60 }).children[0].title;
    assert.match(tip, /^PEAK starts in 30m 00s \(from .+ till .+\)/);
    assert.match(tip, /Half price until it starts\./);
  });
});

test('the tooltip gives the windows in the reader\'s own clock as well as UTC', () => {
  // Europe/Warsaw is UTC+2 here, so 01:00-04:00 UTC reads 03:00-06:00 locally.
  // Printing only UTC would make the bar a puzzle; only local would lose the source.
  const DS = loadModule();
  assert.equal(DS.localWindows(utc(14, 0)), '03:00–06:00 and 08:00–12:00');
  withFakeNow('2026-08-17T02:00:00Z', () => {
    const tip = DS.buildPeakBars({}).children[0].title;
    assert.match(tip, /03:00–06:00 and 08:00–12:00 your time/);
    assert.match(tip, /01:00–04:00 and 06:00–10:00 UTC/);
  });
});

test('peakSummaryText: the phase without the styling', () => {
  // The same three answers AnthropicView.usageSummaryText gives for its peak part.
  const DS = loadModule();
  withFakeNow('2026-08-17T02:00:00Z', () => assert.equal(DS.peakSummaryText({}), 'PEAK 2:00:00 left'));
  withFakeNow('2026-08-17T00:30:00Z', () => {
    assert.equal(DS.peakSummaryText({ peakLeadMinutes: 60 }), 'PEAK in 30m 00s');
    assert.equal(DS.peakSummaryText({}), '');
  });
});

test('the bar belongs to DSH rows and to nothing else', () => {
  const DS = loadModule();
  assert.equal(DS.isDeepSeekHarness({ kind: 'mcp_client', friendly: 'DSH' }), true);
  assert.equal(DS.isDeepSeekHarness({ kind: 'mcp_client', label: 'DSH — C:\\dr' }), true);
  // The long spelling is what a server older than the rename sends; a popup ahead of
  // its server must not drop the bar (the two version-skew independently).
  assert.equal(DS.isDeepSeekHarness({ kind: 'mcp_client', friendly: 'DeepSeek Harness' }), true);
  for (const other of [
    { kind: 'mcp_client', friendly: 'Claude Code' },
    { kind: 'mcp_client', friendly: 'Kilo CLI', provider: 'deepseek', model: 'deepseek-chat' },
    { kind: 'extension', label: 'DSH' },
    null,
  ]) assert.equal(DS.isDeepSeekHarness(other), false, JSON.stringify(other));
});
