// The glue around every provider usage store, checked in both trees from their sources.
// Run: node --test tests/js/usage-wiring.test.mjs   (or: npm run test:js)
//
// A store (vendor/<provider>-data.js) can pass its own unit tests and still do nothing, or
// kill the whole background, because a line elsewhere is missing:
//   * the file is not in Firefox's background.scripts, or not in Chromium's importScripts, or
//     comes before vendor/period-history.js, which the store needs at load time. The first
//     two end in "XData is not defined" and take the background down at startup.
//   * background.js does not answer the get<Provider>Usage message the popup and Options
//     send, so the Usage card waits for ever (askBackground resolves null).
//   * "Reset to Defaults" does not call the store's clearHistory(), so a reset leaves the
//     samples behind.
//   * the settings block never gives the store its refresh rate or its background clock.
// The usage-sampler tests slice only the settings block, so none of this is covered there.
// Providers are discovered from vendor/*-data.js, so a fifth store is held to the same rules
// without anyone remembering to add it here.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = (rel) => readFileSync(path.join(ROOT, rel), 'utf8');

const GECKO = 'extension-gecko';
const CHROMIUM = 'extension-chromium';

// vendor/*-data.js files that publish a usage store: { file, global }.
const STORES = readdirSync(path.join(ROOT, GECKO, 'vendor'))
  .filter((f) => f.endsWith('-data.js'))
  .map((f) => ({ file: `vendor/${f}`, src: read(`${GECKO}/vendor/${f}`) }))
  .filter((s) => /createUsageStore/.test(s.src))
  .map((s) => ({ file: s.file, global: (s.src.match(/globalThis\.(\w+)\s*=\s*\{/) || [])[1] }));

// How each tree names the data modules it loads, in load order.
const loadOrder = {
  [GECKO]: () => JSON.parse(read(`${GECKO}/manifest.json`)).background.scripts,
  [CHROMIUM]: () => {
    // Anchored to the line start: the header comment says "importScripts() loads ...".
    const m = read(`${CHROMIUM}/background.js`).match(/^importScripts\(([^)]*)\)/m);
    assert.ok(m, 'no importScripts(...) call in the chromium background.js');
    return [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1]);
  },
};

const slice = (src, from, to) => {
  const a = src.indexOf(from);
  assert.ok(a >= 0, `"${from}" not found in background.js`);
  const b = src.indexOf(to, a);
  assert.ok(b > a, `"${to}" not found after "${from}"`);
  return src.slice(a, b);
};

test('the usage stores are found, so the checks below cannot pass by matching nothing', () => {
  assert.ok(STORES.length >= 4, `found only ${STORES.map((s) => s.file)}`);
  for (const s of STORES) assert.ok(s.global, `${s.file} publishes no global`);
  assert.ok(STORES.some((s) => s.file === 'vendor/mistral-data.js'));
});

for (const tree of [GECKO, CHROMIUM]) {
  test(`${tree}: every usage store is loaded after period-history.js and before background.js`, () => {
    const order = loadOrder[tree]();
    const base = order.indexOf('vendor/period-history.js');
    assert.ok(base >= 0, 'vendor/period-history.js is not loaded');
    for (const { file } of STORES) {
      const at = order.indexOf(file);
      assert.ok(at >= 0, `${file} is not loaded by ${tree}: the background would die on "is not defined"`);
      assert.ok(at > base, `${file} is loaded before vendor/period-history.js, which it uses at load time`);
      const last = order.indexOf('background.js');
      if (last >= 0) assert.ok(at < last, `${file} is loaded after background.js`);
    }
    // Both trees carry the same set, so one cannot lose a provider the other has.
    const mine = order.filter((f) => /^vendor\/[\w-]+-data\.js$/.test(f)).sort();
    const other = loadOrder[tree === GECKO ? CHROMIUM : GECKO]()
      .filter((f) => /^vendor\/[\w-]+-data\.js$/.test(f)).sort();
    assert.deepEqual(mine, other);
  });

  test(`${tree}: background.js builds each store, answers its message, resets it and feeds its settings`, () => {
    const bg = read(`${tree}/background.js`);
    const reset = slice(bg, "request.type === 'resetToDefaults'", 'sendResponse({ success: true })');
    const settings = slice(bg, '// ---- provider usage settings', '// ---- end provider usage settings');
    const actions = [];
    for (const { global, file } of STORES) {
      const built = bg.match(new RegExp(`const (\\w+) = ${global}\\.createUsageStore\\(`));
      assert.ok(built, `${tree}/background.js never builds ${global}.createUsageStore (${file})`);
      const store = built[1];

      const answers = bg.match(new RegExp(
        `request\\.action === '(get\\w+Usage)'\\) \\{\\s*${store}\\.get\\(request\\.force\\)`));
      assert.ok(answers, `${tree}/background.js has no get<Provider>Usage handler calling ${store}.get(request.force)`);
      actions.push(answers[1]);

      assert.ok(reset.includes(`${store}.clearHistory()`),
        `"Reset to Defaults" in ${tree} does not clear ${store}'s history`);
      assert.ok(settings.includes(`${store}.setRefreshMinutes(`),
        `${store} never gets its refresh rate in ${tree}'s provider usage settings`);
      assert.ok(settings.includes(`${store}.get(true)`),
        `${store} has no background clock entry in ${tree}'s provider usage settings`);
    }
    assert.equal(new Set(actions).size, actions.length, 'two stores answer one message');
  });

  test(`${tree}: every get<Provider>Usage message the popup or Options sends is answered here`, () => {
    const bg = read(`${tree}/background.js`);
    const answered = new Set([...bg.matchAll(/request\.action === '(get\w+Usage)'/g)].map((m) => m[1]));
    for (const rel of ['popup/popup.js', 'options.js']) {
      const asked = new Set([...read(`${tree}/${rel}`).matchAll(/'(get\w+Usage)'/g)].map((m) => m[1]));
      assert.ok(asked.size >= 4, `${rel}: found only ${[...asked]}`);
      for (const action of asked) {
        assert.ok(answered.has(action), `${rel} sends ${action}, which ${tree}/background.js does not answer`);
      }
    }
  });
}
