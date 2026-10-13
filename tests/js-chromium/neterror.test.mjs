/*
 * CHROMIUM TWIN of tests/js/neterror.test.mjs — the net-error humanization block in
 * extension-chromium/background.js: NET_ERROR_HUMAN / humanizeNetError.
 *
 * The VOCABULARY differs by engine and that is the whole point of a separate twin: Blink
 * reports `net::ERR_*` strings from BOTH webRequest AND webNavigation, so there is no
 * decimal-nsresult form to decode (gecko's NSRESULT_NAMES table has no chromium analog).
 * The humanized OUTPUT FORMAT is shared with gecko — "phrase [CODE]" for a mapped code,
 * and an unmapped code passes through unchanged so nothing is ever swallowed.
 *
 * Like the gecko original: background.js is a top-level SW script, so we slice the pure
 * block out of the source (stable anchors) and instantiate it.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');

function loadNetErrorHelpers() {
  const start = BG_SRC.indexOf('const NET_ERROR_HUMAN');
  const end = BG_SRC.indexOf('browser.webRequest.onBeforeRequest.addListener', start);
  assert.ok(start >= 0 && end > start, 'net-error block not found in background.js');
  const block = BG_SRC.slice(start, end);
  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    ${block}
    return { NET_ERROR_HUMAN, humanizeNetError };
  `);
  return factory();
}

// ---- net::ERR_* humanization (the Blink vocabulary) --------------------------

test('humanizeNetError maps net::ERR_CONNECTION_REFUSED to a plain-English cause, keeping the code', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  const out = humanizeNetError('net::ERR_CONNECTION_REFUSED');
  assert.match(out, /connection refused/i, 'human-readable cause included');
  assert.match(out, /\[net::ERR_CONNECTION_REFUSED\]/, 'the raw code stays visible');
});

test('the common connectivity failures each map to a DISTINCT cause an agent can branch on', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  const cases = {
    'net::ERR_CONNECTION_REFUSED': /connection refused/i,
    'net::ERR_CONNECTION_RESET': /connection reset/i,
    'net::ERR_ABORTED': /aborted/i,
    'net::ERR_TIMED_OUT': /timeout|timed out/i,
    'net::ERR_NAME_NOT_RESOLVED': /unknown host|dns/i,
  };
  const outputs = Object.entries(cases).map(([code, rx]) => {
    const out = humanizeNetError(code);
    assert.match(out, rx, code);
    assert.match(out, new RegExp('\\[' + code.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\]'), `${code} keeps its code`);
    return out;
  });
  assert.equal(new Set(outputs).size, outputs.length, 'each failure reads distinctly');
});

test('the SOCKS/proxy failure codes are mapped — the ones a would-be proxy user would hit', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  // Chromium has no per-tab proxy, but a browser-global proxy or a blocked port still
  // surfaces these; they must read clearly rather than as an opaque code.
  assert.match(humanizeNetError('net::ERR_PROXY_CONNECTION_FAILED'), /proxy connection failed/i);
  assert.match(humanizeNetError('net::ERR_SOCKS_CONNECTION_FAILED'), /SOCKS proxy connection failed/i);
});

// ---- passthrough + edge cases -------------------------------------------------

test('an unmapped net::ERR_* code passes through unchanged (nothing is swallowed)', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  assert.equal(humanizeNetError('net::ERR_SOMETHING_BRAND_NEW'), 'net::ERR_SOMETHING_BRAND_NEW');
});

test('empty / null input is null; a non-net string passes through unchanged', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  assert.equal(humanizeNetError(''), null);
  assert.equal(humanizeNetError(null), null);
  assert.equal(humanizeNetError(undefined), null);
  assert.equal(humanizeNetError('some other error'), 'some other error');
});

// Chromium has NO decimal "Error code <nsresult>" form (that is gecko's webNavigation
// quirk) — a numeric-looking string is not decoded, it is just returned as-is.
test('there is no decimal-nsresult decoding on chromium: a numeric string is returned raw', () => {
  const { humanizeNetError } = loadNetErrorHelpers();
  assert.equal(humanizeNetError('Error code 2152398861'), 'Error code 2152398861',
    'Blink never reports the decimal nsresult form — nothing to decode');
});

// ---- table consistency ---------------------------------------------------------

test('every code in NET_ERROR_HUMAN resolves to its phrase followed by the bracketed code', () => {
  const { NET_ERROR_HUMAN, humanizeNetError } = loadNetErrorHelpers();
  const codes = Object.keys(NET_ERROR_HUMAN);
  assert.ok(codes.length >= 20, 'the humanization table is populated (a parse regression would empty it)');
  for (const code of codes) {
    const out = humanizeNetError(code);
    assert.ok(out.startsWith(NET_ERROR_HUMAN[code]), `${code} keeps its human text`);
    assert.ok(out.endsWith(`[${code}]`), `${code} keeps its bracketed code`);
    assert.ok(code.startsWith('net::ERR_'), `${code} is a Blink net-error code`);
  }
});
