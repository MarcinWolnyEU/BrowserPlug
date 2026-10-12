// Options page script for BrowserPlug extension configuration

// Portability shim (shared verbatim with extension-chromium): in Firefox
// `globalThis.browser` exists so this rebinds the same object; in Chromium it falls
// through to `chrome`. `??` short-circuits, so a jsdom mock of globalThis.browser
// never evaluates the bare `chrome` identifier.
const browser = globalThis.browser ?? chrome;

// Fresh-install defaults, kept in one place so the on-screen "Default: N" hints
// can't drift from the values loadSettings() actually falls back to.
const DEFAULT_WS_PORT = 8772;   // extension → server WebSocket
// The server's MCP HTTP port (health / restart). It PREFERS 2830 but walks upward
// (2831, 2832, … 2942) when the port is taken, so this is only a starting point;
// the live port is learned over the WebSocket (see learnedMcpPort below).
const DEFAULT_MCP_PORT = 2830;

// The WAN "Available online" check: an external service GETs the server's token-free
// /reachable (served on the WS port) from several internet nodes and reports what it
// saw. {target} is replaced with that URL; results are polled from the template
// origin's /check-result/<id> (the check-host.net API shape; a custom checker must
// speak the same one, from one origin).
const DEFAULT_CHECK_TEMPLATE = 'https://check-host.net/check-http?host={target}&max_nodes=3';
const WAN_CHECK_POLL_MS = 2000;      // nodes take a few seconds each; null = still working
const WAN_CHECK_TIMEOUT_MS = 90000;  // some nodes never report; give up eventually
// IPv4 on purpose (the `4.` prefix): a router port-forward is an IPv4 affair, and the
// default hostname would happily answer with an IPv6 the check could never reach.
// tnedi.me is ident.me's own documented mirror.
const PUBLIC_IP_URLS = ['https://4.ident.me/json', 'https://4.tnedi.me/json'];
const NET_MODE_LABELS = { local: 'Local', lan: 'LAN', wan: 'WAN (online)' };

document.addEventListener('DOMContentLoaded', async function() {
    // Show the extension's own version so it can be confirmed at a glance
    try {
        document.getElementById('extVersion').textContent = browser.runtime.getManifest().version;
    } catch (e) {
        console.error('Could not read extension version:', e);
    }

    // Before anything renders: the surface picker shows a different, single card on
    // Android, and a flash of the desktop set would be the first thing a phone user sees.
    await applyPlatform();

    // Load saved settings FIRST and await it: the inputs must hold the real
    // host/port before initTabs() can trigger the Health tab's auto-fetch,
    // otherwise the first probe uses the HTML-default localhost:2830.
    await loadSettings();

    // Update WebSocket URL when hostname or port changes
    document.getElementById('hostname').addEventListener('input', updateWebSocketUrl);
    document.getElementById('port').addEventListener('input', updateWebSocketUrl);
    // …and the port echoes in the network-mode panels (firewall notes, WAN URL).
    document.getElementById('port').addEventListener('input', () => {
        const wsPort = document.getElementById('port').value || DEFAULT_WS_PORT;
        document.getElementById('lanFirewallPort').textContent = wsPort;
        document.getElementById('wanFirewallPort').textContent = wsPort;
        renderWanAddress();
    });

    // Form submission
    document.getElementById('optionsForm').addEventListener('submit', saveSettings);

    // Button event listeners
    // `true` = a real press, the only thing allowed to dial. Passing the listener
    // directly would hand the click EVENT in as `dial`, which is truthy, so the page
    // would dial on its own initial render too.
    document.getElementById('refreshStatus').addEventListener(
        'click', () => refreshConnectionStatus(true));
    document.getElementById('saveAdvanced').addEventListener('click', saveAdvancedSettings);
    document.getElementById('resetDefaults').addEventListener('click', resetToDefaults);

    // Default browser (server-side setting; auto-saved on change). Loaded after the
    // form, since it needs the background's socket rather than storage.
    document.getElementById('defaultBrowserSelect').addEventListener('change', onDefaultBrowserChange);
    document.getElementById('defaultBrowserAutoLaunch').addEventListener('change', onDefaultBrowserChange);
    loadDefaultBrowser();

    // Interface options (auto-save on change; reflected live in the popup)
    document.getElementById('hideEmptySessions').addEventListener('change', saveInterfaceSettings);
    document.getElementById('toolCallSort').addEventListener('change', saveInterfaceSettings);

    // Provider-specific settings (auto-save on change; reflected live in the popup)
    for (const id of Object.keys(PROVIDER_SUB_FIELDS)) {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', () => {
            syncProviderSubState();
            saveProviderSettings();
            refreshProviderAuthIfOn(id);
        });
    }
    document.getElementById('anthropicPeakLeadMinutes').addEventListener('change', saveProviderSettings);
    document.getElementById('anthropicUsageRefreshMinutes').addEventListener('change', saveProviderSettings);
    document.getElementById('anthropicShowWeekly').addEventListener('change', saveProviderSettings);
    document.getElementById('deepseekPeakLeadMinutes').addEventListener('change', saveProviderSettings);
    document.getElementById('anthropicBackgroundRefresh').addEventListener('change', saveProviderSettings);
    document.getElementById('openaiBackgroundRefresh').addEventListener('change', saveProviderSettings);
    for (const id of ['copilotBackgroundRefresh', 'copilotUsageRefreshMinutes', 'copilotShowCompletions']) {
        document.getElementById(id).addEventListener('change', saveProviderSettings);
    }
    for (const id of ['anthropicUsageRefreshMinutes', 'openaiUsageRefreshMinutes', 'copilotUsageRefreshMinutes']) {
        document.getElementById(id).addEventListener('input', syncRefreshLabels);
    }

    // claude.ai sign-in state for the settings above (see refreshAnthropicAuth)
    document.getElementById('anthropicAuthRecheck').addEventListener('click', refreshAnthropicAuth);
    document.getElementById('anthropicSignIn').addEventListener('click', () => {
        browser.tabs.create({ url: 'https://claude.ai/' });
    });
    refreshProviderAuthIfOn('anthropicDisplayMode');

    // chatgpt.com sign-in state, and the two settings it gates (see refreshOpenAIAuth)
    document.getElementById('openaiUsageRefreshMinutes').addEventListener('change', saveProviderSettings);
    document.getElementById('openaiShowWeekly').addEventListener('change', saveProviderSettings);
    document.getElementById('openaiAuthRecheck').addEventListener('click', refreshOpenAIAuth);
    // github.com sign-in state for the GitHub Copilot card (see refreshCopilotAuth)
    document.getElementById('copilotAuthRecheck').addEventListener('click', refreshCopilotAuth);
    document.getElementById('copilotSignIn').addEventListener('click', () => {
        browser.tabs.create({ url: 'https://github.com/login' });
    });
    refreshProviderAuthIfOn('copilotDisplayMode');
    document.getElementById('openaiSignIn').addEventListener('click', () => {
        browser.tabs.create({ url: 'https://chatgpt.com/' });
    });
    refreshProviderAuthIfOn('openaiDisplayMode');

    // Kilo sessions (auto-save; the master toggle also enables/disables the sub-option)
    document.getElementById('kiloLoadDetails').addEventListener('change', () => { syncKiloSubState(); saveKiloSettings(); });
    document.getElementById('kiloClarifyNames').addEventListener('change', saveKiloSettings);

    // Captured-body censoring (General): censor-panel.js owns the card.
    if (globalThis.BrowserplugCensorPanel) globalThis.BrowserplugCensorPanel.init({ showStatus });

    // UI surface selector (auto-save; the background applies it live)
    document.querySelectorAll('input[name="uiSurface"]').forEach((el) => {
        el.addEventListener('change', onUiSurfaceChange);
    });
    // Mobile only; the button is display:none elsewhere, so this is a dead listener on
    // desktop rather than a missing element.
    const openOverlayBtn = document.getElementById('openOverlay');
    if (openOverlayBtn) openOverlayBtn.addEventListener('click', openExtensionOverlay);

    // Connection map (Health pane); must be armed BEFORE initTabs(): restoring the
    // Health tab as the initial tab starts the map's polling via selectTab().
    initConnectionMap();

    // Sidebar tab navigation (General / Advanced / Health)
    initTabs();

    // Fill the "Default: N" port hint from the shared constant so it can't drift
    // from the real fallback value.
    const wsPortHint = document.getElementById('wsPortHint');
    if (wsPortHint) wsPortHint.textContent = `Default: ${DEFAULT_WS_PORT}`;

    // Health pane controls (Refresh / Restart; the port they use is set in General)
    document.getElementById('healthRefresh').addEventListener('click', refreshHealth);
    initRestartControl();

    // MCP HTTP port; a connection setting, so it lives in General ▸ Server connection
    // (right under the WebSocket URL). Auto-saved on change; it drives the Health pane.
    const mcpPortInput = document.getElementById('mcpPort');
    mcpPortInput.addEventListener('input', syncHealthUrl);
    mcpPortInput.addEventListener('change', saveMcpPort);
    // Keep the polled-URL preview in sync with the hostname too.
    document.getElementById('hostname').addEventListener('input', syncHealthUrl);
    // Ask the background for the port the server ACTUALLY listens on before drawing the
    // URL previews; a relocated server must not show up here as a stale URL.
    await refreshLearnedMcpPort();
    syncHealthUrl();

    // Connection token: one field, shown/copied/validated.
    document.getElementById('tokenShow').addEventListener('click', toggleTokenVisibility);
    document.getElementById('tokenCopy').addEventListener('click', copyToken);
    document.getElementById('lanTokenCopy').addEventListener('click', copyToken);
    document.getElementById('wanTokenCopy').addEventListener('click', copyToken);
    document.getElementById('connectionToken').addEventListener('input', renderTokenHint);
    renderTokenHint();

    // Network mode: the Local / LAN / WAN panels. Picking a panel is a VIEW change;
    // only the Set button rebinds (and restarts) the server.
    document.querySelectorAll('input[name="netMode"]').forEach((el) => {
        el.addEventListener('change', onNetModeChange);
    });
    document.getElementById('netModeApply').addEventListener('click', applyNetMode);
    document.getElementById('remoteRecheck').addEventListener('click', () => renderNetworkPanels());
    document.getElementById('lanConnTest').addEventListener('click',
        () => testConnection('lanConnTest', 'lanConnTestResult'));
    document.getElementById('wanConnTest').addEventListener('click',
        () => testConnection('wanConnTest', 'wanConnTestResult'));
    document.getElementById('wanReachRefresh').addEventListener('click', () => startWanCheck());
    document.getElementById('wanCheckTemplate').addEventListener('change', onWanTemplateChange);
    document.getElementById('wanPublicAddr').addEventListener('change', onWanPublicAddrChange);
    document.getElementById('wanPublicAddr').addEventListener('input', renderWanAddress);
    // A hand-edited bind survives re-renders (dirty flag) and toggles the Apply button.
    ['lanBindHost', 'wanBindHost'].forEach((id) => {
        document.getElementById(id).addEventListener('input', (e) => {
            e.target.dataset.dirty = '1';
            renderNetModeStatus();
        });
    });
    // Sequenced: detection is gated on lastHealth (a remote browser must not show its
    // OWN public IP as the server's), so the health probe has to land first.
    renderNetworkPanels().then(detectPublicAddress);

    // The token can land WHILE this page is open (a local browser fetches it one
    // reconnect away), so fill the field when it does instead of leaving it empty.
    if (browser.storage.onChanged) {
        browser.storage.onChanged.addListener((changes, area) => {
            if (area === 'local' && changes.autoToken) loadConnectionToken();
        });
    }

    // Initial connection status check
    refreshConnectionStatus();
});

// ---------------------------------------------------------------------------
// Connection token: ONE field, holding whichever token this browser uses.
//
// It has two homes underneath, and the user should never have to think about that:
//   * storage.SYNC (connectionToken): a token you typed. Explicit intent; follows
//     your Firefox account, which is what you want for a token you carry by hand.
//   * storage.LOCAL (autoToken): one the extension fetched for itself from a
//     server on THIS machine. It must NOT ride Firefox Sync: it is a secret
//     belonging to one machine's server, and syncing it would spread it.
//
// So: the field is filled from whichever is in force, and saving writes it back to
// SYNC only when it is not the auto-fetched value (see saveSettings).
// ---------------------------------------------------------------------------

// The token the extension fetched itself, if any. Kept so saveSettings can tell an
// untouched auto token from one the user actually typed.
let autoTokenValue = null;

// Every browser holds a device token of its own, bp1.<label>.<secret>
// (shared-py/browserplug_shared/device_tokens.py); the server's master token never leaves it. A value that
// does not look like one is not rejected here, but the server will refuse it.
const DEVICE_TOKEN_RE = /^bp1\.[A-Za-z0-9_-]{1,32}\.[A-Za-z0-9_-]{43}$/;
// What a pre-1.9.1 server handed out: its one shared token.
const LEGACY_TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;

async function loadConnectionToken() {
    const field = document.getElementById('connectionToken');
    try {
        const [sync, local] = await Promise.all([
            browser.storage.sync.get({ connectionToken: '' }),
            browser.storage.local.get({ autoToken: null })
        ]);
        autoTokenValue = local.autoToken || null;
        field.value = (sync.connectionToken || '').trim() || autoTokenValue || '';
    } catch (e) {
        console.error('Error loading the connection token:', e);
    }
    renderTokenHint();
}

function renderTokenHint() {
    const value = document.getElementById('connectionToken').value.trim();
    const note = document.getElementById('tokenHint');
    if (!note) return;

    if (!value) {
        note.hidden = false;
        note.className = 'field-note warn';
        note.textContent = 'No token. A server on this machine will hand one over on the next connection; '
            + 'a server on another machine cannot. Paste its token here.';
        return;
    }
    if (LEGACY_TOKEN_RE.test(value)) {
        note.hidden = false;
        note.className = 'field-note err';
        note.textContent = 'This is a shared token from a server older than 1.9.1, which the server now refuses. '
            + 'Paste a device token (it starts with bp1.): press Copy in Options on the server\'s machine, '
            + 'or run python gen_ws_token.py --device there.';
        return;
    }
    if (!DEVICE_TOKEN_RE.test(value)) {
        note.hidden = false;
        note.className = 'field-note err';
        note.textContent = 'Not a device token. They look like bp1.<label>.<secret>, 59 or so characters of '
            + 'letters, digits, - _ and two dots. Check for a stray space or a partial paste.';
        return;
    }
    note.hidden = false;
    note.className = 'field-note ok';
    note.textContent = value === autoTokenValue
        ? 'Fetched from the server on this machine. Copy makes a new token for a browser on another one.'
        : 'Looks like a device token.';
}

function toggleTokenVisibility() {
    const field = document.getElementById('connectionToken');
    const btn = document.getElementById('tokenShow');
    const shown = field.type === 'text';
    field.type = shown ? 'password' : 'text';
    btn.textContent = shown ? 'Show' : 'Hide';
}

// A browser on another machine needs a device token of its OWN. Copying this browser's
// would let the two pose as the server to each other, which is what device tokens exist
// to prevent. So Copy asks the server on this machine for a fresh one: /ws-token answers
// loopback callers only, which is also why this works only next to the server.
async function newDeviceToken() {
    try {
        const res = await fetch(`http://127.0.0.1:${effectiveMcpPort()}/ws-token`, { cache: 'no-store' });
        if (!res.ok) return null;
        const body = await res.json();
        const token = body && typeof body.token === 'string' ? body.token.trim() : '';
        return DEVICE_TOKEN_RE.test(token) ? token : null;
    } catch (e) {
        return null;
    }
}

async function copyToken() {
    const token = await newDeviceToken();
    if (!token) {
        showStatus('Copy makes a new token for another browser, so it only works on the machine running '
            + 'the server. On that machine, run python gen_ws_token.py --device', 'error');
        return;
    }
    try {
        await navigator.clipboard.writeText(token);
        showStatus('Copied a new device token. Paste it into the other browser\'s options; '
            + 'every browser needs its own', 'success');
    } catch (e) {
        showStatus(`Could not copy. The new device token is: ${token}`, 'error');
    }
}

// ---------------------------------------------------------------------------
// Network mode: Local / LAN / WAN, where the browsers that use this server live.
//
// Three panels over ONE server-side fact (/health's ws_mode: the bind plus the
// persisted ws-mode file). Picking a panel changes nothing; each panel's
// "Set to X & Restart" button is what rebinds the server and restarts it.
// ---------------------------------------------------------------------------

function setNote(id, cls, text) {
    const note = document.getElementById(id);
    if (!note) return;
    note.hidden = false;
    note.className = `field-note ${cls}`;
    note.textContent = text;
}

// The server's answer to "where are you and how are you bound"; null when no server
// answers on this machine (expected on the remote browser itself). The 2s abort guard
// is the house rule: a fetch that never settles must not wedge a render.
async function fetchHealthSnapshot() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 2000);
    try {
        const res = await fetch(currentHealthUrl(), await serverFetchInit(currentHealthUrl(), { method: 'GET', signal: controller.signal }));
        if (res.ok) return await res.json();
    } catch (e) { /* no server here; a legitimate state, see renderNetModeStatus */
    } finally {
        clearTimeout(timer);
    }
    return null;
}

let lastHealth = null;      // last /health snapshot; null = no server answers here
let viewedNetMode = null;   // which panel is displayed; a VIEW, never persisted

// local | lan | wan: what the server says it is. ws_mode is new; against an older
// server derive the coarser answer from whether the bind accepts other machines.
function serverNetMode(health) {
    if (!health) return null;
    return health.ws_mode || (health.ws_accepts_remote ? 'lan' : 'local');
}

// Highlight the chosen network-mode card and check its radio (the uiSurface picker's
// pattern, scoped to #netModeOptions so the two radiogroups can never cross-talk).
function setNetModeSelection(mode) {
    const options = document.getElementById('netModeOptions');
    if (!options) return;
    options.querySelectorAll('.surface-card').forEach((card) => {
        const selected = card.dataset.mode === mode;
        card.classList.toggle('selected', selected);
        const input = card.querySelector('input');
        if (input) input.checked = selected;
    });
}

// Unlike onUiSurfaceChange this persists NOTHING: the panels are three views over one
// server-side fact, and only the Set button changes that fact.
function onNetModeChange(event) {
    viewedNetMode = event.target.value;
    setNetModeSelection(viewedNetMode);
    renderNetworkPanels({ refetch: false });
}

function bindInputFor(mode) {
    if (mode === 'lan') return document.getElementById('lanBindHost');
    if (mode === 'wan') return document.getElementById('wanBindHost');
    return null;    // Local has no bind input; localhost is the whole point
}

function clearBindDirty() {
    ['lanBindHost', 'wanBindHost'].forEach((id) => {
        delete document.getElementById(id).dataset.dirty;
    });
}

// Redraw everything below the picker from lastHealth (+ optionally re-ask the server).
async function renderNetworkPanels({ refetch = true } = {}) {
    const wsPort = document.getElementById('port').value || DEFAULT_WS_PORT;
    document.getElementById('lanFirewallPort').textContent = wsPort;
    document.getElementById('wanFirewallPort').textContent = wsPort;

    if (refetch) lastHealth = await fetchHealthSnapshot();
    const mode = serverNetMode(lastHealth);

    if (viewedNetMode === null) {
        // First render lands on the server's own panel. No server answering here means
        // this is probably the REMOTE browser being set up; LAN is the likeliest read.
        viewedNetMode = mode || 'lan';
    }
    setNetModeSelection(viewedNetMode);

    document.getElementById('netPanelLocal').hidden = viewedNetMode !== 'local';
    document.getElementById('netPanelLan').hidden = viewedNetMode !== 'lan';
    document.getElementById('netPanelWan').hidden = viewedNetMode !== 'wan';
    document.getElementById('wsCleartextWarning').hidden = viewedNetMode === 'local';

    // A firewall rule scoped to the wrong profile blocks the LAN silently, and from the
    // server's own machine no check on this page can see it: the connection test dials the
    // Hostname field from this browser, and a connection that starts on the server's
    // machine never meets the inbound rule. Ask the server instead, whenever the LAN panel
    // is on screen; wan-diagnosis.js owns the rest, including the rate limit that keeps a PowerShell
    // round-trip off the health poll.
    if (viewedNetMode === 'lan') {
        document.dispatchEvent(new CustomEvent('browserplug:lan-firewall-check'));
    }

    const envName = (lastHealth && lastHealth.ws_host_env) || 'BROWSERPLUG_WS_HOST';
    document.getElementById('remoteBindEnv').textContent = envName;
    document.getElementById('wanBindEnv').textContent = envName;

    // An env-set bind wins at every start: show its VALUE, disabled. An editable
    // field the server would ignore is worse than a locked one that tells the truth.
    const envBound = !!(lastHealth && lastHealth.ws_host_source === 'env');
    ['lanBindHost', 'wanBindHost'].forEach((id) => {
        const input = document.getElementById(id);
        input.disabled = envBound;
        if (envBound) input.value = lastHealth.ws_host;
    });
    // Otherwise reflect the live bind in the matching panel's input, but never over
    // a value the user is editing (dirty flag) or has focused right now.
    const liveInput = bindInputFor(mode);
    if (!envBound && liveInput && lastHealth && lastHealth.ws_accepts_remote
            && !liveInput.dataset.dirty && document.activeElement !== liveInput) {
        liveInput.value = lastHealth.ws_host;
    }

    renderNetModeStatus();
    renderLanAddresses();
    renderWanAddress();
    maybeStartWanCheck();
}

// The shared status line + the one button that changes anything.
function renderNetModeStatus() {
    // Mid-Set, applyNetMode owns both the note (busy) and the button (disabled);
    // a re-render must not overwrite the one or re-enable the other.
    if (netModeApplyBusy) return;
    const btn = document.getElementById('netModeApply');
    const mode = serverNetMode(lastHealth);

    if (!lastHealth) {
        // No server answering HERE. Expected on the remote browser itself; the
        // columns still describe the server's machine; this one just points at it.
        setNote('netModeState', 'warn', 'No server is answering on this machine; these steps are for a browser '
            + 'running next to the server. On this one, fill in its address and token below.');
        btn.hidden = true;
        return;
    }

    const label = NET_MODE_LABELS[mode] || mode;
    const envBound = lastHealth.ws_host_source === 'env';
    let text = `The server is currently configured as: ${label} (bound to ${lastHealth.ws_host || 'localhost'}).`;
    if (envBound) {
        text += ` Set by ${lastHealth.ws_host_env} in its environment, which always wins; `
            + 'it cannot be changed from this page. Unset it to control the bind from here.';
    }
    setNote('netModeState', mode === viewedNetMode ? 'ok' : 'warn', text);

    if (envBound) {
        btn.hidden = true;
        return;
    }
    if (mode !== viewedNetMode) {
        btn.hidden = false;
        btn.disabled = false;
        btn.textContent = `Set to ${NET_MODE_LABELS[viewedNetMode]} & Restart`;
        return;
    }
    // Mode already matches, but a hand-edited Advanced bind may still differ from
    // the live one, and applying THAT is a restart too.
    const input = bindInputFor(viewedNetMode);
    const edited = input && input.value.trim() && input.value.trim() !== lastHealth.ws_host;
    btn.hidden = !edited;
    if (edited) {
        btn.disabled = false;
        btn.textContent = 'Apply bind & Restart';
    }
}

// The LAN panel's address list: which of the server machine's addresses the remote
// browser should dial, each labeled with its adapter so the real NIC stands out from
// the WSL/Hyper-V/Docker virtuals. 192.168.* is highlighted and sorted first.
function renderLanAddresses() {
    const box = document.getElementById('lanAddrList');
    if (!lastHealth) {
        box.replaceChildren(makeCode('no server here; use the address of the machine running it', true));
        return;
    }
    const wsPort = lastHealth.ws_port || document.getElementById('port').value || DEFAULT_WS_PORT;
    const rows = Array.isArray(lastHealth.ws_lan_addresses) ? lastHealth.ws_lan_addresses : [];
    const urls = Array.isArray(lastHealth.ws_lan_urls) ? lastHealth.ws_lan_urls : [];

    box.replaceChildren();
    if (rows.length) {
        rows.forEach((row) => {
            const div = document.createElement('div');
            div.className = row.recommended ? 'lan-addr recommended' : 'lan-addr';
            if (row.recommended) div.title = 'A home-router (192.168.*) address; usually the right one';
            if (row.interface) {
                const name = document.createElement('span');
                name.className = 'lan-if';
                name.textContent = `${row.interface} —`;
                div.appendChild(name);
            }
            div.appendChild(makeCode(`ws://${row.ip}:${wsPort}`));
            box.appendChild(div);
        });
    } else if (urls.length) {
        // An older server (or one whose adapter lookup failed) reports bare URLs.
        urls.forEach((url) => box.appendChild(makeCode(url)));
    } else {
        box.appendChild(makeCode('this machine has no LAN address; reach it through a tunnel instead', true));
    }
}

function makeCode(text, muted = false) {
    const code = document.createElement('code');
    if (muted) code.className = 'muted';
    code.textContent = text;
    return code;
}

// The Set button: rebind the server's WebSocket to the viewed panel's posture and
// restart it onto that address, then watch it come back.
//
// POST /ws-bind carries the token in a header: the endpoint is on localhost, and any
// web page can POST to localhost, so the server demands something a page cannot send.

// In-flight latch: while a Set is running, a Recheck/panel-switch re-render must not
// resurrect the button (double-click = a second restart racing the first) or clobber
// the busy note with a status line.
let netModeApplyBusy = false;

async function applyNetMode() {
    if (netModeApplyBusy) return;
    const desired = viewedNetMode;
    const btn = document.getElementById('netModeApply');
    const input = bindInputFor(desired);
    const host = desired === 'local' ? 'localhost' : ((input && input.value.trim()) || '0.0.0.0');

    const token = document.getElementById('connectionToken').value.trim();
    if (!token) {
        setNote('netModeState', 'err', 'The connection token is needed to change the bind, and this page has none. '
            + 'Do this on a browser that is already connected to the server.');
        return;
    }

    const oldPid = lastHealth && lastHealth.server_pid;
    const label = NET_MODE_LABELS[desired] || desired;
    setNote('netModeState', 'busy', `Switching to ${label} (${host}) and restarting the server…`);
    netModeApplyBusy = true;
    btn.disabled = true;
    try {
        try {
            // Same abort guard as every other fetch here: a wedged server must fail
            // the flow in 10s, not hold the button hostage forever.
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 10000);
            let res;
            // Local never sends a mode; it is derived from the bind, and the
            // server clears the stored LAN/WAN choice on a revert. The v2 signature covers
            // these exact bytes, so a request in transit cannot swap them; only a server
            // that predates v2 gets them under v1 (postSignedBody).
            const bindBody = JSON.stringify(desired === 'local' ? { host } : { host, mode: desired });
            try {
                res = await postSignedBody(currentServerUrl('/ws-bind'), token, bindBody, controller.signal);
            } finally {
                clearTimeout(timer);
            }
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                setNote('netModeState', 'err', body.error || `The server refused (HTTP ${res.status}).`);
                return;
            }
        } catch (e) {
            setNote('netModeState', 'err', `Could not reach the server: ${e.message}`);
            return;
        }

        // It acks, then tears itself down gracefully (drain, close, stop) before a
        // replacement rebinds the ports — up to ~20s before the new process even
        // starts. Success needs BOTH the desired mode AND a new pid: the OLD process
        // already reports the new mode in its last moment, and declaring victory
        // against a server about to exit would race the restart it just asked for.
        for (let i = 0; i < RESTART_WAIT_SECONDS * 2; i++) {
            await new Promise((r) => setTimeout(r, 500));
            // The replacement may come back on a different MCP port (the walk).
            await refreshLearnedMcpPort();
            const health = await fetchHealthSnapshot();
            if (health && serverNetMode(health) === desired
                    && (!oldPid || health.server_pid !== oldPid)) {
                lastHealth = health;
                clearBindDirty();
                netModeApplyBusy = false;   // before the render, or it skips the status line
                renderNetworkPanels({ refetch: false });
                // The bind just moved, so the cached verdict is about the old one.
                document.dispatchEvent(new CustomEvent('browserplug:lan-firewall-check',
                    { detail: { force: true } }));
                showStatus(`Server is back up as ${label}`, 'success');
                return;
            }
        }
        setNote('netModeState', 'err', 'The server did not come back within 10s. Check its log.');
    } finally {
        netModeApplyBusy = false;
        btn.disabled = false;
    }
}

// --- WAN "Available online": prove the internet can actually reach this server -----

let wanCheck = { generation: 0, timer: null, lastTarget: null };

function wanReachState(cls, text) {
    const indicator = document.getElementById('wanReachIndicator');
    indicator.classList.remove('connected', 'disconnected', 'degraded');
    if (cls) indicator.classList.add(cls);
    document.getElementById('wanReachText').textContent = text;
}

// What the external nodes are asked to GET: the server's one deliberately token-free
// endpoint, on the WS port. The token never rides in this URL; that is the point.
function wanCheckTarget() {
    const addr = document.getElementById('wanPublicAddr').value.trim();
    if (!addr) return null;
    const port = document.getElementById('port').value || DEFAULT_WS_PORT;
    return `http://${addr}:${port}/reachable`;
}

function checkTemplate() {
    const custom = (document.getElementById('wanCheckTemplate').value || '').trim();
    return custom || DEFAULT_CHECK_TEMPLATE;
}

// GET returning parsed JSON or null, with the Accept header (or check-host answers
// HTML) and the house abort guard.
async function fetchJson(url, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const res = await fetch(url, {
            headers: { 'Accept': 'application/json' },
            cache: 'no-store',
            signal: controller.signal
        });
        if (res.ok) return await res.json();
    } catch (e) { /* unreachable / not JSON / aborted; all read as null */
    } finally {
        clearTimeout(timer);
    }
    return null;
}

// Auto-run: only when the WAN panel is showing, and only when the target changed;
// switching panels back and forth must not hammer the check service.
function maybeStartWanCheck() {
    if (viewedNetMode !== 'wan') return;
    const target = wanCheckTarget();
    if (!target || target === wanCheck.lastTarget) return;
    startWanCheck();
}

async function startWanCheck() {
    const gen = ++wanCheck.generation;   // supersedes any check still in flight
    clearTimeout(wanCheck.timer);
    document.getElementById('wanReachLink').hidden = true;
    document.dispatchEvent(new CustomEvent('browserplug:wan-check-reset'));

    const target = wanCheckTarget();
    wanCheck.lastTarget = target;
    if (!target) {
        wanReachState(null, 'Not checked; no public address yet');
        return;
    }

    const template = checkTemplate();
    if (!template.includes('{target}')) {
        wanReachState('disconnected', 'Invalid check URL template; it must contain {target}');
        return;
    }
    // split/join, not replace(): a template may use {target} more than once, and
    // String.replace with a string pattern would fill only the first.
    const url = template.split('{target}').join(encodeURIComponent(target));
    let origin;
    try {
        origin = new URL(url).origin;   // parse AFTER substitution; the raw template may not
    } catch (e) {
        wanReachState('disconnected', 'Invalid check URL template');
        return;
    }

    wanReachState('degraded', 'Checking…');
    const started = await fetchJson(url, 10000);
    if (gen !== wanCheck.generation) return;   // superseded while we waited
    if (!started || !started.ok || !started.request_id) {
        wanReachState('disconnected', 'Check failed; the check service did not accept the request');
        return;
    }
    // The link comes from the check service's JSON; with a CUSTOM template that is an
    // arbitrary server, so only ever render web schemes (no javascript:, no data:).
    if (typeof started.permanent_link === 'string' && /^https?:\/\//i.test(started.permanent_link)) {
        const link = document.getElementById('wanReachLink');
        link.href = started.permanent_link;
        link.hidden = false;
    }
    pollWanCheck(gen, origin, started.request_id, Date.now());
}

async function pollWanCheck(gen, origin, requestId, startedAt) {
    if (gen !== wanCheck.generation) return;
    const results = await fetchJson(`${origin}/check-result/${encodeURIComponent(requestId)}`, 10000);
    if (gen !== wanCheck.generation) return;

    if (results) {
        const nodes = Object.values(results);
        // Per node: null = still working; an HTTP result is [[success, time, message,
        // status, ip]]. One node seeing our 200 is proof enough; the others may sit
        // behind filters of their own.
        const hit = nodes.some((n) => Array.isArray(n) && Array.isArray(n[0]) && String(n[0][3]) === '200');
        const pending = nodes.some((n) => n === null);
        if (hit) {
            wanReachState('connected', 'Available online');
            document.dispatchEvent(new CustomEvent('browserplug:wan-check-reset'));
            return;
        }
        if (nodes.length && !pending) {
            wanReachState('disconnected', 'Not reachable; check the firewall and the router port-forward');
            document.dispatchEvent(new CustomEvent('browserplug:wan-unreachable'));
            return;
        }
    }
    if (Date.now() - startedAt > WAN_CHECK_TIMEOUT_MS) {
        wanReachState('disconnected', 'Not reachable (the check timed out)');
        document.dispatchEvent(new CustomEvent('browserplug:wan-unreachable'));
        return;
    }
    wanCheck.timer = setTimeout(() => pollWanCheck(gen, origin, requestId, startedAt), WAN_CHECK_POLL_MS);
}

// --- The WAN panel's public address: detected once, overridable for DDNS -----------

function renderWanAddress() {
    const addr = document.getElementById('wanPublicAddr').value.trim();
    const port = document.getElementById('port').value || DEFAULT_WS_PORT;
    document.getElementById('wanPublicUrl').replaceChildren(
        addr ? makeCode(`ws://${addr}:${port}`) : makeCode('ws://…', true));
}

async function detectPublicAddress() {
    const field = document.getElementById('wanPublicAddr');
    try {
        const stored = await browser.storage.sync.get({ wanPublicAddress: '' });
        if (stored.wanPublicAddress) {
            // An override (a DDNS name, a known static IP) beats detection outright.
            field.value = stored.wanPublicAddress;
            renderWanAddress();
            maybeStartWanCheck();
            return;
        }
    } catch (e) { /* storage unavailable; fall through to detection */ }

    if (field.value.trim()) return;   // never overwrite something the user typed
    if (!lastHealth) {
        // No server on THIS machine: detecting would fill in this browser's own public
        // IP, the wrong machine's. The field wants the SERVER's address, which only
        // its operator knows; leave it to them (or to a synced override above).
        field.placeholder = "enter the server machine's public IP or DDNS name";
        return;
    }
    for (const url of PUBLIC_IP_URLS) {
        const data = await fetchJson(url, 6000);
        if (field.value.trim()) return;   // …not even one typed while we waited
        if (data && data.ip) {
            field.value = data.ip;
            renderWanAddress();
            maybeStartWanCheck();
            return;
        }
    }
    field.placeholder = 'could not detect; enter your public IP or DDNS name';
}

// Auto-saved on change (like the UI-surface picker), and a saved value re-runs the
// check: the address is both what the remote browser dials and what the check probes.
async function onWanPublicAddrChange() {
    const value = document.getElementById('wanPublicAddr').value.trim();
    try {
        if (value) await browser.storage.sync.set({ wanPublicAddress: value });
        else await browser.storage.sync.remove('wanPublicAddress');
    } catch (error) {
        console.error('Error saving the public address:', error);
    }
    renderWanAddress();
    if (value) startWanCheck();
    else detectPublicAddress();   // cleared = back to auto-detect
}

async function onWanTemplateChange() {
    const value = document.getElementById('wanCheckTemplate').value.trim();
    if (value && !value.includes('{target}')) {
        // Refused, not saved: a template the checker cannot fill is dead weight.
        wanReachState('disconnected', 'Invalid check URL template; it must contain {target}');
        return;
    }
    try {
        if (value) await browser.storage.sync.set({ wanCheckTemplate: value });
        else await browser.storage.sync.remove('wanCheckTemplate');
        showStatus('Check URL template saved', 'success');
    } catch (error) {
        console.error('Error saving the check template:', error);
        showStatus('Error saving the check template', 'error');
    }
    startWanCheck();
}

// Does this browser actually reach that server? Opens a WebSocket to the configured
// address exactly like the background script does. A successful handshake proves the
// three things people get wrong at once: the server runs, its bind allows this
// machine, and the firewall lets the port through. It sends NOTHING and closes
// immediately; an unauthenticated socket that says nothing never enters the
// server's browser registry, so probing cannot disturb a live session.
async function testConnection(btnId = 'lanConnTest', noteId = 'lanConnTestResult') {
    const host = document.getElementById('hostname').value.trim() || 'localhost';
    const port = document.getElementById('port').value || DEFAULT_WS_PORT;
    const url = `ws://${host}:${port}`;
    const btn = document.getElementById(btnId);

    setNote(noteId, 'busy', `Opening a WebSocket to ${url}…`);
    btn.disabled = true;
    try {
        const outcome = await new Promise((resolve) => {
            let settled = false;
            const done = (v) => { if (!settled) { settled = true; resolve(v); } };
            let socket;
            try {
                socket = new WebSocket(url);
            } catch (e) {
                done({ ok: false, detail: e.message });
                return;
            }
            const timer = setTimeout(() => {
                try { socket.close(); } catch (e) { /* already gone */ }
                done({ ok: false, detail: 'timed out after 5s' });
            }, 5000);
            socket.onopen = () => {
                clearTimeout(timer);
                try { socket.close(); } catch (e) { /* fine */ }
                done({ ok: true });
            };
            socket.onclose = (ev) => {
                clearTimeout(timer);
                // 4401 = the server is there and demanding a token: reachable, which is
                // exactly what this test is for. The token itself is proven by the real
                // connection (see the Connection status card).
                if (ev.code === 4401) done({ ok: true, note: 'it asked for a token' });
                else done({ ok: false, detail: `closed (code ${ev.code})` });
            };
            socket.onerror = () => {
                clearTimeout(timer);
                done({ ok: false, detail: 'the connection was refused or never answered' });
            };
        });

        if (outcome.ok) {
            setNote(noteId, 'ok', `Reached the server at ${url}`
                + (outcome.note ? `; ${outcome.note}, so the token above must match.` : '.'));
        } else {
            setNote(noteId, 'err', `Could not reach ${url}; ${outcome.detail}. `
                + 'Check that the server is running, that its mode allows other machines '
                + '(the Set button above), and that the firewall lets the port through.');
        }
    } finally {
        btn.disabled = false;
    }
}

async function loadSettings() {
    try {
        const result = await browser.storage.sync.get({
            hostname: 'localhost',
            port: DEFAULT_WS_PORT,
            // Shared secret for servers that accept connections beyond localhost.
            // A secret: it is never logged, and never sent to the background's CONFIG
            // (which IS console-logged); background.js reads it from storage itself.
            connectionToken: '',
            retryInterval: 5000,
            maxRetries: -1,
            pingTimeout: 5000,
            // Interface options
            uiSurface: 'popup',
            hideEmptySessions: true,
            toolCallSort: 'bottom',
            // Provider-specific (Anthropic usage bar)
            anthropicDisplayMode: PROVIDER_MODE_DEFAULTS.anthropicDisplayMode,
            anthropicPeakLeadMinutes: 0,
            anthropicUsageRefreshMinutes: 5,
            anthropicShowWeekly: false,
            anthropicBackgroundRefresh: false,
            // Provider-specific (OpenAI usage bar; off by default, see PROVIDER_MODE_DEFAULTS)
            openaiDisplayMode: PROVIDER_MODE_DEFAULTS.openaiDisplayMode,
            openaiUsageRefreshMinutes: 5,
            openaiShowWeekly: false,
            openaiBackgroundRefresh: false,
            // Provider-specific (GitHub Copilot quotas)
            copilotDisplayMode: PROVIDER_MODE_DEFAULTS.copilotDisplayMode,
            copilotUsageRefreshMinutes: 5,
            copilotShowCompletions: false,
            copilotBackgroundRefresh: false,
            // Provider-specific (DeepSeek price clock)
            deepseekDisplayMode: PROVIDER_MODE_DEFAULTS.deepseekDisplayMode,
            deepseekPeakLeadMinutes: 0,
            // Kilo sessions
            kiloLoadDetails: true,
            kiloClarifyNames: true,
            // Health pane
            mcpPort: DEFAULT_MCP_PORT,
            // Network mode (WAN panel); auto-saved on change, never via Save
            wanCheckTemplate: '',
            wanPublicAddress: '',
            // Test configuration overrides
            testPort: null,
            testHostname: null
        });

        // Use test overrides if they exist, otherwise use regular config
        const displayHostname = result.testHostname || result.hostname;
        const displayPort = result.testPort || result.port;

        document.getElementById('hostname').value = displayHostname;
        document.getElementById('port').value = displayPort;
        // The token field is filled by loadConnectionToken(): it may hold the value the
        // extension fetched for itself (storage.local), which is NOT in `result`.
        await loadConnectionToken();
        document.getElementById('retryInterval').value = result.retryInterval;
        document.getElementById('maxRetries').value = result.maxRetries;
        document.getElementById('pingTimeout').value = result.pingTimeout;
        document.getElementById('hideEmptySessions').checked = result.hideEmptySessions;
        document.getElementById('toolCallSort').value = result.toolCallSort;
        document.getElementById('mcpPort').value = result.mcpPort;
        setProviderMode('anthropicDisplayMode', result.anthropicDisplayMode);
        document.getElementById('anthropicPeakLeadMinutes').value = result.anthropicPeakLeadMinutes;
        document.getElementById('anthropicUsageRefreshMinutes').value = result.anthropicUsageRefreshMinutes;
        document.getElementById('anthropicShowWeekly').checked = result.anthropicShowWeekly;
        document.getElementById('anthropicBackgroundRefresh').checked = result.anthropicBackgroundRefresh;
        setProviderMode('openaiDisplayMode', result.openaiDisplayMode);
        document.getElementById('openaiUsageRefreshMinutes').value = result.openaiUsageRefreshMinutes;
        document.getElementById('openaiShowWeekly').checked = result.openaiShowWeekly;
        document.getElementById('openaiBackgroundRefresh').checked = result.openaiBackgroundRefresh;
        setProviderMode('copilotDisplayMode', result.copilotDisplayMode);
        document.getElementById('copilotUsageRefreshMinutes').value = result.copilotUsageRefreshMinutes;
        document.getElementById('copilotShowCompletions').checked = result.copilotShowCompletions;
        document.getElementById('copilotBackgroundRefresh').checked = result.copilotBackgroundRefresh;
        syncRefreshLabels();
        setProviderMode('deepseekDisplayMode', result.deepseekDisplayMode);
        document.getElementById('deepseekPeakLeadMinutes').value = result.deepseekPeakLeadMinutes;
        document.getElementById('kiloLoadDetails').checked = result.kiloLoadDetails;
        document.getElementById('kiloClarifyNames').checked = result.kiloClarifyNames;
        if (globalThis.BrowserplugCensorPanel) await globalThis.BrowserplugCensorPanel.load();
        document.getElementById('wanCheckTemplate').value = result.wanCheckTemplate;
        // The stored override (or ''), so a Reset actually empties the field. Live
        // detection is detectPublicAddress()'s job and runs after this bulk fill.
        document.getElementById('wanPublicAddr').value = result.wanPublicAddress;
        syncKiloSubState();
        syncProviderSubState();
        // uiSurface is a SYNCED key, so a phone inherits whatever the desktop chose.
        // Three of the four surfaces do not exist on Android and their cards are
        // hidden there, so a stored 'tab' left the picker with nothing selected and
        // the background clearing the popup. On a phone the answer is always Overlay.
        setSurfaceSelection(isAndroid() ? 'popup' : result.uiSurface);
        syncHealthUrl();

        // Show warning if test overrides are active
        if (result.testPort || result.testHostname) {
            showStatus(`Using test configuration overrides (port: ${result.testPort || 'default'}, hostname: ${result.testHostname || 'default'})`, 'success');
        }

        updateWebSocketUrl();
    } catch (error) {
        console.error('Error loading settings:', error);
        showStatus('Error loading settings', 'error');
    }
}

async function saveSettings(event) {
    event.preventDefault();

    const hostname = document.getElementById('hostname').value.trim();
    const port = parseInt(document.getElementById('port').value);
    const connectionToken = document.getElementById('connectionToken').value.trim();

    if (!hostname) {
        showStatus('Hostname is required', 'error');
        return;
    }

    // isNaN first: an emptied/garbage number input parses to NaN, and NaN fails
    // no range comparison; without this it would be saved and become ws://host:NaN.
    if (isNaN(port) || port < 1 || port > 65535) {
        showStatus('Port must be a number between 1 and 65535', 'error');
        return;
    }

    try {
        // Get existing configuration to preserve test overrides
        const existingConfig = await browser.storage.sync.get({
            hostname: 'localhost',
            port: DEFAULT_WS_PORT,
            testPort: null,
            testHostname: null,
            retryInterval: 5000,
            maxRetries: -1,
            pingTimeout: 5000
        });

        // Test overrides FILL these form fields at load (loadSettings shows
        // testHostname/testPort when set), so saving the form untouched would
        // silently promote a temporary test value into the real config, the same
        // leak the token field guards against with autoTokenValue. A displayed
        // override the user left as-is keeps the STORED value; only a value they
        // actually typed replaces it.
        const overrideHostname = existingConfig.testHostname ? String(existingConfig.testHostname) : null;
        const overridePort = existingConfig.testPort ? parseInt(existingConfig.testPort, 10) : null;
        const hostnameIsUntouchedOverride = overrideHostname !== null && hostname === overrideHostname;
        const portIsUntouchedOverride = overridePort !== null && port === overridePort;
        const hostnameToSave = hostnameIsUntouchedOverride ? existingConfig.hostname : hostname;
        const portToSave = portIsUntouchedOverride ? existingConfig.port : port;
        const overridesActive = !!(existingConfig.testPort || existingConfig.testHostname);

        // Save the new settings while preserving test overrides
        await browser.storage.sync.set({
            hostname: hostnameToSave,
            port: portToSave,
            // Preserve existing test overrides and other settings
            testPort: existingConfig.testPort,
            testHostname: existingConfig.testHostname,
            retryInterval: existingConfig.retryInterval,
            maxRetries: existingConfig.maxRetries,
            pingTimeout: existingConfig.pingTimeout
        });

        // The token is stored separately, and WHERE depends on whose it is:
        //
        //   * the value the extension fetched itself (autoTokenValue) stays where it is,
        //     in storage.LOCAL. Writing it to the synced key would hand a secret that
        //     belongs to ONE machine's server to every profile the user syncs; the one
        //     thing this design must not do. Saving the form with the field untouched is
        //     the common case, and it must not be a leak.
        //   * anything else is a token the user typed for a server they are pointing at
        //     by hand: explicit intent, so it goes to storage.SYNC and follows them.
        //
        // An empty field leaves the key UNSET, never '': background.js omits the token
        // from its hello only when there is no value, and a server that requires one must
        // never see an empty answer.
        if (connectionToken && connectionToken !== autoTokenValue) {
            await browser.storage.sync.set({ connectionToken });
        } else {
            await browser.storage.sync.remove('connectionToken');
        }

        // Notify background script of configuration change. The token is deliberately
        // NOT in this payload: it would land in CONFIG, which is console-logged and
        // handed to the popup. The background re-reads it from storage on each hello,
        // and this message reconnects the socket, so a changed token takes effect now.
        //
        // While a test override is active NO message is sent at all: the background's
        // updateConfig() persists its running CONFIG, whose hostname/port ARE the
        // override values, back into the real storage keys on every config message,
        // which would undo the preservation above. The background keeps dialing the
        // override anyway; values saved here take effect once the overrides are cleared.
        if (!overridesActive) {
            await browser.runtime.sendMessage({
                type: 'configUpdated',
                config: { hostname, port }
            });
        }

        renderTokenHint();        // re-judge the value that was just saved
        renderNetworkPanels();    // a new host/port means a different server to ask
        reportUnsavedConnectionEdits();   // nothing is unsaved now; clear the warning

        if (overridesActive) {
            showStatus('Settings saved! Test overrides are still active; the extension keeps '
                + 'using them, and displayed override values were NOT written into your saved config.', 'success');
        } else {
            showStatus('Settings saved successfully!', 'success');
        }
    } catch (error) {
        console.error('Error saving settings:', error);
        showStatus('Error saving settings', 'error');
    }
}

async function saveAdvancedSettings() {
    const retryInterval = parseInt(document.getElementById('retryInterval').value);
    const maxRetries = parseInt(document.getElementById('maxRetries').value);
    const pingTimeout = parseInt(document.getElementById('pingTimeout').value);

    // isNaN first on each (same guard as saveMcpPort): an emptied/garbage number
    // input parses to NaN, which fails no range comparison. Persisting NaN here is
    // not cosmetic: the background runs setTimeout(reconnect, CONFIG.retryInterval),
    // and setTimeout treats NaN as 0ms: a reconnect-hammering loop that survives
    // browser restarts because the value is loaded back from storage.
    if (isNaN(retryInterval) || retryInterval < 1000 || retryInterval > 60000) {
        showStatus('Retry interval must be a number between 1000 and 60000ms', 'error');
        return;
    }

    if (isNaN(maxRetries) || maxRetries < -1 || maxRetries > 100) {
        showStatus('Max retries must be a number: -1 or between 0 and 100', 'error');
        return;
    }

    if (isNaN(pingTimeout) || pingTimeout < 1000 || pingTimeout > 30000) {
        showStatus('Ping timeout must be a number between 1000 and 30000ms', 'error');
        return;
    }

    try {
        // Get existing configuration to preserve all settings including test overrides
        const existingConfig = await browser.storage.sync.get({
            hostname: 'localhost',
            port: 8772,
            testPort: null,
            testHostname: null
        });

        await browser.storage.sync.set({
            retryInterval: retryInterval,
            maxRetries: maxRetries,
            pingTimeout: pingTimeout,
            // Preserve existing configuration
            hostname: existingConfig.hostname,
            port: existingConfig.port,
            testPort: existingConfig.testPort,
            testHostname: existingConfig.testHostname
        });

        // Notify background script of advanced configuration change.
        // Withheld while a test override is active, for the same reason as in
        // saveSettings: ANY config message makes the background's updateConfig()
        // persist its running CONFIG, whose hostname/port ARE the override values,
        // back into the real storage keys, clobbering the user's saved config.
        if (!existingConfig.testPort && !existingConfig.testHostname) {
            await browser.runtime.sendMessage({
                type: 'advancedConfigUpdated',
                config: { retryInterval, maxRetries, pingTimeout }
            });
        }

        showStatus('Advanced settings saved successfully!', 'success');
    } catch (error) {
        console.error('Error saving advanced settings:', error);
        showStatus('Error saving advanced settings', 'error');
    }
}

// Kilo-session options (auto-saved). The "clarify names" sub-option only makes
// sense when detail-loading is on, so it's disabled (greyed) otherwise.
function syncKiloSubState() {
    const on = document.getElementById('kiloLoadDetails').checked;
    const sub = document.getElementById('kiloClarifyNames');
    sub.disabled = !on;
    const label = sub.closest('.checkbox-label');
    if (label) label.style.opacity = on ? '' : '0.5';
}

async function saveKiloSettings() {
    const kiloLoadDetails = document.getElementById('kiloLoadDetails').checked;
    const kiloClarifyNames = document.getElementById('kiloClarifyNames').checked;
    try {
        await browser.storage.sync.set({ kiloLoadDetails, kiloClarifyNames });
        showStatus('Kilo settings saved', 'success');
    } catch (error) {
        console.error('Error saving Kilo settings:', error);
        showStatus('Error saving Kilo settings', 'error');
    }
}

// Where each provider's indicator is allowed to appear: nowhere (and, for a provider
// that fetches, no fetch either); on the popup's Usage tab only; or on the Usage tab and
// on the row of every connected client of that kind. Same three on every card, so no
// card needs its own explanation. Whether anything is fetched with the popup closed is a
// separate checkbox (<provider>BackgroundRefresh), not a fourth mode.
const PROVIDER_DISPLAY_MODES = ['disabled', 'tab', 'client'];

// The mode each card ships with. popup.js keeps the same table and falls back to it for a
// value it does not know, so both pages draw such a card the same way. OpenAI ships off:
// it needs a signed-in chatgpt.com profile, and a panel that can only say "not signed
// in" is not a default worth shipping.
const PROVIDER_MODE_DEFAULTS = {
    anthropicDisplayMode: 'client',
    openaiDisplayMode: 'disabled',
    copilotDisplayMode: 'client',
    deepseekDisplayMode: 'disabled',
};

// select id -> the fields that only mean something while that panel is on. Hidden with
// the whole .field (label and help text), not greyed: a card whose provider is off shows
// its select and nothing under it.
const PROVIDER_SUB_FIELDS = {
    anthropicDisplayMode: ['anthropicBackgroundRefresh', 'anthropicPeakLeadMinutes',
                           'anthropicUsageRefreshMinutes', 'anthropicShowWeekly'],
    openaiDisplayMode: ['openaiBackgroundRefresh', 'openaiUsageRefreshMinutes', 'openaiShowWeekly'],
    copilotDisplayMode: ['copilotBackgroundRefresh', 'copilotUsageRefreshMinutes', 'copilotShowCompletions'],
    deepseekDisplayMode: ['deepseekPeakLeadMinutes'],
};

// The "Query every N min" checkbox quotes the refresh-rate field below it. It follows
// every keystroke and shows the value a save would store (clamped to 1-60, 5 when
// blank), not the raw text in the box.
function syncRefreshLabels() {
    for (const span of document.querySelectorAll('[data-refresh-minutes-of]')) {
        const input = document.getElementById(span.dataset.refreshMinutesOf);
        if (!input) continue;
        const v = parseInt(input.value, 10);
        span.textContent = String(isNaN(v) ? 5 : Math.min(60, Math.max(1, v)));
    }
}

// A stored value from a newer build (or a hand-edited profile) must not leave the
// select showing nothing; fall back to the mode the card ships with. Not to the first
// option: that is Disabled, and the next save of any provider setting would store it.
function setProviderMode(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = PROVIDER_DISPLAY_MODES.includes(value) ? value : (PROVIDER_MODE_DEFAULTS[id] || 'disabled');
}

function providerMode(id) {
    const el = document.getElementById(id);
    const v = el && el.value;
    return PROVIDER_DISPLAY_MODES.includes(v) ? v : 'disabled';
}

// Each card's help text promises that Disabled stops the requests to its site, and the
// sign-in line comes from a forced usage read, which is one of them. So a card's sign-in
// is checked only while the card is on, starting the moment it is switched on; Re-check
// asks whatever the mode.
const PROVIDER_AUTH_CHECKS = {
    anthropicDisplayMode: { prefix: 'anthropic', site: 'claude.ai', check: () => refreshAnthropicAuth() },
    openaiDisplayMode: { prefix: 'openai', site: 'chatgpt.com', check: () => refreshOpenAIAuth() },
    copilotDisplayMode: { prefix: 'copilot', site: 'github.com', check: () => refreshCopilotAuth() },
};

function refreshProviderAuthIfOn(modeId) {
    const card = PROVIDER_AUTH_CHECKS[modeId];
    if (!card) return;
    if (providerMode(modeId) !== 'disabled') {
        card.check();
        return;
    }
    const dot = document.getElementById(`${card.prefix}AuthDot`);
    const text = document.getElementById(`${card.prefix}AuthText`);
    const help = document.getElementById(`${card.prefix}AuthHelp`);
    const signIn = document.getElementById(`${card.prefix}SignIn`);
    if (!dot || !text) return;
    dot.className = 'token-state-dot';
    text.textContent = `Not checked: this card is Disabled, so nothing is sent to ${card.site}.`;
    if (help) help.textContent = 'Re-check asks anyway.';
    if (signIn) signIn.hidden = true;
}

function syncProviderSubState() {
    for (const [selectId, fields] of Object.entries(PROVIDER_SUB_FIELDS)) {
        if (!document.getElementById(selectId)) continue;
        const on = providerMode(selectId) !== 'disabled';
        for (const id of fields) {
            const sub = document.getElementById(id);
            if (!sub) continue;
            if ('disabled' in sub) sub.disabled = !on;
            const wrap = sub.closest('.field');
            if (wrap) wrap.hidden = !on;
        }
    }
}

// Provider-specific settings (Anthropic usage bar). Auto-saved on change and
// clamped to their valid ranges; the popup + background react live (popup reads
// on its 2s refresh, background via storage.onChanged for the refresh rate).
async function saveProviderSettings() {
    const clampInt = (id, def, min, max) => {
        let v = parseInt(document.getElementById(id).value, 10);
        if (isNaN(v)) v = def;
        v = Math.min(max, Math.max(min, v));
        document.getElementById(id).value = v; // reflect the clamped value back
        return v;
    };
    const anthropicPeakLeadMinutes = clampInt('anthropicPeakLeadMinutes', 0, 0, 240);
    const anthropicUsageRefreshMinutes = clampInt('anthropicUsageRefreshMinutes', 5, 1, 60);
    const anthropicShowWeekly = document.getElementById('anthropicShowWeekly').checked;
    const anthropicBackgroundRefresh = document.getElementById('anthropicBackgroundRefresh').checked;
    const openaiUsageRefreshMinutes = clampInt('openaiUsageRefreshMinutes', 5, 1, 60);
    const openaiShowWeekly = document.getElementById('openaiShowWeekly').checked;
    const openaiBackgroundRefresh = document.getElementById('openaiBackgroundRefresh').checked;
    const copilotUsageRefreshMinutes = clampInt('copilotUsageRefreshMinutes', 5, 1, 60);
    const copilotShowCompletions = document.getElementById('copilotShowCompletions').checked;
    const copilotBackgroundRefresh = document.getElementById('copilotBackgroundRefresh').checked;
    const deepseekPeakLeadMinutes = clampInt('deepseekPeakLeadMinutes', 0, 0, 240);
    syncRefreshLabels();
    try {
        // set() merges into existing storage, so no need to re-read/preserve.
        await browser.storage.sync.set({
            anthropicDisplayMode: providerMode('anthropicDisplayMode'),
            anthropicPeakLeadMinutes,
            anthropicUsageRefreshMinutes,
            anthropicShowWeekly,
            anthropicBackgroundRefresh,
            openaiDisplayMode: providerMode('openaiDisplayMode'),
            openaiUsageRefreshMinutes,
            openaiShowWeekly,
            openaiBackgroundRefresh,
            copilotDisplayMode: providerMode('copilotDisplayMode'),
            copilotUsageRefreshMinutes,
            copilotShowCompletions,
            copilotBackgroundRefresh,
            deepseekDisplayMode: providerMode('deepseekDisplayMode'),
            deepseekPeakLeadMinutes
        });
        showStatus('Provider settings saved', 'success');
    } catch (error) {
        console.error('Error saving provider settings:', error);
        showStatus('Error saving provider settings', 'error');
    }
}

// The usage bar and the PEAK indicator are read from claude.ai itself; the
// background page fetches /api/organizations/{org}/usage with THIS profile's
// cookies (AnthropicData). Signed out, that fetch 403s, buildUsageBars() returns
// null, and BOTH markers simply vanish from every Claude Code row in the popup,
// leaving a row that looks like the feature doesn't exist. This block states the
// sign-in state and what it costs, so that disappearance is never a mystery.
// force:true; the answer must reflect NOW, not a cache from before a sign-in.
async function refreshAnthropicAuth() {
    const dot = document.getElementById('anthropicAuthDot');
    const text = document.getElementById('anthropicAuthText');
    const help = document.getElementById('anthropicAuthHelp');
    const signIn = document.getElementById('anthropicSignIn');
    const recheck = document.getElementById('anthropicAuthRecheck');
    if (!dot || !text || !help) return;

    dot.className = 'token-state-dot';
    text.textContent = 'Checking your claude.ai sign-in…';
    help.textContent = '';
    signIn.hidden = true;
    recheck.disabled = true;

    let data = null;
    let failure = null;
    try {
        const r = await browser.runtime.sendMessage({ action: 'getClaudeUsage', force: true });
        if (r && r.success) data = r.data;
        else failure = (r && r.error) || 'the extension did not answer';
    } catch (error) {
        failure = error.message || String(error);
    }
    recheck.disabled = false;

    const pct = (v) => Math.round(Math.min(100, Math.max(0, Number(v) || 0)));
    const reason = String((data && data.error) || failure || '');

    if (data && data.ok && data.session && !data.stale) {
        const now = `session ${pct(data.session.percent)}%`
            + (data.weekly ? `, weekly ${pct(data.weekly.percent)}%` : '');
        dot.className = 'token-state-dot ok';
        text.textContent = 'Signed in to claude.ai in this browser.';
        help.textContent = `Your usage is readable (right now: ${now}), so every Claude Code row in the popup's `
            + 'Connections panel shows the usage bar, plus the orange PEAK bar during Anthropic peak hours '
            + '(weekdays 12:00–18:00 UTC). The settings below apply to it.';
        return;
    }

    // A stale hit means we still have older numbers cached but the latest refresh
    // failed; the bars keep rendering, they just stop moving. Say exactly that.
    if (data && data.ok && data.session && data.stale) {
        dot.className = 'token-state-dot warn';
        text.textContent = 'Signed in, but the last refresh failed.';
        help.textContent = `claude.ai did not answer the latest usage fetch (${reason}). The popup keeps showing the `
            + 'last numbers it got, so the usage bar is now out of date until a refresh succeeds. If you signed out '
            + 'of claude.ai in this browser, sign back in; otherwise this is usually a passing network error.';
        signIn.hidden = false;
        return;
    }

    // 401/403 is claude.ai telling us there is no session on this profile.
    if ((data && data.signedOut) || /HTTP 40[13]/.test(reason)) {
        dot.className = 'token-state-dot warn';
        text.textContent = 'Not signed in to claude.ai in this browser.';
        help.textContent = `claude.ai answered ${reason}; this browser profile has no claude.ai session, and the `
            + 'usage numbers can only be read with its cookies. Consequence: every Claude Code row in the popup\'s '
            + 'Connections panel drops BOTH the usage bar and the PEAK indicator (the row shows only '
            + '"pid N · X sessions · since …"), and the settings below do nothing until you sign in. Nothing else is '
            + 'affected; tabs, navigation, page tools and MCP calls all work signed out. Sign in to claude.ai in '
            + 'THIS browser, then Re-check.';
        signIn.hidden = false;
        return;
    }

    dot.className = 'token-state-dot bad';
    text.textContent = 'Could not read your claude.ai usage.';
    help.textContent = `The usage fetch failed${reason ? ` (${reason})` : ''}. While it keeps failing, the usage bar `
        + 'and the PEAK indicator stay hidden on every Claude Code row in the popup; the rest of BrowserPlug is '
        + 'unaffected. This is usually a network problem or a signed-out claude.ai session; Re-check, or open '
        + 'claude.ai and confirm you are signed in.';
    signIn.hidden = false;
}

// The GitHub Copilot card's sign-in line. The wording lives in CopilotView.authState
// (vendor/copilot-view.js, unit-tested there); this only asks the background for one
// forced read and paints the answer.
async function refreshCopilotAuth() {
    const dot = document.getElementById('copilotAuthDot');
    const text = document.getElementById('copilotAuthText');
    const help = document.getElementById('copilotAuthHelp');
    const signIn = document.getElementById('copilotSignIn');
    const recheck = document.getElementById('copilotAuthRecheck');
    const CV = globalThis.CopilotView;
    if (!dot || !text || !help || !CV || !CV.authState) return;

    dot.className = 'token-state-dot';
    text.textContent = 'Checking your github.com sign-in…';
    help.textContent = '';
    signIn.hidden = true;
    recheck.disabled = true;

    let data = null;
    let failure = null;
    try {
        const r = await browser.runtime.sendMessage({ action: 'getCopilotUsage', force: true });
        if (r && r.success) data = r.data;
        else failure = (r && r.error) || 'the extension did not answer';
    } catch (error) {
        failure = error.message || String(error);
    }
    recheck.disabled = false;

    const s = CV.authState(data, failure);
    dot.className = `token-state-dot ${s.tone}`;
    text.textContent = s.text;
    help.textContent = s.help;
    signIn.hidden = !s.signIn;
}

// The OpenAI counterpart of refreshAnthropicAuth, and the same reasoning: signed out,
// the bar simply vanishes from every Codex CLI row, which looks exactly like a feature
// that was never built. It differs in one way worth stating on screen — reading this
// usage takes TWO calls, a session call for a short-lived bearer and then the usage call
// it opens, so "signed in but no token" is a real state and gets its own sentence.
// force:true; the answer must reflect NOW, not a cache from before a sign-in.
async function refreshOpenAIAuth() {
    const dot = document.getElementById('openaiAuthDot');
    const text = document.getElementById('openaiAuthText');
    const help = document.getElementById('openaiAuthHelp');
    const signIn = document.getElementById('openaiSignIn');
    const recheck = document.getElementById('openaiAuthRecheck');
    if (!dot || !text || !help) return;

    dot.className = 'token-state-dot';
    text.textContent = 'Checking your chatgpt.com sign-in…';
    help.textContent = '';
    signIn.hidden = true;
    recheck.disabled = true;

    let data = null;
    let failure = null;
    try {
        const r = await browser.runtime.sendMessage({ action: 'getOpenAIUsage', force: true });
        if (r && r.success) data = r.data;
        else failure = (r && r.error) || 'the extension did not answer';
    } catch (error) {
        failure = error.message || String(error);
    }
    recheck.disabled = false;

    const pct = (v) => Math.round(Math.min(100, Math.max(0, Number(v) || 0)));
    const reason = String((data && data.error) || failure || '');

    if (data && data.ok && data.session && !data.stale) {
        const now = `${data.session.windowName || 'session'} ${pct(data.session.percent)}%`
            + (data.weekly ? `, weekly ${pct(data.weekly.percent)}%` : '');
        dot.className = 'token-state-dot ok';
        text.textContent = data.plan
            ? `Signed in to chatgpt.com in this browser (plan: ${data.plan}).`
            : 'Signed in to chatgpt.com in this browser.';
        help.textContent = `Your usage is readable (right now: ${now}), so the settings below apply. These `
            + 'windows are account-wide and shared with Codex, so they move when you use ChatGPT in a tab too.';
        return;
    }

    // Older numbers cached, latest refresh failed: the bar keeps rendering, it just
    // stops moving. Say exactly that, as the Anthropic block does.
    if (data && data.ok && data.session && data.stale) {
        dot.className = 'token-state-dot warn';
        text.textContent = 'Signed in, but the last refresh failed.';
        help.textContent = `chatgpt.com did not answer the latest usage read (${reason}). The popup keeps showing `
            + 'the last numbers it got, so the bar is out of date until a refresh succeeds. If you signed out of '
            + 'chatgpt.com in this browser, sign back in; otherwise this is usually a passing network error.';
        signIn.hidden = false;
        return;
    }

    // The session endpoint answers 200 with an empty body to a signed-out profile, so
    // this is a missing token rather than an HTTP error — there is nothing to see in a
    // status code, which is why the data layer names it.
    if (data && data.signedOut) {
        dot.className = 'token-state-dot warn';
        text.textContent = 'Not signed in to chatgpt.com in this browser.';
        help.textContent = 'chatgpt.com issued no access token for this profile, and the usage windows can only be '
            + 'read with one. Consequence: every Codex CLI row in the popup\'s Connections panel shows "signed out" '
            + 'instead of a usage bar, and the settings below do nothing until you sign in. Nothing else is affected; '
            + 'tabs, navigation, page tools and MCP calls all work signed out. Sign in to chatgpt.com in THIS '
            + 'browser, then Re-check.';
        signIn.hidden = false;
        return;
    }

    dot.className = 'token-state-dot bad';
    text.textContent = 'Could not read your chatgpt.com usage.';
    help.textContent = `The usage read failed${reason ? ` (${reason})` : ''}. While it keeps failing, the bar stays `
        + 'hidden on every Codex CLI row; the rest of BrowserPlug is unaffected. This is usually a network problem '
        + 'or a signed-out chatgpt.com session; Re-check, or open chatgpt.com and confirm you are signed in.';
    signIn.hidden = false;
}

async function saveInterfaceSettings() {
    // set() merges into existing storage, so no need to re-read/preserve the
    // other settings here.
    const hideEmptySessions = document.getElementById('hideEmptySessions').checked;
    const toolCallSort = document.getElementById('toolCallSort').value;
    try {
        await browser.storage.sync.set({ hideEmptySessions, toolCallSort });
        showStatus('Interface settings saved', 'success');
    } catch (error) {
        console.error('Error saving interface settings:', error);
        showStatus('Error saving interface settings', 'error');
    }
}

// Highlight the chosen UI-surface card and check its radio.
// Which platform this page is running on, stamped on <body> for the CSS to key off.
// getPlatformInfo is the honest signal: a Firefox fork reports "Firefox" from
// getBrowserInfo and serves a stock UA, but nothing lies about the OS. Any failure
// leaves the desktop layout, which is the safe default — a phone showing four surfaces
// is untidy, a desktop showing only Overlay would have lost three working options.
async function applyPlatform() {
    try {
        if (browser.runtime.getPlatformInfo) {
            const info = await browser.runtime.getPlatformInfo();
            if (info && info.os === 'android') {
                document.body.dataset.platform = 'android';
            }
        }
    } catch (e) {
        console.error('getPlatformInfo failed; assuming desktop:', e);
    }
}

// applyPlatform() stamped this before anything else ran, so every later decision can
// just read it instead of awaiting getPlatformInfo() again.
function isAndroid() {
    return document.body.dataset.platform === 'android';
}

// Open the extension's own interface from this page.
//
// NOT via browserAction.openPopup() on Android: GeckoView implements it, but Fenix never
// hooked it up (Bugzilla 1817809, NEW since 2023), and the call neither opens anything
// nor throws — it resolves, so there is not even an error to report. Bugzilla 2045535 is
// the same silence seen from the other end. MDN will tell you it works: browser-compat-data
// claims firefox_android 79 for openPopup. It does not.
//
// So on a phone we open popup.html as a tab, which is what the surface looks like there
// anyway (popup.html sizes itself full-screen on Android). Desktop keeps openPopup, where
// it works and gives the real anchored panel.
async function openExtensionOverlay() {
    const note = document.getElementById('openOverlayNote');
    const setNoteText = (cls, text) => {
        if (!note) return;
        note.hidden = false;
        note.className = `field-note ${cls}`;
        note.textContent = text;
    };
    try {
        if (isAndroid()) {
            await browser.tabs.create({ url: browser.runtime.getURL('popup/popup.html') });
            setNoteText('ok', 'Opened BrowserPlug. Firefox for Android cannot open the '
                + 'anchored overlay from a page (Bugzilla 1817809), so it opens as a tab; '
                + 'the browser menu opens the real overlay.');
            return;
        }
        await browser.browserAction.openPopup();
        if (note) note.hidden = true;
    } catch (e) {
        console.error('Opening the overlay failed:', e);
        setNoteText('err', 'Firefox would not open the overlay from here. '
            + 'Open BrowserPlug from the browser menu instead.');
    }
}

function setSurfaceSelection(value) {
    const options = document.getElementById('uiSurfaceOptions');
    if (!options) return;
    options.querySelectorAll('.surface-card').forEach((card) => {
        const selected = card.dataset.surface === value;
        card.classList.toggle('selected', selected);
        const input = card.querySelector('input');
        if (input) input.checked = selected;
    });
}

async function onUiSurfaceChange(event) {
    const uiSurface = event.target.value;
    setSurfaceSelection(uiSurface);
    // set() merges into existing storage; the background reacts via storage.onChanged.
    try {
        await browser.storage.sync.set({ uiSurface });
        showStatus('UI surface updated', 'success');
    } catch (error) {
        console.error('Error saving UI surface:', error);
        showStatus('Error saving UI surface', 'error');
    }
}


// In-flight latch, the same one applyNetMode() keeps. Two presses (or one press with the
// handler wired twice, which happens when DOMContentLoaded is delivered twice — see the
// "Wire ONCE" note further down) must not tear the socket down and redial it twice; the
// second dial would land on the first one's half-open connection.
let connectionRefreshBusy = false;

// What the socket will actually dial is what was SAVED, never what is on screen:
// background.js reads hostname/port from storage.sync (loadConfig), while the HTTP dot
// beside this button probes the value in the input box. Edit the host, press Reconnect
// without saving, and the page shows "Reachable" next to "Disconnected" with nothing to
// explain it. Read storage at press time rather than tracking a dirty flag, so this
// cannot drift out of step with what was really written.
async function reportUnsavedConnectionEdits() {
    const note = document.getElementById('connDirtyNote');
    if (!note) return;
    try {
        const saved = await browser.storage.sync.get({
            hostname: 'localhost', port: DEFAULT_WS_PORT, connectionToken: ''
        });
        const host = (document.getElementById('hostname').value || '').trim();
        const port = parseInt(document.getElementById('port').value, 10);
        const token = (document.getElementById('connectionToken')?.value || '').trim();
        const changed = [];
        if (host && host !== saved.hostname) changed.push('hostname');
        if (!isNaN(port) && port !== saved.port) changed.push('port');
        // The auto-fetched token lives in storage.local and legitimately differs from the
        // synced key; only a value the user typed counts as an edit (saveSettings draws
        // the same line).
        if (token && token !== saved.connectionToken && token !== autoTokenValue) {
            changed.push('connection token');
        }
        if (!changed.length) {
            note.hidden = true;
            return;
        }
        const list = changed.length === 1 ? changed[0]
            : `${changed.slice(0, -1).join(', ')} and ${changed[changed.length - 1]}`;
        note.hidden = false;
        note.className = 'field-note warn';
        note.textContent = `Unsaved ${list}. This button uses the saved settings, `
            + 'so press Save first or it will keep dialling the old address.';
    } catch (e) {
        console.error('Could not compare the form against saved settings:', e);
        note.hidden = true;
    }
}

// `dial` is true only for a real press of the button. The initial render calls this too,
// and a page that opened itself must not start dialling on its own.
async function refreshConnectionStatus(dial) {
    if (connectionRefreshBusy) return;
    connectionRefreshBusy = true;
    // Only for a real press: the automatic render on load would otherwise scold the user
    // about a form they have not touched yet.
    if (dial) await reportUnsavedConnectionEdits();
    const btn = document.getElementById('refreshStatus');
    const reconnecting = !!dial && !wsIsConnected;
    if (btn) {
        btn.disabled = true;
        btn.textContent = reconnecting ? 'Reconnecting…' : 'Refreshing…';
    }
    setHttpStatus('checking');
    try {
        // Disconnected means the button read "Reconnect", so DIAL before reporting.
        // Re-reading first would repaint the same red the press was meant to clear.
        if (reconnecting) {
            try {
                await browser.runtime.sendMessage({ action: 'forceReconnect' });
                // Give the socket a moment to open; the status is not worth reading
                // in the gap between the dial and the handshake.
                await new Promise((resolve) => setTimeout(resolve, 700));
            } catch (e) {
                console.error('Reconnect request failed:', e);
            }
        }
        try {
            const response = await browser.runtime.sendMessage({
                type: 'getConnectionStatus'
            });
            updateConnectionStatus(response || {});
        } catch (error) {
            console.error('Error getting connection status:', error);
            updateConnectionStatus({ connected: false });
        }
        // The same press re-tests the HTTP port. They fail independently, and showing two
        // dots would be theatre if one button refreshed only one of them.
        const health = await fetchHealthSnapshot();
        if (health) lastHealth = health;
        setHttpStatus(!!health);
    } finally {
        connectionRefreshBusy = false;
        if (btn) {
            btn.disabled = false;
            syncConnectionButton();
        }
    }
    // Browsers come and go while this page is open; this is the one control the user
    // already presses to re-read the link, so re-read the roster with it.
    loadDefaultBrowser();
}

// `state` = { connected, serverHealthy }. "Connected" reflects the WebSocket;
// green ("Connected") is reserved for a socket the server also answers health
// probes on. A socket that's up but unresponsive shows the amber "degraded" state.
// Whether the WebSocket is up, as last rendered. The button under Connection reads it to
// decide whether pressing it should re-read or DIAL.
let wsIsConnected = false;

// The one control beside the status. On the page where the connection is configured, a
// button that only ever re-read the state was useless precisely when it mattered: a red
// light and nothing to press. The popup has offered Reconnect all along; this is the same
// action, in the place people were already pressing.
function syncConnectionButton() {
    const btn = document.getElementById('refreshStatus');
    if (!btn || btn.disabled) return;   // mid-operation; the label is the operation's
    btn.textContent = wsIsConnected ? 'Refresh' : 'Reconnect';
    btn.title = wsIsConnected
        ? 'Re-read the WebSocket state and re-test the HTTP port.'
        : 'Dial the server again, and re-test the HTTP port.';
}

// The second dot. It is a separate fact from the WebSocket, not a detail of it: a firewall
// rule can cover one port and not the other, which is exactly how a remote browser ends up
// connected and unable to load this page's own data.
function setHttpStatus(state) {
    const dot = document.getElementById('httpIndicator');
    const text = document.getElementById('httpText');
    if (!dot || !text) return;
    dot.classList.remove('connected', 'disconnected', 'degraded');
    if (state === 'checking') {
        dot.classList.add('degraded');
        text.textContent = 'Checking…';
        text.title = '';
        return;
    }
    dot.classList.add(state ? 'connected' : 'disconnected');
    text.textContent = state ? 'Reachable' : 'Unreachable';
    text.title = state
        ? 'The server answered on its HTTP port.'
        : 'No answer on the server HTTP port. From another machine that port also needs '
          + 'the connection token and a firewall rule of its own.';
}

function updateConnectionStatus(state) {
    state = state || {};
    const connected = !!state.connected;
    const healthy = state.serverHealthy; // true | false | null | undefined
    const indicator = document.getElementById('connectionIndicator');
    const text = document.getElementById('connectionText');
    wsIsConnected = connected;
    syncConnectionButton();

    indicator.classList.remove('connected', 'disconnected', 'degraded');
    if (!connected) {
        indicator.classList.add('disconnected');
        text.textContent = 'Disconnected';
        text.title = 'No WebSocket to the server.';
    } else if (healthy === false) {
        indicator.classList.add('degraded');
        // This label shares one row with the hostname, port and URL fields, so the
        // long form goes in the tooltip; the amber dot already says "not simply
        // connected", and the word the user needs to read first is the failure.
        text.textContent = 'Server not responding';
        text.title = 'The WebSocket is up, but the server is not answering health probes.';
    } else {
        indicator.classList.add('connected');
        text.textContent = 'Connected';
        text.title = 'WebSocket up and the server is answering health probes.';
    }
}

function updateWebSocketUrl() {
    const hostname = document.getElementById('hostname').value.trim();
    const port = document.getElementById('port').value;

    if (hostname && port) {
        const wsUrl = `ws://${hostname}:${port}`;
        document.getElementById('wsUrl').value = wsUrl;
    }
}

async function resetToDefaults() {
    if (!confirm('Are you sure? This deletes every saved option here AND the server\'s own settings file, reverting both to their fresh-install state. The server\'s master token is kept (rotate it with gen_ws_token.py), but a token you typed into Connection token is deleted with the other options, so a browser on another machine needs it pasted in again. This cannot be undone.')) {
        return;
    }

    try {
        // The SERVER keeps settings of its own (config.json: the WebSocket bind and the
        // LAN/WAN choice), so clearing only this browser's storage would leave it bound
        // wherever the last "allow browsers on other machines" put it -- a reset that
        // visibly did not reset. Do the server half FIRST, while the token field still
        // holds the credential that endpoint demands: storage.sync.clear() takes it away.
        const serverReset = await requestServerConfigReset();

        // Actually delete every stored key rather than just re-typing defaults into
        // the form. With storage empty, every reader (background, popup, this page)
        // falls back to its built-in defaults, exactly like a fresh install.
        await browser.storage.sync.clear();
        if (globalThis.BrowserplugCensorPanel) await globalThis.BrowserplugCensorPanel.reset();

        // Re-apply defaults to the running background session (reconnect with the
        // default host/port) without re-persisting them. The uiSurface reverts via
        // the background's storage.onChanged listener reacting to the cleared key.
        await browser.runtime.sendMessage({ type: 'resetToDefaults' });

        // Repopulate the whole form from the now-empty storage → shows the defaults
        // for connection, advanced, interface, and UI-surface options.
        await loadSettings();

        // The network panels hold state loadSettings does not own (address list, WAN
        // URL, check verdict); re-derive them, then re-detect the public address the
        // cleared override just vacated.
        renderNetworkPanels().then(detectPublicAddress);

        // The reset deleted config.json, so the default browser is back to "first
        // started"; re-read it rather than leaving the old pick on screen.
        loadDefaultBrowser();

        // Say what really happened on BOTH sides. A server that kept its settings is
        // not a fresh install, and reporting one would send the user hunting for a
        // stale bind they were told no longer existed.
        if (!serverReset.ok) {
            showStatus(`Extension settings deleted, but the server kept its own: ${serverReset.error}`, 'error');
        } else if (serverReset.restarting) {
            showStatus('All settings deleted; the server is restarting on its default localhost bind…', 'success');
            pollHealthUntilBack(6, 1500);
        } else {
            showStatus('All settings deleted; extension and server reverted to fresh-install defaults', 'success');
        }
    } catch (error) {
        console.error('Error resetting settings:', error);
        showStatus('Error resetting settings', 'error');
    }
}

// The server half of a reset: POST /reset-config deletes its config.json. Guarded like
// /ws-bind (the token in a custom header, demanded even from localhost), so it needs the
// token this page already holds. Returns {ok, restarting} or {ok:false, error}; never throws, since
// the extension-side reset must go ahead either way.
async function requestServerConfigReset() {
    const token = document.getElementById('connectionToken').value.trim();
    if (!token) {
        return { ok: false, error: 'this page has no connection token, so the server would refuse the request' };
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
        const res = await fetch(currentServerUrl('/reset-config'), {
            method: 'POST',
            headers: Object.assign({ 'content-type': 'application/json' },
                await serverAuthHeaders(token, 'POST', currentServerUrl('/reset-config'))),
            cache: 'no-store',
            signal: controller.signal
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            return { ok: false, error: body.error || `it refused (HTTP ${res.status})` };
        }
        const data = await res.json().catch(() => ({}));
        return { ok: true, restarting: !!data.restarting };
    } catch (e) {
        const msg = e && e.name === 'AbortError' ? 'the request timed out' : `could not reach it (${e.message})`;
        return { ok: false, error: msg };
    } finally {
        clearTimeout(timer);
    }
}

// ============================================================================
// Default browser (General ▸ Default browser)
//
// The setting belongs to the SERVER (its config.json), so this whole section reads
// and writes over the WebSocket via the background page. Nothing is kept in
// storage.sync: two browsers' options pages must never hold different answers to
// "which browser is the default", and only the server can arbitrate.
// ============================================================================
const DEFAULT_BROWSER_FIRST = 'first_started';

// Last payload the server sent. The candidate list is read back out of it on save,
// so a save always describes a browser the server itself listed.
let defaultBrowserState = null;

async function loadDefaultBrowser() {
    const select = document.getElementById('defaultBrowserSelect');
    if (!select) return;
    try {
        const res = await browser.runtime.sendMessage({ action: 'getDefaultBrowser' });
        if (!res || !res.success) throw new Error((res && res.error) || 'no answer from the server');
        renderDefaultBrowser(res.data);
    } catch (e) {
        defaultBrowserState = null;
        setDefaultBrowserControlsEnabled(false);
        setNote('defaultBrowserNote', 'warn',
            `The server did not answer, so this setting cannot be shown or changed right now (${e.message}).`);
    }
}

function renderDefaultBrowser(payload) {
    defaultBrowserState = payload || null;
    const select = document.getElementById('defaultBrowserSelect');
    const settings = (payload && payload.settings) || {};
    const browsers = (payload && payload.browsers) || [];

    // Current first, then the rest in slot order, then anything not connected (the
    // browser that was picked earlier and has since been closed).
    const ordered = browsers.slice().sort((a, b) => {
        if (a.is_current !== b.is_current) return a.is_current ? -1 : 1;
        if (a.connected !== b.connected) return a.connected ? -1 : 1;
        return (a.slot || 99) - (b.slot || 99);
    });

    select.textContent = '';
    select.appendChild(new Option('First started', DEFAULT_BROWSER_FIRST));
    ordered.forEach((b) => {
        const name = b.name || 'Unnamed browser';
        const label = b.is_current ? `${name} (this browser)`
            : b.connected ? name
            : `${name} (not connected)`;
        select.appendChild(new Option(label, b.instance_id));
    });

    const wanted = settings.mode === DEFAULT_BROWSER_FIRST
        ? DEFAULT_BROWSER_FIRST : (settings.instance_id || DEFAULT_BROWSER_FIRST);
    select.value = wanted;
    // A stored pin the server no longer lists at all (its snapshot was lost) would
    // leave the select on nothing; say so rather than silently reading as default.
    if (!select.value) select.value = DEFAULT_BROWSER_FIRST;

    document.getElementById('defaultBrowserAutoLaunch').checked = !!settings.auto_launch;
    document.getElementById('defaultBrowserArgs').value = settings.launch_args || '';

    setDefaultBrowserControlsEnabled(true);
    syncDefaultBrowserFields();
}

function selectedDefaultBrowser() {
    const value = document.getElementById('defaultBrowserSelect').value;
    if (!value || value === DEFAULT_BROWSER_FIRST) return null;
    const browsers = (defaultBrowserState && defaultBrowserState.browsers) || [];
    return browsers.find((b) => b.instance_id === value) || { instance_id: value };
}

// Show the launch checkbox only for a real browser, and the arguments only once the
// checkbox is on; a server that may not start anything has nothing to pass to it.
function syncDefaultBrowserFields() {
    const chosen = selectedDefaultBrowser();
    const launchField = document.getElementById('defaultBrowserLaunchField');
    const argsField = document.getElementById('defaultBrowserArgsField');
    const checkbox = document.getElementById('defaultBrowserAutoLaunch');

    launchField.hidden = !chosen;
    // Nothing to run: a browser on another machine, or one whose program the server
    // never saw. Better disabled with a reason than offered and refused on save.
    const canLaunch = !chosen || chosen.can_launch !== false;
    checkbox.disabled = !canLaunch;
    if (!canLaunch) checkbox.checked = false;

    argsField.hidden = launchField.hidden || !checkbox.checked;

    if (chosen && !canLaunch) {
        setNote('defaultBrowserNote', 'warn',
            'The server cannot start that browser itself: it is on another machine, or the '
            + 'server never saw which program it was started from.');
    } else if (chosen && !chosen.connected) {
        setNote('defaultBrowserNote', 'warn',
            'That browser is not connected right now. Until it is, agents fall back to '
            + 'whichever browser is.');
    } else {
        // setNote always reveals the element, so clearing means hiding it here.
        const note = document.getElementById('defaultBrowserNote');
        if (note) { note.textContent = ''; note.hidden = true; }
    }
}

function setDefaultBrowserControlsEnabled(enabled) {
    ['defaultBrowserSelect', 'defaultBrowserAutoLaunch'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.disabled = !enabled;
    });
}

async function saveDefaultBrowser() {
    const value = document.getElementById('defaultBrowserSelect').value;
    const settings = value === DEFAULT_BROWSER_FIRST
        ? { mode: DEFAULT_BROWSER_FIRST }
        : {
            mode: 'pinned',
            instance_id: value,
            auto_launch: document.getElementById('defaultBrowserAutoLaunch').checked,
        };

    try {
        const res = await browser.runtime.sendMessage({ action: 'setDefaultBrowser', settings });
        if (!res || !res.success) throw new Error((res && res.error) || 'no answer from the server');
        if (res.data && res.data.ok === false) throw new Error(res.data.error || 'the server refused it');
        renderDefaultBrowser(res.data);
        showStatus('Default browser saved on the server', 'success');
    } catch (e) {
        showStatus(`Could not save the default browser: ${e.message}`, 'error');
        // Put the form back on what the server actually holds, so the page never
        // shows a setting that was not stored.
        loadDefaultBrowser();
    }
}

function onDefaultBrowserChange() {
    syncDefaultBrowserFields();
    saveDefaultBrowser();
}

function showStatus(message, type) {
    const statusDiv = document.getElementById('status');
    // Errors interrupt (assertive); success confirmations wait for a pause (polite).
    statusDiv.setAttribute('aria-live', type === 'error' ? 'assertive' : 'polite');
    statusDiv.textContent = message;
    statusDiv.className = `status ${type} visible`;

    // Hide (fade out) after 5 seconds. The element stays in the DOM/a11y tree so
    // the aria-live region keeps announcing future messages.
    clearTimeout(statusDiv._hideTimer);
    statusDiv._hideTimer = setTimeout(() => {
        statusDiv.classList.remove('visible');
    }, 5000);
}

// ============================================================================
// Sidebar tab navigation (General / Advanced / Health)
// ============================================================================

const TAB_STORAGE_KEY = 'browserplugOptionsTab';

function initTabs() {
    const tabs = Array.from(document.querySelectorAll('.nav-item[role="tab"]'));
    if (!tabs.length) return;
    const valid = tabs.map((t) => t.dataset.tab);

    function selectTab(name, { focus = false } = {}) {
        if (!valid.includes(name)) return;
        tabs.forEach((t) => {
            const on = t.dataset.tab === name;
            t.setAttribute('aria-selected', on ? 'true' : 'false');
            t.tabIndex = on ? 0 : -1;
            const pane = document.getElementById('pane-' + t.dataset.tab);
            if (pane) pane.hidden = !on;
            if (on && focus) t.focus();
        });
        try { localStorage.setItem(TAB_STORAGE_KEY, name); } catch (e) { /* private mode */ }
        if (name === 'health') refreshHealth();
        // The connection map polls ONLY while its pane is the visible one.
        setConnMapPaneOpen(name === 'health');
    }

    tabs.forEach((t, i) => {
        t.addEventListener('click', () => selectTab(t.dataset.tab));
        t.addEventListener('keydown', (e) => {
            let j = null;
            if (e.key === 'ArrowDown' || e.key === 'ArrowRight') j = (i + 1) % tabs.length;
            else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') j = (i - 1 + tabs.length) % tabs.length;
            else if (e.key === 'Home') j = 0;
            else if (e.key === 'End') j = tabs.length - 1;
            if (j !== null) {
                e.preventDefault();
                selectTab(tabs[j].dataset.tab, { focus: true });
            }
        });
    });

    // Restore the last-viewed tab: URL hash beats stored preference; default General.
    const fromHash = (location.hash || '').replace(/^#/, '');
    let stored = null;
    try { stored = localStorage.getItem(TAB_STORAGE_KEY); } catch (e) { /* private mode */ }
    let initial = 'general';
    if (valid.includes(fromHash)) initial = fromHash;
    else if (valid.includes(stored)) initial = stored;
    selectTab(initial);
}

// ============================================================================
// Health pane: fetch and render the server's GET /health endpoint
// ============================================================================

// The port the SERVER says it is on, learned over the WebSocket (the extension's
// `server.health` response carries `mcp_port`; background.js keeps it as
// lastServerMcpPort and hands it over as `serverMcpPort`). It is the truth: the
// server prefers 2830 but walks upward when that port is taken, so the configured
// value below is only a starting point / manual override. null until the socket is
// up and a health probe has answered.
let learnedMcpPort = null;

async function refreshLearnedMcpPort() {
    try {
        const status = await browser.runtime.sendMessage({ type: 'getConnectionStatus' });
        const port = status && parseInt(status.serverMcpPort, 10);
        if (port && !isNaN(port)) learnedMcpPort = port;
    } catch (e) { /* background unavailable; keep whatever we knew */ }
    return learnedMcpPort;
}

function configuredMcpPort() {
    const port = parseInt(document.getElementById('mcpPort').value, 10);
    return (isNaN(port) || port < 1 || port > 65535) ? DEFAULT_MCP_PORT : port;
}

// What Health/Restart actually talk to: the live port when we know it, else the
// configured starting point.
function effectiveMcpPort() {
    return learnedMcpPort || configuredMcpPort();
}

function healthUrlFor(port) {
    const host = (document.getElementById('hostname').value || '').trim() || 'localhost';
    return `http://${host}:${port}/health`;
}

function currentHealthUrl() {
    return healthUrlFor(effectiveMcpPort());
}

// Any other endpoint on the server's HTTP port (/ws-bind, /restart, …).
function currentServerUrl(path) {
    const host = (document.getElementById('hostname').value || '').trim() || 'localhost';
    return `http://${host}:${effectiveMcpPort()}${path}`;
}

// Requests to the server's OWN HTTP port prove the connection token. That port follows
// the LAN/WAN bind now, so a browser on another machine can reach it and has to
// authenticate; a local one is let through without, but signing anyway keeps both paths
// identical. The proof is an HMAC over the method, path, time and a nonce
// (ws-crypto.js signRequest); the token itself never crosses the network, which on a
// LAN bind is plain http. Deliberately NOT used by fetchJson(), which talks to
// check-host.net: nothing derived from our token has any business there.
async function serverAuthHeaders(token, method, url, body) {
    const wsCrypto = globalThis.BrowserplugWsCrypto;
    if (!token || !wsCrypto) return {};
    try {
        return { [wsCrypto.AUTH_HEADER]: await wsCrypto.signRequest(token, method, url, body) };
    } catch (e) {
        console.warn('Could not sign a request to the server:', e);
        return {};
    }
}

// A POST with a body is signed v2, and a server older than v2 refuses that with 403. It
// accepts v1, which leaves the body unsigned, while a v2 server refuses a v1 body; so one
// v1 retry after a 403 reaches the old server without weakening a new one. When the
// retry is refused too, the first refusal is the one that explains why.
async function postSignedBody(url, token, body, signal) {
    const post = async (signedBody) => fetch(url, {
        method: 'POST',
        headers: Object.assign({ 'content-type': 'application/json' },
            await serverAuthHeaders(token, 'POST', url, signedBody)),
        body,
        signal
    });
    const first = await post(body);
    if (first.status !== 403) return first;
    const retry = await post(undefined);
    return retry.ok ? retry : first;
}

async function serverFetchInit(url, extra) {
    extra = extra || {};
    const token = (document.getElementById('connectionToken')?.value || '').trim();
    const headers = Object.assign({}, extra.headers || {},
        await serverAuthHeaders(token, extra.method || 'GET', url, extra.body));
    return Object.assign({ cache: 'no-store' }, extra, { headers: headers });
}

function syncHealthUrl() {
    const el = document.getElementById('healthUrlDisplay');
    if (el) el.textContent = currentHealthUrl();
    // Keep the restart modal's fallback URL on the same port, so it always shows the
    // endpoint the HTTP fallback would really call.
    const restartEl = document.getElementById('restartUrlDisplay');
    if (restartEl) restartEl.textContent = currentRestartUrl();
    // Say plainly which port is really in use when the server has moved off the
    // configured one, otherwise the field would read as the truth when it isn't.
    const live = document.getElementById('mcpPortLive');
    if (live) {
        live.textContent = (learnedMcpPort && learnedMcpPort !== configuredMcpPort())
            ? `In use now: ${learnedMcpPort}; the server reports that as its port.`
            : '';
    }
}

async function saveMcpPort() {
    const port = parseInt(document.getElementById('mcpPort').value, 10);
    if (isNaN(port) || port < 1 || port > 65535) {
        showStatus('MCP port must be between 1 and 65535', 'error');
        return;
    }
    try {
        // set() merges into existing storage, so no need to re-read the rest.
        await browser.storage.sync.set({ mcpPort: port });
        syncHealthUrl();
        showStatus('MCP port saved', 'success');
    } catch (error) {
        console.error('Error saving MCP port:', error);
        showStatus('Error saving MCP port', 'error');
    }
}

async function refreshHealth() {
    const out = document.getElementById('healthOutput');
    if (!out) return;
    // The server may have walked off the configured port; the WebSocket knows where
    // it landed. Ask first, so the very first probe goes to the right place.
    await refreshLearnedMcpPort();
    const port = effectiveMcpPort();
    const url = healthUrlFor(port);
    syncHealthUrl();

    // Loading state (aria-busy lets assistive tech know a fetch is underway)
    out.setAttribute('aria-busy', 'true');
    out.textContent = '';
    const loading = document.createElement('div');
    loading.className = 'health-loading';
    const spinner = document.createElement('span');
    spinner.className = 'health-spinner';
    loading.appendChild(spinner);
    loading.appendChild(document.createTextNode('Contacting server…'));
    out.appendChild(loading);

    // Abort if the peer accepts the TCP connection but never sends a response
    // (e.g. a wedged server mid-restart), so the spinner can't hang forever.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 6000);
    try {
        const res = await fetch(url, await serverFetchInit(url, { method: 'GET', signal: controller.signal }));
        if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`.trim());
        const data = await res.json();
        renderHealth(out, data);
    } catch (error) {
        // The port we tried is unreachable. Try the OTHER port we hold, the one the
        // server reported over the WebSocket, or the configured starting point, and
        // adopt it if it answers. This self-heals the port-drift failure.
        const corrected = await tryOtherMcpPort(out, port);
        if (!corrected) renderHealthError(out, url, error);
    } finally {
        clearTimeout(timer);
    }
}

// /health did not answer on `triedPort`. The extension only ever knows two candidate
// ports: the one the server reported over the WS (asked for again here: it may have
// landed while we were failing) and the configured one, so try those, and nothing
// else. This is deliberately NOT a port scanner. On success the working port is
// adopted (persisted + previews re-synced). Returns true if it recovered.
async function tryOtherMcpPort(out, triedPort) {
    await refreshLearnedMcpPort();

    const candidates = [];
    for (const p of [learnedMcpPort, configuredMcpPort()]) {
        if (p && p !== triedPort && !candidates.includes(p)) candidates.push(p);
    }
    if (!candidates.length) return false;   // nothing else to try

    for (const port of candidates) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 6000);
        let data = null;
        try {
            const res = await fetch(healthUrlFor(port), await serverFetchInit(healthUrlFor(port), { method: 'GET', signal: controller.signal }));
            clearTimeout(timer);
            if (!res.ok) continue;
            data = await res.json();
        } catch (e) {
            clearTimeout(timer);
            continue;
        }
        // It answered: this is where the server really is. Use it from now on
        // (Refresh/Restart) and make it the configured starting point too.
        learnedMcpPort = port;
        if (port !== configuredMcpPort()) {
            document.getElementById('mcpPort').value = port;
            try {
                await browser.storage.sync.set({ mcpPort: port });
            } catch (e) {
                console.error('Error saving the corrected MCP port:', e);
            }
            showStatus(`MCP port corrected to ${port}; the server answers there (${triedPort} did not)`, 'success');
        }
        syncHealthUrl();
        renderHealth(out, data);
        return true;
    }
    return false;
}

// ============================================================================
// Restart control: confirmation modal, then cycle the shared server.
//
// Prefer the WebSocket (the background relays a `server.restart` request over the
// extension's live socket): it reaches the server even when the MCP HTTP control
// port is unreachable, the very failure the health check surfaces. Fall back to
// POST /restart only if the extension isn't connected or the WS request fails.
//
// How long the wait is, and why it is not 9 seconds: the server no longer exits
// 0.3s after the ack. It drains in-flight tool calls, closes the browser sockets
// with a close frame, and stops its HTTP listener before it goes, and only then
// does the replacement get the ports it has been waiting for. Worst case that is
// roughly 20s of teardown before the new process can even bind.
// ============================================================================

// The wait budget shared by both restart paths (this button and the network-mode
// rebind, which restarts the server too).
const RESTART_WAIT_SECONDS = 60;
const RESTART_POLL_DELAY_MS = 1000;

function currentRestartUrl() {
    const host = (document.getElementById('hostname').value || '').trim() || 'localhost';
    return `http://${host}:${effectiveMcpPort()}/restart`;
}

function initRestartControl() {
    const btn = document.getElementById('healthRestart');
    const dialog = document.getElementById('restartDialog');
    if (!btn) return;

    btn.addEventListener('click', () => {
        const disp = document.getElementById('restartUrlDisplay');
        if (disp) disp.textContent = currentRestartUrl();
        if (dialog && typeof dialog.showModal === 'function') {
            // Escape/backdrop dismissal leaves returnValue unchanged, so clear it
            // first; otherwise a prior "confirm" could re-fire on a later Escape.
            dialog.returnValue = '';
            dialog.showModal();
        } else if (confirm('Restart the shared BrowserPlug server? In-flight tool calls will be interrupted.')) {
            // Fallback for browsers without <dialog> support.
            restartServer();
        }
    });

    if (dialog) {
        dialog.addEventListener('close', () => {
            if (dialog.returnValue === 'confirm') restartServer();
        });
    }
}

async function restartServer() {
    const out = document.getElementById('healthOutput');
    showStatus('Restarting server…', 'success');

    const result = await requestRestart();
    if (!result.ok) {
        showStatus(`Restart failed: ${result.error}`, 'error');
        if (out) renderHealthError(out, result.url || currentRestartUrl(), new Error(result.error));
        return;
    }

    // The ack carries the pid of the process that is leaving, which is what makes the
    // wait below honest: the dying server answers /health for its whole teardown.
    showStatus(`Restart requested over ${result.via}; waiting for the server to come back…`, 'success');
    if (out) {
        out.setAttribute('aria-busy', 'true');
        out.textContent = '';
        const loading = document.createElement('div');
        loading.className = 'health-loading';
        const spinner = document.createElement('span');
        spinner.className = 'health-spinner';
        loading.appendChild(spinner);
        loading.appendChild(document.createTextNode('Server restarting…'));
        out.appendChild(loading);
    }
    pollHealthUntilBack(RESTART_WAIT_SECONDS, RESTART_POLL_DELAY_MS, result.pid);
}

// Try the WebSocket path first (via the background), then fall back to POST
// /restart. Returns {ok:true, via, pid} or {ok:false, error, url}. `pid` is the
// OUTGOING server's — both transports ack with it, and the wait needs it.
async function requestRestart() {
    // 1) WebSocket via the background script.
    let wsError = 'extension not connected';
    try {
        const resp = await browser.runtime.sendMessage({ action: 'restartServer' });
        if (resp && resp.success) return { ok: true, via: 'WebSocket', pid: resp.data && resp.data.pid };
        wsError = (resp && resp.error) || wsError;
    } catch (e) {
        wsError = (e && e.message) || String(e);
    }

    // 2) HTTP fallback: POST /restart on the MCP HTTP port.
    const url = currentRestartUrl();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 6000);
    try {
        const res = await fetch(url, await serverFetchInit(url, { method: 'POST', signal: controller.signal }));
        clearTimeout(timer);
        if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`.trim());
        const body = await res.json().catch(() => ({}));
        return { ok: true, via: 'HTTP', pid: body && body.pid };
    } catch (httpErr) {
        clearTimeout(timer);
        const aborted = httpErr && httpErr.name === 'AbortError';
        const httpMsg = aborted ? 'request timed out' : ((httpErr && httpErr.message) || String(httpErr));
        return { ok: false, url, error: `WebSocket (${wsError}) and HTTP (${httpMsg}) both failed` };
    }
}

// Keep the spinner honest about how long the wait has been running. Without it a
// restart that takes 20s to drain is indistinguishable from one that hung.
function setRestartProgress(seconds) {
    const loading = document.querySelector('#healthOutput .health-loading');
    if (!loading || !loading.lastChild) return;
    loading.lastChild.textContent = seconds < 3 ? 'Server restarting…'
        : `Server restarting… ${seconds}s`;
}

// After a restart the old process drains, closes its sockets and frees the ports;
// a replacement then rebinds. Poll /health until the NEW one answers.
//
// `oldPid` is what makes this a real check. The outgoing server keeps answering
// /health for its entire teardown, so "ok" alone means nothing — the old code
// declared victory on the first ok response, and the only reason it usually got
// away with it was that the first poll came 1.5s after an exit that took 0.3s.
// With a graceful teardown that window is seconds wide, and every one of them
// would have reported a restart that had not happened yet.
async function pollHealthUntilBack(attempts, delayMs, oldPid) {
    // A wall-clock bound as well as an attempt count: a half-up server that accepts
    // the connection and never answers costs the full abort guard per attempt, and
    // attempts alone would let that stretch the wait many times over.
    const deadline = Date.now() + attempts * delayMs + 15000;
    const startedAt = Date.now();
    for (let i = 0; i < attempts && Date.now() < deadline; i++) {
        await new Promise((r) => setTimeout(r, delayMs));
        setRestartProgress(Math.round((Date.now() - startedAt) / 1000));
        // The replacement can come back on a different port (the walk); the reconnected
        // socket tells us where, so re-ask rather than hammering the old one.
        await refreshLearnedMcpPort();
        // Same abort guard as refreshHealth: a half-up server can accept the TCP
        // connection and never answer, and a fetch that never settles would wedge
        // this loop, and the restart spinner, forever.
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 6000);
        try {
            const res = await fetch(currentHealthUrl(), await serverFetchInit(currentHealthUrl(), { method: 'GET', signal: controller.signal }));
            if (res.ok) {
                const data = await res.json();
                // Still the process we just asked to leave: it is draining, not back.
                if (!oldPid || !data || data.server_pid !== oldPid) {
                    lastHealth = data;
                    renderHealth(document.getElementById('healthOutput'), data);
                    showStatus('Server is back up', 'success');
                    return;
                }
            }
        } catch (e) { /* not back yet; keep polling */
        } finally {
            clearTimeout(timer);
        }
    }
    // Out of budget. Say so plainly instead of dropping a plain refresh into the pane:
    // a refresh that happens to reach the OLD server renders a healthy-looking report
    // for a restart that never completed, which is the same lie in a different place.
    const waited = Math.round((Date.now() - startedAt) / 1000);
    showStatus(`The server did not come back within ${waited}s`, 'error');
    renderHealthError(document.getElementById('healthOutput'), currentHealthUrl(),
        new Error(`No replacement server answered within ${waited}s. It may still be `
            + `draining, or the replacement failed to start — check the server log `
            + `(browserplug-server.startup.log names a spawn that died).`));
}

function renderHealth(out, data) {
    out.setAttribute('aria-busy', 'false');
    out.textContent = '';
    data = data || {};

    // Status pill: ok (reachable + extension connected) / warn (reachable but no
    // extension) / bad (server reports a non-ok status).
    const status = data.status || 'unknown';
    const connected = !!data.extension_connected;
    const pillClass = status === 'ok' ? (connected ? 'ok' : 'warn') : 'bad';
    const pill = document.createElement('span');
    pill.className = 'health-status-pill ' + pillClass;
    const dot = document.createElement('span');
    dot.className = 'dot';
    pill.appendChild(dot);
    pill.appendChild(document.createTextNode(status === 'ok'
        ? (connected ? 'Healthy; extension connected' : 'Healthy; no extension connected')
        : status));
    out.appendChild(pill);

    const rows = [
        ['Server PID', data.server_pid],
        ['Server version', data.server_version],
        ['Started', data.started],
        ['Uptime', formatUptime(data.uptime_seconds)],
        ['WebSocket port', data.ws_port],
        ['MCP HTTP port', data.mcp_port],
        ['Extension connected', boolCell(data.extension_connected)],
        ['Extension version', data.extension_version],
        ['Browser major', data.browser_major],
        ['Browser PIDs', arrCell(data.browser_pids || data.firefox_pids)],
        ['MCP client PIDs', arrCell(data.client_pids)],
        ['Local file access', fileAccessCell(data.extension_file_access)],
        ['Last command', lastCommandCell(data.last_command)],
        ['Generated at', data.generated_at],
    ];
    // Only when something is actually out of step; a permanent "—" row would read
    // as a field rather than as the alarm it is.
    if (data.version_warning) rows.splice(2, 0, ['Version mismatch', data.version_warning]);

    const dl = document.createElement('dl');
    dl.className = 'health-grid';
    rows.forEach(([key, val]) => {
        const dt = document.createElement('dt');
        dt.textContent = key;
        const dd = document.createElement('dd');
        appendCell(dd, val);
        dl.appendChild(dt);
        dl.appendChild(dd);
    });
    out.appendChild(dl);

    // Raw JSON (collapsed)
    const details = document.createElement('details');
    details.className = 'health-raw';
    const summary = document.createElement('summary');
    summary.textContent = 'Raw JSON';
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify(data, null, 2);
    details.appendChild(summary);
    details.appendChild(pre);
    out.appendChild(details);
}

function renderHealthError(out, url, error) {
    out.setAttribute('aria-busy', 'false');
    out.textContent = '';
    const box = document.createElement('div');
    box.className = 'health-error';
    const aborted = error && error.name === 'AbortError';

    const strong = document.createElement('strong');
    strong.textContent = aborted ? 'Request timed out.' : 'Server unreachable.';
    box.appendChild(strong);
    box.appendChild(document.createElement('br'));

    box.appendChild(document.createTextNode('Could not reach '));
    const code = document.createElement('code');
    code.textContent = url;
    box.appendChild(code);
    box.appendChild(document.createElement('br'));

    box.appendChild(document.createTextNode(aborted
        ? 'No response within 6 seconds.'
        : ((error && error.message) ? error.message : String(error))));
    box.appendChild(document.createElement('br'));
    box.appendChild(document.createTextNode(
        'Check the MCP HTTP Port in General settings and that the shared BrowserPlug server is running.'));
    out.appendChild(box);
}

// ---- Health cell helpers ---------------------------------------------------

// Append a value to a <dd>. Accepts a DOM Node, a string/number, or null/undefined
// (rendered as a muted em dash). Using DOM APIs (not innerHTML) avoids any HTML
// injection from server-reported strings.
function appendCell(dd, val) {
    if (val === null || val === undefined || val === '') {
        const m = document.createElement('span');
        m.className = 'muted';
        m.textContent = '—';
        dd.appendChild(m);
    } else if (val instanceof Node) {
        dd.appendChild(val);
    } else {
        dd.textContent = String(val);
    }
}

function boolCell(v) {
    if (v === null || v === undefined) return null;
    const span = document.createElement('span');
    span.className = v ? 'yes' : 'no';
    span.textContent = v ? 'yes' : 'no';
    return span;
}

function arrCell(arr) {
    if (!Array.isArray(arr) || arr.length === 0) return null;
    return arr.join(', ');
}

function formatUptime(seconds) {
    if (seconds === null || seconds === undefined) return null;
    let s = Math.floor(Number(seconds));
    if (isNaN(s)) return String(seconds);
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); s -= m * 60;
    const parts = [];
    if (d) parts.push(d + 'd');
    if (h || d) parts.push(h + 'h');
    if (m || h || d) parts.push(m + 'm');
    parts.push(s + 's');
    return `${parts.join(' ')} (${seconds}s)`;
}

function fileAccessCell(fa) {
    if (fa === null || fa === undefined) return null;
    const read = fa.can_read_opened_file_tab;
    if (read === true) return 'can read opened file:// tabs';
    if (read === false) return 'cannot read file:// tabs';
    return JSON.stringify(fa);
}

function lastCommandCell(lc) {
    if (lc === null || lc === undefined) return null;
    const tool = lc.tool || lc.action || 'command';
    const at = lc.at ? ' @ ' + lc.at : '';
    const ack = lc.acknowledged_by_extension ? ' ✓' : '';
    let out = tool + at + ack;
    // The per-hop breakdown the server assembled for this call (plus the
    // launcher's stdio-side report); same numbers the map pops as timers.
    const t = lc.timings;
    if (t) {
        const has = (v) => v !== null && v !== undefined;
        const bits = [];
        if (has(t.launcher_ms)) bits.push(`total ${fmtMs(t.launcher_ms)}`);
        else if (has(t.server_ms)) bits.push(`server ${fmtMs(t.server_ms)}`);
        if (has(t.server_dispatch_ms)) bits.push(`dispatch ${fmtMs(t.server_dispatch_ms)}`);
        if (has(t.browser_ms)) bits.push(`browser ${fmtMs(t.browser_ms)}`);
        if (has(t.server_finalize_ms)) bits.push(`finalize ${fmtMs(t.server_finalize_ms)}`);
        if (has(t.proxy_overhead_ms)) bits.push(`proxy ${fmtMs(t.proxy_overhead_ms)}`);
        if (bits.length) out += ' · ' + bits.join(' · ');
    }
    return out;
}

// ============================================================================
// Connection map (Health pane): a live SVG topology of everything connected
// to the shared server: MCP clients on the left, their stdio launchers next,
// the server in the middle, browsers on the right. No libraries, plain SVG
// built with DOM calls. Deep-dive doc: .tmp/dev-docs/connection-map.md
//
// Data: the server's get_connections_info(), fetched the same way the popup
// does (background relays `server.get_connections` over the live WebSocket;
// that answer also says WHICH browser we are, via requester_slot). When this
// browser's socket is down, falls back to GET /health, which embeds the same
// payload, reachable for a local server, and the map then merely lacks the
// "this browser" marker.
//
// It polls every MAP_POLL_MS, but ONLY while the Health pane is selected AND
// the page is actually visible (document.visibilityState); nothing runs from
// a background tab or while another pane is up.
//
// When a tool call COMPLETES between two polls (last_command arrives with
// finished timings), the map replays it: a worm runs the request path
// client → launcher → server → browser, a second one runs the response back,
// and "<N> ms" labels pop out of every hop, from last_command.timings.
// ============================================================================

const MAP_POLL_MS = 2000;
const MAP_ACTIVE_AGE_S = 4;    // edge "flow" lights while activity is this fresh (server clock)
const MAP_EXIT_MS = 380;       // must outlast the .dying opacity transition (.35s)
// Replay worm duration per hop. Full cycle = 8 × this (4 hops out + 4 back, since
// the extension and the browser are separate boxes); keep the cycle under
// MAP_POLL_MS or sustained traffic outruns the animations.
const MAP_SEG_MS = 240;
const MAP_REPLAY_QUEUE_MAX = 2; // waiting replays beyond this drop oldest-first
const SVG_NS = 'http://www.w3.org/2000/svg';

// Logical layout units; the SVG viewBox scales to the card's real width.
const MAP_W = 976;
const MAP_NODE_H = 44;
const MAP_LAUNCHER_H = 30;     // launchers are plumbing, drawn smaller on purpose
const MAP_EXT_H = MAP_LAUNCHER_H;  // so is the extension: same box size as a launcher
const MAP_ROW_H = 62;
const MAP_PAD_Y = 24;
// Five columns. `ext` and `browser` are deliberately separate parties: the WebSocket
// ends at the EXTENSION, which then drives the BROWSER through the WebExtension API;
// two different things with two different versions, lifetimes and failure modes.
const MAP_COL = {
    client:   { x: 12,  w: 190 },
    launcher: { x: 240, w: 100 },
    server:   { x: 384, w: 150 },
    ext:      { x: 580, w: 100 },
    browser:  { x: 772, w: 192 },
};

// Zoom/pan of the whole picture (a transform on the viewport <g>, not the viewBox,
// so layout and zoom stay independent). Five columns make the labels small at card
// width; this is how you read them.
const MAP_ZOOM_MIN = 0.5;
const MAP_ZOOM_MAX = 5;
const MAP_ZOOM_STEP = 1.25;    // one button press / one wheel notch
const MAP_PAN_MARGIN = 90;     // logical units of content that must stay in view

// A routing line is not a wire on the board: it is slung between two boxes and stands off
// them, so the view's own movement has to show on it. Panning swings the belly the way it
// swings on a cable you walk past, and panning down (looking at the board from higher up)
// foreshortens the droop. The numbers are set by what a hand can SEE: a 100 px drag has to
// move the belly far enough to read as movement, which is why the pan reference below is
// about a hand's width of drag and not the width of the board. Nothing is transformed in 3D.
const MAP_ROUTE_SAG_MIN = 14;        // even the shortest hop hangs enough to read as slack
const MAP_ROUTE_SAG_MAX = 30;        // and none of them hangs out of the bottom of the box
const MAP_ROUTE_SAG_RATIO = 0.14;    // ... of the span between the two ends
const MAP_ROUTE_SWING = 0.85;        // sideways lean of the belly at a fully leaned view
const MAP_ROUTE_FORESHORTEN = 0.35;  // droop lost when the view has panned fully down
const MAP_ROUTE_PAN_FULL = 110;      // pan units that get there (about 80 px of drag at 100%)

let mapPaneOpen = false;        // Health tab is the selected pane
let mapPollTimer = null;
let mapFetchBusy = false;
let mapHasSnapshot = false;     // ever rendered real data (drives the empty/stale states)
const mapNodes = new Map();     // key → {el, title, sub, portInEl, portOutEl, kind, w, h, x, y, tx, ty, data, dying}
const mapEdges = new Map();     // key → {el, line, flow, hit, titleEl, fromKey, toKey, dying}
const mapPrevActivity = new Map(); // node key → activity epoch from the previous snapshot
let mapRaf = null;
let mapTipKey = null;           // node key the tooltip currently describes
let mapReplayedKey = null;      // started_epoch of the last ENQUEUED command
// Replays are SERIALIZED: rapid command sequences (an agent hitting several
// tools per second) must not cancel or interleave each other's animations;
// each replay runs to completion, then the next starts.
let mapReplayQueue = [];
let mapReplayActive = false;
// Zoom/pan state (see applyMapTransform); survives every re-render because it lives
// on the persistent viewport <g>, not on anything the renderer rebuilds.
let mapZoom = 1;
let mapPanX = 0;
let mapPanY = 0;
let mapPanFrom = null;          // {x, y, panX, panY} while dragging
let mapZoomWired = false;       // the zoom/pan listeners are attached exactly once

function mapReducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

function initConnectionMap() {
    // Pause/resume with the page itself (switching away from the Firefox tab,
    // minimizing the window). The pane check lives in syncConnMapPolling.
    document.addEventListener('visibilitychange', syncConnMapPolling);
    initMapZoomPan();
}

// Everything drawn (nodes, edges, ms pops) lives inside ONE group so zoom/pan is a
// single transform on it; the svg's viewBox stays the pure layout space, which keeps
// the renderer's geometry math independent of how far the user has zoomed in.
// Created lazily so the module survives an options.html that predates it.
function mapViewport() {
    const svg = document.getElementById('connMapSvg');
    if (!svg) return null;
    let vp = document.getElementById('connMapViewport');
    if (!vp || vp.ownerSVGElement !== svg) {
        vp = document.createElementNS(SVG_NS, 'g');
        vp.setAttribute('id', 'connMapViewport');
        svg.appendChild(vp);
    }
    return vp;
}

// Called by selectTab() whenever the selected pane changes.
function setConnMapPaneOpen(open) {
    mapPaneOpen = !!open;
    syncConnMapPolling();
}

function syncConnMapPolling() {
    const run = mapPaneOpen && document.visibilityState !== 'hidden';
    const badge = document.getElementById('mapLive');
    const badgeText = document.getElementById('mapLiveText');
    if (badge) badge.classList.toggle('paused', !run);
    if (badgeText) badgeText.textContent = run ? 'live' : 'paused';
    if (run && mapPollTimer === null) {
        refreshConnectionMap();
        mapPollTimer = setInterval(refreshConnectionMap, MAP_POLL_MS);
    } else if (!run && mapPollTimer !== null) {
        clearInterval(mapPollTimer);
        mapPollTimer = null;
    }
}

async function refreshConnectionMap() {
    if (mapFetchBusy || document.visibilityState === 'hidden') return;
    mapFetchBusy = true;
    try {
        const got = await fetchConnMapData();
        renderConnectionMap(buildConnMapModel(got.data, got.via));
    } catch (e) {
        connMapUnreachable(e);
    } finally {
        mapFetchBusy = false;
    }
}

// WebSocket relay first (works wherever the server is, and identifies THIS
// browser); GET /health second (works while this extension is disconnected).
async function fetchConnMapData() {
    let wsError = null;
    try {
        const resp = await browser.runtime.sendMessage({ action: 'getServerConnections' });
        if (resp && resp.success && resp.data && Array.isArray(resp.data.connections)) {
            return { via: 'live WebSocket', data: resp.data };
        }
        wsError = (resp && resp.error) || 'no data over the WebSocket';
    } catch (e) {
        wsError = e.message || String(e);
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3000);
    try {
        const res = await fetch(currentHealthUrl(), await serverFetchInit(currentHealthUrl(), { method: 'GET', signal: controller.signal }));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const health = await res.json();
        return {
            via: 'HTTP /health',
            data: {
                connections: health.connections || [],
                extension_instances: health.extension_instances || [],
                generated_epoch: health.generated_epoch,
                generated_hms: health.generated_at,
                last_command: health.last_command || null,
                requester_slot: null,
            },
        };
    } catch (httpErr) {
        const httpMsg = (httpErr && httpErr.name === 'AbortError') ? 'timed out' : (httpErr.message || String(httpErr));
        throw new Error(`WebSocket (${wsError}) and ${currentHealthUrl()} (${httpMsg}) both failed`);
    } finally {
        clearTimeout(timer);
    }
}

// Normalize the server payload into what the renderer draws. All time math uses
// the SERVER's clock only (generated_epoch vs activity epochs).
function buildConnMapModel(payload, via) {
    const conns = Array.isArray(payload.connections) ? payload.connections : [];
    const hideEmptyEl = document.getElementById('hideEmptySessions');
    const hideEmpty = !!(hideEmptyEl && hideEmptyEl.checked);

    const model = {
        via,
        generatedEpoch: (typeof payload.generated_epoch === 'number') ? payload.generated_epoch : null,
        generatedHms: payload.generated_hms || null,
        lastCommand: payload.last_command || null,
        wsPort: null,
        httpPort: null,
        server: null,
        clients: [],
        launchers: [],
        extensions: [],
        browsers: [],
    };

    const srvRow = conns.find((c) => c.kind === 'server') || null;
    if (srvRow) {
        // detail is "ws://…:8772  +  http://…:2830/mcp/"; lift the two ports out.
        const ports = /:(\d+)\D+:(\d+)/.exec(srvRow.detail || '');
        if (ports) { model.wsPort = ports[1]; model.httpPort = ports[2]; }
    }
    model.server = {
        key: 'server',
        kind: 'server',
        title: 'BrowserPlug server',
        sub: srvRow ? [`pid ${srvRow.pid}`, srvRow.version ? 'v' + srvRow.version : null]
            .filter(Boolean).join(' · ') : 'unreachable',
        // Its two attachment points ARE the ports (drawn as stubs on the box).
        portInLabel: model.httpPort ? ':' + model.httpPort : 'http',
        portOutLabel: model.wsPort ? ':' + model.wsPort : 'ws',
        row: srvRow,
        activityEpoch: null,   // the hub pulses with everything; keep it calm instead
    };

    conns.filter((c) => c.kind === 'mcp_client').forEach((c) => {
        const sessions = Array.isArray(c.sessions) ? c.sessions : [];
        const withCommands = sessions.filter((s) => s.commands && s.commands.length).length;
        const total = (typeof c.session_count === 'number') ? c.session_count : sessions.length;
        const shown = hideEmpty ? withCommands : total;
        const cwdTail = c.cwd ? String(c.cwd).split(/[\\/]/).filter(Boolean).pop() : null;
        const launcherPid = c.launcher_pid;
        const node = {
            key: 'client:' + (c.pid !== null && c.pid !== undefined ? c.pid : c.label),
            kind: 'client',
            title: c.friendly || 'MCP client',
            sub: [
                (c.pid !== null && c.pid !== undefined) ? `pid ${c.pid}` : null,
                cwdTail,
            ].filter(Boolean).join(' · '),
            // How this client leaves its box: through a stdio launcher, or
            // straight over HTTP (e.g. GitLab Duo's http config).
            portOutLabel: (launcherPid !== null && launcherPid !== undefined) ? 'stdio' : 'http',
            launcherKey: null,
            // Which browser this client's calls are routed to (server-side state; the
            // map draws it as the orange routing line). Null when the server predates
            // the field, nothing is connected, or its selection points at a browser
            // that left — that client's calls error, so no line is the honest picture.
            targetBrowserKey: (c.target_browser_slot !== null && c.target_browser_slot !== undefined)
                ? 'browser:' + c.target_browser_slot : null,
            targetBrowserExplicit: !!c.target_browser_explicit,
            sessionsShown: shown,
            sessionsTotal: total,
            row: c,
            activityEpoch: (typeof c.activity_epoch === 'number') ? c.activity_epoch : null,
        };
        if (launcherPid !== null && launcherPid !== undefined) {
            node.launcherKey = 'launcher:' + launcherPid;
            model.launchers.push({
                key: node.launcherKey,
                kind: 'launcher',
                title: 'launcher',
                sub: 'pid ' + launcherPid,
                portInLabel: 'stdio',
                portOutLabel: 'http',
                clientKey: node.key,
                clientTitle: node.title,
                row: { pid: launcherPid, since_hms: c.launcher_since_hms || null,
                       version: c.launcher_version || null },
                activityEpoch: null,   // its traffic is the client's; don't double-pulse
            });
        }
        model.clients.push(node);
    });

    // Each connected browser becomes TWO nodes: the extension the WebSocket actually
    // reaches, and the browser it drives through the WebExtension API. They come from
    // the registry (extension_instances); the matching connections row (by slot; by
    // order for an older server) adds process facts.
    const extRows = conns.filter((c) => c.kind === 'extension');
    const instances = (Array.isArray(payload.extension_instances) ? payload.extension_instances : [])
        .filter((i) => i.currently_connected);
    instances.forEach((inst, idx) => {
        const row = extRows.find((c) => c.slot === inst.slot) || extRows[idx] || null;
        const remote = !!(inst.is_remote || (row && row.is_remote) || isRemoteAddr(inst.remote_addr));
        const isSelf = payload.requester_slot !== null && payload.requester_slot !== undefined
            && inst.slot === payload.requester_slot;
        const subBits = [];
        if (remote) subBits.push(inst.remote_addr || 'remote');
        else if (inst.pid) subBits.push(`pid ${inst.pid}`);
        if (isSelf) subBits.push('this browser');
        // `friendly` (the edition) over `name`: name carries the pid, which the sub
        // line and the tooltip already show; don't print it twice.
        const browserTitle = inst.friendly || inst.name || 'Firefox';
        const extKey = 'ext:' + inst.slot;
        const browserKey = 'browser:' + inst.slot;
        // REAL commands only; the map's own polling must not light its own edge.
        const activityEpoch = (typeof inst.last_activity_epoch === 'number')
            ? inst.last_activity_epoch : null;
        model.extensions.push({
            key: extKey,
            kind: 'ext',
            title: 'extension',
            sub: inst.version ? 'v' + inst.version : '',
            portInLabel: 'ws',
            portOutLabel: 'api',
            browserKey,
            browserTitle,
            remote,
            isSelf,
            inst,
            row,
            activityEpoch,
        });
        model.browsers.push({
            key: browserKey,
            kind: 'browser',
            title: browserTitle,
            sub: subBits.join(' · '),
            portInLabel: 'webext',
            remote,
            // One REMOTE pill per browser, on the browser box; the extension box
            // beside it is on the same machine and would only repeat the badge.
            remotePill: remote,
            isSelf,
            extKey,
            inst,
            row,
            activityEpoch,
        });
    });

    return model;
}

function isRemoteAddr(addr) {
    return !!addr && addr !== '127.0.0.1' && addr !== '::1' && String(addr).toLowerCase() !== 'localhost';
}

function connMapUnreachable(err) {
    const wrap = document.getElementById('connMap');
    const msg = document.getElementById('connMapMsg');
    const src = document.getElementById('connMapSource');
    if (!wrap || !msg) return;
    if (mapHasSnapshot) {
        // Keep the last picture, ghosted; better than a blank while it restarts.
        wrap.classList.add('stale');
        msg.textContent = 'Lost the server; retrying every 2 s…';
    } else {
        msg.textContent = 'No server answered. Is the shared BrowserPlug server running? Retrying every 2 s…';
    }
    msg.hidden = false;
    if (src) src.textContent = '';
    console.error('Connection map fetch failed:', err);
}

// ---- rendering --------------------------------------------------------------

function renderConnectionMap(model) {
    const svg = document.getElementById('connMapSvg');
    const wrap = document.getElementById('connMap');
    const msg = document.getElementById('connMapMsg');
    const src = document.getElementById('connMapSource');
    if (!svg || !wrap) return;
    const vp = mapViewport();

    const firstSnapshot = !mapHasSnapshot;
    mapHasSnapshot = true;
    wrap.classList.remove('stale');
    if (msg) msg.hidden = true;
    if (src) {
        src.textContent = `via ${model.via}` + (model.generatedHms ? ` · ${model.generatedHms}` : '');
    }

    // ---- layout: fix every node's target position ----
    const rows = Math.max(1, model.clients.length, model.browsers.length);
    const height = Math.max(150, rows * MAP_ROW_H + MAP_PAD_Y * 2);
    svg.setAttribute('viewBox', `0 0 ${MAP_W} ${height}`);

    const colY = (n, i, h) => (height - n * MAP_ROW_H) / 2 + i * MAP_ROW_H + (MAP_ROW_H - h) / 2;
    model.server.tx = MAP_COL.server.x;
    model.server.ty = height / 2 - MAP_NODE_H / 2;
    model.clients.forEach((n, i) => { n.tx = MAP_COL.client.x; n.ty = colY(model.clients.length, i, MAP_NODE_H); });
    // extensions[i] and browsers[i] are the same browser, same row, by construction.
    model.extensions.forEach((n, i) => { n.tx = MAP_COL.ext.x; n.ty = colY(model.extensions.length, i, MAP_EXT_H); });
    model.browsers.forEach((n, i) => { n.tx = MAP_COL.browser.x; n.ty = colY(model.browsers.length, i, MAP_NODE_H); });
    // A launcher rides its client's row, centered on the same midline.
    const clientsByKey = new Map();
    model.clients.forEach((c) => clientsByKey.set(c.key, c));
    model.launchers.forEach((l) => {
        const c = clientsByKey.get(l.clientKey);
        l.tx = MAP_COL.launcher.x;
        l.ty = (c ? c.ty : height / 2 - MAP_NODE_H / 2) + (MAP_NODE_H - MAP_LAUNCHER_H) / 2;
    });

    const nodes = [model.server].concat(model.clients, model.launchers,
                                        model.extensions, model.browsers);
    const seenNodes = new Set();

    nodes.forEach((n) => {
        seenNodes.add(n.key);
        let st = mapNodes.get(n.key);
        if (st && st.dying) {           // came back before its fade-out finished
            st.el.remove();
            mapNodes.delete(n.key);
            st = null;
        }
        const isNew = !st;
        if (isNew) {
            st = createMapNode(n);
            mapNodes.set(n.key, st);
            vp.appendChild(st.el);
            st.x = n.tx;                // new nodes fade in IN PLACE at their spot
            st.y = n.ty;
            st.el.setAttribute('transform', `translate(${st.x} ${st.y})`);
            st.el.classList.add('enter');
            requestAnimationFrame(() => requestAnimationFrame(() => st.el.classList.remove('enter')));
        }
        st.tx = n.tx;
        st.ty = n.ty;
        st.data = n;
        updateMapNode(st, n);

        // Activity pulse: only on a CHANGE observed between two snapshots.
        const prev = mapPrevActivity.get(n.key);
        if (!isNew && prev !== undefined && n.activityEpoch !== null && n.activityEpoch > prev + 0.05) {
            pulseMapNode(st);
        }
        if (n.activityEpoch !== null) mapPrevActivity.set(n.key, n.activityEpoch);
    });

    // ---- edges: one per link, keyed from>to so a rewire redraws cleanly ----
    const httpLabel = `MCP over HTTP${model.httpPort ? ' :' + model.httpPort : ''}`;
    const edgeDefs = [];
    model.clients.forEach((c) => {
        if (c.launcherKey) {
            edgeDefs.push({ from: c.key, to: c.launcherKey, remote: false,
                            activityEpoch: c.activityEpoch,
                            title: `stdio: ${c.title} ⇄ launcher` });
            edgeDefs.push({ from: c.launcherKey, to: 'server', remote: false,
                            activityEpoch: c.activityEpoch,
                            title: `${httpLabel}: launcher (${c.title}) ⇄ server` });
        } else {
            edgeDefs.push({ from: c.key, to: 'server', remote: false,
                            activityEpoch: c.activityEpoch,
                            title: `${httpLabel}: ${c.title} ⇄ server` });
        }
    });
    // The WebSocket ends at the EXTENSION; the extension drives its browser in-process.
    model.extensions.forEach((x) => {
        edgeDefs.push({ from: 'server', to: x.key, remote: x.remote,
                        activityEpoch: x.activityEpoch,
                        title: `WebSocket${model.wsPort ? ' :' + model.wsPort : ''}: server ⇄ `
                             + `extension in ${x.browserTitle}`
                             + (x.remote ? ` (remote ${(x.inst && x.inst.remote_addr) || ''})` : '') });
    });
    model.browsers.forEach((b) => {
        edgeDefs.push({ from: b.extKey, to: b.key, remote: b.remote,
                        activityEpoch: b.activityEpoch,
                        title: `WebExtension API: extension ⇄ ${b.title} (in-process)` });
    });
    // ROUTING line (orange, arrowed, over the top of the hops): which browser this
    // client's tool calls actually land in. The hop edges say how the bytes travel;
    // with several browsers connected they can't say WHICH one a client is driving.
    const browserKeys = new Set(model.browsers.map((b) => b.key));
    model.clients.forEach((c) => {
        if (!c.targetBrowserKey || !browserKeys.has(c.targetBrowserKey)) return;
        const target = model.browsers.find((b) => b.key === c.targetBrowserKey);
        edgeDefs.push({
            from: c.launcherKey || c.key,
            to: c.targetBrowserKey,
            route: true,
            remote: false,
            activityEpoch: null,     // the hops already carry the traffic animation
            title: `${c.title}'s calls are routed to ${target ? target.title : 'this browser'}`
                 + (c.targetBrowserExplicit
                     ? ' (selected with browsers(action="use"))'
                     : ' (the default: no browser explicitly selected)'),
        });
    });

    const seenEdges = new Set();
    edgeDefs.forEach((def) => {
        const ekey = edgeKeyFor(def.from, def.to);
        seenEdges.add(ekey);
        let est = mapEdges.get(ekey);
        if (est && est.dying) {
            est.el.remove();
            mapEdges.delete(ekey);
            est = null;
        }
        if (!est) {
            est = createMapEdge(def.from, def.to);
            mapEdges.set(ekey, est);
            vp.insertBefore(est.el, vp.firstChild);   // edges under every node
            est.el.classList.add('enter');
            requestAnimationFrame(() => requestAnimationFrame(() => est.el.classList.remove('enter')));
        }
        est.el.classList.toggle('remote', !!def.remote);
        // A routing line is drawn differently (arrowed, dipping under the boxes it
        // flies over), so the geometry has to know; redrawMapEdges reads est.route.
        est.route = !!def.route;
        est.el.classList.toggle('route', est.route);
        est.line.setAttribute('marker-end', est.route ? 'url(#mapRouteArrow)' : '');
        const age = (model.generatedEpoch !== null && def.activityEpoch !== null && def.activityEpoch !== undefined)
            ? model.generatedEpoch - def.activityEpoch : Infinity;
        est.el.classList.toggle('active', age < MAP_ACTIVE_AGE_S);
        est.titleEl.textContent = def.title;
    });

    // ---- anything not in this snapshot fades out, then leaves the DOM ----
    mapNodes.forEach((st, key) => {
        if (seenNodes.has(key) || st.dying) return;
        st.dying = true;
        st.el.classList.add('dying');
        if (mapTipKey === key) hideMapTip();
        setTimeout(() => {
            if (!st.dying) return;
            st.el.remove();
            mapNodes.delete(key);
            mapPrevActivity.delete(key);
        }, MAP_EXIT_MS);
    });
    mapEdges.forEach((est, key) => {
        if (seenEdges.has(key) || est.dying) return;
        est.dying = true;
        est.el.classList.add('dying');
        setTimeout(() => {
            if (!est.dying) return;
            est.el.remove();
            mapEdges.delete(key);
        }, MAP_EXIT_MS);
    });

    // Tooltip open on a node that just changed? Refresh what it says.
    if (mapTipKey) {
        const st = mapNodes.get(mapTipKey);
        if (st && !st.dying) fillMapTip(st.data);
        else hideMapTip();
    }

    // Glue edges to their (possibly brand-new) nodes NOW: a replay may spawn a
    // worm before the first animation frame gets to redraw them.
    redrawMapEdges();
    maybeReplayCommand(model, firstSnapshot);
    kickMapAnimation();
    // The viewBox height moves with the number of rows, so re-clamp the pan against
    // the box that actually exists now (a no-op at the default 100% / no pan).
    applyMapTransform();
}

function createMapNode(n) {
    const w = MAP_COL[n.kind].w;
    // Launcher and extension are the small boxes: plumbing between the two ends that
    // matter (the MCP client and the browser), and drawn the same size as each other.
    const compact = n.kind === 'launcher' || n.kind === 'ext';
    const h = n.kind === 'launcher' ? MAP_LAUNCHER_H : n.kind === 'ext' ? MAP_EXT_H : MAP_NODE_H;
    const el = document.createElementNS(SVG_NS, 'g');
    el.setAttribute('class', `mn k-${n.kind}`);
    el.setAttribute('data-key', n.key);
    el.setAttribute('tabindex', '0');

    const ring = document.createElementNS(SVG_NS, 'rect');
    ring.setAttribute('class', 'mn-ring');
    ring.setAttribute('x', '-3');
    ring.setAttribute('y', '-3');
    ring.setAttribute('width', String(w + 6));
    ring.setAttribute('height', String(h + 6));
    ring.setAttribute('rx', '12');
    el.appendChild(ring);
    ring.addEventListener('animationend', () => el.classList.remove('pulse'));

    const box = document.createElementNS(SVG_NS, 'rect');
    box.setAttribute('class', 'mn-box');
    box.setAttribute('width', String(w));
    box.setAttribute('height', String(h));
    box.setAttribute('rx', compact ? '8' : '10');
    el.appendChild(box);

    const dot = document.createElementNS(SVG_NS, 'circle');
    dot.setAttribute('class', 'mn-dot');
    dot.setAttribute('cx', compact ? '11' : '16');
    dot.setAttribute('cy', String(h / 2));
    dot.setAttribute('r', compact ? '4' : '5');
    el.appendChild(dot);

    const textX = compact ? '21' : '30';
    const title = document.createElementNS(SVG_NS, 'text');
    title.setAttribute('class', 'mn-title');
    title.setAttribute('x', textX);
    title.setAttribute('y', compact ? '13' : '19');
    el.appendChild(title);

    const sub = document.createElementNS(SVG_NS, 'text');
    sub.setAttribute('class', 'mn-sub');
    sub.setAttribute('x', textX);
    sub.setAttribute('y', compact ? '25' : '34');
    el.appendChild(sub);

    if (n.remotePill) {
        const pillW = 50;
        const pill = document.createElementNS(SVG_NS, 'rect');
        pill.setAttribute('class', 'mn-pill');
        pill.setAttribute('x', String(w - pillW - 6));
        pill.setAttribute('y', '6');
        pill.setAttribute('width', String(pillW));
        pill.setAttribute('height', '12');
        pill.setAttribute('rx', '6');
        el.appendChild(pill);
        const pillText = document.createElementNS(SVG_NS, 'text');
        pillText.setAttribute('class', 'mn-pill-text');
        pillText.setAttribute('x', String(w - pillW / 2 - 6));
        pillText.setAttribute('y', '15');
        pillText.setAttribute('text-anchor', 'middle');
        pillText.textContent = 'REMOTE';
        el.appendChild(pillText);
    }

    const st = { el, title, sub, portInEl: null, portOutEl: null,
                 kind: n.kind, w, h, x: 0, y: 0, tx: 0, ty: 0, data: n, dying: false };

    // Attachment points: a stub per port/pipe this box exposes. Whether a node
    // HAS an in/out port is fixed by its kind, so creating them once is safe.
    if (n.portInLabel !== undefined) st.portInEl = addPortStub(el, 'in', 0, h);
    if (n.portOutLabel !== undefined) st.portOutEl = addPortStub(el, 'out', w, h);

    el.addEventListener('mouseenter', (e) => { showMapTip(st); positionMapTip(e.clientX, e.clientY); });
    el.addEventListener('mousemove', (e) => positionMapTip(e.clientX, e.clientY));
    el.addEventListener('mouseleave', hideMapTip);
    el.addEventListener('focus', () => {
        showMapTip(st);
        const r = el.getBoundingClientRect();
        positionMapTip(r.left + r.width / 2, r.bottom);
    });
    el.addEventListener('blur', hideMapTip);

    return st;
}

// A port stub: the visible attachment point every edge glues to, a socket on
// the box border plus a tiny label. Outgoing (right-edge) labels sit ABOVE the
// line and incoming (left-edge) labels BELOW it, so the two ends of one link
// can't collide in the gap between columns. Returns the label element.
function addPortStub(el, side, x, h) {
    const dot = document.createElementNS(SVG_NS, 'circle');
    dot.setAttribute('class', 'mn-port-dot');
    dot.setAttribute('cx', String(x));
    dot.setAttribute('cy', String(h / 2));
    dot.setAttribute('r', '3.5');
    el.appendChild(dot);
    const label = document.createElementNS(SVG_NS, 'text');
    label.setAttribute('class', 'mn-port');
    if (side === 'out') {
        label.setAttribute('x', String(x + 6));
        label.setAttribute('y', String(h / 2 - 6));
        label.setAttribute('text-anchor', 'start');
    } else {
        label.setAttribute('x', String(x - 6));
        label.setAttribute('y', String(h / 2 + 14));
        label.setAttribute('text-anchor', 'end');
    }
    el.appendChild(label);
    return label;
}

function updateMapNode(st, n) {
    // Truncation budget follows the box width (mn-title ≈6.6px/char, mn-sub ≈6px), minus
    // the text inset, which is smaller on the compact boxes (launcher, extension).
    const inset = (st.kind === 'launcher' || st.kind === 'ext') ? 26 : 44;
    const titleMax = Math.max(8, Math.floor((st.w - inset) / 6.6));
    const subMax = Math.max(8, Math.floor((st.w - inset + 4) / 6.0));
    st.title.textContent = truncEnd(n.title, titleMax);
    st.sub.textContent = truncMiddle(n.sub || '', subMax);
    if (st.portInEl) st.portInEl.textContent = n.portInLabel || '';
    if (st.portOutEl) st.portOutEl.textContent = n.portOutLabel || '';
    st.el.classList.toggle('remote', !!n.remote);
    st.el.classList.toggle('self', !!n.isSelf);
    st.el.setAttribute('aria-label',
        `${n.title}${n.sub ? '; ' + n.sub : ''}${n.remote ? ' (remote)' : ''}`);
}

function pulseMapNode(st) {
    if (mapReducedMotion()) return;
    st.el.classList.remove('pulse');
    // Force a style flush so re-adding the class restarts the ring animation.
    void st.el.getBoundingClientRect();
    st.el.classList.add('pulse');
}

function edgeKeyFor(fromKey, toKey) {
    return 'edge:' + fromKey + '>' + toKey;
}

function createMapEdge(fromKey, toKey) {
    const el = document.createElementNS(SVG_NS, 'g');
    el.setAttribute('class', 'me');
    el.setAttribute('data-key', edgeKeyFor(fromKey, toKey));

    const line = document.createElementNS(SVG_NS, 'path');
    line.setAttribute('class', 'me-line');
    el.appendChild(line);

    const flow = document.createElementNS(SVG_NS, 'path');
    flow.setAttribute('class', 'me-flow');
    el.appendChild(flow);

    const hit = document.createElementNS(SVG_NS, 'path');
    hit.setAttribute('class', 'me-hit');
    el.appendChild(hit);

    // Native SVG tooltip; plenty for a line; nodes get the rich one.
    const titleEl = document.createElementNS(SVG_NS, 'title');
    el.appendChild(titleEl);

    return { el, line, flow, hit, titleEl, fromKey, toKey, dying: false };
}

// One rAF loop: ease every node toward its target and keep the edges glued to
// the nodes while they move. Runs only while something is actually moving.
function kickMapAnimation() {
    if (mapRaf !== null) return;
    const step = () => {
        mapRaf = null;
        let moving = false;
        const snap = mapReducedMotion();
        mapNodes.forEach((st) => {
            if (st.x !== st.tx || st.y !== st.ty) {
                if (snap) {
                    st.x = st.tx;
                    st.y = st.ty;
                } else {
                    st.x += (st.tx - st.x) * 0.22;
                    st.y += (st.ty - st.y) * 0.22;
                    if (Math.abs(st.tx - st.x) < 0.6 && Math.abs(st.ty - st.y) < 0.6) {
                        st.x = st.tx;
                        st.y = st.ty;
                    } else {
                        moving = true;
                    }
                }
                st.el.setAttribute('transform', `translate(${st.x} ${st.y})`);
            }
        });
        redrawMapEdges();
        if (moving) mapRaf = requestAnimationFrame(step);
    };
    mapRaf = requestAnimationFrame(step);
}

// A cable slung between two points, as ONE quadratic Bézier: the droop 4t(1-t)·sag is
// quadratic in t, so the curve whose control point is the chord's midpoint pushed
// 2·sag along (hx, hy) IS the hanging line — no sampling, and the end tangents lift the
// arrowhead into the box the way a real cable meets its post.
function mapHangPath(x1, y1, x2, y2, sag, hx, hy) {
    const cx = (x1 + x2) / 2 + 2 * sag * hx;
    const cy = (y1 + y2) / 2 + 2 * sag * hy;
    return `M ${x1} ${y1} Q ${cx.toFixed(2)} ${cy.toFixed(2)}, ${x2} ${y2}`;
}

function mapClampUnit(n) {
    return isFinite(n) ? Math.max(-1, Math.min(1, n)) : 0;
}

// Which way the routing cables hang, for the view as it stands. The pan itself is the
// input, not the camera's position over the board: a pan of 0 is the reset view and hangs
// straight down at every zoom, and one pan unit is the same amount of drag whether the
// picture sits at 50% or 500% (mapClientScale() leaves zoom out of it). Deriving it from
// mapCenterPoint() instead read as a bug: zooming in with no pan starts off-centre, so the
// cables leaned before the user had dragged anything.
function mapRouteHang() {
    return { x: MAP_ROUTE_SWING * mapClampUnit(mapPanX / MAP_ROUTE_PAN_FULL),
             y: 1 - MAP_ROUTE_FORESHORTEN * mapClampUnit(mapPanY / MAP_ROUTE_PAN_FULL) };
}

function redrawMapEdges() {
    const hang = mapRouteHang();
    mapEdges.forEach((est) => {
        const from = mapNodes.get(est.fromKey);
        const to = mapNodes.get(est.toKey);
        if (!from || !to) return;
        let d;
        if (est.route) {
            // Not a wire: it flies from the client's last box to the browser its calls
            // land in, over the hops in between. Leaves and arrives at the BOTTOM edge
            // and HANGS below, so it can't be read as another link between two ports
            // (and edges sit under every node, so it passes behind the boxes it crosses).
            // The longer the throw, the deeper the slack, as a slung cable behaves.
            const x1 = from.x + from.w / 2;
            const y1 = from.y + from.h;
            const x2 = to.x + to.w / 2;
            const y2 = to.y + to.h;
            const sag = Math.min(MAP_ROUTE_SAG_MAX, Math.max(MAP_ROUTE_SAG_MIN,
                Math.hypot(x2 - x1, y2 - y1) * MAP_ROUTE_SAG_RATIO));
            d = mapHangPath(x1, y1, x2, y2, sag, hang.x, hang.y);
        } else {
            // Out of the source's right-edge port stub, into the target's left-edge one.
            const x1 = from.x + from.w;
            const y1 = from.y + from.h / 2;
            const x2 = to.x;
            const y2 = to.y + to.h / 2;
            const mx = (x1 + x2) / 2;
            d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
        }
        est.line.setAttribute('d', d);
        est.flow.setAttribute('d', d);
        est.hit.setAttribute('d', d);
    });
}

// ---- command replay: worms out, worms back, timers popping off every hop ----

// A NEW completed command showed up between two polls → queue its replay. Keyed
// on timings.started_epoch so each call replays exactly once; a call watched
// while in flight replays when its timings turn complete. The queue is what
// keeps rapid sequences WATCHABLE: replays never cancel or interleave each
// other (that read as out-of-order animation, seen live 2026-07-15); when
// commands outrun the animations, the oldest waiting replays drop instead.
function maybeReplayCommand(model, firstSnapshot) {
    const lc = model.lastCommand;
    if (!lc || !lc.timings || !lc.timings.complete) return;
    const key = (lc.timings.started_epoch !== null && lc.timings.started_epoch !== undefined)
        ? lc.timings.started_epoch : lc.at;
    if (key === null || key === undefined || key === mapReplayedKey) return;
    mapReplayedKey = key;
    if (firstSnapshot) return;   // history from before the pane opened; don't reenact it
    mapReplayQueue.push(lc);
    if (mapReplayQueue.length > MAP_REPLAY_QUEUE_MAX) {
        mapReplayQueue.splice(0, mapReplayQueue.length - MAP_REPLAY_QUEUE_MAX);
    }
    pumpReplayQueue();
}

function pumpReplayQueue() {
    if (mapReplayActive) return;
    const lc = mapReplayQueue.shift();
    if (!lc) return;
    mapReplayActive = true;
    replayCommand(lc, () => {
        mapReplayActive = false;
        pumpReplayQueue();
    });
}

function replayCommand(lc, done) {
    const finish = () => { if (done) done(); };
    const clientKey = (lc.client_pid !== null && lc.client_pid !== undefined)
        ? 'client:' + lc.client_pid : null;
    const clientSt = clientKey ? mapNodes.get(clientKey) : null;
    const launcherKey = clientSt && clientSt.data ? clientSt.data.launcherKey : null;
    const hasSlot = lc.browser_slot !== null && lc.browser_slot !== undefined;
    const extKey = hasSlot ? 'ext:' + lc.browser_slot : null;
    const browserKey = hasSlot ? 'browser:' + lc.browser_slot : null;

    // The request path, hop by hop; the response retraces it backwards.
    const hops = [];
    if (clientSt && launcherKey && mapNodes.has(launcherKey)) {
        hops.push(edgeKeyFor(clientKey, launcherKey));
        hops.push(edgeKeyFor(launcherKey, 'server'));
    } else if (clientSt) {
        hops.push(edgeKeyFor(clientKey, 'server'));
    }
    // …and on into the extension, which then acts on its browser.
    if (extKey && mapNodes.has(extKey)) {
        hops.push(edgeKeyFor('server', extKey));
        if (browserKey && mapNodes.has(browserKey)) {
            hops.push(edgeKeyFor(extKey, browserKey));
        }
    }

    if (mapReducedMotion() || !hops.length) {
        spawnMsPops(lc);
        finish();
        return;
    }
    let at = 0;
    hops.forEach((ek) => { scheduleWorm(ek, 'fwd', at); at += MAP_SEG_MS; });
    hops.slice().reverse().forEach((ek) => { scheduleWorm(ek, 'rev', at); at += MAP_SEG_MS; });
    // Pops spawn as the cycle closes; they fade on their own while the next
    // queued replay (if any) starts its worms.
    setTimeout(() => { spawnMsPops(lc); finish(); }, at + 60);
}

function scheduleWorm(edgeKey, dir, delayMs) {
    setTimeout(() => {
        // The edge may have died while this replay waited its turn.
        const est = mapEdges.get(edgeKey);
        if (!est || est.dying) return;
        const worm = document.createElementNS(SVG_NS, 'path');
        worm.setAttribute('class', 'me-worm ' + dir);
        worm.setAttribute('d', est.line.getAttribute('d') || '');
        // pathLength normalizes every hop to 100 units, so one pair of CSS
        // keyframes animates any segment regardless of its real length.
        worm.setAttribute('pathLength', '100');
        worm.style.animationDuration = MAP_SEG_MS + 'ms';
        est.el.appendChild(worm);
        setTimeout(() => worm.remove(), MAP_SEG_MS + 160);
    }, delayMs);
}

// "<N> ms" timers popping out of each piece of the finished call cycle, from
// last_command.timings: total at the client, proxy overhead at the launcher,
// dispatch at the server, and the browser leg at the browser.
function spawnMsPops(lc) {
    const svg = mapViewport();
    if (!svg) return;
    const t = lc.timings || {};
    const has = (v) => v !== null && v !== undefined;

    const clientSt = has(lc.client_pid) ? mapNodes.get('client:' + lc.client_pid) : null;
    const launcherSt = clientSt && clientSt.data && clientSt.data.launcherKey
        ? mapNodes.get(clientSt.data.launcherKey) : null;
    const serverSt = mapNodes.get('server');
    const browserSt = has(lc.browser_slot) ? mapNodes.get('browser:' + lc.browser_slot) : null;

    const pops = [];
    if (clientSt && has(t.launcher_ms)) pops.push([clientSt, t.launcher_ms, 'total']);
    if (launcherSt && has(t.proxy_overhead_ms)) pops.push([launcherSt, t.proxy_overhead_ms, 'proxy']);
    // A server-side-only call (browsers, server_info) has no dispatch leg; its
    // whole server stay is the number worth popping there.
    if (serverSt && has(t.server_dispatch_ms)) pops.push([serverSt, t.server_dispatch_ms, 'dispatch']);
    else if (serverSt && has(t.server_ms)) pops.push([serverSt, t.server_ms, 'server']);
    if (browserSt && has(t.browser_ms)) pops.push([browserSt, t.browser_ms, 'in browser']);

    pops.forEach(([st, ms, label], i) => {
        const g = document.createElementNS(SVG_NS, 'g');
        g.setAttribute('class', 'ms-pop');
        g.setAttribute('transform', `translate(${st.x + st.w / 2} ${st.y - 7})`);
        const text = document.createElementNS(SVG_NS, 'text');
        text.setAttribute('class', 'ms-pop-text');
        text.setAttribute('text-anchor', 'middle');
        text.style.animationDelay = (i * 110) + 'ms';
        text.textContent = `${fmtMs(ms)} ${label}`;
        g.appendChild(text);
        svg.appendChild(g);
        setTimeout(() => g.remove(), 2700 + i * 110);
    });
}

function fmtMs(ms) {
    ms = Number(ms);
    if (!isFinite(ms) || ms < 1) return '<1 ms';
    if (ms < 10) return (Math.round(ms * 10) / 10) + ' ms';
    return Math.round(ms) + ' ms';
}

// ---- zoom & pan -------------------------------------------------------------
// Five columns of 7.5–12.5px type scaled down to the card's width is a lot to ask
// of a reader, so the whole picture zooms (buttons, wheel, double-click to reset)
// and pans (drag). It is ONE transform on the viewport <g>: the renderer keeps
// laying out in untouched logical units and knows nothing about any of this.

function initMapZoomPan() {
    const svg = document.getElementById('connMapSvg');
    const wrap = document.getElementById('connMap');
    if (!svg || !wrap || mapZoomWired) return;
    // Wire ONCE: a second init (DOMContentLoaded can be delivered twice under a
    // test harness) would otherwise make every button press count double.
    mapZoomWired = true;

    const zin = document.getElementById('connMapZoomIn');
    const zout = document.getElementById('connMapZoomOut');
    const zreset = document.getElementById('connMapZoomReset');
    if (zin) zin.addEventListener('click', () => nudgeMapZoom(MAP_ZOOM_STEP));
    if (zout) zout.addEventListener('click', () => nudgeMapZoom(1 / MAP_ZOOM_STEP));
    if (zreset) zreset.addEventListener('click', resetMapView);

    // Wheel zooms about the pointer, which means swallowing the scroll; hence the
    // explicitly non-passive listener. But ONLY while the zoom actually moves: at
    // either limit the wheel goes back to scrolling the page, so a settings page can
    // never trap the reader inside a widget.
    svg.addEventListener('wheel', (e) => {
        const next = clampMapZoom(mapZoom * Math.pow(MAP_ZOOM_STEP, e.deltaY > 0 ? -1 : 1));
        if (Math.abs(next - mapZoom) < 0.0005) return;
        e.preventDefault();
        setMapZoom(next, mapPointAt(e.clientX, e.clientY));
    }, { passive: false });

    svg.addEventListener('dblclick', resetMapView);

    // Drag to pan. Pointer capture keeps the drag alive when the cursor leaves the
    // box; the tooltip goes away because it tracks the pointer and would smear along.
    svg.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        mapPanFrom = { x: e.clientX, y: e.clientY, panX: mapPanX, panY: mapPanY,
                       scale: mapClientScale() };
        wrap.classList.add('panning');
        hideMapTip();
        try { svg.setPointerCapture(e.pointerId); } catch (_) { /* jsdom / no capture */ }
    });
    svg.addEventListener('pointermove', (e) => {
        if (!mapPanFrom) return;
        e.preventDefault();
        const k = mapPanFrom.scale || 1;
        mapPanX = mapPanFrom.panX + (e.clientX - mapPanFrom.x) / k;
        mapPanY = mapPanFrom.panY + (e.clientY - mapPanFrom.y) / k;
        applyMapTransform();
    });
    const endPan = (e) => {
        if (!mapPanFrom) return;
        mapPanFrom = null;
        wrap.classList.remove('panning');
        try { svg.releasePointerCapture(e.pointerId); } catch (_) { /* ditto */ }
    };
    svg.addEventListener('pointerup', endPan);
    svg.addEventListener('pointercancel', endPan);

    applyMapTransform();
}

// The svg's viewBox as [w, h], parsed from the attribute rather than read off
// svg.viewBox.baseVal, which jsdom does not implement.
function mapViewBox() {
    const svg = document.getElementById('connMapSvg');
    const parts = ((svg && svg.getAttribute('viewBox')) || '').trim().split(/[\s,]+/).map(Number);
    if (parts.length === 4 && parts.every((n) => isFinite(n)) && parts[2] > 0 && parts[3] > 0) {
        return { w: parts[2], h: parts[3] };
    }
    return { w: MAP_W, h: 150 };
}

// CSS pixels per logical unit (the svg is width:100% of the card). 0 when the page
// has no layout yet (jsdom, or the pane never shown); callers fall back.
function mapClientScale() {
    const svg = document.getElementById('connMapSvg');
    if (!svg || !svg.getBoundingClientRect) return 0;
    const rect = svg.getBoundingClientRect();
    return rect.width ? rect.width / mapViewBox().w : 0;
}

// Logical (pre-transform) coordinates under a client point, the anchor a zoom
// keeps still. Null when there is no layout to measure against.
function mapPointAt(clientX, clientY) {
    const svg = document.getElementById('connMapSvg');
    const k = mapClientScale();
    if (!svg || !k) return null;
    const rect = svg.getBoundingClientRect();
    return {
        x: ((clientX - rect.left) / k - mapPanX) / mapZoom,
        y: ((clientY - rect.top) / k - mapPanY) / mapZoom,
    };
}

// The logical point currently in the middle of the box, the anchor for button zooms.
function mapCenterPoint() {
    const vb = mapViewBox();
    return { x: (vb.w / 2 - mapPanX) / mapZoom, y: (vb.h / 2 - mapPanY) / mapZoom };
}

function clampMapZoom(z) {
    return Math.min(MAP_ZOOM_MAX, Math.max(MAP_ZOOM_MIN, z));
}

function nudgeMapZoom(factor, anchor) {
    setMapZoom(mapZoom * factor, anchor);
}

// Zoom to `z`, keeping `anchor` (a logical point) under the same pixel:
// v = pan + zoom·L is held constant ⇒ pan' = pan + (zoom − zoom')·L.
function setMapZoom(z, anchor) {
    const next = clampMapZoom(z);
    const a = anchor || mapCenterPoint();
    mapPanX += (mapZoom - next) * a.x;
    mapPanY += (mapZoom - next) * a.y;
    mapZoom = next;
    applyMapTransform();
}

function resetMapView() {
    mapZoom = 1;
    mapPanX = 0;
    mapPanY = 0;
    applyMapTransform();
}

// Never let the picture be dragged fully out of the box: MAP_PAN_MARGIN logical
// units of it stay reachable on every side, so there is always something to grab.
function clampMapPan() {
    const vb = mapViewBox();
    const w = MAP_W * mapZoom;
    const h = vb.h * mapZoom;
    const m = Math.min(MAP_PAN_MARGIN, w, h);
    mapPanX = Math.min(vb.w - m, Math.max(m - w, mapPanX));
    mapPanY = Math.min(vb.h - m, Math.max(m - h, mapPanY));
}

function applyMapTransform() {
    const vp = mapViewport();
    if (!vp) return;
    clampMapPan();
    vp.setAttribute('transform',
        `translate(${mapPanX.toFixed(2)} ${mapPanY.toFixed(2)}) scale(${mapZoom.toFixed(3)})`);
    // The viewpoint just moved, so the routing cables hang at a new angle. Every other
    // edge is flat on the board and rides the transform unchanged.
    redrawMapEdges();
    const level = document.getElementById('connMapZoomReset');
    if (level) level.textContent = Math.round(mapZoom * 100) + '%';
    const wrap = document.getElementById('connMap');
    if (wrap) {
        wrap.classList.toggle('zoomed',
            Math.abs(mapZoom - 1) > 0.001 || mapPanX !== 0 || mapPanY !== 0);
    }
}

// ---- tooltip ----------------------------------------------------------------

function showMapTip(st) {
    mapTipKey = st.data.key;
    fillMapTip(st.data);
    const tip = document.getElementById('connMapTip');
    if (tip) {
        tip.classList.add('visible');
        tip.setAttribute('aria-hidden', 'false');
    }
}

function hideMapTip() {
    mapTipKey = null;
    const tip = document.getElementById('connMapTip');
    if (tip) {
        tip.classList.remove('visible');
        tip.setAttribute('aria-hidden', 'true');
    }
}

function positionMapTip(cx, cy) {
    const tip = document.getElementById('connMapTip');
    if (!tip) return;
    const pad = 14;
    let left = cx + pad;
    let top = cy + pad;
    const w = tip.offsetWidth || 0;
    const h = tip.offsetHeight || 0;
    if (left + w > window.innerWidth - 8) left = Math.max(8, cx - w - pad);
    if (top + h > window.innerHeight - 8) top = Math.max(8, cy - h - pad);
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
}

function fillMapTip(n) {
    const tip = document.getElementById('connMapTip');
    if (!tip) return;
    tip.textContent = '';

    const title = document.createElement('div');
    title.className = 'tip-title';
    // The extension box gets no kind chip: its title already IS "extension", and the
    // chip would only say it twice.
    if (n.kind !== 'ext') {
        const kindChip = document.createElement('span');
        kindChip.className = 'tip-kind' + (n.remote ? ' remote' : '');
        kindChip.textContent = n.kind === 'server' ? 'server'
            : n.kind === 'client' ? 'MCP client'
            : n.kind === 'launcher' ? 'launcher'
            : (n.remote ? 'remote browser' : 'browser');
        title.appendChild(kindChip);
    }
    title.appendChild(document.createTextNode(n.title + (n.isSelf ? ' (this browser)' : '')));
    tip.appendChild(title);

    const row = n.row || {};
    const inst = n.inst || {};
    const pairs = [];
    if (n.kind === 'server') {
        pairs.push(['pid', row.pid]);
        pairs.push(['version', row.version]);
        pairs.push(['endpoints', row.detail]);
        pairs.push(['started', row.since_hms]);
        pairs.push(['last activity', row.activity_hms]);
    } else if (n.kind === 'client') {
        pairs.push(['pid', row.pid]);
        pairs.push(['cwd', row.cwd]);
        pairs.push(['sessions', n.sessionsShown === n.sessionsTotal
            ? String(n.sessionsTotal)
            : `${n.sessionsShown} with commands · ${n.sessionsTotal} total`]);
        if (row.provider || row.model) {
            pairs.push(['model', [row.provider, row.model].filter(Boolean).join(' / ')]);
        }
        const lastCmd = latestClientCommand(row);
        if (lastCmd) pairs.push(['last command', `${lastCmd.tool} @ ${lastCmd.time_hms}`]);
        pairs.push(['started', row.since_hms]);
        pairs.push(['last activity', row.activity_hms]);
    } else if (n.kind === 'launcher') {
        pairs.push(['pid', row.pid]);
        // Null when this launcher never reported one; i.e. a process older than
        // version reporting itself, which /health's version_warning also calls out.
        pairs.push(['version', row.version || 'unknown (too old to report one)']);
        pairs.push(['role', 'stdio ⇄ HTTP proxy (server/launcher.py)']);
        pairs.push(['client', n.clientTitle]);
        pairs.push(['started', row.since_hms]);
    } else if (n.kind === 'ext') {
        // The EXTENSION half: the socket, its version, what it last did.
        pairs.push(['version', inst.version]);
        pairs.push(['browser', n.browserTitle]);
        pairs.push(['socket', row.detail]);
        if (n.remote) pairs.push(['remote address', inst.remote_addr]);
        pairs.push(['connected at', inst.last_connect_hms
            + (inst.connect_count > 1 ? ` (connect ×${inst.connect_count})` : '')]);
        if (inst.last_action) {
            pairs.push(['last command', `${inst.last_action} @ ${inst.last_activity_hms || '—'}`]);
        }
    } else {
        // The BROWSER half: the process, and what it is holding open right now.
        pairs.push(['pid', inst.pid]);
        if (n.remote) pairs.push(['remote address', inst.remote_addr]);
        pairs.push(['windows', numOrNull(inst.windows)]);
        pairs.push(['tabs', tabsSummary(inst)]);
        pairs.push(['started', row.since_hms]);
    }

    pairs.forEach(([k, v]) => {
        if (v === null || v === undefined || v === '') return;
        const r = document.createElement('div');
        r.className = 'tip-row';
        const kEl = document.createElement('span');
        kEl.className = 'tip-k';
        kEl.textContent = k;
        const vEl = document.createElement('span');
        vEl.className = 'tip-v';
        vEl.textContent = String(v);
        r.appendChild(kEl);
        r.appendChild(vEl);
        tip.appendChild(r);
    });

    // The foreground tab of each window, listed under the tab counts: which tabs
    // this browser is actually sitting on, and when each last did anything.
    if (n.kind === 'browser') {
        (Array.isArray(inst.active_tab_list) ? inst.active_tab_list : []).forEach((t) => {
            const line = document.createElement('div');
            line.className = 'tip-tab';
            line.textContent = `[${t.id}] ${tipTabTitle(t.title)}`
                + (t.last_hms ? ` (last: ${t.last_hms})` : '');
            tip.appendChild(line);
        });
    }
}

// The first 15 characters of a tab title, "…" if there was more. A tab that has no
// title yet says so rather than rendering as an empty pair of brackets.
function tipTabTitle(title) {
    const s = String(title || '').trim();
    if (!s) return '(untitled)';
    return s.length > 15 ? s.slice(0, 15) + '…' : s;
}

// A count the browser pushed, or null so fillMapTip drops the row entirely; an
// extension too old to send browser_state must show nothing, never a made-up "0".
function numOrNull(v) {
    return (typeof v === 'number' && isFinite(v)) ? String(v) : null;
}

// "12 loaded / 42 total"; loaded = not discarded (Firefox and Chrome both unload idle
// tabs, so the tab count alone says nothing about what is actually in memory). The
// ACTIVE count is deliberately not here: it is the foreground tab of each window, so
// with one window it read "1 active" forever. Those tabs are listed individually
// underneath instead, which is the part that actually says something.
function tabsSummary(inst) {
    const loaded = numOrNull(inst.loaded_tabs);
    const total = numOrNull(inst.tabs);
    if (loaded && total) return `${loaded} loaded / ${total} total`;
    return total ? `${total} total` : (loaded ? `${loaded} loaded` : null);
}

// Most recent command across a client's sessions (each session's commands are
// already most-recent-first).
function latestClientCommand(row) {
    let best = null;
    (row.sessions || []).forEach((s) => {
        const c = s.commands && s.commands[0];
        if (c && (!best || String(c.time_hms) > String(best.time_hms))) best = c;
    });
    return best;
}

function truncEnd(s, n) {
    s = String(s === null || s === undefined ? '' : s);
    return s.length <= n ? s : s.slice(0, n - 1) + '…';
}

// Middle ellipsis; paths keep their telling start AND end ("C:\dr\…\browserplug").
function truncMiddle(s, n) {
    s = String(s === null || s === undefined ? '' : s);
    if (s.length <= n) return s;
    const tail = Math.floor((n - 1) / 2);
    return s.slice(0, n - 1 - tail) + '…' + s.slice(s.length - tail);
}