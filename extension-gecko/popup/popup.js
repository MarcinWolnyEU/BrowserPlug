/*
 * BrowserPlug Firefox Extension - Popup Script
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 */

// Portability shim (shared verbatim with extension-chromium): in Firefox
// `globalThis.browser` exists so this rebinds the same object; in Chromium it falls
// through to `chrome`. `??` short-circuits, so a jsdom mock of globalThis.browser
// never evaluates the bare `chrome` identifier.
const browser = globalThis.browser ?? chrome;
// MV3 Chromium exposes the toolbar-button API as `chrome.action`, not
// `chrome.browserAction`, so alias it onto the shim; otherwise the button reads in
// renderConnection() throw and the status line is stuck on neutral placeholders.
// No-op on Firefox (browserAction already exists) and under a jsdom mock that provides
// browserAction, so the shared-file byte-identity and popup.test.mjs both hold.
if (browser && !browser.browserAction && browser.action) browser.browserAction = browser.action;

// Check connection status on popup open
// True once the platform probe has answered "android". Read by checkFileAccess, which
// has a different thing to say there, and by the class that widens and scales the body.
let isMobile = false;
// Chrome, Edge and Brave define no `browser` global (Firefox does). There the proxy card is
// about a whole-browser switch, and the permission is granted with a button, not a toggle.
// A missing `browser` global is not proof: recent Chrome defines one itself. The manifest
// settles it: only the Chromium build declares a service worker, the Firefox one a page.
const isChromium = !globalThis.browser || (() => {
  try {
    return !!browser.runtime.getManifest().background.service_worker;
  } catch (e) {
    return false;
  }
})();

document.addEventListener('DOMContentLoaded', async () => {
  const serverInfo = document.getElementById('serverInfo');

  // Widen and scale BEFORE the panels render: on Android this document is a full-screen
  // overlay, not a 360px popup. Any failure leaves the desktop sizing, which is merely
  // narrow on a phone rather than broken.
  try {
    if (browser.runtime.getPlatformInfo) {
      const info = await browser.runtime.getPlatformInfo();
      isMobile = !!(info && info.os === 'android');
      if (isMobile) document.body.classList.add('mobile');
    }
  } catch (e) {
    console.error('getPlatformInfo failed; assuming desktop:', e);
  }

  // Show the extension's own version next to the title
  try {
    const ev = document.getElementById('extVersion');
    if (ev) ev.textContent = 'Version ' + browser.runtime.getManifest().version;
  } catch (e) {
    console.error('Could not read extension version:', e);
  }

  // Paint the status immediately from the live toolbar state, so the first frame
  // shows the real state instead of the static "disconnected" placeholder.
  renderConnection({});

  // The tab strip (popup-tabs.js) restores the last tab and reports every switch. The
  // Usage pane renders the moment it is shown and then rides the 2s timer below while
  // it stays showing; nothing is fetched for a pane nobody is looking at.
  // Whether the Usage tab exists at all is settled FIRST: restore onto it and take it
  // away a tick later and the popup opens with a visible flinch.
  await applyUsageTabVisibility();
  if (globalThis.PopupTabs) {
    try {
      await globalThis.PopupTabs.init({
        storage: browser.storage.local,
        onShow: (tab) => { if (tab === 'usage') updateUsagePane(); },
      });
    } catch (e) {
      console.error('Tab strip failed to initialise:', e);
    }
  }

  // Load configuration from storage and background script
  async function updateDisplay() {
    // Hoisted out of the try so the catch can re-render from what we ACTUALLY
    // heard from the background, instead of inventing a state.
    let status = null;
    try {
      // Connection status from the background script. The reply also carries the
      // background's LIVE config, the one really being dialed (see loadConfigToUI).
      status = await new Promise((resolve) => {
        browser.runtime.sendMessage({ action: 'getConnectionStatus' }, (response) => {
          if (browser.runtime.lastError) {
            resolve({ connected: false, retryAttempts: 0, config: {} });
          } else {
            resolve(response || { connected: false, retryAttempts: 0, config: {} });
          }
        });
      });

      // Storage is only the FALLBACK for the footer (see loadConfigToUI), and the
      // only place the test overrides are visible as such.
      const storageConfig = await browser.storage.sync.get({
        hostname: 'localhost',
        port: 8772,
        retryInterval: 5000,
        maxRetries: -1,
        pingTimeout: 5000,
        // Test configuration overrides
        testPort: null,
        testHostname: null
      });

      renderConnection(status);
      loadConfigToUI(storageConfig, status.config);

    } catch (error) {
      // NEVER invent state here. A failure anywhere in the try (a storage read, a
      // missing element, a future edit) says nothing about the CONNECTION, yet this
      // catch used to repaint the popup with a hardcoded phase:'disconnected'.
      // That is the whole bug class GATE 1.7 was about, and hardening one throw
      // site would not have removed it.
      // Re-render from what is genuinely known: the status we did get (if we got
      // one), letting renderConnection read the verdict and the words off the live
      // toolbar button, the single source of truth, which cannot lie. With no status
      // at all the phase stays UNKNOWN (renderConnection falls back to the button)
      // rather than being asserted as disconnected.
      console.error('Error updating display:', error);
      renderConnection(status || {});
    }
  }

  // Show the server actually being dialed in the info footer.
  //
  // `liveConfig` is the background's CONFIG (from the getConnectionStatus reply):
  // it is the URL truly being dialed; the background folds the test overrides into
  // it at load, and updateConfig() can change it WITHOUT persisting to storage, so
  // storage can disagree. Prefer it; fall back to storage (re-applying the override
  // precedence) only when the background didn't report one; the reply's fallback
  // shape is `config: {}`.
  function loadConfigToUI(storageConfig, liveConfig) {
    // Runs inside updateDisplay's try: a missing #serverInfo would throw there.
    // The catch no longer downgrades the connection display, but there is still
    // nothing to draw, so degrade to "no footer".
    if (!serverInfo) return;

    const cfg = storageConfig || {};
    const live = liveConfig || {};
    const effectiveHostname = live.hostname || cfg.testHostname || cfg.hostname || 'localhost';
    const effectivePort = live.port || cfg.testPort || cfg.port || 8772;
    const wsUrl = `ws://${effectiveHostname}:${effectivePort}`;

    serverInfo.textContent = `Server: ${wsUrl}`;

    // The test overrides only exist in storage (the background folds them into
    // hostname/port and does not report them separately). Name ONLY the ones that
    // are actually in the URL above: the old indicator printed `effectivePort`
    // unconditionally, so a hostname-only override advertised the port that was
    // NOT overridden; an override that storage holds but the background never
    // adopted is not "active" at all.
    const overridden = [];
    if (cfg.testHostname && String(cfg.testHostname) === String(effectiveHostname)) {
      overridden.push(String(effectiveHostname));
    }
    if (cfg.testPort && String(cfg.testPort) === String(effectivePort)) {
      overridden.push(String(effectivePort));
    }
    if (overridden.length) {
      const testIndicator = document.createElement('div');
      testIndicator.className = 'test-config';
      testIndicator.textContent = `⚙️ Test config active (${overridden.join(':')})`;
      serverInfo.appendChild(testIndicator);
    }
  }

  // Initial display update
  await updateDisplay();

  // Right-click menu for connection rows (built once, reused by every row).
  initConnCtxMenu();

  // Connections list (server + extension + MCP clients), refreshed live while open
  await updateConnections();
  const connectionsTimer = setInterval(updateConnections, 2000);

  // The Usage pane, on the same cadence. Each tick also decides whether the Usage tab
  // exists at all, then returns at once while the pane is hidden.
  const usageTimer = setInterval(updateUsagePane, 2000);

  // Live connection status line (countdown + retry attempts update as they happen)
  const statusTimer = setInterval(refreshStatus, 700);

  window.addEventListener('unload', () => {
    clearInterval(connectionsTimer);
    clearInterval(usageTimer);
    clearInterval(statusTimer);
  });

  // Open the full Options page (server + advanced settings live there)
  const openOptionsButton = document.getElementById('openOptions');
  if (openOptionsButton) {
    openOptionsButton.addEventListener('click', () => {
      browser.runtime.openOptionsPage();
    });
  }

  // Local file (file://) access: a card on the Permissions tab with a state pill in
  // its head, the explanation always in view, and a Re-check that re-reads the live
  // state.
  if (document.getElementById('fileAccessIndicator')) {
    const recheckBtn = document.getElementById('fileAccessRecheck');
    if (recheckBtn) recheckBtn.addEventListener('click', checkFileAccess);
    const openAddonsLink = document.getElementById('openAddons');
    if (openAddonsLink) openAddonsLink.addEventListener('click', openAddonsManager);
    const filesPageLink = document.getElementById('openExtensionPageFiles');
    if (filesPageLink) filesPageLink.addEventListener('click', openExtensionPage);
    checkFileAccess();
  }

  // SOCKS proxy routing (optional "proxy" permission): the same kind of card,
  // mirroring the file-access one.
  if (document.getElementById('proxyAccessIndicator')) {
    const recheckBtn = document.getElementById('proxyAccessRecheck');
    if (recheckBtn) recheckBtn.addEventListener('click', checkProxyAccess);
    const grantBtn = document.getElementById('proxyAccessGrant');
    if (grantBtn) grantBtn.addEventListener('click', requestProxyPermission);
    const openAddonsLink = document.getElementById('openAddonsProxy');
    if (openAddonsLink) openAddonsLink.addEventListener('click', openAddonsManager);
    checkProxyAccess();
  }

  // "Allow user scripts" (Chromium only): a card on the Permissions tab. On Firefox the
  // card stays hidden and checkUserScripts returns at once.
  if (document.getElementById('userScriptsIndicator')) {
    const recheckBtn = document.getElementById('userScriptsRecheck');
    if (recheckBtn) recheckBtn.addEventListener('click', checkUserScripts);
    const pageLink = document.getElementById('openExtensionPage');
    if (pageLink) pageLink.addEventListener('click', openExtensionPage);
    checkUserScripts();
  }

  // Both status blocks share the about:addons deep-link resolution.
  updateAddonsLinks();
});

// Tracks which connection rows the user expanded, so the 2s refresh doesn't collapse them
const expandedConnKeys = new Set();

// Latest account-wide Claude usage + the render options in effect, kept
// module-level so the right-click "Copy connection info" path can mirror them.
let lastClaudeUsage = null;
let lastUsageOpts = { showWeekly: false, peakLeadMinutes: 0 };
// The same, for the DeepSeek price clock (its lead is its own setting).
let lastDeepseekOpts = { peakLeadMinutes: 0 };
// The same, for the OpenAI bar.
let lastOpenAIUsage = null;
// The same, for GitHub Copilot, plus the Copilot rows the Usage tab reports on.
let lastCopilotUsage = null;
let lastCopilotRows = [];

// Which Claude Code rows have their usage chart expanded (survives the 2s
// refresh, like expandedConnKeys). Keyed the same way (pid: / label).
const expandedChartKeys = new Set();

// Claude usage bar/chart rendering lives in vendor/anthropic-view.js; popup.html
// loads it before this file. (The data layer, fetch/store, runs in background.)
const AnthropicView = globalThis.AnthropicView || {};
const isClaudeCode = AnthropicView.isClaudeCode || (() => false);

// The provider/model badge lives in vendor/kilocode-view.js; the data
// (c.provider / c.model) is detected server-side, for Kilo CLI and pi alike.
// popup.html loads it.
const KiloCode = globalThis.KiloCode || {};
const hasModelBadge = KiloCode.hasModelBadge || (() => false);

// The DeepSeek peak/off-peak price clock lives in vendor/deepseek-view.js and is
// pure local-clock math — no account, no fetch. It rides the DeepSeek Harness (DSH) row
// ONLY: the windows are DeepSeek's own, and the Anthropic peak bar above stays
// Claude Code's own for the same reason.
const DeepSeekView = globalThis.DeepSeekView || {};
const isDeepSeekHarness = DeepSeekView.isDeepSeekHarness || (() => false);

// OpenAI's row bar. Optional at load time (popup.html lists each view file separately,
// and a build mid-upgrade can be missing one), so every use is guarded.
const OpenAIView = globalThis.OpenAIView || {};
const isOpenAIClient = OpenAIView.isOpenAIClient || (() => false);

// GitHub Copilot's row bar, session badge and copy text (vendor/copilot-view.js), guarded
// the same way.
const CopilotView = globalThis.CopilotView || {};
const isCopilotClient = CopilotView.isCopilotClient || (() => false);

// Where each provider's indicator may appear, and the render options that go with it.
// 'disabled' shows nothing anywhere and suppresses the provider's fetch; 'tab' shows it
// on the Usage tab only; 'client' shows it on the Usage tab and as a bar on each
// connected client's row. Read through one helper by both the Usage pane and the list,
// so a stored value cannot come to mean two different things in one popup.
const DISPLAY_MODES = ['disabled', 'tab', 'client'];
const PROVIDER_MODE_DEFAULTS = {
  anthropicDisplayMode: 'client',
  deepseekDisplayMode: 'disabled',
  // OpenAI ships off: it needs a signed-in chatgpt.com profile, and a panel that can
  // only say "not signed in" is not a default worth having.
  openaiDisplayMode: 'disabled',
  copilotDisplayMode: 'client',
};

// The modes in effect at the last render, for the right-click "Copy connection info"
// path — it mirrors the panel, so it has to know what the panel decided to draw.
let lastProviderSettings = null;

async function readProviderSettings() {
  const s = await browser.storage.sync.get({
    ...PROVIDER_MODE_DEFAULTS,
    anthropicShowWeekly: false,
    anthropicPeakLeadMinutes: 0,
    // DeepSeek's own lead, kept separate: the two peaks mean different things
    // (capacity vs price), so a lead that suits one need not suit the other.
    deepseekPeakLeadMinutes: 0,
    openaiShowWeekly: false,
    copilotShowCompletions: false,
  });
  const leadOf = (v) => Math.min(240, Math.max(0, Number(v) || 0));
  const modeOf = (v, key) => (DISPLAY_MODES.includes(v) ? v : PROVIDER_MODE_DEFAULTS[key]);
  return {
    anthropic: {
      mode: modeOf(s.anthropicDisplayMode, 'anthropicDisplayMode'),
      opts: { showWeekly: !!s.anthropicShowWeekly, peakLeadMinutes: leadOf(s.anthropicPeakLeadMinutes) },
    },
    deepseek: {
      mode: modeOf(s.deepseekDisplayMode, 'deepseekDisplayMode'),
      opts: { peakLeadMinutes: leadOf(s.deepseekPeakLeadMinutes) },
    },
    openai: {
      mode: modeOf(s.openaiDisplayMode, 'openaiDisplayMode'),
      // No peak lead: OpenAI publishes no fixed-schedule pricing or capacity window,
      // so there is no second bar here to warn about one.
      opts: { showWeekly: !!s.openaiShowWeekly },
    },
    copilot: {
      mode: modeOf(s.copilotDisplayMode, 'copilotDisplayMode'),
      opts: { showCompletions: !!s.copilotShowCompletions },
    },
  };
}

// The Usage tab exists only while some provider is on. All three off and the pane would
// be an empty box behind a tab promising numbers, so the tab comes off the strip and
// PopupTabs moves the selection if that is where the user was standing.
// Called at startup, before the strip restores the remembered tab, and then at the top
// of every updateUsagePane tick, ahead of the early return for a hidden pane: a hidden
// pane is exactly the moment this has to be decided.
async function applyUsageTabVisibility(prov) {
  const tabs = globalThis.PopupTabs;
  if (!tabs || !tabs.setVisible) return;
  const p = prov || await readProviderSettings();
  tabs.setVisible('usage', ['anthropic', 'openai', 'copilot', 'deepseek']
    .some(k => p[k] && p[k].mode !== 'disabled'));
}

// Account-wide usage from the background, which caches it, so the 2s poll and the
// Connections list asking within the same tick cost one fetch between them at most.
function askBackground(action, extra) {
  return new Promise((resolve) => {
    browser.runtime.sendMessage({ action, ...extra }, (r) => {
      if (browser.runtime.lastError) resolve(null);
      else resolve((r && r.success) ? r.data : null);
    });
  });
}

// The Copilot reading, from the background's cache. While Copilot shows "other account"
// (on a row, or on the Usage tab in 'tab' mode), the user's next move is to switch
// accounts on github.com, and the cache would keep the old login for up to
// refreshMinutes after they do. So a standing mismatch re-reads github.com, at most
// every COPILOT_RECHECK_MS across both callers (the 2s row poll and the Usage tab). The
// mismatch is judged on this function's own last reading: lastCopilotUsage is the rows'
// copy and stays null in 'tab' mode.
const COPILOT_RECHECK_MS = 10000;
let lastCopilotRecheckAt = 0;
let lastCopilotReading = null;
async function readCopilotUsage() {
  const seen = lastCopilotReading;
  const stuck = !!(seen && CopilotView.accountMismatch
    && lastCopilotRows.some(r => CopilotView.accountMismatch(seen, r)));
  const force = stuck && Date.now() - lastCopilotRecheckAt >= COPILOT_RECHECK_MS;
  if (force) lastCopilotRecheckAt = Date.now();
  const reading = await askBackground('getCopilotUsage', force ? { force: true } : undefined);
  lastCopilotReading = reading;
  return reading;
}

// The Usage tab (popup/usage-pane.js draws it). Fetches only while the tab is showing,
// and only for providers not set to Disabled: that setting promises to stop the fetch,
// and the pane keeps the promise by saying so in the provider's card instead. The
// background caches both readings, so this costs one message each per tick.
async function updateUsagePane() {
  const prov = await readProviderSettings();
  // Settled first, ahead of every guard below: whether the Usage tab exists is a fact
  // about the SETTINGS, and it must still be decided while the pane is hidden or the
  // pane module failed to load.
  await applyUsageTabVisibility(prov);
  const host = document.getElementById('usagePane');
  const pane = document.getElementById('pane-usage');
  const usagePane = globalThis.UsagePane;
  if (!host || !usagePane) return;
  if (pane && pane.hidden) return;
  const state = {
    anthropic: { ...prov.anthropic, usage: null },
    openai: { ...prov.openai, usage: null },
    copilot: { ...prov.copilot, usage: null, rows: lastCopilotRows },
    deepseek: prov.deepseek,
  };
  // The three reads are independent; a slow provider must not hold up the other two.
  [state.anthropic.usage, state.openai.usage, state.copilot.usage] = await Promise.all([
    state.anthropic.mode !== 'disabled' ? askBackground('getClaudeUsage') : null,
    state.openai.mode !== 'disabled' ? askBackground('getOpenAIUsage') : null,
    state.copilot.mode !== 'disabled' ? readCopilotUsage() : null,
  ]);
  usagePane.render(host, state);
}

// ---- Row furniture: the kind class, its icon, the expand chevron ----------------
const SVGNS_POPUP = 'http://www.w3.org/2000/svg';

function rowKindClass(c) {
  if (!c) return 'client';
  if (c.kind === 'server') return 'server';
  if (c.kind === 'extension') return c.is_remote ? 'extension remote' : 'extension';
  return 'client';
}

// Line icons in the options page's stroke style. One shape per row kind: the server
// (two racks), a browser (a globe), a remote browser (a phone), a client (a prompt).
const ROW_ICON_PATHS = {
  server: ['M4 3h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z',
           'M4 13h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2z',
           'M6 7h.01', 'M6 17h.01'],
  extension: ['M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z', 'M2 12h20',
              'M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z'],
  remote: ['M7 2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z', 'M12 18h.01'],
  client: ['M4 17l6-6-6-6', 'M12 19h8'],
};

function strokeSvg(cls, paths, strokeWidth) {
  const svg = document.createElementNS(SVGNS_POPUP, 'svg');
  svg.setAttribute('class', cls);
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', String(strokeWidth));
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  for (const d of paths) {
    const p = document.createElementNS(SVGNS_POPUP, 'path');
    p.setAttribute('d', d);
    svg.appendChild(p);
  }
  return svg;
}

function rowIcon(c) {
  const kind = c && c.kind === 'extension' && c.is_remote ? 'remote'
    : (c && (c.kind === 'server' || c.kind === 'extension') ? c.kind : 'client');
  const svg = strokeSvg('conn-ic', ROW_ICON_PATHS[kind], 1.8);
  // A browser row is the only kind with a slot, and the slot is the one thing on the
  // row an agent is told to type (`browsers(target=N)`), so it goes on the glyph rather
  // than inside the name, where it used to be read back out of a server-built string.
  const slot = c ? c.slot : null;
  if (slot === null || slot === undefined) return svg;
  const wrap = document.createElement('span');
  wrap.className = 'conn-idwrap';
  wrap.title = `Browser slot ${slot}: an agent switches to it with `
    + `browsers(action="use", target=${slot})`;
  wrap.appendChild(svg);
  const num = document.createElement('span');
  num.className = 'conn-slot';
  num.textContent = String(slot);
  wrap.appendChild(num);
  return wrap;
}

function chevronIcon() {
  return strokeSvg('chev', ['M9 18l6-6-6-6'], 2);
}

// browser.browserAction.getBadgeBackgroundColor returns a [r,g,b,a] ColorArray.
// Nothing paints it any more -- the badge has no text, so the browser renders none
// of it -- but the background still writes it per phase, which makes it the one
// piece of connection state the popup can read straight out of the toolbar button
// instead of asking for it. Only the channels matter now; expose those.
function badgeColor(c) {
  if (Array.isArray(c) && c.length === 4) {
    const [r, g, b] = c;
    return { r, g, b };
  }
  return { r: 0, g: 0, b: 0 };
}

// Render the connection status line + a contextual Stop/Reconnect link.
//
// The toolbar button is the SINGLE source of truth: the background computes the
// state once (updateActionIndicator in background.js) and writes it to the button
// as a tooltip, an icon and an unrendered badge colour. Here we just mirror the
// two we can read back -- colour for the verdict, tooltip for the words -- so
// there is no duplicated phase→text logic, and the popup shows the correct state
// on the first paint (a direct read of the persistent action state, not a message
// round-trip that could momentarily read "Disconnected"). The badge chip that used
// to sit in front of the words went with the badge text itself; the green/red
// background of this line was already saying the same thing.
//
// `state` only contributes the PHASE (which picks the Stop/Reconnect link). An
// absent/unknown phase is fine; it falls back to the button. Callers must pass the
// status they really have (or nothing); never a fabricated one.
async function renderConnection(state) {
  state = state || {};
  const status = document.getElementById('status');
  const action = document.getElementById('connAction');
  if (!status || !action) return;

  let title = 'BrowserPlug', isConnected = false;
  try {
    const ba = browser.browserAction;
    const [bc, tt] = await Promise.all([
      ba.getBadgeBackgroundColor({}), ba.getTitle({})
    ]);
    title = tt || 'BrowserPlug';
    const col = badgeColor(bc);
    isConnected = col.g > col.r && col.g > col.b; // green == connected in our palette
  } catch (e) {
    // Action state unreadable; keep neutral placeholders (never a false "Disconnected").
  }

  // Descriptor = the tooltip text, minus the redundant app-name prefix.
  const desc = title.replace(/^BrowserPlug:\s*/, '');

  // Tone: the green badge is the healthy connection. Amber while the background is
  // still trying (connecting / waiting) or holds a socket to a server that is not
  // answering; red for everything else. The verdict is the badge colour; the phase
  // only decides amber over red.
  const phase = state.phase || (isConnected ? 'connected' : '');
  const trying = (phase === 'connecting' || phase === 'waiting');
  // The ONE place the line is allowed to differ from the button, and only for the healthy
  // connection: that tooltip answers "what is this button for?" for somebody hovering the
  // toolbar, and inside the popup the panel around it has already answered that. So the
  // LINE says Connected and the button keeps its sentence — untouched, because it is
  // background.js that writes it and nothing here writes back. Keyed on the green badge,
  // the same signal the tone below is, so the word and the colour cannot disagree. Every
  // other phase still passes through verbatim, which is what keeps the popup and the
  // toolbar from telling different stories about a FAILURE, the case that matters.
  // An encrypted connection is the normal case and keeps the plain word. Only a live
  // socket the background reports as NOT encrypted says so; an unknown state (no status
  // yet on the first paint) is not evidence either way.
  // A token that once worked was refused (4401). The background keeps it and waits for
  // this click (adoptServerToken) rather than trusting whoever answered on the port.
  const tokenRejected = !isConnected && state.tokenRejected === true;
  // Chromium only: a healthy connection whose "Allow user scripts" switch is off cannot
  // run page_eval. The line says so and links to the Permissions tab, amber like any
  // other connection that works only in part.
  const scriptsBlocked = isConnected && isChromium && !!globalThis.ChromiumAccess
    && !globalThis.ChromiumAccess.userScriptsEnabled(browser);
  const noEncryption = isConnected && state.encrypted === false;
  // A Re-authorize click that failed says why on the same line, until the next attempt.
  const adoptError = tokenRejected && typeof state.adoptError === 'string' && state.adoptError
    ? ` (re-authorize failed: ${state.adoptError})` : '';
  // Nothing to adopt: the 4401 named no endpoint, or one on a port this browser does not
  // trust. Reconnect only meets the same 4401. Once the user sets the MCP port or pastes a
  // token in Options, the next 4401 offers Re-authorize. The line leaves the port out: a
  // squatter on 8772 chooses it.
  const nothingToAdopt = tokenRejected && state.tokenAdoptable !== true
    ? '. Set the server\'s MCP port or paste its token in Options.' : '';
  const line = tokenRejected
    ? 'Token rejected: the server no longer accepts this browser\'s token' + adoptError + nothingToAdopt
    : isConnected
      ? 'Connected' + (scriptsBlocked ? ' but scripts are blocked' : '')
        + (noEncryption ? ', no encryption \u{1F513}' : '')
      : desc;
  const tone = isConnected ? (scriptsBlocked ? 'warn' : 'ok')
    : (!tokenRejected && (trying || phase === 'connected') ? 'warn' : 'bad');
  status.className = 'status ' + tone;
  const strip = document.getElementById('statusStrip');
  if (strip) strip.className = 'pp-status ' + tone;
  // The LINE keeps the full descriptor on hover, so the sentence it drops for the
  // healthy connection is one hover away rather than gone. Only then: every other
  // phase already prints the descriptor, and a tooltip repeating the text under the
  // cursor is noise. (This hover was the brand-row dot's job until 1.8.8, when the dot
  // went for saying a second time what the line and the strip around it already say.)
  status.title = isConnected ? desc : '';

  let msg = status.querySelector('.msg');
  if (!msg) {
    const dot = document.createElement('span');
    dot.className = 'dot';
    msg = document.createElement('span');
    msg.className = 'msg';
    status.replaceChildren(dot, msg);
  }
  // The 700 ms poll lands here every time; rebuilding an unchanged link would swallow a
  // click that straddles a tick, so the line is only touched when it changed.
  const lineKey = line + (scriptsBlocked ? '\u0000link' : '');
  if (msg.dataset.key !== lineKey) {
    msg.dataset.key = lineKey;
    if (scriptsBlocked) {
      const [head, tail] = line.split('scripts are blocked');
      const link = document.createElement('a');
      link.href = '#';
      link.dataset.goto = 'permissions';
      link.dataset.scrollTo = 'userScriptsCard';
      link.title = 'Show the Allow user scripts setting';
      link.textContent = 'scripts are blocked';
      msg.replaceChildren(head, link, tail);
    } else {
      msg.textContent = line;
    }
  }
  if (!status.dataset.linkBound) {
    status.dataset.linkBound = '1';
    status.addEventListener('click', (e) => {
      const a = e.target.closest && e.target.closest('a[data-goto]');
      if (!a) return;
      e.preventDefault();
      if (globalThis.PopupTabs) globalThis.PopupTabs.show(a.dataset.goto);
      // The pane is showing now, so the target has a position. The Permissions tab is
      // taller than the popup, and the card is the last one on it.
      const target = a.dataset.scrollTo && document.getElementById(a.dataset.scrollTo);
      if (target && target.scrollIntoView) target.scrollIntoView({ block: 'center' });
    });
  }

  // The Stop/Reconnect button is the only phase-dependent control. "Stop" is offered
  // only while actively trying (connecting/waiting); everything else offers
  // "Reconnect". Phase comes from the live status; default to Reconnect. The action
  // is re-read from the button at click time, so one listener serves every render.
  const reauthorize = tokenRejected && state.tokenAdoptable === true;
  action.textContent = reauthorize ? 'Re-authorize' : (trying ? 'Stop' : 'Reconnect');
  action.dataset.action = reauthorize ? 'adoptServerToken'
    : (trying ? 'stopReconnect' : 'forceReconnect');
  if (!action.dataset.bound) {
    action.dataset.bound = '1';
    action.addEventListener('click', (e) => {
      e.preventDefault();
      browser.runtime.sendMessage({ action: action.dataset.action }, () => setTimeout(refreshStatus, 200));
    });
  }
}

// Lightweight live poll: just the connection status line.
// A missing reply (lastError / no response) means the BACKGROUND didn't answer; it
// is not evidence of a disconnected SERVER. Render from the toolbar action
// alone (renderConnection reads it directly) rather than asserting a phase.
function refreshStatus() {
  browser.runtime.sendMessage({ action: 'getConnectionStatus' }, (response) => {
    if (browser.runtime.lastError) renderConnection({});
    else renderConnection(response || {});
  });
}

// What identifies a BROWSER, as opposed to the process running it: everything the
// extension said about itself in its hello. A remote browser has no pid, no exe path
// and no start time on the server's machine, so this is the whole answer to "which
// browser is that?". Somebody asks it when the wrong browser answered, or when a
// phone and a desktop are both on the list.
//
// One builder, two callers: the panel row's tooltip and "Copy connection info". Those
// two must never disagree about what a row says (.tmp/dev-docs/extension-popup-js.md).
function browserFacts(c) {
  if (!c || c.kind !== 'extension') return [];
  const out = [];
  if (c.remote_addr) out.push(`remote address ${c.remote_addr}`);
  const bi = c.browser_info || {};
  // browser_info arrives whole, because a Firefox fork's real identity is in a key of
  // its own (Zen: `zen: {version}`) rather than in `name`, which says "Firefox".
  const known = new Set(['name', 'vendor', 'version', 'buildID']);
  const browser = [
    [bi.name, bi.version].filter(Boolean).join(' '),
    bi.vendor,
    bi.buildID ? `build ${bi.buildID}` : null,
    ...Object.keys(bi).filter(k => !known.has(k) && bi[k] && typeof bi[k] === 'object')
      .map(k => `${k} ${bi[k].version || JSON.stringify(bi[k])}`),
  ].filter(Boolean);
  if (browser.length) out.push(`browser ${browser.join(' · ')}`);
  const p = c.platform || {};
  const platform = [p.os, p.arch].filter(Boolean).join(' ');
  if (platform) out.push(`platform ${platform}`);
  if (c.engine) out.push(`engine ${c.engine}`);
  if (c.version) out.push(`extension v${c.version}`);
  return out;
}

async function updateConnections() {
  const list = document.getElementById('connList');
  const countEl = document.getElementById('connCount');
  if (!list || !countEl) return;

  let resp;
  try {
    resp = await new Promise((resolve) => {
      browser.runtime.sendMessage({ action: 'getServerConnections' }, (r) => {
        if (browser.runtime.lastError) resolve({ success: false, error: 'extension not ready' });
        else resolve(r || { success: false, error: 'no response' });
      });
    });
  } catch (e) {
    resp = { success: false, error: e.message };
  }

  if (!resp || !resp.success || !resp.data) {
    lastCopilotRows = [];
    countEl.textContent = '-';
    const li = document.createElement('li');
    li.className = 'conn-empty';
    li.textContent = `(${(resp && resp.error) || 'server not reachable'})`;
    list.replaceChildren(li);
    return;
  }

  // Interface options:
  //  - hideEmptySessions: drop MCP sessions that never ran a command
  //    (handshake/tool-list-only) from the list and from the "N sessions" count.
  //  - toolCallSort: order of each session's recent tool-call list. The server
  //    sends commands most-recent-first, so 'top' renders as-is and 'bottom'
  //    (default) reverses to put the latest at the bottom.
  const { hideEmptySessions, toolCallSort, kiloClarifyNames } = await browser.storage.sync.get({
    hideEmptySessions: true,
    toolCallSort: 'bottom',
    // Kilo: resolve provider/model display names via models.dev.
    kiloClarifyNames: true
  });
  // The per-provider indicators (Options ▸ Advanced ▸ <provider> sessions). Only
  // 'client' puts anything on a row down here; 'tab' keeps it on the Usage tab and
  // 'disabled' shows it nowhere.
  const provider = await readProviderSettings();
  lastProviderSettings = provider;
  const usageOpts = provider.anthropic.opts;
  lastUsageOpts = usageOpts;
  const deepseekOpts = provider.deepseek.opts;
  lastDeepseekOpts = deepseekOpts;

  const conns = resp.data.connections || [];
  countEl.textContent = conns.length;
  lastCopilotRows = conns.filter(isCopilotClient);

  if (conns.length === 0) {
    const li = document.createElement('li');
    li.className = 'conn-empty';
    li.textContent = '(none)';
    list.replaceChildren(li);
    return;
  }

  // Account-wide Claude usage for the per-Claude-Code bars. Fetched only when the
  // panel is set to draw them AND a Claude Code client is present; the background caches
  // it for anthropicUsageRefreshMinutes (default 5), so this 2s poll almost always
  // answers from memory. Set to Disabled, this is the call that stops — which is what
  // the Options card promises. With the card's background query ticked, an alarm keeps
  // sampling whether or not this popup is open (vendor/usage-sampler.js). Awaited HERE,
  // before any DOM mutation, to preserve the "no blink" invariant (see the single
  // replaceChildren below).
  let claudeUsage = null;
  if (provider.anthropic.mode === 'client' && conns.some(isClaudeCode)) {
    claudeUsage = await askBackground('getClaudeUsage');
  }
  lastClaudeUsage = claudeUsage;

  // The same bargain for OpenAI: only while a Codex CLI row is on screen and the panel
  // is set to draw on it. The background caches, and the two-call token chain happens
  // there, so this stays one message either way.
  let openaiUsage = null;
  if (provider.openai.mode === 'client' && conns.some(isOpenAIClient)) {
    openaiUsage = await askBackground('getOpenAIUsage');
  }
  lastOpenAIUsage = openaiUsage;

  // And for GitHub Copilot: only while a Copilot Chat row is on screen and the panel is
  // set to draw on it.
  let copilotUsage = null;
  if (provider.copilot.mode === 'client' && lastCopilotRows.length) {
    copilotUsage = await readCopilotUsage();
  }
  lastCopilotUsage = copilotUsage;

  // Provider/model NAMES: gather the distinct (provider, model) pairs from the rows
  // that carry them and resolve them all in ONE round-trip (the background caches +
  // hits models.dev at most once). Attaches provider_name/model_name for the
  // badge. Awaited before the DOM build to keep the "no blink" invariant.
  if (kiloClarifyNames) {
    const kiloConns = conns.filter(hasModelBadge);
    if (kiloConns.length) {
      const seen = new Set(); const pairs = [];
      for (const c of kiloConns) {
        const k = `${c.provider || ''}\n${c.model || ''}`;
        if (!seen.has(k)) { seen.add(k); pairs.push({ provider: c.provider, model: c.model }); }
      }
      const names = await new Promise((resolve) => {
        browser.runtime.sendMessage({ action: 'resolveKiloNames', pairs }, (r) => {
          if (browser.runtime.lastError) resolve(null);
          else resolve((r && r.success) ? r.data : null);
        });
      });
      if (names) {
        for (const c of kiloConns) {
          const nm = names[`${c.provider || ''}\n${c.model || ''}`];
          if (nm) { c.provider_name = nm.providerName; c.model_name = nm.modelName; }
        }
      }
    }
  }

  // Build the whole list off-DOM, then swap it in with a single atomic
  // replaceChildren. The old code cleared the <ul> and then hit an `await`
  // (storage.sync.get) before repopulating, so the browser painted an empty
  // list every 2s refresh, the visible "blink". All awaits are now done
  // above; the DOM is mutated exactly once here, so there's no empty frame.
  const frag = document.createDocumentFragment();
  for (const c of conns) {
    const li = document.createElement('li');
    li.className = 'conn-row ' + rowKindClass(c);

    const isMcpClient = c.kind === 'mcp_client';
    let sessions = Array.isArray(c.sessions) ? c.sessions : [];
    if (hideEmptySessions && isMcpClient) {
      sessions = sessions.filter(s => (s.commands || []).length > 0);
    }
    const hasDetail = isMcpClient && sessions.length > 0;
    const key = c.pid != null ? `pid:${c.pid}` : (c.label || '');
    const label = c.label || 'Unknown';

    // Row head: an icon column, the name over its detail line, the activity time at
    // the end. The name is the first <span> in the row on purpose: it carries the
    // tooltip, and that is where a hover (and popup.test.mjs) looks for it.
    const header = document.createElement('div');
    header.className = 'conn-head';
    header.appendChild(rowIcon(c));
    const main = document.createElement('div');
    main.className = 'conn-main';
    const name = document.createElement('span');
    name.className = 'conn-name';
    name.textContent = label;
    name.title = `PID: ${c.pid != null ? c.pid : '?'} - ${c.exe_path || 'unknown path'}`
      // Who ships this client. The server names it (server.py :: _CLIENT_VENDORS) and
      // leaves it null for anything it could not identify, so an unrecognized row is
      // unchanged rather than labelled "unknown vendor".
      + (c.vendor ? `\nvendor: ${c.vendor}` : '')
      + (c.cmdline ? `\ncmdline: ${c.cmdline}` : '')
      + (c.cwd ? `\ncwd: ${c.cwd}` : '')
      + browserFacts(c).map(f => `\n${f}`).join('');
    main.appendChild(name);

    const right = document.createElement('div');
    right.className = 'conn-right';
    const activity = document.createElement('span');
    activity.className = 'activity';
    activity.title = 'Last activity';
    activity.textContent = c.activity_hms || '-';
    right.appendChild(activity);
    // The process, under the time it was last busy. Every kind of row shows it in this
    // one place; a row without one (a REMOTE browser has no local process) shows none.
    if (c.pid !== null && c.pid !== undefined) {
      const pid = document.createElement('span');
      pid.className = 'conn-pid';
      pid.title = 'Process ID';
      pid.textContent = `pid ${c.pid}`;
      right.appendChild(pid);
    }

    header.appendChild(main);
    header.appendChild(right);
    li.appendChild(header);

    if (c.detail || c.since_hms) {
      const det = document.createElement('div');
      det.className = 'conn-detail';
      // With the filter on, rewrite the "N sessions" count to the visible total.
      let text = c.detail || '';
      if (c.detail && hideEmptySessions && isMcpClient) {
        const n = sessions.length;
        text = c.detail.replace(/\d+ sessions?/, `${n} session${n === 1 ? '' : 's'}`);
      }
      // Claude Code rows get the account-wide usage bars; Kilo CLI rows get a
      // provider/model badge, both inserted inline between the "pid …" and
      // "N sessions" segments. Falls back to plain text when there's nothing.
      let inlineEl = null;
      if (isClaudeCode(c) && provider.anthropic.mode === 'client') {
        inlineEl = AnthropicView.buildUsageBars(claudeUsage, usageOpts);
        const hasUsageData = !!(claudeUsage && claudeUsage.session);
        if (inlineEl && hasUsageData) {
          // Clicking the bar toggles an expanded usage chart for this row.
          // Only wired when there IS usage data; a peak-only bar (signed-out
          // profile) has nothing to graph.
          inlineEl.style.cursor = 'pointer';
          inlineEl.addEventListener('click', (e) => {
            e.stopPropagation();
            if (expandedChartKeys.has(key)) expandedChartKeys.delete(key);
            else expandedChartKeys.add(key);
            updateConnections();
          });
        }
        // Usage fetch failing (profile not signed in to claude.ai, network):
        // a muted marker instead of silently dropping the bars. Sits first
        // in the inline slot, before any PEAK bar; matching
        // AnthropicView.usageSummaryText's copy-path order.
        if (claudeUsage && claudeUsage.ok === false) {
          const na = document.createElement('span');
          na.className = 'conn-na';
          na.title = 'Account usage unavailable: ' + (claudeUsage.error || 'fetch failed') +
            '\nSign in to claude.ai in this browser profile to restore the usage bars.' +
            '\nDetails: Options ▸ Advanced ▸ Anthropic sessions.';
          na.textContent = 'usage n/a';
          if (inlineEl) inlineEl.insertBefore(na, inlineEl.firstChild);
          else inlineEl = na;
        }
      } else if (isDeepSeekHarness(c) && provider.deepseek.mode === 'client' && DeepSeekView.buildPeakBars) {
        // null outside the peak window (and its lead), exactly like the Claude bar:
        // the row then renders as plain text, with no gap where a bar would sit.
        inlineEl = DeepSeekView.buildPeakBars(deepseekOpts);
      } else if (isOpenAIClient(c) && provider.openai.mode === 'client' && OpenAIView.buildUsageBars) {
        inlineEl = OpenAIView.buildUsageBars(openaiUsage, provider.openai.opts);
        // Signed out of chatgpt.com, or a failed refresh: a muted marker rather than a
        // row that looks like the feature was never built. Same treatment, and the same
        // wording, as the Claude Code row above.
        if (openaiUsage && openaiUsage.ok === false) {
          const na = document.createElement('span');
          na.className = 'conn-na';
          na.title = (openaiUsage.signedOut
            ? 'No chatgpt.com session in this browser profile.'
            : 'Account usage unavailable: ' + (openaiUsage.error || 'fetch failed'))
            + '\nSign in to chatgpt.com in this browser profile to restore the usage bar.'
            + '\nDetails: Options ▸ Advanced ▸ OpenAI sessions.';
          na.textContent = openaiUsage.signedOut ? 'signed out' : 'usage n/a';
          if (inlineEl) inlineEl.insertBefore(na, inlineEl.firstChild);
          else inlineEl = na;
        }
      } else if (isCopilotClient(c) && CopilotView.buildRowInline) {
        // The usage bar when the display mode puts it on the row, and the session badge
        // whenever the server sent session details; copilot-view.js decides which apply,
        // including the "other account" marker when the two GitHub logins differ.
        inlineEl = CopilotView.buildRowInline(c, copilotUsage, provider.copilot.opts,
          provider.copilot.mode === 'client');
      } else if (hasModelBadge(c)) {
        inlineEl = KiloCode.buildProviderBadge(c);
      }
      if (inlineEl) {
        // The bar leads the line. It used to sit between "pid 41812" and "2 sessions";
        // the pid now has its own place under the activity time, and what is left of
        // the detail reads as the bar's tail.
        det.appendChild(inlineEl);
        if (text) det.appendChild(document.createTextNode(' · ' + text));
      } else {
        det.textContent = text;
      }
      // Process creation time, the last item in the bottom row, with its own
      // tooltip. Same-day is HH:MM:SS; an earlier day is YYYY-MM-DD HH:MM.
      if (c.since_hms) {
        const sinceEl = document.createElement('span');
        sinceEl.className = 'since';
        sinceEl.title = 'Time when OS created a process';
        sinceEl.textContent = `${(text || inlineEl) ? ' · ' : ''}since ${c.since_hms}`;
        det.appendChild(sinceEl);
      }
      main.appendChild(det);
    }

    // Expanded usage chart (full width of the connections block), toggled by
    // clicking the usage bar; re-rendered each refresh so it stays live.
    // (claudeUsage.session guard: a {ok:false} fetch failure is truthy but has
    // nothing to graph; don't render an empty chart from a stale toggle.)
    if (isClaudeCode(c) && provider.anthropic.mode === 'client'
        && claudeUsage && claudeUsage.session && expandedChartKeys.has(key) && AnthropicView.buildUsageChart) {
      li.appendChild(AnthropicView.buildUsageChart(claudeUsage, usageOpts));
    }

    if (hasDetail) {
      // A client with sessions expands in place: the whole head is the target, the
      // chevron at its end turns, and aria-expanded on the row says which way.
      li.classList.add('clickable');
      const panel = document.createElement('div');
      panel.className = 'sessions';

      for (const s of sessions) {
        const sh = document.createElement('div');
        sh.className = 'sess-h';
        sh.textContent = `session ${s.id} - last ${s.last_activity_hms}`;
        panel.appendChild(sh);

        let cmds = s.commands || [];
        // Server order is most-recent-first; flip to latest-at-bottom by default.
        if (toolCallSort !== 'top') cmds = cmds.slice().reverse();
        if (cmds.length === 0) {
          const none = document.createElement('div');
          none.className = 'sess-none';
          none.textContent = '(no commands yet)';
          panel.appendChild(none);
        } else {
          for (const cmd of cmds) {
            const row = document.createElement('div');
            row.className = 'cmd';
            const t = document.createElement('span');
            t.className = 't';
            t.textContent = (cmd.time_hms || '') + '  ';
            const tool = document.createElement('span');
            tool.className = 'tool';
            tool.textContent = cmd.tool || '?';
            row.appendChild(t);
            row.appendChild(tool);
            const params = cmd.params || {};
            const pkeys = Object.keys(params);
            if (pkeys.length) {
              const pr = document.createElement('span');
              pr.className = 'p';
              pr.textContent = '  ' + pkeys.map(k => `${k}: ${params[k]}`).join(', ');
              row.appendChild(pr);
            }
            panel.appendChild(row);
          }
        }
      }
      li.appendChild(panel);
      right.appendChild(chevronIcon());

      const apply = () => {
        const open = expandedConnKeys.has(key);
        panel.hidden = !open;
        li.setAttribute('aria-expanded', String(open));
      };
      apply();
      header.addEventListener('click', () => {
        if (expandedConnKeys.has(key)) expandedConnKeys.delete(key);
        else expandedConnKeys.add(key);
        apply();
      });
    }

    // Right-click anywhere on the row → Copy connection info / Copy PID.
    // Capture the connection + the interface options in effect for THIS render
    // so the copy reflects exactly what the panel is showing.
    li.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      // A right-click also produced a pointerdown, so drop the hold it armed.
      cancelConnHold();
      openConnCtxMenu(e, c, { hideEmptySessions, toolCallSort });
    });
    // …and press-and-hold, the only route on a phone. Same connection, same options.
    armConnHold(li, () => ({ c, opts: { hideEmptySessions, toolCallSort } }));

    frag.appendChild(li);
  }
  list.replaceChildren(frag);
}

// --- Local file (file://) reading ------------------------------------------
// No Firefox lets an extension OPEN file:// itself (always "Illegal URL"); the
// user opens the file and the extension reads that tab. This reports whether
// reading is available:
//   - Firefox < 153: granted automatically by the required <all_urls> host perm.
//   - Firefox 153+:  gated by the per-extension opt-in, read via
//                    extension.isAllowedFileSchemeAccess() (accurate there).
// (permissions.contains({origins:['file:///*']}) is deliberately NOT used - it
// just echoes <all_urls> and stays true even when file reading is off.)
async function checkFileAccess() {
  const dot = document.getElementById('fileAccessIndicator');
  const text = document.getElementById('fileAccessText');
  if (!dot || !text) return;

  // No Firefox for Android lets an extension read local files, and no toggle exists to
  // change that, so the desktop "Disabled" reads as a setting the user forgot to flip.
  // State the platform limit and stop; the enable steps below would be a dead end.
  if (isMobile) {
    setPermissionState(dot, 'unknown', 'Unavailable');
    text.textContent = 'Unavailable on mobile';
    const help = document.getElementById('fileAccessEnableHelp');
    if (help) help.hidden = true;
    // The desktop paragraph tells you to open the file yourself with Ctrl+O, which is
    // not a thing on a phone, and it never says why the row reads Unavailable. Swap it
    // for the reason.
    const how = document.getElementById('fileAccessDesktopHow');
    if (how) how.hidden = true;
    const why = document.getElementById('fileAccessMobileWhy');
    if (why) why.hidden = false;
    return;
  }

  let browserMajor = null;
  try {
    if (browser.runtime.getBrowserInfo) {
      const info = await browser.runtime.getBrowserInfo();
      browserMajor = parseInt(info.version, 10);
    }
  } catch (e) { /* version unknown */ }

  let canRead = null;
  if (browserMajor !== null && browserMajor < 153) {
    canRead = true;
  } else {
    try {
      if (browser.extension && browser.extension.isAllowedFileSchemeAccess) {
        canRead = await browser.extension.isAllowedFileSchemeAccess();
      }
    } catch (e) { console.error('isAllowedFileSchemeAccess failed:', e); }
  }

  if (canRead === true) {
    setPermissionState(dot, 'granted', 'Enabled');
    text.textContent = 'BrowserPlug can read local files you open.';
  } else if (canRead === false) {
    setPermissionState(dot, 'denied', 'Disabled');
    text.textContent = "BrowserPlug can't read local files.";
  } else {
    setPermissionState(dot, 'unknown', 'Unknown');
    text.textContent = 'File-reading access unknown.';
  }

  // Only show the "how to enable" guidance when reading isn't already on. On
  // FF152 it's always on (via <all_urls>), so there's nothing to enable and no
  // about:addons toggle exists, so showing those steps there would be a dead end.
  // Chromium has no about:addons: its switch is "Allow access to file URLs" on the
  // extension's own card in chrome://extensions.
  const needsHelp = (canRead !== true);
  const enableHelp = document.getElementById('fileAccessEnableHelp');
  if (enableHelp) enableHelp.hidden = !needsHelp || isChromium;
  const chromiumHelp = document.getElementById('fileAccessChromiumHelp');
  if (chromiumHelp) chromiumHelp.hidden = !needsHelp || !isChromium;
  const pageLink = document.getElementById('openExtensionPageFiles');
  if (pageLink && isChromium && globalThis.ChromiumAccess) pageLink.textContent = extensionPageUrl();
}

// --- SOCKS proxy routing (optional "proxy" permission) ----------------------
// Mirrors the file-access block: reports whether the extension holds the
// optional "proxy" permission (togglable in about:addons ▸ Permissions), which
// is what enables per-tab SOCKS routing (e.g. opening .onion via a Tor proxy).
async function checkProxyAccess() {
  const dot = document.getElementById('proxyAccessIndicator');
  const text = document.getElementById('proxyAccessText');
  if (!dot || !text) return;

  let granted = null;
  try {
    if (isChromium) {
      // Chrome has no optional `proxy` permission to grant; the consent is a switch the
      // extension keeps itself (background.js PROXY_CONSENT_KEY), written only from here.
      granted = (await browser.storage.local.get('allowBrowserProxy')).allowBrowserProxy === true;
    } else if (browser.permissions && browser.permissions.contains) {
      granted = await browser.permissions.contains({ permissions: ['proxy'] });
    }
  } catch (e) { console.error('reading the proxy permission failed:', e); }

  if (granted === true) {
    setPermissionState(dot, 'granted', 'Enabled');
    text.textContent = isChromium
      ? 'BrowserPlug can switch this whole browser to a SOCKS proxy.'
      : 'BrowserPlug can route tabs through a SOCKS proxy.';
  } else if (granted === false) {
    setPermissionState(dot, 'denied', 'Disabled');
    text.textContent = isChromium
      ? "BrowserPlug can't switch the browser to a proxy."
      : "BrowserPlug can't route tabs through a proxy.";
  } else {
    setPermissionState(dot, 'unknown', 'Unknown');
    text.textContent = 'Proxy-routing access unknown.';
  }

  // Only show the "how to enable" guidance when the permission isn't already on, and
  // show the route that EXISTS on this platform: desktop flips a toggle in about:addons,
  // a phone has no such page and must ask Firefox for the permission directly.
  const needsHelp = (granted !== true);
  const enableHelp = document.getElementById('proxyAccessEnableHelp');
  if (enableHelp) enableHelp.hidden = !needsHelp || isMobile || isChromium;
  const mobileHelp = document.getElementById('proxyAccessMobileHelp');
  if (mobileHelp) mobileHelp.hidden = !needsHelp || !isMobile;
  const chromiumHelp = document.getElementById('proxyAccessChromiumHelp');
  if (chromiumHelp) chromiumHelp.hidden = !needsHelp || !isChromium;
  const chromiumScope = document.getElementById('proxyAccessChromiumScope');
  if (chromiumScope) chromiumScope.hidden = !isChromium;
  const grant = document.getElementById('proxyAccessGrant');
  if (grant) {
    // On Chromium the button is a switch and stays visible; elsewhere it is a one-time grant.
    grant.hidden = isChromium ? false : (!needsHelp || !isMobile);
    if (isChromium) {
      grant.textContent = granted === true ? 'Turn off the browser-wide proxy'
                                           : 'Allow the browser-wide proxy';
    }
  }
}

// permissions.request() is the only way to grant an optional permission on Firefox for
// Android (no about:addons there); it needs a user gesture, so it hangs off this click
// and nothing may be awaited before it. Supported on Firefox for Android since 120, and
// the manifest already floors Android at 142.
async function requestProxyPermission() {
  if (isChromium) {
    try {
      const on = (await browser.storage.local.get('allowBrowserProxy')).allowBrowserProxy === true;
      await browser.storage.local.set({ allowBrowserProxy: !on });
    } catch (e) {
      console.error('writing the proxy consent failed:', e);
    }
    checkProxyAccess();
    return;
  }
  try {
    await browser.permissions.request({ permissions: ['proxy'] });
  } catch (e) {
    console.error('permissions.request(proxy) failed:', e);
  }
  // Re-read either way: the user may have refused, and the row must show what IS.
  checkProxyAccess();
}

// --- "Allow user scripts" (Chromium only) ------------------------------------
// The per-extension switch on the browser's extensions page. It gates page_eval and cannot
// be flipped by the extension, so the card reports it and sends the user to the page that
// has it. The same probe drives the "scripts are blocked" link in the status line.
function extensionPageUrl() {
  return globalThis.ChromiumAccess.extensionsPageUrl(browser.runtime.id, {
    userAgent: navigator.userAgent,
    brave: !!navigator.brave,
  });
}

async function checkUserScripts() {
  const card = document.getElementById('userScriptsCard');
  const dot = document.getElementById('userScriptsIndicator');
  const text = document.getElementById('userScriptsText');
  if (!card || !dot || !text || !isChromium || !globalThis.ChromiumAccess) return;
  card.hidden = false;

  const on = globalThis.ChromiumAccess.userScriptsEnabled(browser);
  if (on) {
    setPermissionState(dot, 'granted', 'Enabled');
    text.textContent = 'BrowserPlug can run JavaScript in pages (page_eval).';
  } else {
    setPermissionState(dot, 'denied', 'Disabled');
    text.textContent = "BrowserPlug can't run JavaScript in pages; page_eval is blocked.";
  }

  const help = document.getElementById('userScriptsEnableHelp');
  if (help) help.hidden = on;
  // The URL is the link text, so it can still be copied if the browser refuses to open
  // it from here.
  const link = document.getElementById('openExtensionPage');
  if (link) link.textContent = extensionPageUrl();
}

async function openExtensionPage(evt) {
  if (evt) evt.preventDefault();
  try {
    await browser.tabs.create({ url: extensionPageUrl() });
  } catch (e) {
    console.error('Could not open the extensions page:', e);
  }
}

// The state pill in a permission card's head: a class for the colour, a word for
// the reader.
function setPermissionState(pill, tone, word) {
  if (!pill) return;
  pill.className = 'state ' + tone;
  pill.textContent = word;
}

// The "about:addons" text is a working link ONLY when there's an about:addons
// tab to switch to AND we're not already on it (extensions can't OPEN
// about:addons - it's a privileged page). Otherwise it renders as plain text.
// Applies to every about:addons link in the popup (file-access + proxy blocks).
async function updateAddonsLinks() {
  let canSwitch = false;
  try {
    const allTabs = await browser.tabs.query({});
    const hasAddonsTab = allTabs.some(t => t.url && t.url.startsWith('about:addons'));
    const [activeTab] = await browser.tabs.query({ active: true, currentWindow: true });
    const activeIsAddons = !!(activeTab && activeTab.url && activeTab.url.startsWith('about:addons'));
    canSwitch = hasAddonsTab && !activeIsAddons;
  } catch (e) {
    console.error('Could not check about:addons tabs:', e);
  }
  for (const id of ['openAddons', 'openAddonsProxy']) {
    const link = document.getElementById(id);
    if (!link) continue;
    link.classList.toggle('as-text', !canSwitch);
    if (canSwitch) link.setAttribute('href', '#');
    else link.removeAttribute('href');
  }
}

// Click handler for the live about:addons link: switch to the existing tab.
async function openAddonsManager(evt) {
  if (evt) evt.preventDefault();
  const link = (evt && evt.currentTarget) || document.getElementById('openAddons');
  if (link && link.classList.contains('as-text')) return; // inert: plain text, nothing to do
  try {
    const tabs = await browser.tabs.query({});
    const existing = tabs.find(t => t.url && t.url.startsWith('about:addons'));
    if (existing) {
      await browser.tabs.update(existing.id, { active: true });
      if (existing.windowId != null && browser.windows && browser.windows.update) {
        try { await browser.windows.update(existing.windowId, { focused: true }); } catch (e) {}
      }
    }
  } catch (e) {
    console.error('Could not focus about:addons tab:', e);
  }
}

// --- Right-click menu for connection rows -----------------------------------
// A single reusable custom menu (the browser.menus/contextMenus API can't target
// arbitrary popup DOM). Right-clicking a connection row opens it with two items:
//   • Copy connection info, the row's block as RICH TEXT (HTML) so it pastes
//     formatted into Word / MS Teams. Sessions are rendered expanded regardless
//     of the row's collapsed state, honoring the "Hide command-less sessions"
//     setting, and WITHOUT the ▸/▾ show/hide disclosure.
//   • Copy PID, just the process id, as plain text.

const CTX_LABELS = { info: 'Copy connection info', pid: 'Copy PID' };

let connCtxMenu = null;      // the <div class="ctx-menu"> element
let ctxItemInfo = null;      // "Copy connection info" item
let ctxItemPid = null;       // "Copy PID" item
let ctxMenuConn = null;      // connection object the menu was opened for
let ctxMenuOpts = null;      // { hideEmptySessions, toolCallSort } captured at open
let ctxFlashTimer = null;    // pending "Copied ✓" → close timer
let ctxBackdrop = null;      // dimmer behind the menu, modal presentation only
let holdTimer = null;        // pending press-and-hold
let holdRow = null;          // the <li> currently held, for its pressed styling

// Press and hold opens the same menu as a right-click. It is the ONLY route on a phone,
// which has no contextmenu event to hang it off, and it is a second route on desktop.
//
// Three seconds, at the user's request: long enough that a hold cannot be confused with
// a tap on a list that also scrolls. Movement cancels it for the same reason — a drag is
// a scroll, not a hold — and so does letting go early.
const CONN_HOLD_MS = 3000;
const CONN_HOLD_SLOP_PX = 10;

function cancelConnHold() {
  if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
  if (holdRow) { holdRow.classList.remove('holding'); holdRow = null; }
}

// `li` is the row; `get()` returns the {c, opts} captured for THIS render, so a hold
// that outlives a 2s refresh still copies what the row was showing.
function armConnHold(li, get) {
  li.addEventListener('pointerdown', (e) => {
    // Primary button only: a right-click has its own path and would otherwise arm a
    // hold that fires three seconds after the menu it already opened.
    if (e.button !== 0) return;
    cancelConnHold();
    const startX = e.clientX, startY = e.clientY;
    holdRow = li;
    li.classList.add('holding');
    const move = (ev) => {
      if (Math.abs(ev.clientX - startX) > CONN_HOLD_SLOP_PX
          || Math.abs(ev.clientY - startY) > CONN_HOLD_SLOP_PX) cancelConnHold();
    };
    li.addEventListener('pointermove', move);
    const done = () => {
      li.removeEventListener('pointermove', move);
      cancelConnHold();
    };
    li.addEventListener('pointerup', done, { once: true });
    li.addEventListener('pointercancel', done, { once: true });
    holdTimer = setTimeout(() => {
      holdTimer = null;
      const { c, opts } = get();
      cancelConnHold();
      openConnCtxMenu(null, c, opts, { modal: true });
    }, CONN_HOLD_MS);
  });
}

function initConnCtxMenu() {
  if (connCtxMenu) return;
  const menu = document.createElement('div');
  menu.className = 'ctx-menu';
  menu.style.display = 'none';

  ctxItemInfo = document.createElement('div');
  ctxItemInfo.className = 'ctx-menu-item';
  ctxItemInfo.textContent = CTX_LABELS.info;
  ctxItemInfo.addEventListener('click', async () => {
    const c = ctxMenuConn, opts = ctxMenuOpts;
    if (!c) { hideConnCtxMenu(); return; }
    const ok = await copyConnectionInfo(c, opts);
    flashCtxItem(ctxItemInfo, ok ? 'Copied ✓' : 'Copy failed', CTX_LABELS.info);
  });

  ctxItemPid = document.createElement('div');
  ctxItemPid.className = 'ctx-menu-item';
  ctxItemPid.textContent = CTX_LABELS.pid;
  ctxItemPid.addEventListener('click', async () => {
    if (ctxItemPid.classList.contains('disabled')) return;
    const c = ctxMenuConn;
    if (!c || c.pid == null) { hideConnCtxMenu(); return; }
    const ok = await copyPlainText(String(c.pid));
    flashCtxItem(ctxItemPid, ok ? `Copied PID ${c.pid} ✓` : 'Copy failed', CTX_LABELS.pid);
  });

  menu.appendChild(ctxItemInfo);
  menu.appendChild(ctxItemPid);
  document.body.appendChild(menu);
  connCtxMenu = menu;

  // Only the modal presentation shows this. It needs NO click handler of its own: the
  // document-level dismissal below already closes on any click outside the menu, and the
  // backdrop is outside it. (Verified by drilling — adding one here changes nothing.)
  // It is here to dim the popup and to swallow taps on the rows underneath.
  ctxBackdrop = document.createElement('div');
  ctxBackdrop.className = 'ctx-backdrop';
  ctxBackdrop.style.display = 'none';
  document.body.appendChild(ctxBackdrop);

  // Dismiss: left-click / right-click outside the menu, Escape, scroll, blur.
  document.addEventListener('click', (e) => {
    if (isCtxMenuOpen() && !menu.contains(e.target)) hideConnCtxMenu();
  });
  document.addEventListener('contextmenu', (e) => {
    // Row handlers stopPropagation, so this only fires for right-clicks elsewhere.
    if (isCtxMenuOpen() && !menu.contains(e.target)) hideConnCtxMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') hideConnCtxMenu();
  });
  window.addEventListener('scroll', hideConnCtxMenu, true);
  window.addEventListener('blur', hideConnCtxMenu);
}

function isCtxMenuOpen() {
  return !!(connCtxMenu && connCtxMenu.style.display !== 'none');
}

function openConnCtxMenu(e, c, opts, { modal = false } = {}) {
  if (!connCtxMenu) return;
  if (ctxFlashTimer) { clearTimeout(ctxFlashTimer); ctxFlashTimer = null; }
  ctxItemInfo.textContent = CTX_LABELS.info;
  ctxItemPid.textContent = CTX_LABELS.pid;

  ctxMenuConn = c;
  ctxMenuOpts = opts;

  // "Copy PID" is only meaningful when a PID is known (it usually is, but the
  // extension row can lack one when port→PID resolution failed).
  ctxItemPid.classList.toggle('disabled', !(c && c.pid != null));

  connCtxMenu.classList.toggle('as-modal', modal);
  if (ctxBackdrop) ctxBackdrop.style.display = modal ? 'block' : 'none';
  if (modal) {
    // Centred by CSS; no anchor to clamp against, and clearing the inline coordinates
    // stops a previous right-click's position fighting the transform.
    connCtxMenu.style.display = 'block';
    connCtxMenu.style.left = '';
    connCtxMenu.style.top = '';
    return;
  }

  // Show first (so it has a measured size), then clamp within the viewport.
  connCtxMenu.style.display = 'block';
  const pad = 4;
  const mw = connCtxMenu.offsetWidth;
  const mh = connCtxMenu.offsetHeight;
  let x = e.clientX;
  let y = e.clientY;
  if (x + mw + pad > window.innerWidth) x = Math.max(pad, window.innerWidth - mw - pad);
  if (y + mh + pad > window.innerHeight) y = Math.max(pad, window.innerHeight - mh - pad);
  connCtxMenu.style.left = x + 'px';
  connCtxMenu.style.top = y + 'px';
}

function hideConnCtxMenu() {
  if (connCtxMenu) {
    connCtxMenu.style.display = 'none';
    connCtxMenu.classList.remove('as-modal');
  }
  if (ctxBackdrop) ctxBackdrop.style.display = 'none';
  cancelConnHold();
  ctxMenuConn = null;
  ctxMenuOpts = null;
}

// Show a transient confirmation on the clicked item, then close the menu.
function flashCtxItem(item, message, restore) {
  if (ctxFlashTimer) clearTimeout(ctxFlashTimer);
  item.textContent = message;
  ctxFlashTimer = setTimeout(() => {
    ctxFlashTimer = null;
    item.textContent = restore;
    hideConnCtxMenu();
  }, 700);
}

// Build the connection's block as a styled DOM subtree for rich-text copy.
// Mirrors the popup rendering, but: sessions are always expanded, the ▸/▾
// disclosure is dropped, and every style is inline so the formatting survives
// a paste into Word / MS Teams (class-based CSS does not travel via the clipboard).
function buildConnectionCopyNode(c, opts) {
  const { hideEmptySessions, toolCallSort } = opts || {};
  const isMcpClient = c.kind === 'mcp_client';
  let sessions = Array.isArray(c.sessions) ? c.sessions : [];
  if (hideEmptySessions && isMcpClient) {
    sessions = sessions.filter(s => (s.commands || []).length > 0);
  }
  const label = c.label || 'Unknown';

  const root = document.createElement('div');
  root.style.cssText = 'font-family: Arial, sans-serif; font-size: 12px; color: #000;';

  const header = document.createElement('div');
  const name = document.createElement('span');
  // The panel paints the slot on the row's icon and the pid under the activity time.
  // A paste has neither column, so both are spelled out here, the same way the
  // activity time is: `[6] Opera GX pid 45216 (activity: 13:22:04)`.
  name.textContent = (c.slot !== null && c.slot !== undefined ? `[${c.slot}] ` : '')
    + label
    + (c.pid !== null && c.pid !== undefined ? ` pid ${c.pid}` : '');
  if (c.kind === 'server') name.style.fontWeight = 'bold';
  header.appendChild(name);

  const small = document.createElement('small');
  small.style.color = 'gray';
  small.textContent = ` (activity: ${c.activity_hms || '-'})`;
  header.appendChild(small);
  root.appendChild(header);

  if (c.detail || c.since_hms) {
    const det = document.createElement('div');
    det.style.cssText = 'font-size: 10px; color: #888;';
    let text = c.detail || '';
    if (c.detail && hideEmptySessions && isMcpClient) {
      const n = sessions.length;
      text = c.detail.replace(/\d+ sessions?/, `${n} session${n === 1 ? '' : 's'}`);
    }
    // Mirror the panel's inline usage bars as text (bars don't survive a paste),
    // inserted after the "pid …" segment for Claude Code rows.
    // Only what the panel itself drew: a provider kept to the Usage tab (or switched
    // off) has nothing on this row, and a copied row that disagrees with the screen is
    // worse than one that says less.
    const modeOf = (k) => (lastProviderSettings ? lastProviderSettings[k].mode : 'client');
    let inlineTxt = '';
    if (isClaudeCode(c) && modeOf('anthropic') === 'client') inlineTxt = AnthropicView.usageSummaryText(lastClaudeUsage, lastUsageOpts);
    else if (isDeepSeekHarness(c) && modeOf('deepseek') === 'client' && DeepSeekView.peakSummaryText) inlineTxt = DeepSeekView.peakSummaryText(lastDeepseekOpts);
    else if (isOpenAIClient(c) && modeOf('openai') === 'client' && OpenAIView.usageSummaryText) inlineTxt = OpenAIView.usageSummaryText(lastOpenAIUsage, lastProviderSettings && lastProviderSettings.openai.opts);
    else if (isCopilotClient(c) && CopilotView.rowSummaryText) {
      inlineTxt = CopilotView.rowSummaryText(c, lastCopilotUsage,
        lastProviderSettings && lastProviderSettings.copilot.opts, modeOf('copilot') === 'client');
    }
    else if (hasModelBadge(c)) inlineTxt = KiloCode.summaryText(c);
    // Bar first, matching the panel (where it leads the line now that the pid it used
    // to follow has moved to the row's right-hand column).
    if (inlineTxt) text = [inlineTxt, text].filter(Boolean).join(' · ');
    det.textContent = text;
    // Process creation time, last item in the bottom row (matches the panel).
    if (c.since_hms) {
      const sinceEl = document.createElement('span');
      sinceEl.textContent = `${text ? ' · ' : ''}since ${c.since_hms}`;
      det.appendChild(sinceEl);
    }
    root.appendChild(det);
  }

  // What the process IS (shown only in the panel's hover tooltip) surfaced into
  // the copied text ONCE here, right below the detail line; these describe the
  // connection, not any single session, so they're emitted at the root rather
  // than repeated inside the per-session loop.
  //
  // `cmdline` is the one that answers "what is this?" for a client running under
  // a generic interpreter: a row reading "Node" has an exe_path of plain node.exe,
  // and only the argv names the tool (`…/@deepseek-ai/dsh/lib/bin.js`). The server
  // masks secret-looking values before sending it. `launcher` is here because an
  // MCP client reaches this server two ways — through a stdio launcher or straight
  // over HTTP — and which one it is changes where a connection problem lives.
  const facts = [
    c.vendor ? `vendor ${c.vendor}` : null,
    c.exe_path ? `exe_path ${c.exe_path}` : null,
    c.cmdline ? `cmdline ${c.cmdline}` : null,
    c.cwd ? `cwd ${c.cwd}` : null,
    ...browserFacts(c),
    c.launcher_pid
      ? `launcher pid ${c.launcher_pid}`
        + (c.launcher_version ? ` · v${c.launcher_version}` : ' · version not reported')
      : (c.kind === 'mcp_client' ? 'launcher none (direct HTTP client)' : null),
  ];
  for (const fact of facts) {
    if (!fact) continue;
    const row = document.createElement('div');
    row.style.cssText = 'font-size: 11px; color: #777; margin-top: 1px;';
    row.textContent = fact;
    root.appendChild(row);
  }

  if (isMcpClient && sessions.length > 0) {
    const panel = document.createElement('div');
    panel.style.cssText = 'margin: 3px 0 2px 12px; padding-left: 6px; border-left: 2px solid #ddd;';
    for (const s of sessions) {
      const sh = document.createElement('div');
      sh.style.cssText = 'font-size: 11px; color: #555; margin-top: 3px;';
      sh.textContent = `session ${s.id} - last ${s.last_activity_hms}`;
      panel.appendChild(sh);

      let cmds = s.commands || [];
      if (toolCallSort !== 'top') cmds = cmds.slice().reverse();
      if (cmds.length === 0) {
        const none = document.createElement('div');
        none.style.cssText = 'font-size: 11px; color: #aaa; margin-left: 10px;';
        none.textContent = '(no commands yet)';
        panel.appendChild(none);
      } else {
        for (const cmd of cmds) {
          const row = document.createElement('div');
          row.style.cssText = 'font-size: 11px; margin-left: 10px;';
          const t = document.createElement('span');
          t.style.color = '#999';
          t.textContent = (cmd.time_hms || '') + '  ';
          const tool = document.createElement('span');
          tool.style.fontFamily = 'monospace';
          tool.textContent = cmd.tool || '?';
          row.appendChild(t);
          row.appendChild(tool);
          const params = cmd.params || {};
          const pkeys = Object.keys(params);
          if (pkeys.length) {
            const pr = document.createElement('span');
            pr.style.color = '#06c';
            pr.textContent = '  ' + pkeys.map(k => `${k}: ${params[k]}`).join(', ');
            row.appendChild(pr);
          }
          panel.appendChild(row);
        }
      }
    }
    root.appendChild(panel);
  }

  return root;
}

// Copy a connection's block as rich text (text/html + a text/plain fallback).
async function copyConnectionInfo(c, opts) {
  const node = buildConnectionCopyNode(c, opts);
  // Attach off-screen so innerText reflects layout and the selection fallback
  // has a real range to copy.
  node.style.position = 'fixed';
  node.style.left = '-99999px';
  node.style.top = '0';
  document.body.appendChild(node);
  try {
    const html = node.outerHTML;
    const plain = node.innerText || node.textContent || '';
    try {
      if (navigator.clipboard && window.ClipboardItem && navigator.clipboard.write) {
        await navigator.clipboard.write([new ClipboardItem({
          'text/html': new Blob([html], { type: 'text/html' }),
          'text/plain': new Blob([plain], { type: 'text/plain' })
        })]);
        return true;
      }
    } catch (err) {
      console.error('clipboard.write failed, falling back to execCommand:', err);
    }
    return copyNodeViaSelection(node);
  } finally {
    node.remove();
  }
}

// Fallback rich copy: select the node's rendered contents and let the browser
// serialize them to text/html itself (works within the click's user gesture).
function copyNodeViaSelection(node) {
  try {
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(node);
    sel.removeAllRanges();
    sel.addRange(range);
    const ok = document.execCommand('copy');
    sel.removeAllRanges();
    return ok;
  } catch (err) {
    console.error('selection copy failed:', err);
    return false;
  }
}

// Copy plain text (used for Copy PID), with a hidden-textarea fallback.
async function copyPlainText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) {
    console.error('writeText failed, falling back to execCommand:', err);
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-99999px';
    ta.style.top = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch (err) {
    console.error('textarea copy failed:', err);
    return false;
  }
}

