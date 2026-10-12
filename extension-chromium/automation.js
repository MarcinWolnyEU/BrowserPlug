/*
 * BrowserPlug Firefox Extension - Page automation helpers
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Defines window.__browserplugAuto: a self-contained DOM-automation toolkit that
 * background.js injects into a tab/frame via browser.tabs.executeScript. It runs
 * in the content-script isolated world (Firefox Xray vision), which is enough to
 * read the page structure and drive reactive frameworks (the native value-setter
 * + dispatched input/change trick works from here).
 *
 * Every method returns a JSON-serializable object (or a Promise of one). Async
 * methods rely on Firefox resolving a Promise returned from executeScript.
 *
 * The object persists on `window` for the life of the document, so element refs
 * minted by snapshot() stay valid for later click()/fill() calls (until navigation).
 *
 * v2: single-shot waitCheck (background orchestrates waits so they survive
 * navigations), target/nth/within/enabled_only disambiguation, snapshot dedupe
 * + forms mode, pageState diagnostics, press/hover/scroll, fill handles <select>.
 *
 * Two later additions, both about not reporting a plausible falsehood:
 *   snapshot mode:"records" extracts repeated listing cards with every field
 *     scoped to ONE card, flags a card holding several live prices as ambiguous
 *     instead of picking, and refuses when no pattern is found with confidence;
 *   click resolves the toggle behind the target and reports its real
 *     post-settle state; a checkbox that did not move is ok:false, not a bare "ok".
 */
(function () {
  'use strict';
  if (window.__browserplugAuto && window.__browserplugAuto.__v === 2) return;

  const self = {};
  self.__v = 2;
  self.refs = {};               // ref id -> Element
  self.elRefs = new WeakMap();  // Element -> ref id
  self.refSeq = 0;

  // Stable per-DOCUMENT identity (Firefox 153+, Chromium since MV3), stamped onto every
  // result envelope so the background can tell "that element is gone" apart from "that
  // ref belonged to a document which no longer exists". frameId cannot answer this: ids
  // are REUSED, and a navigation swaps the document underneath the same frameId, so a
  // ref minted before the navigation and an element merely removed after it produce the
  // identical "not found". This toolkit dies with its document, so the value is captured
  // once at construction; re-injection into a live document hits the __v guard above and
  // keeps the original.
  //
  // null when the browser is too old, or the API is unreachable from this context. Every
  // consumer must read null as UNKNOWN and never as "the document changed"; a false
  // staleness claim would send an agent re-snapshotting a page that was fine.
  function documentIdOrNull() {
    try {
      const rt = (typeof browser !== 'undefined' && browser.runtime) ||
                 (typeof chrome !== 'undefined' && chrome.runtime) || null;
      if (!rt || typeof rt.getDocumentId !== 'function') return null;
      // Takes a WindowProxy or a frame element, NOT zero arguments; a no-arg call
      // throws "Incorrect argument types" (live-verified, Firefox 153).
      return rt.getDocumentId(window) || null;
    } catch (e) {
      return null;   // includes the documented throw for an already-unloaded frame
    }
  }
  self.docId = documentIdOrNull();

  // ---- low-level helpers ---------------------------------------------------

  function refFor(el) {
    let r = self.elRefs.get(el);
    if (!r) {
      r = 'e' + (++self.refSeq);
      self.elRefs.set(el, r);
    }
    self.refs[r] = el; // keep the strong map current
    return r;
  }

  function isVisible(el) {
    if (!(el instanceof Element)) return false;
    // getClientRects() is empty when the element OR any ancestor is display:none
    if (el.getClientRects().length === 0) {
      // <option>/<select> internals have no rects but are still meaningful
      if (el.tagName !== 'OPTION') return false;
    }
    const cs = window.getComputedStyle(el);
    if (!cs) return false;
    if (cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
    if (parseFloat(cs.opacity || '1') === 0) return false;
    return true;
  }

  function isEnabled(el) {
    if (el.disabled) return false;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
    return true;
  }

  function textOf(el) {
    if (!el) return '';
    return (el.innerText || el.textContent || '').trim();
  }

  function labelText(el) {
    // Associated <label for=id> or wrapping <label>
    try {
      if (el.id) {
        const lab = document.querySelector('label[for="' + (window.CSS ? CSS.escape(el.id) : el.id) + '"]');
        const t = textOf(lab);
        if (t) return t;
      }
      let p = el.closest && el.closest('label');
      const pt = textOf(p);
      if (pt) return pt;
    } catch (e) { /* ignore */ }
    return '';
  }

  function accessibleName(el) {
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const labelledby = el.getAttribute && el.getAttribute('aria-labelledby');
    if (labelledby) {
      const parts = labelledby.split(/\s+/).map(id => {
        return textOf(document.getElementById(id));
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    const lab = labelText(el);
    if (lab) return lab;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      if (el.placeholder && el.placeholder.trim()) return el.placeholder.trim();
      if (el.name) return el.name;
    }
    const txt = (el.innerText || el.textContent || '').trim();
    if (txt) return txt.slice(0, 200);
    if (el.value && typeof el.value === 'string') return el.value.slice(0, 200);
    // el.title is a plain string on HTML elements but an SVGAnimatedString on SVG
    // nodes; read the attribute so .trim() is always safe.
    const ttl = el.getAttribute && el.getAttribute('title');
    if (ttl && ttl.trim()) return ttl.trim();
    return '';
  }

  function roleOf(el) {
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName;
    switch (tag) {
      case 'A': return el.hasAttribute('href') ? 'link' : 'generic';
      case 'BUTTON': return 'button';
      case 'SELECT': return 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'SUMMARY': return 'button';
      case 'INPUT': {
        const t = (el.getAttribute('type') || 'text').toLowerCase();
        if (t === 'checkbox') return 'checkbox';
        if (t === 'radio') return 'radio';
        if (t === 'button' || t === 'submit' || t === 'reset' || t === 'image') return 'button';
        if (t === 'range') return 'slider';
        return 'textbox';
      }
      default:
        if (el.isContentEditable) return 'textbox';
        return 'generic';
    }
  }

  function cssPath(el) {
    if (!(el instanceof Element)) return null;
    if (el.id) return '#' + (window.CSS ? CSS.escape(el.id) : el.id);
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 4) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const sameTag = Array.prototype.filter.call(parent.children, c => c.tagName === node.tagName);
        if (sameTag.length > 1) {
          part += ':nth-of-type(' + (sameTag.indexOf(node) + 1) + ')';
        }
      }
      parts.unshift(part);
      if (node.id) { parts[0] = '#' + (window.CSS ? CSS.escape(node.id) : node.id); break; }
      node = parent;
      depth++;
    }
    return parts.join(' > ');
  }

  function currentValue(el) {
    if (el.isContentEditable) return (el.innerText || '').slice(0, 500);
    if (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) return el.checked;
    if ('value' in el && typeof el.value === 'string') return el.value.slice(0, 500);
    return undefined;
  }

  function describe(el) {
    return {
      ref: refFor(el),
      role: roleOf(el),
      name: accessibleName(el).slice(0, 160),
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute ? (el.getAttribute('type') || undefined) : undefined,
      selector: cssPath(el)
    };
  }

  function describeFull(el, vis) {
    const d = describe(el);
    d.visible = vis;
    d.enabled = isEnabled(el);
    const v = currentValue(el);
    if (v !== undefined) d.value = v;
    if (el.getAttribute && el.getAttribute('aria-expanded') != null) d.expanded = el.getAttribute('aria-expanded');
    return d;
  }

  // ---- page paint/health diagnostics ----------------------------------------
  // Distinguishes "page genuinely empty" from "page exists but is not painted"
  // (window unfocused/minimized -> Firefox defers layout, body measures 0x0) and
  // from "present but hidden via CSS" (e.g. an anti-FOUC body{display:none}).
  function pageState() {
    const out = {
      url: location.href,
      title: document.title,
      readyState: document.readyState,
      focused: document.hasFocus(),
      visibility: document.visibilityState
    };
    try {
      const body = document.body;
      const html = document.documentElement;
      const br = body ? body.getBoundingClientRect() : null;
      out.bodyW = br ? Math.round(br.width) : 0;
      out.bodyH = br ? Math.round(br.height) : 0;
      const bodyCs = body ? getComputedStyle(body) : null;
      const htmlCs = html ? getComputedStyle(html) : null;
      out.hiddenByCss = !!((bodyCs && bodyCs.display === 'none') ||
                           (htmlCs && htmlCs.display === 'none') ||
                           (bodyCs && bodyCs.visibility === 'hidden'));
      out.painted = out.bodyW > 0 && out.bodyH > 0 && !out.hiddenByCss;
      if (!out.painted) {
        out.warning = out.hiddenByCss
          ? 'Page content exists but is hidden by CSS (display:none on body/html — e.g. an anti-FOUC guard). The DOM is intact; this is not an empty page.'
          : 'Page is not laid out (document measures 0x0): its window is in the background, where Firefox defers layout. Results read from the DOM still hold: text, snapshot elements, page_act steps and page_eval; element sizes, visibility checks and screenshots may not. Stay in this background tab. Last resort, only if a step fails because an element has no size: tabs action="activate" lays the page out, but it brings the tab to the front of the user\'s window.';
      }
    } catch (e) {
      out.stateError = String((e && e.message) || e);
    }
    return out;
  }
  self.pageState = pageState;

  const INTERACTIVE_SELECTOR = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]', '[role=combobox]',
    '[role=textbox]', '[role=searchbox]', '[role=menuitem]', '[role=menuitemcheckbox]',
    '[role=tab]', '[role=switch]', '[role=option]', '[role=slider]',
    '[contenteditable=""]', '[contenteditable="true"]',
    '[tabindex]:not([tabindex="-1"])'
  ].join(', ');

  function collectIn(scope) {
    const found = Array.prototype.slice.call(scope.querySelectorAll(INTERACTIVE_SELECTOR));
    // de-dup while preserving order
    const seen = new Set();
    const out = [];
    for (const el of found) {
      if (seen.has(el)) continue;
      seen.add(el);
      out.push(el);
    }
    return out;
  }

  // A scope that was ASKED FOR but cannot be resolved must never silently widen
  // back to the whole document: the container it names (a modal, a row) may have
  // closed, and the same accessible name elsewhere on a live authenticated page
  // is a DIFFERENT element. Unresolvable scope => zero elements, always.
  function collect(root) {
    const info = resolveScopeInfo(root);
    if (info.given && !info.el) return [];
    return collectIn(info.el || document);
  }

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  function nameMatches(name, wanted) {
    if (!name) return false;
    const b = String(wanted == null ? '' : wanted).trim().toLowerCase();
    if (!b) return false; // empty target must not match everything
    const a = name.trim().toLowerCase();
    return a === b || a.indexOf(b) !== -1;
  }

  // ---- target resolution ---------------------------------------------------
  // Accepts {ref}, {selector}, {text, role?}, or the unified {target} which is
  // tried as: a live ref ("e5") -> a CSS selector -> an accessible-name text
  // match. Disambiguators: {within} (ref or selector scoping the search),
  // {nth} (0-based pick among matches), {enabled_only} (drop disabled matches).
  // Resolve a `within` scope to {given, el, invalid, why}. The three outcomes are
  // deliberately distinct: NOT GIVEN (search the document), GIVEN-AND-FOUND (search
  // that subtree), GIVEN-BUT-MISSING (search NOTHING; see collect/resolveAll). A
  // malformed selector is reported apart from an absent container: they call for
  // opposite next moves (fix the selector vs. re-snapshot), and collapsing them
  // sends the agent into a re-snapshot loop over a selector that can never parse.
  function resolveScopeInfo(within) {
    if (within === undefined || within === null) return { given: false, el: null };
    const s = String(within);
    if (!s.trim()) return { given: true, el: null, invalid: true, why: 'the scope is an empty string' };
    if (/^e\d+$/.test(s)) {
      const el = self.refs[s];
      return { given: true, el: (el && el.isConnected) ? el : null };
    }
    try {
      return { given: true, el: document.querySelector(s) };
    } catch (e) {
      return { given: true, el: null, invalid: true, why: 'invalid CSS selector: ' + ((e && e.message) || e) };
    }
  }
  function resolveScope(within) { return resolveScopeInfo(within).el; }

  function resolveAll(t) {
    t = t || {};
    // Same rule as collect(): an unresolvable `within` yields NO matches (never
    // a document-wide search); see scopeMissHint for what the caller reports.
    const info = resolveScopeInfo(t.within);
    if (info.given && !info.el) return [];
    const scope = info.el || document;
    let matched = [];

    function byText(text, role) {
      return collectIn(scope).filter(el => {
        if (role && roleOf(el) !== role) return false;
        return nameMatches(accessibleName(el), text);
      });
    }

    if (t.ref) {
      const el = self.refs[t.ref];
      matched = (el && el.isConnected) ? [el] : [];
    } else if (t.selector) {
      try { matched = Array.prototype.slice.call(scope.querySelectorAll(t.selector)); }
      catch (e) { matched = []; }
    } else if (t.target) {
      // `target` outranks `text`: a step can carry BOTH (e.g. a fill whose
      // `text` was meant as the value-alias) and must not have its resolution
      // hijacked by the accessible-name field.
      const target = String(t.target);
      if (/^e\d+$/.test(target) && self.refs[target] && self.refs[target].isConnected) {
        matched = [self.refs[target]];
      } else {
        let cssMatches = null;
        try { cssMatches = Array.prototype.slice.call(scope.querySelectorAll(target)); }
        catch (e) { cssMatches = null; }
        matched = (cssMatches && cssMatches.length > 0) ? cssMatches : byText(target, t.role);
      }
    } else if (t.text) {
      matched = byText(t.text, t.role);
    }

    // A resolved scope must CONTAIN the match. The ref paths above look elements up
    // in self.refs, which spans the whole document; without this a `within` would be
    // silently ignored for refs (the targeting mode page_snapshot hands out first),
    // so {ref of the row's Delete, within: "#confirm-modal"} would click the row.
    if (info.el) matched = matched.filter(el => info.el.contains(el));

    if (t.enabled_only || t.enabledOnly) matched = matched.filter(isEnabled);
    if (t.nth !== undefined && t.nth !== null) {
      const i = Number(t.nth);
      matched = (i >= 0 && i < matched.length) ? [matched[i]] : [];
    }
    return matched;
  }
  self.resolveAll = resolveAll;

  // Zero matches because the `within` container itself is gone. resolveAll/collect
  // refuse to widen the search to the document in that case, so the zero is a
  // deliberate refusal, not "the element disappeared"; say which, or the agent
  // draws the wrong conclusion (and a re-issue without `within` would click a
  // same-named element elsewhere on the live page).
  function scopeMissHint(args) {
    if (!args) return null;
    const info = resolveScopeInfo(args.within);
    if (!info.given || info.el) return null;
    const s = String(args.within);
    if (info.invalid) {
      return 'scope not usable: within "' + s + '" — ' + info.why + '. Nothing was searched and nothing was ' +
        'acted on. Fix the selector (this is plain CSS: jQuery extensions like :contains() and :has(...) with ' +
        'text predicates do not parse here — match by ref, id/class, or use `text` for accessible-name matching).';
    }
    const why = /^e\d+$/.test(s)
      ? 'that snapshot ref is stale — refs are cleared on navigation/reload, and a removed element no longer resolves'
      : 'no element in the current document matches that selector';
    return 'scope not found: within "' + s + '" — ' + why + '. The search was NOT widened to the whole page ' +
      '(a scoped target must never fall through to a same-named element outside its container), so nothing ' +
      'matched and nothing was acted on. Re-run page_snapshot to see the current DOM — the container may have ' +
      'closed/re-rendered — or drop `within` if you really do mean a page-wide match.';
  }
  self.scopeMissHint = scopeMissHint;

  // A target written as a snapshot ref ("e5") that no longer resolves is almost
  // always STALE: refs live on `window` for the life of a document and are wiped
  // on navigation/reload (background re-injects a fresh toolkit). Distinguish that
  // from a genuinely-wrong selector so the agent re-snapshots instead of assuming
  // the element vanished. Returns a message string, or null when neither the scope
  // nor a ref-shaped target explains the miss (so callers fall back to their
  // generic "not found"). This is the shared diagnostic channel: every method that
  // resolves a target consults it on zero matches.
  function staleRefHint(args) {
    if (!args) return null;
    const scopeMiss = scopeMissHint(args);
    if (scopeMiss) return scopeMiss; // the scope explains the miss; the target was never even searched for
    const cands = [args.ref, args.target];
    for (const c of cands) {
      if (c == null) continue;
      const s = String(c);
      if (!/^e\d+$/.test(s)) continue;
      const el = self.refs[s];
      if (!el) {
        return 'stale ref "' + s + '": snapshot refs are cleared on navigation/reload and no "' + s +
          '" exists in the current document — re-run page_snapshot for fresh refs, or target by CSS selector / visible text.';
      }
      if (!el.isConnected) {
        return 'stale ref "' + s + '": that element was removed from the page (detached after a re-render) — ' +
          're-run page_snapshot, or target by CSS selector / visible text.';
      }
      // The ref is LIVE and the container IS there: the only thing that can still zero the
      // match is resolveAll's containment filter. That is the one miss the agent can act on
      // straight away, so never let it surface as the generic "no element matched", which
      // reads as "re-snapshot" (pointless: the ref is good) or "drop the scope" (dangerous:
      // it would then hit the same-named twin the scope was there to exclude).
      const scope = resolveScopeInfo(args.within);
      if (scope.el && !scope.el.contains(el)) {
        const d = describe(el);
        return 'ref "' + s + '" is a live element (' + d.role + (d.name ? ' "' + d.name + '"' : '') +
          ') but it is NOT inside within "' + String(args.within) + '" — the scoped search therefore matched ' +
          'nothing and nothing was acted on. Either drop `within` (if that element IS the one you mean), or ' +
          'target something inside the container (re-run page_snapshot with root="' + String(args.within) +
          '" to see what is in there).';
      }
    }
    return null;
  }
  self.staleRefHint = staleRefHint;

  // Resolve a target, scroll it into view, and report its bounding box (CSS px,
  // both viewport-relative and page-absolute); the basis for an element-only
  // screenshot. Async so a couple of animation frames pass (smooth-scroll /
  // layout) before we read the box.
  self.rect = async function (args) {
    args = args || {};
    const matched = resolveAll(args);
    if (matched.length === 0) {
      return { ok: false, error: staleRefHint(args) || 'no element matches', matchCount: 0, target: targetEcho(args) };
    }
    if (matched.length > 1 && (args.nth === undefined || args.nth === null)) {
      return { ok: false, matchCount: matched.length, target: targetEcho(args),
        error: 'matched ' + matched.length + ' elements — pass a more specific selector (or nth) to pick one' };
    }
    const el = matched[0];
    try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) { /* ignore */ }
    await new Promise(function (r) {
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(function () { requestAnimationFrame(r); });
      else setTimeout(r, 32);
    });
    const r = el.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    return {
      ok: true,
      matchCount: matched.length,
      dpr: dpr,
      viewport: { w: window.innerWidth, h: window.innerHeight },
      page: { x: r.left + window.scrollX, y: r.top + window.scrollY, w: r.width, h: r.height },
      view: { x: r.left, y: r.top, w: r.width, h: r.height },
      target: describe(el)
    };
  };

  function dispatchPointerClick(el) {
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const base = { bubbles: true, cancelable: true, composed: true, view: window, clientX: cx, clientY: cy, button: 0, buttons: 1 };
    try { el.focus({ preventScroll: true }); } catch (e) { try { el.focus(); } catch (e2) { /* ignore */ } }
    const seq = ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
    for (const type of seq) {
      let ev;
      const isPointer = type.indexOf('pointer') === 0;
      try {
        ev = isPointer ? new PointerEvent(type, base) : new MouseEvent(type, base);
      } catch (e) {
        try { ev = new MouseEvent(isPointer ? 'mousedown' : type, base); } catch (e2) { ev = null; }
      }
      if (ev) el.dispatchEvent(ev);
    }
  }

  function dialogCount() {
    return document.querySelectorAll('[role=dialog], dialog[open], .modal.show, .gl-modal').length;
  }

  // The page's total element count; the cheapest honest "how much content is
  // here" primitive, and the one that turns "domChanged: true" into "+24 nodes". A
  // LIVE HTMLCollection's length, NOT querySelectorAll('*'), which would allocate a
  // full static NodeList on every read. Never throws: a measurement must not be able
  // to fail the step it is measuring.
  function elementCount() {
    try { return document.getElementsByTagName('*').length; }
    catch (e) { return undefined; }
  }

  // ---- click state confirmation --------------------------------------------
  // A click that toggled the filter and a click that landed on nothing look
  // IDENTICAL at the event layer: dispatchEvent always returns. Filter sidebars
  // are where that gap costs the most; the checkbox is wrapped in a styled
  // <label>, its <input> is hidden or not rendered until the section expands,
  // and a silent no-op means the agent reads an UNFILTERED result set and
  // reports the wrong "cheapest". So: find the control the click was aiming at,
  // read its state before AND after, and refuse to call a toggle "ok" when
  // nothing moved. (Mirrors fill()'s valueReverted contract: a control that did
  // not take the change is ok:false, not a success with a footnote.)

  const CHECKED_ROLES = {
    checkbox: 1, radio: 1, switch: 1, menuitemcheckbox: 1, menuitemradio: 1
  };
  const CHECKED_ROLE_SELECTOR =
    '[role=checkbox],[role=radio],[role=switch],[role=menuitemcheckbox],[role=menuitemradio]';

  function isNativeToggle(n) {
    return !!(n && n.tagName === 'INPUT' && (n.type === 'checkbox' || n.type === 'radio'));
  }

  // The state-bearing control behind a click target. A click almost never lands
  // ON the <input> in a modern filter UI; it lands on the <label>, on a styled
  // <span> inside it, or on a div carrying role="checkbox". Returns null when the
  // target is not toggle-shaped at all (ordinary buttons/links take no penalty),
  // or {missingFor} when a <label for=...> names an id that is not in the
  // document; the virtualized-filter shape, which can never toggle anything.
  function controlFor(el) {
    if (!el || el.nodeType !== 1) return null;
    if (isNativeToggle(el)) return { el: el, kind: 'native', via: 'self' };
    const role = el.getAttribute && el.getAttribute('role');
    if (role && CHECKED_ROLES[role] && el.getAttribute('aria-checked') != null) {
      return { el: el, kind: 'aria', via: 'self' };
    }
    const label = el.tagName === 'LABEL' ? el : (el.closest ? el.closest('label') : null);
    if (label) {
      let ctl = null;
      try { ctl = label.control || null; } catch (e) { ctl = null; }
      const forId = label.getAttribute && label.getAttribute('for');
      if (!ctl && forId) {
        try { ctl = document.getElementById(forId); } catch (e) { ctl = null; }
        if (!ctl) return { missingFor: forId, label: label };
      }
      if (!ctl) { try { ctl = label.querySelector('input[type=checkbox], input[type=radio]'); } catch (e) { ctl = null; } }
      if (isNativeToggle(ctl)) return { el: ctl, kind: 'native', via: 'label' };
      if (ctl && ctl.getAttribute && CHECKED_ROLES[ctl.getAttribute('role')] &&
          ctl.getAttribute('aria-checked') != null) {
        return { el: ctl, kind: 'aria', via: 'label' };
      }
    }
    let inner = null;
    try { inner = el.querySelectorAll('input[type=checkbox], input[type=radio]'); }
    catch (e) { inner = null; }
    if (inner && inner.length === 1) return { el: inner[0], kind: 'native', via: 'descendant' };
    let ariaAnc = null;
    try { ariaAnc = el.closest ? el.closest(CHECKED_ROLE_SELECTOR) : null; } catch (e) { ariaAnc = null; }
    if (ariaAnc && ariaAnc.getAttribute('aria-checked') != null) {
      return { el: ariaAnc, kind: 'aria', via: 'ancestor' };
    }
    return null;
  }

  function readChecked(ctl) {
    if (!ctl || !ctl.el) return undefined;
    if (ctl.kind === 'native') return !!ctl.el.checked;
    const v = ctl.el.getAttribute('aria-checked');
    return v === 'true' ? true : (v === 'false' ? false : undefined);
  }

  // The confirm poll below holds a control it resolved BEFORE the click, and a
  // framework routinely answers a click by REPLACING that node (a key change, a
  // list re-render, an innerHTML rewrite). Reading the detached original forever
  // would report a click that worked as one that changed nothing: the exact
  // false negative this whole check exists to prevent, inverted. So re-resolve
  // whenever the node has left the document; by id first (the stable handle
  // across a re-render), then by re-running resolution from the click target.
  function refreshControl(ctl, target) {
    if (!ctl || !ctl.el || ctl.el.isConnected) return ctl;
    const id = ctl.el.id;
    if (id) {
      let fresh = null;
      try { fresh = document.getElementById(id); } catch (e) { fresh = null; }
      if (fresh) {
        return { el: fresh, kind: ctl.kind, via: ctl.via, reresolved: 'id' };
      }
    }
    if (target && target.isConnected) {
      const again = controlFor(target);
      if (again && again.el) {
        again.reresolved = 'target';
        return again;
      }
    }
    return ctl; // genuinely gone; the caller reports `detached` rather than a no-op
  }

  // Why a click can land on nothing. Computed LAZILY; only once a click has
  // turned out to change nothing, so a hit-test false positive (a transparent
  // overlay that forwards clicks, a mid-animation element) can never block a
  // click that would have worked. Diagnosis only, never a gate.
  function collapsedAncestor(el) {
    let n = el && el.parentElement;
    let depth = 0;
    while (n && n.nodeType === 1 && depth < 12) {
      try {
        if (n.tagName === 'DETAILS' && !n.open) {
          return { reason: 'inside a closed <details> section', selector: cssPath(n) };
        }
        if (n.hasAttribute && n.hasAttribute('hidden')) {
          return { reason: 'inside an element carrying the [hidden] attribute', selector: cssPath(n) };
        }
        if (n.getAttribute && n.getAttribute('aria-hidden') === 'true') {
          return { reason: 'inside an aria-hidden="true" subtree', selector: cssPath(n) };
        }
        const cs = window.getComputedStyle(n);
        if (cs && cs.display === 'none') {
          return { reason: 'inside a display:none ancestor', selector: cssPath(n) };
        }
        if (cs && (cs.overflow === 'hidden' || cs.overflowY === 'hidden') &&
            n.clientHeight === 0 && n.scrollHeight > 0) {
          return {
            reason: 'inside a collapsed (height:0, overflow:hidden) ancestor — a section/accordion that is not expanded',
            selector: cssPath(n)
          };
        }
      } catch (e) { /* ignore */ }
      n = n.parentElement;
      depth++;
    }
    return null;
  }

  function reachability(el) {
    const out = {};
    let r = null;
    try { r = el.getBoundingClientRect(); } catch (e) { r = null; }
    if (r) out.rect = { w: Math.round(r.width), h: Math.round(r.height) };
    const collapsed = collapsedAncestor(el);
    if (collapsed) out.collapsedAncestor = collapsed;
    if (r && (r.width === 0 || r.height === 0)) {
      out.reachable = false;
      out.why = 'the element measures 0x0 — it is clipped or collapsed, so a click at its centre point lands nowhere';
      return out;
    }
    if (!r || typeof document.elementFromPoint !== 'function') return out; // unknown; claim nothing
    // Capability probe: a real layout engine always answers for (0,0). Without
    // one (jsdom, an unpainted window) elementFromPoint reports null/undefined
    // for EVERY point, which would turn every no-op into a bogus "clipped".
    let probe;
    try { probe = document.elementFromPoint(0, 0); } catch (e) { return out; }
    if (probe === undefined || probe === null) return out;
    let hit;
    try {
      const cx = Math.min(Math.max(r.left + r.width / 2, 0), Math.max(0, (window.innerWidth || 0) - 1));
      const cy = Math.min(Math.max(r.top + r.height / 2, 0), Math.max(0, (window.innerHeight || 0) - 1));
      hit = document.elementFromPoint(cx, cy);
    } catch (e) { return out; }
    if (hit === undefined) return out;
    if (hit === null) {
      out.reachable = false;
      out.why = 'nothing is painted at the element\'s centre point — it is scrolled out of view or clipped by an overflow:hidden ancestor';
      return out;
    }
    if (hit === el || (el.contains && el.contains(hit)) || (hit.contains && hit.contains(el))) {
      out.reachable = true;
      return out;
    }
    let cs = null;
    try { cs = window.getComputedStyle(hit); } catch (e) { /* ignore */ }
    if (cs && cs.pointerEvents === 'none') { out.reachable = true; return out; }
    out.reachable = false;
    out.occludedBy = describe(hit);
    out.why = 'another element is painted on top of it (' + roleOf(hit) + ' "' +
      accessibleName(hit).slice(0, 60) + '"), so the click landed on that instead';
    return out;
  }

  function clickNoOpMessage(ctl, res, checkedAfter) {
    let msg = 'clicked, but the control did not change state: ' +
      (ctl.kind === 'native' ? '.checked' : 'aria-checked') + ' is still ' +
      String(checkedAfter) + ' after the click settled';
    const reach = res.reachability || {};
    if (reach.collapsedAncestor) {
      msg += ' — the target is ' + reach.collapsedAncestor.reason + ' (' +
        reach.collapsedAncestor.selector + '). Expand that section first, re-run page_snapshot, then click again.';
    } else if (reach.reachable === false) {
      msg += ' — ' + reach.why + '.';
    } else if (res.structuralChange) {
      msg += '. The page DID re-render, so the site may apply this filter some other way — verify the ' +
        'result count / URL before assuming it applied.';
    } else {
      msg += ' and no DOM structure changed, so the click had no effect. The site probably binds its ' +
        'handler elsewhere or needs the section expanded first. Alternatives that often work: set the ' +
        'state directly with action="check" (value true/false), or drive the same filter through its ' +
        'URL parameter / a plain input on the page.';
    }
    return msg;
  }

  // ---- public methods ------------------------------------------------------

  self.snapshot = function (args) {
    args = args || {};
    // collect() refuses to widen an unresolvable `root` to the document, so without
    // this the snapshot would come back an honest-looking empty list and the server
    // would misdiagnose it ("page may still be loading"). Say the root is missing.
    const rootInfo = resolveScopeInfo(args.root);
    if (rootInfo.given && !rootInfo.el) {
      return {
        ok: false,
        pageState: pageState(),
        error: 'root not found: "' + String(args.root) + '" — ' +
          (rootInfo.invalid ? rootInfo.why : 'no element in the current document matches that selector') +
          '. The snapshot was NOT widened to the whole page (an empty list would have read as "nothing on the ' +
          'page"). Drop `root` to snapshot the document, or re-run without it to find the container\'s new form.'
      };
    }
    const max = args.max || 150;
    const includeHidden = !!args.includeHidden;
    const dedupe = args.dedupe !== false;

    if (args.mode === 'records') {
      // Ahead of collect(): a listing page's records have nothing to do with its
      // interactive-element scan, and that scan is the expensive part.
      return recordsSnapshot(args, pageState(), rootInfo);
    }

    const all = collect(args.root);
    const state = pageState();

    if (args.mode === 'forms') {
      return formsSnapshot(all, state, max);
    }

    const items = [];
    let considered = 0;
    const groups = new Map(); // dedupe key -> first item index
    let deduped = 0;
    for (const el of all) {
      const vis = isVisible(el);
      if (!includeHidden && !vis) continue;
      considered++;
      const d = describeFull(el, vis);
      if (dedupe) {
        // Collapse runs of same-role+name elements (e.g. 450 identical grid
        // buttons) so duplicates don't spend the element cap.
        const key = d.role + '|' + d.name + '|' + d.tag + '|' + (d.enabled ? '1' : '0');
        if (d.name && groups.has(key)) {
          const first = groups.get(key);
          first.dupCount = (first.dupCount || 1) + 1;
          if (!first.dupLastRef || true) first.dupLastRef = d.ref;
          deduped++;
          continue;
        }
        if (d.name) groups.set(key, d);
      }
      if (items.length < max) items.push(d); else break;
    }
    return {
      ok: true,
      pageState: state,
      count: items.length,
      candidates: includeHidden ? all.length : considered,
      dedupedDuplicates: deduped,
      truncated: considered > items.length + deduped,
      elements: items
    };
  };

  // Structured view of form controls: radio/checkbox groups collapsed by name
  // with options + current selection, inputs with values, buttons with state.
  function formsSnapshot(all, state, max) {
    const radioGroups = new Map();
    const checkboxGroups = new Map();
    const inputs = [];
    const selects = [];
    const buttons = [];
    const other = [];
    for (const el of all) {
      if (!isVisible(el)) continue;
      const tag = el.tagName;
      if (tag === 'INPUT' && (el.type === 'radio' || el.type === 'checkbox')) {
        const map = el.type === 'radio' ? radioGroups : checkboxGroups;
        const key = el.name || '(unnamed)';
        if (!map.has(key)) map.set(key, []);
        map.get(key).push({
          ref: refFor(el), value: el.value, label: accessibleName(el).slice(0, 80),
          checked: el.checked, enabled: isEnabled(el)
        });
      } else if (tag === 'SELECT') {
        selects.push({
          ref: refFor(el), label: accessibleName(el).slice(0, 80),
          selected: el.value,
          options: Array.prototype.slice.call(el.options, 0, 30).map(o => o.text.trim().slice(0, 60)),
          enabled: isEnabled(el)
        });
      } else if (tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable) {
        const role = roleOf(el);
        if (role === 'button') {
          buttons.push({ ref: refFor(el), label: accessibleName(el).slice(0, 80), enabled: isEnabled(el) });
        } else {
          inputs.push({
            ref: refFor(el), label: accessibleName(el).slice(0, 80),
            type: el.type || (el.isContentEditable ? 'contenteditable' : tag.toLowerCase()),
            value: (currentValue(el) === undefined ? '' : String(currentValue(el))).slice(0, 120),
            required: !!el.required, enabled: isEnabled(el)
          });
        }
      } else if (tag === 'BUTTON' || roleOf(el) === 'button') {
        buttons.push({ ref: refFor(el), label: accessibleName(el).slice(0, 80), enabled: isEnabled(el) });
      } else if (other.length < 20) {
        other.push(describe(el));
      }
    }
    const toGroups = (map) => Array.from(map.entries()).map(([name, opts]) => ({
      name: name,
      options: opts,
      selected: opts.filter(o => o.checked).map(o => o.value)
    }));
    return {
      ok: true,
      mode: 'forms',
      pageState: state,
      radioGroups: toGroups(radioGroups),
      checkboxGroups: toGroups(checkboxGroups),
      inputs: inputs.slice(0, max),
      selects: selects,
      buttons: buttons.slice(0, max),
      other: other
    };
  }

  // ---- records: structured extraction from repeated cards ------------------
  // Listing pages (product grids, marketplace search results) are the one shape
  // where the obvious move, two querySelectorAll calls zipped by index, is
  // reliably WRONG: the arrays drift the moment one card contributes a different
  // number of matches than another, and a card that embeds a variant switcher
  // carries several price nodes, so the "price" that lands next to a title can
  // belong to a different capacity entirely. Nothing here reads outside ONE card
  // element, so a field can never be sourced from a sibling; where a card is
  // genuinely ambiguous it is REPORTED as such instead of silently picked; and
  // when no repeated pattern is found with confidence the answer is a refusal,
  // not noise dressed as data.

  function reEscape(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  const CURRENCY_TOKENS = ['zł', 'PLN', 'EUR', 'USD', 'GBP', 'CHF', 'CZK', 'Kč', 'HUF', 'Ft',
    'SEK', 'NOK', 'DKK', 'UAH', 'RON', 'BGN', 'TRY', 'JPY', 'CNY', 'CAD', 'AUD',
    '€', '$', '£', '¥', '₴', '₽', '₺', '₹'];
  const CUR_ALT = CURRENCY_TOKENS.map(reEscape).join('|');
  // Thousands separators include the space family (Polish/French grouping uses
  // NBSP and NNBSP, which look identical to a normal space in a screenshot).
  const NUM_ALT = '\\d{1,3}(?:[ \\u00a0\\u202f\\u2009.,]\\d{3})*(?:[.,]\\d{1,2})?|\\d+(?:[.,]\\d{1,2})?';
  // A number is only a price when a currency sits on one side of it; a bare
  // "14" (capacity, review count, rank) must never be harvested as money.
  const PRICE_RE = new RegExp(
    '(' + CUR_ALT + ')\\s*(' + NUM_ALT + ')|(' + NUM_ALT + ')\\s*(' + CUR_ALT + ')', 'gi');

  function hasPrice(text) {
    if (!text) return false;
    PRICE_RE.lastIndex = 0;
    return PRICE_RE.test(text);
  }

  // "1 199,00" (PL) / "$1,199.00" (US) / "1.199,00" (DE) all mean the same
  // number. Decide the decimal separator from position, never from locale
  // guessing; return null rather than a wrong number when it does not parse.
  function parsePriceNumber(raw) {
    if (raw == null) return null;
    // JS \s already covers NBSP/NNBSP/thin space; the separators PL and FR
    // grouping actually use, and the ones that look like a plain space.
    const s = String(raw).replace(/\s/g, '');
    const lastComma = s.lastIndexOf(',');
    const lastDot = s.lastIndexOf('.');
    let dec = -1;
    if (lastComma >= 0 && lastDot >= 0) {
      dec = Math.max(lastComma, lastDot);           // the LAST one is the decimal point
    } else if (lastComma >= 0) {
      const tail = s.length - lastComma - 1;
      dec = (tail === 1 || tail === 2) ? lastComma : -1;   // 3 trailing digits = grouping
    } else if (lastDot >= 0) {
      const tail = s.length - lastDot - 1;
      dec = (tail === 1 || tail === 2) ? lastDot : -1;
    }
    let intPart = dec >= 0 ? s.slice(0, dec) : s;
    const fracPart = dec >= 0 ? s.slice(dec + 1) : '';
    intPart = intPart.replace(/[.,]/g, '');
    if (!/^\d+$/.test(intPart)) return null;
    if (fracPart && !/^\d+$/.test(fracPart)) return null;
    const n = Number(intPart + (fracPart ? '.' + fracPart : ''));
    return isFinite(n) ? n : null;
  }

  // A struck-through price is the "was" price, not a competing variant; it must
  // not make an otherwise-unambiguous card look ambiguous. Stops at the card so a
  // styled container above it can never mark every price as struck.
  function isStruck(el, stopAt) {
    let n = el;
    let depth = 0;
    while (n && n.nodeType === 1 && depth < 6) {
      const tag = n.tagName;
      if (tag === 'DEL' || tag === 'S' || tag === 'STRIKE') return true;
      try {
        const cs = window.getComputedStyle(n);
        const td = String((cs && (cs.textDecorationLine || cs.textDecoration)) || '');
        if (td.indexOf('line-through') !== -1) return true;
      } catch (e) { /* ignore */ }
      if (n === stopAt) break;
      n = n.parentElement;
      depth++;
    }
    return false;
  }

  const PRICE_TEXT_MAX = 48;

  // The OUTERMOST short price-bearing elements in a card. Outermost (not
  // innermost) because sites routinely split one price across spans
  // ("<span>1 199</span><span>,00 zł</span>"), so the parent is the unit that
  // reads correctly; the length cap is what stops the whole card qualifying.
  function priceNodesIn(card) {
    const kept = [];
    let els;
    try { els = card.querySelectorAll('*'); } catch (e) { return kept; }
    const limit = Math.min(els.length, 400);
    for (let i = 0; i < limit; i++) {
      const el = els[i];
      const t = textOf(el);
      if (!t || t.length > PRICE_TEXT_MAX) continue;
      if (!hasPrice(t)) continue;
      let covered = false;
      for (let j = 0; j < kept.length; j++) {
        if (kept[j].contains(el)) { covered = true; break; }
      }
      if (covered) continue;
      kept.push(el);
    }
    return kept;
  }

  function priceUnitsFrom(node, card) {
    const units = [];
    // When a node holds several price-bearing children (the classic
    // "<del>was</del><span>now</span>" pair) treat each child as its own unit so
    // struck-through classification is per price, not per block.
    const kids = node.children || [];
    const matching = [];
    for (let i = 0; i < kids.length; i++) {
      const t = textOf(kids[i]);
      if (t && t.length <= PRICE_TEXT_MAX && hasPrice(t)) matching.push(kids[i]);
    }
    const sources = matching.length >= 2 ? matching : [node];
    for (let s = 0; s < sources.length; s++) {
      const src = sources[s];
      const t = textOf(src);
      const struck = isStruck(src, card);
      const found = pricesIn(t, 4);
      // Labels are worked out after the scan: they run PRICE_RE again, and a nested exec
      // on the shared global regex would move the scan's lastIndex.
      const labels = found.length === 1 ? [lonePriceLabel(src, card)]
        : (holdsHeading(src) ? [] : labelsBetween(t, found));
      for (let f = 0; f < found.length; f++) {
        const m = found[f];
        const cur = m[1] || m[4] || null;
        const value = parsePriceNumber(m[2] || m[3]);
        const u = { text: m[0].trim() };
        if (value !== null) u.value = value;
        if (cur) u.currency = cur;
        if (struck) u.struck = true;
        if (labels[f]) u.label = labels[f];
        units.push(u);
      }
    }
    return units;
  }

  // ---- price labels -------------------------------------------------------------
  // The words printed beside a price inside its card ("z dostawą", "Najniższa cena:",
  // "od", a variant name), so a card with several live prices says which is which. On a
  // marketplace listing where every card shows a price with and without delivery, a bare
  // "AMBIGUOUS" on 62 of 65 rows sent every model in the eval of 2026-10-08 off to scrape
  // the grid with page_eval.
  const PRICE_LABEL_MAX = 40;
  const LABEL_TEXT_MAX = 90;

  function holdsHeading(node) {
    try { return !!node.querySelector('h1,h2,h3,h4,h5,h6'); } catch (e) { return true; }
  }

  function pricesIn(text, limit) {
    const out = [];
    PRICE_RE.lastIndex = 0;
    let m;
    while ((m = PRICE_RE.exec(text)) !== null && out.length < (limit || 8)) {
      out.push(m);
      if (m[0].length === 0) PRICE_RE.lastIndex++; // never spin on a zero-width match
    }
    PRICE_RE.lastIndex = 0;
    return out;
  }

  function tidyLabel(s) {
    const t = String(s || '').replace(/\s+/g, ' ')
      .replace(/^[\s:|•·–—,;\/()\[\]-]+|[\s:|•·–—,;\/()\[\]-]+$/g, '').trim();
    if (!t) return '';
    return t.length > PRICE_LABEL_MAX ? t.slice(0, PRICE_LABEL_MAX).trim() + '…' : t;
  }

  // A price alone in its element: the words around it there, else in the nearest of two
  // ancestors whose text still holds this one price and stays short. A wider container
  // would hand this price the words that belong to its neighbour, and one that holds the
  // card's heading would label it with the product's title.
  function lonePriceLabel(src, card) {
    let node = src;
    for (let depth = 0; node && depth < 3; depth++) {
      if (holdsHeading(node)) break;
      const t = textOf(node).replace(/\s+/g, ' ');
      if (t.length > LABEL_TEXT_MAX) break;
      const found = pricesIn(t, 2);
      if (found.length !== 1) break;
      const m = found[0];
      const label = tidyLabel(t.slice(0, m.index) + ' ' + t.slice(m.index + m[0].length));
      if (label) return label;
      if (node === card) break;
      node = node.parentElement;
    }
    return '';
  }

  // Several prices in one run of text. Words before the first price and after the last
  // belong to that price. Words between two prices belong to the one before them, unless
  // they end in a colon ("Z dostawą: 47,48 zł"), which introduces the one after.
  function labelsBetween(text, found) {
    const parts = found.map(function () { return []; });
    let prev = 0;
    for (let k = 0; k <= found.length; k++) {
      const end = k < found.length ? found[k].index : text.length;
      const seg = text.slice(prev, end);
      const label = tidyLabel(seg);
      if (label) {
        if (k === 0) parts[0].push(label);
        else if (k === found.length) parts[found.length - 1].push(label);
        else if (/:\s*$/.test(seg)) parts[k].push(label);
        else parts[k - 1].push(label);
      }
      if (k < found.length) prev = found[k].index + found[k][0].length;
    }
    return parts.map(function (p) { return tidyLabel(p.join(' ')); });
  }

  const REDIRECT_PARAMS = ['redirect', 'url', 'u', 'target', 'dest', 'destination'];

  // Sponsored/tracked listings hand out a click-counter URL with the real
  // product URL buried in a query parameter. Passing that through unchanged
  // makes every provenance link in a report point at the tracker.
  // The wrapped value is absolute as often as it is site-root-relative, so both
  // are accepted (and resolved); anything else, a bare id, a flag, or a fragment,
  // is left alone, because inventing a link is worse than reporting the wrapper.
  function unwrapRedirect(href) {
    try {
      const u = new URL(href, location.href);
      for (let i = 0; i < REDIRECT_PARAMS.length; i++) {
        const v = u.searchParams.get(REDIRECT_PARAMS[i]);
        if (!v) continue;
        if (!/^(https?:)?\/\//i.test(v) && v.charAt(0) !== '/') continue;
        try {
          return { href: new URL(v, location.href).href, unwrappedFrom: href };
        } catch (e) { /* unparseable target; keep the wrapper */ }
      }
    } catch (e) { /* not a parseable URL; pass through */ }
    return { href: href };
  }

  function bestLinkIn(card) {
    let links;
    try { links = card.querySelectorAll('a[href]'); } catch (e) { return null; }
    let best = null;
    const limit = Math.min(links.length, 40);
    for (let i = 0; i < limit; i++) {
      const a = links[i];
      const href = a.getAttribute('href');
      if (!href || /^(#|javascript:|mailto:|tel:)/i.test(href)) continue;
      const t = textOf(a);
      let score = Math.min(t.length, 120);
      try { if (a.querySelector('h1,h2,h3,h4,h5,h6')) score += 200; } catch (e) { /* ignore */ }
      try { if (a.closest('h1,h2,h3,h4,h5,h6')) score += 200; } catch (e) { /* ignore */ }
      if (a.getAttribute('aria-label') || a.getAttribute('title')) score += 20;
      if (!best || score > best.score) {
        best = { a: a, score: score, href: href,
                 text: t || a.getAttribute('aria-label') || a.getAttribute('title') || '' };
      }
    }
    return best;
  }

  function cardTitle(card, link) {
    let t = '';
    try {
      const h = card.querySelector('h1,h2,h3,h4,h5,h6');
      if (h) t = textOf(h);
    } catch (e) { /* ignore */ }
    if (!t && link) t = link.text || '';
    if (!t) {
      try {
        const named = card.querySelector('[itemprop=name],[data-testid*=title],[data-testid*=name],[class*=title],[class*=name]');
        if (named) t = textOf(named);
      } catch (e) { /* ignore */ }
    }
    return t ? t.replace(/\s+/g, ' ').trim().slice(0, 200) : '';
  }

  function variantControlsIn(card) {
    try {
      return card.querySelectorAll(
        'select, input[type=radio], [role=radio], [role=option], [role=listbox], [data-variant]'
      ).length;
    } catch (e) { return 0; }
  }

  function buildRecord(card, index) {
    const rec = { index: index, ref: refFor(card) };
    const link = bestLinkIn(card);
    if (link) {
      let abs = link.href;
      try { abs = new URL(link.href, location.href).href; } catch (e) { /* keep raw */ }
      const un = unwrapRedirect(abs);
      rec.link = un.href;
      if (un.unwrappedFrom) rec.linkUnwrappedFrom = un.unwrappedFrom;
    }
    const title = cardTitle(card, link);
    if (title) rec.title = title;

    const nodes = priceNodesIn(card);
    const units = [];
    for (let i = 0; i < nodes.length; i++) {
      const us = priceUnitsFrom(nodes[i], card);
      for (let j = 0; j < us.length; j++) units.push(us[j]);
    }
    const seen = {};
    const distinct = [];
    for (let i = 0; i < units.length; i++) {
      const u = units[i];
      const key = (u.struck ? 'S:' : '') + (u.value !== undefined ? String(u.value) : u.text);
      if (seen[key]) continue;
      seen[key] = 1;
      distinct.push(u);
    }
    const live = distinct.filter(function (u) { return !u.struck; });
    const struck = distinct.filter(function (u) { return !!u.struck; });
    if (distinct.length) rec.prices = distinct;
    if (struck.length) rec.priceWas = struck[0];
    const variants = variantControlsIn(card);
    if (variants) rec.variantControls = variants;

    if (live.length === 1) {
      rec.price = live[0];
    } else if (live.length > 1) {
      rec.price = null;
      rec.ambiguous = true;
      rec.ambiguousReason = live.length + ' different prices sit inside this one card' +
        (variants ? ' and it embeds a variant selector (' + variants + ' control' + (variants === 1 ? '' : 's') +
          '), so which price applies depends on the selected variant' : '') +
        ' — no single price can be attributed to this title. ' +
        (rec.link ? 'Open its own page (' + rec.link + ') to read the real one.'
                  : 'Open the item page to read the real one.');
    } else {
      rec.price = null;
    }

    try {
      const img = card.querySelector('img');
      if (img) {
        let src = img.getAttribute('src') || img.getAttribute('data-src') || '';
        if (!src) {
          const ss = img.getAttribute('srcset') || '';
          src = ((ss.split(',')[0] || '').trim().split(/\s+/)[0]) || '';
        }
        if (src && !/^data:/i.test(src)) {
          let abs = src;
          try { abs = new URL(src, location.href).href; } catch (e) { /* keep raw */ }
          rec.image = { src: abs.slice(0, 300) };
          const alt = (img.getAttribute('alt') || '').trim();
          if (alt) rec.image.alt = alt.slice(0, 120);
        }
      }
    } catch (e) { /* ignore */ }

    rec.text = textOf(card).replace(/\s+/g, ' ').slice(0, 200);
    return rec;
  }

  function normClassKey(el) {
    const cls = el.classList ? Array.prototype.slice.call(el.classList) : [];
    return el.tagName + '|' + cls.slice(0, 6).sort().join('.');
  }

  function childSigKey(el) {
    const kids = el.children || [];
    const tags = [];
    for (let i = 0; i < kids.length && i < 8; i++) tags.push(kids[i].tagName);
    return el.tagName + '#' + tags.join(',');
  }

  // textContent, not innerText: scoring touches thousands of nodes and innerText
  // forces a reflow on every one of them.
  function rawText(el) {
    try { return (el.textContent || '').trim(); } catch (e) { return ''; }
  }

  function scoreGroup(items) {
    const n = items.length;
    const sampleN = Math.min(n, 12);
    let withLink = 0;
    let withPrice = 0;
    let totalText = 0;
    for (let i = 0; i < sampleN; i++) {
      const it = items[i];
      const t = rawText(it);
      totalText += t.length;
      try { if (it.querySelector('a[href]')) withLink++; } catch (e) { /* ignore */ }
      if (hasPrice(t)) withPrice++;
    }
    const avgText = totalText / sampleN;
    if (avgText < 12) return { reject: 'items carry almost no text (avg ' + Math.round(avgText) + ' chars) — looks like nav/pagination chrome, not listings' };
    const linkRatio = withLink / sampleN;
    const priceRatio = withPrice / sampleN;
    if (linkRatio < 0.5 && priceRatio < 0.5) {
      return { reject: 'most items have neither a link nor a price (' + Math.round(linkRatio * 100) + '% linked, ' + Math.round(priceRatio * 100) + '% priced)' };
    }
    // What makes a group a LISTING is money, not link count. A footer link matrix
    // out-counts a product grid several times over, so weighting mostly by size
    // (n^1.4, links and prices interchangeable) handed the footer the win;
    // arithmetic worked through on the real shapes, then pinned by
    // footer-link-matrix.html. Hence: a softer size exponent, and prices as a
    // separate MULTIPLIER rather than an alternative to links.
    return {
      score: Math.pow(n, 1.2) * Math.min(avgText, 400) *
             (0.3 + 0.7 * linkRatio) * (1 + 2 * priceRatio),
      avgText: Math.round(avgText),
      linkRatio: linkRatio,
      priceRatio: priceRatio
    };
  }

  function detectItemGroups(rootEl) {
    let desc;
    try { desc = rootEl.querySelectorAll('*'); } catch (e) { return { candidates: [], rejected: [] }; }
    // The ROOT itself is a candidate container: with root="#results" the cards are
    // usually its direct children, and querySelectorAll('*') never returns the node
    // it was called on; omitting it made a correctly-scoped call find nothing.
    const nodes = [rootEl];
    const limit = Math.min(desc.length, 20000);
    for (let i = 0; i < limit; i++) nodes.push(desc[i]);
    const groupsFound = [];
    for (let i = 0; i < nodes.length; i++) {
      const el = nodes[i];
      const kids = el.children;
      if (!kids || kids.length < 3 || kids.length > 400) continue;
      // Two independent groupings: by tag+class (the common case) and by
      // tag+child-tag-signature (sites that mint a unique class per card).
      const keyers = [normClassKey, childSigKey];
      for (let k = 0; k < keyers.length; k++) {
        const buckets = {};
        for (let j = 0; j < kids.length; j++) {
          const key = keyers[k](kids[j]);
          if (!buckets[key]) buckets[key] = [];
          buckets[key].push(kids[j]);
        }
        let best = null;
        for (const key in buckets) {
          if (!Object.prototype.hasOwnProperty.call(buckets, key)) continue;
          if (!best || buckets[key].length > best.length) best = buckets[key];
        }
        // The two keyers usually agree; keep one entry per distinct group so the
        // runner-up reported to the agent is a genuine alternative, not a twin.
        if (best && best.length >= 3) {
          let dup = false;
          for (let d = 0; d < groupsFound.length; d++) {
            const g = groupsFound[d];
            if (g.container === el && g.items.length === best.length && g.items[0] === best[0]) {
              dup = true;
              break;
            }
          }
          if (!dup) groupsFound.push({ container: el, items: best });
        }
      }
    }
    // Score only the most promising groups; scoring walks each item's subtree.
    groupsFound.sort(function (a, b) { return b.items.length - a.items.length; });
    const candidates = [];
    const rejected = [];
    const considered = Math.min(groupsFound.length, 60);
    for (let i = 0; i < considered; i++) {
      const g = groupsFound[i];
      const s = scoreGroup(g.items);
      if (s.reject) {
        if (rejected.length < 3) {
          rejected.push({ selector: cssPath(g.container), count: g.items.length, why: s.reject });
        }
        continue;
      }
      candidates.push({ container: g.container, items: g.items, score: s.score,
                        avgText: s.avgText, linkRatio: s.linkRatio, priceRatio: s.priceRatio });
    }
    candidates.sort(function (a, b) { return b.score - a.score; });
    return { candidates: candidates, rejected: rejected, groupsSeen: groupsFound.length };
  }

  function guessItemSelector(items) {
    const first = items[0];
    if (!first) return null;
    const cls = first.classList ? Array.prototype.slice.call(first.classList) : [];
    const shared = cls.filter(function (c) {
      if (!c || /^\d/.test(c)) return false;
      for (let i = 0; i < items.length; i++) {
        if (!items[i].classList || !items[i].classList.contains(c)) return false;
      }
      return true;
    }).slice(0, 3);
    const tag = first.tagName.toLowerCase();
    if (shared.length) {
      return tag + shared.map(function (c) {
        return '.' + (window.CSS ? CSS.escape(c) : c);
      }).join('');
    }
    const parent = cssPath(first.parentElement);
    return parent ? parent + ' > ' + tag : tag;
  }

  function recordsSnapshot(args, state, rootInfo) {
    const scopeEl = rootInfo.el || null;
    const searchRoot = scopeEl || document.body || document.documentElement;
    if (!searchRoot) {
      return { ok: false, mode: 'records', pageState: state, error: 'the document has no body to search' };
    }
    const cap = Math.min(Math.max(Number(args.max) || 100, 1), 300);
    const wanted = args.item_selector || args.itemSelector || null;
    let items = null;
    let detection = null;

    if (wanted) {
      let list;
      try {
        list = Array.prototype.slice.call((scopeEl || document).querySelectorAll(wanted));
      } catch (e) {
        return { ok: false, mode: 'records', pageState: state,
          error: 'invalid item_selector "' + wanted + '": ' + ((e && e.message) || e) +
            ' (plain CSS only — :contains()/:has() with text predicates do not parse here)' };
      }
      if (list.length === 0) {
        return { ok: false, mode: 'records', pageState: state,
          error: 'item_selector "' + wanted + '" matched no elements' +
            (scopeEl ? ' inside root "' + String(args.root) + '"' : '') +
            '. Drop item_selector to let the repeated-card pattern be detected automatically, ' +
            'or re-run page_snapshot to see the current DOM.' };
      }
      items = list;
      detection = { method: 'item_selector', itemSelector: wanted };
    } else {
      const found = detectItemGroups(searchRoot);
      if (found.candidates.length === 0) {
        let msg = 'no repeated-card pattern could be identified with confidence on this page';
        if (found.rejected.length) {
          msg += '. The closest repeated groups were rejected: ' +
            found.rejected.map(function (r) {
              return r.count + '× under ' + r.selector + ' (' + r.why + ')';
            }).join('; ');
        }
        msg += '. Rather than return a guess as data: pass item_selector="<css matching ONE card>" ' +
          '(page_snapshot or page_eval will show you the card class), or root="<container>" to scope ' +
          'the search to the listing grid.';
        return { ok: false, mode: 'records', pageState: state, error: msg,
          rejectedCandidates: found.rejected, groupsSeen: found.groupsSeen };
      }
      const best = found.candidates[0];
      items = best.items;
      detection = {
        method: 'auto',
        score: Math.round(best.score),
        candidatesConsidered: found.candidates.length,
        containerSelector: cssPath(best.container),
        itemSelector: guessItemSelector(best.items),
        avgTextLength: best.avgText,
        linkedShare: Math.round(best.linkRatio * 100) / 100,
        pricedShare: Math.round(best.priceRatio * 100) / 100
      };
      if (found.candidates.length > 1) {
        detection.runnerUp = {
          count: found.candidates[1].items.length,
          selector: guessItemSelector(found.candidates[1].items)
        };
      }
    }

    const records = [];
    for (let i = 0; i < items.length && records.length < cap; i++) {
      records.push(buildRecord(items[i], i));
    }
    const ambiguous = records.filter(function (r) { return r.ambiguous; }).length;
    const priced = records.filter(function (r) { return r.price; }).length;
    // A short, price-free repeated group IS a real pattern; a related-posts list,
    // a footer nav, a breadcrumb trail all qualify, and returning its rows is
    // honest. But on an article page it is almost certainly not the listing the
    // caller meant, so say the detection is weak instead of letting a confident-
    // looking table stand in for one. (Only for AUTO detection: an explicit
    // item_selector is the caller's own claim about what a card is.)
    if (detection.method === 'auto' && priced === 0 && records.length < 8) {
      detection.lowConfidence =
        'these ' + records.length + ' rows carry no prices, and there are few of them — this ' +
        'may be a related-links / navigation list rather than the listing you meant. Pin the ' +
        'real grid with item_selector="<css for one card>" or root="<container>" if so.';
    }
    return {
      ok: true,
      mode: 'records',
      pageState: state,
      detection: detection,
      count: records.length,
      totalItems: items.length,
      truncated: items.length > records.length,
      ambiguousCount: ambiguous,
      withPrice: priced,
      records: records
    };
  }

  self.click = async function (args) {
    args = args || {};
    const matched = resolveAll(args);
    if (matched.length === 0) {
      const state = pageState();
      const res = { ok: false, error: staleRefHint(args) || 'No element matched', target: targetEcho(args) };
      if (!state.painted) { res.pageState = state; }
      return res;
    }
    const visible = matched.filter(isVisible);
    const actionable = visible.filter(isEnabled);
    const target = actionable[0] || visible[0] || matched[0];
    const vis = isVisible(target);
    const enabled = isEnabled(target);
    if (!vis) {
      const state = pageState();
      const res = { ok: false, error: state.painted ? 'Element is not visible' : 'Element has no box because the page is not painted', target: describe(target), candidates: matched.length };
      if (!state.painted) res.pageState = state;
      return res;
    }
    if (!enabled) {
      return { ok: false, error: 'Element is disabled', target: describe(target), candidates: matched.length };
    }
    const before = { url: location.href, dialogs: dialogCount(), active: document.activeElement, expanded: target.getAttribute && target.getAttribute('aria-expanded') };
    // What state-bearing control is this click aiming at, and where is it now?
    let ctl = controlFor(target);
    const checkedBefore = (ctl && ctl.el) ? readChecked(ctl) : undefined;
    let mutations = 0;
    let childListMutations = 0;
    let attrMutations = 0;
    let nodesAdded = 0;
    let nodesRemoved = 0;
    let checkedAfter = checkedBefore;
    // A mutation RECORD count cannot tell "8 new rows arrived" from "the same
    // 40 rows were re-rendered". Count the element nodes that actually entered and
    // left, so a net delta of 0 against a high added/removed pair reads as a
    // RE-RENDER rather than as new content.
    function countElements(nodeList) {
      let n = 0;
      for (let i = 0; i < nodeList.length; i++) {
        if (nodeList[i] && nodeList[i].nodeType === 1) n++;
      }
      return n;
    }
    const mo = new MutationObserver(list => {
      mutations += list.length;
      for (const m of list) {
        if (m.type === 'childList') {
          childListMutations++;
          nodesAdded += countElements(m.addedNodes);
          nodesRemoved += countElements(m.removedNodes);
        } else attrMutations++;
      }
    });
    mo.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
    try {
      try { target.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) { /* ignore */ }
      dispatchPointerClick(target);
      await sleep(70);
      // A framework-driven toggle commits on its own tick, and a controlled one
      // can re-render a beat later (the live evidence: .checked still read false
      // in the same synchronous tick in which the URL had ALREADY gained the
      // filter). Read it back a few times and stop the moment it moves. Only a
      // toggle-shaped target pays this latency.
      if (ctl && ctl.el) {
        ctl = refreshControl(ctl, target);
        checkedAfter = readChecked(ctl);
        if (checkedAfter === checkedBefore) {
          for (const d of [80, 120, 200]) {
            await sleep(d);
            ctl = refreshControl(ctl, target);   // the re-render can land on any tick
            checkedAfter = readChecked(ctl);
            if (checkedAfter !== checkedBefore) break;
          }
        }
      }
    } finally {
      mo.disconnect(); // never leak the observer, even if dispatch throws
    }
    const after = { url: location.href, dialogs: dialogCount(), active: document.activeElement, expanded: target.getAttribute && target.getAttribute('aria-expanded') };
    const urlChanged = before.url !== after.url;
    const dialogOpened = after.dialogs > before.dialogs;
    const expandedChanged = before.expanded !== after.expanded;
    const res = {
      ok: true,
      clicked: true,
      target: describe(target),
      visible: vis,
      enabled: enabled,
      ambiguous: actionable.length > 1,
      candidates: actionable.length || visible.length,
      domChanged: mutations > 0 || urlChanged || before.dialogs !== after.dialogs || before.active !== after.active,
      urlChanged: urlChanged,
      dialogOpened: dialogOpened,
      expandedChanged: expandedChanged,
      mutations: mutations,
      // Attribute-only churn is what a focus ring / :hover class produces, so a
      // bare mutation count says "something happened" even for a click that did
      // nothing. childList (or a URL/dialog change) is the signal that the page
      // actually re-rendered.
      structuralChange: childListMutations > 0 || urlChanged || dialogOpened,
      mutationBreakdown: { childList: childListMutations, attributes: attrMutations },
      nodesAdded: nodesAdded,
      nodesRemoved: nodesRemoved
    };
    // Anything OTHER than the toggle that visibly happened; enough to say the
    // click did land, even if the control's own state reads unchanged.
    const somethingElseHappened = urlChanged || dialogOpened || expandedChanged;

    if (ctl && ctl.el) {
      const detached = !ctl.el.isConnected;
      res.control = {
        ref: refFor(ctl.el),
        kind: ctl.kind,
        via: ctl.via,
        role: roleOf(ctl.el),
        name: accessibleName(ctl.el).slice(0, 80),
        checkedBefore: checkedBefore,
        checked: detached ? undefined : checkedAfter,
        checkedChanged: !detached && checkedAfter !== checkedBefore
      };
      if (ctl.reresolved) res.control.reresolved = ctl.reresolved;
      if (detached) {
        // The control left the document and could not be re-resolved. That IS a
        // re-render, i.e. the click did something; reporting "the toggle never
        // moved" would be a confident lie about a node that no longer exists.
        res.control.detached = true;
        res.controlDetached = true;
      }
      // `self` and `label` are EXPLICIT associations (the element is the toggle, or
      // HTML says it labels it), so a re-render elsewhere is no excuse; that is
      // precisely the observed filter-sidebar failure: the page repaints, the
      // checkbox does not move, nothing is filtered. `descendant`/`ancestor` are
      // INFERRED (a row that happens to contain one checkbox), so a structural
      // change there is decent evidence the click did the thing it was aimed at.
      const inferred = ctl.via === 'descendant' || ctl.via === 'ancestor';
      if (!detached && checkedAfter !== undefined && checkedAfter === checkedBefore &&
          !somethingElseHappened && !(inferred && res.structuralChange)) {
        res.ok = false;
        res.checkedUnchanged = true;
        res.reachability = reachability(target);
        res.error = clickNoOpMessage(ctl, res, checkedAfter);
      }
    } else if (ctl && ctl.missingFor) {
      // A <label for="x"> whose x is not in the document can never toggle
      // anything, however cleanly the click dispatches. This is the virtualized
      // filter-sidebar shape: the inputs are rendered only once their section
      // expands, so every click on the visible labels is a guaranteed no-op.
      res.labelForMissing = ctl.missingFor;
      if (!somethingElseHappened && !res.structuralChange) {
        res.ok = false;
        res.reachability = reachability(target);
        res.error = 'clicked, but nothing changed — this <label> points at for="' + ctl.missingFor +
          '" and no element with that id exists in the document, so there was no control to toggle. ' +
          'Filter sidebars commonly render their inputs only once the section is expanded: expand it ' +
          '(click the section header), re-run page_snapshot, then target the control itself.';
      }
    }
    if (res.ok && !res.domChanged) {
      // Nothing observable at all. Say WHY it could not have worked, if we can.
      const reach = reachability(target);
      if (reach.reachable === false || reach.collapsedAncestor) {
        res.reachability = reach;
        res.noEffectHint = reach.collapsedAncestor
          ? 'the target is ' + reach.collapsedAncestor.reason + ' (' + reach.collapsedAncestor.selector + ')'
          : reach.why;
      }
    }
    return res;
  };

  function targetEcho(args) {
    const t = {};
    for (const k of ['target', 'ref', 'selector', 'text', 'role', 'nth', 'within']) {
      if (args[k] !== undefined && args[k] !== null) t[k] = args[k];
    }
    return t;
  }

  function setNativeValue(el, value) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
  }

  // Read the value back AFTER the framework's reactive flush: a controlled
  // component (React/Vue/Vueform "sync" mode) that ignores synthetic edits
  // re-renders its bound value on the next tick, so a read taken synchronously
  // reports a value that is about to be thrown away. Only an exact revert to
  // the pre-write value fails; a transformed value (formatter, mask,
  // uppercase) is a commit, just not verbatim. Never report success on a
  // no-op write.
  async function confirmValueTook(el, before, wanted) {
    await sleep(80);
    const after = currentValue(el);
    if (after === undefined) return { took: true, after: after }; // element exposes no readable value; can't verify
    if (after !== wanted && after === before) {
      return {
        took: false, after: after,
        error: 'value did not take — the field re-rendered its previous value ' +
          JSON.stringify(before) + ' right after the synthetic edit. This is a ' +
          'framework-controlled input that ignores untrusted events (MV2 cannot ' +
          'forge trusted ones). Try driving it via its real UI (click the option, ' +
          'press keys), or set the app\'s own model via page_eval world="main".'
      };
    }
    return { took: true, after: after };
  }

  async function fillOne(f) {
    const el = resolveAll(f)[0];
    if (!el) return { target: targetEcho(f), ok: false, error: staleRefHint(f) || 'not found' };
    if (!isVisible(el)) return { target: describe(el), ok: false, error: 'not visible' };
    if (!isEnabled(el)) return { target: describe(el), ok: false, error: 'disabled' };
    if (el.tagName === 'SELECT') {
      // route to select so fill "just works" on dropdowns
      return self.select({ ref: refFor(el), value: f.value, label: f.value });
    }
    if (f.value === undefined || f.value === null) {
      return { target: describe(el), ok: false, error: 'no value provided (checkboxes need true/false)' };
    }
    const before = currentValue(el);
    let wanted;
    try {
      try { el.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
      if (el.isContentEditable) {
        wanted = String(f.value);
        el.textContent = wanted;
        el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true }));
      } else if (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) {
        wanted = f.value === true || f.value === 'true' || f.value === '1' || f.value === 1;
        if (el.checked !== wanted) {
          el.checked = wanted;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        }
      } else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
        wanted = String(f.value);
        // native setter defeats React/Vue's overridden value setter
        setNativeValue(el, wanted);
        el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
        el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
      } else {
        wanted = String(f.value);
        // some other element exposing a value property; plain assignment
        try { el.value = wanted; } catch (e) { /* ignore */ }
        el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
        el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
      }
      if (f.submit) {
        const ko = { bubbles: true, cancelable: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 };
        el.dispatchEvent(new KeyboardEvent('keydown', ko));
        el.dispatchEvent(new KeyboardEvent('keyup', ko));
      }
      try { el.blur(); } catch (e) { /* ignore */ }
      const check = await confirmValueTook(el, before, wanted);
      if (!check.took) {
        return { target: describe(el), ok: false, valueReverted: true, value: check.after, error: check.error };
      }
      return { target: describe(el), ok: true, confirmedValue: check.after };
    } catch (e) {
      return { target: describe(el), ok: false, error: String((e && e.message) || e) };
    }
  }

  self.fill = async function (args) {
    args = args || {};
    // A batch fill's top-level `within` scopes every field it doesn't override;
    // otherwise each field would resolve document-wide and the scope would be
    // silently dropped for the whole batch. `within: null` on a field is "no scope
    // given" everywhere else (resolveScopeInfo treats null and undefined alike), so it
    // inherits too; a serializer that writes nulls must not widen the field to the page.
    const scoped = (v) => v !== undefined && v !== null;
    const fields = (args.fields || [args]).map(f =>
      (scoped(args.within) && f && !scoped(f.within) && f !== args)
        ? Object.assign({}, f, { within: args.within })
        : f);
    const results = [];
    for (const f of fields) results.push(await fillOne(f)); // in order; later fields may depend on earlier ones
    if (results.length === 1) return results[0];
    return {
      ok: results.every(r => r.ok),
      filled: results.filter(r => r.ok).length,
      failed: results.filter(r => !r.ok).length,
      results: results
    };
  };

  self.select = async function (args) {
    args = args || {};
    const el = resolveAll(args)[0];
    if (!el) return { ok: false, error: staleRefHint(args) || 'not found', target: targetEcho(args) };
    if (el.tagName !== 'SELECT') return { ok: false, error: 'target is not a <select> (for custom dropdowns: click to open, then click the option)', target: describe(el) };
    if (!isEnabled(el)) return { ok: false, error: 'disabled', target: describe(el) };
    const before = currentValue(el);
    let chosen = null;
    if (args.value !== undefined && args.value !== null) {
      el.value = String(args.value);
      if (el.value === String(args.value)) chosen = el.value;
    }
    // fall back to matching the visible label; with args.value when no
    // explicit label was given, so select "just works" with either
    const labelWanted = (args.label !== undefined && args.label !== null) ? args.label : args.value;
    if (chosen === null && labelWanted !== undefined && labelWanted !== null) {
      const opt = Array.prototype.slice.call(el.options).find(o =>
        o.text.trim() === String(labelWanted) || o.text.trim().toLowerCase().indexOf(String(labelWanted).toLowerCase()) !== -1);
      if (opt) { el.value = opt.value; chosen = opt.value; }
    }
    if (chosen === null) return { ok: false, error: 'option not found', target: describe(el), available: Array.prototype.slice.call(el.options).map(o => o.text.trim().slice(0, 80)).slice(0, 30) };
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    const check = await confirmValueTook(el, before, chosen);
    if (!check.took) {
      return { ok: false, valueReverted: true, target: describe(el), value: check.after, error: check.error };
    }
    const sel = el.options[el.selectedIndex];
    return { ok: true, target: describe(el), selectedValue: el.value, selectedLabel: sel ? sel.text.trim() : null };
  };

  self.press = function (args) {
    args = args || {};
    const key = args.key || args.value;
    if (!key) return { ok: false, error: 'no key provided (e.g. "Enter", "Escape", "ArrowDown", "a")' };
    // press is the one action that runs with NO target (it keys whatever has focus),
    // so it never reaches resolveAll's scope refusal on its own: check the scope here
    // or "press Enter inside the login modal" would, once that modal is gone, submit
    // whichever form happens to hold focus on the live page.
    const scopeErr = scopeMissHint(args);
    if (scopeErr) return { ok: false, error: scopeErr, target: targetEcho(args) };
    let el = document.activeElement || document.body;
    if (args.target || args.ref || args.selector || args.text) {
      const m = resolveAll(args)[0];
      if (!m) return { ok: false, error: staleRefHint(args) || 'not found', target: targetEcho(args) };
      el = m;
      try { el.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
    } else {
      const scopeEl = resolveScope(args.within);
      if (scopeEl && !scopeEl.contains(el)) {
        return { ok: false, target: describe(el),
          error: 'no target given and the focused element is OUTSIDE within "' + String(args.within) + '" — ' +
            'the key was not sent. Pass a target inside the container, or click it first to move focus there.' };
      }
    }
    const KEYCODES = { Enter: 13, Escape: 27, Tab: 9, Backspace: 8, Delete: 46, ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39, Home: 36, End: 35, PageUp: 33, PageDown: 34, ' ': 32 };
    const code = KEYCODES[key] || (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0);
    const base = { bubbles: true, cancelable: true, composed: true, key: key, code: KEYCODES[key] ? key : ('Key' + key.toUpperCase()), keyCode: code, which: code };
    el.dispatchEvent(new KeyboardEvent('keydown', base));
    if (key.length === 1) el.dispatchEvent(new KeyboardEvent('keypress', base));
    el.dispatchEvent(new KeyboardEvent('keyup', base));
    return { ok: true, pressed: key, target: describe(el) };
  };

  self.hover = function (args) {
    args = args || {};
    const el = resolveAll(args)[0];
    if (!el) return { ok: false, error: staleRefHint(args) || 'not found', target: targetEcho(args) };
    if (!isVisible(el)) return { ok: false, error: 'not visible', target: describe(el) };
    try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) { /* ignore */ }
    const r = el.getBoundingClientRect();
    const base = { bubbles: true, cancelable: true, composed: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    for (const type of ['pointerover', 'pointerenter', 'pointermove', 'mouseover', 'mouseenter', 'mousemove']) {
      let ev;
      try { ev = type.indexOf('pointer') === 0 ? new PointerEvent(type, base) : new MouseEvent(type, base); }
      catch (e) { ev = null; }
      if (ev) el.dispatchEvent(ev);
    }
    return { ok: true, hovered: true, target: describe(el) };
  };

  self.scroll = function (args) {
    args = args || {};
    // Same target-optional fallback as press: without this, a scroll whose `within`
    // has vanished would silently scroll the window instead.
    const scopeErr = scopeMissHint(args);
    if (scopeErr) return { ok: false, error: scopeErr, target: targetEcho(args) };
    if (args.target || args.ref || args.selector || args.text) {
      const el = resolveAll(args)[0];
      if (!el) return { ok: false, error: staleRefHint(args) || 'not found', target: targetEcho(args) };
      try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) { /* ignore */ }
      return { ok: true, scrolledTo: describe(el), scrollY: Math.round(window.scrollY) };
    }
    const to = args.to !== undefined ? args.to : args.value; // act steps carry it as `value`
    // A targetless scroll with a `within` that RESOLVES scrolls THAT CONTAINER. Dropping
    // the scope and moving the viewport instead would be the same call getting opposite
    // treatment (an unresolvable scope is refused above) and would leave the panel the
    // caller named exactly where it was; its overflow, not the window's, is what scrolls.
    const scopeEl = resolveScope(args.within);
    if (scopeEl) {
      const maxTop = Math.max(0, scopeEl.scrollHeight - scopeEl.clientHeight);
      const from = scopeEl.scrollTop || 0;
      let next;
      if (to === 'top') next = 0;
      else if (to === 'bottom') next = maxTop;
      else if (typeof to === 'number') next = to;
      else if (typeof args.by === 'number') next = from + args.by;
      else next = from + Math.round(scopeEl.clientHeight * 0.8); // default: one container viewport
      // assigning scrollTop (rather than el.scrollTo/scrollBy) is what a scrollable
      // element actually reads; browsers clamp the assignment, so clamp here too and the
      // reported position stays the real one.
      scopeEl.scrollTop = Math.max(0, Math.min(maxTop, Math.round(next)));
      return {
        ok: true,
        scrolledContainer: describe(scopeEl),
        scrollTop: Math.round(scopeEl.scrollTop || 0),
        maxScrollTop: maxTop
      };
    }
    if (to === 'top') window.scrollTo(0, 0);
    else if (to === 'bottom') window.scrollTo(0, document.documentElement.scrollHeight);
    else if (typeof to === 'number') window.scrollTo(0, to);
    else if (typeof args.by === 'number') window.scrollBy(0, args.by);
    else window.scrollBy(0, Math.round(window.innerHeight * 0.8)); // default: one viewport down
    return { ok: true, scrollY: Math.round(window.scrollY), maxScrollY: Math.max(0, document.documentElement.scrollHeight - window.innerHeight) };
  };

  // A wait predicate arrives COMPILED. The agent's expression is a string on the wire,
  // and this file used to run it through eval in place, which addons-linter reports as
  // DANGEROUS_EVAL on every submission and which a reviewer cannot tell apart from an
  // extension evaluating strings it did not write. The background now hands over
  // `args.predicateFn` instead, built on its own side of the injection: gecko inlines
  // it into the executeScript code string, where page_eval's wrapper already lives;
  // chromium evaluates the string in the same user-scripts world page_eval uses and
  // hands over a function that replays the outcome (its isolated world refuses eval
  // under the extension CSP). Either way this file never evaluates a string, and a
  // string that arrives without its function is a caller bug: reported, never read as
  // false.
  //
  // Two kinds of throw. A predicate that does not PARSE can never start matching and is
  // returned as a syntaxError; a predicate that THROWS at run time (a global that is not
  // there yet) is an unknown and stays a predicateError the poll loop keeps retrying.
  const PREDICATE_SYNTAX_PREFIX = 'predicate is not valid JavaScript: ';
  function runPredicate(args) {
    if (typeof args.predicateFn !== 'function') {
      return { error: 'predicate was not compiled by the background (predicateFn missing)' };
    }
    try {
      return { truth: !!args.predicateFn() };
    } catch (e) {
      if (e && e.name === 'SyntaxError') return { syntaxError: String(e.message || e) };
      return { error: String((e && e.message) || e) };
    }
  }

  // One-shot wait condition check. The BACKGROUND drives the polling loop
  // (re-injecting this check), so waits survive navigations mid-wait and
  // always evaluate against the CURRENT document; the in-page poll version
  // could keep matching a stale, pre-navigation DOM.
  self.waitCheck = function (args) {
    args = args || {};
    const gone = !!args.gone;
    let predicateError = null;
    let matched = null;
    let selectorMatchCount;
    let selectorVisible;
    // A predicate that does not parse fails the wait outright when it is the ONLY
    // condition (nothing else could ever satisfy it, so polling to the deadline would
    // only delay the same answer). Beside a selector, text or url condition it is
    // reported as a predicateError and the other conditions still decide.
    const predicateOutcome = () => {
      const p = runPredicate(args);
      if (!p.syntaxError) return p;
      const msg = PREDICATE_SYNTAX_PREFIX + p.syntaxError;
      if (!args.selector && !args.textContains && !args.urlContains) return { fail: msg };
      return { error: msg };
    };
    // `within` scopes the SELECTOR and TEXT conditions only; urlContains and predicate
    // are page-global and cannot be scoped.
    //
    // A missing container is NOT an error: a wait POLLS, so it may simply not have
    // rendered yet. But it can never SATISFY the check either, not even a `gone` one.
    // "Nothing is visible inside a container that isn't there" is VACUOUS TRUTH: it would
    // make "click Submit, then wait for the spinner inside the results panel to be gone"
    // pass INSTANTLY, before the panel exists, handing the next act step a still-loading
    // page (and a typo'd container name would pass every gone-wait the same way). This
    // check is ONE-SHOT and cannot know whether the scope was ever there, so it reports
    // the miss (matched:null + scopeMissing) and background's actWait, which owns the
    // wait's history, decides whether a scope that WAS seen and is now gone means done.
    // A malformed selector still fails fast: polling it can never succeed.
    const scopeInfo = resolveScopeInfo(args.within);
    if (scopeInfo.invalid) return { matched: null, error: scopeMissHint(args) };
    if (scopeInfo.given && !scopeInfo.el) {
      // The container `within` names is absent (not rendered yet, or removed). It scopes
      // the SELECTOR and TEXT conditions, so those match nothing here, but urlContains
      // and predicate are PAGE-GLOBAL and must STILL be evaluated: otherwise a positive
      // url/predicate wait that happens to carry a `within` would falsely time out, and a
      // gone one would hinge on the wrong signal. The missing scope is reported alongside.
      let m = null;
      if (args.urlContains) {
        const has = location.href.indexOf(args.urlContains) !== -1;
        if (gone ? !has : has) m = 'url';
      }
      if (!m && args.predicate) {
        const p = predicateOutcome();
        if (p.fail) return { error: p.fail };
        if (p.error) predicateError = p.error;
        else if (gone ? !p.truth : p.truth) m = 'predicate';
      }
      return {
        matched: m,
        url: location.href,
        readyState: document.readyState,
        predicateError: predicateError,
        selectorMatchCount: 0,
        selectorVisible: false,
        scopeMissing: String(args.within)
      };
    }
    const scopeEl = scopeInfo.el || document;
    if (args.selector) {
      // The wait `target` is the universal target (ref | CSS | accessible-name); same as
      // every other action, NOT raw CSS. A ref ("e7") or a name ("Saving") fed straight to
      // querySelectorAll is a valid-but-unmatched TYPE selector, so a gone-wait would
      // VACUOUSLY succeed the instant it ran. Resolve it the way targets resolve everywhere
      // (ref -> CSS -> accessible name), honoring `within`.
      const sel = String(args.selector);
      let list;
      if (/^e\d+$/.test(sel)) {
        const el = self.refs[sel];
        list = (el && el.isConnected && (!scopeInfo.el || scopeInfo.el.contains(el))) ? [el] : [];
      } else {
        try { list = Array.prototype.slice.call(scopeEl.querySelectorAll(sel)); }
        catch (e) { return { matched: null, error: 'invalid selector: ' + e.message }; }
        if (list.length === 0 && !/[.#\[\]>+~:()]/.test(sel)) {
          // a bare word that matched no element AS CSS (querySelectorAll("Saving") looks for
          // <Saving>); the universal target also accepts accessible-name text, so track that.
          list = collectIn(scopeEl).filter(el => nameMatches(accessibleName(el), sel));
        }
      }
      // Visibility is a property of the MATCH SET, not of list[0]: a hidden template/stale
      // twin often precedes the live element; testing only the first would time out a wait
      // whose element is on screen, and, inverted with gone:true, call a still-visible
      // modal dismissed. Diagnostics for the timeout report: match count + any-visible.
      selectorMatchCount = list.length;
      selectorVisible = list.some(isVisible);
      const present = args.requireVisible ? selectorVisible : list.length > 0;
      if (gone ? !present : present) matched = 'selector';
    }
    if (!matched && args.textContains) {
      const root = scopeInfo.el || document.body;
      const has = !!root && (root.innerText || '').indexOf(args.textContains) !== -1;
      if (gone ? !has : has) matched = 'text';
    }
    if (!matched && args.urlContains) {
      const has = location.href.indexOf(args.urlContains) !== -1;
      if (gone ? !has : has) matched = 'url';
    }
    if (!matched && args.predicate) {
      // gone inverts the predicate exactly as it inverts the other conditions; a
      // gone-wait written as a predicate ("the overlay is still up") must match when the
      // predicate goes FALSE. A THROWN predicate is an unknown, never a "gone".
      const p = predicateOutcome();
      if (p.fail) return { error: p.fail };
      if (p.error) predicateError = p.error;
      else if (gone ? !p.truth : p.truth) matched = 'predicate';
    }
    const out = { matched: matched, url: location.href, readyState: document.readyState, predicateError: predicateError };
    if (selectorMatchCount !== undefined) {
      out.selectorMatchCount = selectorMatchCount;
      out.selectorVisible = selectorVisible;
    }
    return out;
  };

  // Legacy in-page polling wait (kept for direct invocation/tests; the act()
  // pipeline uses waitCheck + background-driven polling instead).
  self.waitFor = function (args) {
    args = args || {};
    const timeout = Math.min(args.timeoutMs || 5000, 25000);
    const interval = Math.max(args.intervalMs || 100, 30);
    const start = Date.now();
    return new Promise(resolve => {
      (function poll() {
        const r = self.waitCheck(args);
        // A malformed `within`/selector can never START matching; polling it to the
        // deadline would report a bare TIMEOUT, the one outcome that reads as "just wait
        // longer", and bury the fixable cause waitCheck already diagnosed.
        if (r.error) {
          resolve({ ok: false, error: r.error, elapsedMs: Date.now() - start, url: location.href });
          return;
        }
        if (r.matched) {
          resolve({ ok: true, matched: r.matched, elapsedMs: Date.now() - start, url: location.href });
          return;
        }
        const remaining = timeout - (Date.now() - start);
        if (remaining <= 0) {
          resolve({ ok: false, timedOut: true, elapsedMs: Date.now() - start, url: location.href, predicateError: r.predicateError, scopeMissing: r.scopeMissing, waitedFor: { selector: args.selector, textContains: args.textContains, urlContains: args.urlContains, predicate: args.predicate, gone: !!args.gone } });
          return;
        }
        setTimeout(poll, Math.min(interval, remaining));
      })();
    });
  };

  // Describe the current state of "watch" targets (selectors/refs) so an
  // action result can report semantic effects (e.g. "submit is now enabled")
  // without a follow-up call, and COUNT what the caller named.
  self.inspect = function (args) {
    args = args || {};
    const targets = args.watch || [];
    const out = [];
    for (const w of targets.slice(0, 10)) {
      const els = resolveAll(typeof w === 'string' ? { target: w } : w);
      if (els.length === 0) { out.push({ watch: w, found: false }); continue; }
      const el = els[0];
      const d = describeFull(el, isVisible(el));
      d.watch = w;
      d.found = true;
      out.push(d);
    }
    const res = { ok: true, watched: out };
    // An agent that names WHAT it cares about gets that quantity measured
    // rather than a boolean. Raw querySelectorAll on purpose; the point is a
    // stable, cheap, repeatable NUMBER, so ref / accessible-name resolution would
    // only add ambiguity. Exactly one entry per input selector, in input order, so
    // the background's before/after pairing can never drift the way two result
    // lists zipped by index do. A selector that cannot parse, or a `within` that
    // is not in the document, reports an ERROR; never a silent 0, which would
    // read as "the rows disappeared".
    if (Array.isArray(args.count) && args.count.length) {
      const scopeInfo = resolveScopeInfo(args.within);
      const scopeEl = scopeInfo.el || document;
      res.counts = args.count.slice(0, 8).map(function (sel) {
        if (scopeInfo.given && !scopeInfo.el) {
          return { selector: String(sel), error: 'the within scope "' + String(args.within) + '" is not in the document' };
        }
        try {
          return { selector: String(sel), count: scopeEl.querySelectorAll(String(sel)).length };
        } catch (e) {
          return { selector: String(sel), error: 'invalid selector: ' + String((e && e.message) || e) };
        }
      });
    }
    return res;
  };

  // ---- dispatcher ----------------------------------------------------------
  // async so a single try/catch covers both synchronous throws and rejected
  // promises from the async methods (click/waitFor/fill); the structured
  // {ok:false,error} contract is preserved on every path. Firefox awaits the
  // returned promise from executeScript.
  //
  // Every result leaves here carrying the document's element count before and
  // after the method ran, so the background can subtract without paying for an extra
  // injection on the common path. The dispatch switch itself lives in invokeMethod
  // because it must stay BETWEEN these two anchors; parity_lib.parse_automation_methods
  // reads the method registry out of exactly this window.
  self.invoke = async function (method, args) {
    args = args || {};
    const nodesAtStart = elementCount();
    let out;
    try {
      out = await invokeMethod(method, args);
    } catch (e) {
      out = { ok: false, error: String((e && e.message) || e) };
    }
    if (out && typeof out === 'object') {
      out.nodesAtStart = nodesAtStart;
      out.nodesAtEnd = elementCount();
      // Omitted rather than sent as null: the background distinguishes "no docId in this
      // response" (unknown) from a value, and an explicit null invites reading it as an
      // answer. See documentIdOrNull above.
      if (self.docId) out.docId = self.docId;
    }
    return out;
  };

  async function invokeMethod(method, args) {
    switch (method) {
      case 'snapshot': return self.snapshot(args);
      case 'click': return await self.click(args);
      // `await` (not a bare `return`): a returned promise settles AFTER the try block
      // is left, so a rejection would escape this catch and lose the {ok:false} envelope.
      case 'fill': return await self.fill(args);
      case 'select': return await self.select(args);
      case 'check': return await self.fill(args);
      case 'press': return self.press(args);
      case 'hover': return self.hover(args);
      case 'scroll': return self.scroll(args);
      case 'wait_for': return await self.waitFor(args);
      case 'wait_check': return self.waitCheck(args);
      case 'page_state': return { ok: true, pageState: pageState() };
      case 'inspect': return self.inspect(args);
      case 'rect': return await self.rect(args);
      default: return { ok: false, error: 'unknown automation method: ' + method };
    }
  }

  window.__browserplugAuto = self;
})();
