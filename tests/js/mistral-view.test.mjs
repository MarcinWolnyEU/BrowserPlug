// Unit tests for extension-gecko/vendor/mistral-view.js (jsdom, no browser).
// Run: node --test tests/js/mistral-view.test.mjs   (or: npm run test:js)
//
// Three things matter here. The row is found by the display name the server prints
// ("Mistral Vibe", server/reporting/client_identity.py), so a rename there must turn this
// red. The text says what is spent of an allowance in money, and says "over budget" when
// pay-as-you-go carries usage past 100 %. The bar never draws wider than its track, however
// far past the budget the percentage goes. And the reading's two allowances (Vibe & Studio
// at the top level, API in `usage.api`) go through the same helpers and reach the tooltip,
// the chart and the Options line.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/mistral-view.js'), 'utf8');
const CLIENT_IDENTITY = readFileSync(path.join(__dirname, '../../server/reporting/client_identity.py'), 'utf8');

const CHART_SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/usage-chart.js'), 'utf8');

// `chart` also loads usage-chart.js, as popup.html does before the view files.
function loadModule({ chart = false } = {}) {
  const dom = new JSDOM('<!doctype html><body></body>');
  const g = dom.window;
  g.globalThis = g;
  // eslint-disable-next-line no-new-func
  if (chart) new Function('window', 'document', 'globalThis', CHART_SRC)(g, g.document, g);
  // eslint-disable-next-line no-new-func
  new Function('window', 'document', 'globalThis', SRC)(g, g.document, g);
  return g.MistralView;
}

const DAY = 86400e3;
// The live reading of 2026-11-26, six days before the reset (the reset a little over 5
// days out so the countdown does not sit on a boundary).
const RESETS_AT = () => new Date(Date.now() + 5 * DAY + 14 * 3600e3 + 60e3).toISOString();
const usage = (over = {}) => Object.assign({
  ok: true, accountId: 'abc', plan: 'Pro', percent: 0.13369414333333332, budget: 255, spent: 0.3409, currency: 'EUR',
  resetsAt: RESETS_AT(),
  api: { percent: 0.010619, budget: 25.5, spent: 0.0027, currency: 'EUR', resetsAt: RESETS_AT() },
}, over);

test('the Mistral Vibe row is recognised by the display name the server prints', () => {
  const MV = loadModule();
  assert.equal(MV.isMistralClient({ kind: 'mcp_client', friendly: 'Mistral Vibe' }), true);
  assert.equal(MV.isMistralClient({ kind: 'mcp_client', label: 'Mistral Vibe — C:\\work' }), true,
    'the label prefix is accepted for a popup ahead of its server');
  assert.equal(MV.isMistralClient({ kind: 'mcp_client', friendly: 'Mistral' }), false);
  assert.equal(MV.isMistralClient({ kind: 'extension', friendly: 'Mistral Vibe' }), false);
  assert.equal(MV.isMistralClient(null), false);
  // The coupling the dev doc warns about, held on the Python side too.
  assert.match(CLIENT_IDENTITY, /return "Mistral Vibe"/);
});

test('the text names the money spent, the budget, the percentage and the reset', () => {
  const MV = loadModule();
  assert.equal(MV.usageText(usage()), '€0.34 of €255 · 0.1% · resets in 5d 14h');
  assert.equal(MV.usageText(usage({ percent: 12, spent: 30.6, budget: 255 })), '€30.60 of €255 · 12% · resets in 5d 14h');
  assert.equal(MV.usageText(usage({ currency: 'USD', budget: 1500.5, spent: 0.5 })),
    'USD 0.50 of USD 1,500.50 · 0.1% · resets in 5d 14h', 'another currency is named by its ISO code');
});

test('without a budget figure the text keeps the percentage, and without a date no reset', () => {
  const MV = loadModule();
  assert.equal(MV.usageText(usage({ budget: null, spent: null, resetsAt: null })), '0.1%');
});

test('a reset date that does not parse prints no reset text, not "resets in 0s"', () => {
  const MV = loadModule();
  for (const resetsAt of ['soon', '2026-13-45', NaN, {}]) {
    assert.equal(MV.resetLeft(usage({ resetsAt })), '', String(resetsAt));
    assert.equal(MV.usageText(usage({ resetsAt })), '€0.34 of €255 · 0.1%', String(resetsAt));
  }
});

test('over budget: the text says so, and the bar stops at the full width', () => {
  const MV = loadModule();
  const over = usage({ percent: 121.6, spent: 310.08 });
  assert.equal(MV.overBudget(over), true);
  assert.equal(MV.usageText(over), '€310.08 of €255 · 121.6% · over budget · resets in 5d 14h');
  const bar = MV.makeUsageBar(over);
  assert.equal(bar.children[0].style.width, '100%', 'not 121.6%');
  assert.equal(bar.children[0].style.background, 'rgb(244, 67, 54)', 'red, like every other nearly-full bar');
  assert.doesNotMatch(bar.title, /Pay-as-you-go/, 'no flag is reported any more, and over budget is in the text');
  assert.equal(MV.overBudget(usage({ percent: 100 })), false, 'exactly the budget is not over it');
});

test('the bar is the same 38px widget as the other providers, in the same colours', () => {
  const MV = loadModule();
  const bar = MV.makeUsageBar(usage({ percent: 12 }));
  assert.equal(bar.style.width, '38px');
  assert.equal(bar.children[0].style.width, '12%');
  assert.equal(bar.children[0].style.background, 'rgb(76, 175, 80)', 'green below 50%');
  assert.equal(MV.makeUsageBar(usage({ percent: 60 })).children[0].style.background, 'rgb(240, 173, 78)');
  assert.match(bar.title, /^Mistral Pro\nVibe & Studio: €0\.34 of €255 · 12% · resets in/);
  assert.match(MV.makeUsageBar(usage({ stale: true })).title, /Last refresh failed; this is the previous reading\./);
});

test('the tooltip names the plan, both allowances and the stale line, in that order', () => {
  const MV = loadModule();
  assert.deepEqual(MV.makeUsageBar(usage({ stale: true })).title.split('\n'), [
    'Mistral Pro',
    'Vibe & Studio: €0.34 of €255 · 0.1% · resets in 5d 14h',
    'API: €0.00 of €25.50 · 0% · resets in 5d 14h',
    'Last refresh failed; this is the previous reading.',
  ]);
  // No plan, no API: just the Vibe line, as before the second round.
  assert.equal(MV.makeUsageBar(usage({ plan: null, api: null })).title,
    'Vibe & Studio: €0.34 of €255 · 0.1% · resets in 5d 14h');
  assert.equal(MV.makeUsageBar(usage({ plan: undefined, api: undefined })).title.split('\n').length, 1);
  // The API line goes through the same text, so it says "over budget" too.
  const api = { percent: 120, budget: 25.5, spent: 30.6, currency: 'EUR', resetsAt: RESETS_AT() };
  assert.match(MV.makeUsageBar(usage({ api })).title, /API: €30\.60 of €25.50 · 120% · over budget · resets in 5d 14h/);
  assert.equal(MV.overBudget(api), true);
});

test('the row carries the bar, or a marker when the reading failed, or nothing before a reading', () => {
  const MV = loadModule();
  const el = MV.buildRowInline(usage());
  assert.equal(el.children.length, 1);
  assert.equal(el.children[0].style.width, '38px');
  assert.equal(MV.buildRowInline(null), null, 'no reading yet: the row is plain text, with no gap');

  const out = MV.buildRowInline({ ok: false, signedOut: true, error: 'not signed in to chat.mistral.ai' });
  assert.equal(out.className, 'conn-na');
  assert.equal(out.textContent, 'signed out');
  assert.match(out.title, /No chat\.mistral\.ai session/);
  const bad = MV.buildRowInline({ ok: false, error: 'HTTP 500' });
  assert.equal(bad.textContent, 'usage n/a');
  assert.match(bad.title, /HTTP 500/);
});

test('the copy text says what the row shows', () => {
  const MV = loadModule();
  assert.equal(MV.rowSummaryText(usage()), '€0.34 of €255 · 0.1% · resets in 5d 14h', 'the row is a Vibe client: no API text');
  assert.equal(MV.rowSummaryText({ ok: false, signedOut: true }), 'not signed in to chat.mistral.ai');
  assert.equal(MV.rowSummaryText({ ok: false, error: 'HTTP 500' }), 'usage n/a (HTTP 500)');
  assert.equal(MV.rowSummaryText(null), '');
});

test('the chart draws Vibe & Studio in violet and API in teal, and is absent until a window exists', () => {
  const end = Date.now() + 5 * DAY;
  const win = { start: end - 30 * DAY, end };
  const pts = (key, a, b) => [{ t: Date.now() - 3600e3, [key]: a }, { t: Date.now(), [key]: b }];
  const withWindows = usage({
    monthlyWindow: win, monthlyHistory: pts('monthly', 0.1, 0.13),
    apiWindow: win, apiHistory: pts('api', 0.01, 0.011),
  });
  assert.equal(loadModule().buildUsageChart(withWindows), null, 'without the chart module');
  const MV = loadModule({ chart: true });
  assert.equal(MV.buildUsageChart(usage()), null, 'a reading with no window draws no chart');
  const chart = MV.buildUsageChart(withWindows);
  assert.deepEqual([...chart.querySelectorAll('polyline')].map(p => p.getAttribute('stroke')), ['#8e5fd0', '#00897b']);
  assert.match(chart.firstChild.textContent, /● Vibe & Studio/);
  assert.match(chart.firstChild.textContent, /● API/);
  assert.doesNotMatch(chart.firstChild.textContent, /Code completions|Month\b/);
  assert.equal(chart.querySelectorAll('.usage-axis').length, 1, 'the same month: one shared axis row');
  // Each line is plotted from its own history (the 50 % API sample sits above the 10 % Vibe one).
  const apart = MV.buildUsageChart(usage({
    monthlyWindow: win, monthlyHistory: pts('monthly', 10, 10), apiWindow: win, apiHistory: pts('api', 50, 50),
  }));
  const lastY = (poly) => Number(poly.getAttribute('points').split(' ').pop().split(',')[1]);
  const [vibeLine, apiLine] = apart.querySelectorAll('polyline');
  assert.ok(lastY(apiLine) < lastY(vibeLine), 'smaller y is higher up the plot');

  // No API allowance in the reading: the Vibe line alone.
  const vibeOnly = MV.buildUsageChart(usage({
    api: null, monthlyWindow: win, monthlyHistory: pts('monthly', 0.1, 0.13), apiWindow: win, apiHistory: pts('api', 1, 2),
  }));
  assert.deepEqual([...vibeOnly.querySelectorAll('polyline')].map(p => p.getAttribute('stroke')), ['#8e5fd0']);
  assert.doesNotMatch(vibeOnly.firstChild.textContent, /API/);
});

test('authState: signed in quotes the usage; signed out and failures offer the sign-in button', () => {
  const MV = loadModule();
  const ok = MV.authState(usage(), null);
  assert.equal(ok.tone, 'ok');
  assert.equal(ok.text, 'Signed in to chat.mistral.ai in this browser (Pro plan).');
  assert.match(ok.help, /Vibe & Studio: €0\.34 of €255 · 0\.1% · resets in 5d 14h; API: €0\.00 of €25\.50 · 0%/);
  assert.match(ok.help, /included allowances admin\.mistral\.ai shows under Subscription/);
  assert.equal(ok.signIn, false);
  assert.equal(MV.authState(usage({ plan: null }), null).text, 'Signed in to chat.mistral.ai in this browser.', 'no plan: no brackets');
  const noApi = MV.authState(usage({ api: null }), null);
  assert.doesNotMatch(noApi.help, /API:/);
  const out = MV.authState({ ok: false, signedOut: true }, null);
  assert.deepEqual([out.tone, out.signIn], ['warn', true]);
  assert.match(out.help, /Mistral Vibe row shows "signed out"/);
  const bad = MV.authState(null, 'the extension did not answer');
  assert.equal(bad.tone, 'bad');
  assert.match(bad.help, /the extension did not answer/);
  const stale = MV.authState(usage({ stale: true, error: 'HTTP 502' }), null);
  assert.deepEqual([stale.tone, stale.signIn], ['warn', true]);
  assert.match(stale.help, /HTTP 502/);
  assert.match(stale.help, /admin\.mistral\.ai did not answer/);
  assert.match(bad.help, /from admin\.mistral\.ai failed/);
  assert.match(out.text, /Not signed in to chat\.mistral\.ai/, 'sign-in still happens at chat.mistral.ai');
});
