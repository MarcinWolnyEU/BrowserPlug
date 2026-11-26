/*
 * BrowserPlug: Mistral AI usage: VIEW layer
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Display helpers for a Mistral Vibe row in the popup's Connections panel, for the Usage
 * tab card and Options sign-in line, and for the plain-text copy path. Loaded in the
 * POPUP and on the Options page; publishes `MistralView`. The data half is
 * mistral-data.js, which fetches the two included allowances and the plan in the
 * background.
 *
 * A reading carries the Vibe & Studio allowance at its top level (the row is a Vibe client,
 * so the bar and the copied row show it) and the API allowance as `usage.api`. Both go
 * through the same text and over-budget helpers.
 *
 * The bar is anthropic-view.js's widget (same size, colours and thresholds), so the same
 * kind of fact reads the same way one row down. One text line serves the row tooltip, the
 * copied row and the Usage tab meter, so the three cannot disagree.
 */
(function () {
  'use strict';

  // openai-view.js's formatter, copied for the reason given there: each view file is
  // loaded separately and any of them may be missing.
  function fmtLeft(ms) {
    if (!(ms > 0)) ms = 0;
    let s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); const ss = s % 60;
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
    if (m > 0) return `${m}m ${String(ss).padStart(2, '0')}s`;
    return `${ss}s`;
  }

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const clampPct = (v) => clamp(Number(v) || 0, 0, 100);
  const pctColor = (p) => (p >= 80 ? '#f44336' : (p >= 50 ? '#f0ad4e' : '#4CAF50'));
  const SIGNED_OUT_TEXT = 'not signed in to chat.mistral.ai';

  // ---- classification -------------------------------------------------------
  // The server names the row (client_identity.friendly_from_cmdline); the label prefix is
  // accepted too, for a popup running ahead of its server.
  function isMistralClient(c) {
    if (!c || c.kind !== 'mcp_client') return false;
    return c.friendly === 'Mistral Vibe' || (c.label || '').startsWith('Mistral Vibe');
  }

  // ---- numbers ----------------------------------------------------------------

  // EUR is shown as its sign, any other currency as its ISO code. A whole amount drops
  // the cents ("€255"), an amount spent keeps them ("€0.34").
  function fmtMoney(v, currency, whole) {
    const n = Number(v) || 0;
    const digits = whole && Number.isInteger(n) ? 0 : 2;
    const num = n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
    if (!currency) return num;
    return currency === 'EUR' ? `€${num}` : `${currency} ${num}`;
  }

  // One decimal, and none for a whole number: "0.1", "12", "121.6".
  const fmtPct = (p) => String(Math.round((Number(p) || 0) * 10) / 10);

  const overBudget = (usage) => !!usage && Number(usage.percent) > 100;

  // How long until the monthly allowance resets ('5d 14h'), or '' when no date came back.
  function resetLeft(usage) {
    const ms = usage && usage.resetsAt ? new Date(usage.resetsAt) - Date.now() : NaN;
    return isNaN(ms) ? '' : fmtLeft(ms);
  }

  // "€0.34 of €255 · 0.1% · resets in 5d 14h", for any allowance ({percent, budget, spent,
  // currency, resetsAt}). Without a budget figure the amounts drop out and the percentage
  // stays; past 100 % it says so (only pay-as-you-go allows that).
  function usageText(usage) {
    const parts = [];
    if (usage.budget != null && usage.spent != null) {
      parts.push(`${fmtMoney(usage.spent, usage.currency)} of ${fmtMoney(usage.budget, usage.currency, true)}`);
    }
    parts.push(`${fmtPct(usage.percent)}%`);
    if (overBudget(usage)) parts.push('over budget');
    const left = resetLeft(usage);
    if (left) parts.push(`resets in ${left}`);
    return parts.join(' · ');
  }

  // ---- bar --------------------------------------------------------------------

  function usageTooltip(usage) {
    const lines = [];
    if (usage.plan) lines.push(`Mistral ${usage.plan}`);
    lines.push(`Vibe & Studio: ${usageText(usage)}`);
    if (usage.api) lines.push(`API: ${usageText(usage.api)}`);
    if (usage.stale) lines.push('Last refresh failed; this is the previous reading.');
    return lines.join('\n');
  }

  // The bar stops at the full width when the budget is exceeded; the text says by how much.
  function makeUsageBar(usage) {
    const pct = clampPct(usage.percent);
    const bar = document.createElement('span');
    bar.style.cssText = 'display:inline-block; position:relative; width:38px; height:8px; '
      + 'border-radius:4px; background:#e6e6e6; overflow:hidden; cursor:help; vertical-align:middle;';
    const fill = document.createElement('span');
    fill.style.cssText = `position:absolute; left:0; top:0; bottom:0; width:${pct}%; background:${pctColor(pct)};`;
    bar.appendChild(fill);
    bar.title = usageTooltip(usage);
    return bar;
  }

  function marker(text, title) {
    const na = document.createElement('span');
    na.className = 'conn-na';
    na.textContent = text;
    na.title = title;
    return na;
  }

  // ---- the row ----------------------------------------------------------------

  // What a Mistral Vibe row carries inline: the usage bar, or a muted marker when the
  // reading failed. Null while no reading has arrived, so the row renders as plain text
  // with no gap.
  function buildRowInline(usage) {
    if (!usage) return null;
    if (usage.ok === false) {
      return marker(usage.signedOut ? 'signed out' : 'usage n/a',
        (usage.signedOut ? 'No chat.mistral.ai session in this browser profile.'
          : 'Mistral usage unavailable: ' + (usage.error || 'fetch failed'))
        + '\nSign in to chat.mistral.ai in this browser profile to see the usage bar.'
        + '\nDetails: Options ▸ Advanced ▸ Mistral AI sessions.');
    }
    const wrap = document.createElement('span');
    wrap.style.cssText = 'display:inline-flex; align-items:center; gap:4px; vertical-align:middle; margin:0 2px;';
    wrap.appendChild(makeUsageBar(usage));
    return wrap;
  }

  // Plain text for the right-click copy path: what the row shows, in words. '' when the
  // row shows nothing.
  function rowSummaryText(usage) {
    if (!usage) return '';
    if (usage.ok === false) {
      return usage.signedOut ? SIGNED_OUT_TEXT : `usage n/a (${usage.error || 'fetch failed'})`;
    }
    return usageText(usage);
  }

  // The monthly chart (usage-chart.js draws it): the violet Vibe & Studio line and, when the
  // reading has an API allowance, the teal API line (the chart's second monthly slot), both
  // on the month's axis. Null until a window exists, or without the chart module.
  function buildUsageChart(usage) {
    const UC = globalThis.UsageChart;
    if (!UC || !usage || (!usage.monthlyWindow && !usage.apiWindow)) return null;
    // The chart reads each sample's value under the slot's own name (`completions`), while
    // the store files the API line's samples under `api`.
    const apiSamples = Array.isArray(usage.apiHistory) ? usage.apiHistory : [];
    const shown = Object.assign({}, usage, {
      completionsHistory: apiSamples.map(h => ({ t: h.t, completions: h.api })),
      completionsWindow: usage.apiWindow,
    });
    return UC.build(shown, {
      monthly: true, monthlyLabel: 'Vibe & Studio', completions: !!usage.api, completionsLabel: 'API',
    });
  }

  // ---- Options card -------------------------------------------------------------

  // The sign-in line on Options ▸ Advanced ▸ Mistral AI sessions, from one forced read.
  // Pure, so options.js only paints it: { tone: 'ok'|'warn'|'bad', text, help, signIn }
  // where signIn says whether to offer the "Open chat.mistral.ai" button. The usage is read
  // from admin.mistral.ai, but the session is made at chat.mistral.ai.
  function authState(data, failure) {
    const reason = String((data && data.error) || failure || '');
    if (data && data.ok) {
      if (data.stale) {
        return {
          tone: 'warn', signIn: true,
          text: 'Signed in to chat.mistral.ai, but the last refresh failed.',
          help: `admin.mistral.ai did not answer the latest usage read (${reason}). The popup keeps showing the `
            + 'last numbers it got until a refresh succeeds.',
        };
      }
      return {
        tone: 'ok', signIn: false,
        text: `Signed in to chat.mistral.ai in this browser${data.plan ? ` (${data.plan} plan)` : ''}.`,
        help: 'Your Mistral usage is readable, so the settings below apply. Right now, Vibe & Studio: '
          + `${usageText(data)}` + (data.api ? `; API: ${usageText(data.api)}` : '')
          + '. They are the included allowances admin.mistral.ai shows under Subscription.',
      };
    }
    if (data && data.signedOut) {
      return {
        tone: 'warn', signIn: true,
        text: 'Not signed in to chat.mistral.ai in this browser.',
        help: 'The included allowances can only be read with a Mistral session. Until you sign in, every '
          + 'Mistral Vibe row shows "signed out" instead of a usage bar and the Usage tab draws no Mistral '
          + 'card. Sign in to chat.mistral.ai in THIS browser, then Re-check.',
      };
    }
    return {
      tone: 'bad', signIn: true,
      text: 'Could not read your Mistral usage.',
      help: `The usage read from admin.mistral.ai failed${reason ? ` (${reason})` : ''}. While it keeps failing, the bar stays `
        + 'hidden on every Mistral Vibe row; the rest of BrowserPlug is unaffected.',
    };
  }

  globalThis.MistralView = {
    isMistralClient, fmtLeft, fmtMoney, usageText, resetLeft, overBudget,
    makeUsageBar, buildRowInline, rowSummaryText, buildUsageChart, authState,
  };
})();
