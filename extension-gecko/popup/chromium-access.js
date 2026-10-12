/*
 * BrowserPlug: Chromium-only permission helpers for the popup
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Two questions the popup asks only on Chrome, Edge and Brave: is the per-extension
 * "Allow user scripts" switch on, and where is this extension's page in chrome://extensions.
 * Pure functions with the platform handed in, so a node test drives them without a browser.
 * Loaded in the POPUP; publishes `ChromiumAccess`. Shared byte-identical with both trees
 * and inert on Firefox, where popup.js never calls it.
 */
(function () {
  'use strict';

  // Same probe as userScriptsAvailable() in the chromium background.js. With the switch
  // off, reading `chrome.userScripts` throws rather than returning undefined.
  function userScriptsEnabled(api) {
    try {
      return !!(api && api.userScripts && typeof api.userScripts.execute === 'function');
    } catch (e) {
      return false;
    }
  }

  // The scheme is the browser's own: Edge and Brave answer chrome:// only by redirecting
  // it. `brave` is `navigator.brave`, which only Brave defines.
  function extensionsScheme({ userAgent = '', brave = false } = {}) {
    if (brave) return 'brave';
    if (/\bEdg\//.test(userAgent)) return 'edge';
    return 'chrome';
  }

  // The extension's own card; the id changes with every install, so it is never baked in.
  function extensionsPageUrl(id, platform) {
    return `${extensionsScheme(platform)}://extensions/?id=${encodeURIComponent(id)}`;
  }

  globalThis.ChromiumAccess = { userScriptsEnabled, extensionsScheme, extensionsPageUrl };
})();
