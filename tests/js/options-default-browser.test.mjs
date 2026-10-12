// Unit tests for Options ▸ General ▸ Default browser against the REAL options.html
// (jsdom, no Firefox). The setting lives in the SERVER's config file, so the page only
// ever renders what the server sent and writes back through the same relay. What these
// guard:
//   * the list is First started, then this browser, then the other connected ones,
//     each named without its process id, and a browser that was picked and has since
//     been closed STAYS on it, labelled;
//   * the launch checkbox appears only once a real browser is picked, and the startup
//     arguments only once that checkbox is on;
//   * a browser the server cannot start (remote, or no known program) disables the
//     checkbox with a reason instead of offering a save the server would refuse;
//   * every change writes the whole setting back over the relay, and a refusal puts the
//     form back on what the server actually holds rather than leaving the rejected pick
//     on screen.
// Run: node --test tests/js/options-default-browser.test.mjs   (or: npm run test:js)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import { domContentLoaded } from './dom-ready.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OPTIONS_HTML = readFileSync(path.join(__dirname, '../../extension-gecko/options.html'), 'utf8');
const OPTIONS_JS = readFileSync(path.join(__dirname, '../../extension-gecko/options.js'), 'utf8');

const flush = () => new Promise((resolve) => setImmediate(resolve));

// Objects the page builds live in the JSDOM realm, so their Object prototype is not
// this realm's and deepStrictEqual refuses them. Copy across the boundary first.
const plain = (value) => JSON.parse(JSON.stringify(value));

// The payload server/browsers/default_browser.py's payload() builds: the stored setting plus the
// candidates to pick from.
function makePayload({ settings = {}, browsers = null } = {}) {
  return {
    settings: {
      mode: 'first_started', instance_id: null, name: null, engine: null,
      exe_path: null, auto_launch: false, launch_args: '', ...settings,
    },
    browsers: browsers || [
      { instance_id: 'me', slot: 1, name: 'Firefox Developer Edition', engine: 'gecko',
        exe_path: 'C:\\ff\\firefox.exe', remote: false, connected: true,
        is_current: true, is_selected: false, can_launch: true },
      { instance_id: 'zen', slot: 2, name: 'Zen Browser', engine: 'gecko',
        exe_path: 'C:\\zen\\zen.exe', remote: false, connected: true,
        is_current: false, is_selected: false, can_launch: true },
    ],
    current_instance_id: 'me',
    launch_wait_seconds: 25.0,
  };
}

// Load the real options.html and run the real options.js. `state.saves` collects every
// setDefaultBrowser the page sends; `state.reply` decides what the server answers.
async function initOptions({ payload = makePayload(), reply = null } = {}) {
  const dom = new JSDOM(OPTIONS_HTML, { pretendToBeVisual: true, runScripts: 'outside-only' });
  const { window } = dom;
  const pageReady = domContentLoaded(window);
  const state = { payload, saves: [], reply, loads: 0 };

  window.browser = {
    runtime: {
      lastError: null,
      getManifest: () => ({ version: '0.0.0-test' }),
      sendMessage: (msg) => {
        if (msg && msg.action === 'getDefaultBrowser') {
          state.loads += 1;
          return Promise.resolve(state.payload
            ? { success: true, data: state.payload }
            : { success: false, error: 'not connected' });
        }
        if (msg && msg.action === 'setDefaultBrowser') {
          state.saves.push(plain(msg.settings));
          if (state.reply) return Promise.resolve(state.reply(msg.settings));
          // Echo the write back the way the server does: its answer is the payload
          // built from the NEW settings, and the page re-renders from that. Returning
          // the pre-save payload would silently undo every change on screen.
          state.payload = {
            ...state.payload,
            settings: { ...state.payload.settings, ...plain(msg.settings) },
          };
          return Promise.resolve({ success: true, data: { ...state.payload, ok: true } });
        }
        if (msg && (msg.type === 'getConnectionStatus' || msg.action === 'getConnectionStatus')) {
          return Promise.resolve({ connected: true, serverHealthy: true, serverMcpPort: 2830 });
        }
        if (msg && msg.action === 'getServerConnections') {
          return Promise.resolve({ success: false, error: 'not connected' });
        }
        if (msg && msg.action === 'getClaudeUsage') {
          return Promise.resolve({ success: false, error: 'HTTP 403' });
        }
        return Promise.resolve({ success: true });
      },
    },
    storage: {
      sync: {
        get: (defaults) => Promise.resolve({ ...defaults }),
        set: () => Promise.resolve(),
        remove: () => Promise.resolve(),
        clear: () => Promise.resolve(),
      },
      local: { get: (defaults) => Promise.resolve({ ...defaults }), set: () => Promise.resolve() },
      onChanged: { addListener: () => {} },
    },
    tabs: { create: () => Promise.resolve() },
  };
  window.fetch = async () => ({ ok: true, json: async () => ({ status: 'ok' }), text: async () => '' });
  window.confirm = () => true;

  window.eval(OPTIONS_JS);
  await pageReady;
  await flush();
  await flush();
  return { window, document: window.document, state };
}

const optionLabels = (document) =>
  Array.from(document.getElementById('defaultBrowserSelect').options).map((o) => o.textContent);

async function change(window, id) {
  window.document.getElementById(id).dispatchEvent(new window.Event('change'));
  await flush();
  await flush();
}

test('the list is First started, then this browser, then the other connected ones', async () => {
  const { document } = await initOptions();
  assert.deepEqual(optionLabels(document), [
    'First started',
    'Firefox Developer Edition (this browser)',
    'Zen Browser',
  ]);
  assert.equal(document.getElementById('defaultBrowserSelect').value, 'first_started');
});

test('a browser that was picked and has since been closed stays on the list', async () => {
  // Dropping it would render as "First started" for a server that is still pinned, and
  // the next save would clear a setting the user never touched.
  const payload = makePayload({
    settings: { mode: 'pinned', instance_id: 'gone', name: 'Zen Browser',
                exe_path: 'C:\\zen\\zen.exe' },
    browsers: [
      { instance_id: 'me', slot: 1, name: 'Firefox Developer Edition', connected: true,
        is_current: true, can_launch: true },
      { instance_id: 'gone', slot: null, name: 'Zen Browser', connected: false,
        is_current: false, is_selected: true, can_launch: true },
    ],
  });
  const { document } = await initOptions({ payload });
  assert.ok(optionLabels(document).includes('Zen Browser (not connected)'));
  assert.equal(document.getElementById('defaultBrowserSelect').value, 'gone');
  assert.equal(document.getElementById('defaultBrowserNote').hidden, false);
});

test('the launch checkbox is hidden on First started and shown once a browser is picked', async () => {
  const { window, document } = await initOptions();
  assert.equal(document.getElementById('defaultBrowserLaunchField').hidden, true);
  assert.equal(document.getElementById('defaultBrowserArgsField').hidden, true);

  document.getElementById('defaultBrowserSelect').value = 'zen';
  await change(window, 'defaultBrowserSelect');
  assert.equal(document.getElementById('defaultBrowserLaunchField').hidden, false);
  // The arguments belong to the launch, so they wait for the checkbox.
  assert.equal(document.getElementById('defaultBrowserArgsField').hidden, true);

  document.getElementById('defaultBrowserAutoLaunch').checked = true;
  await change(window, 'defaultBrowserAutoLaunch');
  assert.equal(document.getElementById('defaultBrowserArgsField').hidden, false);
});

test('a browser the server cannot start disables the checkbox and says why', async () => {
  const payload = makePayload({
    browsers: [
      { instance_id: 'me', slot: 1, name: 'Firefox Developer Edition', connected: true,
        is_current: true, can_launch: true },
      { instance_id: 'r', slot: 2, name: 'Firefox (10.0.0.5)', connected: true,
        is_current: false, remote: true, can_launch: false },
    ],
  });
  const { window, document } = await initOptions({ payload });
  document.getElementById('defaultBrowserSelect').value = 'r';
  await change(window, 'defaultBrowserSelect');

  const checkbox = document.getElementById('defaultBrowserAutoLaunch');
  assert.equal(checkbox.disabled, true);
  assert.equal(checkbox.checked, false);
  assert.match(document.getElementById('defaultBrowserNote').textContent, /another machine/);
});

test('picking a browser writes the whole setting back to the server', async () => {
  const { window, document, state } = await initOptions();
  document.getElementById('defaultBrowserSelect').value = 'zen';
  await change(window, 'defaultBrowserSelect');
  document.getElementById('defaultBrowserAutoLaunch').checked = true;
  await change(window, 'defaultBrowserAutoLaunch');

  // The startup arguments are read-only in the page and are never part of a write: they
  // become a process's argv, so only config.json on the server host may set them.
  const args = document.getElementById('defaultBrowserArgs');
  assert.equal(args.readOnly, true);
  args.value = '--renderer-cmd-prefix=calc.exe';
  await change(window, 'defaultBrowserArgs');

  assert.equal(state.saves.length, 2);
  assert.deepEqual(state.saves.at(-1), {
    mode: 'pinned', instance_id: 'zen', auto_launch: true,
  });
});

test('choosing First started sends no browser and no launch', async () => {
  const payload = makePayload({ settings: { mode: 'pinned', instance_id: 'zen',
                                            auto_launch: true, launch_args: '-P work' } });
  const { window, document, state } = await initOptions({ payload });
  assert.equal(document.getElementById('defaultBrowserSelect').value, 'zen');

  document.getElementById('defaultBrowserSelect').value = 'first_started';
  await change(window, 'defaultBrowserSelect');
  assert.deepEqual(state.saves.at(-1), { mode: 'first_started' });
});

test('a refusal re-reads the server rather than leaving the rejected pick on screen', async () => {
  const { window, document, state } = await initOptions({
    reply: () => ({ success: true, data: { ok: false, error: 'that browser is not connected' } }),
  });
  const loadsBefore = state.loads;
  document.getElementById('defaultBrowserSelect').value = 'zen';
  await change(window, 'defaultBrowserSelect');

  assert.ok(state.loads > loadsBefore, 'the page re-read the stored setting');
  assert.equal(document.getElementById('defaultBrowserSelect').value, 'first_started');
  assert.match(document.getElementById('status').textContent, /not connected/);
});

test('a server that does not answer disables the controls instead of showing a made-up default', async () => {
  const { document } = await initOptions({ payload: null });
  assert.equal(document.getElementById('defaultBrowserSelect').disabled, true);
  assert.equal(document.getElementById('defaultBrowserAutoLaunch').disabled, true);
  assert.match(document.getElementById('defaultBrowserNote').textContent, /did not answer/);
});
