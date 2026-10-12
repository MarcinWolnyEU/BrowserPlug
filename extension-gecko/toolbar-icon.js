// Toolbar icon: connection state x toolbar theme. Shared by both trees, byte-identical.
//
// updateActionIndicator() in each background.js already decides what the toolbar button
// should say about the connection; it hands the state here and this file owns everything
// about turning that into a picture. Splitting it out is what lets one file answer the
// question of which of the two glyph sets reads against the toolbar the user is actually
// looking at, without either background.js growing an engine branch.
//
// Both trees ship the same art, and the DIRECTORY LAYOUT is the contract with
// assets/icons/render.py:
//
//   icons/toolbar/{light,dark}/<state>/icon-<size>.png
//
// Two glyph sets (navy ink for a light toolbar, white ink for a dark one) because a
// toolbar icon is drawn ON the toolbar, and a theme can make that surface any colour.
// Picking between them means knowing the toolbar's colour. Firefox's background page can
// read it itself, and there are two ways, each covering the other's blind spot:
//
//   theme.getCurrent()     answers for an extension-supplied theme (the built-in Dark and
//                          Light count as extension-supplied), and returns an object with
//                          NO `colors` for the default theme — including "System theme —
//                          auto", the one setting that actually follows the OS.
//   prefers-color-scheme   answers for exactly that case, because an extension page's
//                          computed scheme follows the browser's.
//
// So: ask the theme first, fall back to the media query when it declines to answer, and
// re-pick on theme.onUpdated (a theme was applied or removed) and on the media query's own
// change event (the OS flipped under "System theme — auto"). Neither read needs the "theme"
// permission — that one gates theme.update()/reset(), which this file never calls.
//
// Chrome's service worker has neither matchMedia nor a theme API, so it cannot ask. It is
// TOLD instead: extension-chromium/scheme-probe.js keeps an offscreen document alive whose
// only job is to run matchMedia, and hands each answer to setTheme(). Until the first
// answer, and for good if the offscreen document cannot be created, the icon stays on the
// light set.

(function () {
    'use strict';

    const SIZES = [16, 24, 32, 48, 64];
    // Four pictures, in the order assets/icons/README.md tables them: the plug seated
    // and green, seated and amber (socket up, server not answering its health probes),
    // pulled back one pin-length (connecting, or retrying), pulled clear (down for any
    // reason). background.js decides which; this file only knows they each have a dir.
    const STATES = ['connected', 'unhealthy', 'connecting', 'disconnected'];

    // Read the namespace lazily rather than binding it once: on chromium this file is
    // importScripts'd right after shim.js, and the tests swap the globals between cases.
    function ns() {
        return globalThis.browser || globalThis.chrome || null;
    }

    function actionApi() {
        const b = ns();
        if (!b) return null;
        return b.browserAction || b.action || null;  // MV2 browser_action -> MV3 action
    }

    function mediaQuery() {
        if (typeof globalThis.matchMedia !== 'function') return null;
        try {
            return globalThis.matchMedia('(prefers-color-scheme: dark)');
        } catch (e) {
            return null;
        }
    }

    // Seeded synchronously so the first paint has a path; resolveTheme() (Firefox) or
    // setTheme() (Chromium) corrects it a tick later if the toolbar turns out to be dark.
    // Light is also where a browser that can never answer stays.
    let themeDir = 'light';
    let lastState = 'connecting';

    // --- reading a theme colour ---------------------------------------------------------

    // Theme colours arrive as "#rgb", "#rrggbb", "rgb()/rgba()", or [r,g,b(,a)].
    function parseColor(value) {
        if (Array.isArray(value)) {
            if (value.length < 3) return null;
            if (value.length > 3 && Number(value[3]) === 0) return null;  // fully transparent
            return [Number(value[0]), Number(value[1]), Number(value[2])];
        }
        if (typeof value !== 'string') return null;
        const text = value.trim();
        const hex = /^#([0-9a-f]{3,8})$/i.exec(text);
        if (hex) {
            const d = hex[1];
            if (d.length === 3 || d.length === 4) {
                return [0, 1, 2].map(function (i) { return parseInt(d[i] + d[i], 16); });
            }
            if (d.length === 6 || d.length === 8) {
                return [0, 2, 4].map(function (i) { return parseInt(d.slice(i, i + 2), 16); });
            }
            return null;
        }
        const fn = /^rgba?\(([^)]+)\)$/i.exec(text);
        if (fn) {
            const parts = fn[1].split(/[,/\s]+/).filter(function (p) { return p !== ''; });
            if (parts.length < 3) return null;
            if (parts.length > 3 && parseFloat(parts[3]) === 0) return null;
            const rgb = parts.slice(0, 3).map(function (p) { return parseFloat(p); });
            if (rgb.some(function (n) { return !isFinite(n); })) return null;
            return rgb;
        }
        return null;
    }

    // Perceived brightness (the YIQ luma, 0..1), NOT the WCAG relative luminance — the only
    // question here is "is this surface dark", and mid-grey has to land on 0.5.
    function brightness(rgb) {
        return (rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114) / 255;
    }

    // 'light' | 'dark' | null (this theme said nothing useable).
    function themeFromColors(colors) {
        // A theme that styles its toolbar ink has already answered the question — match it.
        // bookmark_text is the older spelling of toolbar_text; themes still ship either.
        const ink = parseColor(colors.icons)
            || parseColor(colors.toolbar_text)
            || parseColor(colors.bookmark_text);
        if (ink) return brightness(ink) > 0.5 ? 'dark' : 'light';
        const surface = parseColor(colors.toolbar) || parseColor(colors.frame);
        if (surface) return brightness(surface) < 0.5 ? 'dark' : 'light';
        return null;
    }

    function resolveTheme() {
        const b = ns();
        const fromMedia = function () {
            const mql = mediaQuery();
            return mql ? (mql.matches ? 'dark' : 'light') : null;
        };
        if (!b || !b.theme || typeof b.theme.getCurrent !== 'function') {
            return Promise.resolve(fromMedia());
        }
        let getting;
        try {
            getting = Promise.resolve(b.theme.getCurrent());
        } catch (e) {
            return Promise.resolve(fromMedia());
        }
        return getting.then(
            function (theme) {
                // The default theme (incl. "System theme — auto") answers with no colors.
                const picked = theme && theme.colors ? themeFromColors(theme.colors) : null;
                return picked || fromMedia();
            },
            function () { return fromMedia(); }
        );
    }

    // --- painting -----------------------------------------------------------------------

    function pathsFor(state) {
        const known = STATES.indexOf(state) >= 0 ? state : 'disconnected';
        const paths = {};
        for (const size of SIZES) {
            paths[size] = 'icons/toolbar/' + themeDir + '/' + known + '/icon-' + size + '.png';
        }
        return paths;
    }

    // Paint `state` and remember it, so a later theme change repaints the same state.
    function apply(state) {
        lastState = STATES.indexOf(state) >= 0 ? state : 'disconnected';
        const action = actionApi();
        if (!action || typeof action.setIcon !== 'function') return false;
        const paths = pathsFor(lastState);
        try {
            const result = action.setIcon({ path: paths });
            // MV3 rejects the promise when a path does not resolve. Say so: a missing file
            // is a build mistake, and an icon that silently never changes looks exactly
            // like a connection state that simply never moved.
            if (result && typeof result.catch === 'function') {
                result.catch(function (e) {
                    console.warn('BrowserPlug: setIcon failed', paths[16], e);
                });
            }
        } catch (e) {
            console.warn('BrowserPlug: setIcon failed', paths[16], e);
            return false;
        }
        return true;
    }

    // Re-read the toolbar theme and repaint if it moved. Resolves to the theme in use.
    function refresh() {
        return resolveTheme().then(function (picked) {
            return setTheme(picked);
        });
    }

    // Adopt a theme someone else worked out (an MV3 worker cannot ask for itself) and
    // repaint if it moved. Anything but 'light' or 'dark' is ignored, which is what keeps
    // a browser with no signal on the seed instead of on a path that does not exist.
    function setTheme(picked) {
        if ((picked !== 'light' && picked !== 'dark') || picked === themeDir) return themeDir;
        themeDir = picked;
        apply(lastState);
        return themeDir;
    }

    function watchTheme() {
        const b = ns();
        if (b && b.theme && b.theme.onUpdated
            && typeof b.theme.onUpdated.addListener === 'function') {
            try {
                b.theme.onUpdated.addListener(function () { refresh(); });
            } catch (e) {
                console.warn('BrowserPlug: theme.onUpdated unavailable', e);
            }
        }
        const mql = mediaQuery();
        if (!mql) return;
        const onChange = function () { refresh(); };
        if (typeof mql.addEventListener === 'function') mql.addEventListener('change', onChange);
        else if (typeof mql.addListener === 'function') mql.addListener(onChange);
    }

    globalThis.browserplugToolbarIcon = {
        SIZES: SIZES,
        STATES: STATES,
        pathsFor: pathsFor,
        apply: apply,
        refresh: refresh,
        setTheme: setTheme,
        theme: function () { return themeDir; }
    };

    watchTheme();
    refresh();
}());
