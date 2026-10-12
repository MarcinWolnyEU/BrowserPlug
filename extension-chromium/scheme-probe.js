// Chromium only: tells toolbar-icon.js whether the browser is in light or dark mode.
//
// The toolbar icon has a light and a dark glyph set (see toolbar-icon.js), and picking one
// needs prefers-color-scheme, which an MV3 service worker cannot read. The documented way
// round is an offscreen document with the MATCH_MEDIA reason: it runs matchMedia and
// reports each answer here. This file keeps that document alive and forwards what it says
// to toolbarIcon.setTheme(); offscreen.js is the page half.
//
// It answers for the browser's colour mode, not for a custom Chrome theme, which no
// extension API can read. Where the document cannot be created the icon simply stays on
// the light set that toolbar-icon.js seeds itself with.

(function () {
    'use strict';

    const REPORT = 'browserplug.colorScheme';
    const QUERY = 'browserplug.colorScheme.query';
    const PAGE = 'offscreen.html';

    function api() {
        return globalThis.chrome || null;
    }

    async function hasDocument(c) {
        if (typeof c.runtime.getContexts !== 'function') return false;
        const found = await c.runtime.getContexts({
            contextTypes: ['OFFSCREEN_DOCUMENT'],
            documentUrls: [c.runtime.getURL(PAGE)]
        });
        return found.length > 0;
    }

    // Create the document if it is not there, then ask it for the current answer. A fresh
    // document reports on load and an old one only on change, so the query is what lets a
    // worker that just woke up learn a scheme that changed while it slept.
    async function ensure() {
        const c = api();
        if (!c || !c.offscreen || typeof c.offscreen.createDocument !== 'function') return false;
        try {
            if (!(await hasDocument(c))) {
                await c.offscreen.createDocument({
                    url: PAGE,
                    reasons: ['MATCH_MEDIA'],
                    justification: 'Read prefers-color-scheme to pick the light or dark toolbar icon.'
                });
            }
            await c.runtime.sendMessage({ type: QUERY });
            return true;
        } catch (e) {
            console.warn('BrowserPlug: colour-scheme probe unavailable, toolbar icon stays light', e);
            return false;
        }
    }

    function onMessage(message, sender) {
        const c = api();
        if (!message || message.type !== REPORT || !c || sender.id !== c.runtime.id) return;
        const icon = globalThis.browserplugToolbarIcon;
        if (icon && typeof icon.setTheme === 'function') {
            icon.setTheme(message.dark ? 'dark' : 'light');
        }
    }

    // Registered synchronously at load: a worker woken by the document's own message has
    // to have its listener in place before the first await.
    const c = api();
    if (c && c.runtime && c.runtime.onMessage) c.runtime.onMessage.addListener(onMessage);

    globalThis.browserplugSchemeProbe = { ensure: ensure, onMessage: onMessage };
    ensure();
}());
