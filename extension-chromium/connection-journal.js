/*
 * connection-journal.js — what the extension remembers about its own socket.
 *
 * When the server drops a browser and the browser cannot get back in, the only
 * account of what happened lives in the extension's background console, and that
 * console is gone the moment the browser is force-killed. A wedge on 2026-09-21 cost
 * exactly that: the server logged a close with no code, then two sockets that opened
 * and never sent a hello, and nothing on the extension side survived to say why.
 *
 * So the extension keeps its own record. Every connection event (a socket opened, a
 * hello sent, a close with its code and reason, an error, a scheduled retry, a 4401,
 * a token fetch, the user pressing Stop, a watchdog reconnect) lands in a 30-entry
 * ring that is written through to storage.local on every append. A force-kill takes
 * the process, not the file, so the next browser start reads back the tail from
 * before the kill. The hello then carries the entries the server has not seen yet,
 * which puts the browser's account of the outage in the server log next to the
 * server's own.
 *
 * Bounded on every axis, because this runs on a browser that may be out of memory:
 * 30 entries in the ring, 20 in a hello, 120 characters of reason text. The write is
 * fire-and-forget and a failure is swallowed — losing the record of an event is
 * better than losing the event.
 *
 * Load it before background.js (gecko: background.scripts; chromium: importScripts)
 * and it registers a singleton bound to this browser's storage.local. The factory is
 * exported beside it so the tests can drive it with fakes.
 */
(() => {
  'use strict';

  const STORAGE_KEY = 'connectionJournal';
  const MAX_ENTRIES = 30;    // the ring
  const MAX_IN_HELLO = 20;   // what one hello may carry
  const MAX_REASON = 120;    // characters of free text per entry

  // The wire vocabulary the server reads. Listed here so a typo at a call site is
  // visible to a reader; note() does NOT reject an unknown name, because a dropped
  // event is worse than an unexpected one.
  const EVENTS = ['open', 'hello', 'close', 'error', 'retry', 'rejected',
                  'token_fetch', 'stopped', 'reconnect'];

  function clampReason(value) {
    if (value === undefined || value === null) return undefined;
    const text = String(value);
    if (!text) return undefined;
    return text.length > MAX_REASON ? text.slice(0, MAX_REASON) : text;
  }

  function asInt(value) {
    if (value === undefined || value === null || value === '') return undefined;
    const n = Number(value);
    return Number.isFinite(n) ? Math.trunc(n) : undefined;
  }

  // opts.storage : a browser.storage.local-like {get, set}; null keeps the journal in
  //                memory only, which is what a test slice without storage gets.
  // opts.now     : clock, for tests.
  // opts.max     : ring size.
  // opts.onError : reporter for a failed load. A failed WRITE is deliberately silent.
  function createJournal(opts) {
    opts = opts || {};
    const area = opts.storage || null;
    const now = opts.now || (() => Date.now());
    const max = Math.max(1, opts.max || MAX_ENTRIES);
    const onError = opts.onError || (() => {});

    let entries = [];
    let pending = 0;      // appended since the last hello went out
    let loaded = false;

    function persist() {
      if (!area || typeof area.set !== 'function') return;
      try {
        const p = area.set({ [STORAGE_KEY]: entries });
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (e) { /* storage is unavailable; the in-memory ring still holds */ }
    }

    function note(event, fields) {
      const f = fields || {};
      const entry = { t: asInt(now()), e: String(event) };
      const code = asInt(f.code);
      if (code !== undefined) entry.code = code;
      const reason = clampReason(f.reason);
      if (reason !== undefined) entry.reason = reason;
      const attempt = asInt(f.attempt);
      if (attempt !== undefined) entry.attempt = attempt;

      entries.push(entry);
      if (entries.length > max) entries = entries.slice(entries.length - max);
      pending = Math.min(pending + 1, entries.length);
      persist();
      return entry;
    }

    // Everything since the previous hello was SENT, oldest first, newest kept when
    // there are more than the cap: an outage that produced 40 events is better
    // described by its last 20 than by its first 20.
    function sinceLastHello(limit) {
      const cap = Math.max(1, limit || MAX_IN_HELLO);
      const n = Math.min(pending, entries.length);
      if (!n) return [];
      const slice = entries.slice(entries.length - n);
      return slice.length > cap ? slice.slice(slice.length - cap) : slice;
    }

    // Called AFTER the frame is on the wire, so a hello that throws mid-send does not
    // close a window the server never heard about.
    function noteHelloSent() {
      const entry = note('hello');
      pending = 0;
      return entry;
    }

    async function load() {
      if (loaded) return entries;
      if (!area || typeof area.get !== 'function') { loaded = true; return entries; }
      try {
        const stored = await area.get({ [STORAGE_KEY]: null });
        const list = stored && stored[STORAGE_KEY];
        if (Array.isArray(list) && !entries.length) {
          entries = list
            .filter((x) => x && typeof x === 'object' && typeof x.e === 'string')
            .slice(-max);
          // The previous life reported everything up to its last hello. What came
          // after it is what the server still has not been told.
          let lastHello = -1;
          for (let i = 0; i < entries.length; i++) if (entries[i].e === 'hello') lastHello = i;
          pending = entries.length - 1 - lastHello;
        }
      } catch (e) {
        onError(e);
      }
      loaded = true;
      return entries;
    }

    return {
      note,
      noteHelloSent,
      sinceLastHello,
      load,
      recent: (n) => entries.slice(Math.max(0, entries.length - Math.max(1, n || 10))),
      all: () => entries.slice(),
      pendingCount: () => pending,
      isLoaded: () => loaded
    };
  }

  globalThis.ConnectionJournal = {
    createJournal, STORAGE_KEY, MAX_ENTRIES, MAX_IN_HELLO, MAX_REASON, EVENTS
  };

  // The singleton background.js writes to. Built here rather than in background.js so
  // both trees get it by loading the file, with nothing to wire up at the call sites.
  let area = null;
  try {
    const api = globalThis.browser || globalThis.chrome;
    area = (api && api.storage && api.storage.local) || null;
  } catch (e) {
    area = null;
  }
  const instance = createJournal({
    storage: area,
    onError: (e) => { try { console.warn('connection journal:', e && e.message); } catch (_) {} }
  });
  globalThis.browserplugJournal = instance;

  // Read back whatever survived the last browser exit. Fire-and-forget: the first
  // hello of a cold start is scheduled behind loadConfig() and a WebSocket handshake,
  // so this one storage read lands well ahead of it, and if it does not, the hello
  // simply carries less.
  try {
    const p = instance.load();
    if (p && typeof p.catch === 'function') p.catch(() => {});
  } catch (e) { /* nothing to restore */ }
})();
