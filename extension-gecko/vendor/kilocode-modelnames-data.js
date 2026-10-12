/*
 * BrowserPlug; Kilo model/provider name resolution: DATA layer
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Resolves Kilo's raw provider/model CODES (e.g. gitlab / duo-chat-haiku-4-5)
 * into friendly display NAMES via the models.dev registry; the same catalog
 * opencode/Kilo is built on, so no hardcoded map. Loaded in the BACKGROUND page;
 * publishes `KiloModelNames`. The display half is kilocode-view.js.
 *
 * Batch by design: resolve([{provider,model}, …]) resolves a whole set at once,
 * hitting models.dev only ONCE per batch and only when something is missing from
 * the persistent storage.local cache. Cache = just the resolved names (small),
 * cleared by "Reset to Defaults".
 */
(function () {
  'use strict';

  const MODELS_DEV_URL = 'https://models.dev/api.json';
  const CACHE_KEY = 'kiloModelNames';
  const keyOf = (p, m) => `${p || ''}\n${m || ''}`;

  // opts.local: storage.local-like {get,set,remove}; omit → no persistence.
  // opts.fetch: injectable fetch(url) (defaults to ambient fetch); for tests.
  // opts.url / opts.cacheKey: tunables.
  function createResolver(opts) {
    opts = opts || {};
    const local = opts.local || null;
    const doFetch = opts.fetch || ((u) => fetch(u));
    const URL = opts.url || MODELS_DEV_URL;
    const KEY = opts.cacheKey || CACHE_KEY;

    let cache = null;      // { providers: {id: name}, models: {key: name} }
    let loaded = false;

    async function loadCache() {
      if (loaded) return;
      loaded = true;
      if (!local) { cache = { providers: {}, models: {} }; return; }
      try {
        const got = await local.get(KEY);
        const c = got && got[KEY];
        cache = (c && c.providers && c.models) ? c : { providers: {}, models: {} };
      } catch (e) { cache = { providers: {}, models: {} }; }
    }

    function saveCache() {
      if (!local) return;
      try { const p = local.set({ [KEY]: cache }); if (p && p.catch) p.catch(() => {}); } catch (e) {}
    }

    async function fetchCatalog() {
      const r = await doFetch(URL);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }

    // Resolve a batch of {provider, model} pairs → { key: {providerName, modelName} }.
    // One catalog fetch for the whole batch, only if something is uncached. On a
    // fetch failure, returns whatever the cache has (names may be null → caller
    // falls back to the raw code). "Not found in the registry" is cached as null
    // so we don't refetch it; a failed fetch is NOT cached, so it retries.
    async function resolve(pairs) {
      await loadCache();
      const misses = [];
      for (const { provider, model } of (pairs || [])) {
        if (!provider && !model) continue;
        const k = keyOf(provider, model);
        const needModel = model && cache.models[k] === undefined;
        const needProvider = provider && cache.providers[provider] === undefined;
        if (needModel || needProvider) misses.push({ provider, model, k });
      }
      if (misses.length) {
        let catalog = null;
        try { catalog = await fetchCatalog(); } catch (e) { /* keep cache; retry next time */ }
        if (catalog) {
          for (const { provider, model, k } of misses) {
            const prov = catalog[provider];
            if (provider) cache.providers[provider] = (prov && prov.name) || null;
            if (model) cache.models[k] = (prov && prov.models && prov.models[model] && prov.models[model].name) || null;
          }
          saveCache();
        }
      }
      const out = {};
      for (const { provider, model } of (pairs || [])) {
        if (!provider && !model) continue;
        const k = keyOf(provider, model);
        out[k] = {
          providerName: (provider && cache.providers[provider]) || null,
          modelName: (model && cache.models[k]) || null,
        };
      }
      return out;
    }

    // Full reset (used by "Reset to Defaults"): clear in-memory + persisted cache.
    function clear() {
      cache = { providers: {}, models: {} };
      loaded = true;
      if (local) { try { const p = local.remove(KEY); if (p && p.catch) p.catch(() => {}); } catch (e) {} }
    }

    return { resolve, clear, keyOf };
  }

  globalThis.KiloModelNames = { createResolver, keyOf, CACHE_KEY };
})();
