/*
 * BrowserPlug: OpenAI (chatgpt.com) usage: VIEW layer
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Display helpers for the popup's Connections panel and its Usage tab: the usage bar,
 * its tooltip, and the plain-text mirror the copy path uses. Loaded in the POPUP;
 * publishes `OpenAIView`. The data half is openai-data.js.
 *
 * Laid out as a sibling of anthropic-view.js, and drawing the identical bar, because it
 * IS the same widget reading the same kind of fact: a rolling short window and a weekly
 * one, each a percentage with a reset time. What differs is where the numbers come from
 * and that there is no peak-hours indicator to sit beside it — OpenAI has no
 * fixed-schedule pricing or capacity window to draw one from, so nothing here consults
 * the clock except to count down a reset that was fetched.
 */
(function () {
  'use strict';

  // anthropic-view.js's formatters, copied rather than imported, for the reason
  // deepseek-view.js gives: popup.html loads each view file separately and any of them
  // can be absent, so a shared import would make this module fail to render whenever a
  // neighbour is missing. Keep them in step by hand if that file's change.
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

  // ---- classification -----------------------------------------------------
  // Codex CLI is the OpenAI client that reaches this server; the row is named by the
  // server (server.py :: _friendly_from_cmdline). The long spelling is accepted for the
  // same reason the DeepSeek view accepts one: a popup ahead of its server must not
  // silently drop the bar.
  function isOpenAIClient(c) {
    if (!c || c.kind !== 'mcp_client') return false;
    const label = c.label || '';
    return c.friendly === 'Codex CLI' || label.startsWith('Codex CLI')
      || c.friendly === 'Codex' || label.startsWith('Codex ');
  }

  // ---- bars ---------------------------------------------------------------

  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

  function windowLine(name, w) {
    let l = `${name} ${clampPct(w.percent)}%`;
    if (w.resetsAt) l += ` · resets in ${fmtLeft(new Date(w.resetsAt) - Date.now())}`;
    return l;
  }

  function usageTooltip(usage, opts) {
    const lines = [];
    const s = usage.session;
    if (s) lines.push(windowLine(cap(s.windowName || 'session'), s));
    if (opts && opts.showWeekly && usage.weekly) {
      lines.push(windowLine(cap(usage.weekly.windowName || 'weekly'), usage.weekly));
    }
    if (usage.plan) lines.push(`Plan: ${usage.plan}`);
    if (usage.limitReached) {
      lines.push(usage.reachedType
        ? `Limit reached (${usage.reachedType}) — requests are being refused.`
        : 'Limit reached — requests are being refused.');
    }
    lines.push('ChatGPT and Codex share this allowance.');
    return lines.join('\n');
  }

  // Byte for byte the bar anthropic-view.js builds, down to the split when a weekly
  // window is shown: a reader should not have to learn a second shape to read the same
  // kind of fact one row down. The one addition is the exhausted state, which Anthropic
  // has no equivalent of — there a full bar means "you are at the cap", here the API is
  // already saying no.
  function makeUsageBar(usage, opts) {
    const bar = document.createElement('span');
    const track = usage.limitReached ? '#f7d4d1' : '#e6e6e6';
    bar.style.cssText = 'display:inline-block; position:relative; width:38px; height:8px; '
      + `border-radius:4px; background:${track}; overflow:hidden; cursor:help; vertical-align:middle;`;
    const sPct = clampPct(usage.session.percent);
    const color = (p) => (usage.limitReached ? '#f44336' : pctColor(p));
    if (opts && opts.showWeekly && usage.weekly) {
      const wPct = clampPct(usage.weekly.percent);
      const top = document.createElement('span');
      top.style.cssText = `position:absolute; left:0; top:0; height:4px; width:${sPct}%; background:${color(sPct)};`;
      const bot = document.createElement('span');
      bot.style.cssText = `position:absolute; left:0; bottom:0; height:4px; width:${wPct}%; background:${color(wPct)}; opacity:.9;`;
      const div = document.createElement('span');
      div.style.cssText = 'position:absolute; left:0; right:0; top:3px; height:1px; background:rgba(255,255,255,.7);';
      bar.appendChild(top); bar.appendChild(bot); bar.appendChild(div);
    } else {
      const fill = document.createElement('span');
      fill.style.cssText = `position:absolute; left:0; top:0; bottom:0; width:${sPct}%; background:${color(sPct)};`;
      bar.appendChild(fill);
    }
    bar.title = usageTooltip(usage, opts);
    return bar;
  }

  // The bar for a client row. Null when there is nothing to draw, so the row renders as
  // if the feature were not there rather than leaving a gap — buildUsageBars()'s
  // contract in anthropic-view.js, kept here.
  function buildUsageBars(usage, opts) {
    if (!usage || !usage.session) return null;
    const wrap = document.createElement('span');
    wrap.style.cssText = 'display:inline-flex; align-items:center; gap:4px; vertical-align:middle; margin:0 2px;';
    wrap.appendChild(makeUsageBar(usage, opts));
    return wrap;
  }

  // The chart (usage-chart.js draws it): the 5-hour session trace and the weekly one, each on
  // its own axis. Null when neither window is known yet, or there is no chart module.
  function buildUsageChart(usage) {
    const UC = globalThis.UsageChart;
    if (!UC || !usage || !(usage.sessionWindow || (usage.weekly && usage.weeklyWindow))) return null;
    return UC.build(usage, { session: true, weekly: true });
  }

  // Plain-text summary for the right-click copy path (bars don't survive a paste).
  function usageSummaryText(usage, opts) {
    if (usage && usage.session) {
      let s = `${usage.session.windowName || 'session'} ${clampPct(usage.session.percent)}%`;
      if (usage.session.resetsAt) s += ` (resets in ${fmtLeft(new Date(usage.session.resetsAt) - Date.now())})`;
      if (opts && opts.showWeekly && usage.weekly) {
        s += ` · weekly ${clampPct(usage.weekly.percent)}%`;
        if (usage.weekly.resetsAt) s += ` (resets in ${fmtLeft(new Date(usage.weekly.resetsAt) - Date.now())})`;
      }
      if (usage.limitReached) s += ' · LIMIT REACHED';
      return s;
    }
    if (usage && usage.ok === false) {
      return usage.signedOut ? 'not signed in to chatgpt.com' : `usage n/a (${usage.error || 'fetch failed'})`;
    }
    return '';
  }

  globalThis.OpenAIView = {
    isOpenAIClient, fmtLeft,
    buildUsageBars, buildUsageChart, usageSummaryText,
  };
})();
