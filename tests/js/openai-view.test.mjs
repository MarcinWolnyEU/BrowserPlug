// Unit tests for extension-gecko/vendor/openai-view.js (jsdom, no browser).
// Run: node --test tests/js/openai-view.test.mjs   (or: npm run test:js)
//
// This bar IS anthropic-view.js's bar, reading the same kind of fact one card along, so
// the geometry cases read their expectations out of THAT file rather than repeating the
// numbers: restyling Claude's bar fails here instead of leaving the two rows to drift
// apart unnoticed. The rest is what OpenAI has and Anthropic does not — an exhausted
// state the API reports directly, and a signed-out state that has to say so out loud.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/openai-view.js'), 'utf8');
const ANTHROPIC_SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/anthropic-view.js'), 'utf8');

function loadModule() {
  const dom = new JSDOM('<!doctype html><body></body>');
  const g = dom.window;
  g.globalThis = g;
  // eslint-disable-next-line no-new-func
  new Function('window', 'document', 'globalThis', SRC)(g, g.document, g);
  return g.OpenAIView;
}

const HOUR = 3600e3;
const NOW = Date.UTC(2026, 8, 9, 12, 0, 0);

// The countdowns are (resetsAt - Date.now()), so on the live clock a fixture built at
// +2h renders as "1h 59m" by the time the assertion reads it. Pin the clock instead.
function withFakeNow(fn) {
  const RealDate = Date;
  class FakeDate extends RealDate {
    constructor(...args) { if (args.length === 0) super(NOW); else super(...args); }
    static now() { return NOW; }
  }
  globalThis.Date = FakeDate;
  try { return fn(); } finally { globalThis.Date = RealDate; }
}

const usage = (over = {}) => Object.assign({
  ok: true,
  session: { percent: 42, resetsAt: new Date(NOW + 2 * HOUR).toISOString(), windowName: '5-hour' },
  weekly: { percent: 9, resetsAt: new Date(NOW + 72 * HOUR).toISOString(), windowName: 'weekly' },
  plan: 'plus',
  limitReached: false,
}, over);

test('the bar rides Codex CLI rows and nothing else', () => {
  const OV = loadModule();
  assert.equal(OV.isOpenAIClient({ kind: 'mcp_client', friendly: 'Codex CLI' }), true);
  assert.equal(OV.isOpenAIClient({ kind: 'mcp_client', label: 'Codex CLI — C:\\dr' }), true);
  for (const other of [
    { kind: 'mcp_client', friendly: 'Claude Code' },
    { kind: 'mcp_client', friendly: 'DSH' },
    { kind: 'mcp_client', friendly: 'GitHub Copilot Chat' },
    { kind: 'extension', label: 'Codex CLI' },
    null,
  ]) assert.equal(OV.isOpenAIClient(other), false, JSON.stringify(other));
});

test('built to the same spec as the Claude Code usage bar', () => {
  const OV = loadModule();
  assert.ok(ANTHROPIC_SRC.includes("width:38px; height:8px; "),
    'anthropic-view.js still builds a 38x8 usage bar (update both if not)');
  assert.ok(ANTHROPIC_SRC.includes("(p >= 80 ? '#f44336' : (p >= 50 ? '#f0ad4e' : '#4CAF50'))"),
    'and still uses this three-step colour ramp');

  const bar = OV.buildUsageBars(usage(), {}).children[0];
  assert.equal(bar.style.width, '38px');
  assert.equal(bar.style.height, '8px');
  assert.equal(bar.style.borderRadius, '4px');
  assert.equal(bar.firstChild.style.width, '42%');
  assert.equal(bar.firstChild.style.background, 'rgb(76, 175, 80)', '#4CAF50: 42% is below the amber step');
});

test('showWeekly splits the bar the same way, top short window / bottom weekly', () => {
  const OV = loadModule();
  const single = OV.buildUsageBars(usage(), {}).children[0];
  assert.equal(single.childElementCount, 1, 'one fill by default');

  const split = OV.buildUsageBars(usage(), { showWeekly: true }).children[0];
  assert.equal(split.childElementCount, 3, 'top, bottom, divider');
  assert.equal(split.children[0].style.width, '42%');
  assert.equal(split.children[1].style.width, '9%');
});

test('an exhausted allowance is red, not a full green bar', () => {
  // Anthropic has no equivalent: there a full bar means "at the cap", here the API is
  // already refusing requests, and the two should not look alike.
  const OV = loadModule();
  const bar = OV.buildUsageBars(usage({ limitReached: true, session: { percent: 100, windowName: '5-hour' } }), {}).children[0];
  assert.equal(bar.style.background, 'rgb(247, 212, 209)', '#f7d4d1, the exhausted track');
  assert.equal(bar.firstChild.style.background, 'rgb(244, 67, 54)', '#f44336');
  assert.match(bar.title, /Limit reached/);
});

test('the tooltip names the window by its real length and says who shares it', () => {
  const OV = loadModule();
  const tip = withFakeNow(() => OV.buildUsageBars(usage(), { showWeekly: true }).children[0].title);
  assert.match(tip, /^5-hour 42% · resets in 2h 00m/m);
  assert.match(tip, /Weekly 9% · resets in 3d 0h/);
  assert.match(tip, /Plan: plus/);
  // The single most surprising thing about these numbers, so it is always on screen.
  assert.match(tip, /ChatGPT and Codex share this allowance\./);
});

test('no usage means no element — the row renders as if the feature were not there', () => {
  const OV = loadModule();
  assert.equal(OV.buildUsageBars(null, {}), null);
  assert.equal(OV.buildUsageBars({ ok: false, error: 'HTTP 500' }, {}), null);
});

test('usageSummaryText: the panel without the styling, for the copy path', () => {
  const OV = loadModule();
  assert.match(withFakeNow(() => OV.usageSummaryText(usage(), {})), /^5-hour 42% \(resets in 2h 00m\)$/);
  assert.match(withFakeNow(() => OV.usageSummaryText(usage(), { showWeekly: true })), /· weekly 9% \(resets in 3d 0h\)/);
  assert.match(OV.usageSummaryText(usage({ limitReached: true }), {}), /· LIMIT REACHED$/);
  assert.equal(OV.usageSummaryText(null, {}), '');
});

test('a signed-out profile says so, rather than reading as a network fault', () => {
  const OV = loadModule();
  const out = { ok: false, signedOut: true, error: 'not signed in to chatgpt.com' };
  assert.equal(OV.usageSummaryText(out, {}), 'not signed in to chatgpt.com');
  assert.match(OV.usageSummaryText({ ok: false, error: 'HTTP 500' }, {}), /^usage n\/a \(HTTP 500\)$/);
});
