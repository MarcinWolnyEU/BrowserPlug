/*
 * BrowserPlug Chromium Extension - namespace shim (imported first by background.js)
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 */

// Chrome has no `browser` global. Alias it so the shared vendor data modules
// (vendor/anthropic-data.js, vendor/kilocode-modelnames-data.js) and every
// WebExtensions-style `browser.*` idiom in background.js work unchanged.
//
// `||` (not `??`) on purpose: in the SW `chrome` is always defined, and a
// Firefox-flavored context (never actually reached here) would keep its own
// globalThis.browser. Chrome MV3 `chrome.*` returns promises when the callback is
// omitted, so the promise-style `await browser.foo(...)` calls in background.js
// behave the same as they do under Firefox's native `browser`.
globalThis.browser = globalThis.browser || chrome;
