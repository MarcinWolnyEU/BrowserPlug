/*
 * A file:// open/navigate refusal that reports what ACTUALLY happened.
 *
 * The failure being fixed: `tabs(action="open", url="file:///...")` and
 * `navigate(url="file:///...")` both answered with the browser's own bare
 * "Illegal URL". That answers none of the three questions the agent then has:
 *   - did a tab get created anyway? (an earlier session saw one that had, and spent
 *     five calls working around a file that was already open)
 *   - is this a setting I can change? (it reads as though the whole capability is
 *     missing — but READING an already-open file:// tab is a separate, grantable
 *     permission, and page_read/page_snapshot/page_act work once it is on)
 *   - what does work instead?
 *
 * Re-measured on Firefox 153 the tab is NOT created, while that session's older
 * Firefox created one. Both observations are real, so neither may be hard-coded:
 * the refusal QUERIES the tab list and reports the outcome it finds.
 *
 * Sliced out of background.js by anchor and driven with stubs; the twin pair exists
 * because each tree names its own file-access toggle and its own engine reality
 * (Chrome CAN open file:// — so there this path fires only if it genuinely threw).
 * Run with: npm run test:js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const BG_SRC = fs.readFileSync(
  new URL('../../extension-gecko/background.js', import.meta.url), 'utf-8');

const TOGGLE = /about:addons/;          // the gecko path to the local-file switch
const FILE = 'file:///C:/dr/browserplug/tests/fixtures/titled.html';

function loadRefusal() {
  const start = BG_SRC.indexOf('// ---- file:// open/navigate refusal');
  const end = BG_SRC.indexOf('async function handleTabsAction(', start);
  assert.ok(start >= 0 && end > start, 'file:// refusal block not found in background.js');
  const block = BG_SRC.slice(start, end);

  // eslint-disable-next-line no-new-func
  const factory = new Function(`
    let tabs = [];
    let queryThrows = false;
    let fileAccess = { can_read_files: false };
    let accessThrows = false;
    const browser = { tabs: { query: async () => {
      if (queryThrows) throw new Error('tabs.query unavailable');
      return tabs;
    } } };
    async function getFileAccessState() {
      if (accessThrows) throw new Error('isAllowedFileSchemeAccess failed');
      return fileAccess;
    }

    ${block}

    return { fileUrlRefusal, FILE_ACCESS_TOGGLE,
             setTabs: (t) => { tabs = t; },
             breakQuery: () => { queryThrows = true; },
             setAccess: (v) => { fileAccess = v; },
             breakAccess: () => { accessThrows = true; } };
  `);
  return factory();
}

test('a non-file URL is not this branch\'s business — the generic error stands', async () => {
  const m = loadRefusal();
  for (const u of ['https://allegro.pl/', 'about:blank', '', null, undefined, 'notfile:///x']) {
    assert.equal(await m.fileUrlRefusal(u, 'open a tab on', 'boom'), null,
      `${JSON.stringify(u)} must fall through to the ordinary API error`);
  }
});

test('NO tab created: it says so, and names what the user has to do instead', async () => {
  const m = loadRefusal();
  m.setTabs([{ id: 3, url: 'https://example.org/', title: 'Example' }]);
  const msg = await m.fileUrlRefusal(FILE, 'open a tab on', 'Illegal URL: ' + FILE);

  assert.match(msg, /NO tab was created and nothing was navigated/);
  assert.match(msg, /exactly where it was/, 'the targeted tab is unchanged — say it');
  assert.match(msg, /Ctrl\+O/, 'the ONE thing that actually works');
  assert.match(msg, /platform rule, NOT a BrowserPlug setting/,
    'and it must not be confused with the file-access permission');
  assert.ok(msg.includes(FILE), 'names the file it refused');
  assert.match(msg, /Illegal URL/, 'the raw platform words survive, they are the ground truth');
});

test('a tab DID appear: the refusal is called partial and the tab id is handed over', async () => {
  // The five-call workaround seen in the wild: the file had already opened. The message has
  // to end that hunt on the first call.
  const m = loadRefusal();
  m.setTabs([{ id: 489, url: FILE + '?v=1', title: 'BrowserPlug Titled Page' }]);
  const msg = await m.fileUrlRefusal(FILE, 'open a tab on', 'Illegal URL');

  assert.match(msg, /A TAB IS NEVERTHELESS OPEN ON THAT FILE: tab 489/);
  assert.match(msg, /BrowserPlug Titled Page/);
  assert.match(msg, /refusal was PARTIAL/);
  assert.match(msg, /tab_id=489/, 'the next call, spelled out');
  assert.match(msg, /Do not retry and do not work around it/);
  assert.doesNotMatch(msg, /NO tab was created/, 'the two outcomes must never both be claimed');
});

test('the tab match ignores query and hash on BOTH sides', async () => {
  const m = loadRefusal();
  m.setTabs([{ id: 12, url: FILE, title: 'T' }]);
  const msg = await m.fileUrlRefusal(FILE + '#section', 'navigate a tab to', 'Illegal URL');
  assert.match(msg, /tab 12/);
});

test('an unrelated file:// tab is not mistaken for the one we asked for', async () => {
  const m = loadRefusal();
  m.setTabs([{ id: 5, url: 'file:///C:/other/report.html', title: 'Other' }]);
  const msg = await m.fileUrlRefusal(FILE, 'open a tab on', 'Illegal URL');
  assert.match(msg, /NO tab was created/);
});

test('a tabs.query that throws degrades to the no-claim wording, never to a crash', async () => {
  // A probe that cannot answer must not replace a real platform error with a worse one.
  const m = loadRefusal();
  m.breakQuery();
  const msg = await m.fileUrlRefusal(FILE, 'open a tab on', 'Illegal URL');
  assert.match(msg, /NO tab was created/);
  assert.match(msg, /SEPARATE, user-grantable permission/);
});

test('the READ permission is reported separately, in each of its three states', async () => {
  const denied = loadRefusal();
  denied.setAccess({ can_read_files: false });
  const off = await denied.fileUrlRefusal(FILE, 'open a tab on', 'Illegal URL');
  assert.match(off, /it is currently OFF/);
  assert.match(off, TOGGLE, 'names this engine\'s path to the switch');
  assert.match(off, /page_read/, 'and what becomes possible once it is on');

  const ok = loadRefusal();
  ok.setAccess({ can_read_files: true });
  const on = await ok.fileUrlRefusal(FILE, 'open a tab on', 'Illegal URL');
  assert.match(on, /currently GRANTED/);
  assert.match(on, /extension_file_access\.can_read_opened_file_tab/);
  assert.doesNotMatch(on, /currently OFF/);

  const unknown = loadRefusal();
  unknown.breakAccess();
  const dunno = await unknown.fileUrlRefusal(FILE, 'open a tab on', 'Illegal URL');
  assert.match(dunno, /could not be read here/,
    'unknown is an honest answer; claiming either state would be a guess');
  assert.match(dunno, TOGGLE);
});

test('the verb is the caller\'s, so open and navigate read as different actions', async () => {
  const m = loadRefusal();
  assert.match(await m.fileUrlRefusal(FILE, 'open a tab on', 'x'), /refused to open a tab on/);
  assert.match(await m.fileUrlRefusal(FILE, 'navigate a tab to', 'x'), /refused to navigate a tab to/);
});

test('the message never claims the capability is simply absent', async () => {
  // That claim is what cost that session: the agent wrote the feature off.
  const m = loadRefusal();
  const msg = await m.fileUrlRefusal(FILE, 'open a tab on', 'Illegal URL');
  assert.doesNotMatch(msg, /not supported|unsupported|cannot be read|no way to/i);
  assert.match(msg, /SEPARATE, user-grantable permission/);
});
