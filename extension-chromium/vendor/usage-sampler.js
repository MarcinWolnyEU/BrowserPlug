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
  // opts.storage    : a storage.local-like {get, set}, optional. Holds the back-off below, so
  //                   an MV3 worker evicted between two ticks does not forget it.
  //
  // Back-off: a sample that answers `signedOut` makes the clock skip the next 1, then 2, 4,
  // ... ticks of that provider, up to about an hour of wall time. A profile with no session
  // would otherwise fire the same failing request at the full rate for ever. The first
  // answer that is not signed out, a change to that provider's alarm, or wake(key) (the
  // popup or Options asking for a reading) puts it back to the full rate.
  const BACKOFF_KEY = 'usageSamplerBackoff';
  const BACKOFF_CAP_MINUTES = 60;

  function createSampler(opts) {
    opts = opts || {};
    const alarms = opts.alarms || null;
    const storage = opts.storage || null;
    const providers = (opts.providers || []).slice();
    const onError = opts.onError || (() => {});
    const byAlarm = new Map();
    const armed = new Map(); // key -> period in minutes, for whoever asks what is running
    const backoff = new Map(); // key -> { gap, skip } ticks, see above
    for (const p of providers) byAlarm.set(alarmName(p.key), p);

    // Null once the stored back-off is read (at once when there is no storage).
    let loading = null;
    if (storage) {
      loading = Promise.resolve()
        .then(() => storage.get(BACKOFF_KEY))
        .then((r) => {
          const saved = (r && r[BACKOFF_KEY]) || {};
          for (const p of providers) {
            const s = saved[p.key];
            if (s && s.gap >= 1 && s.skip >= 0) backoff.set(p.key, { gap: s.gap, skip: s.skip });
          }
        })
        .catch(onError)
        .then(() => { loading = null; });
    }
    const whenLoaded = (fn) => { if (loading) loading.then(fn); else fn(); };
    function save() {
      if (!storage) return;
      try {
        Promise.resolve(storage.set({ [BACKOFF_KEY]: Object.fromEntries(backoff) })).catch(onError);
      } catch (e) { onError(e); }
    }

    if (alarms && alarms.onAlarm && alarms.onAlarm.addListener) {
      alarms.onAlarm.addListener((alarm) => { handleAlarm(alarm && alarm.name); });
    }

    function handleAlarm(name) {
      const provider = byAlarm.get(name);
      if (!provider) return false; // somebody else's alarm; the watchdog's, normally
      whenLoaded(() => tick(provider));
      return true;
    }

    function tick(provider) {
      const state = backoff.get(provider.key);
      if (state && state.skip > 0) { state.skip--; save(); return; }
      let p;
      try { p = provider.sample(); } catch (e) { onError(e); return; }
      Promise.resolve(p).then((r) => noteAnswer(provider.key, !!(r && r.signedOut)), onError);
    }

    function noteAnswer(key, signedOut) {
      if (!signedOut) { wake(key); return; }
      // The cap counts skipped ticks plus the one sampled, so a 5-minute clock backs off to
      // one sample an hour; a clock already at 60 minutes has no room to back off.
      const room = Math.floor(BACKOFF_CAP_MINUTES / (armed.get(key) || 5)) - 1;
      const previous = backoff.get(key);
      const gap = Math.min(previous ? previous.gap * 2 : 1, room);
      if (gap < 1) return;
      backoff.set(key, { gap, skip: gap });
      save();
    }

    // Back to the full rate for one provider.
    function wake(key) {
      whenLoaded(() => { if (backoff.delete(key)) save(); });
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
        wake(key);
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
        wake(key);   // a new schedule starts at the full rate
      } catch (e) { onError(e); }
    }

    return {
      apply,
      handleAlarm,
      wake,
      armedPeriod: (key) => (armed.has(key) ? armed.get(key) : null),
    };
  }

  // popup.js and options.js read a stored display mode they do not know (a newer build's, a
  // hand edit, the retired 'always') as the card's shipped default. The clock has to read it the same way, or a
  // card that both pages show as Disabled would still be polled in the background.
  // `defaults` is the background's USAGE_SETTINGS_DEFAULTS; only its *DisplayMode keys are
  // looked at. Returns a copy.
  const DISPLAY_MODES = ['disabled', 'tab', 'client'];
  function withKnownModes(settings, defaults) {
    const out = Object.assign({}, settings);
    for (const key of Object.keys(defaults || {})) {
      if (/DisplayMode$/.test(key) && !DISPLAY_MODES.includes(out[key])) out[key] = defaults[key];
    }
    return out;
  }

  globalThis.UsageSampler = {
    ALARM_PREFIX, alarmName, refreshMinutesOf, createSampler, withKnownModes,
  };
})();
