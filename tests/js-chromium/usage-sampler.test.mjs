/*
 * Chromium twin of tests/js/usage-sampler.test.mjs. Same SHARED vendor/usage-sampler.js
 * (invariant C keeps the two copies byte-identical), but sliced against the MV3 worker's
 * own background.js — which is where the schedule hazard actually bites: the worker is
 * evicted after 30s idle, so the block below this one runs again, and again, and each
 * re-run is a chance to restart an alarm that was about to fire.
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
 * The gecko original is tests/js/usage-sampler.test.mjs.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const SRC = fs.readFileSync(
  new URL('../../extension-chromium/vendor/usage-sampler.js', import.meta.url), 'utf-8');
const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');
const POPUP_SRC = fs.readFileSync(
  new URL('../../extension-chromium/popup/popup.js', import.meta.url), 'utf-8');

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

test('a stored legacy "always" becomes the Usage tab with the box ticked', () => {
  const US = loadModule();
  assert.deepEqual(US.legacyModePatch({
    anthropicDisplayMode: 'always', openaiDisplayMode: 'client', deepseekDisplayMode: 'always',
  }), {
    anthropicDisplayMode: 'tab', anthropicBackgroundRefresh: true,
    // DeepSeek fetches nothing, so there is no box to tick.
    deepseekDisplayMode: 'tab',
  });
  assert.equal(US.legacyModePatch({ anthropicDisplayMode: 'client' }), null,
    'nothing to migrate means nothing written, or every start would fire storage.onChanged');
  assert.equal(US.legacyModePatch({}), null);
  assert.deepEqual(US.LEGACY_MODE_KEYS,
    ['anthropicDisplayMode', 'openaiDisplayMode', 'deepseekDisplayMode']);
});

// ---- the arming glue, out of the real background.js ---------------------------------
// applyUsageSettings() sliced on its own section anchors (invariant E checks both trees
// still carry them). Its free names are passed in, so the slice runs as written.
function loadBackgroundGlue({ stored, alarms }) {
  const start = BG_SRC.indexOf('// ---- provider usage settings + the always-on sampler');
  const end = BG_SRC.indexOf('// ---- end provider usage settings', start);
  assert.ok(start >= 0 && end > start,
    'the provider-usage-settings block was not found in background.js between its anchors');

  const refreshCalls = { anthropic: [], openai: [], copilot: [] };
  const store = (key) => ({
    setRefreshMinutes: (m) => refreshCalls[key].push(m),
    get: () => Promise.resolve(),
  });
  const changeListeners = [];
  const installedListeners = [];
  const writes = [];
  const browser = {
    alarms,
    runtime: { onInstalled: { addListener: (fn) => installedListeners.push(fn) } },
    storage: {
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
    'copilotUsageStore', 'console',
    `${BG_SRC.slice(start, end)}\nreturn { usageSampler, applyUsageSettings };`);
  const out = factory(browser, loadModule(), store('anthropic'), store('openai'), store('copilot'),
    { warn() {}, error() {} });
  return { ...out, refreshCalls, changeListeners, installedListeners, writes };
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

test('background glue: a legacy "always" is rewritten once and keeps its clock', async () => {
  const alarms = fakeAlarms();
  const glue = loadBackgroundGlue({
    alarms,
    stored: { anthropicDisplayMode: 'always', anthropicUsageRefreshMinutes: 9 },
  });
  // Only the slice's own top-level applyUsageSettings(), the one a real start runs.
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(glue.writes, [{ anthropicDisplayMode: 'tab', anthropicBackgroundRefresh: true }]);
  assert.equal(glue.usageSampler.armedPeriod('anthropic'), 9,
    'the clock "always" used to run survives the migration');
  await glue.applyUsageSettings();
  assert.equal(glue.writes.length, 1, 'a migrated profile is not written again');
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
  for (const key of ['anthropicDisplayMode', 'openaiDisplayMode', 'copilotDisplayMode',
    'anthropicBackgroundRefresh', 'openaiBackgroundRefresh', 'copilotBackgroundRefresh']) {
    assert.ok(block.includes(`changes.${key}`),
      `${key} decides whether a clock runs; not watching it leaves a change without effect `
      + 'until the next browser start');
  }
});

test('the background defaults agree with the popup PROVIDER_MODE_DEFAULTS', () => {
  // Two carriers for one fact. Disagree and a clock runs for a provider the popup is not
  // drawing, or the reverse.
  const bgOf = (k) => (BG_SRC.match(new RegExp(`${k}:\\s*'(\\w+)'`)) || [])[1];
  const popupOf = (k) => (POPUP_SRC.match(new RegExp(`${k}:\\s*'(\\w+)'`)) || [])[1];
  for (const key of ['anthropicDisplayMode', 'openaiDisplayMode', 'copilotDisplayMode']) {
    assert.ok(bgOf(key), `${key} default not found in background.js`);
    assert.equal(bgOf(key), popupOf(key), `${key} default disagrees between background and popup`);
  }
});

// ---- the DeepSeek default changed from 'client' to 'disabled' after 1.9.1 ----------------

test('legacyDefaultsPatch: an update from 1.9.1 or older keeps the clock the old default showed', () => {
  const US = loadModule();
  for (const v of ['1.9.1', '1.9.0', '1.8.12', '1.9']) {
    assert.deepEqual(US.legacyDefaultsPatch(v, {}), { deepseekDisplayMode: 'client' }, v);
  }
  assert.equal(US.legacyDefaultsPatch('1.9.1', { deepseekDisplayMode: 'disabled' }), null,
    'a saved choice is the user\'s, whatever it is');
  for (const v of ['1.9.2', '1.10.0', '2.0', undefined, '', 'nightly']) {
    assert.equal(US.legacyDefaultsPatch(v, {}), null, String(v));
  }
});

test('background glue: an update from 1.9.1 writes the old DeepSeek default down, once', async () => {
  const glue = loadBackgroundGlue({ alarms: fakeAlarms(), stored: {} });
  assert.equal(glue.installedListeners.length, 1, 'no runtime.onInstalled listener is registered');
  await new Promise((r) => setTimeout(r, 0));
  const before = glue.writes.length;
  glue.installedListeners[0]({ reason: 'update', previousVersion: '1.9.1' });
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(glue.writes.slice(before), [{ deepseekDisplayMode: 'client' }]);
  glue.installedListeners[0]({ reason: 'update', previousVersion: '1.9.1' });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(glue.writes.length, before + 1, 'a stored value is never overwritten');
  glue.installedListeners[0]({ reason: 'install' });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(glue.writes.length, before + 1, 'a fresh install takes the new default');
});
