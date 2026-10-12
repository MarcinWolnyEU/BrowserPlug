/*
 * The test.* wire actions are harness helpers and must not run in a release build.
 *
 * Gecko gate: storage.sync must hold BOTH testPort and testHostname, which only the
 * Firefox harness seeds (before Firefox's first start). A normal profile has neither and
 * nothing on the wire writes them. Sliced out of background.js by anchor and driven with
 * stubs; the Chromium twin (tests/js-chromium/testgate.test.mjs) gates on BUILD_CONFIG.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

const ACTIONS = ['get_popup_state', 'get_options_state', 'get_storage_values',
  'validate_ui_sync', 'refresh_ui_state', 'visit_url', 'visit_multiple_urls',
  'create_test_tabs'].map((a) => `test.${a}`);

function load(storage, { throws = false } = {}) {
  const start = BG_SRC.indexOf('// Test helper action handler');
  const end = BG_SRC.indexOf('async function handleGetPopupState(', start);
  assert.ok(start >= 0 && end > start, 'test helper block not found in background.js');
  const block = BG_SRC.slice(start, end);
  // eslint-disable-next-line no-new-func
  return new Function('storage', 'throws', `
    const ran = [];
    const errors = [];
    const browser = { storage: { sync: { get: async (defaults) => {
      if (throws) throw new Error('storage unavailable');
      return Object.assign({}, defaults, storage);
    } } } };
    function sendError(id, code, message) { errors.push({ id, code, message }); }
    function sendResponse() {}
    const stub = (n) => async () => { ran.push(n); };
    const handleGetPopupState = stub('popup'), handleGetOptionsState = stub('options'),
      handleGetStorageValues = stub('storage'), handleValidateUISync = stub('validate'),
      handleRefreshUIState = stub('refresh'), handleVisitURL = stub('visit'),
      handleVisitMultipleURLs = stub('visitMany'), handleCreateTestTabs = stub('tabs');
    ${block}
    return { handleTestAction, testHelpersEnabled, ran, errors };
  `)(storage, throws);
}

test('a normal profile refuses every test.* action and runs no handler', async () => {
  for (const action of ACTIONS) {
    const m = load({});
    await m.handleTestAction('r1', action, {});
    assert.deepEqual(m.ran, [], `${action} must not reach its handler`);
    assert.equal(m.errors.length, 1);
    assert.equal(m.errors[0].code, 'TEST_ACTIONS_UNSUPPORTED');
    assert.match(m.errors[0].message, new RegExp(action.replace('.', '\\.')));
  }
});

test('a half-seeded or default-port profile does not open the gate', async () => {
  for (const s of [{ testPort: 7777 }, { testHostname: 'localhost' },
    { testPort: 8772, testHostname: 'localhost' }, { testPort: '7777', testHostname: 'localhost' },
    { testPort: 7777, testHostname: '' }, { testPort: null, testHostname: null }]) {
    assert.equal(await load(s).testHelpersEnabled(), false, JSON.stringify(s));
  }
});

test('an unreadable storage fails closed', async () => {
  const m = load({ testPort: 7777, testHostname: 'localhost' }, { throws: true });
  await m.handleTestAction('r2', 'test.visit_url', { url: 'https://example.org/' });
  assert.equal(m.errors[0].code, 'TEST_ACTIONS_UNSUPPORTED');
  assert.deepEqual(m.ran, []);
});

test('the harness-seeded profile (testPort + testHostname) runs the handlers', async () => {
  const m = load({ testPort: 7777, testHostname: 'localhost' });
  await m.handleTestAction('r3', 'test.get_popup_state', {});
  await m.handleTestAction('r4', 'test.create_test_tabs', {});
  assert.deepEqual(m.ran, ['popup', 'tabs']);
  assert.deepEqual(m.errors, []);
  await m.handleTestAction('r5', 'test.nope', {});
  assert.equal(m.errors[0].code, 'UNKNOWN_ACTION', 'an enabled build still rejects unknown test actions');
});

test('glue: the dispatcher routes the test namespace through handleTestAction', () => {
  assert.match(BG_SRC, /case 'test':\s+await handleTestAction\(id, action, data\);/);
});

test('glue: the harness seeds what the gate reads', () => {
  const py = fs.readFileSync(
    new URL('../firefox_test_utils.py', import.meta.url), 'utf-8');
  assert.match(py, /'testPort': self\.test_port/);
  assert.match(py, /'testHostname': 'localhost'/);
});
