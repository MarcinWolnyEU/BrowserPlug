/*
 * BrowserPlug Firefox Extension - Content Script
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 */

// Content script for page interaction.
// Runs on every page at document_start, keep it silent; a load banner here is
// noise in EVERY page's console, including the user's own debugging.

// ---- Console capture --------------------------------------------------------
// Runs at document_start so errors thrown during load/hydration are captured
// BEFORE any agent call arrives. Two independent paths:
//   (1) console.log/warn/error/... hooked in the PAGE's MAIN world via an
//       injected <script>, where `arguments` is native (no Xray). It serializes
//       to a string IN the page and hands it over in a synchronous DOM event, so
//       the call is recorded before the page's console call returns. This avoids the
//       "Permission denied to access property length" Xray error that wrapping
//       the page console from the isolated world via exportFunction hits (the
//       page's Function.prototype.apply reading .length off a content-script
//       arguments object). Caveat: the inline injection is subject to page CSP
//       (a strict script-src blocks it), but path (2) still works regardless.
//   (2) uncaught errors + unhandled rejections: plain content-script listeners
//       on the shared window (CSP-immune; no exportFunction).
// Entries are batched to the background script, which keeps a per-tab buffer. The batch
// timer is not what a digest waits for: before the background reads that buffer it sends
// 'flushConsole', answered by deliverEmitted below.
const consoleCapture = (function setupConsoleCapture() {
  if (window !== window.top) return null; // top frame only; bounds noise
  // Names this document. Each entry and each flush answer carries it, so the background
  // can keep a navigation's digest to the document the navigation landed on.
  const doc = Math.random().toString(36).slice(2) + Date.now().toString(36);
  let pending = [];
  let flushTimer = null;
  let captured = 0;
  let numbered = 0;
  const MAX_PER_LOAD = 500;
  // Batches sent whose runtime.sendMessage promise has not settled yet. Until it settles
  // the background may not hold the batch, so a flush answer carries it again.
  const inFlight = new Set();

  function flush() {
    flushTimer = null;
    if (pending.length === 0) return;
    const batch = pending;
    pending = [];
    let sent;
    try {
      sent = browser.runtime.sendMessage({ type: 'console_entries', entries: batch });
    } catch (e) { return; /* background gone, drop */ }
    inFlight.add(batch);
    Promise.resolve(sent).then(() => {}, () => {}).then(() => { inFlight.delete(batch); });
  }

  // doc and n identify an entry, so the background keeps one copy of an entry that reached
  // it both in a batch and in a flush answer.
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

  // (1) MAIN-world console hook, injected as an inline <script> (self-contained;
  // serializes args in the page and dispatches '__browserplugConsole' with
  // "level:text" as its detail). dispatchEvent runs this listener before it returns,
  // so the listener goes on before the hook is injected.
  window.addEventListener('__browserplugConsole', function (ev) {
    const handed = ev.detail;
    const cut = typeof handed === 'string' ? handed.indexOf(':') : -1;
    if (cut > 0) record(handed.slice(0, cut), handed.slice(cut + 1));
  });
  function mainWorldConsoleHook() {
    if (window.__browserplugConsoleHooked) return;
    window.__browserplugConsoleHooked = true;
    // Taken now, so a page script that replaces either later cannot cut the hook off.
    var dispatch = EventTarget.prototype.dispatchEvent;
    var Handover = CustomEvent;
    // A page listener for this event that logs would otherwise re-enter the hook until
    // the stack runs out.
    var handing = false;
    var levels = ['log', 'info', 'warn', 'error', 'debug'];
    levels.forEach(function (level) {
      var orig = console[level];
      if (typeof orig !== 'function') return;
      console[level] = function () {
        if (!handing) {
          handing = true;
          try {
            var parts = [];
            for (var i = 0; i < arguments.length; i++) {
              var a = arguments[i];
              if (typeof a === 'string') { parts.push(a); continue; }
              if (a instanceof Error) { parts.push(a.message); continue; }
              try { parts.push(JSON.stringify(a)); }
              catch (e) { try { parts.push(String(a)); } catch (e2) { parts.push('[unserializable]'); } }
            }
            dispatch.call(window, new Handover('__browserplugConsole', { detail: level + ':' + parts.join(' ') }));
          } catch (e) { /* never break the page's console call */ }
          handing = false;
        }
        return orig.apply(this, arguments);
      };
    });
  }
  try {
    const s = document.createElement('script');
    s.textContent = '(' + mainWorldConsoleHook.toString() + ')();';
    (document.head || document.documentElement || document).appendChild(s);
    s.remove();
  } catch (e) { /* CSP blocked inline injection; errors below still captured */ }

  // Answers 'flushConsole' in the task that receives it. Both capture paths record
  // synchronously, so everything the page emitted before the request is pending here, in
  // a batch still in flight, or already in the background's buffer, and the answer carries
  // the first two. A busy page delays the answer only by the tasks queued ahead of the
  // request, and no timer is on the path (a background tab clamps timers to about a second).
  function deliverEmitted() {
    const entries = [];
    inFlight.forEach((batch) => { for (const e of batch) entries.push(e); });
    for (const e of pending) entries.push(e);
    pending = [];
    return { ok: true, doc: doc, entries: entries };
  }

  // (2) Uncaught page errors fire 'error' events on the shared window.
  window.addEventListener('error', (e) => {
    try {
      if (e && e.message !== undefined) {
        const loc = e.filename ? ' (' + e.filename + ':' + e.lineno + ')' : '';
        record('error', 'Uncaught: ' + e.message + loc);
      }
    } catch (err) { /* ignore */ }
  }, true);

  // Unhandled promise rejections also dispatch on the shared window; a plain
  // content-script listener receives them (e.reason is Xray-wrapped, so guard it).
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


// Listen for messages from background script
browser.runtime.onMessage.addListener((request, sender, sendResponse) => {
  const { action } = request;

  try {
    switch (action) {
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
        // The background reads its console buffer only after this answers
        // (flushConsoleCapture in background.js). A subframe has no capture of its own.
        sendResponse(consoleCapture ? consoleCapture.deliverEmitted() : { ok: true, capture: false });
        break;

      // NB: page script evaluation deliberately does NOT live here. page_eval
      // injects its code via tabs.executeScript (see AUTOMATION_SRC in
      // background.js); a message-driven eval() in a content script is both
      // redundant and a standing AMO-review rejection.

      default:
        sendResponse({ error: `Unknown content action: ${action}` });
    }
  } catch (error) {
    sendResponse({ error: error.message });
  }

  return true; // Keep message channel open for async response
});