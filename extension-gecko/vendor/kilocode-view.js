/*
 * BrowserPlug; Kilo CLI display: VIEW layer
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Kilo-specific PRESENTATION for the popup's Connections panel. The raw
 * provider/model CODES are detected server-side (server/usage_panels/kilocode_data.py) and
 * arrive as `c.provider` / `c.model`; the friendly display NAMES are resolved via
 * kilocode-modelnames-data.js (models.dev) and, when available, attached as
 * `c.provider_name` / `c.model_name`. This module just formats them into a
 * compact badge, falling back to the raw code. Publishes `KiloCode`.
 */
(function () {
  'use strict';

  // Fallback labels for Kilo's providerID values, used only when models.dev
  // didn't resolve a name (offline / not-found / clarification disabled).
  const PROVIDER_LABELS = {
    openrouter: 'OpenRouter',
    gitlab: 'GitLab Duo',
    anthropic: 'Anthropic',
    openai: 'OpenAI',
    google: 'Google',
    'x-ai': 'xAI',
    deepseek: 'DeepSeek',
    mistral: 'Mistral',
    ollama: 'Ollama',
    kilo: 'Kilo',
  };

  function isKiloCli(c) {
    return !!c && c.kind === 'mcp_client'
      && (c.friendly === 'Kilo CLI' || (c.label || '').startsWith('Kilo CLI'));
  }

  // Which rows get the badge. Kilo CLI is no longer the only client whose current
  // provider/model the server can read (pi's session transcript gives the same pair,
  // see server/usage_panels/pi_data.py), and the formatting below never depended on WHICH agent it
  // came from — so the gate is the data being there, not the client's name. A client
  // the server can't read stays without a badge exactly as before.
  function hasModelBadge(c) {
    return !!c && c.kind === 'mcp_client' && !!(c.provider || c.model);
  }

  function providerLabel(id) {
    if (!id) return null;
    return PROVIDER_LABELS[String(id).toLowerCase()] || id;
  }

  // Model slugs are often "provider/model"; drop the redundant prefix.
  function modelLabel(model) {
    if (!model) return null;
    const s = String(model);
    const slash = s.indexOf('/');
    return slash >= 0 ? s.slice(slash + 1) : s;
  }

  // Resolved display name preferred; else the fallback label / stripped code.
  function providerDisplay(c) {
    return (c && c.provider_name) || providerLabel(c && c.provider);
  }
  function modelDisplay(c) {
    return (c && c.model_name) || modelLabel(c && c.model);
  }

  // A compact "◆ Provider · model" badge for a Kilo CLI row, or null when the
  // server couldn't detect anything.
  function buildProviderBadge(c) {
    const provider = c && c.provider;
    const model = c && c.model;
    if (!provider && !model) return null;
    const span = document.createElement('span');
    span.style.cssText = 'display:inline-flex; align-items:center; vertical-align:middle; '
      + 'margin:0 2px; padding:0 5px; border-radius:8px; background:var(--pill-neutral-bg, #eef1f6); color:var(--pill-neutral-text, #334); '
      + 'font-size:10px; line-height:14px; cursor:help; white-space:nowrap;';
    const parts = [providerDisplay(c), modelDisplay(c)].filter(Boolean);
    span.textContent = '◆ ' + parts.join(' · ');
    span.title = `providerID=${provider || '?'} · modelID=${model || '?'}`;
    return span;
  }

  // Plain-text version for the right-click "Copy connection info" path.
  function summaryText(c) {
    const provider = c && c.provider;
    const model = c && c.model;
    if (!provider && !model) return '';
    return [providerDisplay(c), modelDisplay(c)].filter(Boolean).join(' · ');
  }

  globalThis.KiloCode = { isKiloCli, hasModelBadge, providerLabel, modelLabel,
    buildProviderBadge, summaryText };
})();
