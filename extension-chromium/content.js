/*
 * BrowserPlug Chromium Extension - Content Script (ISOLATED world)
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 */

// Portability shim (mirrors popup.js/options.js): Chromium content scripts only expose
// `chrome`; `globalThis.browser` is undefined here and falls through to `chrome`. `??`
// short-circuits so a jsdom mock of globalThis.browser never evaluates the bare `chrome`.
const browser = globalThis.browser ?? chrome;

// ISOLATED-world content script, run at document_start on every page (top frame only, per
// the manifest). It is the private per-frame extension context; same DOM as the page, but
// its own JS realm. It does NOT wrap the page console or the page's fetch/XHR itself: those
// live in the page's MAIN world, handled by content-main.js. This script is the bridge:
//   * console capture: records the __browserplugConsole events content-main.js dispatches,
//     batched into the same TAB_CONSOLE sink (500/load cap, 20-entry or 300 ms flush), and
//     answers the SW's flushConsole before it reads that sink; PLUS the CSP-immune
//     uncaught-error / unhandled-rejection listeners that run here.
//   * D5a body-capture relay: pulls the armed monitor state at startup and forwards SW
//     arm/disarm broadcasts into the MAIN world, and relays captured __browserplugNetBody
//     bodies from the MAIN world up to the SW.
//   * legacy text/html messaging: extractText/extractHTML/getPageTitle/getPageURL.

// ---- Console capture (relay + error listeners) ------------------------------
// The MAIN-world hook (content-main.js) is what actually wraps console.*; here we only
// record the __browserplugConsole events it dispatches and batch them, plus own the error /
// unhandledrejection listeners (CSP-immune, no page-world code needed). The batch timer is
// not what a digest waits for: before the worker reads its console buffer it sends
// 'flushConsole', answered by deliverEmitted below.
const consoleCapture = (function setupConsoleCapture() {
  if (window !== window.top) return null; // top frame only; bounds noise
  // Names this document. Each entry and each flush answer carries it, so the worker can
  // keep a navigation's digest to the document the navigation landed on.
  const doc = Math.random().toString(36).slice(2) + Date.now().toString(36);
  let pending = [];
  let flushTimer = null;
  let captured = 0;
  let numbered = 0;
  const MAX_PER_LOAD = 500;
  // Batches sent whose runtime.sendMessage promise has not settled yet. Until it settles
  // the worker may not hold the batch, so a flush answer carries it again.
  const inFlight = new Set();

  function flush() {
    flushTimer = null;
    if (pending.length === 0) return;
    const batch = pending;
    pending = [];
    let sent;
    try {
      sent = browser.runtime.sendMessage({ type: 'console_entries', entries: batch });
    } catch (e) { return; /* background gone; drop */ }
    inFlight.add(batch);
    Promise.resolve(sent).then(() => {}, () => {}).then(() => { inFlight.delete(batch); });
  }

  // doc and n identify an entry, so the worker keeps one copy of an entry that reached it
  // both in a batch and in a flush answer.
  function push(level, text) {
    pending.push({ level: level, text: text, ts: Date.now(), url: location.href, doc: doc, n: ++numbered });
  }

  function record(level, text) {
    if (captured >= MAX_PER_LOAD) return;
    captured++;
    let safe;
    try { safe = String(text).slice(0, 2000); } catch (e) { safe = '[unserializable]'; }
    push(level, safe);
    if (captured === MAX_PER_LOAD) {
      push('warn', '[browserplug] console capture cap reached (' + MAX_PER_LOAD + ' entries this page load); further entries dropped');
    }
    if (pending.length >= 20) flush();
    else if (!flushTimer) flushTimer = setTimeout(flush, 300);
  }

  // content-main.js hands each console call and page error over in a '__browserplugConsole'
  // event whose detail is "level:text". dispatchEvent runs this listener before it returns,
  // in either world, so the entry is recorded before the page's call returns. This file is
  // declared before content-main.js in the manifest, so the listener is on first.
  window.addEventListener('__browserplugConsole', function (ev) {
    const handed = ev.detail;
    const cut = typeof handed === 'string' ? handed.indexOf(':') : -1;
    if (cut > 0) record(handed.slice(0, cut), handed.slice(cut + 1));
  });

  // Answers 'flushConsole' in the task that receives it. Every capture path records
  // synchronously, so everything the page emitted before the request is pending here, in
  // a batch still in flight, or already in the worker's buffer, and the answer carries the
  // first two. A busy page delays the answer only by the tasks queued ahead of the
  // request, and no timer is on the path (a background tab's timers wake about once a second).
  function deliverEmitted() {
    const entries = [];
    inFlight.forEach((batch) => { for (const e of batch) entries.push(e); });
    for (const e of pending) entries.push(e);
    pending = [];
    return { ok: true, doc: doc, entries: entries };
  }

  // (2) Uncaught-error / unhandled-rejection listeners. NOTE: on real Chromium these do
  // NOT fire for the PAGE's own uncaught throws; an ISOLATED-world content script is a
  // separate JS realm and only sees its OWN errors, not the page's. The page-error capture
  // that actually fires on Chrome lives in the MAIN world (content-main.js section a2),
  // handed over in the same __browserplugConsole event. These are kept as a fallback (and are
  // the jsdom-tested path in content-relay.test.mjs); on Chrome they harmlessly no-op for
  // page errors, so there is no double capture.
  window.addEventListener('error', (e) => {
    try {
      if (e && e.message !== undefined) {
        const loc = e.filename ? ' (' + e.filename + ':' + e.lineno + ')' : '';
        record('error', 'Uncaught: ' + e.message + loc);
      }
    } catch (err) { /* ignore */ }
  }, true);

  // Unhandled promise rejections also dispatch on the shared window.
  window.addEventListener('unhandledrejection', (e) => {
    try {
      let reason = '';
      try { reason = (e.reason && e.reason.message) ? String(e.reason.message) : String(e.reason); }
      catch (er) { reason = '[unserializable]'; }
      record('error', 'Unhandled rejection: ' + reason);
    } catch (err) { /* ignore */ }
  });

  return { deliverEmitted: deliverEmitted };
})();


// ---- D5a: response-body monitor relay (SW <-> MAIN-world wrapper) ------------
// content.js is the ISOLATED-world middleman between the SW (chrome.* messaging) and the
// MAIN-world wrapper in content-main.js (window.postMessage only).
function browserplugForwardMonitorState(state) {
  try {
    window.postMessage({
      __browserplugMonitorState: true,
      armed: !!(state && state.armed),
      urlPatterns: (state && Array.isArray(state.urlPatterns)) ? state.urlPatterns : []
    }, '*');
  } catch (e) { /* ignore */ }
}

// Pull the current armed state once at document_start so a page loaded mid-session arms
// its MAIN-world wrapper without waiting for the next SW arm broadcast.
try {
  browser.runtime.sendMessage({ type: 'get_monitor_state' })
    .then((state) => { if (state) browserplugForwardMonitorState(state); })
    .catch(() => {});
} catch (e) { /* background gone */ }

// Relay captured bodies from the MAIN-world wrapper up to the SW. The SW correlates each
// entry to its webRequest log record by (tabId via sender, url, method, nearest ts).
window.addEventListener('message', (ev) => {
  if (ev.source !== window || !ev.data || ev.data.__browserplugNetBody !== true) return;
  try {
    browser.runtime.sendMessage({
      type: 'net_body',
      entry: {
        url: ev.data.url,
        method: ev.data.method,
        status: ev.data.status,
        contentType: ev.data.contentType,
        body: ev.data.body,
        truncated: ev.data.truncated,
        ts: ev.data.ts
      }
    }).catch(() => {});
  } catch (e) { /* background gone; drop */ }
});


// Listen for messages from background script
browser.runtime.onMessage.addListener((request, sender, sendResponse) => {
  const { action } = request;

  try {
    switch (action) {
      case 'browserplug_monitor_state':
        // SW arm/disarm broadcast (D5a); forward into the MAIN-world wrapper.
        browserplugForwardMonitorState({ armed: request.armed, urlPatterns: request.urlPatterns });
        sendResponse({ ok: true });
        break;

      case 'extractText':
        sendResponse({
          text: document.body ? document.body.innerText : '',
          title: document.title,
          url: window.location.href
        });
        break;

      case 'extractHTML':
        sendResponse({
          html: document.documentElement.outerHTML,
          title: document.title,
          url: window.location.href
        });
        break;

      case 'getPageTitle':
        sendResponse({ title: document.title });
        break;

      case 'getPageURL':
        sendResponse({ url: window.location.href });
        break;

      case 'flushConsole':
        // The worker reads its console buffer only after this answers
        // (flushConsoleCapture in background.js). A subframe has no capture of its own.
        sendResponse(consoleCapture ? consoleCapture.deliverEmitted() : { ok: true, capture: false });
        break;

      // NB: page script evaluation deliberately does NOT live here. page_eval runs its
      // code via the SW (chrome.userScripts.execute / scripting.executeScript); a
      // message-driven eval() in a content script is both redundant and a standing
      // web-store-review rejection.

      default:
        sendResponse({ error: `Unknown content action: ${action}` });
    }
  } catch (error) {
    sendResponse({ error: error.message });
  }

  return true; // Keep message channel open for async response
});
