/*
 * Unit tests for the net-error humanization block in extension-gecko/background.js:
 * NET_ERROR_HUMAN / NSRESULT_NAMES / humanizeNetError.
 *
 * webNavigation reports an uncommitted load as "Error code <decimal nsresult>"
 * while webRequest names the SAME failure symbolically — humanizeNetError must
 * decode the integer form so both surfaces read alike, keep the raw integer
 * visible, and fall back to the raw form for genuinely unmapped codes.
 *
 * Like proxy.test.mjs: background.js is a top-level extension script, so we
 * slice the pure block out of the source (stable anchors) and instantiate it.
 * Run with: npm run test:js  (== node --test "tests/js/*.test.mjs")
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

function loadNetErrorHelpers() {
  const start = BG_SRC.indexOf('const NET_ERROR_HUMAN');
  const end = BG_SRC.indexOf('browser.webRequest.onBeforeRequest.addListener', start);
  assert.ok(start >= 0 && end > start, 'net-error block not found in background.js');
  const block = BG_SRC.slice(start, end);
  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    ${block}
    return { NET_ERROR_HUMAN, NSRESULT_NAMES, humanizeNetError };
  `);
  return factory();
}

// ---- decimal "Error code N" decoding (the webNavigation form) ----------------

test('decodes "Error code 2152398850" to NS_BINDING_ABORTED, keeping the raw int', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  const out = humanizeNetError('Error code 2152398850'); // 0x804B0002
  assert.match(out, /NS_BINDING_ABORTED/);
  assert.match(out, /2152398850/, 'raw integer stays visible');
  assert.match(out, /aborted/i, 'human-readable cause included');
});

test('decodes the common connectivity failures to distinct causes', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  // acceptance: refused / reset / aborted / timed-out / unknown-host must each
  // be distinguishable by name, so an agent can branch on cause.
  const cases = {
    2152398861: /NS_ERROR_CONNECTION_REFUSED.*|connection refused/,
    2152398868: /NS_ERROR_NET_RESET|connection reset/,
    2152398850: /NS_BINDING_ABORTED|aborted/,
    2152398862: /NS_ERROR_NET_TIMEOUT|timeout/,
    2152398878: /NS_ERROR_UNKNOWN_HOST|unknown host/,
  };
  const outputs = Object.entries(cases).map(([code, rx]) => {
    const out = humanizeNetError(`Error code ${code}`);
    assert.match(out, new RegExp(rx), `code ${code}`);
    assert.doesNotMatch(out, /^Error code /, `code ${code} must not stay raw`);
    return out;
  });
  assert.equal(new Set(outputs).size, outputs.length, 'each failure reads distinctly');
});

test('decodes NS_ERROR_DOM_BAD_URI (0x805303F4) — the login-failure code from the session', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  const out = humanizeNetError(`Error code ${0x805303F4}`);
  assert.match(out, /NS_ERROR_DOM_BAD_URI/);
});

test('an unmapped integer falls back to the raw form (nothing is swallowed)', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  assert.equal(humanizeNetError('Error code 99999999'), 'Error code 99999999');
});

// ---- symbolic passthrough (the webRequest form) -------------------------------

test('symbolic names keep their human text and name, without a bogus raw int', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  const out = humanizeNetError('NS_ERROR_CONNECTION_REFUSED');
  assert.match(out, /connection refused/);
  assert.match(out, /\[NS_ERROR_CONNECTION_REFUSED\]/);
});

test('an unknown symbolic name passes through unchanged; empty input is null', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  assert.equal(humanizeNetError('NS_ERROR_SOMETHING_NEW'), 'NS_ERROR_SOMETHING_NEW');
  assert.equal(humanizeNetError(''), null);
  assert.equal(humanizeNetError(null), null);
});

// ---- table consistency ---------------------------------------------------------

test('every NSRESULT name that has a human mapping resolves through the full chain', () => {
  const { NSRESULT_NAMES, NET_ERROR_HUMAN, humanizeNetError } = loadNetErrorHelpers();
  for (const [num, name] of Object.entries(NSRESULT_NAMES)) {
    const out = humanizeNetError(`Error code ${num}`);
    assert.match(out, new RegExp(name), `${num} -> ${name}`);
    if (NET_ERROR_HUMAN[name]) {
      assert.ok(out.startsWith(NET_ERROR_HUMAN[name]), `${name} keeps its human text`);
    }
  }
});
