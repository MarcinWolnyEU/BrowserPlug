// Unit tests for the Options ▸ Health connection map against the REAL
// options.html (jsdom, no Firefox). What they guard:
//   * selecting the Health tab draws the full topology from the same
//     get_connections_info() payload the popup consumes: client → launcher →
//     server → browsers, one edge per LINK (keyed edge:from>to);
//   * port stubs are visible attachment points: stdio on the client/launcher,
//     the MCP HTTP port on the server's left edge, the WS port on its right,
//     ws on each browser;
//   * a REMOTE browser is visually distinct (`.remote` node + edge, REMOTE pill,
//     its address on the node) — the map's whole point for remote connections;
//   * requester_slot marks "this browser" (`.self`);
//   * a connection that disappears between two polls leaves the map, and the
//     map polls ONLY while the Health pane is active (live/paused badge);
//   * a command COMPLETING between two polls replays across the map (worms out
//     and back) and pops "<N> ms" timers from last_command.timings — but a
//     command that finished before the pane opened does not replay;
//   * the Server health grid's last-command cell prints the same breakdown.
//
// The payload fixtures below are validated against tests/fixtures/connections_info_contract.json
// — the same file tests/unit/test_connections_contract.py holds the REAL Python
// get_connections_info() to. Frozen by hand and checked against nothing, they used to
// survive a server-side key rename with every assertion green while the real map broke.
//
// The replay animations run on a MOCK CLOCK (t.mock.timers), not on wall-clock sleeps:
// the map's timings are exact and instant, so nothing here is load- or flake-sensitive.
// Run: node --test tests/js/options-map.test.mjs   (or: npm run test:js)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import { domContentLoaded } from './dom-ready.mjs';
import { assertMatchesContract } from './contract.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OPTIONS_HTML = readFileSync(path.join(__dirname, '../../extension-gecko/options.html'), 'utf8');
const OPTIONS_JS = readFileSync(path.join(__dirname, '../../extension-gecko/options.js'), 'utf8');

// Drain microtasks + the jsdom task queue WITHOUT setTimeout: once t.mock.timers is on,
// setTimeout no longer fires by itself, so a setTimeout-based flush would hang forever.
// setImmediate is left real (only the 'setTimeout' api is mocked).
const flush = () => new Promise((resolve) => setImmediate(resolve));

// Every fixture goes out through the contract. requireAll:true — this file's payload
// claims to be "as the server builds it", so it is held to the whole shape, and a key
// Python renamed cannot keep living here under its old name.
function contract(value, objectName) {
  assertMatchesContract(value, objectName, { requireAll: true });
  return value;
}

// A get_connections_info() payload as the server builds it: the server row, one
// local + one remote browser, one MCP client behind a stdio launcher (pid 888).
// requester_slot says WE are slot 1 (the WS relay adds that key, not the emitter).
function makePayload() {
  return contract({
    count: 4,
    generated_hms: '12:00:02',
    generated_epoch: 1000.0,
    requester_slot: 1,
    last_command: null,
    version_warning: null,
    // The fair-queue block. The map draws nothing from it, but the contract
    // requires it of every payload, so a fixture that omits it is claiming a shape
    // the server no longer emits — exactly the drift `contract()` exists to catch.
    browser_concurrency: {
      max_concurrent_per_browser: 3,
      max_queued_per_browser: 24,
      max_queue_wait_seconds: 120.0,
      browsers: [],
    },
    connections: [
      {
        kind: 'server', label: 'BrowserPlug server', pid: 42, version: '1.7.5',
        exe_path: 'C:\\dr\\browserplug\\.venv\\Scripts\\python.exe',
        activity_hms: '12:00:02', activity_epoch: 1000.0, since_hms: '11:00:00',
        detail: 'ws://localhost:8772  +  http://localhost:2830/mcp/',
      },
      {
        kind: 'extension', label: '[1] Firefox Developer Edition', slot: 1,
        is_remote: false, remote_addr: '127.0.0.1', pid: 111,
        exe_path: 'C:\\Program Files\\Firefox Developer Edition\\firefox.exe',
        engine: 'gecko', platform: { os: 'win', arch: 'x86-64' },
        browser_info: { name: 'Firefox', vendor: 'Mozilla', version: '155.0' },
        version: '1.7.33',
        activity_hms: '12:00:01', activity_epoch: 999.0, since_hms: '11:01:00',
        detail: 'WebSocket: 8772',
      },
      {
        // A remote browser has no process to probe: no pid, no exe, no start time —
        // its hello (platform + browser_info) is all that identifies it, which is how
        // the label can name the phone at all.
        kind: 'extension', label: '[2, Remote] Firefox for Android Nightly', slot: 2,
        is_remote: true, remote_addr: '192.168.0.7', pid: null, exe_path: null,
        engine: null, platform: { os: 'android', arch: 'aarch64' },
        browser_info: { name: 'Firefox Nightly', vendor: 'Mozilla', version: '157.0a1' },
        version: '1.7.33',
        activity_hms: '11:50:00', activity_epoch: 400.0, since_hms: null,
        detail: 'WebSocket: 8772 · from 192.168.0.7',
      },
      {
        kind: 'mcp_client', label: 'Claude Code — C:\\dr\\browserplug',
        friendly: 'Claude Code', vendor: 'Anthropic',
        cwd: 'C:\\dr\\browserplug', pid: 777, launcher_pid: 888,
        launcher_version: '1.7.5',
        // This client's calls are routed to browser 2 (the remote one), explicitly
        // chosen with browsers(action="use") — the map's orange routing line.
        target_browser_slot: 2, target_browser_explicit: true,
        launcher_since_hms: '11:29:58', exe_path: 'C:\\claude\\claude.exe',
        cmdline: 'C:\\claude\\claude.exe --dangerously-skip-permissions',
        since_hms: '11:30:00', started_hms: '11:30:00', session_count: 2,
        sessions: [
          { id: 'abc', last_activity_hms: '12:00:00',
            commands: [{ time_hms: '12:00:00', tool: 'tabs', params: '' }] },
          { id: 'def', last_activity_hms: '11:59:00', commands: [] },
        ],
        activity_hms: '12:00:00', activity_epoch: 998.0,
        detail: 'pid 777 · 2 sessions',
      },
    ],
    extension_instances: [
      {
        slot: 1, pid: 111, name: 'Firefox Developer Edition',
        friendly: 'Firefox Developer Edition', version: '1.7.5',
        // Pushed by the extension (browser_state): the BROWSER half of the map.
        windows: 3, tabs: 42, active_tabs: 3, loaded_tabs: 12, state_hms: '12:00:00',
        // The foreground tab of each of its 3 windows, named rather than counted.
        active_tab_list: [
          { id: 5, title: 'Example Domain', last_hms: '12:00:01' },
          { id: 12, title: 'A very long page title that will not fit', last_hms: '11:58:03' },
          { id: 31, title: '', last_hms: null },
        ],
        exe_path: 'C:\\Program Files\\Firefox Developer Edition\\firefox.exe',
        last_connect_hms: '11:01:00', connect_count: 1,
        last_activity_hms: '12:00:01', last_activity_epoch: 999.0,
        last_action: 'tabs.list', remote_addr: '127.0.0.1', is_remote: false,
        currently_connected: true,
      },
      {
        // A remote browser has no process to probe, and this one runs an extension
        // too old to push its window/tab counts — every such field stays null.
        slot: 2, pid: null, name: 'remote-192.168.0.7', friendly: 'Firefox',
        version: '1.7.4',
        windows: null, tabs: null, active_tabs: null, loaded_tabs: null,
        active_tab_list: null, state_hms: null,
        exe_path: null,
        last_connect_hms: '11:40:00',
        connect_count: 3, last_activity_hms: null, last_activity_epoch: null,
        last_action: null, remote_addr: '192.168.0.7', is_remote: true,
        currently_connected: true,
      },
    ],
  }, 'payload');
}

// last_command as the server serves it once a call's cycle finished (server
// stamps + the launcher's /timing report merged in).
function makeCompletedCommand(startedEpoch) {
  return contract({
    tool: 'tabs', action: 'tabs.list', params: '',
    at: '2026-07-15 12:00:01.100', client_pid: 777, session: 'session-',
    acknowledged_by_extension: true, acknowledged_at: '2026-07-15 12:00:01.180',
    // The structured outcome verdict. A clean call still carries it — that is the point:
    // "failed" is a value the operator reads, never the absence of a key.
    outcome: { failed: false, code: null, retryable: false, degraded: [] },
    browser_slot: 1, launcher_pid: 888,
    timings: {
      started_epoch: startedEpoch,
      server_dispatch_ms: 3.2, browser_ms: 85.0, server_finalize_ms: 1.4,
      server_ms: 89.6, launcher_ms: 132.4, proxy_overhead_ms: 42.8,
      complete: true,
    },
  }, 'last_command');
}

// What the server serves for a call that never sent a WS command (browsers, server_info):
// no action, nothing acknowledged, no browser leg — its whole server stay is `server_ms`.
function makeServerSideCommand(startedEpoch, { launcher = true } = {}) {
  return contract({
    tool: 'browsers', action: null, params: { action: 'list' },
    at: '2026-07-15 12:00:03.000', client_pid: 777, session: 'session-',
    acknowledged_by_extension: null, acknowledged_at: null,
    outcome: { failed: false, code: null, retryable: false, degraded: [] },
    browser_slot: null, launcher_pid: 888,
    timings: {
      started_epoch: startedEpoch,
      server_dispatch_ms: null, browser_ms: null, server_finalize_ms: null,
      server_ms: 2.5,
      launcher_ms: launcher ? 9.0 : null,
      proxy_overhead_ms: launcher ? 6.5 : null,
      complete: true,
    },
  }, 'last_command');
}

function makeBrowserMock(state) {
  return {
    runtime: {
      lastError: null,
      getManifest: () => ({ version: '0.0.0-test' }),
      sendMessage: (msg) => {
        if (msg && msg.action === 'getServerConnections') {
          state.connectionsPolls += 1;
          return Promise.resolve(state.payload
            ? { success: true, data: state.payload }
            : { success: false, error: 'not connected' });
        }
        if (msg && (msg.type === 'getConnectionStatus' || msg.action === 'getConnectionStatus')) {
          return Promise.resolve({ connected: true, serverHealthy: true, serverMcpPort: 2830 });
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
      local: {
        get: (defaults) => Promise.resolve({ ...defaults }),
        set: () => Promise.resolve(),
      },
      onChanged: { addListener: () => {} },
    },
    tabs: { create: () => Promise.resolve() },
  };
}

// Load the real options.html, run the real options.js, land on the Health tab.
async function initOptionsOnHealth({ payload = makePayload() } = {}) {
  const dom = new JSDOM(OPTIONS_HTML, { pretendToBeVisual: true, runScripts: 'outside-only' });
  const { window } = dom;
  const pageReady = domContentLoaded(window);
  const state = { payload, connectionsPolls: 0 };
  window.browser = makeBrowserMock(state);
  // /health and the remote-setup probe: a minimal healthy answer is enough.
  window.fetch = async () => ({ ok: true, json: async () => ({ status: 'ok' }), text: async () => '' });

  // Bridge the page's timers to globalThis so node's mock clock can drive them.
  // node:test's t.mock.timers patches globalThis; options.js runs in the JSDOM realm and
  // resolves `setTimeout` off the jsdom window, which is a different global object — so
  // the mock would never see it. Forward INDIRECTLY (resolved at call time), which also
  // means init below still runs on real timers and only the animations under test move
  // onto the fake clock, once a test calls useMockClock().
  window.setTimeout = (fn, ms, ...rest) => setTimeout(fn, ms, ...rest);
  window.clearTimeout = (id) => clearTimeout(id);

  window.eval(OPTIONS_JS);
  await pageReady;
  await flush();
  await flush();
  window.document.getElementById('tab-health').click();
  await flush();
  await flush();
  return { window, document: window.document, state };
}

// The map's REAL animation constants. They can't be read off the running page: they are
// top-level `const`s, and jsdom's window.eval keeps those in a per-call lexical scope
// (only `var`/function declarations reach `window`). So read them from the SOURCE, with a
// loud guard — change MAP_SEG_MS in the product and these tests follow it, instead of
// silently going on asserting a stale hard-coded number.
function mapConst(name) {
  const m = new RegExp(`^const ${name} = (\\d+)`, 'm').exec(OPTIONS_JS);
  assert.ok(m, `const ${name} not found in options.js — it was renamed or reshaped. `
    + 'Re-point this at the real constant; do not hard-code its value here.');
  return Number(m[1]);
}
const MAP_EXIT_MS = mapConst('MAP_EXIT_MS');
const MAP_SEG_MS = mapConst('MAP_SEG_MS');

// Take over the page's clock. Everything scheduled from here on advances only when a test
// says so — exactly and instantly. Auto-restored when the test ends.
const useMockClock = (t) => t.mock.timers.enable({ apis: ['setTimeout'] });

// Advance the fake clock, then let the promise jobs it woke settle.
async function tick(t, ms) {
  t.mock.timers.tick(ms);
  await flush();
}

// A poll landed: hand the map `payload` and let its async fetch resolve.
async function poll(window, state, payload) {
  state.payload = payload;
  window.refreshConnectionMap();
  await flush();
  await flush();
}

test('selecting Health draws the full topology: nodes for every party, one edge per link', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    assert.equal(svg.querySelectorAll('g.mn').length, 7,
      'server + 1 MCP client + its launcher + 2 browsers, each split into extension + browser');
    assert.equal(svg.querySelectorAll('g.me').length, 7,
      'client→launcher, launcher→server, server→extension→browser for each of the two, '
      + 'plus the routing line to the browser this client drives');
    assert.ok(svg.querySelector('g.mn.k-server[data-key="server"]'), 'the server node exists');
    assert.ok(svg.querySelector('g.me[data-key="edge:client:777>launcher:888"]'), 'stdio link');
    assert.ok(svg.querySelector('g.me[data-key="edge:launcher:888>server"]'), 'http link');
    assert.ok(svg.querySelector('g.me[data-key="edge:server>ext:1"]'),
      'the WebSocket ends at the EXTENSION, not at the browser');
    assert.ok(svg.querySelector('g.me[data-key="edge:ext:1>browser:1"]'),
      'and the extension drives its browser over the WebExtension API');
    assert.equal(svg.querySelector('g.me[data-key="edge:server>browser:1"]'), null,
      'so there is no server→browser edge any more');
    assert.equal(document.getElementById('connMapMsg').hidden, true,
      'the placeholder message clears once a snapshot rendered');
    assert.match(document.getElementById('connMapSource').textContent, /live WebSocket/,
      'the footer names the data path used');
  } finally {
    window.close();
  }
});

test('the launcher is on the map and every box shows its ports as attachment stubs', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    const launcher = svg.querySelector('g.mn.k-launcher[data-key="launcher:888"]');
    assert.ok(launcher, 'the stdio launcher gets its own node');
    assert.ok([...launcher.querySelectorAll('text')].some(t => t.textContent.includes('888')),
      'named by its pid');

    const ports = (el) => [...el.querySelectorAll('text.mn-port')].map(t => t.textContent).sort();
    assert.deepEqual(ports(svg.querySelector('g.mn[data-key="client:777"]')), ['stdio'],
      'a launcher-backed client leaves through a stdio port');
    assert.deepEqual(ports(launcher), ['http', 'stdio'],
      'the launcher bridges stdio in → http out');
    assert.deepEqual(ports(svg.querySelector('g.mn[data-key="server"]')), [':2830', ':8772'],
      'the server names its real ports (MCP HTTP in, extension WS out)');
    assert.deepEqual(ports(svg.querySelector('g.mn[data-key="ext:1"]')), ['api', 'ws'],
      'the extension takes the WebSocket in and leaves through the WebExtension API');
    assert.deepEqual(ports(svg.querySelector('g.mn[data-key="browser:1"]')), ['webext']);
  } finally {
    window.close();
  }
});

test('a remote browser is unmistakable: .remote node with REMOTE pill and its address, .remote edge', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    const remoteNode = svg.querySelector('g.mn[data-key="browser:2"]');
    assert.ok(remoteNode, 'the remote browser has a node');
    assert.ok(remoteNode.classList.contains('remote'), 'it carries the .remote styling class');
    assert.ok([...remoteNode.querySelectorAll('text')].some(t => t.textContent === 'REMOTE'),
      'it wears the REMOTE pill');
    assert.ok([...remoteNode.querySelectorAll('text')].some(t => t.textContent.includes('192.168.0.7')),
      'its remote address is on the node');

    const localNode = svg.querySelector('g.mn[data-key="browser:1"]');
    assert.ok(localNode && !localNode.classList.contains('remote'),
      'the local browser must NOT look remote');

    // Both halves of a remote browser are remote — and so are both of its edges.
    assert.ok(svg.querySelector('g.mn[data-key="ext:2"]').classList.contains('remote'),
      'the extension running inside it is remote as well');
    assert.ok(!svg.querySelector('g.mn[data-key="ext:2"]')
      .querySelector('text.mn-pill-text'), 'but only ONE pill per browser, on the browser box');

    const remoteEdge = svg.querySelector('g.me[data-key="edge:server>ext:2"]');
    assert.ok(remoteEdge.classList.contains('remote'), 'its line is styled remote too');
    assert.ok(svg.querySelector('g.me[data-key="edge:ext:2>browser:2"]').classList.contains('remote'));
    const localEdge = svg.querySelector('g.me[data-key="edge:server>ext:1"]');
    assert.ok(!localEdge.classList.contains('remote'));
  } finally {
    window.close();
  }
});

test('requester_slot marks the browser this page lives in as .self ("this browser")', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    const self = svg.querySelector('g.mn[data-key="browser:1"]');
    assert.ok(self.classList.contains('self'));
    assert.ok([...self.querySelectorAll('text')].some(t => t.textContent.includes('this browser')));
    assert.ok(!svg.querySelector('g.mn[data-key="browser:2"]').classList.contains('self'));
  } finally {
    window.close();
  }
});

// The hop edges show how the bytes travel; with several browsers connected they
// cannot show WHICH browser a client is driving. That is what the routing line is.
test('an orange routing line runs from the client to the browser its calls land in', async () => {
  const { window, document, state } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    // From the client's LAST box (its launcher) to the browser it selected (slot 2).
    const route = svg.querySelector('g.me[data-key="edge:launcher:888>browser:2"]');
    assert.ok(route, 'the routing line exists');
    assert.ok(route.classList.contains('route'), 'and is styled as a routing line, not a wire');
    assert.match(route.querySelector('.me-line').getAttribute('marker-end'), /mapRouteArrow/,
      'it points AT the browser (arrowhead)');
    assert.match(route.querySelector('title').textContent, /Claude Code's calls are routed to/,
      'and its tooltip says whose calls go where');
    assert.match(route.querySelector('title').textContent, /use/,
      'saying the choice was explicit');
    assert.equal(svg.querySelector('g.me[data-key="edge:launcher:888>browser:1"]'), null,
      'exactly one browser per client: the other one gets no line');

    // A client whose selected browser is gone routes NOWHERE, and the server says so
    // by sending no slot; drawing a line to some other browser would be a lie.
    const orphaned = makePayload();
    orphaned.connections.find((c) => c.kind === 'mcp_client').target_browser_slot = null;
    await poll(window, state, orphaned);
    assert.ok(route.classList.contains('dying'),
      'no routing line when the client is not routed anywhere');
  } finally {
    window.close();
  }
});

// The routing line is the one edge that is not lying on the board. It is slung between
// two boxes and stands off them, so the view's own movement has to show on it.
test('the routing line hangs below the boxes and swings as the view pans', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    const route = svg.querySelector('g.me[data-key="edge:launcher:888>browser:2"]');
    const curve = (el) => {
      const d = el.getAttribute('d');
      const m = /^M ([-\d.]+) ([-\d.]+) Q ([-\d.]+) ([-\d.]+), ([-\d.]+) ([-\d.]+)$/.exec(d);
      assert.ok(m, `a hanging cable is ONE quadratic Bézier (control point = the droop); got ${d}`);
      return { x1: +m[1], y1: +m[2], cx: +m[3], cy: +m[4], x2: +m[5], y2: +m[6] };
    };

    const cable = curve(route.querySelector('.me-line'));
    const ends = Math.max(cable.y1, cable.y2);
    assert.ok(cable.cy > ends, `the belly hangs BELOW both ends (got ${cable.cy} vs ${ends})`);
    assert.equal(cable.cx, (cable.x1 + cable.x2) / 2,
      'and hangs straight down in the reset view: pan 0 is not a leaning view');
    assert.equal(route.querySelector('.me-hit').getAttribute('d'),
      route.querySelector('.me-line').getAttribute('d'),
      'the hover target follows the cable, not the straight line it used to be');

    const wire = svg.querySelector('g.me[data-key="edge:server>ext:1"]');
    const flat = wire.querySelector('.me-line').getAttribute('d');

    // Drag right: the cable stands off the board, so the view's movement swings it. The
    // amount is what makes this visible rather than theoretical — a drag of a hand's width
    // has to move the belly by something a reader can see, which is what the floor below
    // is for. An earlier version leaned by ~6px per 100px of drag and read as broken.
    svg.dispatchEvent(new window.MouseEvent('pointerdown', { button: 0, clientX: 100, clientY: 50 }));
    svg.dispatchEvent(new window.MouseEvent('pointermove', { clientX: 180, clientY: 50 }));
    const swung = curve(route.querySelector('.me-line'));
    const belly = (c) => ({ x: (c.x1 + c.x2) / 4 + c.cx / 2, y: (c.y1 + c.y2) / 4 + c.cy / 2 });
    assert.ok(belly(swung).x - belly(cable).x > 10,
      `80px of drag has to move the belly visibly (moved ${belly(swung).x - belly(cable).x})`);
    assert.equal(wire.querySelector('.me-line').getAttribute('d'), flat,
      'while a flat wire is not reshaped by the pan at all');

    // Down: the view is looking at the board from higher up, so the droop shortens.
    svg.dispatchEvent(new window.MouseEvent('pointermove', { clientX: 100, clientY: 180 }));
    assert.ok(belly(curve(route.querySelector('.me-line'))).y < belly(cable).y - 3,
      'and panning down foreshortens it');
    svg.dispatchEvent(new window.MouseEvent('pointerup', { clientX: 100, clientY: 180 }));
  } finally {
    window.close();
  }
});

test('a connection that dies between polls fades out and leaves the map (and its edge with it)', async (t) => {
  const { window, document, state } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    assert.ok(svg.querySelector('g.mn[data-key="browser:2"]'), 'precondition: browser 2 is on the map');
    useMockClock(t);

    // The remote browser disconnects; the next poll no longer reports it.
    const gone = makePayload();
    gone.connections = gone.connections.filter(c => c.slot !== 2);
    gone.extension_instances = gone.extension_instances.filter(i => i.slot !== 2);
    await poll(window, state, gone);

    const dyingNode = svg.querySelector('g.mn[data-key="browser:2"]');
    assert.ok(dyingNode && dyingNode.classList.contains('dying'),
      'first it fades (the .dying transition class)');
    assert.ok(svg.querySelector('g.mn[data-key="ext:2"]').classList.contains('dying'),
      'its extension goes with it — they are one connection drawn as two boxes');
    assert.ok(svg.querySelector('g.me[data-key="edge:server>ext:2"]').classList.contains('dying'),
      'its edges fade with it');

    // One ms short of the exit: still fading, NOT yet gone. The fade is the point — an
    // immediate removal would pass a bare "it eventually disappears" assertion too.
    await tick(t, MAP_EXIT_MS - 1);
    assert.ok(svg.querySelector('g.mn[data-key="browser:2"]'),
      'it must still be there mid-fade — the .dying transition has not finished');

    await tick(t, 1);
    assert.equal(svg.querySelector('g.mn[data-key="browser:2"]'), null, 'then it is removed');
    assert.equal(svg.querySelector('g.mn[data-key="ext:2"]'), null);
    assert.equal(svg.querySelector('g.me[data-key="edge:server>ext:2"]'), null);
    assert.equal(svg.querySelector('g.me[data-key="edge:ext:2>browser:2"]'), null);
    assert.equal(svg.querySelectorAll('g.mn').length, 5, 'the survivors stay');
  } finally {
    window.close();
  }
});

test('the map only runs on the Health pane: the live badge pauses when leaving it', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const badge = document.getElementById('mapLive');
    assert.ok(!badge.classList.contains('paused'), 'live while Health is up');
    assert.equal(document.getElementById('mapLiveText').textContent, 'live');

    document.getElementById('tab-general').click();
    await flush();
    assert.ok(badge.classList.contains('paused'), 'paused as soon as another pane is selected');
    assert.equal(document.getElementById('mapLiveText').textContent, 'paused');
  } finally {
    window.close();
  }
});

test('hovering a node fills the rich tooltip from the row data', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const node = document.querySelector('g.mn[data-key="client:777"]');
    node.dispatchEvent(new window.MouseEvent('mouseenter', { clientX: 20, clientY: 20 }));
    const tip = document.getElementById('connMapTip');
    assert.ok(tip.classList.contains('visible'));
    assert.match(tip.textContent, /Claude Code/);
    assert.match(tip.textContent, /C:\\dr\\browserplug/, 'the full cwd (the node shows only its tail)');
    assert.match(tip.textContent, /tabs @ 12:00:00/, 'the most recent command across its sessions');
    assert.match(tip.textContent, /1 with commands · 2 total/,
      'session counts honor the hide-empty-sessions option (on by default)');

    node.dispatchEvent(new window.MouseEvent('mouseleave'));
    assert.ok(!tip.classList.contains('visible'), 'gone on mouseleave');

    // The launcher explains itself too.
    const launcher = document.querySelector('g.mn[data-key="launcher:888"]');
    launcher.dispatchEvent(new window.MouseEvent('mouseenter', { clientX: 20, clientY: 20 }));
    assert.match(tip.textContent, /stdio ⇄ HTTP proxy/);
    assert.match(tip.textContent, /Claude Code/, 'and names whose launcher it is');
    assert.match(tip.textContent, /11:29:58/, 'with its own start time, like everything else');
  } finally {
    window.close();
  }
});

test('the extension and its browser are separate boxes, each describing its own half', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    const ext = svg.querySelector('g.mn.k-ext[data-key="ext:1"]');
    assert.ok(ext, 'the extension gets its own node');
    const extText = [...ext.querySelectorAll('text')].map(t => t.textContent).join(' ');
    assert.match(extText, /\bextension\b/, 'named as what it is');
    assert.ok(!/BrowserPlug extension/.test(extText), 'plainly "extension", not branded');
    assert.match(extText, /v1\.7\.5/, 'and carries the EXTENSION version, not the browser one');
    assert.ok(!/slot/i.test(extText), 'the slot is an internal routing key; it is not shown');

    const tip = document.getElementById('connMapTip');
    ext.dispatchEvent(new window.MouseEvent('mouseenter', { clientX: 20, clientY: 20 }));
    assert.match(tip.textContent, /extension/, 'its tooltip is about the extension');
    assert.equal(tip.querySelector('.tip-kind'), null,
      'and carries no kind chip: the title already says "extension"');
    assert.ok(!/slot/i.test(tip.textContent), 'no slot row either');
    assert.match(tip.textContent, /1\.7\.5/, 'version');
    assert.match(tip.textContent, /tabs\.list @ 12:00:01/, 'what it last did');
    assert.match(tip.textContent, /Firefox Developer Edition/, 'and which browser it runs in');

    // The BROWSER half: the process, and what it is holding open right now.
    const browser = svg.querySelector('g.mn.k-browser[data-key="browser:1"]');
    browser.dispatchEvent(new window.MouseEvent('mouseenter', { clientX: 20, clientY: 20 }));
    assert.match(tip.textContent, /pid.*111/s, 'the browser process');
    assert.match(tip.textContent, /windows.*3/s, 'total windows');
    assert.match(tip.textContent, /12 loaded \/ 42 total/,
      'how many tabs are in memory, out of how many exist');
    assert.ok(!/active/.test(tip.textContent),
      'the active COUNT is gone: it was "1 active" forever on a single-window browser');

    // …replaced by the active tabs themselves: [id] title (last: hh:mm:ss).
    const tabs = [...tip.querySelectorAll('.tip-tab')].map((el) => el.textContent);
    assert.equal(tabs.length, 3, 'one line per window\'s foreground tab');
    assert.equal(tabs[0], '[5] Example Domain (last: 12:00:01)');
    assert.equal(tabs[1], '[12] A very long pag… (last: 11:58:03)',
      'a long title is cut at 15 characters');
    assert.equal(tabs[2], '[31] (untitled)',
      'a tab with no title and no known event says both, rather than showing blanks');
  } finally {
    window.close();
  }
});

test('a browser whose extension never pushed counts shows no invented zeroes', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const tip = document.getElementById('connMapTip');
    document.querySelector('g.mn[data-key="browser:2"]')
      .dispatchEvent(new window.MouseEvent('mouseenter', { clientX: 20, clientY: 20 }));
    assert.match(tip.textContent, /192\.168\.0\.7/, 'it still describes what it does know');
    assert.ok(!/windows/.test(tip.textContent),
      'but an absent count must be an ABSENT row — never a made-up 0');
    assert.ok(!/tabs/.test(tip.textContent));
  } finally {
    window.close();
  }
});

test('the map zooms and pans, and the reset button restores 100%', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const vp = document.getElementById('connMapViewport');
    const level = document.getElementById('connMapZoomReset');
    assert.ok(vp, 'everything is drawn inside the viewport group that zoom/pan transforms');
    assert.ok(vp.querySelector('g.mn'), 'nodes really live in it');
    assert.equal(level.textContent, '100%');

    document.getElementById('connMapZoomIn').click();
    assert.match(vp.getAttribute('transform'), /scale\(1\.250\)/, 'one press = one step in');
    assert.equal(level.textContent, '125%');
    assert.ok(document.getElementById('connMap').classList.contains('zoomed'));

    document.getElementById('connMapZoomOut').click();
    assert.match(vp.getAttribute('transform'), /scale\(1\.000\)/, 'and one step back out');

    // Drag to pan: the transform's translate follows the pointer.
    const svg = document.getElementById('connMapSvg');
    svg.dispatchEvent(new window.MouseEvent('pointerdown', { button: 0, clientX: 100, clientY: 50 }));
    svg.dispatchEvent(new window.MouseEvent('pointermove', { clientX: 160, clientY: 80 }));
    const panned = vp.getAttribute('transform');
    assert.ok(/translate\((?!0\.00 0\.00)/.test(panned), `the picture moved (got ${panned})`);
    svg.dispatchEvent(new window.MouseEvent('pointerup', { clientX: 160, clientY: 80 }));

    level.click();
    assert.equal(vp.getAttribute('transform'), 'translate(0.00 0.00) scale(1.000)',
      'reset puts it back exactly');
    assert.equal(level.textContent, '100%');
    assert.ok(!document.getElementById('connMap').classList.contains('zoomed'));
  } finally {
    window.close();
  }
});

test('the wheel zooms, but hands the scroll back to the page at the zoom limits', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    const vp = document.getElementById('connMapViewport');
    const wheel = (deltaY) => {
      const e = new window.WheelEvent('wheel', { deltaY, cancelable: true, bubbles: true });
      svg.dispatchEvent(e);
      return e;
    };

    assert.ok(wheel(-120).defaultPrevented, 'zooming in swallows the scroll');
    assert.match(vp.getAttribute('transform'), /scale\(1\.250\)/);

    // Wind all the way out to the floor, then one more notch.
    for (let i = 0; i < 20; i++) wheel(120);
    assert.match(vp.getAttribute('transform'), /scale\(0\.500\)/, 'clamped at MAP_ZOOM_MIN');
    assert.ok(!wheel(120).defaultPrevented,
      'at the limit the page must scroll again — a settings page may not trap the wheel');
  } finally {
    window.close();
  }
});

test('the health grid reports the server version, and the mismatch warning only when there is one', async () => {
  const { window, document } = await initOptionsOnHealth();
  try {
    const out = document.getElementById('healthOutput');
    window.renderHealth(out, { status: 'ok', server_version: '1.7.5', version_warning: null });
    assert.match(out.textContent, /Server version.*1\.7\.5/s);
    assert.ok(!/Version mismatch/.test(out.textContent),
      'no alarm row while everything is in lockstep');

    const warning = 'Browser Zen Browser (PID 38288) has a newer version (1.7.6) than the server (1.7.5)';
    window.renderHealth(out, { status: 'ok', server_version: '1.7.5', version_warning: warning });
    assert.match(out.textContent, /Version mismatch/);
    assert.match(out.textContent, /Zen Browser \(PID 38288\)/, 'verbatim, as /health worded it');
  } finally {
    window.close();
  }
});

// The replay's shape in time: N hops out, N back, one MAP_SEG_MS each, then the pops
// spawn 60ms after the cycle closes (replayCommand's `at + 60`). Derived from the real
// constant so the tests cannot drift from the product's own timing.
const cycleMs = (hops) => hops * 2 * MAP_SEG_MS;
const popText = (svg) => [...svg.querySelectorAll('.ms-pop-text')].map(t => t.textContent);

test('a command completing between polls replays: worms out and back, then ms timers pop', async (t) => {
  const { window, document, state } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    assert.equal(svg.querySelector('.me-worm'), null, 'nothing replays without a completed command');
    const full = cycleMs(4);   // client→launcher→server→extension→browser, and back
    useMockClock(t);

    const payload = makePayload();
    payload.last_command = makeCompletedCommand(2000.5);
    await poll(window, state, payload);
    await tick(t, 0);   // the first hop's worm is scheduled at delay 0

    const worm = svg.querySelector('g.me[data-key="edge:client:777>launcher:888"] .me-worm.fwd');
    assert.ok(worm, 'the request worm starts on the client→launcher hop');
    assert.ok(worm.getAttribute('d'), 'and has real geometry to run along');
    assert.equal(popText(svg).length, 0, 'the timers wait for the cycle to close');

    // One ms before the cycle closes: still animating, nothing popped yet. Then it lands.
    await tick(t, full + 59);
    assert.equal(popText(svg).length, 0,
      `no timer may pop before the response gets home (${full}ms + 60)`);

    await tick(t, 1);
    const pops = popText(svg);
    assert.ok(pops.some(p => p === '132 ms total'), `client total pops (got: ${pops})`);
    assert.ok(pops.some(p => p === '43 ms proxy'), 'launcher overhead pops');
    assert.ok(pops.some(p => p === '3.2 ms dispatch'), 'server dispatch pops');
    assert.ok(pops.some(p => p === '85 ms in browser'), 'browser leg pops');
    assert.ok(svg.querySelector('g.me[data-key="edge:ext:1>browser:1"] .me-worm'),
      'and the worm reached the browser THROUGH its extension');
  } finally {
    window.close();
  }
});

test('a server-side-only call (browsers) replays without a browser leg and pops "server"', async (t) => {
  const { window, document, state } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    const short = cycleMs(2);   // client→launcher→server, and back — no browser hop
    useMockClock(t);

    const payload = makePayload();
    payload.last_command = makeServerSideCommand(3000.5);
    await poll(window, state, payload);
    await tick(t, 0);

    assert.ok(svg.querySelector('g.me[data-key="edge:client:777>launcher:888"] .me-worm.fwd'),
      'the request worm still runs the client→launcher→server path');

    await tick(t, short + 60);
    assert.equal(svg.querySelector('g.me[data-key="edge:server>ext:1"] .me-worm'), null,
      'but no worm ever enters a browser this call never touched');
    const pops = popText(svg);
    assert.ok(pops.some(p => p === '2.5 ms server'), `whole server stay pops (got: ${pops})`);
    assert.ok(pops.some(p => p === '9 ms total'), 'client total pops');
    assert.ok(!pops.some(p => p.includes('in browser')), 'no browser timer for a browserless call');
    assert.ok(!pops.some(p => p.includes('dispatch')), 'no dispatch leg either');
  } finally {
    window.close();
  }
});

test('rapid successive commands replay one after another — never cancelling or interleaving', async (t) => {
  // The bug seen live (2026-07-15): an agent firing several tools per second
  // made each new replay cancel the previous one mid-flight, so fragments of
  // two different cycles interleaved (server→browser playing before
  // client→launcher→server). Replays are serialized now.
  const { window, document, state } = await initOptionsOnHealth();
  try {
    const svg = document.getElementById('connMapSvg');
    const aCycle = cycleMs(4);   // A: the full path to browser 1
    const bCycle = cycleMs(2);   // B: server-side only
    useMockClock(t);

    const a = makePayload();
    a.last_command = makeCompletedCommand(4000.1);
    await poll(window, state, a);
    await tick(t, 80);   // A is mid-flight

    // B (server-side-only) completes while A is still animating.
    const b = makePayload();
    b.last_command = makeServerSideCommand(4001.2, { launcher: false });
    await poll(window, state, b);

    // Exactly as A's cycle closes: A's timers must pop even though B arrived mid-replay
    // (the old token logic killed them exactly here) — and B must not have started popping.
    await tick(t, (aCycle + 60) - 80);
    const pops = popText(svg);
    assert.ok(pops.some(p => p === '85 ms in browser'),
      `A's timers survive B's arrival (got: ${pops})`);
    assert.ok(!pops.some(p => p === '2.5 ms server'),
      'B has not popped yet — it waited its turn behind A');

    // B then runs to completion on its own, on the far side of A's cycle.
    await tick(t, bCycle + 60);
    assert.ok(popText(svg).some(p => p === '2.5 ms server'),
      `B replayed after A (got: ${popText(svg)})`);
  } finally {
    window.close();
  }
});

test('a command that finished before the pane opened is history, not a replay', async (t) => {
  const payload = makePayload();
  payload.last_command = makeCompletedCommand(1500.0);
  const { window, document } = await initOptionsOnHealth({ payload });
  try {
    useMockClock(t);
    // Run the clock well past a full replay cycle: nothing was ever scheduled, so
    // nothing can appear. (A wall-clock sleep could only ever prove "not yet".)
    await tick(t, cycleMs(4) * 4);
    assert.equal(document.querySelector('.me-worm'), null, 'no worms for pre-existing history');
    assert.equal(document.querySelector('.ms-pop'), null, 'no timers either');
  } finally {
    window.close();
  }
});

test('the Server health grid last-command cell prints the same timing breakdown', async () => {
  const { window } = await initOptionsOnHealth();
  try {
    const out = window.lastCommandCell(makeCompletedCommand(2000.5));
    assert.match(out, /tabs @ .* ✓/);
    assert.match(out, /total 132 ms/);
    assert.match(out, /dispatch 3\.2 ms/);
    assert.match(out, /browser 85 ms/);
    assert.match(out, /finalize 1\.4 ms/);
    assert.match(out, /proxy 43 ms/);
    // Without timings the cell reads exactly as before.
    assert.equal(window.lastCommandCell({ tool: 'tabs', at: '12:00', acknowledged_by_extension: true }),
      'tabs @ 12:00 ✓');
  } finally {
    window.close();
  }
});
