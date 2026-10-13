'use strict';
/*
 * Property-based fuzzing of the body censor (extension-gecko/body-censor.js).
 *
 * The censor reads captured request and response bodies, which are attacker-shaped text, and
 * hand-rolls a JSON scanner, a multipart splitter and a backtracking-risk check. Each property
 * below must hold for ANY input, so fast-check feeds it hundreds of generated strings per run.
 * It runs locally under `npm run test:js`; a failing case prints its seed and counterexample,
 * and `fc.assert(..., { seed, path })` replays it.
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fc = require('fast-check');

require('../../../extension-gecko/body-censor.js');
const C = globalThis.BrowserplugBodyCensor;

const RUNS = { numRuns: 400 };
const CONTENT_TYPES = [
  'application/json', 'application/x-www-form-urlencoded', 'text/plain', 'text/html',
  'multipart/form-data; boundary=XyZ', 'multipart/form-data', '', undefined,
];
const contentType = fc.constantFrom(...CONTENT_TYPES);
const censoring = C.prepareSettings({ censorSecrets: true });

test('censorText never throws and always returns a string and a count', () => {
  fc.assert(fc.property(fc.string({ unit: 'binary', maxLength: 600 }), contentType, (text, ct) => {
    const out = C.censorText(text, ct, censoring);
    assert.equal(typeof out.content, 'string');
    assert.ok(Number.isInteger(out.count) && out.count >= 0);
  }), RUNS);
});

test('censoring twice changes nothing more than censoring once', () => {
  fc.assert(fc.property(fc.string({ unit: 'binary', maxLength: 600 }), contentType, (text, ct) => {
    const once = C.censorText(text, ct, censoring).content;
    assert.equal(C.censorText(once, ct, censoring).content, once);
  }), RUNS);
});

test('a credential value in a JSON body never survives, whatever it holds', () => {
  const secret = fc.stringMatching(/^[A-Za-z0-9]{8,40}$/).map((s) => `S${s}Z9`);
  const key = fc.constantFrom('password', 'token', 'access_token', 'client_secret', 'api_key', 'Authorization');
  fc.assert(fc.property(key, secret, (k, value) => {
    const body = JSON.stringify({ user: 'someone', [k]: value, nested: { list: [1, 2, 3] } });
    const out = C.censorText(body, 'application/json', censoring);
    assert.ok(!out.content.includes(value), `${k} value leaked`);
    assert.ok(out.count >= 1);
    JSON.parse(out.content); // the masked body is still valid JSON
  }), RUNS);
});

test('a credential in a form body never survives', () => {
  fc.assert(fc.property(fc.stringMatching(/^[A-Za-z0-9]{10,30}$/), (value) => {
    const out = C.censorText(`user=a&password=S${value}&x=1`, 'application/x-www-form-urlencoded', censoring);
    assert.ok(!out.content.includes(`S${value}`));
    assert.ok(out.content.includes('user=a'));
  }), RUNS);
});

test('pattern validation and compilation never throw on arbitrary pattern text', () => {
  fc.assert(fc.property(fc.string({ unit: 'binary', maxLength: 80 }), (src) => {
    const problem = C.patternProblem(src);
    assert.ok(problem === null || problem === undefined || typeof problem === 'string');
    assert.equal(typeof C.hasNestedUnboundedQuantifier(src), 'boolean');
    assert.ok(Array.isArray(C.compilePatterns([src])));
    assert.ok(Array.isArray(C.splitPatternLines(src)));
  }), RUNS);
});

test('every pattern the censor accepts finishes quickly on a hostile subject', () => {
  const subject = `${'a'.repeat(5000)}!`;
  fc.assert(fc.property(fc.string({ maxLength: 40 }), (src) => {
    const regexes = C.compilePatterns([src]);
    const started = Date.now();
    C.censorText(subject, 'text/plain', { censorSecrets: false, regexes, active: regexes.length > 0 });
    assert.ok(Date.now() - started < 1500, `pattern ${JSON.stringify(src)} backtracks`);
  }), { numRuns: 300 });
});

test('prepareSettings tolerates any stored value', () => {
  fc.assert(fc.property(fc.anything(), (raw) => {
    const prepared = C.prepareSettings(raw);
    assert.equal(typeof prepared.active, 'boolean');
    assert.ok(Array.isArray(prepared.regexes));
  }), RUNS);
});
