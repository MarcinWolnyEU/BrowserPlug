// Unit tests for extension-gecko/vendor/kilocode-modelnames-data.js (no DOM, no Firefox).
// Run: node --test tests/js/kilocode-modelnames-data.test.mjs   (or: npm run test:js)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/kilocode-modelnames-data.js'), 'utf8');

function loadModule() {
  const g = {};
  g.globalThis = g;
  // eslint-disable-next-line no-new-func
  new Function('globalThis', 'fetch', SRC)(g, () => { throw new Error('ambient fetch not expected'); });
  return g.KiloModelNames;
}

// Minimal models.dev-shaped catalog.
const CATALOG = {
  openrouter: { name: 'OpenRouter', models: { 'deepseek/deepseek-v4-flash': { name: 'DeepSeek V4 Flash' } } },
  gitlab: { name: 'GitLab', models: { 'duo-chat-haiku-4-5': { name: 'Agentic Chat (Claude Haiku 4.5)' } } },
};

function fakeLocal() {
  const stored = {};
  return {
    stored,
    get: async (k) => ({ [k]: stored[k] }),
    set: async (obj) => { Object.assign(stored, obj); },
    remove: async (k) => { delete stored[k]; },
  };
}

test('resolve: batch resolves names; one fetch; cache hit avoids refetch', async () => {
  const KMN = loadModule();
  const local = fakeLocal();
  let fetches = 0;
  const fetchImpl = async () => { fetches++; return { ok: true, json: async () => CATALOG }; };
  const r = KMN.createResolver({ local, fetch: fetchImpl });

  const pairs = [
    { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' },
    { provider: 'gitlab', model: 'duo-chat-haiku-4-5' },
  ];
  const out = await r.resolve(pairs);
  assert.equal(fetches, 1);   // one catalog fetch for the whole batch
  assert.equal(out['openrouter\ndeepseek/deepseek-v4-flash'].modelName, 'DeepSeek V4 Flash');
  assert.equal(out['openrouter\ndeepseek/deepseek-v4-flash'].providerName, 'OpenRouter');
  assert.equal(out['gitlab\nduo-chat-haiku-4-5'].modelName, 'Agentic Chat (Claude Haiku 4.5)');

  const out2 = await r.resolve(pairs);
  assert.equal(fetches, 1);   // cache hit → no refetch
  assert.equal(out2['gitlab\nduo-chat-haiku-4-5'].modelName, 'Agentic Chat (Claude Haiku 4.5)');
  assert.ok(local.stored.kiloModelNames);   // persisted
});

test('resolve: unknown model cached as null, not refetched; failed fetch retries', async () => {
  const KMN = loadModule();
  const local = fakeLocal();
  let fetches = 0, mode = 'fail';
  const fetchImpl = async () => {
    fetches++;
    if (mode === 'fail') throw new Error('offline');
    return { ok: true, json: async () => CATALOG };
  };
  const r = KMN.createResolver({ local, fetch: fetchImpl });

  const unknown = [{ provider: 'openrouter', model: 'nope/nope' }];
  const f1 = await r.resolve(unknown);
  assert.equal(f1['openrouter\nnope/nope'].modelName, null); // failed fetch → null
  assert.equal(fetches, 1);
  mode = 'ok';
  await r.resolve(unknown);                 // retries because failure wasn't cached
  assert.equal(fetches, 2);
  await r.resolve(unknown);                 // now cached as null → no refetch
  assert.equal(fetches, 2);
});

test('clear() drops the cache (memory + storage) → next resolve refetches', async () => {
  const KMN = loadModule();
  const local = fakeLocal();
  let fetches = 0;
  const fetchImpl = async () => { fetches++; return { ok: true, json: async () => CATALOG }; };
  const r = KMN.createResolver({ local, fetch: fetchImpl });
  const pairs = [{ provider: 'gitlab', model: 'duo-chat-haiku-4-5' }];
  await r.resolve(pairs);
  assert.equal(fetches, 1);
  r.clear();
  assert.equal(local.stored.kiloModelNames, undefined);
  await r.resolve(pairs);
  assert.equal(fetches, 2);                  // refetched after clear
});

test('resolve: partial cache — only the uncached pair triggers a (single) fetch', async () => {
  const KMN = loadModule();
  const local = fakeLocal();
  let fetches = 0;
  const fetchImpl = async () => { fetches++; return { ok: true, json: async () => CATALOG }; };
  const r = KMN.createResolver({ local, fetch: fetchImpl });
  await r.resolve([{ provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' }]);
  assert.equal(fetches, 1);
  const out = await r.resolve([
    { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' },  // already cached
    { provider: 'gitlab', model: 'duo-chat-haiku-4-5' },              // new
  ]);
  assert.equal(fetches, 2);   // exactly one more fetch, for the batch with the new pair
  assert.equal(out['openrouter\ndeepseek/deepseek-v4-flash'].modelName, 'DeepSeek V4 Flash');
  assert.equal(out['gitlab\nduo-chat-haiku-4-5'].modelName, 'Agentic Chat (Claude Haiku 4.5)');
});
