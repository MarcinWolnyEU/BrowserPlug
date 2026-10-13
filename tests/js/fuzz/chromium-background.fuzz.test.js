'use strict';
/*
 * Property-based fuzzing of two pure helpers sliced out of the Chromium service worker
 * (extension-chromium/background.js), the way the twin tests slice them: humanizeNetError,
 * which shows a browser error code to an agent, and isPrivilegedUrl, which decides whether a
 * tab can be scripted. Both take strings the page or the browser chose.
 * Runs locally under `npm run test:js`.
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const fc = require('fast-check');

const SRC = fs.readFileSync(
  path.join(__dirname, '../../../extension-chromium/background.js'), 'utf-8');

function slice(startMarker, endMarker) {
  const start = SRC.indexOf(startMarker);
  const end = SRC.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} block not found in background.js`);
  return SRC.slice(start, end);
}

// eslint-disable-next-line no-new-func
const { NET_ERROR_HUMAN, humanizeNetError } = new Function(
  `${slice('const NET_ERROR_HUMAN', 'browser.webRequest.onBeforeRequest.addListener')}
   return { NET_ERROR_HUMAN, humanizeNetError };`)();
// eslint-disable-next-line no-new-func
const { isPrivilegedUrl } = new Function(
  `${slice('function isPrivilegedUrl(url) {', '// Resolve the target tab')}
   return { isPrivilegedUrl };`)();

const RUNS = { numRuns: 500 };

test('humanizeNetError never throws, and never invents text for an unknown code', () => {
  fc.assert(fc.property(fc.string({ unit: 'binary', maxLength: 120 }), (code) => {
    const out = humanizeNetError(code);
    if (code === '') assert.equal(out, null);
    else if (!Object.prototype.hasOwnProperty.call(NET_ERROR_HUMAN, code)) assert.equal(out, code);
  }), RUNS);
});

test('a mapped code keeps its own name in brackets', () => {
  fc.assert(fc.property(fc.constantFrom(...Object.keys(NET_ERROR_HUMAN)), (code) => {
    assert.ok(humanizeNetError(code).endsWith(`[${code}]`));
  }));
});

test('humanizeNetError tolerates non-string input', () => {
  fc.assert(fc.property(fc.anything(), (value) => {
    assert.doesNotThrow(() => humanizeNetError(value));
  }), RUNS);
});

test('isPrivilegedUrl never throws, and always reports a boolean', () => {
  const url = fc.oneof(
    fc.string({ unit: 'binary', maxLength: 200 }), fc.webUrl(), fc.constant(undefined), fc.constant(null));
  fc.assert(fc.property(url, (u) => {
    assert.equal(typeof isPrivilegedUrl(u), 'boolean');
  }), RUNS);
});

test('browser-internal schemes are privileged whatever follows them', () => {
  const scheme = fc.constantFrom(
    'chrome', 'edge', 'devtools', 'chrome-extension', 'view-source', 'about', 'data', 'javascript');
  fc.assert(fc.property(scheme, fc.string({ maxLength: 80 }), (s, rest) => {
    assert.equal(isPrivilegedUrl(`${s}:${rest}`), true);
  }), RUNS);
});

test('an ordinary web address on another host is never privileged', () => {
  fc.assert(fc.property(fc.domain(), fc.webPath(), (domain, p) => {
    fc.pre(!/(^|\.)(chromewebstore|chrome)\.google\.com$/.test(domain));
    assert.equal(isPrivilegedUrl(`https://${domain}${p}`), false);
  }), RUNS);
});
