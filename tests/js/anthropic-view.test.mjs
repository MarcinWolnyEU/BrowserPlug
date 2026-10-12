// Unit tests for extension-gecko/vendor/anthropic-view.js (jsdom, no Firefox).
// Run: node --test tests/js/anthropic-view.test.mjs   (or: npm run test:js)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/anthropic-view.js'), 'utf8');
const CHART_SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/usage-chart.js'), 'utf8');

// Freeze the clock: the module reads the ambient `Date`, so overriding the global
// makes its internal `new Date()` / `Date.now()` deterministic.
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
  new Function('window', 'document', 'globalThis', CHART_SRC)(g, g.document, g);
  // eslint-disable-next-line no-new-func
  new Function('window', 'document', 'globalThis', SRC)(g, g.document, g);
  return g.AnthropicView;
}

test('peakInfo: mid-peak weekday is inPeak at 50%', () => {
  const AU = loadModule();
  const p = AU.peakInfo(new Date('2026-07-02T15:00:00Z'), 0); // Thu 15:00 UTC
  assert.equal(p.inPeak, true);
  assert.equal(p.visible, true);
  assert.ok(Math.abs(p.frac - 0.5) < 1e-9);
});

test('peakInfo: off-peak hidden with lead 0, shown with a big lead', () => {
  const AU = loadModule();
  const at = new Date('2026-07-02T19:00:00Z');
  assert.equal(AU.peakInfo(at, 0).visible, false);
  assert.equal(AU.peakInfo(at, 20 * 60).visible, true);
  assert.equal(AU.peakInfo(at, 0).inPeak, false);
});

test('peakInfo: lead window just before peak is visible but not inPeak', () => {
  const AU = loadModule();
  const p = AU.peakInfo(new Date('2026-07-02T11:30:00Z'), 60);
  assert.equal(p.inPeak, false);
  assert.equal(p.visible, true);
});

test('peakInfo: weekend skips to Monday for the next peak', () => {
  const AU = loadModule();
  const p = AU.peakInfo(new Date('2026-07-04T15:00:00Z'), 0); // Saturday
  assert.equal(p.inPeak, false);
  assert.equal(p.start.getUTCDay(), 1); // Monday
});

test('buildUsageBars: usage bar + peak bar during peak; null when no data', () => {
  const AU = loadModule();
  const usage = { session: { percent: 47, resetsAt: '2026-07-02T15:50:00Z' }, weekly: { percent: 10 } };
  withFakeNow('2026-07-02T15:00:00Z', () => {
    const el = AU.buildUsageBars(usage, { showWeekly: false, peakLeadMinutes: 0 });
    assert.ok(el);
    assert.equal(el.children.length, 2);   // usage + peak
  });
  withFakeNow('2026-07-02T19:00:00Z', () => {
    const el = AU.buildUsageBars(usage, { showWeekly: false, peakLeadMinutes: 0 });
    assert.equal(el.children.length, 1);   // usage only
    // no usage data AND off-peak -> nothing to render at all
    assert.equal(AU.buildUsageBars(null, { peakLeadMinutes: 0 }), null);
    assert.equal(AU.buildUsageBars({}, { peakLeadMinutes: 0 }), null);
  });
});

test('buildUsageBars: PEAK renders without usage data (signed-out profile)', () => {
  // Peak hours are pure local-clock math — a profile that cannot fetch usage
  // (not signed in to claude.ai) must still get the PEAK indicator (issue 5).
  const AU = loadModule();
  withFakeNow('2026-07-02T15:00:00Z', () => {
    for (const usage of [null, {}, { ok: false, error: 'HTTP 403' }]) {
      const el = AU.buildUsageBars(usage, { peakLeadMinutes: 0 });
      assert.ok(el, `expected a peak-only bar for ${JSON.stringify(usage)}`);
      assert.equal(el.children.length, 1);           // just the peak bar
      assert.match(el.children[0].title, /^PEAK /);  // it IS the peak bar
    }
  });
});

test('buildUsageBars: showWeekly splits the bar into two fills', () => {
  const AU = loadModule();
  const el = AU.buildUsageBars({ session: { percent: 47 }, weekly: { percent: 10 } }, { showWeekly: true, peakLeadMinutes: 0 });
  const bar = el.children[0];
  assert.equal(bar.children.length, 3);    // top + bottom + divider
  assert.match(bar.title, /Session 47%/);
  assert.match(bar.title, /Weekly 10%/);
});

test('usageSummaryText mirrors the bar as text', () => {
  const AU = loadModule();
  const usage = { session: { percent: 47, resetsAt: '2030-01-01T00:00:00Z' }, weekly: { percent: 10 } };
  const s = AU.usageSummaryText(usage, { showWeekly: true, peakLeadMinutes: 0 });
  assert.match(s, /^session 47%/);
  assert.match(s, /weekly 10%/);
});

test('usageSummaryText: "usage n/a" on a failed fetch; PEAK survives without data', () => {
  const AU = loadModule();
  withFakeNow('2026-07-02T15:00:00Z', () => {  // Thu, mid-peak
    // fetch failed (e.g. signed out) -> the copy text says so, marker first
    const s = AU.usageSummaryText({ ok: false, error: 'HTTP 403' }, { peakLeadMinutes: 0 });
    assert.match(s, /^usage n\/a \(HTTP 403\) · PEAK /);
    // no usage object at all -> peak-only text
    assert.match(AU.usageSummaryText(null, { peakLeadMinutes: 0 }), /^PEAK /);
  });
  withFakeNow('2026-07-02T19:00:00Z', () => {  // off-peak, lead 0
    assert.equal(AU.usageSummaryText(null, { peakLeadMinutes: 0 }), '');
    assert.equal(AU.usageSummaryText({ ok: false, error: 'x' }, { peakLeadMinutes: 0 }), 'usage n/a (x)');
  });
});

test('buildUsageChart renders an svg with a window; placeholder without one', () => {
  const AU = loadModule();
  const end = Date.parse('2026-07-02T15:50:00Z');
  const usage = {
    session: { percent: 47 },
    sessionWindow: { start: end - 5 * 3600e3, end },
    history: [
      { t: end - 5 * 3600e3 + 1000, session: 5 },
      { t: end - 60e3, session: 47 },
    ],
  };
  const el = AU.buildUsageChart(usage, { showWeekly: true });
  assert.equal(el.querySelectorAll('svg').length, 1, 'no weekly data: only the session chart');
  assert.ok(el.querySelectorAll('polyline').length >= 1);
  const empty = AU.buildUsageChart({ session: { percent: 1 }, history: [] }, {});
  assert.equal(empty.querySelectorAll('svg').length, 0);
});

test('buildUsageChart: session and weekly share one plot, each line on its own time axis', () => withFakeNow('2026-07-02T14:00:00Z', () => {
  const AU = loadModule();
  const end = Date.parse('2026-07-02T15:50:00Z');
  const weekEnd = Date.parse('2026-07-06T09:00:00Z');
  const usage = {
    session: { percent: 47 },
    weekly: { percent: 10 },
    sessionWindow: { start: end - 5 * 3600e3, end },
    history: [{ t: end - 60e3, session: 47 }],
    weeklyWindow: { start: weekEnd - 7 * 24 * 3600e3, end: weekEnd },
    weeklyHistory: [{ t: end - 3600e3, weekly: 9 }, { t: end - 60e3, weekly: 10 }],
  };
  const el = AU.buildUsageChart(usage, { showWeekly: true });
  assert.equal(el.querySelectorAll('svg').length, 1, 'one plot');
  const lines = [...el.querySelectorAll('polyline')];
  assert.equal(lines.length, 2);
  const blue = lines.find(l => l.getAttribute('stroke').includes('accent'));
  const green = lines.find(l => l.getAttribute('stroke') === '#43a047');
  assert.ok(blue && green, 'session in blue, week in the logo green');
  const weekly = green.getAttribute('points').split(' ');
  // 1 h apart on a 7-day axis is ~0.6% of the width; on the 5-hour axis it would be 20%.
  const dx = Number(weekly[1].split(',')[0]) - Number(weekly[0].split(',')[0]);
  assert.ok(dx > 0 && dx < 3, `weekly samples spread over the 7-day axis, dx=${dx}`);
  const axes = [...el.querySelectorAll('.usage-axis')];
  assert.equal(axes.length, 2, 'one axis row per line');
  assert.match(axes[0].firstChild.textContent, /^\d{1,2}:\d{2}/, 'session labels read HH:MM');
  assert.match(axes[1].firstChild.textContent, /^\w{3} \d{1,2}:\d{2}/, 'week labels read Day HH:MM');
  assert.equal(AU.buildUsageChart(usage, { showWeekly: false }).querySelectorAll('polyline').length, 1);
}));

test('buildUsageChart: axes print "-" and drop the now marker while now is outside the window', () => {
  const end = Date.parse('2026-07-02T15:50:00Z');
  const weekEnd = Date.parse('2026-07-06T09:00:00Z');
  const usage = {
    session: { percent: 0 },
    weekly: { percent: 10 },
    sessionWindow: { start: end - 5 * 3600e3, end },
    history: [{ t: end - 60e3, session: 47 }],
    weeklyWindow: { start: weekEnd - 7 * 24 * 3600e3, end: weekEnd },
    weeklyHistory: [{ t: end - 3600e3, weekly: 9 }],
  };
  // Session ended 8 h ago and no new one started; the week is still running.
  withFakeNow('2026-07-02T23:50:00Z', () => {
    const AU = loadModule();
    const el = AU.buildUsageChart(usage, { showWeekly: true });
    const axes = [...el.querySelectorAll('.usage-axis')];
    assert.equal(axes.length, 2);
    assert.equal(axes[0].firstChild.textContent, '-');
    assert.equal(axes[0].lastChild.textContent, '-');
    assert.match(axes[1].firstChild.textContent, /^\w{3} \d{1,2}:\d{2}/, 'the running week keeps its labels');
    assert.equal(el.querySelectorAll('svg line[stroke-dasharray="2 2"]').length, 1, 'only the week has a now marker');
  });
  // Before the window starts counts as outside too.
  withFakeNow('2026-07-02T09:00:00Z', () => {
    const AU = loadModule();
    const el = AU.buildUsageChart(usage, { showWeekly: false });
    const ax = el.querySelector('.usage-axis');
    assert.equal(ax.firstChild.textContent, '-');
    assert.equal(ax.lastChild.textContent, '-');
  });
});

// Exact peak-window boundaries. The window is weekdays 12:00–18:00 UTC and
// half-open [start, end): inPeak === (now >= start && now < end), with
// frac === (now - start) / (end - start). 2026-07-02 is a Thursday (weekday).
test('peakInfo: start boundary is inPeak (frac 0); one second before end still inPeak (frac ~1)', () => {
  const AU = loadModule();
  const start = new Date('2026-07-02T12:00:00Z'); // Thu, exact peak start
  assert.equal(start.getUTCDay(), 4);             // sanity: Thursday is a weekday
  const ps = AU.peakInfo(start, 0);
  assert.equal(ps.inPeak, true);
  assert.equal(ps.visible, true);
  assert.ok(Math.abs(ps.frac - 0) < 1e-9);

  const nearEnd = AU.peakInfo(new Date('2026-07-02T17:59:59Z'), 0);
  assert.equal(nearEnd.inPeak, true);
  assert.ok(nearEnd.frac > 0.999 && nearEnd.frac < 1);
});

test('peakInfo: end boundary is off-peak (half-open); one second before start not yet inPeak', () => {
  const AU = loadModule();
  const end = new Date('2026-07-02T18:00:00Z');   // Thu, exact peak end
  assert.equal(end.getUTCDay(), 4);
  const pe = AU.peakInfo(end, 0);
  assert.equal(pe.inPeak, false);   // end is excluded from [start, end)
  assert.equal(pe.visible, false);  // next peak ~18h away, lead 0 → hidden

  const beforeStart = AU.peakInfo(new Date('2026-07-02T11:59:59Z'), 0);
  assert.equal(beforeStart.inPeak, false);
});
