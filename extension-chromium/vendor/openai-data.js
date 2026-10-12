/*
 * BrowserPlug: OpenAI (chatgpt.com) usage: DATA layer
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Fetch / parse / cache the ACCOUNT-WIDE ChatGPT and Codex rate-limit windows. Loaded
 * in the BACKGROUND page (needs the logged-in cookies + storage). Publishes `OpenAIData`.
 * The display half is openai-view.js.
 *
 * Unlike claude.ai, this takes TWO calls, and the second one is why:
 *
 *   1. GET https://chatgpt.com/api/auth/session      (cookies)  -> { accessToken, account }
 *   2. GET https://chatgpt.com/backend-api/wham/usage (Bearer)  -> { rate_limit: {...} }
 *
 * The usage endpoint refuses cookies alone (401); the bearer from step 1 is what opens
 * it, and it is short-lived, so it is fetched fresh on every refresh rather than cached.
 * It is the same endpoint the Codex CLI itself reads, so the numbers here are the ones
 * `codex /status` prints, for ChatGPT and Codex together — they share one allowance.
 *
 * DO NOT read the plan from /backend-api/accounts/check. It answers 200 to cookies
 * alone and reports a Plus account as `plan_type: "guest"`, `plan_display_name: "Free"`
 * — a wrong answer is worse than the 401 the usage endpoint gives, because nothing
 * about it looks like a failure. The plan comes from the session or from the usage
 * payload, both of which have seen the bearer.
 */
(function () {
  'use strict';

  const ORIGIN = 'https://chatgpt.com';
  const SESSION_URL = ORIGIN + '/api/auth/session';
  const USAGE_URL = ORIGIN + '/backend-api/wham/usage';

  // What the two windows are called once you know their length. OpenAI publishes the
  // window as a duration, not a name, and the durations are the only stable part: the
  // primary has been five hours and the secondary a week, but reading the label off the
  // number means a change to either shows up as a correct label rather than a wrong one.
  const SIGNED_OUT = 'not signed in to chatgpt.com';
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  function windowName(seconds) {
    const s = Number(seconds) || 0;
    if (!s) return 'window';
    if (s % 86400 === 0) {
      const d = s / 86400;
      return d === 7 ? 'weekly' : `${d}-day`;
    }
    if (s % 3600 === 0) return `${s / 3600}-hour`;
    return `${Math.round(s / 60)}-minute`;
  }

  // ---- network ------------------------------------------------------------

  // The bearer, and who it belongs to. A signed-out profile still answers 200 here,
  // with an empty object — so "no accessToken" IS the signed-out signal, and it has to
  // be tested for rather than waited on as an HTTP error that never comes.
  async function fetchSession() {
    const r = await fetch(SESSION_URL, { credentials: 'include', headers: { accept: 'application/json' } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const s = await r.json().catch(() => null);
    const token = s && s.accessToken;
    if (!token) throw new Error(SIGNED_OUT);
    return {
      accessToken: token,
      accountId: (s.account && (s.account.id || s.account.account_id)) || null,
      plan: (s.account && s.account.planType) || null,
      expires: s.expires || null,
    };
  }

  async function fetchUsageRaw(session) {
    const headers = { accept: 'application/json', authorization: `Bearer ${session.accessToken}` };
    if (session.accountId) headers['ChatGPT-Account-Id'] = session.accountId;
    const r = await fetch(USAGE_URL, { credentials: 'include', headers });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  // ---- parsing ------------------------------------------------------------

  // `reset_at` is epoch SECONDS here (claude.ai sends an ISO string), so it is converted
  // once, at the edge, and everything downstream sees the same shape as the Anthropic
  // store: {percent, resetsAt} with resetsAt a value `new Date()` understands.
  function parseWindow(w) {
    if (!w) return null;
    const at = Number(w.reset_at);
    return {
      percent: clamp(Number(w.used_percent) || 0, 0, 100),
      resetsAt: at ? new Date(at * 1000).toISOString() : null,
      windowSeconds: Number(w.limit_window_seconds) || null,
      windowName: windowName(w.limit_window_seconds),
    };
  }

  function parseUsage(u) {
    const rl = (u && u.rate_limit) || {};
    const credits = (u && u.credits) || null;
    return {
      session: parseWindow(rl.primary_window),
      weekly: parseWindow(rl.secondary_window),
      plan: (u && u.plan_type) || null,
      // `allowed: false` / `limit_reached: true` is the state where the next request is
      // refused; the view paints it red rather than leaving a 100% bar to be read as
      // "nearly full".
      limitReached: !!(rl.limit_reached || rl.allowed === false),
      reachedType: (u && u.rate_limit_reached_type) || null,
      creditBalance: credits ? credits.balance : null,
      hasCredits: credits ? !!credits.has_credits : null,
    };
  }

  // Fetch + parse, both legs. Throws on any failure; `SIGNED_OUT` is the one message
  // the view special-cases, so keep its spelling.
  async function fetchUsage() {
    const session = await fetchSession();
    const parsed = parseUsage(await fetchUsageRaw(session));
    if (!parsed.plan) parsed.plan = session.plan;
    return Object.assign({ ok: true, fetchedAt: Date.now(), accountId: session.accountId }, parsed);
  }

  // ---- store --------------------------------------------------------------

  // A failed fetch is not retried on every get(). The popup asks every 2 s while its
  // Usage tab is open, so a signed-out browser would otherwise send a request each time.
  // The failure's answer is replayed for RETRY_AFTER_FAILURE_MS, or the refresh interval
  // if that is shorter; get(true) skips the wait.
  const RETRY_AFTER_FAILURE_MS = 60000;

  // Session-window rollover test, as in anthropic-data.js (copied: the data files are loaded
  // separately). `reset_at` jitters by a moment between calls; a real rollover moves it by
  // the window's length.
  const WINDOW_ROLLOVER_MS = 2 * 60000;
  const SESSION_FALLBACK_MS = 5 * 3600e3;
  // Usage only climbs inside a window, so a fall of more than this many points is a reset
  // that kept the deadline.
  const RESET_DROP = 1;

  // Cache + refresh interval + in-flight de-duplication, plus the history behind the Usage
  // tab's chart: the session samples of the current 5-hour window and the weekly ones
  // (period-history.js), which have their own axis and rollover rules. INSTANTIATE ONCE, in
  // the background page.
  //   opts.local: a storage.local-like {get,set,remove}; omit -> no persistence.
  //   opts.fetchUsage: injectable fetch (for tests).
  //   opts.refreshMinutes: 1..60.
  function createUsageStore(opts) {
    opts = opts || {};
    const local = opts.local || null;
    const HISTORY_KEY = opts.historyKey || 'openaiUsageHistory';
    const HISTORY_MAX = opts.historyMax || 600;
    const doFetch = opts.fetchUsage || fetchUsage;
    // Samples per ChatGPT account (res.accountId), so signing in as another account
    // neither clears them nor shows them. An entry saved before the session samples existed
    // has only the weekly part, and loads as an empty session history.
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
    let refreshMs = clamp(Number(opts.refreshMinutes) || 5, 1, 60) * 60000;
    let cache = null, inflight = null;
    let failure = null;   // { at, answer } of the last failed fetch

    const ready = accounts.ready;

    // The session window's length comes from the payload; a reading without one is a 5-hour window.
    const sessionMs = (res) => (res.session && res.session.windowSeconds > 0
      ? res.session.windowSeconds * 1000 : SESSION_FALLBACK_MS);

    function recordSession(a, res) {
      const s = res.session;
      if (!s) return;
      const end = s.resetsAt ? new Date(s.resetsAt).getTime() : null;
      const percent = Number(s.percent) || 0;
      const last = a.history[a.history.length - 1];
      const moved = !!end && !!a.windowEnd && Math.abs(end - a.windowEnd) > WINDOW_ROLLOVER_MS;
      const dropped = !!last && last.session - percent > RESET_DROP;
      if (moved || dropped) { a.history = []; a.windowEnd = end || a.windowEnd; }
      else if (end && !a.windowEnd) a.windowEnd = end;
      a.history.push({ t: res.fetchedAt || Date.now(), session: percent });
      if (a.history.length > HISTORY_MAX) a.history.splice(0, a.history.length - HISTORY_MAX);
    }

    // The cache holds the bare reading; what the popup gets also carries the samples.
    function attach(res) {
      const a = accounts.use(res.accountId);
      const out = Object.assign({}, res);
      out.history = a.history.slice();
      if (a.windowEnd) out.sessionWindow = { start: a.windowEnd - sessionMs(res), end: a.windowEnd };
      return a.weekly.attach(out);
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
        .then(res => {
          cache = res; failure = null;
          if (res.ok && (res.session || res.weekly)) {
            const a = accounts.use(res.accountId);
            recordSession(a, res);
            if (res.weekly) a.weekly.record(res.weekly, res.fetchedAt || Date.now());
            accounts.persist();
          }
          return attach(res);
        })
        .catch(err => {
          const message = String((err && err.message) || err);
          // Keep showing the last good numbers when a refresh fails, flagged stale, so
          // the bar stops moving instead of disappearing — the same bargain the
          // Anthropic store strikes, and the reason the Options card can tell the two
          // apart. A sign-out is NOT that case: those numbers are somebody else's now.
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

  globalThis.OpenAIData = {
    ORIGIN, SESSION_URL, USAGE_URL, SIGNED_OUT,
    fetchSession, fetchUsage, parseUsage, parseWindow, windowName, createUsageStore,
  };
})();
