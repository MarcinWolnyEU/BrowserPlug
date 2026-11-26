/*
 * BrowserPlug: Mistral AI usage: DATA layer
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Fetch / parse / cache the two included allowances (Vibe & Studio, API) and the plan name
 * of the Mistral account this browser profile is signed in to. Loaded in the BACKGROUND
 * page (needs the logged-in cookies). Publishes `MistralData`. The display half is
 * mistral-view.js.
 *
 * Three calls per refresh, all on the browser's cookies, all with `Accept: application/json`
 * and no query string:
 *
 *   A. GET admin.mistral.ai/api/local-trpc/billing.budget  (the one that must succeed)
 *        -> { result: { data: { json: {
 *             usage_percentage, initial_budget, currency, reset_at,      <- TOP LEVEL
 *             api_budget:  { usage_percentage, initial_budget, currency, reset_at },
 *             vibe_budget: { usage_percentage, initial_budget, currency, reset_at } } } } }
 *      TRAP: on admin.mistral.ai the top-level fields are the API budget, while the top level
 *      of chat.mistral.ai's billing.budget (the first version's source) is the Vibe budget.
 *      Only `vibe_budget` and `api_budget` are read here, never the top level.
 *   B. GET admin.mistral.ai/api/local-trpc/organization.get  (the plan name)
 *        -> the tRPC envelope around an object of about 37 keys: the organization's id, name,
 *           email domain, roles, DNS records, branding. Only `org_tier` and
 *           `active_chat_plan` are kept, and parsePlan() returns one display string, so
 *           nothing else can reach the cache, storage.local or an error message.
 *   C. GET auth.mistral.ai/sessions/whoami (Ory Kratos) -> { identity: { id }, ... }
 *
 * `usage_percentage` is a PERCENT (0 to 100, or more with pay-as-you-go), not a fraction:
 * 0.1337 means 0.13 %, which is EUR 0.34 of a EUR 255 budget. Reading it as a fraction
 * would show 13 % and be wrong by a factor of 100.
 *
 * Signed out, each call answers 401. C exists only for the account id, which keys the
 * per-account history. Its body also carries the email, the name, the session id and a
 * device list with IP addresses; only `identity.id` is kept, and nothing from it is logged.
 * If B or C fails for any other reason the allowances still show: the plan is left out
 * (or the last good one is kept), and the history is filed under the id the last good
 * reading had, or under `unknown` when none was ever seen.
 *
 * The reading's top-level fields are the Vibe & Studio allowance (the Mistral Vibe row bar,
 * the `monthly` history line and the Options summary use it); the API allowance rides in
 * `api`, with its own `api` history line.
 */
(function () {
  'use strict';

  const BUDGET_URL = 'https://admin.mistral.ai/api/local-trpc/billing.budget';
  const ORG_URL = 'https://admin.mistral.ai/api/local-trpc/organization.get';
  const WHOAMI_URL = 'https://auth.mistral.ai/sessions/whoami';
  const SIGNED_OUT = 'not signed in to chat.mistral.ai';
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const JSON_GET = { credentials: 'include', headers: { accept: 'application/json' } };

  // ---- network ------------------------------------------------------------

  async function fetchBudgetRaw() {
    const r = await fetch(BUDGET_URL, JSON_GET);
    if (r.status === 401) throw new Error(SIGNED_OUT);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  // The plan's display name, or null when the call cannot say (any failure but a sign-out).
  // It never fails the reading.
  async function fetchPlan() {
    let r;
    try { r = await fetch(ORG_URL, JSON_GET); } catch (e) { return null; }
    if (r.status === 401) throw new Error(SIGNED_OUT);
    if (!r.ok) return null;
    const j = await r.json().catch(() => null);
    return parsePlan(j);
  }

  // The account id, or null when the call cannot say (any failure but a sign-out).
  async function fetchAccountId() {
    let r;
    try { r = await fetch(WHOAMI_URL, JSON_GET); } catch (e) { return null; }
    if (r.status === 401) throw new Error(SIGNED_OUT);
    if (!r.ok) return null;
    const j = await r.json().catch(() => null);
    const id = j && j.identity && j.identity.id;
    return typeof id === 'string' && id ? id : null;
  }

  // ---- parsing ------------------------------------------------------------

  // The tRPC envelope is unwrapped here and nowhere else; `null` when there is no body.
  function unwrap(raw) {
    const data = raw && raw.result && raw.result.data;
    return data ? (data.json || data) : null;
  }

  // One allowance ({usage_percentage, initial_budget, currency, reset_at}). `percent` is
  // USED, as for every other provider, and is NOT clamped: with pay-as-you-go it can pass
  // 100. A budget that is missing or zero leaves `budget` and `spent` null; the percentage
  // still shows. Throws when the percentage is not reported.
  function parseAllowance(b) {
    // null and '' are "not reported": Number(null) is 0, which would read as 0 % used.
    if (!b || typeof b !== 'object' || b.usage_percentage == null || b.usage_percentage === '') {
      throw new Error('unexpected response');
    }
    const percent = Number(b.usage_percentage);
    if (!isFinite(percent)) throw new Error('unexpected response');
    const budget = Number(b.initial_budget);
    const hasBudget = b.initial_budget != null && isFinite(budget) && budget > 0;
    return {
      percent: Math.max(0, percent),
      budget: hasBudget ? budget : null,
      spent: hasBudget ? (Math.max(0, percent) / 100) * budget : null,
      currency: typeof b.currency === 'string' && b.currency ? b.currency : null,
      // A date that does not parse is no date: the view would print "resets in 0s".
      resetsAt: typeof b.reset_at === 'string' && isFinite(Date.parse(b.reset_at)) ? b.reset_at : null,
    };
  }

  // The Vibe & Studio allowance becomes the reading's top-level fields and is required;
  // the API allowance is `api`, or null when it is missing or unreadable. The top level of
  // the body is the API budget and is deliberately ignored.
  function parseBudget(raw) {
    const b = unwrap(raw);
    if (!b) throw new Error('unexpected response');
    const vibe = parseAllowance(b.vibe_budget);
    let api = null;
    try { api = parseAllowance(b.api_budget); } catch (e) { api = null; }
    return Object.assign(vibe, { api });
  }

  // "SOME_PLAN" -> "Some plan".
  function readable(code) {
    const t = code.trim().toLowerCase().replace(/[_\s]+/g, ' ');
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  // The plan's display name, following the label function in Mistral's admin console, or
  // null when the body does not say. Reads `org_tier` and `active_chat_plan` and nothing
  // else of the organization object.
  function parsePlan(raw) {
    const o = unwrap(raw);
    if (!o || typeof o !== 'object') return null;
    if (o.org_tier === 'E') return 'Enterprise';
    const plan = o.active_chat_plan;
    if (plan === null) return 'Free';
    if (typeof plan !== 'string' || !plan.trim()) return null;
    if (plan === 'INDIVIDUAL') return 'Pro';
    if (plan === 'TEAM') return 'Team';
    if (plan === 'EDU') return 'Education';
    return readable(plan);
  }

  // Fetch + parse, all three legs. Throws on a failure of the budget call or on a sign-out
  // anywhere; SIGNED_OUT is the one message the view special-cases, so keep its spelling.
  async function fetchUsage() {
    const parsed = parseBudget(await fetchBudgetRaw());
    const [accountId, plan] = await Promise.all([fetchAccountId(), fetchPlan()]);
    return Object.assign({ ok: true, fetchedAt: Date.now(), accountId, plan }, parsed);
  }

  // ---- store --------------------------------------------------------------

  // A failed fetch is not retried on every get(). The popup asks every 2 s while its
  // Usage tab is open, so a signed-out browser would otherwise send a request each time.
  // The failure's answer is replayed for RETRY_AFTER_FAILURE_MS, or the refresh interval
  // if that is shorter; get(true) skips the wait.
  const RETRY_AFTER_FAILURE_MS = 60000;

  // Cache + refresh interval + in-flight de-duplication, plus the monthly history behind
  // the Usage tab's chart (period-history.js): a line for the Vibe & Studio allowance and
  // one for the API allowance, both on the axis of their own month. INSTANTIATE ONCE, in
  // the background.
  //   opts.local: a storage.local-like {get,set,remove}; omit -> no persistence.
  //   opts.fetchUsage: injectable fetch (for tests).
  //   opts.refreshMinutes: 1..60.
  function createUsageStore(opts) {
    opts = opts || {};
    const local = opts.local || null;
    const HISTORY_KEY = opts.historyKey || 'mistralUsageHistory';
    const doFetch = opts.fetchUsage || fetchUsage;
    // Monthly samples per Mistral account, so signing in as another account neither clears
    // them nor shows them. The month is `resetsAt` minus one calendar month, in UTC. The two
    // allowances are tracked separately: each decides its own period start after a reset,
    // and an entry saved before the API line existed loads with an empty one.
    const month = (key) => globalThis.PeriodHistory.create({ key, nominalStart: globalThis.PeriodHistory.monthBefore });
    const accounts = globalThis.PeriodHistory.createAccountSet({
      local, key: HISTORY_KEY,
      create: () => ({ monthly: month('monthly'), api: month('api') }),
      save: (a) => Object.assign(a.monthly.toJSON(), a.api.toJSON()),
      load: (a, saved) => { a.monthly.restore(saved); a.api.restore(saved); },
    });
    const idOf = (res) => String(res.accountId || '').toLowerCase();
    let refreshMs = clamp(Number(opts.refreshMinutes) || 5, 1, 60) * 60000;
    let cache = null, inflight = null;
    let failure = null;   // { at, answer } of the last failed fetch

    const ready = accounts.ready;

    const sample = (w) => ({ percent: Math.round(w.percent * 100) / 100, resetsAt: w.resetsAt });
    const chartable = (w) => !!w && !!w.resetsAt && isFinite(w.percent);

    function record(res) {
      const vibe = chartable(res), api = chartable(res.api);
      if (!vibe && !api) return;
      const a = accounts.use(idOf(res));
      const t = res.fetchedAt || Date.now();
      if (vibe) a.monthly.record(sample(res), t);
      if (api) a.api.record(sample(res.api), t);
      accounts.persist();
    }

    // The cache holds the bare reading; what the popup gets also carries the samples.
    function attach(res) {
      const a = accounts.use(idOf(res));
      return a.api.attach(a.monthly.attach(Object.assign({}, res)));
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
          // whoami can fail for a moment while the budget call works. Keep the id the last
          // good reading had, so one failed call does not file this sample under `unknown`
          // and cut the chart to a single point. After a sign-out `cache` holds no reading,
          // so another account's numbers never borrow the previous account's id.
          if (!res.accountId && cache && cache.ok && cache.accountId) {
            res = Object.assign({}, res, { accountId: cache.accountId });
          }
          // The plan call fails the same way and gets the same treatment, so the card's
          // pill does not flip back to "signed in" for one failed refresh.
          if (!res.plan && cache && cache.ok && cache.plan) {
            res = Object.assign({}, res, { plan: cache.plan });
          }
          cache = res; failure = null; record(res); return attach(res);
        })
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

  globalThis.MistralData = {
    BUDGET_URL, ORG_URL, WHOAMI_URL, SIGNED_OUT,
    fetchAccountId, fetchPlan, fetchUsage, parseBudget, parsePlan, createUsageStore,
  };
})();
