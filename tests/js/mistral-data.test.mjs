// Unit tests for extension-gecko/vendor/mistral-data.js (no DOM, no browser).
// Run: node --test tests/js/mistral-data.test.mjs   (or: npm run test:js)
//
// What matters most here:
//   * `usage_percentage` is a PERCENT, not a fraction. 0.1337 is 0.13 % of the budget
//     (EUR 0.34 of 255), which is what Le Chat itself shows. Read as a fraction it would
//     become 13 %, a wrong number that looks plausible.
//   * the TOP LEVEL of admin.mistral.ai's billing.budget is the API budget, not Vibe's. Only
//     `vibe_budget` and `api_budget` are read; a body whose top level differs must not leak in.
//   * where SIGNED_OUT comes from. Each call answers 401 to a signed-out browser, and a
//     403 is something else (a role without billing access), not a sign-out.
//   * the whoami body (email, name, session id, device list with IP addresses) and the
//     organization body (about 37 keys) are never kept. Only identity.id, and the plan
//     name derived from `org_tier` and `active_chat_plan`, may leave fetchUsage.
//   * a failing whoami or plan call must not hide a good budget reading; the history files
//     under the last good id (or `unknown`) and the plan is carried over.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/mistral-data.js'), 'utf8');
const PERIOD_SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/period-history.js'), 'utf8');

function loadModule(fetchImpl) {
  const g = {};
  g.globalThis = g;
  const fetch = fetchImpl || (() => { throw new Error('fetch not expected in this test'); });
  // eslint-disable-next-line no-new-func
  new Function('globalThis', PERIOD_SRC)(g);
  // eslint-disable-next-line no-new-func
  new Function('globalThis', 'fetch', SRC)(g, fetch);
  return g.MistralData;
}

const reply = (body, status = 200) => Promise.resolve({
  ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body),
});

const BUDGET_URL = 'https://admin.mistral.ai/api/local-trpc/billing.budget';
const ORG_URL = 'https://admin.mistral.ai/api/local-trpc/organization.get';
const WHOAMI_URL = 'https://auth.mistral.ai/sessions/whoami';

// The live shape (2026-11-26), identities made up. `vibe` and `api` override the two
// allowances (null leaves one out). The top level is filled with numbers that match
// neither allowance, so every test below pins that it is never read.
const budgetBody = (vibe = {}, api = {}) => {
  const json = {
    usage_percentage: 77, initial_budget: 9, currency: 'USD', reset_at: '2026-11-05T00:00:00Z',
  };
  if (api !== null) {
    json.api_budget = Object.assign({
      usage_percentage: 0.010619, initial_budget: 25.5, currency: 'EUR', reset_at: '2026-12-01T00:00:00Z',
    }, api);
  }
  if (vibe !== null) {
    json.vibe_budget = Object.assign({
      usage_percentage: 0.13369414333333332, initial_budget: 255, currency: 'EUR', reset_at: '2026-12-01T00:00:00Z',
    }, vibe);
  }
  return { result: { data: { json } } };
};
// organization.get: a big object of which only two fields are wanted.
const ORG = {
  id: '00000000-0000-4000-8000-000000000001', name: 'Example Org', email_domain: 'example.org',
  roles: ['admin'], dns_records: [{ name: 'verify.example.org' }], org_tier: 'B', active_chat_plan: 'INDIVIDUAL',
};
const orgBody = (over = {}) => ({ result: { data: { json: Object.assign({}, ORG, over) } } });
const ID = '3f1c9a52-7d0e-4b1e-9c55-0a8d2b6e4f10';
const WHOAMI = {
  id: 'session-secret-id',
  identity: { id: ID, traits: { email: 'someone@example.org', name: { first: 'Some', last: 'One' } } },
  devices: [{ ip_address: '203.0.113.9', user_agent: 'Mozilla/5.0' }],
};
const UNAUTHORIZED = { error: { json: { message: 'UNAUTHORIZED' } } };

const live = (over = {}) => (url) => {
  if (url === BUDGET_URL) return over.budget ? over.budget() : reply(budgetBody());
  if (url === ORG_URL) return over.org ? over.org() : reply(orgBody());
  if (url === WHOAMI_URL) return over.whoami ? over.whoami() : reply(WHOAMI);
  throw new Error('unexpected ' + url);
};

test('parseBudget: usage_percentage is a percent, and the amount spent follows from it', () => {
  const MD = loadModule();
  const b = MD.parseBudget(budgetBody());
  assert.equal(b.percent, 0.13369414333333332, 'kept as reported, not multiplied by 100');
  assert.equal(b.budget, 255);
  assert.equal(b.currency, 'EUR');
  assert.equal(b.resetsAt, '2026-12-01T00:00:00Z');
  assert.equal('payg' in b, false, 'admin.mistral.ai reports no pay-as-you-go flag');
  assert.equal(Math.round(b.spent * 100) / 100, 0.34, 'EUR 0.34, as Le Chat shows it');
  assert.deepEqual(Object.keys(b.api).sort(), ['budget', 'currency', 'percent', 'resetsAt', 'spent']);
  assert.equal(b.api.percent, 0.010619);
  assert.equal(b.api.budget, 25.5);
  assert.equal(Math.round(b.api.spent * 10000) / 10000, 0.0027);
});

test('parseBudget: the top level is the API budget and is never read; Vibe comes from vibe_budget', () => {
  const MD = loadModule();
  const b = MD.parseBudget(budgetBody());
  // The top level here is 77 % of USD 9, resetting on the 5th: none of it may show up.
  assert.deepEqual([b.percent, b.budget, b.currency, b.resetsAt],
    [0.13369414333333332, 255, 'EUR', '2026-12-01T00:00:00Z']);
  assert.deepEqual([b.api.percent, b.api.budget, b.api.currency], [0.010619, 25.5, 'EUR']);
  assert.ok(!JSON.stringify(b).includes('2026-11-05'));
});

test('parseBudget: a missing or unreadable vibe_budget fails the reading, a bad api_budget does not', () => {
  const MD = loadModule();
  assert.throws(() => MD.parseBudget(budgetBody(null)), /unexpected response/);
  assert.throws(() => MD.parseBudget(budgetBody({ usage_percentage: null })), /unexpected response/);
  // Only the top level (what chat.mistral.ai's call used to return): not enough.
  assert.throws(() => MD.parseBudget({ result: { data: { json: { usage_percentage: 5, initial_budget: 100 } } } }),
    /unexpected response/);
  assert.equal(MD.parseBudget(budgetBody({}, null)).api, null, 'api_budget missing');
  for (const usage_percentage of [null, '', 'many', undefined]) {
    const b = MD.parseBudget(budgetBody({}, { usage_percentage }));
    assert.equal(b.api, null, `api_budget with ${String(usage_percentage)}`);
    assert.equal(b.percent, 0.13369414333333332, 'the Vibe reading stands');
  }
  const odd = budgetBody();
  odd.result.data.json.api_budget = 'oops';
  assert.equal(MD.parseBudget(odd).api, null, 'not an object');
});

test('the API allowance follows the same rules as Vibe: a percent, not clamped, no date no reset', () => {
  const MD = loadModule();
  const b = MD.parseBudget(budgetBody({}, { usage_percentage: 110, initial_budget: 25.5, reset_at: 'soon' }));
  assert.equal(b.api.percent, 110);
  assert.equal(Math.round(b.api.spent * 100) / 100, 28.05);
  assert.equal(b.api.resetsAt, null);
  const none = MD.parseBudget(budgetBody({}, { initial_budget: 0 })).api;
  assert.deepEqual([none.budget, none.spent], [null, null]);
});

test('parseBudget: pay-as-you-go can pass 100 and is not clamped', () => {
  const MD = loadModule();
  const b = MD.parseBudget(budgetBody({ usage_percentage: 121.6 }));
  assert.equal(b.percent, 121.6);
  assert.equal(Math.round(b.spent * 100) / 100, 310.08);
});

test('parseBudget: no budget figure leaves the amounts null and keeps the percentage', () => {
  const MD = loadModule();
  for (const initial_budget of [undefined, null, 0, 'n/a']) {
    const b = MD.parseBudget(budgetBody({ initial_budget }));
    assert.deepEqual([b.budget, b.spent, b.percent], [null, null, 0.13369414333333332], String(initial_budget));
  }
});

test('parseBudget: a null or empty percentage is "not reported", not 0 % used', () => {
  const MD = loadModule();
  // Number(null) is 0, which would read as plenty left.
  for (const usage_percentage of [null, undefined, '', 'many']) {
    assert.throws(() => MD.parseBudget(budgetBody({ usage_percentage })), /unexpected response/, String(usage_percentage));
  }
  assert.throws(() => MD.parseBudget({}), /unexpected response/);
  assert.throws(() => MD.parseBudget(null), /unexpected response/);
  assert.equal(MD.parseBudget(budgetBody({ usage_percentage: 0 })).percent, 0, 'a reported 0 is 0');
  assert.equal(MD.parseBudget({ result: { data: { vibe_budget: { usage_percentage: 5, initial_budget: 100 } } } }).spent, 5,
    'a payload without the superjson wrapper is read too');
});

test('fetchUsage makes all three calls with the cookies and Accept: application/json, and no query string', async () => {
  const calls = [];
  const MD = loadModule((url, init) => { calls.push({ url, init }); return live()(url); });
  const u = await MD.fetchUsage();
  assert.equal(u.ok, true);
  assert.equal(u.accountId, ID);
  assert.equal(u.percent, 0.13369414333333332);
  assert.equal(u.plan, 'Pro');
  assert.equal(u.api.percent, 0.010619);
  assert.deepEqual(calls.map(c => c.url), [BUDGET_URL, WHOAMI_URL, ORG_URL]);
  for (const c of calls) {
    assert.equal(c.init.credentials, 'include', 'the cookies are the whole credential');
    assert.equal(c.init.headers.accept, 'application/json');
  }
});

test('fetchUsage keeps only identity.id from the whoami body and only the plan from the organization body', async () => {
  const MD = loadModule(live());
  const u = await MD.fetchUsage();
  const dump = JSON.stringify(u);
  for (const secret of ['someone@example.org', 'Some', 'One', '203.0.113.9', 'Mozilla', 'session-secret-id', 'traits', 'devices',
    'Example Org', 'example.org', '00000000-0000-4000-8000-000000000001', 'verify.example.org', 'dns_records', 'org_tier', 'INDIVIDUAL']) {
    assert.ok(!dump.includes(secret), `${secret} leaked into the reading`);
  }
  assert.equal(MD.SIGNED_OUT, 'not signed in to chat.mistral.ai');
});

test('a 401 from the budget call is SIGNED_OUT, and whoami is not asked after it', async () => {
  const asked = [];
  const MD = loadModule((url) => { asked.push(url); return reply(UNAUTHORIZED, 401); });
  await assert.rejects(MD.fetchUsage(), (e) => e.message === MD.SIGNED_OUT);
  assert.deepEqual(asked, [BUDGET_URL]);
});

test('a 401 from the organization call is SIGNED_OUT too', async () => {
  const MD = loadModule(live({ org: () => reply({ detail: null }, 401) }));
  await assert.rejects(MD.fetchUsage(), (e) => e.message === MD.SIGNED_OUT);
});

test('a 401 from whoami is SIGNED_OUT too, even after a good budget read', async () => {
  const MD = loadModule(live({ whoami: () => reply({ error: { id: 'session_inactive' } }, 401) }));
  await assert.rejects(MD.fetchUsage(), (e) => e.message === MD.SIGNED_OUT);
});

test('a 403 or a 500 from the budget call is an HTTP error, not a sign-out', async () => {
  for (const status of [403, 500]) {
    const MD = loadModule(live({ budget: () => reply({ error: 'x' }, status) }));
    await assert.rejects(MD.fetchUsage(), (e) => e.message === `HTTP ${status}`, String(status));
  }
});

test('whoami failing for any other reason leaves the reading, with no account id', async () => {
  const cases = {
    'a 500': () => reply({}, 500),
    'a network error': () => Promise.reject(new TypeError('NetworkError')),
    'a body that is not JSON': () => Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new SyntaxError('bad')) }),
    'an identity without an id': () => reply({ identity: { traits: {} } }),
    'an id that is not a string': () => reply({ identity: { id: 42 } }),
  };
  for (const [name, whoami] of Object.entries(cases)) {
    const MD = loadModule(live({ whoami }));
    const u = await MD.fetchUsage();
    assert.equal(u.ok, true, name);
    assert.equal(u.accountId, null, name);
    assert.equal(u.percent, 0.13369414333333332, name);
  }
});

test('parsePlan follows the console label table', () => {
  const MD = loadModule();
  const plan = (over) => MD.parsePlan(orgBody(over));
  assert.equal(plan({ org_tier: 'E', active_chat_plan: 'INDIVIDUAL' }), 'Enterprise', 'checked first');
  assert.equal(plan({ org_tier: 'E', active_chat_plan: null }), 'Enterprise');
  assert.equal(plan({ org_tier: 'E', active_chat_plan: undefined }), 'Enterprise');
  assert.equal(plan({ active_chat_plan: 'INDIVIDUAL' }), 'Pro');
  assert.equal(plan({ active_chat_plan: 'TEAM' }), 'Team');
  assert.equal(plan({ active_chat_plan: 'EDU' }), 'Education');
  assert.equal(plan({ active_chat_plan: null }), 'Free');
  assert.equal(plan({ active_chat_plan: 'SOME_PLAN' }), 'Some plan');
  assert.equal(plan({ active_chat_plan: 'SCALE' }), 'Scale');
  for (const bad of [undefined, 42, true, {}, '', '  ']) {
    assert.equal(plan({ active_chat_plan: bad }), null, JSON.stringify(bad));
  }
  const noField = orgBody();
  delete noField.result.data.json.active_chat_plan;
  assert.equal(MD.parsePlan(noField), null, 'field missing');
  for (const raw of [null, undefined, {}, { result: {} }, { result: { data: null } }, 'x', 5]) {
    assert.equal(MD.parsePlan(raw), null, JSON.stringify(raw));
  }
  assert.equal(MD.parsePlan({ result: { data: { active_chat_plan: 'TEAM' } } }), 'Team', 'no superjson wrapper');
});

test('fetchPlan: a 401 is SIGNED_OUT, any other failure is a null plan', async () => {
  const cases = {
    'a 500': () => reply({}, 500),
    'a 403': () => reply({}, 403),
    'a network error': () => Promise.reject(new TypeError('NetworkError')),
    'a body that is not JSON': () => Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new SyntaxError('bad')) }),
    'an unreadable envelope': () => reply({ result: 5 }),
  };
  for (const [name, org] of Object.entries(cases)) {
    const MD = loadModule(live({ org }));
    assert.equal(await MD.fetchPlan(), null, name);
    const u = await MD.fetchUsage();
    assert.deepEqual([u.ok, u.plan, u.percent], [true, null, 0.13369414333333332], name);
  }
  const MD = loadModule(live({ org: () => reply({ detail: null }, 401) }));
  await assert.rejects(MD.fetchPlan(), (e) => e.message === MD.SIGNED_OUT);
  assert.equal(await loadModule(live()).fetchPlan(), 'Pro');
});

test('store: a failed refresh keeps the last numbers, flagged stale', async () => {
  const MD = loadModule();
  let n = 0;
  const store = MD.createUsageStore({
    fetchUsage: () => (n++ === 0
      ? Promise.resolve({ ok: true, fetchedAt: Date.now(), accountId: ID, percent: 10 })
      : Promise.reject(new Error('HTTP 502'))),
  });
  assert.equal((await store.get()).percent, 10);
  const second = await store.get(true);
  assert.equal(second.stale, true);
  assert.equal(second.percent, 10);
  assert.equal(second.error, 'HTTP 502');
});

test('store: a sign-out drops the numbers, which belong to an account this browser left', async () => {
  const MD = loadModule();
  let n = 0;
  const store = MD.createUsageStore({
    fetchUsage: () => (n++ === 0
      ? Promise.resolve({ ok: true, fetchedAt: Date.now(), accountId: ID, percent: 10 })
      : Promise.reject(new Error(MD.SIGNED_OUT))),
  });
  await store.get();
  const after = await store.get(true);
  assert.deepEqual([after.ok, after.signedOut, after.percent], [false, true, undefined]);
});

test('store: a fresh reading is served from cache and concurrent reads share one fetch', async () => {
  const MD = loadModule();
  let fetches = 0;
  const store = MD.createUsageStore({
    fetchUsage: () => { fetches++; return Promise.resolve({ ok: true, fetchedAt: Date.now(), accountId: ID, percent: 1 }); },
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
  const MD = loadModule();
  let fetches = 0;
  let fail = true;
  const store = MD.createUsageStore({
    fetchUsage: () => {
      fetches++;
      return fail ? Promise.reject(new Error(MD.SIGNED_OUT))
        : Promise.resolve({ ok: true, fetchedAt: Date.now(), accountId: ID, percent: 3 });
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
  assert.equal((await store.get()).percent, 3, 'retried once the minute is up');
  assert.equal(fetches, 3);
  clock += 2000;
  await store.get();
  assert.equal(fetches, 3, 'a success is cached for the refresh interval as before');
});

test('store: the month is sampled per account on its own axis, and only what the chart needs is stored', async (t) => {
  let clock = Date.parse('2030-03-10T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const MD = loadModule();
  let reading = { accountId: ID.toUpperCase(), resetsAt: '2030-04-01T00:00:00Z', percent: 20 };
  const saved = {};
  const local = {
    get: async (k) => ({ [k]: saved[k] }),
    set: async (o) => Object.assign(saved, JSON.parse(JSON.stringify(o))),
    remove: async (k) => { delete saved[k]; },
  };
  const make = () => MD.createUsageStore({
    fetchUsage: async () => Object.assign({ ok: true, fetchedAt: Date.now() }, reading),
    refreshMinutes: 60, local,
  });
  const store = make();
  await store.get(true);
  clock += 6 * 60000;
  const a = await store.get(true);
  assert.deepEqual(a.monthlyHistory.map(h => h.monthly), [20, 20]);
  assert.equal(a.monthlyWindow.start, Date.parse('2030-03-01T00:00:00Z'), 'one calendar month back');
  assert.equal(a.monthlyWindow.end, Date.parse('2030-04-01T00:00:00Z'));
  assert.deepEqual(Object.keys(saved.mistralUsageHistory.accounts), [ID], 'filed under the lower-cased identity id');
  assert.equal(saved.mistralUsageHistory.accounts[ID].data.monthlySamples.length, 2, 'persisted');
  assert.deepEqual(Object.keys(saved), ['mistralUsageHistory'], 'one storage key, named for the provider');

  // A background restart picks the samples up again.
  clock += 6 * 60000;
  assert.equal((await make().get(true)).monthlyHistory.length, 3);

  // Another account has its own samples; the first one's stay put.
  reading = { ...reading, accountId: 'other-id' };
  clock += 6 * 60000;
  assert.equal((await store.get(true)).monthlyHistory.length, 1);
  reading = { ...reading, accountId: ID };
  clock += 6 * 60000;
  assert.equal((await store.get(true)).monthlyHistory.length, 3);
  assert.deepEqual(Object.keys(saved.mistralUsageHistory.accounts).sort(), [ID, 'other-id'].sort());

  // A reading whose whoami failed borrows the last good id instead of opening an `unknown`
  // bucket (the two tests below pin both sides of that).
  reading = { ...reading, accountId: null };
  clock += 6 * 60000;
  await store.get(true);
  assert.equal(saved.mistralUsageHistory.accounts.unknown, undefined);

  // The month rolls over: a new period starts.
  reading = { ...reading, accountId: ID, resetsAt: '2030-05-01T00:00:00Z', percent: 0 };
  clock += 6 * 60000;
  const b = await store.get(true);
  assert.equal(b.monthlyHistory.length, 1);
  assert.equal(b.monthlyWindow.end, Date.parse('2030-05-01T00:00:00Z'));

  // What the chart needs and nothing else: a period, and (time, percent) pairs.
  const entry = saved.mistralUsageHistory.accounts[ID];
  assert.deepEqual(Object.keys(entry).sort(), ['data', 'seen']);
  assert.deepEqual(Object.keys(entry.data).sort(),
    ['apiEnd', 'apiSamples', 'apiStart', 'monthlyEnd', 'monthlySamples', 'monthlyStart']);
  assert.ok(entry.data.monthlySamples.every((s) => Object.keys(s).sort().join() === 'monthly,t'));
  store.clearHistory();
  assert.equal(saved.mistralUsageHistory, undefined);
});

test('store: a real fetch writes no whoami field except the id into storage.local', async () => {
  const MD = loadModule(live());
  const saved = {};
  const local = {
    get: async (k) => ({ [k]: saved[k] }),
    set: async (o) => Object.assign(saved, JSON.parse(JSON.stringify(o))),
    remove: async (k) => { delete saved[k]; },
  };
  const store = MD.createUsageStore({ local });
  const r = await store.get(true);
  assert.equal(r.ok, true);
  const dump = JSON.stringify(saved) + JSON.stringify(r);
  for (const secret of ['someone@example.org', 'Some', '203.0.113.9', 'Mozilla', 'session-secret-id']) {
    assert.ok(!dump.includes(secret), `${secret} reached the store`);
  }
  assert.deepEqual(Object.keys(saved.mistralUsageHistory.accounts), [ID]);
});

test('parseBudget: a reset_at that does not parse is no date, so nothing prints "resets in 0s"', () => {
  const MD = loadModule();
  for (const reset_at of ['soon', '', null, undefined, 20261201, '2026-13-45']) {
    assert.equal(MD.parseBudget(budgetBody({ reset_at })).resetsAt, null, String(reset_at));
  }
  assert.equal(MD.parseBudget(budgetBody({ reset_at: '2026-12-01T00:00:00Z' })).resetsAt, '2026-12-01T00:00:00Z');
});

test('store: one failed whoami keeps the last good account id, so the history is not split', async (t) => {
  let clock = Date.parse('2030-03-10T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const MD = loadModule();
  let accountId = ID;
  const saved = {};
  const local = {
    get: async (k) => ({ [k]: saved[k] }),
    set: async (o) => Object.assign(saved, JSON.parse(JSON.stringify(o))),
    remove: async (k) => { delete saved[k]; },
  };
  const store = MD.createUsageStore({
    fetchUsage: async () => ({ ok: true, fetchedAt: Date.now(), accountId, percent: 20, resetsAt: '2030-04-01T00:00:00Z' }),
    refreshMinutes: 60, local,
  });
  await store.get(true);
  clock += 6 * 60000;
  accountId = null;                       // whoami answered 500, the budget call worked
  const r = await store.get(true);
  assert.equal(r.accountId, ID, 'the reading carries the last good id');
  assert.equal(r.monthlyHistory.length, 2, 'the chart keeps its earlier sample');
  assert.deepEqual(Object.keys(saved.mistralUsageHistory.accounts), [ID], 'no `unknown` entry, no lost slot');
});

test('store: with no id ever seen the reading is filed under unknown, and a sign-out forgets the old id', async (t) => {
  let clock = Date.parse('2030-03-10T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const MD = loadModule();
  let step = 'first';
  let accountId = null;
  const saved = {};
  const local = {
    get: async (k) => ({ [k]: saved[k] }),
    set: async (o) => Object.assign(saved, JSON.parse(JSON.stringify(o))),
    remove: async (k) => { delete saved[k]; },
  };
  const store = MD.createUsageStore({
    fetchUsage: async () => {
      if (step === 'out') throw new Error(MD.SIGNED_OUT);
      return { ok: true, fetchedAt: Date.now(), accountId, percent: 20, resetsAt: '2030-04-01T00:00:00Z' };
    },
    refreshMinutes: 60, local,
  });
  await store.get(true);
  assert.deepEqual(Object.keys(saved.mistralUsageHistory.accounts), ['unknown'], 'never an id: unknown');

  // Signed in as A, signed out, then somebody else signs in and whoami fails once: that
  // reading must not borrow A's id.
  accountId = ID;
  clock += 6 * 60000;
  await store.get(true);
  step = 'out';
  clock += 6 * 60000;
  assert.equal((await store.get(true)).signedOut, true);
  step = 'first';
  accountId = null;
  clock += 6 * 60000;
  const back = await store.get(true);
  assert.equal(back.accountId, null);
  assert.equal(back.monthlyHistory.length, 2, 'under unknown, which already held the first sample');
});

test('store: a reading without a reset date samples nothing', async () => {
  const MD = loadModule();
  const store = MD.createUsageStore({
    fetchUsage: async () => ({ ok: true, fetchedAt: Date.now(), accountId: ID, percent: 5, resetsAt: null }),
    refreshMinutes: 60,
  });
  const r = await store.get(true);
  assert.deepEqual(r.monthlyHistory, []);
  assert.equal(r.monthlyWindow, undefined);
});

// A storage.local stand-in that keeps what was written, so a second store can read it back.
const memoryLocal = (saved = {}) => ({
  saved,
  get: async (k) => ({ [k]: saved[k] }),
  set: async (o) => Object.assign(saved, JSON.parse(JSON.stringify(o))),
  remove: async (k) => { delete saved[k]; },
});

test('store: the API allowance has its own line, sampled and persisted beside the Vibe one', async (t) => {
  let clock = Date.parse('2030-03-10T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const MD = loadModule();
  let api = { percent: 4, resetsAt: '2030-04-01T00:00:00Z' };
  const local = memoryLocal();
  const make = () => MD.createUsageStore({
    fetchUsage: async () => ({
      ok: true, fetchedAt: Date.now(), accountId: ID, percent: 20, resetsAt: '2030-04-01T00:00:00Z', api,
    }),
    refreshMinutes: 60, local,
  });
  const store = make();
  await store.get(true);
  clock += 6 * 60000;
  const a = await store.get(true);
  assert.deepEqual(a.monthlyHistory.map(h => h.monthly), [20, 20]);
  assert.deepEqual(a.apiHistory.map(h => h.api), [4, 4]);
  assert.equal(a.apiWindow.start, Date.parse('2030-03-01T00:00:00Z'));
  assert.equal(a.apiWindow.end, Date.parse('2030-04-01T00:00:00Z'));
  const data = local.saved.mistralUsageHistory.accounts[ID].data;
  assert.equal(data.apiSamples.length, 2, 'persisted');
  assert.ok(data.apiSamples.every((s) => Object.keys(s).sort().join() === 'api,t'));

  // A restart picks both lines up.
  clock += 6 * 60000;
  const again = make();
  const b = await again.get(true);
  assert.deepEqual([b.monthlyHistory.length, b.apiHistory.length], [3, 3]);

  // A reading with no API allowance (or one without a date) samples only Vibe.
  api = null;
  clock += 6 * 60000;
  const c = await again.get(true);
  assert.deepEqual([c.monthlyHistory.length, c.apiHistory.length], [4, 3]);
  api = { percent: 4, resetsAt: null };
  clock += 6 * 60000;
  assert.equal((await again.get(true)).apiHistory.length, 3);

  // The API line resets on its own: a new month for it alone.
  api = { percent: 0, resetsAt: '2030-05-01T00:00:00Z' };
  clock += 6 * 60000;
  const d = await again.get(true);
  assert.equal(d.apiHistory.length, 1);
  assert.equal(d.apiWindow.end, Date.parse('2030-05-01T00:00:00Z'));
  assert.equal(d.monthlyWindow.end, Date.parse('2030-04-01T00:00:00Z'), 'the Vibe line did not move');
});

test('store: an entry saved by the first version (Vibe line only) loads with an empty API line', async (t) => {
  let clock = Date.parse('2030-03-10T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const MD = loadModule();
  const legacy = {
    accounts: { [ID]: { seen: clock - 1000, data: {
      monthlyEnd: Date.parse('2030-04-01T00:00:00Z'), monthlyStart: Date.parse('2030-03-01T00:00:00Z'),
      monthlySamples: [{ t: clock - 6 * 60000, monthly: 18 }],
    } } },
  };
  const local = memoryLocal({ mistralUsageHistory: legacy });
  const store = MD.createUsageStore({
    fetchUsage: async () => ({ ok: true, fetchedAt: Date.now(), accountId: ID, percent: 20, resetsAt: '2030-04-01T00:00:00Z', api: null }),
    refreshMinutes: 60, local,
  });
  const r = await store.get(true);
  assert.deepEqual(r.monthlyHistory.map(h => h.monthly), [18, 20], 'the old line is kept');
  assert.deepEqual(r.apiHistory, [], 'and the API line starts empty');
  assert.equal(r.apiWindow, undefined);
});

test('store: one failed plan call keeps the last good plan, but a sign-out forgets it', async (t) => {
  let clock = Date.parse('2030-03-10T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const MD = loadModule();
  let step = 'first';
  let plan = 'Pro';
  const store = MD.createUsageStore({
    fetchUsage: async () => {
      if (step === 'out') throw new Error(MD.SIGNED_OUT);
      return { ok: true, fetchedAt: Date.now(), accountId: ID, plan, percent: 20, resetsAt: '2030-04-01T00:00:00Z' };
    },
    refreshMinutes: 60,
  });
  assert.equal((await store.get(true)).plan, 'Pro');
  plan = null;                              // organization.get answered 500, the budget call worked
  clock += 6 * 60000;
  assert.equal((await store.get(true)).plan, 'Pro', 'the last good plan is carried over');
  plan = 'Team';
  clock += 6 * 60000;
  assert.equal((await store.get(true)).plan, 'Team', 'a new answer wins');
  step = 'out';
  clock += 6 * 60000;
  assert.equal((await store.get(true)).signedOut, true);
  step = 'first';
  plan = null;
  clock += 6 * 60000;
  assert.equal((await store.get(true)).plan, null, 'after a sign-out another account does not borrow the plan');
});
