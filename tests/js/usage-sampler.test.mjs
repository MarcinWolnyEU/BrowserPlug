/*
 * Unit tests for extension-gecko/vendor/usage-sampler.js, the background clock behind the
 * "Query every N min" checkbox, plus the glue in extension-gecko/background.js that arms it.
 *
 * Three hazards, and the third is why this file slices background.js at all.
 *
 * 1. The SCHEDULE. browser.alarms.create() RESTARTS an existing alarm, and apply() runs
 *    again on every cold start of the background context. Get the guard wrong and the
 *    clock is not slow, it never strikes at all — a bug with no error, no log, and a
 *    symptom (an empty history) identical to the pull-based behaviour it replaced.
 *
 * 2. The MODE. `disabled` promises to stop the claude.ai request, checkbox or not; an
 *    alarm that outlived the switch to Disabled would break that promise unattended,
 *    which is worse than breaking it with the popup open.
 *
 * 3. The ARMING. This codebase's recurring failure is arming code staying dead behind
 *    hundreds of green helper tests (see the refstale twins and toolbar-icon.test.mjs).
 *    Testing createSampler() alone would pass with the usageSampler.apply() call deleted
 *    from applyUsageSettings(), so the last block runs the REAL applyUsageSettings,
 *    sliced out of background.js, and asserts the settings reach the sampler.
 *
 * The chromium twin is tests/js-chromium/usage-sampler.test.mjs: same shared module, its
 * own background.js.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const SRC = fs.readFileSync(
  new URL('../../extension-gecko/vendor/usage-sampler.js', import.meta.url), 'utf-8');
const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');
const POPUP_SRC = fs.readFileSync(
  new URL('../../extension-gecko/popup/popup.js', import.meta.url), 'utf-8');
const OPTIONS_SRC = fs.readFileSync(
  new URL('../../extension-gecko/options.js', import.meta.url), 'utf-8');

// The module is an IIFE that writes `globalThis`. Shadowing that name gives each case its
// own module instance, with no reset seam in the production file.
function loadModule() {
  const g = {};
  g.globalThis = g;
  // eslint-disable-next-line no-new-func
  new Function('globalThis', SRC)(g);
  return g.UsageSampler;
}

// A browser.alarms that records, and that can be pre-loaded with alarms a previous life
// of the background context left behind.
function fakeAlarms(existing = {}) {
  const store = new Map(Object.entries(existing));
  const listeners = [];
  return {
    creates: [],
    clears: [],
    create(name, info) { this.creates.push({ name, info }); store.set(name, { name, ...info }); },
    clear(name) { this.clears.push(name); return Promise.resolve(store.delete(name)); },
    get(name) { return Promise.resolve(store.get(name)); },
    onAlarm: {
      addListener: (fn) => listeners.push(fn),
      fire: (name) => listeners.forEach((fn) => fn({ name })),
    },
  };
}

function samplerWith(alarms, sampled = []) {
  const US = loadModule();
  const sampler = US.createSampler({
    alarms,
    providers: [
      { key: 'anthropic', sample: () => { sampled.push('anthropic'); return Promise.resolve(); } },
      { key: 'openai', sample: () => { sampled.push('openai'); return Promise.resolve(); } },
    ],
  });
  return { US, sampler, sampled };
}

// The checkbox ticked, on a provider that is shown somewhere.
const ON = { mode: 'client', backgroundRefresh: true };

test('a ticked box arms a periodic alarm at the refresh rate; the first tick is one period out', async () => {
  const alarms = fakeAlarms();
  const { sampler } = samplerWith(alarms);
  await sampler.apply({
    anthropic: { ...ON, refreshMinutes: 5 },
    openai: { mode: 'disabled', backgroundRefresh: true, refreshMinutes: 5 },
  });
  assert.deepEqual(alarms.creates, [
    { name: 'browserplug-usage-anthropic', info: { delayInMinutes: 5, periodInMinutes: 5 } },
  ]);
  assert.equal(sampler.armedPeriod('anthropic'), 5);
  assert.equal(sampler.armedPeriod('openai'), null);
});

test('the Usage-tab-only mode arms the clock too; the box decides, not where the bar is', async () => {
  const alarms = fakeAlarms();
  const { sampler } = samplerWith(alarms);
  await sampler.apply({ anthropic: { mode: 'tab', backgroundRefresh: true, refreshMinutes: 4 } });
  assert.equal(sampler.armedPeriod('anthropic'), 4);
});

test('an unticked box, or Disabled with the box ticked, arms nothing and clears a running alarm', async () => {
  const alarms = fakeAlarms();
  const { sampler } = samplerWith(alarms);
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 3 } });
  assert.equal(alarms.creates.length, 1);

  // Unticked, only the open popup fetches. `disabled` must make no request at all, so a
  // box left ticked underneath it does not count.
  for (const s of [
    { mode: 'client', backgroundRefresh: false },
    { mode: 'tab' },
    { mode: 'disabled', backgroundRefresh: true },
    { backgroundRefresh: true },
  ]) {
    alarms.clears.length = 0;
    await sampler.apply({ anthropic: { ...s, refreshMinutes: 3 } });
    assert.ok(alarms.clears.includes('browserplug-usage-anthropic'), JSON.stringify(s));
    assert.equal(sampler.armedPeriod('anthropic'), null, JSON.stringify(s));
    await sampler.apply({ anthropic: { ...ON, refreshMinutes: 3 } });
  }
});

test('an alarm already running at the right period is LEFT ALONE', async () => {
  // The hazard: create() restarts the schedule. apply() runs again on every cold start of
  // the background context, and on MV3 the reconnect watchdog causes one every 30s while
  // the server is down — re-creating each time would push the next sample past the
  // horizon forever and the clock would never once strike.
  const alarms = fakeAlarms({
    'browserplug-usage-anthropic': { name: 'browserplug-usage-anthropic', periodInMinutes: 5 },
  });
  const { sampler } = samplerWith(alarms);
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 5 } });
  assert.deepEqual(alarms.creates, [], 'a correctly-running alarm must not be re-created');
  assert.equal(sampler.armedPeriod('anthropic'), 5);
});

test('a CHANGED refresh rate does re-create the alarm', async () => {
  const alarms = fakeAlarms({
    'browserplug-usage-anthropic': { name: 'browserplug-usage-anthropic', periodInMinutes: 5 },
  });
  const { sampler } = samplerWith(alarms);
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 12 } });
  assert.deepEqual(alarms.creates, [
    { name: 'browserplug-usage-anthropic', info: { delayInMinutes: 12, periodInMinutes: 12 } },
  ]);
});

test('the refresh rate is clamped exactly as the stores clamp it', () => {
  const US = loadModule();
  assert.equal(US.refreshMinutesOf(0), 5, '0 is falsy: the stores read it as the default');
  assert.equal(US.refreshMinutesOf(0.5), 1);
  assert.equal(US.refreshMinutesOf(900), 60);
  assert.equal(US.refreshMinutesOf('7'), 7);
  assert.equal(US.refreshMinutesOf(undefined), 5);
});

test('a tick samples its own provider; another extension alarm is ignored', async () => {
  const alarms = fakeAlarms();
  const sampled = [];
  const { sampler } = samplerWith(alarms, sampled);
  await sampler.apply({
    anthropic: { ...ON, refreshMinutes: 5 },
    openai: { ...ON, refreshMinutes: 5 },
  });

  // Registered by the constructor, not by the caller: the listener must be added on the
  // first synchronous evaluation or an MV3 worker forgets it.
  alarms.onAlarm.fire('browserplug-usage-openai');
  assert.deepEqual(sampled, ['openai']);

  assert.equal(sampler.handleAlarm('browserplug-reconnect-watchdog'), false,
    'the watchdog shares this event; consuming it would break reconnection');
  assert.deepEqual(sampled, ['openai']);
});

test('a failing sample is reported, not thrown at the alarm', async () => {
  const alarms = fakeAlarms();
  const US = loadModule();
  const errors = [];
  const sampler = US.createSampler({
    alarms,
    providers: [{ key: 'anthropic', sample: () => Promise.reject(new Error('signed out')) }],
    onError: (e) => errors.push(String(e.message || e)),
  });
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 5 } });
  alarms.onAlarm.fire('browserplug-usage-anthropic');
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(errors, ['signed out']);
});

// ---- back-off for a signed-out provider ---------------------------------------------------

// A sampler whose anthropic provider answers what `answers.next` says: true = signed out.
function backoffSampler({ storage } = {}) {
  const alarms = fakeAlarms();
  const US = loadModule();
  const answers = { signedOut: true, calls: 0 };
  const sampler = US.createSampler({
    alarms,
    storage,
    providers: [{
      key: 'anthropic',
      sample: () => { answers.calls++; return Promise.resolve({ ok: false, signedOut: answers.signedOut }); },
    }],
  });
  // Fires n ticks, letting each answer land, and returns how many of them sampled.
  async function ticks(n) {
    const before = answers.calls;
    for (let i = 0; i < n; i++) {
      alarms.onAlarm.fire('browserplug-usage-anthropic');
      await new Promise((r) => setTimeout(r, 0));
    }
    return answers.calls - before;
  }
  return { alarms, sampler, answers, ticks };
}

test('back-off: each signed-out answer doubles the ticks skipped, up to an hour of wall time', async () => {
  const { sampler, ticks } = backoffSampler();
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 5 } });
  // Sampled, skip 1 | sampled, skip 2 | sampled, skip 4 | sampled, skip 8 | sampled, skip 11 (cap).
  const pattern = [];
  for (let i = 0; i < 40; i++) pattern.push(await ticks(1));
  const sampledAt = pattern.flatMap((n, i) => (n ? [i] : []));
  assert.deepEqual(sampledAt, [0, 2, 5, 10, 19, 31],
    'gaps of 1, 2, 4, 8, then 11 skipped ticks (11 x 5 min + the sample = one hour)');
});

test('back-off: a clock already at an hour has no room to back off', async () => {
  const { sampler, ticks } = backoffSampler();
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 60 } });
  assert.equal(await ticks(6), 6);
});

test('back-off: the first answer that is not signed out restores the full rate', async () => {
  const { sampler, answers, ticks } = backoffSampler();
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 5 } });
  assert.equal(await ticks(1), 1);            // signed out: skip 1
  assert.equal(await ticks(1), 0);
  answers.signedOut = false;
  assert.equal(await ticks(1), 1);            // the skipped gap is over; signed in now
  assert.equal(await ticks(4), 4, 'every tick samples again');
  answers.signedOut = true;
  assert.equal(await ticks(2), 1, 'and the back-off starts over at one skipped tick');
});

test('back-off: wake() (the popup or Options asking) restores the full rate at once', async () => {
  const { sampler, ticks } = backoffSampler();
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 5 } });
  assert.equal(await ticks(1), 1);
  assert.equal(await ticks(1), 0, 'backed off');
  sampler.wake('anthropic');
  assert.equal(await ticks(1), 1, 'the next tick samples');
});

test('back-off: a new schedule (changed rate) or a switched-off clock starts at the full rate', async () => {
  const { sampler, ticks } = backoffSampler();
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 5 } });
  assert.equal(await ticks(1), 1);            // signed out: gap 1, skip 1
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 5 } });
  assert.equal(await ticks(1), 0, 'a cold start that leaves the alarm alone keeps the back-off');
  assert.equal(await ticks(1), 1);            // gap 2, skip 2
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 10 } });
  assert.equal(await ticks(1), 1, 'a re-created alarm samples at once, and starts at gap 1');
  assert.equal(await ticks(2), 1, 'one skipped tick, then a sample: the gap grew from 1, not from 2');
  await sampler.apply({ anthropic: { mode: 'disabled', backgroundRefresh: true, refreshMinutes: 10 } });
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 10 } });
  assert.equal(await ticks(1), 1, 'off and on again samples at once');
});

test('back-off: an error that is not "signed out" does not back off', async () => {
  const alarms = fakeAlarms();
  const US = loadModule();
  let calls = 0;
  const sampler = US.createSampler({
    alarms,
    providers: [{ key: 'anthropic', sample: () => { calls++; return Promise.resolve({ ok: false, error: 'HTTP 500' }); } }],
  });
  await sampler.apply({ anthropic: { ...ON, refreshMinutes: 5 } });
  for (let i = 0; i < 5; i++) {
    alarms.onAlarm.fire('browserplug-usage-anthropic');
    await new Promise((r) => setTimeout(r, 0));
  }
  assert.equal(calls, 5);
});

test('back-off: it survives an evicted worker, because it is kept in storage', async () => {
  const store = {};
  const storage = {
    get: (k) => Promise.resolve(k in store ? { [k]: store[k] } : {}),
    set: (o) => { Object.assign(store, JSON.parse(JSON.stringify(o))); return Promise.resolve(); },
  };
  const first = backoffSampler({ storage });
  await first.sampler.apply({ anthropic: { ...ON, refreshMinutes: 5 } });
  assert.equal(await first.ticks(1), 1);
  assert.deepEqual(store.usageSamplerBackoff, { anthropic: { gap: 1, skip: 1 } });

  // A new worker: nothing in memory but the stored state, and the alarm is already running.
  const second = backoffSampler({ storage });
  second.alarms.create('browserplug-usage-anthropic', { delayInMinutes: 5, periodInMinutes: 5 });
  assert.equal(await second.ticks(1), 0, 'the stored skip is honoured');
  assert.deepEqual(store.usageSamplerBackoff, { anthropic: { gap: 1, skip: 0 } });
  assert.equal(await second.ticks(1), 1);
  assert.deepEqual(store.usageSamplerBackoff, { anthropic: { gap: 2, skip: 2 } });
  second.sampler.wake('anthropic');
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(store.usageSamplerBackoff, {});
});

// ---- the arming glue, out of the real background.js ---------------------------------
// applyUsageSettings() sliced on its own section anchors (invariant E checks both trees
// still carry them). Its free names are passed in, so the slice runs as written.
function loadBackgroundGlue({ stored, alarms, local, answer }) {
  const start = BG_SRC.indexOf('// ---- provider usage settings + the always-on sampler');
  const end = BG_SRC.indexOf('// ---- end provider usage settings', start);
  assert.ok(start >= 0 && end > start,
    'the provider-usage-settings block was not found in background.js between its anchors');

  const refreshCalls = { anthropic: [], openai: [], copilot: [], mistral: [] };
  const sampled = { anthropic: [], openai: [], copilot: [], mistral: [] };
  const store = (key) => ({
    setRefreshMinutes: (m) => refreshCalls[key].push(m),
    get: (force) => { sampled[key].push(force); return Promise.resolve(answer); },
  });
  const changeListeners = [];
  const writes = [];
  const browser = {
    alarms,
    storage: {
      local,
      sync: {
        // storage.sync.get takes a defaults object or a key list; a list answers only
        // what is stored.
        get: (keys) => Promise.resolve(Array.isArray(keys)
          ? Object.fromEntries(keys.filter((k) => k in stored).map((k) => [k, stored[k]]))
          : { ...keys, ...stored }),
        set: (patch) => { writes.push(patch); Object.assign(stored, patch); return Promise.resolve(); },
      },
      onChanged: { addListener: (fn) => changeListeners.push(fn) },
    },
  };
  // eslint-disable-next-line no-new-func
  const factory = new Function('browser', 'UsageSampler', 'claudeUsageStore', 'openaiUsageStore',
    'copilotUsageStore', 'mistralUsageStore', 'console',
    `${BG_SRC.slice(start, end)}\nreturn { usageSampler, applyUsageSettings };`);
  const out = factory(browser, loadModule(), store('anthropic'), store('openai'), store('copilot'),
    store('mistral'), { warn() {}, error() {} });
  return { ...out, refreshCalls, sampled, changeListeners, writes };
}

test('background glue: a ticked box reaches the sampler and arms a real alarm', async () => {
  const alarms = fakeAlarms();
  const glue = loadBackgroundGlue({
    alarms,
    stored: { anthropicBackgroundRefresh: true, anthropicUsageRefreshMinutes: 9 },
  });
  await glue.applyUsageSettings();
  assert.deepEqual(alarms.creates, [
    { name: 'browserplug-usage-anthropic', info: { delayInMinutes: 9, periodInMinutes: 9 } },
  ], 'deleting the usageSampler.apply() call from applyUsageSettings must fail here');
  assert.deepEqual(glue.refreshCalls.anthropic.slice(-1), [9], 'the store still gets its TTL');
  assert.equal(glue.usageSampler.armedPeriod('openai'), null, 'OpenAI ships disabled');
});

test('background glue: Mistral arms its own clock, and its tick forces a reading of the Mistral store', async () => {
  const alarms = fakeAlarms();
  const glue = loadBackgroundGlue({
    alarms,
    stored: { mistralDisplayMode: 'tab', mistralBackgroundRefresh: true, mistralUsageRefreshMinutes: 7 },
  });
  await glue.applyUsageSettings();
  assert.deepEqual(alarms.creates, [
    { name: 'browserplug-usage-mistral', info: { delayInMinutes: 7, periodInMinutes: 7 } },
  ], 'deleting the Mistral entry from the providers list must fail here');
  assert.equal(glue.usageSampler.armedPeriod('mistral'), 7);
  assert.deepEqual(glue.refreshCalls.mistral.slice(-1), [7], 'the store still gets its TTL');
  alarms.onAlarm.fire('browserplug-usage-mistral');
  assert.deepEqual(glue.sampled.mistral, [true], 'the tick forces a reading, not a cached one');
  assert.deepEqual([glue.sampled.anthropic, glue.sampled.openai, glue.sampled.copilot], [[], [], []],
    'and touches no other provider');
});

test('background glue: Mistral ships Disabled, so a ticked box alone arms nothing', async () => {
  // A third-party site must not be contacted by an update, nor by a stray box under Disabled.
  const alarms = fakeAlarms();
  const glue = loadBackgroundGlue({ alarms, stored: { mistralBackgroundRefresh: true } });
  await glue.applyUsageSettings();
  assert.deepEqual(alarms.creates, []);
  assert.equal(glue.usageSampler.armedPeriod('mistral'), null);
  assert.deepEqual(glue.refreshCalls.mistral.slice(-1), [5], 'the store TTL defaults to 5 minutes');
});

test('background glue: a stored mode this build does not know reads as the card default, as in the popup', async () => {
  // popup.js and options.js show such a card in its shipped mode. The clock has to agree, or
  // a card both pages show as Disabled is still polled in the background.
  const alarms = fakeAlarms();
  const glue = loadBackgroundGlue({
    alarms,
    stored: {
      mistralDisplayMode: 'always', mistralBackgroundRefresh: true,      // ships disabled
      openaiDisplayMode: 'somewhere-else', openaiBackgroundRefresh: true, // ships disabled
      copilotDisplayMode: 42, copilotBackgroundRefresh: true,             // ships on the row
      anthropicDisplayMode: 'bogus', anthropicBackgroundRefresh: true,    // ships on the row
    },
  });
  await glue.applyUsageSettings();
  assert.equal(glue.usageSampler.armedPeriod('mistral'), null, 'an unknown Mistral mode armed a clock');
  assert.equal(glue.usageSampler.armedPeriod('openai'), null, 'an unknown OpenAI mode armed a clock');
  assert.equal(glue.usageSampler.armedPeriod('copilot'), 5, 'the default is on the row, so the ticked box runs');
  assert.equal(glue.usageSampler.armedPeriod('anthropic'), 5);
  assert.deepEqual(alarms.creates.map((c) => c.name).sort(),
    ['browserplug-usage-anthropic', 'browserplug-usage-copilot']);
});

test('withKnownModes: only the *DisplayMode keys are read, an unknown value becomes the default, and it returns a copy', () => {
  const US = loadModule();
  const defaults = { aDisplayMode: 'client', bDisplayMode: 'disabled', aBackgroundRefresh: false, aUsageRefreshMinutes: 5 };
  const stored = { aDisplayMode: 'tab', bDisplayMode: 'always', aBackgroundRefresh: true, aUsageRefreshMinutes: 'x' };
  const out = US.withKnownModes(stored, defaults);
  assert.deepEqual(out, { aDisplayMode: 'tab', bDisplayMode: 'disabled', aBackgroundRefresh: true, aUsageRefreshMinutes: 'x' });
  assert.equal(stored.bDisplayMode, 'always', 'the input is not changed');
  for (const mode of ['disabled', 'tab', 'client']) {
    assert.equal(US.withKnownModes({ aDisplayMode: mode }, defaults).aDisplayMode, mode, mode);
  }
  assert.equal(US.withKnownModes({}, defaults).aDisplayMode, 'client', 'a missing mode is the default');
  assert.deepEqual(US.withKnownModes({ x: 1 }, undefined), { x: 1 });
});

test('background glue: the retired "always" mode reads as the card default, with nothing written', async () => {
  // A profile saved by an old build may still hold it. It is not migrated: every reader
  // (the clock here, the popup, Options) takes it as the card's shipped mode.
  const alarms = fakeAlarms();
  const glue = loadBackgroundGlue({
    alarms,
    stored: {
      anthropicDisplayMode: 'always', anthropicBackgroundRefresh: true, anthropicUsageRefreshMinutes: 9,
      openaiDisplayMode: 'always', openaiBackgroundRefresh: true,   // ships disabled
    },
  });
  await new Promise((r) => setTimeout(r, 0));   // the slice's own top-level applyUsageSettings()
  assert.equal(glue.usageSampler.armedPeriod('anthropic'), 9, 'Anthropic ships on the row: the ticked box runs');
  assert.equal(glue.usageSampler.armedPeriod('openai'), null, 'OpenAI ships disabled: no clock');
  assert.deepEqual(glue.writes, [], 'nothing is rewritten in storage');
});

test('background glue: the sampler is given storage.local, and a provider answer reaches its back-off', async () => {
  const alarms = fakeAlarms();
  const local = { store: {} };
  const glue = loadBackgroundGlue({
    alarms,
    stored: { anthropicBackgroundRefresh: true },
    local: {
      get: (k) => Promise.resolve(k in local.store ? { [k]: local.store[k] } : {}),
      set: (o) => { Object.assign(local.store, o); return Promise.resolve(); },
    },
    answer: { ok: false, signedOut: true },
  });
  await glue.applyUsageSettings();
  alarms.onAlarm.fire('browserplug-usage-anthropic');
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(local.store.usageSamplerBackoff, { anthropic: { gap: 1, skip: 1 } },
    'passing no storage to createSampler, or dropping the answer, must fail here');
});

test('background glue: each usage message wakes its own provider from a back-off', () => {
  for (const [action, key] of [['getClaudeUsage', 'anthropic'], ['getOpenAIUsage', 'openai'],
    ['getCopilotUsage', 'copilot'], ['getMistralUsage', 'mistral']]) {
    const at = BG_SRC.indexOf(`request.action === '${action}'`);
    assert.ok(at >= 0, `${action} handler not found`);
    assert.ok(BG_SRC.slice(at, at + 600).includes(`usageSampler.wake('${key}')`),
      `${action} must call usageSampler.wake('${key}')`);
  }
});

test('background glue: the shipped defaults arm no clock, and the listener watches the mode', async () => {
  const alarms = fakeAlarms();
  const glue = loadBackgroundGlue({ alarms, stored: {} });
  await glue.applyUsageSettings();
  assert.deepEqual(alarms.creates, [], 'the shipped defaults (box unticked) arm no clock');
  assert.deepEqual(glue.writes, [], 'and a clean profile is never written to');

  assert.ok(glue.changeListeners.length, 'a storage.onChanged listener must be registered');
  const block = BG_SRC.slice(BG_SRC.indexOf('// ---- provider usage settings'),
    BG_SRC.indexOf('// ---- end provider usage settings'));
  for (const key of ['anthropicDisplayMode', 'openaiDisplayMode', 'copilotDisplayMode', 'mistralDisplayMode',
    'anthropicBackgroundRefresh', 'openaiBackgroundRefresh', 'copilotBackgroundRefresh',
    'mistralBackgroundRefresh', 'mistralUsageRefreshMinutes']) {
    assert.ok(block.includes(`changes.${key}`),
      `${key} decides whether a clock runs; not watching it leaves a change without effect `
      + 'until the next browser start');
  }
});

test('the background, popup and Options mode defaults agree', () => {
  // Three carriers for one fact. Disagree and a clock runs for a provider the popup is not
  // drawing, or the reverse; or Options shows, and then saves, a mode the popup does not draw.
  const defaultIn = (src, k) => (src.match(new RegExp(`${k}:\\s*'(\\w+)'`)) || [])[1];
  const start = OPTIONS_SRC.indexOf('const PROVIDER_MODE_DEFAULTS = {');
  assert.ok(start >= 0, 'PROVIDER_MODE_DEFAULTS not found in options.js');
  const optionsTable = OPTIONS_SRC.slice(start, OPTIONS_SRC.indexOf('};', start));
  for (const key of ['anthropicDisplayMode', 'openaiDisplayMode', 'copilotDisplayMode',
    'mistralDisplayMode']) {
    assert.ok(defaultIn(BG_SRC, key), `${key} default not found in background.js`);
    assert.equal(defaultIn(BG_SRC, key), defaultIn(POPUP_SRC, key),
      `${key} default disagrees between background and popup`);
  }
  for (const key of ['anthropicDisplayMode', 'openaiDisplayMode', 'copilotDisplayMode',
    'mistralDisplayMode', 'deepseekDisplayMode']) {
    assert.ok(defaultIn(optionsTable, key), `${key} default not found in options.js`);
    assert.equal(defaultIn(optionsTable, key), defaultIn(POPUP_SRC, key),
      `${key} default disagrees between Options and popup`);
  }
});

