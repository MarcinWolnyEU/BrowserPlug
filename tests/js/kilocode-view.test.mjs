// Unit tests for extension-gecko/vendor/kilocode-view.js (jsdom, no Firefox).
// Run: node --test tests/js/kilocode-view.test.mjs   (or: npm run test:js)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/kilocode-view.js'), 'utf8');

function loadModule() {
  const dom = new JSDOM('<!doctype html><body></body>');
  const g = dom.window;
  g.globalThis = g;
  // eslint-disable-next-line no-new-func
  new Function('window', 'document', 'globalThis', SRC)(g, g.document, g);
  return g.KiloCode;
}

test('isKiloCli matches only Kilo CLI mcp_client rows', () => {
  const K = loadModule();
  assert.equal(K.isKiloCli({ kind: 'mcp_client', friendly: 'Kilo CLI' }), true);
  assert.equal(K.isKiloCli({ kind: 'mcp_client', label: 'Kilo CLI — C:\\dr\\browserplug' }), true);
  assert.equal(K.isKiloCli({ kind: 'mcp_client', friendly: 'Claude Code' }), false);
  assert.equal(K.isKiloCli({ kind: 'server', friendly: 'Kilo CLI' }), false);
  assert.equal(K.isKiloCli(null), false);
});

test('hasModelBadge gates on the data, not on which agent it came from', () => {
  const K = loadModule();
  // pi rows carry the same provider/model pair (server/usage_panels/pi_data.py) and get the badge.
  assert.equal(K.hasModelBadge({ kind: 'mcp_client', friendly: 'pi', provider: 'openrouter',
    model: 'mistralai/devstral-2512' }), true);
  assert.equal(K.hasModelBadge({ kind: 'mcp_client', friendly: 'Kilo CLI', model: 'x' }), true);
  // Nothing detected → no badge, exactly as before.
  assert.equal(K.hasModelBadge({ kind: 'mcp_client', friendly: 'Kilo CLI' }), false);
  assert.equal(K.hasModelBadge({ kind: 'server', provider: 'openrouter' }), false);
  assert.equal(K.hasModelBadge(null), false);
});

test('providerLabel / modelLabel fallbacks', () => {
  const K = loadModule();
  assert.equal(K.providerLabel('gitlab'), 'GitLab Duo');
  assert.equal(K.providerLabel('GITLAB'), 'GitLab Duo');
  assert.equal(K.providerLabel('somenew'), 'somenew');
  assert.equal(K.modelLabel('deepseek/deepseek-v4-flash'), 'deepseek-v4-flash');
  assert.equal(K.modelLabel('duo-chat-haiku-4-5'), 'duo-chat-haiku-4-5');
});

test('buildProviderBadge falls back to code when no resolved name', () => {
  const K = loadModule();
  const el = K.buildProviderBadge({ provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' });
  assert.ok(el);
  assert.match(el.textContent, /OpenRouter/);
  assert.match(el.textContent, /deepseek-v4-flash/);
  assert.match(el.title, /providerID=openrouter/);
  assert.equal(K.buildProviderBadge({}), null);
});

test('buildProviderBadge prefers resolved provider_name / model_name', () => {
  const K = loadModule();
  const el = K.buildProviderBadge({
    provider: 'gitlab', model: 'duo-chat-haiku-4-5',
    provider_name: 'GitLab', model_name: 'Agentic Chat (Claude Haiku 4.5)',
  });
  assert.match(el.textContent, /GitLab · Agentic Chat \(Claude Haiku 4\.5\)/);
  // tooltip still shows the raw ids
  assert.match(el.title, /modelID=duo-chat-haiku-4-5/);
});

test('summaryText mirrors the badge (resolved names preferred)', () => {
  const K = loadModule();
  assert.equal(
    K.summaryText({ provider: 'gitlab', model: 'duo-chat-haiku-4-5', provider_name: 'GitLab', model_name: 'Agentic Chat (Claude Haiku 4.5)' }),
    'GitLab · Agentic Chat (Claude Haiku 4.5)');
  assert.equal(K.summaryText({ provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' }), 'OpenRouter · deepseek-v4-flash');
  assert.equal(K.summaryText({}), '');
});
