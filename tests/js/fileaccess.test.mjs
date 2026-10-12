/*
 * An injection failure on a file:// tab names the SETTING, not "privileged page".
 *
 * What went wrong in the field: page_eval on an ordinary local HTML file the user had
 * opened came back with the generic injection error, whose own suggested diagnosis —
 * "the tab may be a privileged page" — is simply WRONG for a file:// URL. The agent
 * concluded the capability did not exist, and spent the rest of the session
 * re-implementing the page's logic in a throwaway tab, three times, to work around
 * something that was one per-user toggle away. BrowserPlug already KNEW the answer:
 * getFileAccessState() reports it, /health and the popup show it.
 *
 * The rule this pins: the specific message fires ONLY when the tab really is file://
 * AND file access really is off. Access granted, unknown, or a non-file URL all keep
 * the generic message — otherwise the generic case (a genuinely privileged page, a
 * mid-navigation frame) would start getting a confidently wrong diagnosis of its own.
 *
 * background.js is a top-level extension script, so the block is sliced out by anchor
 * and driven with stubs (as connection/actwait do). Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

function loadInjector() {
  const start = BG_SRC.indexOf('// A file:// tab is NOT a privileged page');
  const end = BG_SRC.indexOf('// Inject the toolkit', start);
  assert.ok(start >= 0 && end > start, 'file-access inject block not found in background.js');
  const block = BG_SRC.slice(start, end);

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    let tabUrl = 'https://example.org/';
    let tabMissing = false;
    let fileAccess = { can_read_files: true, browser_major: 153 };
    let fileAccessThrows = false;
    let injectError = new Error('Missing host permission for the tab');
    let injectResult = null;
    const injectCalls = [];

    const browser = {
      tabs: {
        get: async () => {
          if (tabMissing) throw new Error('No tab with id');
          return { id: 7, url: tabUrl };
        },
        executeScript: async (tabId, details) => {
          injectCalls.push({ tabId, details });
          if (injectResult !== null) return injectResult;
          throw injectError;
        }
      }
    };
    async function getFileAccessState() {
      if (fileAccessThrows) throw new Error('isAllowedFileSchemeAccess failed');
      return fileAccess;
    }

    ${block}

    return {
      fileAccessInjectHint, executeScriptWithRetry,
      setTabUrl: (u) => { tabUrl = u; },
      dropTab: () => { tabMissing = true; },
      setFileAccess: (v) => { fileAccess = v; },
      breakFileAccess: () => { fileAccessThrows = true; },
      setInjectError: (e) => { injectError = e; },
      succeedWith: (r) => { injectResult = r; },
      injectCalls
    };
  `);
  return factory();
}

const DENIED = { can_read_files: false, browser_major: 153 };

test('a file:// tab with access DISABLED gets the setting-specific error', async () => {
  const m = loadInjector();
  m.setTabUrl('file:///C:/dr/browserplug/report.html?x=1');
  m.setFileAccess(DENIED);
  await assert.rejects(
    () => m.executeScriptWithRetry(7, { code: '1' }, { attempts: 1, delayMs: 0 }),
    (err) => {
      assert.equal(err.code, 'FILE_ACCESS_DISABLED', 'a distinct status, not INJECT_FAILED');
      assert.match(err.message, /local file \(file:\/\/\) access is DISABLED/);
      assert.match(err.message, /per-user SETTING, not a missing capability/,
        'the exact wrong conclusion that cost the session');
      assert.match(err.message, /file:\/\/\/C:\/dr\/browserplug\/report\.html/,
        'names the file, without its query string');
      assert.match(err.message, /extension_file_access\.can_read_opened_file_tab/,
        'points at the pre-flight check for next time');
      assert.doesNotMatch(err.message, /may be a privileged page/,
        'the misleading diagnosis must be gone, not merely appended to');
      return true;
    });
});

test('the message says the extension can never OPEN a file:// URL itself', async () => {
  // The other half of the original report asked for navigate() to accept file://.
  // That is structurally impossible on every browser, so the error says so rather
  // than leaving the agent to discover it separately.
  const m = loadInjector();
  m.setTabUrl('file:///tmp/report.html');
  m.setFileAccess(DENIED);
  const hint = await m.fileAccessInjectHint(7);
  assert.match(hint, /never OPEN a file:\/\/ URL itself/);
  assert.match(hint, /opened by hand/);
});

test('a file:// tab with access ENABLED keeps the generic message', async () => {
  const m = loadInjector();
  m.setTabUrl('file:///tmp/report.html');
  m.setFileAccess({ can_read_files: true, browser_major: 153 });
  assert.equal(await m.fileAccessInjectHint(7), null);
  await assert.rejects(
    () => m.executeScriptWithRetry(7, { code: '1' }, { attempts: 1, delayMs: 0 }),
    (err) => {
      assert.equal(err.code, 'INJECT_FAILED');
      assert.match(err.message, /may be a privileged page/,
        'access is on, so the tab really is something else — do not over-claim');
      return true;
    });
});

test('an UNKNOWN file-access state never claims the setting is the cause', async () => {
  const m = loadInjector();
  m.setTabUrl('file:///tmp/report.html');
  m.setFileAccess({ can_read_files: null, browser_major: null });
  assert.equal(await m.fileAccessInjectHint(7), null, 'only an explicit false accuses the toggle');

  const broken = loadInjector();
  broken.setTabUrl('file:///tmp/report.html');
  broken.breakFileAccess();
  assert.equal(await broken.fileAccessInjectHint(7), null, 'a probe failure degrades to generic');
});

test('a normal http(s) tab is untouched even with file access disabled', async () => {
  const m = loadInjector();
  m.setTabUrl('https://allegro.pl/listing');
  m.setFileAccess(DENIED);
  assert.equal(await m.fileAccessInjectHint(7), null);
  await assert.rejects(
    () => m.executeScriptWithRetry(7, { code: '1' }, { attempts: 1, delayMs: 0 }),
    /may be a privileged page/);
});

test('a privileged (moz-extension/about) tab is untouched', async () => {
  const m = loadInjector();
  m.setTabUrl('moz-extension://abcd/options.html');
  m.setFileAccess(DENIED);
  assert.equal(await m.fileAccessInjectHint(7), null);
});

test('a tab that vanished mid-diagnosis degrades to the generic message', async () => {
  const m = loadInjector();
  m.dropTab();
  assert.equal(await m.fileAccessInjectHint(7), null);
});

test('a successful injection never runs the file-access probe at all', async () => {
  const m = loadInjector();
  m.setTabUrl('file:///tmp/report.html');
  m.setFileAccess(DENIED);
  m.succeedWith(['ok']);
  const out = await m.executeScriptWithRetry(7, { code: '1' });
  assert.deepEqual(JSON.parse(JSON.stringify(out)), ['ok']);
  assert.equal(m.injectCalls.length, 1, 'the happy path is unchanged and pays nothing');
});

test('transient failures still retry; the diagnosis happens once, at the end', async () => {
  const m = loadInjector();
  m.setTabUrl('file:///tmp/report.html');
  m.setFileAccess(DENIED);
  m.setInjectError(new Error('No window matching {"matchesHost":[]}'));  // transient, retryable
  await assert.rejects(
    () => m.executeScriptWithRetry(7, { code: '1' }, { attempts: 3, delayMs: 0 }),
    /local file \(file:\/\/\) access is DISABLED/);
  assert.equal(m.injectCalls.length, 3, 'retries are not short-circuited by the new branch');
});


test('a refused host permission says the frame is closed to the extension and that a retry cannot help', async () => {
  const m = loadInjector();
  m.setInjectError(new Error('Missing host permission for the tab or frames'));
  await assert.rejects(
    () => m.executeScriptWithRetry(7, { code: '1' }, { attempts: 3, delayMs: 0 }),
    (err) => {
      assert.equal(err.code, 'INJECT_FAILED', 'the code is unchanged');
      assert.match(err.message, /after 1 attempt\(s\): Missing host permission for the tab or frames\./,
        'a permanent refusal is not retried');
      assert.match(err.message, /not allowed into that frame/);
      assert.match(err.message, /retrying cannot succeed/);
      assert.match(err.message, /another frame_id .* or the top document \(omit frame_id\)/);
      assert.doesNotMatch(err.message, /navigating|; retry;/, 'no invitation to retry');
      return true;
    });
  assert.equal(m.injectCalls.length, 1);
});

test('any other injection failure keeps the navigation wording', async () => {
  const m = loadInjector();
  m.setInjectError(new Error('The tab was closed.'));
  await assert.rejects(
    () => m.executeScriptWithRetry(7, { code: '1' }, { attempts: 1, delayMs: 0 }),
    /may have been navigating; retry/);
});
