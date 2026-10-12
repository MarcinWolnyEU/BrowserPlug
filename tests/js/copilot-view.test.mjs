// Unit tests for extension-gecko/vendor/copilot-view.js (jsdom, no browser).
// Run: node --test tests/js/copilot-view.test.mjs   (or: npm run test:js)
//
// The case that matters most is the account check. A browser can hold several GitHub
// accounts and VS Code signs in to Copilot on its own, so the quota this browser reads
// can belong to somebody other than the editor on the row. Drawing that quota as a bar
// would be a wrong number that looks right; the row must say "other account" instead,
// and the copy path must say the same thing.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(__dirname, '../../extension-gecko/vendor/copilot-view.js'), 'utf8');

function loadModule() {
  const dom = new JSDOM('<!doctype html><body></body>');
  const g = dom.window;
  g.globalThis = g;
  // eslint-disable-next-line no-new-func
  new Function('window', 'document', 'globalThis', SRC)(g, g.document, g);
  return g.CopilotView;
}

const usage = (over = {}) => Object.assign({
  ok: true,
  login: 'octo-editor',
  plan: 'free',
  resetsAt: new Date(Date.now() + 3 * 86400e3).toISOString(),
  chat: { percent: 25, used: 50, total: 200, unlimited: false },
  completions: { percent: 5.2, used: 103, total: 2000, unlimited: false },
  premium: null,
}, over);

const row = (copilot) => ({
  kind: 'mcp_client', friendly: 'GitHub Copilot Chat', label: 'GitHub Copilot Chat — c:\\work',
  provider: copilot && copilot.model ? 'github-copilot' : undefined,
  model: copilot && copilot.model,
  copilot,
});

const SESSION = {
  model: 'gpt-6-luna', account: 'octo-editor', sku: 'free_limited_copilot', requests: 3,
  last_request_hms: '16:52:12', chat_title: 'fix the parser', chat_turns: 2, workspace: 'c:\\work',
};

test('the Copilot row is recognised by name, and nothing else is', () => {
  const CV = loadModule();
  assert.equal(CV.isCopilotClient(row(null)), true);
  assert.equal(CV.isCopilotClient({ kind: 'mcp_client', label: 'GitHub Copilot Chat — c:\\x' }), true);
  assert.equal(CV.isCopilotClient({ kind: 'mcp_client', friendly: 'Visual Studio Code' }), false,
    'the editor itself is a different row');
  assert.equal(CV.isCopilotClient({ kind: 'extension', friendly: 'GitHub Copilot Chat' }), false);
});

test('the bar meters premium requests on a paid plan and chat on Copilot Free', () => {
  const CV = loadModule();
  assert.equal(CV.primaryQuota(usage()).name, 'Chat');
  const paid = usage({
    plan: 'individual',
    chat: { percent: 0, used: 0, total: 0, unlimited: true },
    premium: { percent: 40, used: 120, total: 300, unlimited: false },
  });
  assert.equal(CV.primaryQuota(paid).name, 'Premium requests');
  assert.equal(CV.primaryQuota(usage({ chat: { unlimited: true, percent: 0 } })), null,
    'nothing metered means no bar rather than a bar stuck at 0%');
});

test('matching accounts draw the bar and the session badge', () => {
  const CV = loadModule();
  const el = CV.buildRowInline(row(SESSION), usage(), {}, true);
  const spans = [...el.children];
  assert.equal(spans.length, 2);
  assert.equal(spans[0].style.width, '38px', 'the same bar as every other provider');
  assert.match(spans[0].title, /^Chat 25% \(50 of 200\)/);
  assert.match(spans[1].textContent, /gpt-6-luna/);
  assert.match(spans[1].title, /Chat: "fix the parser" · 2 turns/);
  assert.match(spans[1].title, /VS Code Copilot account: octo-editor \(free_limited_copilot\)/);
});

test('different accounts: "other account" instead of the bar, on the row and in the copy', () => {
  const CV = loadModule();
  const other = usage({ login: 'octo-personal' });
  const conn = row(SESSION);
  assert.equal(CV.accountMismatch(other, conn), true);
  const el = CV.buildRowInline(conn, other, {}, true);
  assert.equal(el.querySelector('span[style*="width:38px"]'), null, 'no bar drawn from the wrong account');
  const marker = el.querySelector('.conn-na');
  assert.equal(marker.textContent, 'other account');
  assert.match(marker.title, /octo-personal.*octo-editor/s);
  assert.match(CV.rowSummaryText(conn, other, {}, true), /^usage is for octo-personal, not octo-editor/);
});

test('account comparison ignores case and needs both sides', () => {
  const CV = loadModule();
  assert.equal(CV.accountMismatch(usage({ login: 'OCTO-editor' }), row(SESSION)), false);
  assert.equal(CV.accountMismatch(usage(), row(null)), false, 'details off: nothing to compare with');
  assert.equal(CV.accountMismatch(usage(), row({ ...SESSION, account: null })), false);
});

test('signed out: a marker, and the session badge still shows', () => {
  const CV = loadModule();
  const el = CV.buildRowInline(row(SESSION), { ok: false, signedOut: true, error: 'not signed in to github.com' }, {}, true);
  assert.equal(el.querySelector('.conn-na').textContent, 'signed out');
  assert.match(el.textContent, /gpt-6-luna/);
});

test('the Usage-tab-only mode leaves the bar off the row but keeps the badge', () => {
  const CV = loadModule();
  const el = CV.buildRowInline(row(SESSION), usage(), {}, false);
  assert.equal(el.children.length, 1);
  assert.match(el.textContent, /gpt-6-luna/);
  assert.equal(CV.buildRowInline(row(null), usage(), {}, false), null, 'nothing at all: no inline element');
  assert.equal(CV.rowSummaryText(row(SESSION), usage(), {}, false), 'gpt-6-luna · 2 turns');
});

test('code completions split the bar only when asked', () => {
  const CV = loadModule();
  const one = CV.makeUsageBar(usage(), {});
  const two = CV.makeUsageBar(usage(), { showCompletions: true });
  assert.equal(one.children.length, 1);
  assert.equal(two.children.length, 3, 'two halves and the divider');
  assert.match(two.title, /Code completions 5\.2% \(103 of 2,000\)/);
});

test('the copy text carries the quota, the model and the turns', () => {
  const CV = loadModule();
  const s = CV.rowSummaryText(row(SESSION), usage(), { showCompletions: true }, true);
  assert.match(s, /^Chat 25% \(50 of 200\) · Code completions 5\.2% \(103 of 2,000\) \(resets in \dd \d+h\) · gpt-6-luna · 2 turns$/);
});

test('authState: signed in names the account; signed out and failures offer the sign-in button', () => {
  const CV = loadModule();
  const ok = CV.authState(usage(), null);
  assert.equal(ok.tone, 'ok');
  assert.match(ok.text, /github\.com as octo-editor \(plan: free\)/);
  assert.equal(ok.signIn, false);
  const out = CV.authState({ ok: false, signedOut: true }, null);
  assert.deepEqual([out.tone, out.signIn], ['warn', true]);
  const bad = CV.authState(null, 'the extension did not answer');
  assert.equal(bad.tone, 'bad');
  assert.match(bad.help, /the extension did not answer/);
  assert.equal(CV.authState(usage({ stale: true, error: 'HTTP 502' }), null).tone, 'warn');
});
