/*
 * CHROMIUM TWIN of tests/js/fileaccess.test.mjs — the file:// injection diagnosis in
 * extension-chromium/background.js.
 *
 * Same failure, same rule: an injection failure on an ordinary local HTML file must
 * name the per-user local-file toggle instead of the generic "the tab may be a
 * privileged page", which is confidently wrong for a file:// URL and led to the
 * capability being written off entirely. The specific message fires ONLY when the tab
 * really is file:// AND access really is off; granted / unknown / non-file all keep
 * the generic message.
 *
 * The twin is NOT a copy-paste: MV3 injects through scripting.executeScript, so
 * executeScriptWithRetry takes a whole injection object (the tab id lives at
 * injection.target.tabId, and it can be absent) and its hard-failure vocabulary is
 * Chromium's. Those differences are exactly what this file exists to cover; the UI
 * path named in the message is chrome://extensions, not about:addons.
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-chromium/background.js', import.meta.url), 'utf-8');

function loadInjector() {
  const start = BG_SRC.indexOf('// A file:// tab is NOT a privileged page');
  const end = BG_SRC.indexOf('// Inject the toolkit', start);
  assert.ok(start >= 0 && end > start,
    'file-access inject block not found in the chromium background.js');
  const block = BG_SRC.slice(start, end);

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    let tabUrl = 'https://example.org/';
    let tabMissing = false;
    let fileAccess = { can_read_files: true, browser_major: 141 };
    let fileAccessThrows = false;
    let injectError = new Error('Cannot access contents of the page');
    let injectResult = null;
    const injectCalls = [];

    const browser = {
      tabs: {
        get: async () => {
          if (tabMissing) throw new Error('No tab with id');
          return { id: 7, url: tabUrl };
        }
      },
      scripting: {
        executeScript: async (injection) => {
          injectCalls.push(injection);
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

const DENIED = { can_read_files: false, browser_major: 141 };
const INJ = { target: { tabId: 7 }, world: 'ISOLATED', files: ['automation.js'] };

test('a file:// tab with access DISABLED gets the setting-specific error', async () => {
  const m = loadInjector();
  m.setTabUrl('file:///C:/dr/browserplug/report.html?x=1');
  m.setFileAccess(DENIED);
  await assert.rejects(
    () => m.executeScriptWithRetry(INJ, { attempts: 1, delayMs: 0 }),
    (err) => {
      assert.equal(err.code, 'FILE_ACCESS_DISABLED', 'a distinct status, not INJECT_FAILED');
      assert.match(err.message, /local file \(file:\/\/\) access is DISABLED/);
      assert.match(err.message, /per-user SETTING, not a missing capability/);
      assert.match(err.message, /chrome:\/\/extensions/, 'the CHROMIUM path to the toggle');
      assert.match(err.message, /Allow access to file URLs/, 'the exact control to flip');
      assert.match(err.message, /extension_file_access\.can_read_opened_file_tab/);
      assert.doesNotMatch(err.message, /may be a privileged page/);
      return true;
    });
});

test('the message does NOT repeat gecko\'s "can never OPEN a file:// URL"', async () => {
  // That sentence is true on Firefox and FALSE here — measured 2026-08-03,
  // tabs(action="open") on a file:// URL succeeded on Chrome Canary 150 and page_read
  // then read the page. Shipping gecko's wording on Blink tells an agent a capability
  // it HAS is missing, which is the exact harm the file-access message exists to undo.
  const m = loadInjector();
  m.setTabUrl('file:///tmp/report.html');
  m.setFileAccess(DENIED);
  const msg = await m.fileAccessInjectHint(7);
  assert.match(msg, /Chrome DOES let BrowserPlug OPEN a file:\/\/ URL/);
  assert.match(msg, /only READING the page is gated/);
  assert.doesNotMatch(msg, /never OPEN/);
});

test('the tab id is read out of the injection target, not passed separately', async () => {
  // The MV3 signature difference: gecko takes (tabId, details); this takes the whole
  // scripting.executeScript argument. An injection with no target must not throw.
  const m = loadInjector();
  m.setTabUrl('file:///tmp/report.html');
  m.setFileAccess(DENIED);
  assert.equal(await m.fileAccessInjectHint(undefined), null, 'no tab id => no claim');
  await assert.rejects(
    () => m.executeScriptWithRetry({ world: 'ISOLATED', files: ['automation.js'] },
      { attempts: 1, delayMs: 0 }),
    /may be a privileged page/);
  await assert.rejects(
    () => m.executeScriptWithRetry(INJ, { attempts: 1, delayMs: 0 }),
    /local file \(file:\/\/\) access is DISABLED/);
});

test('a file:// tab with access ENABLED keeps the generic message', async () => {
  const m = loadInjector();
  m.setTabUrl('file:///tmp/report.html');
  m.setFileAccess({ can_read_files: true, browser_major: 141 });
  assert.equal(await m.fileAccessInjectHint(7), null);
  await assert.rejects(
    () => m.executeScriptWithRetry(INJ, { attempts: 1, delayMs: 0 }),
    (err) => {
      assert.equal(err.code, 'INJECT_FAILED');
      assert.match(err.message, /may be a privileged page/);
      return true;
    });
});

test('an UNKNOWN file-access state never claims the setting is the cause', async () => {
  const m = loadInjector();
  m.setTabUrl('file:///tmp/report.html');
  m.setFileAccess({ can_read_files: null, browser_major: null });
  assert.equal(await m.fileAccessInjectHint(7), null);

  const broken = loadInjector();
  broken.setTabUrl('file:///tmp/report.html');
  broken.breakFileAccess();
  assert.equal(await broken.fileAccessInjectHint(7), null);
});

test('a normal http(s) tab is untouched even with file access disabled', async () => {
  const m = loadInjector();
  m.setTabUrl('https://allegro.pl/listing');
  m.setFileAccess(DENIED);
  assert.equal(await m.fileAccessInjectHint(7), null);
  await assert.rejects(
    () => m.executeScriptWithRetry(INJ, { attempts: 1, delayMs: 0 }),
    /may be a privileged page/);
});

test('a chrome:// page is untouched', async () => {
  const m = loadInjector();
  m.setTabUrl('chrome://newtab/');
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
  m.succeedWith([{ result: 'ok' }]);
  const out = await m.executeScriptWithRetry(INJ);
  assert.equal(JSON.parse(JSON.stringify(out))[0].result, 'ok');
  assert.equal(m.injectCalls.length, 1, 'the happy path is unchanged and pays nothing');
});

test('transient failures still retry; the diagnosis happens once, at the end', async () => {
  const m = loadInjector();
  m.setTabUrl('file:///tmp/report.html');
  m.setFileAccess(DENIED);
  m.setInjectError(new Error('The tab was closed.'));   // not in the hard-failure list
  await assert.rejects(
    () => m.executeScriptWithRetry(INJ, { attempts: 3, delayMs: 0 }),
    /local file \(file:\/\/\) access is DISABLED/);
  assert.equal(m.injectCalls.length, 3, 'retries are not short-circuited by the new branch');
});


test('a refused host permission says the frame is closed to the extension and that a retry cannot help', async () => {
  const m = loadInjector();
  m.setInjectError(new Error('Missing host permission for the tab or frames'));
  await assert.rejects(
    () => m.executeScriptWithRetry(INJ, { attempts: 3, delayMs: 0 }),
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
    () => m.executeScriptWithRetry(INJ, { attempts: 1, delayMs: 0 }),
    /may have been navigating; retry/);
});
