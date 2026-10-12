// Unit tests for extension-gecko/vendor/openai-data.js (no DOM, no browser).
// Run: node --test tests/js/openai-data.test.mjs   (or: npm run test:js)
//
// Two things here are worth more than the parsing coverage:
//   * the SIGNED-OUT shape. chatgpt.com answers 200 with an empty body to a profile
//     with no session, so there is no HTTP error to notice and "signed out" has to be
//     derived from the missing accessToken. Get that wrong and the card reports a
//     network fault at somebody who is simply logged out.
//   * the accounts/check ban. That endpoint answers 200 to cookies alone and calls a
//     Plus account "guest"/"Free". A wrong answer that looks like an answer is worse
//     than the 401 the real endpoint gives, so the last test keeps it out of the file.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/openai-data.js'), 'utf8');
const WEEKLY_SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/period-history.js'), 'utf8');

// The data layer needs no DOM — a bare globalThis plus whatever fetch the case wants.
function loadModule(fetchImpl) {
  const g = {};
  g.globalThis = g;
  const fetch = fetchImpl || (() => { throw new Error('fetch not expected in this test'); });
  // eslint-disable-next-line no-new-func
  new Function('globalThis', WEEKLY_SRC)(g);
  // eslint-disable-next-line no-new-func
  new Function('globalThis', 'fetch', SRC)(g, fetch);
  return g.OpenAIData;
}

const json = (body, ok = true, status = 200) => Promise.resolve({
  ok, status, json: () => Promise.resolve(body),
});

// The live payload shape, trimmed to what the parser reads. 18000s = 5h, 604800s = 7d;
// reset_at is epoch SECONDS here, where claude.ai sends an ISO string.
const USAGE = {
  plan_type: 'plus',
  rate_limit: {
    allowed: true,
    limit_reached: false,
    primary_window: { used_percent: 42, limit_window_seconds: 18000, reset_at: 1789003737 },
    secondary_window: { used_percent: 9, limit_window_seconds: 604800, reset_at: 1789590537 },
  },
  credits: { has_credits: false, balance: '0' },
  rate_limit_reached_type: null,
};

test('windowName reads the label off the duration rather than hard-coding it', () => {
  const OD = loadModule();
  assert.equal(OD.windowName(18000), '5-hour');
  assert.equal(OD.windowName(604800), 'weekly');
  assert.equal(OD.windowName(86400), '1-day');
  assert.equal(OD.windowName(2592000), '30-day');
  assert.equal(OD.windowName(900), '15-minute');
  // A window OpenAI has not shipped still gets a correct label, which is the point:
  // the alternative is a "5-hour" caption over a number that no longer means that.
  assert.equal(OD.windowName(7200), '2-hour');
  assert.equal(OD.windowName(0), 'window');
});

test('parseWindow converts epoch SECONDS to something new Date() understands', () => {
  const OD = loadModule();
  const w = OD.parseWindow({ used_percent: 42, limit_window_seconds: 18000, reset_at: 1789003737 });
  assert.equal(w.percent, 42);
  assert.equal(w.windowSeconds, 18000);
  assert.equal(w.windowName, '5-hour');
  assert.equal(new Date(w.resetsAt).getTime(), 1789003737 * 1000);
  // Treating the number as milliseconds is the mistake this guards: it would land in
  // January 1970 and every countdown would read as long expired.
  assert.ok(new Date(w.resetsAt).getUTCFullYear() > 2020);
});

test('parseUsage maps the two windows onto the Anthropic store shape', () => {
  const OD = loadModule();
  const u = OD.parseUsage(USAGE);
  assert.equal(u.session.percent, 42);
  assert.equal(u.weekly.percent, 9);
  assert.equal(u.plan, 'plus');
  assert.equal(u.limitReached, false);
  assert.equal(u.creditBalance, '0');
});

test('parseUsage: an exhausted allowance is limitReached from either field', () => {
  const OD = loadModule();
  const byFlag = OD.parseUsage({ rate_limit: { limit_reached: true, primary_window: { used_percent: 100 } } });
  const byAllowed = OD.parseUsage({ rate_limit: { allowed: false, primary_window: { used_percent: 100 } } });
  assert.equal(byFlag.limitReached, true);
  assert.equal(byAllowed.limitReached, true);
});

test('parseUsage survives a payload with no rate_limit at all', () => {
  const OD = loadModule();
  const u = OD.parseUsage({});
  assert.equal(u.session, null);
  assert.equal(u.weekly, null);
  assert.equal(u.limitReached, false);
});

test('fetchSession: a 200 with no accessToken IS the signed-out signal', async () => {
  // The endpoint does not 401 a signed-out profile; it returns an empty object. Waiting
  // for an HTTP error here waits forever.
  const OD = loadModule(() => json({}));
  await assert.rejects(() => OD.fetchSession(), (e) => e.message === OD.SIGNED_OUT);
});

test('fetchSession carries the bearer, the account and the plan out of one call', async () => {
  const OD = loadModule(() => json({
    accessToken: 'tok', expires: '2026-12-08T20:29:59.689Z',
    account: { id: 'acc-1', planType: 'plus' },
  }));
  const s = await OD.fetchSession();
  assert.equal(s.accessToken, 'tok');
  assert.equal(s.accountId, 'acc-1');
  assert.equal(s.plan, 'plus');
});

test('fetchUsage sends the bearer from the session call on the second leg', async () => {
  const seen = [];
  const OD = loadModule((url, init) => {
    seen.push({ url, init });
    if (url.endsWith('/api/auth/session')) return json({ accessToken: 'tok', account: { id: 'acc-1' } });
    return json(USAGE);
  });
  const res = await OD.fetchUsage();
  assert.equal(res.ok, true);
  assert.equal(res.session.percent, 42);
  assert.equal(seen.length, 2, 'two legs, in order');
  assert.equal(seen[0].url, OD.SESSION_URL);
  assert.equal(seen[1].url, OD.USAGE_URL);
  assert.equal(seen[1].init.headers.authorization, 'Bearer tok');
  assert.equal(seen[1].init.headers['ChatGPT-Account-Id'], 'acc-1');
  // Cookies on both legs: the first needs them to mint the token, and the second is
  // same-origin anyway.
  assert.equal(seen[0].init.credentials, 'include');
});

test('the store caches within the refresh window and force gets past it', async () => {
  let calls = 0;
  const OD = loadModule();
  const store = OD.createUsageStore({
    refreshMinutes: 60,
    fetchUsage: () => { calls += 1; return Promise.resolve({ ok: true, session: { percent: calls }, fetchedAt: Date.now() }); },
  });
  assert.equal((await store.get()).session.percent, 1);
  assert.equal((await store.get()).session.percent, 1, 'second read is the cache');
  assert.equal(calls, 1);
  assert.equal((await store.get(true)).session.percent, 2, 'force refetches');
  assert.equal(calls, 2);
});

test('a failed refresh keeps the last numbers, flagged stale', async () => {
  let calls = 0;
  const OD = loadModule();
  const store = OD.createUsageStore({
    refreshMinutes: 1,
    fetchUsage: () => {
      calls += 1;
      if (calls === 1) return Promise.resolve({ ok: true, session: { percent: 42 }, fetchedAt: Date.now() });
      return Promise.reject(new Error('HTTP 500'));
    },
  });
  await store.get();
  const second = await store.get(true);
  assert.equal(second.ok, true);
  assert.equal(second.session.percent, 42, 'the bar keeps rendering, it just stops moving');
  assert.equal(second.stale, true);
  assert.equal(second.error, 'HTTP 500');
});

test('a SIGN-OUT clears the numbers instead of going stale — they are somebody else\'s now', async () => {
  let calls = 0;
  const OD = loadModule();
  const store = OD.createUsageStore({
    refreshMinutes: 1,
    fetchUsage: () => {
      calls += 1;
      if (calls === 1) return Promise.resolve({ ok: true, session: { percent: 42 }, fetchedAt: Date.now() });
      return Promise.reject(new Error(OD.SIGNED_OUT));
    },
  });
  await store.get();
  const after = await store.get(true);
  assert.equal(after.ok, false);
  assert.equal(after.signedOut, true);
  assert.equal(after.session, undefined, 'no leftover percentages from the previous account');
});

test('two callers in the same tick share one fetch', async () => {
  let calls = 0;
  const OD = loadModule();
  const store = OD.createUsageStore({
    fetchUsage: () => { calls += 1; return Promise.resolve({ ok: true, session: { percent: 1 }, fetchedAt: Date.now() }); },
  });
  await Promise.all([store.get(), store.get()]);
  assert.equal(calls, 1, 'the popup row and the banner ask together every 2s');
});

test('accounts/check appears only as the warning not to use it', () => {
  // It answers 200 to cookies alone and reports a Plus account as guest/Free. A wrong
  // answer that looks like an answer is worse than the 401 the real endpoint gives, so
  // the module records the ban in prose — and this checks the prose never became code.
  const mentions = SRC.split(/\r?\n/).filter((l) => l.includes('accounts/check'));
  assert.ok(mentions.length > 0, 'the warning itself should still be there');
  for (const line of mentions) {
    const t = line.trim();
    assert.ok(t.startsWith('*') || t.startsWith('//'), `not a comment: ${t}`);
  }
});

test('createUsageStore: the weekly samples are kept, persisted, and restart at a reset', async (t) => {
  let clock = Date.parse('2030-01-03T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const OD = loadModule();
  const sec = (iso) => Date.parse(iso) / 1000;
  let weekly = { percent: 40, resetsAt: new Date(sec('2030-01-05T00:00:00Z') * 1000).toISOString(), windowSeconds: 604800 };
  const fakeFetch = async () => ({
    ok: true, fetchedAt: Date.now(), session: { percent: 3, resetsAt: null }, weekly,
  });
  const saved = {};
  const local = {
    get: async (k) => ({ [k]: saved[k] }),
    set: async (o) => Object.assign(saved, o),
    remove: async (k) => { delete saved[k]; },
  };
  const store = OD.createUsageStore({ fetchUsage: fakeFetch, refreshMinutes: 60, local });
  await store.get(true);
  clock += 6 * 60000;
  const a = await store.get(true);
  assert.equal(a.weeklyHistory.length, 2);
  assert.equal(a.weeklyWindow.end - a.weeklyWindow.start, 7 * 24 * 3600e3);
  assert.equal(saved.openaiUsageHistory.accounts.unknown.data.weeklySamples.length, 2, 'persisted');

  // A new store (background page restart) picks the samples up again.
  const again = OD.createUsageStore({ fetchUsage: fakeFetch, refreshMinutes: 60, local });
  clock += 6 * 60000;
  assert.equal((await again.get(true)).weeklyHistory.length, 3);

  // A reset that kept the deadline: usage fell, so the chart restarts there, shorter.
  clock += 3600e3;
  const resetAt = clock;
  weekly = { ...weekly, percent: 0 };
  const b = await again.get(true);
  assert.equal(b.weeklyHistory.length, 1);
  assert.equal(b.weeklyWindow.start, resetAt);
  assert.equal(b.weeklyWindow.end, Date.parse('2030-01-05T00:00:00Z'));

  again.clearHistory();
  assert.equal(saved.openaiUsageHistory, undefined);
});

test('createUsageStore: without a weekly window nothing is sampled', async () => {
  const OD = loadModule();
  const store = OD.createUsageStore({
    fetchUsage: async () => ({ ok: true, fetchedAt: Date.now(), session: { percent: 3 }, weekly: null }),
    refreshMinutes: 60,
  });
  const r = await store.get(true);
  assert.deepEqual(r.weeklyHistory, []);
  assert.equal(r.weeklyWindow, undefined);
});

test('createUsageStore: each ChatGPT account keeps its own weekly samples', async (t) => {
  let clock = Date.parse('2030-01-03T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const OD = loadModule();
  let accountId = 'acct-1';
  const fakeFetch = async () => ({
    ok: true, accountId, fetchedAt: Date.now(), session: { percent: 3 },
    weekly: { percent: 40, resetsAt: '2030-01-05T00:00:00.000Z', windowSeconds: 604800 },
  });
  const store = OD.createUsageStore({ fetchUsage: fakeFetch, refreshMinutes: 60 });
  await store.get(true);
  clock += 6 * 60000;
  assert.equal((await store.get(true)).weeklyHistory.length, 2);
  accountId = 'acct-2';
  clock += 6 * 60000;
  assert.equal((await store.get(true)).weeklyHistory.length, 1);
  accountId = 'acct-1';
  clock += 6 * 60000;
  assert.equal((await store.get(true)).weeklyHistory.length, 3, 'signing back in finds the samples');
});

test('createUsageStore: the 5-hour session trace is sampled, persisted and restarts with the window', async (t) => {
  let clock = Date.parse('2030-01-03T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const OD = loadModule();
  let session = { percent: 10, resetsAt: '2030-01-03T04:00:00.000Z', windowSeconds: 18000 };
  const fakeFetch = async () => ({ ok: true, fetchedAt: Date.now(), session, weekly: null });
  const saved = {};
  const local = {
    get: async (k) => ({ [k]: saved[k] }),
    set: async (o) => Object.assign(saved, o),
    remove: async (k) => { delete saved[k]; },
  };
  const make = () => OD.createUsageStore({ fetchUsage: fakeFetch, refreshMinutes: 60, local });
  const store = make();
  await store.get(true);
  clock += 10 * 60000;
  session = { ...session, percent: 12 };
  const a = await store.get(true);
  assert.deepEqual(a.history.map(h => h.session), [10, 12]);
  assert.equal(a.sessionWindow.end, Date.parse('2030-01-03T04:00:00Z'));
  assert.equal(a.sessionWindow.end - a.sessionWindow.start, 5 * 3600e3, 'the length comes from the payload');
  assert.equal(saved.openaiUsageHistory.accounts.unknown.data.samples.length, 2, 'persisted');

  // A background restart picks the trace up again.
  clock += 10 * 60000;
  assert.equal((await make().get(true)).history.length, 3);

  // A moved deadline is the next window: the old trace goes. A jitter of a few seconds is not.
  session = { ...session, resetsAt: '2030-01-03T04:00:05.000Z' };
  clock += 10 * 60000;
  assert.equal((await store.get(true)).history.length, 3, 'jitter keeps the window');
  session = { percent: 1, windowSeconds: 18000, resetsAt: '2030-01-03T09:00:00.000Z' };
  clock += 10 * 60000;
  const b = await store.get(true);
  assert.deepEqual(b.history.map(h => h.session), [1]);
  assert.equal(b.sessionWindow.start, Date.parse('2030-01-03T04:00:00Z'));
  assert.equal(b.sessionWindow.end, Date.parse('2030-01-03T09:00:00Z'));

  // A reset that kept its deadline (usage fell) also starts the trace over, no dip to zero.
  session = { ...session, percent: 30 };
  clock += 10 * 60000;
  assert.equal((await store.get(true)).history.length, 2);
  session = { ...session, percent: 0 };
  clock += 10 * 60000;
  assert.deepEqual((await store.get(true)).history.map(h => h.session), [0]);

  store.clearHistory();
  assert.equal(saved.openaiUsageHistory, undefined);
});

test('createUsageStore: a history saved before the session trace existed still loads', async () => {
  const OD = loadModule();
  const local = {
    get: async (k) => ({ [k]: { accounts: { acct: { seen: 1, data: {
      weeklyEnd: Date.parse('2030-01-05T00:00:00Z'), weeklyStart: Date.parse('2029-12-29T00:00:00Z'),
      weeklySamples: [{ t: Date.parse('2030-01-03T00:00:00Z'), weekly: 7 }],
    } } } } }),
    set: async () => {}, remove: async () => {},
  };
  const store = OD.createUsageStore({
    fetchUsage: async () => ({ ok: true, accountId: 'acct', fetchedAt: Date.parse('2030-01-03T01:00:00Z'),
      session: { percent: 3 }, weekly: { percent: 7, resetsAt: '2030-01-05T00:00:00.000Z', windowSeconds: 604800 } }),
    refreshMinutes: 60, local,
  });
  const r = await store.get(true);
  assert.equal(r.weeklyHistory.length, 2, 'the weekly samples survive');
  assert.deepEqual(r.history.map(h => h.session), [3], 'the session trace starts from now');
  assert.equal(r.sessionWindow, undefined, 'no reset_at, no window to draw');
});
