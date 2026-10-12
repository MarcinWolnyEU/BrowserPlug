/*
 * BrowserPlug: Anthropic (claude.ai) usage: DATA layer
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Fetch / parse / cache / persist the ACCOUNT-WIDE claude.ai usage. Loaded in the
 * BACKGROUND page (needs the logged-in cookies + storage). Publishes `AnthropicData`.
 * The display half is anthropic-view.js. Source: GET
 * https://claude.ai/api/organizations/{org}/usage (first-party, cookie-auth).
 */
(function () {
  'use strict';

  const API = 'https://claude.ai/api';
  const SESSION_WINDOW_MS = 5 * 3600e3;   // the "5-hour" session budget window
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  // ---- network + parse ----------------------------------------------------
  let orgId = null;

  async function apiJson(path) {
    const r = await fetch(API + path, { credentials: 'include', headers: { accept: 'application/json' } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  const isSignedOut = (message) => /^HTTP 40[13]$/.test(message);

  // Pick the subscription org (Max/Pro/Team/Free); skip API-eval orgs whose
  // rate_limit_tier reads like "auto_api_evaluation".
  async function resolveOrgId() {
    if (orgId) return orgId;
    const orgs = await apiJson('/organizations');
    const list = Array.isArray(orgs) ? orgs : (orgs.organizations || [orgs]);
    const isSub = (t) => typeof t === 'string' && !/api|evaluation/i.test(t);
    const pick = list.find(o => isSub(o.rate_limit_tier)) || list[0];
    orgId = pick && (pick.uuid || pick.id);
    if (!orgId) throw new Error('no organization');
    return orgId;
  }

  // Normalize the /usage payload → { session, weekly } with {percent, resetsAt}.
  // Prefers the new limits[] array; falls back to legacy five_hour / seven_day.
  function parseUsage(u) {
    let session = null, weekly = null;
    if (u && Array.isArray(u.limits)) {
      const s = u.limits.find(l => l.kind === 'session');
      const w = u.limits.find(l => l.kind === 'weekly_all');
      if (s) session = { percent: s.percent, resetsAt: s.resets_at, severity: s.severity };
      if (w) weekly = { percent: w.percent, resetsAt: w.resets_at, severity: w.severity };
    }
    if (!session && u && u.five_hour) session = { percent: u.five_hour.utilization, resetsAt: u.five_hour.resets_at };
    if (!weekly && u && u.seven_day) weekly = { percent: u.seven_day.utilization, resetsAt: u.seven_day.resets_at };
    return { session, weekly };
  }

  // Fetch + parse in one call. Uses the ambient `fetch` (cookies ride along in
  // the background page); throws on any network/HTTP error.
  async function fetchUsage() {
    const id = await resolveOrgId();
    const raw = await apiJson(`/organizations/${id}/usage`);
    const parsed = parseUsage(raw);
    return { ok: true, accountId: id, session: parsed.session, weekly: parsed.weekly, fetchedAt: Date.now() };
  }

  // Session-window rollover test. The /usage `resets_at` jitters by a few ms
  // between calls; a real reset moves it by ~5 hours. Treat ends within 2 minutes
  // as the SAME window, so accumulated history isn't wiped on every sample.
  const WINDOW_ROLLOVER_MS = 2 * 60000;
  function isNewSessionWindow(prevEnd, newEnd) {
    if (!newEnd || !prevEnd) return false;
    return Math.abs(newEnd - prevEnd) > WINDOW_ROLLOVER_MS;
  }

  // A failed fetch is not retried on every get(). The popup asks every 2 s while its
  // Usage tab is open, so a signed-out browser would otherwise send a request each time.
  // The failure's answer is replayed for RETRY_AFTER_FAILURE_MS, or the refresh interval
  // if that is shorter; get(true) skips the wait.
  const RETRY_AFTER_FAILURE_MS = 60000;

  // A stateful account-usage store: fetch + cache (honoring refreshMinutes) +
  // rolling per-session-window history + persistence. INSTANTIATE ONCE, in the
  // background page (pass it browser.storage.local); the popup never creates one.
  //   opts.local: a storage.local-like {get,set,remove}; omit → no persistence.
  //   opts.fetchUsage: injectable fetch (defaults to fetchUsage) (for tests).
  //   opts.refreshMinutes / historyKey / historyMax: tunables.
  function createUsageStore(opts) {
    opts = opts || {};
    const local = opts.local || null;
    const doFetch = opts.fetchUsage || fetchUsage;
    const HISTORY_KEY = opts.historyKey || 'anthropicUsageHistory';
    const HISTORY_MAX = opts.historyMax || 600;
    let refreshMs = clamp(Number(opts.refreshMinutes) || 5, 1, 60) * 60000;

    let cache = null, inflight = null;
    let failure = null;   // { at, answer } of the last failed fetch

    // Samples are kept per organization (res.accountId): signing in as another account
    // neither clears these nor shows them. Per account: the session history of the current
    // 5-hour window, and the weekly samples, which have their own axis and rollover rules
    // in period-history.js (a session reset must not wipe them).
    const accounts = globalThis.PeriodHistory.createAccountSet({
      local, key: HISTORY_KEY,
      create: () => ({ history: [], windowEnd: null, weekly: globalThis.PeriodHistory.create() }),
      save: (a) => Object.assign({ windowEnd: a.windowEnd, samples: a.history }, a.weekly.toJSON()),
      load: (a, saved) => {
        if (Array.isArray(saved.samples)) {
          a.history = saved.samples.slice(-HISTORY_MAX);
          a.windowEnd = saved.windowEnd || null;
        }
        a.weekly.restore(saved);
      },
    });
    const ready = accounts.ready;

    function record(res) {
      if (!res || !res.ok || !res.session) return;
      const a = accounts.use(res.accountId);
      const end = res.session.resetsAt ? new Date(res.session.resetsAt).getTime() : null;
      if (isNewSessionWindow(a.windowEnd, end)) { a.history = []; a.windowEnd = end; }
      else if (end && !a.windowEnd) a.windowEnd = end;
      a.history.push({
        t: res.fetchedAt || Date.now(),
        session: Number(res.session.percent) || 0,
      });
      if (a.history.length > HISTORY_MAX) a.history.splice(0, a.history.length - HISTORY_MAX);
      a.weekly.record(res.weekly, res.fetchedAt || Date.now());
      accounts.persist();
    }

    function attach(res) {
      const a = accounts.use(res.accountId);
      const out = Object.assign({}, res);
      out.history = a.history.slice();
      if (a.windowEnd) out.sessionWindow = { start: a.windowEnd - SESSION_WINDOW_MS, end: a.windowEnd };
      a.weekly.attach(out);
      return out;
    }

    async function get(force) {
      await ready;
      const now = Date.now();
      const fresh = cache && cache.ok && (now - cache.fetchedAt) < refreshMs;
      if (fresh && !force) return attach(cache);
      if (failure && !force && now - failure.at < Math.min(refreshMs, RETRY_AFTER_FAILURE_MS)) {
        return Object.assign({}, failure.answer);
      }
      if (inflight) return inflight;
      inflight = doFetch()
        .then(res => { cache = res; failure = null; record(res); return attach(res); })
        .catch(err => {
          const message = String(err.message || err);
          let answer;
          if (isSignedOut(message)) {
            // claude.ai answers 401/403 when the profile has no session. Those numbers
            // belong to an account this browser has left, so they are dropped, not kept as
            // stale, and the cached org goes too: the next sign-in may be a different one.
            orgId = null;
            cache = { ok: false, error: message, signedOut: true, fetchedAt: Date.now() };
            answer = Object.assign({}, cache);
          } else if (cache && cache.ok) {
            answer = attach(Object.assign({}, cache, { stale: true, error: message }));
          } else {
            cache = { ok: false, error: message, fetchedAt: Date.now() };
            answer = Object.assign({}, cache);
          }
          failure = { at: Date.now(), answer };
          return Object.assign({}, answer);
        })
        .finally(() => { inflight = null; });
      return inflight;
    }

    function setRefreshMinutes(m) {
      refreshMs = clamp(Number(m) || 5, 1, 60) * 60000;
    }

    // Full reset (used by "Reset to Defaults"): clear in-memory + persisted history,
    // every account's.
    function clearHistory() {
      accounts.clear(); cache = null; failure = null;
    }

    return { get, setRefreshMinutes, clearHistory };
  }

  globalThis.AnthropicData = {
    API, SESSION_WINDOW_MS,
    fetchUsage, parseUsage, resolveOrgId, isNewSessionWindow, createUsageStore,
  };
})();
