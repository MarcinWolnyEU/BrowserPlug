/*
 * usage-sampler.js: the background clock behind each provider card's
 * "Query every N min in background to update the usage progress bar" checkbox.
 *
 * The provider stores (vendor/anthropic-data.js, vendor/openai-data.js) are pull-based:
 * they fetch, and record a history sample, only when something ASKS and the cached
 * reading has aged past the refresh rate. Left alone, only the popup asks, so the
 * reading is as old as the last time the popup was open, and the session history in the
 * Usage tab charts the user's popup habits (dense while they watch, empty while they
 * work) instead of the session.
 *
 * With the checkbox ticked, an alarm fires at that provider's refresh rate and forces a
 * reading whether or not the popup is open. Unticked, nothing runs in the background.
 *
 * browser.alarms, not setInterval: an MV3 service worker is evicted after 30s idle and an
 * alarm wakes it back up, where an interval dies with the worker and takes the clock with
 * it. Gecko's persistent background page would tolerate either; one mechanism for both
 * trees is worth more than the indirection it saves.
 *
 * Instantiate ONCE, at the top level of background.js — the constructor registers the
 * onAlarm listener, and an MV3 worker only keeps listeners added on its first synchronous
 * evaluation.
 */
(() => {
  'use strict';

  // Namespaced so handleAlarm can ignore every other alarm in the extension (the
  // reconnect watchdog shares this event), and so a stale alarm from an older build is
  // recognisable by name.
  const ALARM_PREFIX = 'browserplug-usage-';
  const alarmName = (key) => ALARM_PREFIX + key;

  // Deliberately the same expression the stores clamp with: the alarm period IS the
  // cache TTL, and two clamps that disagree would leave the tick landing a breath before
  // the cache expires, sampling nothing.
  const refreshMinutesOf = (m) => Math.min(60, Math.max(1, Number(m) || 5));

  // opts.alarms     : a browser.alarms-like {create, clear, get, onAlarm}
  // opts.providers  : [{ key, sample }] — `key` names the alarm and indexes the settings
  //                   object passed to apply(); `sample` forces one reading, i.e. the
  //                   store's get(true). Forced, because the tick lands exactly at the
  //                   TTL boundary and a millisecond of drift either way would otherwise
  //                   decide whether the sample happens at all.
  // opts.onError    : reporter for a failed sample or a refused alarm call.
  function createSampler(opts) {
    opts = opts || {};
    const alarms = opts.alarms || null;
    const providers = (opts.providers || []).slice();
    const onError = opts.onError || (() => {});
    const byAlarm = new Map();
    const armed = new Map(); // key -> period in minutes, for whoever asks what is running
    for (const p of providers) byAlarm.set(alarmName(p.key), p);

    if (alarms && alarms.onAlarm && alarms.onAlarm.addListener) {
      alarms.onAlarm.addListener((alarm) => { handleAlarm(alarm && alarm.name); });
    }

    function handleAlarm(name) {
      const provider = byAlarm.get(name);
      if (!provider) return false; // somebody else's alarm; the watchdog's, normally
      try {
        const p = provider.sample();
        if (p && p.catch) p.catch(onError);
      } catch (e) { onError(e); }
      return true;
    }

    // Bring the alarms in line with the settings, which are keyed by provider key:
    //   { anthropic: { mode: 'client', backgroundRefresh: true, refreshMinutes: 5 }, … }
    // A clock runs only when the checkbox is ticked AND the provider is shown somewhere.
    // `disabled` must not fetch at all, checkbox or not: the Options card promises that.
    // Serialised, because arming is a read-then-write: the call at startup and a storage
    // change landing in the same tick would both see "no alarm yet", both create one, and
    // the second create would restart the schedule the first had just set.
    let pending = Promise.resolve();
    function apply(settings) {
      pending = pending.catch(() => {}).then(() => applyNow(settings));
      return pending;
    }

    async function applyNow(settings) {
      settings = settings || {};
      for (const provider of providers) {
        const s = settings[provider.key] || {};
        const on = s.backgroundRefresh === true && !!s.mode && s.mode !== 'disabled';
        await arm(provider.key, on ? refreshMinutesOf(s.refreshMinutes) : null);
      }
    }

    async function arm(key, minutes) {
      if (!alarms) return;
      const name = alarmName(key);
      if (minutes === null) {
        armed.delete(key);
        try { await alarms.clear(name); } catch (e) { onError(e); }
        return;
      }
      // Creating an alarm RESTARTS its schedule, and this runs again on every cold start
      // of an MV3 worker — which, while the server is down, the reconnect watchdog causes
      // every 30 seconds. Re-creating unconditionally would push the next sample past the
      // horizon forever and the clock would never once strike. So ask first, and leave a
      // correctly-running alarm alone.
      try {
        const existing = await alarms.get(name);
        if (existing && existing.periodInMinutes === minutes) { armed.set(key, minutes); return; }
      } catch (e) { /* no alarms.get here: fall through and (re)create */ }
      try {
        alarms.create(name, { delayInMinutes: minutes, periodInMinutes: minutes });
        armed.set(key, minutes);
      } catch (e) { onError(e); }
    }

    return {
      apply,
      handleAlarm,
      armedPeriod: (key) => (armed.has(key) ? armed.get(key) : null),
    };
  }

  // Builds up to 1.9.1 had a third display mode, 'always': a line of its own above
  // Connections, plus this clock. The Usage tab replaced the line and the clock became a
  // checkbox, so a stored 'always' turns into 'tab' (it never drew on the rows either)
  // with the checkbox ticked. DeepSeek is a local clock with nothing to fetch, so it has
  // no checkbox. Returns the keys to write back, or null when nothing needs migrating.
  const LEGACY_ALWAYS = {
    anthropicDisplayMode: 'anthropicBackgroundRefresh',
    openaiDisplayMode: 'openaiBackgroundRefresh',
    deepseekDisplayMode: null,
  };
  const LEGACY_MODE_KEYS = Object.keys(LEGACY_ALWAYS);

  function legacyModePatch(stored) {
    const patch = {};
    for (const [modeKey, clockKey] of Object.entries(LEGACY_ALWAYS)) {
      if (!stored || stored[modeKey] !== 'always') continue;
      patch[modeKey] = 'tab';
      if (clockKey) patch[clockKey] = true;
    }
    return Object.keys(patch).length ? patch : null;
  }

  // Up to 1.9.1 the DeepSeek clock defaulted to 'client'; it now defaults to 'disabled'. A
  // profile that never saved the setting showed the clock only through that default, so an
  // update from such a build writes 'client' down instead of quietly removing the clock. A
  // fresh install, an update from a later build and a saved choice are left alone.
  // `stored` is storage.sync's answer for ['deepseekDisplayMode']. Returns the keys to
  // write, or null.
  const LAST_DEEPSEEK_CLIENT_DEFAULT = [1, 9, 1];
  function versionAtMost(version, max) {
    if (!/^\d+(\.\d+)*$/.test(String(version || ''))) return false;
    const parts = String(version).split('.').map(Number);
    for (let i = 0; i < Math.max(parts.length, max.length); i++) {
      const a = parts[i] || 0, b = max[i] || 0;
      if (a !== b) return a < b;
    }
    return true;
  }
  function legacyDefaultsPatch(previousVersion, stored) {
    if (!versionAtMost(previousVersion, LAST_DEEPSEEK_CLIENT_DEFAULT)) return null;
    if (stored && typeof stored.deepseekDisplayMode === 'string') return null;
    return { deepseekDisplayMode: 'client' };
  }

  globalThis.UsageSampler = {
    ALARM_PREFIX, alarmName, refreshMinutesOf, createSampler, LEGACY_MODE_KEYS, legacyModePatch,
    legacyDefaultsPatch,
  };
})();
