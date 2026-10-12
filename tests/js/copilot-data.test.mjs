// Unit tests for extension-gecko/vendor/copilot-data.js (no DOM, no browser).
// Run: node --test tests/js/copilot-data.test.mjs   (or: npm run test:js)
//
// What matters most here:
//   * the Accept header. Without `Accept: application/json` github.com answers the
//     entitlement call with a 400, so a fetch that "works" in a quick manual test with
//     default headers fails in the extension.
//   * where SIGNED_OUT comes from. Both endpoints answer 404 to a signed-out browser;
//     only the login call may turn that into "signed out", because a 404 from the
//     entitlement after a login succeeded means something else.
//   * the login. It is what copilot-view.js compares with VS Code's account, so a
//     store that dropped it would silently show one account's quota on another's row.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/copilot-data.js'), 'utf8');
const PERIOD_SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/period-history.js'), 'utf8');

function loadModule(fetchImpl) {
  const g = {};
  g.globalThis = g;
  const fetch = fetchImpl || (() => { throw new Error('fetch not expected in this test'); });
  // eslint-disable-next-line no-new-func
  new Function('globalThis', PERIOD_SRC)(g);
  // eslint-disable-next-line no-new-func
  new Function('globalThis', 'fetch', SRC)(g, fetch);
  return g.CopilotData;
}

const reply = (body, status = 200) => Promise.resolve({
  ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body),
});

// The live entitlement shape (Copilot Free), numbers changed.
const ENTITLEMENT = {
  licenseType: 'licensed_limited',
  quotas: {
    limits: { premiumInteractions: 0 },
    remaining: { chat: 150, completions: 1897, premiumInteractions: 0 },
    resetDate: '2026-10-01',
    resetDateUtc: '2026-10-01T00:00:00.000Z',
    chatQuota: { percentRemaining: 75.0, total: 200, used: 50.0, hasQuota: true, unlimited: false },
    completionsQuota: { percentRemaining: 94.8, total: 2000, used: 103.0, hasQuota: true, unlimited: false },
    premiumInteractionsQuota: { percentRemaining: 0.0, total: 0, used: 0.0, hasQuota: false, unlimited: false },
  },
  plan: 'free',
};

test('parseQuota reports USED percent, like every other provider here', () => {
  const CD = loadModule();
  assert.deepEqual(CD.parseQuota(ENTITLEMENT.quotas.completionsQuota),
    { percent: 5.2, used: 103, total: 2000, unlimited: false });
  assert.equal(CD.parseQuota({ total: 300, used: 30, hasQuota: true }).percent, 10,
    'no percentRemaining: computed from used/total');
});

test('a quota the plan does not include is null, not an empty bar', () => {
  const CD = loadModule();
  assert.equal(CD.parseQuota(ENTITLEMENT.quotas.premiumInteractionsQuota), null);
  assert.equal(CD.parseQuota(undefined), null);
  assert.deepEqual(CD.parseQuota({ hasQuota: true, unlimited: true, percentRemaining: 100 }),
    { percent: 0, used: 0, total: 0, unlimited: true });
});

test('parseQuota: a null or empty percentRemaining is "not reported", not 100% used', () => {
  const CD = loadModule();
  // Number(null) is 0, which read as "0% left".
  assert.equal(CD.parseQuota({ percentRemaining: null, used: 3, total: 300, hasQuota: true }).percent, 1);
  assert.equal(CD.parseQuota({ percentRemaining: '', used: 3, total: 300, hasQuota: true }).percent, 1);
  assert.equal(CD.parseQuota({ percentRemaining: 0, used: 300, total: 300, hasQuota: true }).percent, 100,
    'a reported 0 is still nothing left');
});

test('parseEntitlement keeps the plan, the reset date and all three quotas', () => {
  const CD = loadModule();
  const e = CD.parseEntitlement(ENTITLEMENT);
  assert.equal(e.plan, 'free');
  assert.equal(e.resetsAt, '2026-10-01T00:00:00.000Z');
  assert.equal(e.chat.percent, 25);
  assert.equal(e.completions.used, 103);
  assert.equal(e.premium, null);
  assert.equal(CD.parseEntitlement({ quotas: { resetDate: '2026-11-01' } }).resetsAt,
    '2026-11-01T00:00:00.000Z', 'the date-only field is read as midnight UTC');
});

test('fetchUsage makes both calls with Accept: application/json and keeps the login', async () => {
  const calls = [];
  const CD = loadModule((url, init) => {
    calls.push({ url, init });
    if (url === 'https://github.com/github-copilot/chat?skip_anchor=true') {
      return reply({ currentUserLogin: 'octo-editor', realIp: '203.0.113.9' });
    }
    if (url === 'https://github.com/github-copilot/chat/entitlement') return reply(ENTITLEMENT);
    throw new Error('unexpected ' + url);
  });
  const u = await CD.fetchUsage();
  assert.equal(u.ok, true);
  assert.equal(u.login, 'octo-editor');
  assert.equal(u.chat.percent, 25);
  assert.equal(calls.length, 2);
  for (const c of calls) {
    assert.equal(c.init.credentials, 'include', 'the cookies are the whole credential');
    assert.equal(c.init.headers.accept, 'application/json', 'without it github.com answers 400');
  }
  assert.ok(!JSON.stringify(u).includes('203.0.113.9'), 'only the login is kept from that response');
});

test('a 404 from the login call is SIGNED_OUT; a 404 from the entitlement is not', async () => {
  const signedOut = loadModule(() => reply({ error: 'Not Found' }, 404));
  await assert.rejects(signedOut.fetchUsage(), (e) => e.message === signedOut.SIGNED_OUT);

  const noEntitlement = loadModule((url) => (url.endsWith('/entitlement')
    ? reply({ error: 'Not Found' }, 404)
    : reply({ currentUserLogin: 'octo' })));
  await assert.rejects(noEntitlement.fetchUsage(), (e) => e.message === 'HTTP 404');
});

test('a login payload without a login is read as signed out', async () => {
  const CD = loadModule(() => reply({ currentUserLogin: null }));
  await assert.rejects(CD.fetchLogin(), (e) => e.message === CD.SIGNED_OUT);
});

test('store: a failed refresh keeps the last numbers, flagged stale', async () => {
  const CD = loadModule();
  let n = 0;
  const store = CD.createUsageStore({
    fetchUsage: () => (n++ === 0
      ? Promise.resolve({ ok: true, fetchedAt: Date.now(), login: 'octo', chat: { percent: 10 } })
      : Promise.reject(new Error('HTTP 502'))),
  });
  assert.equal((await store.get()).login, 'octo');
  const second = await store.get(true);
  assert.equal(second.stale, true);
  assert.equal(second.login, 'octo');
  assert.equal(second.error, 'HTTP 502');
});

test('store: a sign-out drops the numbers, which belong to an account this browser left', async () => {
  const CD = loadModule();
  let n = 0;
  const store = CD.createUsageStore({
    fetchUsage: () => (n++ === 0
      ? Promise.resolve({ ok: true, fetchedAt: Date.now(), login: 'octo' })
      : Promise.reject(new Error(CD.SIGNED_OUT))),
  });
  await store.get();
  const after = await store.get(true);
  assert.deepEqual([after.ok, after.signedOut, after.login], [false, true, undefined]);
});

test('store: a fresh reading is served from cache and concurrent reads share one fetch', async () => {
  const CD = loadModule();
  let fetches = 0;
  const store = CD.createUsageStore({
    fetchUsage: () => { fetches++; return Promise.resolve({ ok: true, fetchedAt: Date.now(), login: 'octo' }); },
  });
  await Promise.all([store.get(), store.get()]);
  await store.get();
  assert.equal(fetches, 1);
  await store.get(true);
  assert.equal(fetches, 2, 'force bypasses the cache');
});

test('store: a failure is answered from memory for a minute, not refetched on every get', async (t) => {
  let clock = 1_000_000;
  t.mock.method(Date, 'now', () => clock);
  const CD = loadModule();
  let fetches = 0;
  let fail = true;
  const store = CD.createUsageStore({
    fetchUsage: () => {
      fetches++;
      return fail ? Promise.reject(new Error(CD.SIGNED_OUT))
        : Promise.resolve({ ok: true, fetchedAt: Date.now(), login: 'octo' });
    },
  });
  assert.equal((await store.get()).signedOut, true);
  // The popup's Usage tab asks every 2 s.
  for (let i = 0; i < 5; i++) {
    clock += 2000;
    assert.equal((await store.get()).signedOut, true);
  }
  assert.equal(fetches, 1, 'a known failure was refetched by the 2 s poll');
  await store.get(true);
  assert.equal(fetches, 2, 'force still goes to the network');
  fail = false;
  clock += 60000;
  assert.equal((await store.get()).login, 'octo', 'retried once the minute is up');
  assert.equal(fetches, 3);
  clock += 2000;
  await store.get();
  assert.equal(fetches, 3, 'a success is cached for the refresh interval as before');
});

test('store: the charted quota is sampled on the axis of its own month', async (t) => {
  let clock = Date.parse('2030-03-10T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const CD = loadModule();
  let reading = {
    login: 'octo', resetsAt: '2030-04-01T00:00:00.000Z',
    premium: { percent: 20, unlimited: false }, chat: { percent: 5, unlimited: false },
  };
  const saved = {};
  const local = {
    get: async (k) => ({ [k]: saved[k] }),
    set: async (o) => Object.assign(saved, o),
    remove: async (k) => { delete saved[k]; },
  };
  const make = () => CD.createUsageStore({
    fetchUsage: async () => Object.assign({ ok: true, fetchedAt: Date.now() }, reading),
    refreshMinutes: 60, local,
  });
  const store = make();
  await store.get(true);
  clock += 6 * 60000;
  const a = await store.get(true);
  assert.deepEqual(a.monthlyHistory.map(h => h.monthly), [20, 20], 'premium requests, not chat');
  assert.equal(a.monthlyWindow.start, Date.parse('2030-03-01T00:00:00Z'), 'one calendar month back');
  assert.equal(a.monthlyWindow.end, Date.parse('2030-04-01T00:00:00Z'));
  assert.equal(saved.copilotUsageHistory.accounts.octo.data.monthlySamples.length, 2, 'persisted');

  // A background restart picks the samples up again.
  clock += 6 * 60000;
  assert.equal((await make().get(true)).monthlyHistory.length, 3);

  // A different github.com account has samples of its own; the first account's stay put,
  // and signing back in (the login's case does not matter) finds them.
  reading = Object.assign({}, reading, { login: 'Other' });
  clock += 6 * 60000;
  assert.equal((await store.get(true)).monthlyHistory.length, 1);
  reading = Object.assign({}, reading, { login: 'OCTO' });
  clock += 6 * 60000;
  assert.equal((await store.get(true)).monthlyHistory.length, 3);
  assert.deepEqual(Object.keys(saved.copilotUsageHistory.accounts).sort(), ['octo', 'other']);

  // The month rolls over: a new period starts.
  reading = Object.assign({}, reading, { resetsAt: '2030-05-01T00:00:00.000Z', premium: { percent: 0, unlimited: false } });
  clock += 6 * 60000;
  const b = await store.get(true);
  assert.equal(b.monthlyHistory.length, 1);
  assert.equal(b.monthlyWindow.end, Date.parse('2030-05-01T00:00:00Z'));
  store.clearHistory();
  assert.equal(saved.copilotUsageHistory, undefined);
});

test('store: an unlimited or absent quota samples nothing', async () => {
  const CD = loadModule();
  const store = CD.createUsageStore({
    fetchUsage: async () => ({
      ok: true, fetchedAt: Date.now(), login: 'octo', resetsAt: '2030-04-01T00:00:00.000Z',
      premium: { percent: 0, unlimited: true }, chat: null, completions: { percent: 4 },
    }),
    refreshMinutes: 60,
  });
  const r = await store.get(true);
  assert.deepEqual(r.monthlyHistory, []);
  assert.equal(r.monthlyWindow, undefined);
});

test('store: code completions get a line of their own on the same month, and old histories still load', async (t) => {
  let clock = Date.parse('2030-03-10T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const CD = loadModule();
  let reading = {
    login: 'octo', resetsAt: '2030-04-01T00:00:00.000Z',
    premium: null, chat: { percent: 25, unlimited: false }, completions: { percent: 5, unlimited: false },
  };
  const saved = {
    // As written before completions were sampled: the monthly part only.
    copilotUsageHistory: { accounts: { octo: { seen: 1, data: {
      monthlyEnd: Date.parse('2030-04-01T00:00:00Z'), monthlyStart: Date.parse('2030-03-01T00:00:00Z'),
      monthlySamples: [{ t: Date.parse('2030-03-09T00:00:00Z'), monthly: 20 }],
    } } } },
  };
  const local = {
    get: async (k) => ({ [k]: saved[k] }),
    set: async (o) => Object.assign(saved, o),
    remove: async (k) => { delete saved[k]; },
  };
  const store = CD.createUsageStore({
    fetchUsage: async () => Object.assign({ ok: true, fetchedAt: Date.now() }, reading),
    refreshMinutes: 60, local,
  });
  await store.get(true);
  clock += 6 * 60000;
  reading = { ...reading, completions: { percent: 6, unlimited: false } };
  const a = await store.get(true);
  assert.deepEqual(a.monthlyHistory.map(h => h.monthly), [20, 25, 25], 'the old samples are kept');
  assert.deepEqual(a.completionsHistory.map(h => h.completions), [5, 6]);
  assert.equal(a.completionsWindow.start, Date.parse('2030-03-01T00:00:00Z'));
  assert.equal(a.completionsWindow.end, Date.parse('2030-04-01T00:00:00Z'));
  assert.equal(saved.copilotUsageHistory.accounts.octo.data.completionsSamples.length, 2, 'persisted');

  // Completions are charted even when the plan caps nothing else, but never while unlimited.
  reading = { ...reading, chat: null, completions: { percent: 7, unlimited: false } };
  clock += 6 * 60000;
  const b = await store.get(true);
  assert.equal(b.monthlyHistory.length, 3, 'no new monthly sample: nothing else is capped');
  assert.equal(b.completionsHistory.length, 3);
  reading = { ...reading, completions: { percent: 0, unlimited: true } };
  clock += 6 * 60000;
  assert.equal((await store.get(true)).completionsHistory.length, 3);

  // The month rolls over: both lines start again.
  reading = { ...reading, resetsAt: '2030-05-01T00:00:00.000Z', chat: { percent: 0, unlimited: false },
    completions: { percent: 0, unlimited: false } };
  clock += 6 * 60000;
  const c = await store.get(true);
  assert.equal(c.monthlyHistory.length, 1);
  assert.equal(c.completionsHistory.length, 1);
});
