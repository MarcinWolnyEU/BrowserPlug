/*
 * censor-panel.js: the "Captured bodies" card in Options > General.
 *
 * The censoring itself is body-censor.js, which the background shares. This file owns
 * the card: the toggle, the patterns textarea with its feedback list, and the Test box.
 * options.js calls init() once, load() whenever it fills the form, and reset() from
 * Reset to defaults. It registers `globalThis.BrowserplugCensorPanel`; load it after
 * body-censor.js and before options.js.
 *
 * Storage. The toggle (`censorSecrets`) and the patterns (`censorPatterns`) live in
 * storage.sync, where the background reads them on every requests(get). The Test box's
 * sample does not. The box is there for pasting a real captured body, and Firefox Sync
 * copies storage.sync to the user's other devices and to Mozilla's servers, so the
 * sample stays in storage.local (`censorTestText`), like the auto-pulled token. A sample
 * an older build left in storage.sync is moved to storage.local on load and deleted from
 * storage.sync.
 *
 * The toggle and the patterns are written separately. storage.sync caps one item at
 * 8 KB of UTF-8, so twenty long non-ASCII patterns can fail their write, and that
 * failure must not stop the user switching censoring on or off.
 */
(() => {
    'use strict';

    const TEST_KEY = 'censorTestText';
    // How much of a pasted sample is kept and re-censored on each keystroke.
    const TEST_MAX = 3000;

    let showStatus = () => {};

    const el = (id) => document.getElementById(id);
    const censorModule = () => globalThis.BrowserplugBodyCensor || null;

    function analyzePatterns() {
        const C = censorModule();
        if (!C) return null;
        return C.analyzePatterns(C.splitPatternLines(el('censorPatterns').value));
    }

    // One line per pattern the background will ignore, with the reason. That includes the
    // ones the page does not store (past the 20th, over 200 characters).
    function renderFeedback() {
        const box = el('censorPatternFeedback');
        if (!box) return;
        box.textContent = '';
        const add = (cls, text) => {
            const line = document.createElement('div');
            line.className = `field-note ${cls}`;
            line.textContent = text;
            box.appendChild(line);
        };
        const analysis = analyzePatterns();
        if (analysis === null) {
            add('err', 'The censoring module did not load, so patterns cannot be checked or applied.');
            return;
        }
        let active = 0;
        analysis.forEach((a, i) => {
            if (a.ok) { active++; return; }
            const shown = a.pattern.length > 40 ? a.pattern.slice(0, 40) + '…' : a.pattern;
            add('err', `Pattern ${i + 1} (${shown}) is ignored: ${a.reason}`);
        });
        if (active > 0) add('ok', `${active} pattern${active === 1 ? '' : 's'} active`);
    }

    // What the agent would get for the pasted sample: the same module the background runs,
    // with the built-in masking on (the row is only visible while it is) and the patterns
    // as typed, saved or not.
    function renderTest() {
        const input = el('censorTestInput');
        const out = el('censorTestOutput');
        const note = el('censorTestNote');
        if (!input || !out || !note) return;
        out.textContent = '';
        const C = censorModule();
        if (!C) {
            note.textContent = 'The censoring module did not load.';
            return;
        }
        if (input.value === '') {
            note.textContent = 'Paste a sample body to see it censored.';
            return;
        }
        const patterns = C.splitPatternLines(el('censorPatterns').value);
        try {
            const r = C.censorText(input.value, '', C.prepareSettings({ censorSecrets: true, censorPatterns: patterns }));
            out.textContent = r.content;
            note.textContent = r.count === 0 ? 'Nothing matched.'
                : `${r.count} value${r.count === 1 ? '' : 's'} censored.`;
        } catch (error) {
            out.textContent = C.REDACTED;
            note.textContent = 'Censoring failed; the agent would get the body withheld.';
        }
    }

    // The patterns row (the patterns and the Test box beside them) belongs to the toggle.
    // The background ignores stored patterns while censoring is off, so the page hides a
    // field that would do nothing.
    function syncPanel() {
        const box = el('censorSecrets');
        const row = el('censorPatternsField');
        if (box && row) row.hidden = !box.checked;
        renderTest();
    }

    async function saveToggle() {
        syncPanel();
        try {
            await browser.storage.sync.set({ censorSecrets: el('censorSecrets').checked });
            showStatus('Body censoring settings saved', 'success');
        } catch (error) {
            console.error('Error saving body censoring settings:', error);
            showStatus('Error saving body censoring settings', 'error');
        }
    }

    async function savePatterns() {
        const analysis = analyzePatterns();
        renderFeedback();
        // Without the module there is nothing to validate against; keep what is stored.
        if (analysis === null) return;
        try {
            await browser.storage.sync.set({
                censorPatterns: analysis.filter((a) => a.storable).map((a) => a.pattern)
            });
            showStatus('Censoring patterns saved', 'success');
        } catch (error) {
            console.error('Error saving censoring patterns:', error);
            showStatus(`Censoring patterns not saved: ${(error && error.message) || error}`, 'error');
        }
    }

    async function saveTestText() {
        try {
            await browser.storage.local.set({ [TEST_KEY]: el('censorTestInput').value.slice(0, TEST_MAX) });
        } catch (error) {
            console.error('Error saving the censor test sample:', error);
        }
    }

    // Returns the stored sample. A sample still in storage.sync (written by an older
    // build) is copied to storage.local when that has none, then deleted from sync.
    async function loadTestText(synced) {
        let sample = '';
        try {
            const local = await browser.storage.local.get({ [TEST_KEY]: null });
            sample = local[TEST_KEY];
            if (typeof synced === 'string') {
                if (typeof sample !== 'string') {
                    sample = synced.slice(0, TEST_MAX);
                    await browser.storage.local.set({ [TEST_KEY]: sample });
                }
                await browser.storage.sync.remove(TEST_KEY);
            }
        } catch (error) {
            console.error('Error loading the censor test sample:', error);
        }
        return typeof sample === 'string' ? sample.slice(0, TEST_MAX) : '';
    }

    async function load() {
        let stored = { censorSecrets: false, censorPatterns: [], [TEST_KEY]: null };
        try {
            stored = await browser.storage.sync.get(stored);
        } catch (error) {
            console.error('Error loading body censoring settings:', error);
        }
        el('censorSecrets').checked = stored.censorSecrets === true;
        el('censorPatterns').value = Array.isArray(stored.censorPatterns)
            ? stored.censorPatterns.filter((p) => typeof p === 'string').join('\n') : '';
        el('censorTestInput').value = await loadTestText(stored[TEST_KEY]);
        renderFeedback();
        syncPanel();
    }

    // Reset to defaults clears storage.sync; the sample lives in storage.local.
    async function reset() {
        try {
            await browser.storage.local.remove(TEST_KEY);
        } catch (error) {
            console.error('Error clearing the censor test sample:', error);
        }
    }

    function init(opts) {
        if (opts && typeof opts.showStatus === 'function') showStatus = opts.showStatus;
        const on = (id, type, fn) => { const node = el(id); if (node) node.addEventListener(type, fn); };
        on('censorSecrets', 'change', saveToggle);
        // The feedback and the Test output follow every keystroke; the saves wait for the
        // change event (blur), like the other text settings.
        on('censorPatterns', 'input', () => { renderFeedback(); renderTest(); });
        on('censorPatterns', 'change', savePatterns);
        on('censorTestInput', 'input', renderTest);
        on('censorTestInput', 'change', saveTestText);
    }

    globalThis.BrowserplugCensorPanel = Object.freeze({ init, load, reset, TEST_KEY, TEST_MAX });
})();
