/*
 * BrowserPlug Chromium Extension - MAIN-world Content Script
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 */

// Runs in the PAGE's own MAIN world at document_start (the MV3 manifest declares this
// file with "world":"MAIN", top frame only). MAIN world has NO chrome.*/browser.*; it
// is the page realm, so everything here talks to the ISOLATED content.js through the DOM:
// a synchronous '__browserplugConsole' event for console calls and page errors, and
// window.postMessage for the body relay. This is the Chromium replacement for gecko's
// inline-<script> injection (content.js used to build a <script> element and append it to
// reach the page world; on Chromium a declared world:"MAIN" content script IS the page
// world, and is CSP-immune; a parity WIN over the gecko inline path a strict script-src
// could block).
//
// Two responsibilities:
//   (a) console hook: wrap console.log/info/warn/error/debug, serialize args IN the
//       page, and hand "level:text" to content.js in a '__browserplugConsole' event.
//   (b) fetch/XHR response-body wrapper (D5a): when a BrowserPlug monitor session is armed
//       (content.js forwards __browserplugMonitorState in), clone matching fetch/XHR response
//       bodies (1 MB cap) and postMessage {__browserplugNetBody,...} out to content.js, which
//       relays them to the service worker. All capture is try/catch-guarded and the page's
//       own fetch/XHR result is ALWAYS returned untouched; capture never breaks the page.

// ---- (a) MAIN-world console hook -------------------------------------------
// Ported verbatim from gecko content.js `mainWorldConsoleHook` (same serialization,
// same '__browserplugConsole' event, same __browserplugConsoleHooked guard). On gecko this
// function's SOURCE was injected as an inline <script>; here it simply runs, because
// this file already executes in the page's main world.
(function browserplugMainWorldConsoleHook() {
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
})();

// ---- (a2) MAIN-world uncaught-error / unhandled-rejection capture ----------
// These MUST live in the MAIN world on Chromium. Unlike Firefox, a Chromium ISOLATED-world
// content script does NOT receive 'error'/'unhandledrejection' events for exceptions thrown
// by the PAGE's own scripts (the isolated world is a separate JS realm; only the main world
// sees the page's uncaught throws). content.js keeps its ISOLATED listeners as a
// jsdom-tested fallback, but on real Chrome the capture that actually fires for a page throw
// is THIS one, handed over in the same '__browserplugConsole' event content.js batches, so
// the SW's TAB_CONSOLE sink and the level='error' text format match gecko exactly.
// (Empirically found: test_agent_experience.test_console_captures_load_errors_and_click_log
// saw console.error captured but the inline-script `throw` missing until this was added.)
(function browserplugMainWorldErrorCapture() {
  if (window.__browserplugErrorHooked) return;
  window.__browserplugErrorHooked = true;
  var dispatch = EventTarget.prototype.dispatchEvent;
  var Handover = CustomEvent;

  function postError(text) {
    try {
      dispatch.call(window, new Handover('__browserplugConsole',
        { detail: 'error:' + String(text).slice(0, 2000) }));
    } catch (e) { /* never break the page */ }
  }

  // Uncaught page errors fire an ErrorEvent on the page's window.
  window.addEventListener('error', function (e) {
    try {
      if (e && e.message !== undefined) {
        var loc = e.filename ? ' (' + e.filename + ':' + e.lineno + ')' : '';
        postError('Uncaught: ' + e.message + loc);
      }
    } catch (err) { /* ignore */ }
  }, true);

  // Unhandled promise rejections dispatch on the page's window too.
  window.addEventListener('unhandledrejection', function (e) {
    try {
      var reason = '';
      try { reason = (e && e.reason && e.reason.message) ? String(e.reason.message) : String(e && e.reason); }
      catch (er) { reason = '[unserializable]'; }
      postError('Unhandled rejection: ' + reason);
    } catch (err) { /* ignore */ }
  });
})();

// ---- (b) fetch/XHR response-body wrapper (D5a monitor sessions) ------------
(function browserplugNetBodyWrapper() {
  if (window.__browserplugNetWrapped) return;
  window.__browserplugNetWrapped = true;

  // 1 MB cap per body; mirrors the gecko StreamFilter truncation semantics: a body over
  // the cap is sliced and flagged truncated, never dropped.
  var MAX_BODY = 1048576;

  // Armed state, driven by the ISOLATED content.js forwarding the SW's monitor state.
  var armed = false;
  var urlPatterns = [];

  // Same glob→regex matching gecko's shouldCaptureRequest uses ('*' → all; '*'→'.*',
  // '?'→'.'; invalid pattern → no match; no patterns → match all).
  function urlMatches(url) {
    if (!urlPatterns || urlPatterns.length === 0) return true;
    return urlPatterns.some(function (pattern) {
      if (pattern === '*') return true;
      var regexPattern = String(pattern).replace(/\*/g, '.*').replace(/\?/g, '.');
      try { return new RegExp(regexPattern).test(url); }
      catch (e) { return false; }
    });
  }

  // Arm/disarm from the ISOLATED content script (which pulls the state at document_start
  // and receives arm/disarm broadcasts from the SW).
  window.addEventListener('message', function (ev) {
    if (ev.source !== window || !ev.data || ev.data.__browserplugMonitorState !== true) return;
    armed = !!ev.data.armed;
    urlPatterns = Array.isArray(ev.data.urlPatterns) ? ev.data.urlPatterns : [];
  });

  function postBody(url, method, status, contentType, text) {
    try {
      var body = text;
      var truncated = false;
      if (typeof body === 'string' && body.length > MAX_BODY) {
        body = body.slice(0, MAX_BODY);
        truncated = true;
      }
      window.postMessage({
        __browserplugNetBody: true,
        url: url,
        method: method,
        status: status,
        contentType: contentType,
        body: body,
        truncated: truncated,
        ts: Date.now()
      }, '*');
    } catch (e) { /* never break the page */ }
  }

  // ---- fetch ----
  try {
    var origFetch = window.fetch;
    if (typeof origFetch === 'function') {
      window.fetch = function () {
        // The page's fetch runs and its promise is returned UNTOUCHED; capture is a
        // separate branch off the same promise, registered before the page attaches its
        // own handlers so response.clone() happens before the page consumes the body.
        var p = origFetch.apply(this, arguments);
        try {
          if (armed) {
            var input = arguments[0];
            var init = arguments[1];
            var method = (init && init.method) ||
              (input && typeof input === 'object' && input.method) || 'GET';
            var reqUrl = (typeof input === 'string') ? input : ((input && input.url) || '');
            p.then(function (response) {
              try {
                if (!armed) return;
                var url = (response && response.url) || reqUrl || '';
                if (!urlMatches(url)) return;
                var ct = '';
                try { ct = response.headers.get('content-type') || ''; } catch (e) { /* ignore */ }
                var status = response.status;
                response.clone().text().then(function (text) {
                  postBody(url, method, status, ct, text);
                }).catch(function () { /* body unreadable; skip */ });
              } catch (e) { /* ignore */ }
            }).catch(function () { /* page fetch rejected; its own handlers see it */ });
          }
        } catch (e) { /* capture setup failed; page fetch unaffected */ }
        return p;
      };
    }
  } catch (e) { /* leave window.fetch as-is */ }

  // ---- XMLHttpRequest ----
  try {
    var XHR = window.XMLHttpRequest;
    if (XHR && XHR.prototype) {
      var origOpen = XHR.prototype.open;
      var origSend = XHR.prototype.send;
      XHR.prototype.open = function (method, url) {
        try { this.__browserplugMethod = method; this.__browserplugUrl = url; } catch (e) { /* ignore */ }
        return origOpen.apply(this, arguments);
      };
      XHR.prototype.send = function () {
        try {
          if (armed) {
            var xhr = this;
            xhr.addEventListener('load', function () {
              try {
                if (!armed) return;
                var url = xhr.__browserplugUrl || xhr.responseURL || '';
                if (!urlMatches(url)) return;
                var rt = xhr.responseType;
                var text;
                if (rt === '' || rt === 'text') { text = xhr.responseText; }
                else if (rt === 'json') { try { text = JSON.stringify(xhr.response); } catch (e) { text = ''; } }
                else { return; } // blob/arraybuffer/document; no text body to capture
                var ct = '';
                try { ct = xhr.getResponseHeader('content-type') || ''; } catch (e) { /* ignore */ }
                postBody(url, xhr.__browserplugMethod || 'GET', xhr.status, ct, text);
              } catch (e) { /* ignore */ }
            });
          }
        } catch (e) { /* ignore */ }
        return origSend.apply(this, arguments);
      };
    }
  } catch (e) { /* leave XMLHttpRequest as-is */ }
})();
