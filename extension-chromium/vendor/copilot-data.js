/*
 * BrowserPlug: GitHub Copilot usage: DATA layer
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Fetch / parse / cache the monthly Copilot quotas of the github.com account this
 * browser profile is signed in to. Loaded in the BACKGROUND page (needs the logged-in
 * cookies). Publishes `CopilotData`. The display half is copilot-view.js.
 *
 * Two calls per refresh, both on github.com's own cookies, both needing
 * `Accept: application/json` (without it the entitlement call answers 400):
 *
 *   1. GET /github-copilot/chat?skip_anchor=true -> { currentUserLogin, … }
 *   2. GET /github-copilot/chat/entitlement      -> { plan, quotas: { … }, … }
 *
 * Signed out, both answer 404 {"error":"Not Found"}; the first call is the one that
 * decides SIGNED_OUT, so a 404 from the second means something else.
 *
 * The first call exists for the login. GitHub lets one browser hold several accounts
 * and switch between them, and Copilot in VS Code signs in on its own, so the account
 * this browser reads can differ from the one the editor spends. The server reads the
 * editor's account out of its Copilot log (server/usage_panels/copilot_data.py) and copilot-view.js
 * compares the two, so one account's numbers are never presented as the other's.
 * That response also carries the caller's IP address; only the login is kept.
 */
(function () {
  'use strict';

  const ORIGIN = 'https://github.com';
  const LOGIN_URL = ORIGIN + '/github-copilot/chat?skip_anchor=true';
  const ENTITLEMENT_URL = ORIGIN + '/github-copilot/chat/entitlement';
  const SIGNED_OUT = 'not signed in to github.com';
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const JSON_GET = { credentials: 'include', headers: { accept: 'application/json' } };

  // ---- network ------------------------------------------------------------

  async function fetchLogin() {
    const r = await fetch(LOGIN_URL, JSON_GET);
    if (r.status === 404) throw new Error(SIGNED_OUT);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json().catch(() => null);
    const login = j && (j.currentUserLogin || (j.currentUser && j.currentUser.login));
    if (!login) throw new Error(SIGNED_OUT);
    return String(login);
  }

  async function fetchEntitlementRaw() {
    const r = await fetch(ENTITLEMENT_URL, JSON_GET);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  // ---- parsing ------------------------------------------------------------

  // One quota in the shape the views draw: `percent` is USED, like every other provider
  // here, although GitHub reports the remainder. A quota the plan does not include
  // (`hasQuota: false`, e.g. premium requests on Copilot Free) is null rather than a 0%
  // bar that reads as "plenty left".
  function parseQuota(q) {
    if (!q || q.hasQuota === false) return null;
    const unlimited = !!q.unlimited;
    const total = Number(q.total) || 0;
    const used = Number(q.used) || 0;
    // null and '' mean "not reported", not 0% left: Number(null) is 0.
    const left = q.percentRemaining == null || q.percentRemaining === '' ? NaN : Number(q.percentRemaining);
    let percent = 100 - clamp(left, 0, 100);
    if (!isFinite(percent)) percent = total ? clamp((used / total) * 100, 0, 100) : 0;
    return {
      percent: unlimited ? 0 : Math.round(percent * 10) / 10,
      used,
      total,
      unlimited,
    };
  }

  function parseEntitlement(e) {
    const q = (e && e.quotas) || {};
    const resetsAt = q.resetDateUtc || (q.resetDate ? `${q.resetDate}T00:00:00.000Z` : null);
    return {
      plan: (e && e.plan) || null,
      licenseType: (e && e.licenseType) || null,
      resetsAt,
      chat: parseQuota(q.chatQuota),
      completions: parseQuota(q.completionsQuota),
      premium: parseQuota(q.premiumInteractionsQuota),
    };
  }

  // Fetch + parse, both legs. Throws on any failure; SIGNED_OUT is the one message the
  // view special-cases, so keep its spelling.
  async function fetchUsage() {
    const login = await fetchLogin();
    const parsed = parseEntitlement(await fetchEntitlementRaw());
    return Object.assign({ ok: true, fetchedAt: Date.now(), login }, parsed);
  }

  // ---- store --------------------------------------------------------------

  // A failed fetch is not retried on every get(). The popup asks every 2 s while its
  // Usage tab is open, so a signed-out browser would otherwise send a request each time.
  // The failure's answer is replayed for RETRY_AFTER_FAILURE_MS, or the refresh interval
  // if that is shorter; get(true) skips the wait.
  const RETRY_AFTER_FAILURE_MS = 60000;

  // The quota the chart follows: the one copilot-view.js's primaryQuota() puts in the bar
  // (premium requests, else chat; an unlimited quota has nothing to plot). Keep the two in
  // step by hand: the view files are loaded separately and cannot import this.
  function chartedQuota(u) {
    if (u.premium && !u.premium.unlimited) return u.premium;
    if (u.chat && !u.chat.unlimited) return u.chat;
    return null;
  }

  // The code-completions quota the chart draws as its second line; null when the plan does
  // not cap it, as for the bar's secondary half.
  function chartedCompletions(u) {
    return u.completions && !u.completions.unlimited ? u.completions : null;
  }

  // Cache + refresh interval + in-flight de-duplication, plus the monthly history behind
  // the Usage tab's chart (period-history.js): a line for the charted quota and one for code
  // completions, both on the axis of their own month. INSTANTIATE ONCE, in the background.
  //   opts.local: a storage.local-like {get,set,remove}; omit -> no persistence.
  //   opts.fetchUsage: injectable fetch (for tests).
  //   opts.refreshMinutes: 1..60.
  function createUsageStore(opts) {
    opts = opts || {};
    const local = opts.local || null;
    const HISTORY_KEY = opts.historyKey || 'copilotUsageHistory';
    const doFetch = opts.fetchUsage || fetchUsage;
    // Monthly samples per github.com login, so signing in as another account neither
    // clears them nor shows them. The two quotas are tracked separately: each decides its own
    // period start after a reset, and an entry saved before completions were sampled simply
    // loads with an empty completions line.
    const month = (key) => globalThis.PeriodHistory.create({ key, nominalStart: globalThis.PeriodHistory.monthBefore });
    const accounts = globalThis.PeriodHistory.createAccountSet({
      local, key: HISTORY_KEY,
      create: () => ({ monthly: month('monthly'), completions: month('completions') }),
      save: (a) => Object.assign(a.monthly.toJSON(), a.completions.toJSON()),
      load: (a, saved) => { a.monthly.restore(saved); a.completions.restore(saved); },
    });
    const loginOf = (res) => String(res.login || '').toLowerCase();
    let refreshMs = clamp(Number(opts.refreshMinutes) || 5, 1, 60) * 60000;
    let cache = null, inflight = null;
    let failure = null;   // { at, answer } of the last failed fetch

    const ready = accounts.ready;

    function record(res) {
      const q = chartedQuota(res), c = chartedCompletions(res);
      if (!(q || c) || !res.resetsAt) return;
      const a = accounts.use(loginOf(res));
      const t = res.fetchedAt || Date.now();
      if (q) a.monthly.record({ percent: q.percent, resetsAt: res.resetsAt }, t);
      if (c) a.completions.record({ percent: c.percent, resetsAt: res.resetsAt }, t);
      accounts.persist();
    }

    // The cache holds the bare reading; what the popup gets also carries the samples.
    function attach(res) {
      const a = accounts.use(loginOf(res));
      return a.completions.attach(a.monthly.attach(Object.assign({}, res)));
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
          const message = String((err && err.message) || err);
          // A failed refresh keeps the last good numbers, flagged stale. A sign-out
          // does not: those numbers belong to an account this browser has left.
          let answer;
          if (cache && cache.ok && message !== SIGNED_OUT) {
            answer = attach(Object.assign({}, cache, { stale: true, error: message }));
          } else {
            cache = { ok: false, error: message, signedOut: message === SIGNED_OUT, fetchedAt: Date.now() };
            answer = cache;
          }
          failure = { at: Date.now(), answer };
          return Object.assign({}, answer);
        })
        .finally(() => { inflight = null; });
      return inflight;
    }

    function setRefreshMinutes(m) { refreshMs = clamp(Number(m) || 5, 1, 60) * 60000; }
    function clearCache() { cache = null; failure = null; }
    // Full reset (used by "Reset to Defaults"): the samples too, every account's.
    function clearHistory() { clearCache(); accounts.clear(); }

    return { get, setRefreshMinutes, clearCache, clearHistory };
  }

  globalThis.CopilotData = {
    ORIGIN, LOGIN_URL, ENTITLEMENT_URL, SIGNED_OUT,
    fetchLogin, fetchUsage, parseEntitlement, parseQuota, createUsageStore,
  };
})();
