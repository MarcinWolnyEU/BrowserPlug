// Unit tests for extension-gecko/vendor/anthropic-data.js (no DOM, no Firefox).
// Run: node --test tests/js/anthropic-data.test.mjs   (or: npm run test:js)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/anthropic-data.js'), 'utf8');
const WEEKLY_SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/period-history.js'), 'utf8');

// The data layer needs no DOM — load it with a bare globalThis + a fetch stub.
function loadModule() {
  const g = {};
  g.globalThis = g;
  // eslint-disable-next-line no-new-func
  new Function('globalThis', WEEKLY_SRC)(g);
  // eslint-disable-next-line no-new-func
  new Function('globalThis', 'fetch', SRC)(g, () => { throw new Error('fetch not expected in this test'); });
  return g.AnthropicData;
}

const LIVE = {
  five_hour: { utilization: 47, resets_at: '2026-07-02T15:50:00.4+00:00' },
  seven_day: { utilization: 10, resets_at: '2026-07-06T22:00:00.4+00:00' },
  limits: [
    { kind: 'session', percent: 47, severity: 'normal', resets_at: '2026-07-02T15:50:00.4+00:00' },
    { kind: 'weekly_all', percent: 10, severity: 'normal', resets_at: '2026-07-06T22:00:00.4+00:00' },
  ],
};

test('parseUsage reads the new limits[] array', () => {
  const AU = loadModule();
  const { session, weekly } = AU.parseUsage(LIVE);
  assert.equal(session.percent, 47);
  assert.equal(session.resetsAt, '2026-07-02T15:50:00.4+00:00');
  assert.equal(weekly.percent, 10);
});

test('parseUsage falls back to legacy five_hour / seven_day', () => {
  const AU = loadModule();
  const { session, weekly } = AU.parseUsage({
    five_hour: { utilization: 33, resets_at: 'x' },
    seven_day: { utilization: 5, resets_at: 'y' },
  });
  assert.equal(session.percent, 33);
  assert.equal(weekly.percent, 5);
});

test('isNewSessionWindow: ms jitter = same window; ~5h jump = new window', () => {
  const AU = loadModule();
  const base = Date.parse('2026-07-02T23:40:00.064Z');
  assert.equal(AU.isNewSessionWindow(base, Date.parse('2026-07-02T23:40:00.087Z')), false);
  assert.equal(AU.isNewSessionWindow(base, base + 5 * 3600e3), true);
  assert.equal(AU.isNewSessionWindow(null, base), false);
  assert.equal(AU.isNewSessionWindow(base, null), false);
  assert.equal(AU.isNewSessionWindow(base, base + 119000), false);
  assert.equal(AU.isNewSessionWindow(base, base + 121000), true);
});

test('createUsageStore: caches within refresh, persists, accumulates history', async () => {
  const AU = loadModule();
  const stored = {};
  const local = {
    get: async (k) => ({ [k]: stored[k] }),
    set: async (obj) => { Object.assign(stored, obj); },
    remove: async (k) => { delete stored[k]; },
  };
  let n = 0;
  const fakeFetch = async () => ({
    ok: true, fetchedAt: Date.now() + (n++),
    session: { percent: 10, resetsAt: '2030-01-01T00:00:00Z' }, weekly: { percent: 3 },
  });
  const store = AU.createUsageStore({ local, fetchUsage: fakeFetch, refreshMinutes: 60 });
  const a = await store.get(true);
  assert.equal(a.ok, true);
  assert.equal(a.history.length, 1);
  assert.ok(a.sessionWindow.end > a.sessionWindow.start);
  const b = await store.get(true);
  assert.equal(b.history.length, 2);
  const c = await store.get(false);           // within refresh → cache hit
  assert.equal(c.history.length, 2);
  assert.equal(stored.anthropicUsageHistory.accounts.unknown.data.samples.length, 2);   // persisted
});

test('createUsageStore: loads persisted history and appends within the window', async () => {
  const AU = loadModule();
  const end = Date.parse('2030-01-01T00:00:00Z');
  const stored = { anthropicUsageHistory: { windowEnd: end, samples: [
    { t: 1, session: 5, weekly: 1 }, { t: 2, session: 6, weekly: 2 },
  ] } };
  const local = { get: async (k) => ({ [k]: stored[k] }), set: async () => {}, remove: async () => {} };
  const fakeFetch = async () => ({
    ok: true, fetchedAt: Date.now(),
    session: { percent: 9, resetsAt: '2030-01-01T00:00:00Z' }, weekly: { percent: 3 },
  });
  const store = AU.createUsageStore({ local, fetchUsage: fakeFetch, refreshMinutes: 60 });
  const r = await store.get(true);
  assert.equal(r.history.length, 3);          // 2 loaded + 1 new, same window
});

test('createUsageStore: transient fetch failure keeps last good numbers; clearHistory resets', async () => {
  const AU = loadModule();
  let mode = 'ok';
  const fakeFetch = async () => {
    if (mode === 'fail') throw new Error('boom');
    return { ok: true, fetchedAt: Date.now(), session: { percent: 42, resetsAt: '2030-01-01T00:00:00Z' }, weekly: { percent: 7 } };
  };
  const store = AU.createUsageStore({ fetchUsage: fakeFetch, refreshMinutes: 60 });
  const ok = await store.get(true);
  assert.equal(ok.session.percent, 42);
  mode = 'fail';
  const stale = await store.get(true);
  assert.equal(stale.session.percent, 42);
  assert.equal(stale.stale, true);
  store.clearHistory();
  mode = 'ok';
  const fresh = await store.get(true);
  assert.equal(fresh.history.length, 1);      // history was wiped, one new sample
});

test('createUsageStore: a 401/403 is signed out, not a stale reading', async () => {
  const AU = loadModule();
  let mode = 'ok';
  const fakeFetch = async () => {
    if (mode !== 'ok') throw new Error(mode);
    return { ok: true, fetchedAt: Date.now(), session: { percent: 42, resetsAt: '2030-01-01T00:00:00Z' }, weekly: { percent: 7 } };
  };
  const store = AU.createUsageStore({ fetchUsage: fakeFetch, refreshMinutes: 60 });
  await store.get(true);
  for (const status of ['HTTP 401', 'HTTP 403']) {
    mode = status;
    const r = await store.get(true);
    assert.equal(r.ok, false);
    assert.equal(r.signedOut, true);
    assert.equal(r.stale, undefined, 'the old numbers belong to an account this browser has left');
    assert.equal(r.session, undefined);
  }
  // Signing back in recovers, and a plain server error is still just stale.
  mode = 'ok';
  assert.equal((await store.get(true)).session.percent, 42);
  mode = 'HTTP 500';
  const soft = await store.get(true);
  assert.equal(soft.stale, true);
  assert.equal(soft.signedOut, undefined);
});

test('createUsageStore: a signed-out answer is replayed for a minute, not refetched on every get', async (t) => {
  let clock = 1_000_000;
  t.mock.method(Date, 'now', () => clock);
  const AU = loadModule();
  let fetches = 0;
  const fakeFetch = async () => { fetches++; throw new Error('HTTP 401'); };
  const store = AU.createUsageStore({ fetchUsage: fakeFetch, refreshMinutes: 60 });
  assert.equal((await store.get()).signedOut, true);
  for (let i = 0; i < 5; i++) {
    clock += 2000;
    assert.equal((await store.get()).signedOut, true);
  }
  assert.equal(fetches, 1, 'every 2 s poll went back to claude.ai (and /organizations)');
  clock += 60000;
  await store.get();
  assert.equal(fetches, 2);
});

test('createUsageStore: a real session reset clears history and opens a new window', async () => {
  const AU = loadModule();
  let resetsAt = '2030-01-01T00:00:00Z';
  const fakeFetch = async () => ({ ok: true, fetchedAt: Date.now(), session: { percent: 20, resetsAt }, weekly: { percent: 5 } });
  const store = AU.createUsageStore({ fetchUsage: fakeFetch, refreshMinutes: 60 });
  await store.get(true);
  const a = await store.get(true);
  assert.equal(a.history.length, 2);
  const win1End = a.sessionWindow.end;
  resetsAt = '2030-01-01T05:00:00Z';           // +5h → a real reset (beyond the 2min tolerance)
  const b = await store.get(true);
  assert.equal(b.history.length, 1);           // old window's history cleared, one new sample
  assert.ok(b.sessionWindow.end > win1End);    // rolled to a new window
});

test('createUsageStore: weekly history survives session resets and keeps the 7-day window', async (t) => {
  let clock = Date.parse('2030-01-01T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const AU = loadModule();
  let sessionReset = '2030-01-01T05:00:00Z';
  const weeklyReset = '2030-01-05T00:00:00Z';
  const fakeFetch = async () => ({
    ok: true, fetchedAt: Date.now(),
    session: { percent: 20, resetsAt: sessionReset }, weekly: { percent: 5, resetsAt: weeklyReset },
  });
  const store = AU.createUsageStore({ fetchUsage: fakeFetch, refreshMinutes: 60 });
  await store.get(true);
  clock += 6 * 60000;
  const a = await store.get(true);
  assert.equal(a.weeklyHistory.length, 2);
  assert.equal(a.weeklyWindow.end - a.weeklyWindow.start, 7 * 24 * 3600e3);
  clock += 60000;                                 // inside the 5-minute gap: not recorded
  assert.equal((await store.get(true)).weeklyHistory.length, 2);
  clock += 6 * 3600e3;
  sessionReset = '2030-01-01T11:00:00Z';          // the session rolls, the week does not
  const b = await store.get(true);
  assert.equal(b.history.length, 1);
  assert.equal(b.weeklyHistory.length, 3);
});

test('createUsageStore: a reset granted mid-week restarts the weekly chart at the reset', async (t) => {
  let clock = Date.parse('2030-01-03T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const AU = loadModule();
  let weekly = { percent: 40, resetsAt: '2030-01-05T00:00:00Z' };
  const fakeFetch = async () => ({
    ok: true, fetchedAt: Date.now(), session: { percent: 1, resetsAt: '2030-01-03T05:00:00Z' }, weekly,
  });
  const store = AU.createUsageStore({ fetchUsage: fakeFetch, refreshMinutes: 60 });
  await store.get(true);
  clock += 3600e3;
  const before = await store.get(true);
  assert.equal(before.weeklyWindow.start, Date.parse('2029-12-29T00:00:00Z'));

  // Same deadline, usage fell to 0: a reset that kept the old resets_at.
  clock += 3600e3;
  const resetAt = clock;
  weekly = { percent: 0, resetsAt: '2030-01-05T00:00:00Z' };
  const kept = await store.get(true);
  assert.equal(kept.weeklyHistory.length, 1, 'no dip to zero: the old samples are gone');
  assert.equal(kept.weeklyWindow.start, resetAt);
  assert.equal(kept.weeklyWindow.end, Date.parse('2030-01-05T00:00:00Z'));

  // New deadline 7 days after an early reset: the window starts at the reset, not 7 days back.
  clock += 3600e3;
  const early = clock - 1800e3;
  weekly = { percent: 0, resetsAt: new Date(early + 7 * 24 * 3600e3).toISOString() };
  const moved = await store.get(true);
  assert.equal(moved.weeklyHistory.length, 1);
  assert.equal(moved.weeklyWindow.start, early);
  assert.equal(moved.weeklyWindow.end - moved.weeklyWindow.start, 7 * 24 * 3600e3);
});

test('createUsageStore: each account keeps its own samples; signing back in finds them', async (t) => {
  let clock = Date.parse('2030-01-03T00:00:00Z');
  t.mock.method(Date, 'now', () => clock);
  const AU = loadModule();
  let accountId = 'org-a';
  const fakeFetch = async () => ({
    ok: true, accountId, fetchedAt: Date.now(),
    session: { percent: 20, resetsAt: '2030-01-03T05:00:00Z' },
    weekly: { percent: 5, resetsAt: '2030-01-05T00:00:00Z' },
  });
  const stored = {};
  const local = {
    get: async (k) => ({ [k]: stored[k] }),
    set: async (o) => Object.assign(stored, o),
    remove: async (k) => { delete stored[k]; },
  };
  const make = () => AU.createUsageStore({ local, fetchUsage: fakeFetch, refreshMinutes: 60 });
  const store = make();
  await store.get(true);
  clock += 6 * 60000;
  assert.equal((await store.get(true)).weeklyHistory.length, 2);

  accountId = 'org-b';                            // somebody else signs in
  clock += 6 * 60000;
  const b = await store.get(true);
  assert.equal(b.weeklyHistory.length, 1, "not org-a's samples");
  assert.equal(b.history.length, 1);

  accountId = 'org-a';                            // and back
  clock += 6 * 60000;
  const a = await store.get(true);
  assert.equal(a.weeklyHistory.length, 3, 'org-a still has what it had, plus the new reading');
  assert.equal(a.history.length, 3);
  assert.deepEqual(Object.keys(stored.anthropicUsageHistory.accounts).sort(), ['org-a', 'org-b']);

  // A new background page (browser restart) reads both accounts back.
  accountId = 'org-b';
  clock += 6 * 60000;
  assert.equal((await make().get(true)).weeklyHistory.length, 2);

  store.clearHistory();
  assert.equal(stored.anthropicUsageHistory, undefined, 'Reset to Defaults clears every account');
});
