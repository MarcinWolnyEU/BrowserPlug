/*
 * Unit tests for extension-gecko/body-censor.js, the shared module behind Options >
 * General > "Captured bodies" (censor secrets toggle + custom regular expressions).
 *
 * Bodies come back RAW from requests(action="get") unless the user opts in, so the
 * first group below pins that "nothing configured" changes nothing. The rest pins the
 * built-in key masking per body shape, the custom-pattern rules (validation, the
 * catastrophic-backtracking heuristic, limits) and the result marker.
 *
 * The glue in background.js is tested in censor-glue.test.mjs; the options page in
 * options.test.mjs. Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import '../../extension-gecko/body-censor.js';

const C = globalThis.BrowserplugBodyCensor;
const R = '<redacted>';

const on = (extra = {}) => C.prepareSettings({ censorSecrets: true, ...extra });
// The patterns alone, without the built-in masking: what the compiled list does by itself.
const patterns = (list) => {
  const regexes = C.compilePatterns(list);
  return { censorSecrets: false, regexes, active: regexes.length > 0 };
};
const builtin = (text, ct) => C.censorText(text, ct, on());

// ---- registration and defaults ------------------------------------------------------

test('registers on globalThis with raw-by-default settings', () => {
  assert.ok(C, 'BrowserplugBodyCensor is not registered');
  assert.equal(C.SETTINGS_DEFAULTS.censorSecrets, false);
  assert.deepEqual([...C.SETTINGS_DEFAULTS.censorPatterns], []);
  assert.equal('censorPatternsIgnoreCase' in C.SETTINGS_DEFAULTS, false, 'case is the pattern\'s business');
  assert.equal(C.MAX_PATTERNS, 20);
  assert.equal(C.MAX_PATTERN_LENGTH, 200);
});

test('nothing configured is inactive and leaves a result untouched', () => {
  for (const raw of [undefined, null, {}, C.SETTINGS_DEFAULTS, { censorSecrets: 'yes' },
                     { censorPatterns: 'not-a-list' }, { censorPatterns: [42, null, ''] }]) {
    assert.equal(C.prepareSettings(raw).active, false, JSON.stringify(raw));
  }
  const result = { request_body: { included: true, content: '{"password":"x"}',
                                   content_type: 'application/json', encoding: 'utf-8' } };
  const before = JSON.stringify(result);
  assert.equal(C.censorResult(result, C.prepareSettings({})), 0);
  assert.equal(JSON.stringify(result), before);
});

test('custom patterns belong to the toggle: stored ones are ignored while it is off', () => {
  const list = ['tok-\\d'];
  const off = C.prepareSettings({ censorSecrets: false, censorPatterns: list });
  assert.equal(off.active, false);
  assert.equal(off.regexes.length, 0);
  assert.equal(C.censorText('tok-1', 'text/plain', off).content, 'tok-1');
  const onS = C.prepareSettings({ censorSecrets: true, censorPatterns: list });
  assert.equal(onS.regexes.length, 1);
  assert.equal(C.censorText('tok-1', 'text/plain', onS).content, R);
});

test('toggle off + no valid pattern = the body text is returned as is', () => {
  const s = C.prepareSettings({ censorSecrets: false, censorPatterns: ['[', ''] });
  assert.equal(s.active, false);
  const r = C.censorText('{"password":"x"}', 'application/json', s);
  assert.deepEqual(r, { content: '{"password":"x"}', count: 0 });
});

// ---- which keys are credentials -------------------------------------------------------

test('credential keys: the named ones, in every common spelling', () => {
  for (const key of [
    'password', 'Password', 'PASSWORD', 'passwd', 'pwd', 'user_password', 'confirmPassword',
    'secret', 'client_secret', 'clientSecret', 'token', 'access_token', 'accessToken',
    'refresh-token', 'refresh_token', 'id_token', 'idToken', 'csrf_token', 'x-auth-token',
    'api_key', 'apiKey', 'apikey', 'api-key', 'authorization', 'Authorization', 'cookie',
    'set-cookie', 'session', 'session_id', 'sessionId', 'otp', 'code_verifier', 'codeVerifier',
    'private_key', 'privateKey', 'secret_key', 'access_key', 'user[password]', 'X-Api-Key',
  ]) assert.equal(C.isCredentialKey(key), true, key);
});

test('credential keys: ordinary names are not', () => {
  for (const key of [
    'username', 'user', 'email', 'name', 'author', 'authority', 'description', 'tokens_per_page_x'.replace('tokens', 'items'),
    'code', 'expires_in', 'token_type_label'.replace('token', 'kind'), 'passenger', 'compass',
    'keyboard', 'monkey', 'id', 'sessions'.replace('sessions', 'sess'),
  ]) assert.equal(C.isCredentialKey(key), false, key);
});

// ---- JSON -----------------------------------------------------------------------------

test('json: masks the values of credential keys, keeps everything else byte for byte', () => {
  const body = '{\n  "user": "bob",\n  "password": "hunter2",\n  "big": 12345678901234567890,\n  "n": 1.50e3\n}';
  const r = builtin(body, 'application/json');
  assert.equal(r.count, 1);
  assert.equal(r.content,
    '{\n  "user": "bob",\n  "password": "<redacted>",\n  "big": 12345678901234567890,\n  "n": 1.50e3\n}');
});

test('json: nested objects and arrays of objects are walked', () => {
  const body = JSON.stringify({
    data: { login: { email: 'a@b.c', accessToken: 'AAA', profile: { name: 'x' } } },
    items: [{ id: 1, api_key: 'k1' }, { id: 2, api_key: 'k2' }, { id: 3 }],
  });
  const r = builtin(body, 'application/json');
  assert.equal(r.count, 3);
  const out = JSON.parse(r.content);
  assert.equal(out.data.login.accessToken, R);
  assert.equal(out.data.login.email, 'a@b.c');
  assert.equal(out.data.login.profile.name, 'x');
  assert.deepEqual(out.items.map((i) => i.api_key), [R, R, undefined]);
});

test('json: a container under a credential key has every leaf masked, structure kept', () => {
  const body = '{"cookies":{"a":"1","b":["x",2,true,null,""]},"other":"ok"}';
  const r = builtin(body, 'application/json');
  assert.deepEqual(JSON.parse(r.content),
    { cookies: { a: R, b: [R, R, true, null, ''] }, other: 'ok' });
  assert.equal(r.count, 3);
});

test('json: numbers under a credential key are masked, booleans/null/empty are not', () => {
  const r = builtin('{"otp":123456,"password":"","secret":null,"token":false}', 'application/json');
  assert.equal(r.content, '{"otp":"<redacted>","password":"","secret":null,"token":false}');
  assert.equal(r.count, 1);
});

test('json: escaped and unicode keys and values are handled', () => {
  const body = '{"pass\\u0077ord":"a\\"b\\\\c","note":"password is in \\"password\\":\\"x\\""}';
  const r = builtin(body, 'application/json');
  assert.equal(r.count, 1);
  const out = JSON.parse(r.content);
  assert.equal(out['password'], R);
  // A credential-looking string INSIDE a value is data, not a key.
  assert.equal(out.note, 'password is in "password":"x"');
});

test('json: a top-level array and a bare string parse too', () => {
  assert.equal(builtin('[{"token":"t"}]', 'application/json').content, '[{"token":"<redacted>"}]');
  assert.deepEqual(builtin('"just a string"', 'application/json'),
    { content: '"just a string"', count: 0 });
});

test('json: sniffed from the body when the content type is missing or wrong', () => {
  assert.equal(builtin('{"password":"p"}', undefined).content, '{"password":"<redacted>"}');
  assert.equal(builtin('{"password":"p"}', 'text/plain').content, '{"password":"<redacted>"}');
});

test('json: a body cut off at max_body_size still gets its secrets masked (text fallback)', () => {
  const cut = '{"user":"bob","password":"hunter2","note":"this got trunc';
  const r = builtin(cut, 'application/json');
  assert.equal(r.count, 1);
  assert.equal(r.content, '{"user":"bob","password":"<redacted>","note":"this got trunc');
  const cutInsideSecret = builtin('{"a":1,"access_token":"eyJhbGciOi', 'application/json');
  assert.equal(cutInsideSecret.content, '{"a":1,"access_token":"<redacted>"');
  assert.equal(cutInsideSecret.count, 1);
});

test('json: nesting deeper than the scanner allows falls back to text, not a crash', () => {
  const deep = '['.repeat(400) + '{"password":"p"}' + ']'.repeat(400);
  const r = builtin(deep, 'application/json');
  assert.equal(r.count, 1);
  assert.ok(r.content.includes('"password":"<redacted>"'));
});

test('json: gecko reports a form body as JSON formData whatever the declared type', () => {
  const formData = '{"user":["bob"],"password":["hunter2"],"csrf_token":["c1","c2"]}';
  const r = builtin(formData, 'application/x-www-form-urlencoded');
  assert.deepEqual(JSON.parse(r.content),
    { user: ['bob'], password: [R], csrf_token: [R, R] });
  assert.equal(r.count, 3);
});

test('json: a large body is scanned in linear time', () => {
  const items = Array.from({ length: 20000 }, (_, i) => ({ id: i, name: 'n' + i, password: 'p' + i }));
  const body = JSON.stringify({ items });
  assert.ok(body.length > 500000);
  const t0 = Date.now();
  const r = builtin(body, 'application/json');
  assert.equal(r.count, 20000);
  assert.ok(Date.now() - t0 < 2000, 'json scan is too slow');
});

// ---- form-urlencoded --------------------------------------------------------------------

test('form: masks credential fields, leaves the rest, decodes keys', () => {
  const r = builtin('a=1&password=hunter2&user%5Bsecret%5D=q&Access+Token=z&b=2&empty=&pwd=',
    'application/x-www-form-urlencoded; charset=UTF-8');
  assert.equal(r.content, 'a=1&password=<redacted>&user%5Bsecret%5D=<redacted>&Access+Token=<redacted>&b=2&empty=&pwd=');
  assert.equal(r.count, 3);
});

test('form: a malformed escape does not throw', () => {
  const r = builtin('x%zz=1&password=2', 'application/x-www-form-urlencoded');
  assert.equal(r.content, 'x%zz=1&password=<redacted>');
});

test('form: with no content type the text scan still catches key=value pairs', () => {
  const r = builtin('grant_type=password&client_secret=abc&code_verifier=xyz', undefined);
  assert.equal(r.content, 'grant_type=password&client_secret=<redacted>&code_verifier=<redacted>');
  assert.equal(r.count, 2);
});

// ---- multipart ----------------------------------------------------------------------------

const part = (name, value, extra = '') =>
  `--XX\r\nContent-Disposition: form-data; name="${name}"${extra}\r\n\r\n${value}\r\n`;

test('multipart: masks credential text fields, leaves other fields and files alone', () => {
  const body = part('user', 'bob') + part('password', 'hunter2') +
    part('upload', 'token=keep-me', '; filename="a.txt"') + part('note', 'hi') + '--XX--\r\n';
  const r = builtin(body, 'multipart/form-data; boundary=XX');
  assert.equal(r.count, 1);
  assert.equal(r.content, part('user', 'bob') + part('password', R) +
    part('upload', 'token=keep-me', '; filename="a.txt"') + part('note', 'hi') + '--XX--\r\n');
});

test('multipart: LF-only line endings, a quoted boundary and no declared boundary', () => {
  const body = '--B1\nContent-Disposition: form-data; name="api_key"\n\nk-1\n--B1--\n';
  assert.equal(builtin(body, 'multipart/form-data; boundary="B1"').content,
    '--B1\nContent-Disposition: form-data; name="api_key"\n\n<redacted>\n--B1--\n');
  assert.equal(builtin(body, 'multipart/form-data').content,
    '--B1\nContent-Disposition: form-data; name="api_key"\n\n<redacted>\n--B1--\n');
});

test('multipart: a truncated final part is still masked', () => {
  const body = part('user', 'bob') + '--XX\r\nContent-Disposition: form-data; name="secret"\r\n\r\nabc';
  const r = builtin(body, 'multipart/form-data; boundary=XX');
  assert.equal(r.count, 1);
  assert.ok(r.content.endsWith('name="secret"\r\n\r\n<redacted>'));
});

test('multipart: no credential fields returns the text unchanged', () => {
  const body = part('user', 'bob') + '--XX--\r\n';
  assert.deepEqual(builtin(body, 'multipart/form-data; boundary=XX'), { content: body, count: 0 });
});

// ---- text fallback ---------------------------------------------------------------------------

test('text: key: value and key=value shapes in free text and html', () => {
  const r = builtin(
    'Authorization: Bearer abc.def\nclient_secret="s3cr3t" id_token: \'x\' <input name=password value=1> password=hunter2',
    'text/plain');
  assert.equal(r.count, 4, r.content);
  assert.ok(r.content.startsWith('Authorization: <redacted>\n'), r.content);
  assert.ok(!/s3cr3t|hunter2|abc\.def/.test(r.content), r.content);
  assert.ok(r.content.includes('client_secret="<redacted>"'));
  assert.ok(r.content.includes("id_token: '<redacted>'"));
});

test('text: header-style lines are masked to the end of the line', () => {
  const r = builtin('GET / HTTP/1.1\r\nCookie: a=1; sid=2\r\nHost: x\r\nSet-Cookie: k=v; Path=/\r\n', 'text/plain');
  assert.equal(r.content, 'GET / HTTP/1.1\r\nCookie: <redacted>\r\nHost: x\r\nSet-Cookie: <redacted>\r\n');
  assert.equal(r.count, 2);
});

test('text: ordinary prose and already-masked values are left alone', () => {
  const same = 'the password field is required; token counts: 5 and "secret": "<redacted>"';
  const r = builtin(same, 'text/plain');
  assert.equal(r.content.includes('"secret": "<redacted>"'), true);
  assert.equal(r.content.includes('required'), true);
  assert.equal(builtin('"secret": "<redacted>"', 'text/plain').count, 0);
});

test('text: hostile input stays fast', () => {
  const t0 = Date.now();
  builtin('"'.repeat(1_000_000), 'text/plain');
  builtin('a'.repeat(1_000_000), 'text/plain');
  builtin('password'.repeat(120_000), 'text/plain');
  builtin('"password":"' + 'x'.repeat(1_000_000), 'text/plain');
  assert.ok(Date.now() - t0 < 4000, 'text scan is too slow');
});

// ---- custom patterns ----------------------------------------------------------------------------

test('patterns: each match is replaced, and counted, in any body', () => {
  const r = C.censorText('key sk-abcdefghij1234 and sk-zzzzzzzzzz9999 end', 'text/plain',
    patterns(['sk-[a-z0-9]{10,}']));
  assert.equal(r.content, 'key <redacted> and <redacted> end');
  assert.equal(r.count, 2);
});

test('patterns: apply with the toggle off, and in addition to it when on', () => {
  const body = '{"password":"p","card":"4111111111111111"}';
  const only = C.censorText(body, 'application/json', patterns(['\\b\\d{16}\\b']));
  assert.equal(only.content, '{"password":"p","card":"<redacted>"}');
  const both = C.censorText(body, 'application/json',
    C.prepareSettings({ censorSecrets: true, censorPatterns: ['\\b\\d{16}\\b'] }));
  assert.equal(both.content, '{"password":"<redacted>","card":"<redacted>"}');
  assert.equal(both.count, 2);
});

test('patterns: several patterns, one per entry, all applied', () => {
  const r = C.censorText('alpha beta gamma', 'text/plain', patterns(['alpha', 'gamma']));
  assert.equal(r.content, '<redacted> beta <redacted>');
});

test('patterns: case-sensitive, and (?i:...) in the pattern itself opts out', () => {
  assert.equal(C.censorText('Secret secret SECRET', 'text/plain', patterns(['secret'])).content,
    'Secret <redacted> SECRET');
  assert.equal(C.censorText('Secret secret SECRET', 'text/plain', patterns(['(?i:secret)'])).content,
    '<redacted> <redacted> <redacted>');
});

test('patterns: an empty match is never replaced, so a lenient pattern cannot loop or count', () => {
  const r = C.censorText('abc', 'text/plain', patterns(['x*']));
  assert.deepEqual(r, { content: 'abc', count: 0 });
});

test('patterns: invalid patterns are flagged with a reason and ignored, never thrown', () => {
  const a = C.analyzePatterns(['[', '(', 'a{2,1}', '*a', 'fine']);
  assert.deepEqual(a.map((x) => x.ok), [false, false, false, false, true]);
  for (const x of a.slice(0, 4)) assert.match(x.reason, /invalid regular expression/);
  const s = patterns(['[', 'fine']);
  assert.equal(s.regexes.length, 1);
  assert.equal(C.censorText('a fine day', 'text/plain', s).content, 'a <redacted> day');
});

test('patterns: blank lines and non-strings are skipped, entries are trimmed', () => {
  assert.deepEqual(C.analyzePatterns(['  a  ', '', '   ', 5, null, 'b']).map((x) => x.pattern), ['a', 'b']);
  assert.deepEqual(C.splitPatternLines('a\r\n\n  b  \n\r\n'), ['a', 'b']);
  assert.deepEqual(C.splitPatternLines(undefined), []);
});

test('patterns: over 200 characters is rejected and not storable', () => {
  const long = 'a'.repeat(201);
  const [a] = C.analyzePatterns([long]);
  assert.equal(a.ok, false);
  assert.equal(a.storable, false);
  assert.match(a.reason, /too long \(201 characters, the limit is 200\)/);
  const [b] = C.analyzePatterns(['a'.repeat(200)]);
  assert.equal(b.ok, true);
  assert.equal(b.storable, true);
});

test('patterns: only 20 apply; the 21st is flagged and not storable', () => {
  const list = Array.from({ length: 25 }, (_, i) => `p${i}x`);
  const a = C.analyzePatterns(list);
  assert.equal(a.filter((x) => x.ok).length, 20);
  assert.equal(a[19].ok, true);
  assert.equal(a[20].ok, false);
  assert.equal(a[20].storable, false);
  assert.match(a[20].reason, /over the limit of 20 patterns/);
  assert.equal(patterns(list).regexes.length, 20);
  // An over-long entry does not use up one of the 20 slots, since it is not saved.
  const mixed = ['b'.repeat(300), ...Array.from({ length: 20 }, (_, i) => `q${i}y`)];
  assert.equal(C.analyzePatterns(mixed).filter((x) => x.storable).length, 20);
});

test('patterns: worst-case stored size fits storage.sync\'s 8 KB per-item quota', () => {
  const worst = Array.from({ length: 20 }, () => '\\'.repeat(200));
  const stored = C.analyzePatterns(worst).filter((x) => x.storable).map((x) => x.pattern);
  assert.ok(JSON.stringify(stored).length + 'censorPatterns'.length < 8192);
});

// ---- catastrophic backtracking -------------------------------------------------------------------

test('backtracking heuristic: nested unbounded quantifiers are rejected', () => {
  for (const src of ['(a+)+', '(a+)+$', '(a*)*', '(a+)*', '(.*)*', '(.*)+x', '([a-z]+)*', '(\\w+\\s*)+',
    '(?:a+)+', '(?:\\d+,?)*', '(x+x+)+y', '((a+)b)*', '(a+){2,}', '(?<n>\\d+)+', '(a|b+)*', '^(\\w+\\.)*\\w+$',
    '(\\d+\\.?)+',
    // A bounded outer count still repeats the group: every split into up to N runs is tried.
    '(a+){1,50}', '(\\w+\\s?){2,99}', '([a-z0-9]+){1,64}x', '(a+){2}', '((a+)b){0,9}']) {
    assert.equal(C.hasNestedUnboundedQuantifier(src), true, src);
    const [a] = C.analyzePatterns([src]);
    assert.equal(a.ok, false, src);
    assert.match(a.reason, /can hang the browser/, src);
  }
});

test('backtracking heuristic: ordinary patterns pass', () => {
  for (const src of ['a+b+', '\\d+\\.\\d+', '(\\d{1,3}\\.){3}\\d{1,3}', '(foo|bar)+', '(?:ab)*c', '[a-z]+@[a-z]+\\.com',
    'sk-[A-Za-z0-9]{20,}', 'eyJ[\\w-]+\\.[\\w-]+\\.[\\w-]+', '(\\w{3})+', '(a+)b', '(a{2,5})+', 'x(?=y+)z*',
    '[(+*]+', '\\(a+\\)+', '(?<year>\\d{4})-(?<m>\\d{2})', '(?<=\\$)\\d+(?:\\.\\d\\d)?',
    // A count that cannot repeat the group (at most once) is not nesting.
    '(a+){1}', '(a+){0,1}', '(a+)?', '((ab){2})+', 'x{2,5}(y{3})+']) {
    assert.equal(C.hasNestedUnboundedQuantifier(src), false, src);
    assert.equal(C.analyzePatterns([src])[0].ok, true, src);
  }
});

test('backtracking heuristic: a rejected pattern is never compiled or run', () => {
  const s = patterns(['(a+)+$']);
  assert.equal(s.regexes.length, 0);
  assert.equal(s.active, false);
  const t0 = Date.now();
  // The classic attack string: with the pattern accepted this would not return.
  const r = C.censorText('a'.repeat(40) + '!', 'text/plain', s);
  assert.equal(r.count, 0);
  assert.ok(Date.now() - t0 < 500);
});

// ---- censorResult: the marker -----------------------------------------------------------------------

function resultOf(req, res) {
  return {
    request_id: 'r1',
    request_body: { included: true, content: req, content_type: 'application/json',
                    encoding: 'utf-8', size_bytes: req.length, truncated: false, note: 'Captured.' },
    response_body: { included: true, content: res, content_type: 'application/json',
                     encoding: 'utf-8', size_bytes: res.length, truncated: false },
  };
}

test('result: masks both bodies and adds the visible marker with the total', () => {
  const result = resultOf('{"password":"p","u":"x"}', '{"access_token":"a","refresh_token":"b"}');
  const total = C.censorResult(result, on());
  assert.equal(total, 3);
  assert.equal(result.values_censored, 3);
  assert.equal(result.values_censored_note, '3 value(s) censored by your Options > General setting');
  assert.equal(result.request_body.content, '{"password":"<redacted>","u":"x"}');
  assert.equal(result.request_body.censored_values, 1);
  assert.equal(result.request_body.note, 'Captured. 1 value(s) censored by your Options > General setting.');
  assert.equal(result.response_body.censored_values, 2);
  assert.equal(result.response_body.note, '2 value(s) censored by your Options > General setting.');
});

test('result: no marker when nothing matched', () => {
  const result = resultOf('{"u":"x"}', '{"ok":true}');
  assert.equal(C.censorResult(result, on()), 0);
  assert.equal('values_censored' in result, false);
  assert.equal('values_censored_note' in result, false);
  assert.equal('censored_values' in result.request_body, false);
  assert.equal(result.request_body.note, 'Captured.');
});

test('result: custom pattern matches count toward the marker', () => {
  const result = resultOf('token sk-abcdefghijkl', 'sk-abcdefghijkl again sk-mnopqrstuvwx');
  assert.equal(C.censorResult(result, patterns(['sk-[a-z]{12}'])), 3);
  assert.equal(result.values_censored_note, '3 value(s) censored by your Options > General setting');
});

test('result: bodies that were not returned are left alone', () => {
  const result = {
    request_body: { included: false, content: null, note: 'No request body was sent' },
    response_body: { included: false, content: null, encoding: 'base64', note: 'Binary' },
  };
  assert.equal(C.censorResult(result, on({ censorPatterns: ['.'] })), 0);
  assert.equal(result.request_body.content, null);
});

test('result: a base64 body is not decoded, and says it was not censored', () => {
  const result = {
    response_body: { included: true, content: 'eyJwYXNzd29yZCI6InAifQ==', encoding: 'base64', note: 'Binary.' },
  };
  assert.equal(C.censorResult(result, on()), 0);
  assert.equal(result.response_body.content, 'eyJwYXNzd29yZCI6InAifQ==');
  assert.equal(result.response_body.note, 'Binary. Binary body: not censored.');
});

test('result: if censoring itself throws, the body is withheld, not returned raw', () => {
  const boom = { [Symbol.replace]() { throw new Error('boom'); } };
  const result = resultOf('{"password":"p"}', '{"u":1}');
  const total = C.censorResult(result, { censorSecrets: false, regexes: [boom], active: true });
  assert.equal(total, 2);
  assert.equal(result.request_body.content, R);
  assert.equal(result.response_body.content, R);
  assert.match(result.request_body.note, /Body withheld: censoring failed/);
  assert.equal(result.values_censored, 2);
});

test('result: content-type of the body drives the shape, not the request', () => {
  const result = {
    response_body: { included: true, content: 'password=p&x=1', content_type: 'application/x-www-form-urlencoded',
                     encoding: 'utf-8' },
  };
  C.censorResult(result, on());
  assert.equal(result.response_body.content, 'password=<redacted>&x=1');
});
