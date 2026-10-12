/*
 * BrowserPlug: usage history per reset period (the sampling half of the usage chart)
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Loaded in the BACKGROUND page; publishes `PeriodHistory`. A provider's usage store
 * (anthropic-data.js, openai-data.js, copilot-data.js) owns one tracker per quota that
 * resets on a schedule longer than a session (a week; for Copilot a month), feeds it every
 * good reading, and spreads the answer over what it returns, so the popup can draw that
 * quota on an axis of its own period (usage-chart.js) instead of the 5-hour one.
 *
 * What the tracker decides is where the CURRENT period begins, because that is the chart's
 * left edge and it is not always `resets_at` minus one period:
 *   - `resets_at` moved: the scheduled rollover, or a reset granted early that restarted
 *     the clock. The old samples are dropped.
 *   - usage fell while `resets_at` stayed put: a reset that kept the old deadline. The old
 *     samples are dropped, and the period starts at this reading, so the chart shows the
 *     time that actually remains instead of a full-length axis with a dip to zero.
 * A session (5-hour) reset is none of these and never touches these samples.
 *
 * `key` names the quota ('weekly', 'monthly'): it is the sample field, the prefix of the
 * `<key>History` / `<key>Window` fields attach() adds, and of the persisted fields.
 *
 * createAccountSet() is the persistence all three stores share. Samples belong to an
 * ACCOUNT (the claude.ai organization, the ChatGPT account id, the github.com login), so
 * each account keeps its own, and signing in as somebody else neither shows their numbers
 * under yours nor loses yours: signing back in finds them where they were.
 */
(function () {
  'use strict';

  const WEEK_MS = 7 * 24 * 3600e3;
  // One sample per 5 minutes at most, so a week holds ~2000 points however often the
  // popup forces a refresh (a month, ~9000, is capped by MAX_SAMPLES: the oldest go).
  const SAMPLE_GAP_MS = 5 * 60000;
  const MAX_SAMPLES = 2100;
  // Inside one period usage only climbs, so a fall of more than this many points between
  // two samples is a reset.
  const RESET_DROP = 1;
  // `resets_at` jitters by a few ms between calls; a real move is far larger.
  const MOVED_MS = 2 * 60000;

  // The same calendar day and time one month before `endMs`, in UTC.
  function monthBefore(endMs) {
    const d = new Date(endMs);
    d.setUTCMonth(d.getUTCMonth() - 1);
    return d.getTime();
  }

  /**
   * @param {{key?: string, periodMs?: number, nominalStart?: function(number): number}} [opts]
   *   nominalStart(endMs) is where a period ending at endMs would normally begin; the
   *   default is one `periodMs` (a week) earlier.
   */
  function create(opts) {
    opts = opts || {};
    const key = opts.key || 'weekly';
    let periodMs = opts.periodMs || WEEK_MS;
    const nominalStart = opts.nominalStart || ((e) => e - periodMs);
    let samples = [], end = null, start = null;

    // `w` is a parsed window {percent, resetsAt, windowSeconds?}; `t` the reading's time.
    function record(w, t) {
      const e = w && w.resetsAt ? new Date(w.resetsAt).getTime() : null;
      if (!e) return;
      if (!opts.nominalStart && w.windowSeconds > 0) periodMs = w.windowSeconds * 1000;
      const percent = Number(w.percent) || 0;
      let last = samples[samples.length - 1];
      const moved = !!end && Math.abs(e - end) > MOVED_MS;
      const dropped = !!last && last[key] - percent > RESET_DROP;
      if (moved || dropped || !end) {
        // A reset cannot predate the last old sample, and a period cannot start in the future.
        const nominal = nominalStart(e);
        start = dropped && !moved ? t : Math.min(Math.max(nominal, last ? last.t : nominal), t);
        samples = [];
        last = null;
      }
      end = e;
      if (last && t - last.t < SAMPLE_GAP_MS) return;
      samples.push({ t, [key]: percent });
      if (samples.length > MAX_SAMPLES) samples.splice(0, samples.length - MAX_SAMPLES);
    }

    // Adds `<key>History` / `<key>Window` to a reading handed to the popup.
    function attach(out) {
      out[`${key}History`] = samples.slice();
      if (end) {
        const s = start && start < end ? start : nominalStart(end);
        out[`${key}Window`] = { start: s, end };
      }
      return out;
    }

    function toJSON() {
      return { [`${key}End`]: end, [`${key}Start`]: start, [`${key}Samples`]: samples };
    }

    function restore(saved) {
      const got = saved && saved[`${key}Samples`];
      if (!Array.isArray(got)) return;
      samples = got.slice(-MAX_SAMPLES);
      end = saved[`${key}End`] || null;
      start = saved[`${key}Start`] || null;
    }

    function clear() { samples = []; end = null; start = null; }

    return { record, attach, toJSON, restore, clear };
  }

  // How many accounts' samples are kept. The one used longest ago goes first.
  const MAX_ACCOUNTS = 6;

  /**
   * One storage.local entry holding the history of every account the store has seen:
   * { accounts: { <id>: { seen, data } } }.
   *   opts.local  storage.local-like {get,set,remove}; omit -> memory only
   *   opts.key    the storage key
   *   opts.create()            a fresh, empty per-account state
   *   opts.save(state)         the state as JSON
   *   opts.load(state, json)   fill a fresh state from what save() produced
   * An entry written before accounts existed (no `accounts` field) belongs to whichever
   * account is seen first: until then it was the only one the store could have had.
   */
  function createAccountSet(opts) {
    const local = opts.local || null;
    const accounts = new Map();   // id -> { state, seen }
    let legacy = null;

    const ready = (async () => {
      if (!local) return;
      try {
        const got = await local.get(opts.key);
        const saved = got && got[opts.key];
        if (!saved) return;
        if (saved.accounts && typeof saved.accounts === 'object') {
          for (const id of Object.keys(saved.accounts)) {
            const rec = saved.accounts[id];
            if (!rec || !rec.data) continue;
            const state = opts.create();
            opts.load(state, rec.data);
            accounts.set(id, { state, seen: Number(rec.seen) || 0 });
          }
        } else {
          legacy = saved;
        }
      } catch (e) { /* first run or unavailable: start empty */ }
    })();

    // The state for `id`, created on first sight. Callers await `ready` before the first use.
    function use(id) {
      id = String(id || 'unknown');
      let rec = accounts.get(id);
      if (!rec) {
        const state = opts.create();
        if (legacy) {
          try { opts.load(state, legacy); } catch (e) { /* unreadable: start empty */ }
          legacy = null;
        }
        rec = { state, seen: 0 };
        accounts.set(id, rec);
        while (accounts.size > MAX_ACCOUNTS) {
          let oldest = null;
          for (const [k, v] of accounts) {
            if (k !== id && (oldest === null || v.seen < accounts.get(oldest).seen)) oldest = k;
          }
          accounts.delete(oldest);
        }
      }
      rec.seen = Date.now();
      return rec.state;
    }

    function persist() {
      if (!local) return;
      try {
        const out = {};
        for (const [id, rec] of accounts) out[id] = { seen: rec.seen, data: opts.save(rec.state) };
        const p = local.set({ [opts.key]: { accounts: out } });
        if (p && p.catch) p.catch(() => {});
      } catch (e) { /* best-effort */ }
    }

    // Everything, every account, in memory and in storage ("Reset to Defaults").
    function clear() {
      accounts.clear();
      legacy = null;
      if (local) { try { const p = local.remove(opts.key); if (p && p.catch) p.catch(() => {}); } catch (e) { /* best-effort */ } }
    }

    return { ready, use, persist, clear };
  }

  globalThis.PeriodHistory = { create, createAccountSet, monthBefore, WEEK_MS };
})();
