// The popup's tab strip (extension-gecko/popup/popup-tabs.js) against the REAL
// popup.html under jsdom. What it pins: the strip restores the tab it was left on,
// a click or an arrow key moves both the selection and the visible pane together,
// the choice is written back to storage.local (but not on the restore), and onShow
// reports every switch including the first. popup-tabs.js is a shared file, so one
// run covers both trees.
// Run: node --test tests/js/popup-tabs.test.mjs   (or: npm run test:js)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const POPUP_HTML = readFileSync(path.join(__dirname, '../../extension-gecko/popup/popup.html'), 'utf8');
const TABS_JS = readFileSync(path.join(__dirname, '../../extension-gecko/popup/popup-tabs.js'), 'utf8');

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function fakeStorage(initial = {}) {
  const data = { ...initial };
  const writes = [];
  return {
    data, writes,
    get: (defaults) => Promise.resolve({ ...defaults, ...data }),
    set: (obj) => { Object.assign(data, obj); writes.push({ ...obj }); return Promise.resolve(); },
  };
}

async function load({ stored = {}, onShow = null } = {}) {
  const dom = new JSDOM(POPUP_HTML, { runScripts: 'outside-only' });
  const { window } = dom;
  window.eval(TABS_JS);
  const storage = fakeStorage(stored);
  const shown = [];
  const first = await window.PopupTabs.init({
    storage,
    onShow: (name) => { shown.push(name); if (onShow) onShow(name); },
  });
  await flush();
  return { window, document: window.document, storage, shown, first };
}

const selected = (document) =>
  [...document.querySelectorAll('.tabs [role="tab"]')].filter(b => b.getAttribute('aria-selected') === 'true')
    .map(b => b.dataset.tab);
const visiblePanes = (document) =>
  [...document.querySelectorAll('.pane[data-pane]')].filter(p => !p.hidden).map(p => p.dataset.pane);

test('popup.html carries the three tabs and their panes, one selected, one visible', () => {
  const dom = new JSDOM(POPUP_HTML);
  const d = dom.window.document;
  assert.deepEqual([...d.querySelectorAll('.tabs [role="tab"]')].map(b => b.dataset.tab),
    ['status', 'permissions', 'usage']);
  assert.deepEqual([...d.querySelectorAll('.pane[data-pane]')].map(p => p.dataset.pane),
    ['status', 'permissions', 'usage']);
  assert.deepEqual(selected(d), ['status'], 'the markup opens on Status before any script runs');
  assert.deepEqual(visiblePanes(d), ['status']);
  dom.window.close();
});

test('with nothing stored the strip opens on Status and reports it once', async () => {
  const { window, document, storage, shown, first } = await load();
  try {
    assert.equal(first, 'status');
    assert.deepEqual(selected(document), ['status']);
    assert.deepEqual(visiblePanes(document), ['status']);
    assert.deepEqual(shown, ['status'], 'the restore counts as a switch, so the pane can render itself');
    assert.deepEqual(storage.writes, [], 'restoring what was just read is not written back');
  } finally {
    window.close();
  }
});

test('the stored tab is restored', async () => {
  const { window, document, shown } = await load({ stored: { popupTab: 'usage' } });
  try {
    assert.deepEqual(selected(document), ['usage']);
    assert.deepEqual(visiblePanes(document), ['usage']);
    assert.deepEqual(shown, ['usage']);
  } finally {
    window.close();
  }
});

test('a stored value that is not a tab falls back to Status', async () => {
  const { window, document } = await load({ stored: { popupTab: 'settings' } });
  try {
    assert.deepEqual(selected(document), ['status']);
  } finally {
    window.close();
  }
});

test('a click moves the selection and the pane together, and is remembered', async () => {
  const { window, document, storage, shown } = await load();
  try {
    document.querySelector('[data-tab="permissions"]').dispatchEvent(new window.Event('click', { bubbles: true }));
    await flush();
    assert.deepEqual(selected(document), ['permissions']);
    assert.deepEqual(visiblePanes(document), ['permissions'], 'exactly one pane shows at a time');
    assert.deepEqual(storage.writes, [{ popupTab: 'permissions' }]);
    assert.deepEqual(shown, ['status', 'permissions']);

    // Clicking the tab that is already showing is not a switch: nothing to report.
    document.querySelector('[data-tab="permissions"]').dispatchEvent(new window.Event('click', { bubbles: true }));
    await flush();
    assert.deepEqual(shown, ['status', 'permissions']);
  } finally {
    window.close();
  }
});

test('arrow keys walk the strip like a native tab list and wrap at the ends', async () => {
  const { window, document } = await load();
  try {
    const key = (name, on) => {
      const e = new window.KeyboardEvent('keydown', { key: name, bubbles: true });
      on.dispatchEvent(e);
    };
    key('ArrowRight', document.querySelector('[data-tab="status"]'));
    assert.deepEqual(selected(document), ['permissions']);
    key('ArrowRight', document.querySelector('[data-tab="permissions"]'));
    assert.deepEqual(selected(document), ['usage']);
    key('ArrowRight', document.querySelector('[data-tab="usage"]'));
    assert.deepEqual(selected(document), ['status'], 'wraps from the last tab to the first');
    key('ArrowLeft', document.querySelector('[data-tab="status"]'));
    assert.deepEqual(selected(document), ['usage'], 'and back the other way');
    assert.deepEqual(visiblePanes(document), ['usage']);
    // Roving tabindex: only the selected tab is in the tab order.
    const idx = [...document.querySelectorAll('.tabs [role="tab"]')].map(b => b.tabIndex);
    assert.deepEqual(idx, [-1, -1, 0]);
  } finally {
    window.close();
  }
});

test('a storage area that fails still leaves a working strip', async () => {
  const dom = new JSDOM(POPUP_HTML, { runScripts: 'outside-only' });
  const { window } = dom;
  try {
    window.eval(TABS_JS);
    const first = await window.PopupTabs.init({
      storage: { get: () => Promise.reject(new Error('no storage')), set: () => Promise.reject(new Error('no storage')) },
    });
    assert.equal(first, 'status');
    window.PopupTabs.show('usage');
    await flush();
    assert.deepEqual(visiblePanes(window.document), ['usage'], 'a failed write costs the memory, not the switch');
  } finally {
    window.close();
  }
});

// --- a tab can be taken off the strip -------------------------------------------------
// The Usage tab is the only one that uses this: with every provider turned off there is
// nothing behind it (popup.js decides, applyUsageTabVisibility). What the strip owes is
// that a hidden tab is unreachable by every route into it — the button, the arrow keys,
// a stale memory, and a direct show() — and that the user is never stranded on it.
const tabButton = (document, name) =>
  document.querySelector(`.tabs [role="tab"][data-tab="${name}"]`);

test('a hidden tab leaves the strip and cannot be shown', async () => {
  const { window, document } = await load();
  try {
    window.PopupTabs.setVisible('usage', false);
    assert.equal(tabButton(document, 'usage').hidden, true, 'the button is off the strip');
    assert.equal(window.PopupTabs.isVisible('usage'), false);

    window.PopupTabs.show('usage');
    await flush();
    assert.equal(window.PopupTabs.current(), 'status',
      'show() must not open a tab that is not there — it falls back to the first that is');
    assert.deepEqual(visiblePanes(document), ['status']);
  } finally {
    window.close();
  }
});

test('hiding the tab the user is standing on steps them off it', async () => {
  const { window, document } = await load({ stored: { popupTab: 'usage' } });
  try {
    assert.equal(window.PopupTabs.current(), 'usage');
    window.PopupTabs.setVisible('usage', false);
    await flush();
    assert.equal(window.PopupTabs.current(), 'status');
    assert.deepEqual(selected(document), ['status']);
    assert.deepEqual(visiblePanes(document), ['status'],
      'the pane goes with the tab; a hidden tab must not leave its pane on screen');
  } finally {
    window.close();
  }
});

test('a remembered tab that is hidden opens on Status without forgetting it', async () => {
  // Hidden BEFORE init, which is where popup.js calls it from. The memory is left alone
  // on purpose: turn a provider back on and the popup returns to where it was left.
  const dom = new JSDOM(POPUP_HTML, { runScripts: 'outside-only' });
  const { window } = dom;
  try {
    window.eval(TABS_JS);
    const storage = fakeStorage({ popupTab: 'usage' });
    window.PopupTabs.setVisible('usage', false);
    const first = await window.PopupTabs.init({ storage });
    assert.equal(first, 'status');
    assert.equal(storage.data.popupTab, 'usage', 'the remembered tab survives being hidden');

    window.PopupTabs.setVisible('usage', true);
    assert.equal(tabButton(window.document, 'usage').hidden, false);
    window.PopupTabs.show('usage');
    await flush();
    assert.deepEqual(visiblePanes(window.document), ['usage']);
  } finally {
    window.close();
  }
});

test('the arrow keys skip a hidden tab instead of landing on it', async () => {
  const { window, document } = await load();
  try {
    window.PopupTabs.setVisible('usage', false);
    const permissions = tabButton(document, 'permissions');
    window.PopupTabs.show('permissions');
    await flush();
    permissions.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    await flush();
    assert.equal(window.PopupTabs.current(), 'status', 'right from the last shown tab wraps to the first');

    tabButton(document, 'status').dispatchEvent(
      new window.KeyboardEvent('keydown', { key: 'End', bubbles: true }));
    await flush();
    assert.equal(window.PopupTabs.current(), 'permissions', 'End is the last SHOWN tab, not Usage');
  } finally {
    window.close();
  }
});
