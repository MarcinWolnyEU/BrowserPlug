// The popup's Usage tab (extension-gecko/popup/usage-pane.js) under jsdom, drawn from
// the real view modules with a fixed clock. What it pins: one card per provider, a card
// that says something in EVERY state it is drawn in (waiting, failed, numbers), the
// meters carrying the percentages the views report, and the peak clocks agreeing with
// the views' own peakInfo. Two things draw NOTHING: a provider turned off, and an OpenAI
// or Copilot account this browser is signed out of. A signed-out Anthropic card keeps
// its peak clock, and Copilot on another GitHub account than the editor draws a card
// without numbers that says so; popup.js takes the whole tab away when every provider
// is off. All shared files, so one run covers both trees.
// Run: node --test tests/js/usage-pane.test.mjs   (or: npm run test:js)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const read = (rel) => readFileSync(path.join(__dirname, '../../extension-gecko', rel), 'utf8');
const POPUP_HTML = read('popup/popup.html');
const VIEW_FILES = ['vendor/usage-chart.js', 'vendor/anthropic-view.js', 'vendor/openai-view.js', 'vendor/copilot-view.js',
  'vendor/deepseek-view.js'];
const VIEWS = Object.fromEntries(VIEW_FILES.map(f => [f, read(f)]));
const PANE_JS = read('popup/usage-pane.js');

// Fixed instants against the two schedules: Anthropic peaks weekdays 12:00-18:00 UTC,
// DeepSeek 01:00-04:00 and 06:00-10:00 UTC Monday to Friday. 2026-08-17 is a Monday.
const DS_PEAK = Date.UTC(2026, 7, 17, 2, 0, 0);        // DeepSeek in peak, Anthropic off-peak
const ANTHROPIC_PEAK = Date.UTC(2026, 7, 17, 14, 0, 0); // the other way round

function load(now, skip = []) {
  const dom = new JSDOM(POPUP_HTML, { runScripts: 'outside-only' });
  const { window } = dom;
  const Real = window.Date;
  window.Date = class extends Real {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  };
  for (const f of VIEW_FILES) if (!skip.includes(f)) window.eval(VIEWS[f]);
  window.eval(PANE_JS);
  const host = window.document.getElementById('usagePane');
  return { window, document: window.document, host, render: (state) => window.UsagePane.render(host, state) };
}

const cards = (host) => [...host.querySelectorAll('.card')];
const cardTitled = (host, title) => cards(host).find(c => c.querySelector('h2').textContent === title);
const pill = (card) => card.querySelector('.card-head .state');
const meters = (card) => [...card.querySelectorAll('.meter')].map(m => ({
  label: m.querySelector('.lbl b').textContent,
  right: m.querySelector('.lbl span').textContent,
  width: m.querySelector('.fill').style.width,
  hot: m.querySelector('.fill').classList.contains('hot'),
  peak: m.querySelector('.fill').classList.contains('peak'),
}));

// A reading whose windows reset two hours and three days after `now`.
const usage = (session, weekly, now = DS_PEAK) => ({
  ok: true,
  session: { percent: session, resetsAt: new Date(now + 2 * 3600e3).toISOString() },
  weekly: weekly == null ? null : { percent: weekly, resetsAt: new Date(now + 3 * 86400e3).toISOString() },
});

test('one card per provider, in the order Anthropic, OpenAI, GitHub Copilot, DeepSeek', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ anthropic: { mode: 'client', usage: null, opts: {} },
             openai: { mode: 'client', usage: null, opts: {} },
             copilot: { mode: 'tab', usage: null, opts: {}, rows: [] },
             deepseek: { mode: 'client', opts: {} } });
    assert.deepEqual(cards(host).map(c => c.querySelector('h2').textContent),
      ['Anthropic', 'OpenAI', 'GitHub Copilot', 'DeepSeek']);
    for (const c of cards(host)) {
      assert.ok(c.textContent.trim().length > 20, `${c.querySelector('h2').textContent}: a card is never blank`);
    }
  } finally {
    window.close();
  }
});

test('a provider set to Disabled draws no card at all', () => {
  // Not an "off" card. A card whose only content is that it is off is furniture the
  // reader has to look past on every open; the setting hides it, the way it says it will.
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ anthropic: { mode: 'disabled', usage: usage(34, 61), opts: {} },
             openai: { mode: 'client', usage: null, opts: {} },
             deepseek: { mode: 'disabled', opts: {} } });
    assert.deepEqual(cards(host).map(c => c.querySelector('h2').textContent), ['OpenAI'],
      'only a provider that is on draws anything — a reading already in hand does not '
      + 'earn a disabled provider a card');
  } finally {
    window.close();
  }
});

test('every provider off leaves the pane empty, for popup.js to take the tab away', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ anthropic: { mode: 'disabled', usage: null, opts: {} },
             openai: { mode: 'disabled', usage: null, opts: {} },
             copilot: { mode: 'disabled', usage: null, opts: {}, rows: [] },
             deepseek: { mode: 'disabled', opts: {} } });
    assert.equal(cards(host).length, 0);
  } finally {
    window.close();
  }
});

test('Anthropic: the two windows become meters, the week only when reported', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ anthropic: { mode: 'client', usage: usage(34, 61), opts: {} } });
    const c = cardTitled(host, 'Anthropic');
    assert.equal(pill(c).textContent, 'signed in');
    const m = meters(c);
    assert.deepEqual(m.map(x => [x.label, x.width, x.hot]), [['Session', '34%', false], ['Week', '61%', false]]);
    assert.match(m[0].right, /^34% · resets in 2h 00m$/);
    assert.match(m[1].right, /^61% · resets in 3d 0h$/);
    // Off-peak at 02:00 UTC: the next peak is today's 12:00, ten hours away.
    assert.match(c.textContent, /Off-peak · next PEAK in 10h 00m/);

    render({ anthropic: { mode: 'client', usage: usage(85, null), opts: {} } });
    const only = meters(cardTitled(host, 'Anthropic'));
    assert.deepEqual(only.map(x => [x.label, x.width, x.hot]), [['Session', '85%', true]],
      'no weekly window reported, no Week meter; 85% is past the red step');
  } finally {
    window.close();
  }
});

test('Anthropic: during peak hours the clock is a filling meter, and the chart follows a window', () => {
  const { window, host, render } = load(ANTHROPIC_PEAK);
  try {
    const u = { ...usage(10, 20, ANTHROPIC_PEAK),
      sessionWindow: { start: ANTHROPIC_PEAK - 3600e3, end: ANTHROPIC_PEAK + 4 * 3600e3 },
      history: [{ t: ANTHROPIC_PEAK - 1800e3, session: 5 }, { t: ANTHROPIC_PEAK, session: 10 }] };
    render({ anthropic: { mode: 'client', usage: u, opts: {} } });
    const c = cardTitled(host, 'Anthropic');
    const peak = meters(c).find(m => m.label === 'Peak hours');
    assert.ok(peak, 'the peak clock is a meter while in peak');
    assert.ok(peak.peak, 'in the peak colour');
    // 14:00 into a 12:00-18:00 window is a third of the way.
    assert.ok(Math.abs(parseFloat(peak.width) - 100 / 3) < 0.01, peak.width);
    assert.match(peak.right, /^PEAK · 4:00:00 left$/);
    assert.ok(c.querySelector('svg'), 'the usage-since-reset chart rides the card when a window exists');
  } finally {
    window.close();
  }
});

test('Anthropic: a failed read still says what happened', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ anthropic: { mode: 'client', usage: { ok: false, error: 'HTTP 401' }, opts: {} } });
    const c = cardTitled(host, 'Anthropic');
    assert.equal(pill(c).textContent, 'unavailable');
    assert.match(c.textContent, /HTTP 401/);
    assert.match(c.textContent, /Sign in to claude\.ai/);
    assert.equal(meters(c).length, 0);
  } finally {
    window.close();
  }
});

test('OpenAI: the window names come from the payload; a failed read links to chatgpt.com', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ openai: { mode: 'client', opts: {}, usage: {
      ok: true, plan: 'plus',
      session: { percent: 42, windowName: '5-hour', resetsAt: new Date(DS_PEAK + 600e3).toISOString() },
      weekly: { percent: 9, windowName: '7-day' },
    } } });
    let c = cardTitled(host, 'OpenAI');
    assert.equal(pill(c).textContent, 'plus');
    assert.deepEqual(meters(c).map(x => [x.label, x.width]), [['5-hour', '42%'], ['7-day', '9%']]);
    assert.match(meters(c)[0].right, /resets in 10m 00s/);
    assert.match(c.textContent, /ChatGPT and Codex share this allowance/);
    assert.equal(c.querySelector('svg'), null, 'no weekly window yet: no chart');

    render({ openai: { mode: 'client', opts: {}, usage: { ok: false, error: 'HTTP 500' } } });
    c = cardTitled(host, 'OpenAI');
    assert.equal(pill(c).textContent, 'unavailable');
    const a = c.querySelector('a');
    assert.ok(a && new URL(a.href).hostname === 'chatgpt.com', 'the fix is a sign-in, so the card links to it');

    render({ openai: { mode: 'client', opts: {}, usage: {
      ok: true, limitReached: true, reachedType: 'primary_window',
      session: { percent: 100, windowName: '5-hour' },
    } } });
    c = cardTitled(host, 'OpenAI');
    assert.equal(pill(c).textContent, 'limit reached');
    assert.ok(meters(c)[0].hot, 'an exhausted window is red whatever its percentage');
    assert.match(c.textContent, /Limit reached \(primary_window\)/);
  } finally {
    window.close();
  }
});

// A Copilot Free reading and a connected VS Code window whose Copilot is `account`.
const copilotUsage = (login = 'octo-editor') => ({
  ok: true, login, plan: 'free', resetsAt: new Date(DS_PEAK + 2 * 86400e3).toISOString(),
  chat: { percent: 25, used: 50, total: 200, unlimited: false },
  completions: { percent: 5.2, used: 103, total: 2000, unlimited: false },
  premium: null,
});
const copilotRow = (account = 'octo-editor') => ({
  kind: 'mcp_client', friendly: 'GitHub Copilot Chat', model: 'gpt-6-luna',
  copilot: { model: 'gpt-6-luna', account, chat_title: 'fix the parser', chat_turns: 2,
             requests: 3, workspace: 'c:\work' },
});

test('GitHub Copilot: the monthly quotas become meters, with the account and the VS Code session', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ copilot: { mode: 'tab', usage: copilotUsage(), opts: {}, rows: [copilotRow()] } });
    const c = cardTitled(host, 'GitHub Copilot');
    assert.equal(pill(c).textContent, 'free');
    assert.deepEqual(meters(c).map(m => [m.label, m.right]),
      [['Chat', '25% · 50 of 200 · resets in 2d 0h'], ['Code completions', '5.2% · 103 of 2000 · resets in 2d 0h']]);
    // Nothing is wrong, so nothing is said about the account or the reset below the meters.
    assert.doesNotMatch(c.textContent, /github\.com account|monthly quotas reset|octo-editor/);
    assert.match(c.textContent, /VS Code \(c:\work\): Model: gpt-6-luna · Chat: "fix the parser" · 2 turns/);
    assert.doesNotMatch(c.textContent, /not the editor's/);
  } finally {
    window.close();
  }
});

test('GitHub Copilot: a browser on another account than the editor draws a card without numbers', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ copilot: { mode: 'tab', usage: copilotUsage('octo-personal'), opts: {}, rows: [copilotRow()] },
             deepseek: { mode: 'client', opts: {} } });
    const c = cardTitled(host, 'GitHub Copilot');
    assert.ok(c, "in 'tab' mode no row says \"other account\", so the card has to");
    assert.equal(pill(c).textContent, 'other account');
    assert.deepEqual(meters(c), [], "another account's quota is not the editor's, so no meters");
    assert.match(c.textContent, /signed in to github\.com as octo-personal, but VS Code's Copilot uses octo-editor/);
    assert.match(c.textContent, /Switch to octo-editor on github\.com/);
    assert.match(c.textContent, /VS Code \(c:\work\): Model: gpt-6-luna/);
    // Only a KNOWN difference changes it: with no VS Code window to compare against, the numbers stay.
    render({ copilot: { mode: 'tab', usage: copilotUsage('octo-personal'), opts: {}, rows: [] } });
    assert.equal(meters(cardTitled(host, 'GitHub Copilot')).length, 2);
  } finally {
    window.close();
  }
});

test('signed out draws no card for OpenAI or GitHub Copilot; Anthropic keeps only its peak clock', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ anthropic: { mode: 'client', usage: { ok: false, error: 'HTTP 401', signedOut: true }, opts: {} },
             openai: { mode: 'client', usage: { ok: false, signedOut: true }, opts: {} },
             copilot: { mode: 'tab', usage: { ok: false, signedOut: true }, opts: {}, rows: [copilotRow()] },
             deepseek: { mode: 'client', opts: {} } });
    assert.deepEqual(cards(host).map(c => c.querySelector('h2').textContent), ['Anthropic', 'DeepSeek'],
      'the peak clocks need no account, so they stay');
    const c = cardTitled(host, 'Anthropic');
    assert.equal(pill(c).textContent, 'signed out');
    assert.match(c.textContent, /Off-peak · next PEAK in/);
    assert.doesNotMatch(c.textContent, /Session|Account usage unavailable/);
  } finally {
    window.close();
  }
});

test('GitHub Copilot: a failed read (signed in) says so and still lists the VS Code session', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ copilot: { mode: 'tab', usage: { ok: false, error: 'HTTP 500' }, opts: {}, rows: [copilotRow()] } });
    const c = cardTitled(host, 'GitHub Copilot');
    assert.equal(pill(c).textContent, 'unavailable');
    assert.equal(c.querySelector('a').href, 'https://github.com/login');
    assert.match(c.textContent, /Model: gpt-6-luna/);
  } finally {
    window.close();
  }
});

test('DeepSeek: peak is a filling meter at double price, off-peak names the next window', () => {
  let { window, host, render } = load(DS_PEAK);
  try {
    render({ deepseek: { mode: 'client', opts: {} } });
    const c = cardTitled(host, 'DeepSeek');
    assert.equal(pill(c).textContent, 'peak');
    const m = meters(c)[0];
    assert.equal(m.label, 'Peak window');
    // 02:00 into 01:00-04:00 is a third of the way.
    assert.ok(Math.abs(parseFloat(m.width) - 100 / 3) < 0.01, m.width);
    assert.match(m.right, /^double price · 2:00:00 left$/);
    assert.match(c.textContent, /01:00–04:00 and 06:00–10:00 UTC, Monday to Friday/);
  } finally {
    window.close();
  }
  ({ window, host, render } = load(ANTHROPIC_PEAK));
  try {
    render({ deepseek: { mode: 'client', opts: {} } });
    const c = cardTitled(host, 'DeepSeek');
    assert.equal(pill(c).textContent, 'off-peak');
    assert.equal(meters(c).length, 0);
    // 14:00 UTC Monday: the next window is Tuesday 01:00, eleven hours away.
    assert.match(c.textContent, /Half price · next PEAK in 11h 00m/);
  } finally {
    window.close();
  }
});

test('a provider missing from the state draws no card, and an empty state still says something', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ deepseek: { mode: 'client', opts: {} } });
    assert.deepEqual(cards(host).map(c => c.querySelector('h2').textContent), ['DeepSeek']);
    render({});
    assert.equal(cards(host).length, 0);
    assert.match(host.textContent, /No usage sources are loaded/);
  } finally {
    window.close();
  }
});

test('when every enabled provider is signed out the pane says why instead of going blank', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    render({ openai: { mode: 'client', usage: { ok: false, signedOut: true }, opts: {} } });
    assert.equal(cards(host).length, 0);
    assert.match(host.textContent, /every enabled provider is signed out in this browser/);
  } finally {
    window.close();
  }
});

test('a view module that did not load is reported as such, not as a sign-out', () => {
  const { window, host, render } = load(DS_PEAK, ['vendor/copilot-view.js']);
  try {
    render({ copilot: { mode: 'tab', usage: copilotUsage(), opts: {}, rows: [] } });
    assert.equal(cards(host).length, 0);
    assert.match(host.textContent, /The GitHub Copilot panel did not load/);
    assert.doesNotMatch(host.textContent, /signed out/);
    // Next to a card that did draw, it is still said.
    render({ copilot: { mode: 'tab', usage: copilotUsage(), opts: {}, rows: [] },
             deepseek: { mode: 'client', opts: {} } });
    assert.deepEqual(cards(host).map(c => c.querySelector('h2').textContent), ['DeepSeek']);
    assert.match(host.textContent, /The GitHub Copilot panel did not load/);
  } finally {
    window.close();
  }
});

test('OpenAI: the 5-hour trace and the weekly one share the chart, each on its own axis', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    const end = DS_PEAK + 2 * 86400e3;
    const base = {
      ok: true, plan: 'plus',
      session: { percent: 42, windowName: '5-hour' },
      weekly: { percent: 9, windowName: 'weekly', resetsAt: new Date(end).toISOString() },
      weeklyWindow: { start: DS_PEAK - 86400e3, end },
      weeklyHistory: [{ t: DS_PEAK - 3600e3, weekly: 8 }, { t: DS_PEAK, weekly: 9 }],
    };
    render({ openai: { mode: 'client', opts: {}, usage: base } });
    let c = cardTitled(host, 'OpenAI');
    assert.equal(c.querySelectorAll('polyline').length, 1, 'no session window yet: the weekly line only');

    render({ openai: { mode: 'client', opts: {}, usage: { ...base,
      sessionWindow: { start: DS_PEAK - 3600e3, end: DS_PEAK + 4 * 3600e3 },
      history: [{ t: DS_PEAK - 1800e3, session: 40 }, { t: DS_PEAK, session: 42 }] } } });
    c = cardTitled(host, 'OpenAI');
    assert.equal(c.querySelectorAll('polyline').length, 2, 'the 5-hour trace joins the weekly one');
    assert.equal(c.querySelectorAll('.usage-axis').length, 2);
    assert.match(c.textContent, /Session/);
    assert.match(c.textContent, /Weekly/);

    // A session window alone is enough for a chart, e.g. a plan with no weekly window.
    render({ openai: { mode: 'client', opts: {}, usage: { ok: true, session: base.session,
      sessionWindow: { start: DS_PEAK - 3600e3, end: DS_PEAK + 4 * 3600e3 },
      history: [{ t: DS_PEAK, session: 42 }] } } });
    assert.equal(cardTitled(host, 'OpenAI').querySelectorAll('polyline').length, 1);
  } finally {
    window.close();
  }
});

test('GitHub Copilot: the chart carries the quota the bar follows and code completions, on one month axis', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    const end = DS_PEAK + 2 * 86400e3;
    const win = { start: DS_PEAK - 28 * 86400e3, end };
    const usage = Object.assign(copilotUsage(), {
      monthlyWindow: win,
      monthlyHistory: [{ t: DS_PEAK - 3600e3, monthly: 24 }, { t: DS_PEAK, monthly: 25 }],
      completionsWindow: win,
      completionsHistory: [{ t: DS_PEAK - 3600e3, completions: 5 }, { t: DS_PEAK, completions: 5.2 }],
    });
    render({ copilot: { mode: 'tab', usage, opts: {}, rows: [] } });
    const c = cardTitled(host, 'GitHub Copilot');
    assert.deepEqual([...c.querySelectorAll('polyline')].map(p => p.getAttribute('stroke')),
      ['#8e5fd0', '#00897b'], 'the charted quota in violet, code completions in teal');
    assert.equal(c.querySelectorAll('.usage-axis').length, 1, 'both quotas share the month, so one axis row');
    assert.equal(c.querySelectorAll('svg line[stroke-dasharray="2 2"]').length, 1, 'and one "now" marker');
    const legend = c.querySelector('.usage-chart').firstChild.textContent;
    assert.match(legend, /● Chat/, 'the line is named for the quota, not just "Monthly"');
    assert.match(legend, /● Code completions/);
    // The meter of each quota wears its line's colour.
    const [m1, m2] = [...c.querySelectorAll('.meter .fill')];
    assert.equal(m1.style.background, 'rgb(142, 95, 208)');
    assert.equal(m2.style.background, 'rgb(0, 137, 123)');

    // Completions alone (nothing else capped) still draw; so does the quota alone.
    render({ copilot: { mode: 'tab', opts: {}, rows: [], usage: { ...usage, chat: null, monthlyWindow: undefined } } });
    assert.equal(cardTitled(host, 'GitHub Copilot').querySelectorAll('polyline').length, 1);
    render({ copilot: { mode: 'tab', opts: {}, rows: [], usage: { ...usage, completions: { unlimited: true } } } });
    assert.equal(cardTitled(host, 'GitHub Copilot').querySelectorAll('polyline').length, 1);
    render({ copilot: { mode: 'tab', usage, opts: {}, rows: [] } });

    // Without a window (nothing sampled yet), or with nothing capped, there is no chart.
    render({ copilot: { mode: 'tab', usage: copilotUsage(), opts: {}, rows: [] } });
    assert.equal(cardTitled(host, 'GitHub Copilot').querySelector('svg'), null);
  } finally {
    window.close();
  }
});

test('meters wear the colour of the chart line of their window; hot stays red', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    const copilot = Object.assign(copilotUsage(), {});
    render({ anthropic: { mode: 'client', usage: usage(34, 85), opts: {} },
             copilot: { mode: 'tab', usage: copilot, opts: {}, rows: [] } });
    const fills = (title) => [...cardTitled(host, title).querySelectorAll('.meter .fill')];
    const [session, week] = fills('Anthropic');
    assert.equal(session.style.background.includes('accent'), true, 'session: the accent blue');
    assert.equal(week.classList.contains('hot'), true);
    assert.equal(week.style.background, '', 'a hot meter keeps the red, not the window colour');
    const [premium] = fills('GitHub Copilot');
    assert.equal(premium.style.background, 'rgb(142, 95, 208)', 'monthly: violet');

    render({ anthropic: { mode: 'client', usage: usage(34, 40), opts: {} } });
    assert.equal(fills('Anthropic')[1].style.background, 'rgb(67, 160, 71)', 'weekly: the logo green');
  } finally {
    window.close();
  }
});

test('a redraw touches only what changed: unchanged nodes, chart included, stay the same objects', () => {
  const { window, host, render } = load(DS_PEAK);
  try {
    const end = DS_PEAK + 2 * 86400e3;
    const reading = (sessionPct, weeklyHistory) => ({
      ok: true, plan: 'plus',
      session: { percent: sessionPct, windowName: '5-hour', resetsAt: new Date(DS_PEAK + 3600e3).toISOString() },
      weekly: { percent: 9, windowName: 'weekly', resetsAt: new Date(end).toISOString() },
      weeklyWindow: { start: DS_PEAK - 86400e3, end }, weeklyHistory,
      sessionWindow: { start: DS_PEAK - 3600e3, end: DS_PEAK + 4 * 3600e3 },
      history: [{ t: DS_PEAK - 1800e3, session: 40 }, { t: DS_PEAK, session: sessionPct }],
    });
    const state = (u) => ({ openai: { mode: 'client', opts: {}, usage: u },
      deepseek: { mode: 'client', opts: {} } });
    render(state(reading(42, [{ t: DS_PEAK, weekly: 9 }])));
    const before = {
      card: cardTitled(host, 'OpenAI'), svg: host.querySelector('svg'),
      weekMeter: host.querySelectorAll('.meter')[1], deepseek: cardTitled(host, 'DeepSeek'),
      session: host.querySelector('.meter .fill'),
    };
    assert.equal(host.querySelector('.meter .lbl span').textContent, '42% · resets in 1h 00m');

    // The same reading again, and then one where only a number moved.
    render(state(reading(42, [{ t: DS_PEAK, weekly: 9 }])));
    render(state(reading(55, [{ t: DS_PEAK, weekly: 9 }])));
    assert.equal(cardTitled(host, 'OpenAI'), before.card, 'the card is not rebuilt');
    assert.equal(host.querySelector('svg'), before.svg, 'nor is the chart');
    assert.equal(host.querySelectorAll('.meter')[1], before.weekMeter);
    assert.equal(cardTitled(host, 'DeepSeek'), before.deepseek);
    assert.equal(host.querySelector('.meter .fill'), before.session, 'the bar is resized, not replaced');
    assert.equal(host.querySelector('.meter .fill').style.width, '55%');
    assert.equal(host.querySelector('.meter .lbl span').textContent, '55% · resets in 1h 00m');
    assert.equal(host.querySelectorAll('polyline').length, 2);

    // A new chart sample changes the polyline's points in place.
    const line = host.querySelector('polyline[stroke="#43a047"]');
    render(state(reading(55, [{ t: DS_PEAK - 3600e3, weekly: 8 }, { t: DS_PEAK, weekly: 9 }])));
    assert.equal(host.querySelector('polyline[stroke="#43a047"]'), line);
    assert.equal(line.getAttribute('points').split(' ').length, 2);

    // A different shape still comes out right: a card leaves, a note appears, a card changes kind.
    render({ openai: { mode: 'client', opts: {}, usage: { ok: false, error: 'HTTP 500' } } });
    assert.deepEqual(cards(host).map(c => c.querySelector('h2').textContent), ['OpenAI']);
    assert.equal(pill(cardTitled(host, 'OpenAI')).textContent, 'unavailable');
    assert.equal(host.querySelector('svg'), null);
    assert.ok(host.querySelector('a'), 'the sign-in link is drawn');
    render({});
    assert.equal(cards(host).length, 0);
    assert.match(host.textContent, /No usage sources are loaded/);
    assert.equal(host.childNodes.length, 1);
  } finally {
    window.close();
  }
});
