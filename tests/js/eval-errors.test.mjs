/*
 * page_eval: an error in the caller's own script is a script error, not an injection failure.
 *
 * What went wrong in the field (eval of 2026-10-08): a script with a top-level `return`,
 * or one that threw a TypeError, came back as "content script injection failed after 4
 * attempt(s): return not in function. The tab may have been navigating; retry". The
 * wrapper let the script's throw reject tabs.executeScript, executeScriptWithRetry took
 * that for a failed injection, ran the script again on every attempt (a click inside it
 * would have been repeated four times), and the message sent the agent after a navigation
 * that never happened.
 *
 * The rules this pins:
 *   - the wrapper turns a syntax error, a throw and a rejected promise into an envelope
 *     carrying the error text, named by its class ("SyntaxError: ..."), in both worlds;
 *   - a result executeScript cannot clone stops the retry loop at once, because the
 *     script already ran, and the count in the message is the attempts actually made;
 *   - the content.execute handler reports the envelope's error as SCRIPT_ERROR.
 *
 * Gecko only: Chromium runs code strings through userScripts.execute, which returns a
 * script's own throw as InjectionResult.error without any retry loop.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

function slice(startAnchor, endAnchor) {
  const start = BG_SRC.indexOf(startAnchor);
  const end = BG_SRC.indexOf(endAnchor, start);
  assert.ok(start >= 0 && end > start, `block ${startAnchor} not found in background.js`);
  return BG_SRC.slice(start, end);
}

function loadWrapper() {
  const block = slice('const DOC_ID_EXPR =', '// ---- wait predicates are compiled here');
  // eslint-disable-next-line no-new-func
  return new Function(`${block}\nreturn { evalWrapperCode, NON_CLONABLE_HELP };`)();
}

// Runs a wrapper the way executeScript would: one expression, with a document and window.
function runWrapped(code, window = {}) {
  const document = { readyState: 'complete' };
  // eslint-disable-next-line no-new-func
  return new Function('document', 'window', `return ${code};`)(document, window);
}

test('isolated world: a top-level return is a SyntaxError in the envelope, not a rejection', async () => {
  const { evalWrapperCode } = loadWrapper();
  const out = await runWrapped(evalWrapperCode('return document.title', 'isolated'));
  assert.equal(out.__browserplugEval, 1);
  assert.match(out.error, /^SyntaxError: /, 'named by its class, so the agent sees it is its code');
  assert.equal(out.value, undefined);
});

test('isolated world: a runtime throw and a rejected promise both come back as errors', async () => {
  const { evalWrapperCode } = loadWrapper();
  const thrown = await runWrapped(evalWrapperCode('null.attributes', 'isolated'));
  assert.match(thrown.error, /^TypeError: /);

  const rejected = await runWrapped(evalWrapperCode('Promise.reject(new Error("boom"))', 'isolated'));
  assert.equal(rejected.error, 'Error: boom');

  const plain = await runWrapped(evalWrapperCode('throw "plain words"', 'isolated'));
  assert.equal(plain.error, 'plain words', 'a thrown non-Error is reported as it was thrown');
});

test('isolated world: a script that works still returns its value and the document state', async () => {
  const { evalWrapperCode } = loadWrapper();
  const out = await runWrapped(evalWrapperCode('1 + 1', 'isolated'));
  assert.equal(out.value, 2);
  assert.equal(out.readyState, 'complete');
  assert.equal(out.error, undefined);
  const none = await runWrapped(evalWrapperCode('undefined', 'isolated'));
  assert.equal(none.value, null);
});

test('main world: a syntax error, a throw and a rejection are reported with their class', async () => {
  const { evalWrapperCode } = loadWrapper();
  const page = { wrappedJSObject: { eval: (s) => (0, eval)(s) } };
  const syntax = await runWrapped(evalWrapperCode('return 1', 'main'), page);
  assert.match(syntax.error, /^SyntaxError: /);
  const rejected = await runWrapped(evalWrapperCode('Promise.reject(new TypeError("t"))', 'main'), page);
  assert.equal(rejected.error, 'TypeError: t');
  const ok = await runWrapped(evalWrapperCode('[1, 2]', 'main'), page);
  assert.deepEqual(ok.result, [1, 2]);
});

test('main world: a page CSP refusal keeps its own message and the isolated-world way out', async () => {
  const { evalWrapperCode } = loadWrapper();
  const page = { wrappedJSObject: { eval: () => { throw new EvalError('call to eval() blocked by CSP'); } } };
  const out = await runWrapped(evalWrapperCode('1', 'main'), page);
  assert.match(out.error, /^main-world eval blocked by the page Content-Security-Policy: EvalError: /);
  assert.match(out.error, /world=isolated/);
});

function loadInjector(injectError) {
  const block = slice('// A file:// tab is NOT a privileged page', '// Inject the toolkit');
  // eslint-disable-next-line no-new-func
  return new Function('injectError', `
    const calls = [];
    const browser = { tabs: {
      get: async () => ({ id: 7, url: 'https://example.org/' }),
      executeScript: async () => { calls.push(1); throw injectError; }
    } };
    async function getFileAccessState() { return { can_read_files: true }; }
    ${block}
    return { executeScriptWithRetry, calls };
  `)(injectError);
}

test('a result that cannot be cloned stops the retries: the script already ran once', async () => {
  const m = loadInjector(new Error("Script '<anonymous code>' result is non-structured-clonable data"));
  await assert.rejects(
    () => m.executeScriptWithRetry(7, { code: '1' }, { attempts: 4, delayMs: 0 }),
    (err) => {
      assert.equal(err.code, 'INJECT_FAILED');
      assert.match(err.message, /after 1 attempt\(s\)/, 'the count is the attempts made');
      return true;
    });
  assert.equal(m.calls.length, 1, 'a second attempt would run the script a second time');
});

test('a transient injection failure is still retried, and the count says how often', async () => {
  const m = loadInjector(new Error('No window matching {"matchesHost":[]}'));
  await assert.rejects(
    () => m.executeScriptWithRetry(7, { code: '1' }, { attempts: 3, delayMs: 0 }),
    /after 3 attempt\(s\)/);
  assert.equal(m.calls.length, 3);
});

test('the content.execute handler reports the envelope error and the clone refusal as SCRIPT_ERROR', () => {
  const handler = slice("case 'content.execute':", "case 'content.read':");
  assert.match(handler, /code: evalWrapperCode\(data\.script, data\.world\)/,
    'the handler injects the wrapper this file tests');
  assert.match(handler, /first\.__browserplugEval && *\n? *first\.error !== undefined/);
  assert.match(handler, /sendError\(id, 'SCRIPT_ERROR', String\(first\.error\)\)/);
  assert.ok(handler.includes('if (NON_CLONABLE_RE.test(') &&
            handler.includes("sendError(id, 'SCRIPT_ERROR', NON_CLONABLE_HELP)"),
            'a refused result is a script error, not an injection failure');
  const { NON_CLONABLE_HELP } = loadWrapper();
  assert.match(NON_CLONABLE_HELP, /Return plain data/);
});
