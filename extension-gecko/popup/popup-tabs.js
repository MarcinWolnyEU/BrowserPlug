/*
 * BrowserPlug: the popup's tab strip
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Status / Permissions / Usage, the options page's sidebar collapsed into a row the
 * way about:preferences collapses its own on a narrow window. Owns nothing but which
 * pane is showing: the panes render themselves (popup.js, usage-pane.js). The last
 * choice is remembered in storage.local, so the popup opens where it was left.
 * Loaded in the POPUP; publishes `PopupTabs`. Shared byte-identical with the chromium
 * tree, hence no `browser` global here: the caller hands in the storage area.
 */
(function () {
  'use strict';

  const TABS = ['status', 'permissions', 'usage'];
  const STORAGE_KEY = 'popupTab';

  let current = null;
  let storage = null;
  const listeners = [];
  // Tabs taken off the strip. Only Usage uses this today: with every provider turned off
  // there is nothing behind it, and a tab whose one job is to explain its own emptiness
  // is worse than no tab. What is REMEMBERED is left alone, so turning a provider back
  // on reopens the popup where the user left it.
  const hiddenTabs = new Set();

  const tabButtons = () => [...document.querySelectorAll('.tabs [role="tab"][data-tab]')];
  const shownButtons = () => tabButtons().filter(b => !hiddenTabs.has(b.dataset.tab));
  const panes = () => [...document.querySelectorAll('.pane[data-pane]')];

  function firstShown() {
    const b = shownButtons()[0];
    return b ? b.dataset.tab : TABS[0];
  }

  // Show one pane, hide the others, and tell whoever asked. `persist` is false only for
  // the restore at load: writing back what was just read is a wasted write, and on
  // Android storage.local writes are not free.
  function show(name, { persist = true } = {}) {
    if (!TABS.includes(name)) name = TABS[0];
    if (hiddenTabs.has(name)) name = firstShown();
    for (const b of tabButtons()) {
      const on = b.dataset.tab === name;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    for (const p of panes()) p.hidden = p.dataset.pane !== name;
    const changed = current !== name;
    current = name;
    if (persist && storage && storage.set) {
      Promise.resolve(storage.set({ [STORAGE_KEY]: name })).catch(() => {});
    }
    if (changed) for (const fn of listeners) fn(name);
    return name;
  }

  // Arrow keys move between tabs like a native tab list; the pane follows the focus.
  function onKey(e) {
    const order = shownButtons().map(b => b.dataset.tab);
    if (!order.length) return;
    const i = order.indexOf(current);
    let next = null;
    if (e.key === 'ArrowRight') next = order[(i + 1) % order.length];
    else if (e.key === 'ArrowLeft') next = order[(i - 1 + order.length) % order.length];
    else if (e.key === 'Home') next = order[0];
    else if (e.key === 'End') next = order[order.length - 1];
    if (!next) return;
    e.preventDefault();
    show(next);
    const b = tabButtons().find(x => x.dataset.tab === next);
    if (b) b.focus();
  }

  /**
   * Take a tab off the strip, or put it back. A hidden tab cannot be selected, is skipped
   * by the arrow keys, and is stepped off if it was the one showing. Safe to call before
   * init(), which is where popup.js calls it from — deciding after the restore would open
   * the popup on a tab that vanishes a frame later.
   */
  function setVisible(name, visible) {
    if (!TABS.includes(name)) return;
    if (visible) hiddenTabs.delete(name);
    else hiddenTabs.add(name);
    for (const b of tabButtons()) {
      if (b.dataset.tab === name) b.hidden = !visible;
    }
    if (!visible && current === name) show(firstShown());
  }

  /**
   * Wire the strip. `storage` is a storage area with get/set (storage.local); `onShow`
   * runs with the tab name every time the visible pane changes, including the restore.
   * Resolves to the tab shown.
   */
  async function init({ storage: area = null, onShow = null } = {}) {
    storage = area;
    if (onShow) listeners.push(onShow);
    for (const b of tabButtons()) {
      b.addEventListener('click', () => show(b.dataset.tab));
      b.addEventListener('keydown', onKey);
    }
    let remembered = TABS[0];
    try {
      if (storage && storage.get) {
        const got = await storage.get({ [STORAGE_KEY]: TABS[0] });
        if (got && TABS.includes(got[STORAGE_KEY])) remembered = got[STORAGE_KEY];
      }
    } catch (e) {
      // A failed read costs the memory, not the popup.
    }
    // `current` starts null, so the restore always counts as a change and onShow fires
    // once for the pane the popup opened on.
    return show(remembered, { persist: false });
  }

  globalThis.PopupTabs = {
    init, show, setVisible, current: () => current,
    isVisible: (name) => !hiddenTabs.has(name),
    TABS, STORAGE_KEY,
  };
})();
