/*
 * CHROMIUM TWIN of tests/js/testgate.test.mjs.
 *
 * Same refusal, different gate: production ships config.js with BUILD_CONFIG = null and
 * only the Chromium harness bakes testMode: true into its per-session extension copy, so
 * the predicate is BUILD_CONFIG.testMode === true (no storage involved, because a
 * synced storage value is not a build fact).
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');

const ACTIONS = ['get_popup_state', 'get_options_state', 'get_storage_values',
  'validate_ui_sync', 'refresh_ui_state', 'visit_url', 'visit_multiple_urls',
  'create_test_tabs'].map((a) => `test.${a}`);

// buildConfig: the value to expose as BUILD_CONFIG, or the string 'undeclared'.
function load(buildConfig) {
  const start = BG_SRC.indexOf('// Test helper action handler');
  const end = BG_SRC.indexOf('async function handleGetPopupState(', start);
  assert.ok(start >= 0 && end > start, 'test helper block not found in background.js');
  const block = BG_SRC.slice(start, end);
  const decl = buildConfig === 'undeclared' ? '' : `const BUILD_CONFIG = ${JSON.stringify(buildConfig)};`;
  // eslint-disable-next-line no-new-func
  return new Function(`
    const ran = [];
    const errors = [];
    function sendError(id, code, message) { errors.push({ id, code, message }); }
    function sendResponse() {}
    const stub = (n) => async () => { ran.push(n); };
    const handleGetPopupState = stub('popup'), handleGetOptionsState = stub('options'),
      handleGetStorageValues = stub('storage'), handleValidateUISync = stub('validate'),
      handleRefreshUIState = stub('refresh'), handleVisitURL = stub('visit'),
      handleVisitMultipleURLs = stub('visitMany'), handleCreateTestTabs = stub('tabs');
    ${decl}
    ${block}
    return { handleTestAction, testHelpersEnabled, ran, errors };
  `)();
}

test('a release build (BUILD_CONFIG = null) refuses every test.* action', async () => {
  for (const action of ACTIONS) {
    const m = load(null);
    await m.handleTestAction('r1', action, {});
    assert.deepEqual(m.ran, [], `${action} must not reach its handler`);
    assert.equal(m.errors.length, 1);
    assert.equal(m.errors[0].code, 'TEST_ACTIONS_UNSUPPORTED');
    assert.match(m.errors[0].message, new RegExp(action.replace('.', '\\.')));
  }
});

test('anything short of testMode: true keeps the gate shut', async () => {
  for (const cfg of ['undeclared', null, {}, { wsPort: 7777 }, { testMode: 'true' },
    { testMode: 1 }, { testMode: false }]) {
    assert.equal(await load(cfg).testHelpersEnabled(), false, JSON.stringify(cfg));
  }
});

test('the harness bake (testMode: true) runs the handlers', async () => {
  const m = load({ wsPort: 7777, wsHost: 'localhost', testMode: true });
  await m.handleTestAction('r3', 'test.get_popup_state', {});
  await m.handleTestAction('r4', 'test.create_test_tabs', {});
  assert.deepEqual(m.ran, ['popup', 'tabs']);
  assert.deepEqual(m.errors, []);
  await m.handleTestAction('r5', 'test.nope', {});
  assert.equal(m.errors[0].code, 'UNKNOWN_ACTION');
});

test('glue: the dispatcher routes the test namespace through handleTestAction', () => {
  assert.match(BG_SRC, /case 'test':\s+await handleTestAction\(id, action, data\);/);
});

test('glue: the shipped config.js is the release value and the harness bakes testMode', () => {
  const cfg = fs.readFileSync(
    new URL('../../extension-chromium/config.js', import.meta.url), 'utf-8');
  assert.match(cfg, /^const BUILD_CONFIG = null;\r?$/m);
  const py = fs.readFileSync(
    new URL('../chromium_test_utils.py', import.meta.url), 'utf-8');
  assert.match(py, /testMode: true/);
});
