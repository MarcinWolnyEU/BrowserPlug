// The colour-scheme probe's page half. A service worker has no matchMedia, an offscreen
// document does, so this page exists only to read prefers-color-scheme and tell the worker.
// scheme-probe.js is the other half and owns the message names; keep the two in step.
(function () {
    'use strict';

    const mql = window.matchMedia('(prefers-color-scheme: dark)');

    function report() {
        try {
            const sent = chrome.runtime.sendMessage({
                type: 'browserplug.colorScheme', dark: mql.matches
            });
            if (sent && typeof sent.catch === 'function') sent.catch(function () {});
        } catch (e) {
            // No receiver (the worker is asleep) is normal; it asks again when it wakes.
        }
    }

    mql.addEventListener('change', report);
    chrome.runtime.onMessage.addListener(function (message) {
        if (message && message.type === 'browserplug.colorScheme.query') report();
    });
    report();
}());
