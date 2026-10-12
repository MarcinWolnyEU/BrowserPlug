/*
 * BrowserPlug: Anthropic (claude.ai) usage: VIEW layer
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Display helpers for the popup's Connections panel: the usage bars, the
 * expandable chart, peak-hours math, and formatting. Loaded in the POPUP;
 * publishes `AnthropicView`. The data half (fetch/parse/store) is anthropic-data.js.
 */
(function () {
  'use strict';


  // ---- formatting ---------------------------------------------------------
  // Compact duration: "2d 3h" / "1h 20m" / "20m 05s" / "45s".
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

  // Clock-style "H:MM:SS" (peak "… left", matching the claude-peak-time site).
  function fmtHMS(ms) {
    if (!(ms > 0)) ms = 0;
    let s = Math.floor(ms / 1000);
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); const ss = s % 60;
    return `${h}:${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}`;
  }

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const clampPct = (v) => clamp(Number(v) || 0, 0, 100);
  const pctColor = (p) => (p >= 80 ? '#f44336' : (p >= 50 ? '#f0ad4e' : '#4CAF50'));

  // ---- peak hours ---------------------------------------------------------
  // Anthropic peak hours: weekdays 12:00–18:00 UTC (= 14:00–20:00 Europe/Warsaw),
  // per claude-peak-time.pages.dev. `visible` is true during peak OR within
  // `leadMinutes` before the next peak begins.
  function peakInfo(now = new Date(), leadMinutes = 0) {
    const START = 12, END = 18;
    const isWeekday = (dow) => dow >= 1 && dow <= 5;
    const start = new Date(now); start.setUTCHours(START, 0, 0, 0);
    const end = new Date(now); end.setUTCHours(END, 0, 0, 0);
    const inPeak = isWeekday(now.getUTCDay()) && now >= start && now < end;
    if (inPeak) {
      return { inPeak: true, visible: true, frac: (now - start) / (end - start), start, end, msLeft: end - now };
    }
    const next = new Date(now); next.setUTCHours(START, 0, 0, 0);
    if (now >= next) next.setUTCDate(next.getUTCDate() + 1);
    while (!isWeekday(next.getUTCDay())) next.setUTCDate(next.getUTCDate() + 1);
    const nextEnd = new Date(next.getTime() + (END - START) * 3600e3);
    const msUntil = next - now;
    const leadMs = Math.max(0, Number(leadMinutes) || 0) * 60000;
    return { inPeak: false, visible: msUntil <= leadMs, frac: 0, start: next, end: nextEnd, msUntil };
  }

  // ---- classification -----------------------------------------------------
  function isClaudeCode(c) {
    return !!c && c.kind === 'mcp_client'
      && (c.friendly === 'Claude Code' || (c.label || '').startsWith('Claude Code'));
  }

  // ---- bars ---------------------------------------------------------------
  function usageTooltip(usage, opts) {
    const lines = [];
    const s = usage.session;
    if (s) {
      let l = `Session ${clampPct(s.percent)}%`;
      if (s.resetsAt) l += ` · resets in ${fmtLeft(new Date(s.resetsAt) - Date.now())}`;
      lines.push(l);
    }
    if (opts && opts.showWeekly && usage.weekly) {
      const w = usage.weekly;
      let l = `Weekly ${clampPct(w.percent)}%`;
      if (w.resetsAt) l += ` · resets in ${fmtLeft(new Date(w.resetsAt) - Date.now())}`;
      lines.push(l);
    }
    lines.push('Click to graph usage since reset');
    return lines.join('\n');
  }

  // The usage bar. Single fill by default; when showWeekly it splits
  // horizontally: top half = session, bottom half = weekly, under one tooltip.
  function makeUsageBar(usage, opts) {
    const bar = document.createElement('span');
    bar.style.cssText = 'display:inline-block; position:relative; width:38px; height:8px; '
      + 'border-radius:4px; background:#e6e6e6; overflow:hidden; cursor:pointer; vertical-align:middle;';
    const sPct = clampPct(usage.session.percent);
    if (opts && opts.showWeekly && usage.weekly) {
      const wPct = clampPct(usage.weekly.percent);
      const top = document.createElement('span');
      top.style.cssText = `position:absolute; left:0; top:0; height:4px; width:${sPct}%; background:${pctColor(sPct)};`;
      const bot = document.createElement('span');
      bot.style.cssText = `position:absolute; left:0; bottom:0; height:4px; width:${wPct}%; background:${pctColor(wPct)}; opacity:.9;`;
      const div = document.createElement('span');
      div.style.cssText = 'position:absolute; left:0; right:0; top:3px; height:1px; background:rgba(255,255,255,.7);';
      bar.appendChild(top); bar.appendChild(bot); bar.appendChild(div);
    } else {
      const fill = document.createElement('span');
      fill.style.cssText = `position:absolute; left:0; top:0; bottom:0; width:${sPct}%; background:${pctColor(sPct)};`;
      bar.appendChild(fill);
    }
    bar.title = usageTooltip(usage, opts);
    return bar;
  }

  function makePeakBar(opts) {
    const p = peakInfo(new Date(), opts && opts.peakLeadMinutes);
    if (!p.visible) return null;
    const bar = document.createElement('span');
    const track = p.inPeak ? '#efe0d0' : '#ededed';
    bar.style.cssText = 'display:inline-block; position:relative; width:38px; height:8px; '
      + `border-radius:4px; background:${track}; overflow:hidden; cursor:help; vertical-align:middle;`;
    const fill = document.createElement('span');
    fill.style.cssText = `position:absolute; left:0; top:0; bottom:0; width:${clampPct(p.frac * 100)}%; background:#e67e22;`;
    bar.appendChild(fill);
    bar.title = p.inPeak
      ? `PEAK from ${p.start.toLocaleTimeString()} till ${p.end.toLocaleTimeString()}, ${fmtHMS(p.msLeft)} left`
      : `PEAK starts in ${fmtLeft(p.msUntil)} (from ${p.start.toLocaleTimeString()} till ${p.end.toLocaleTimeString()})`;
    return bar;
  }

  // The usage bar + (conditionally) the peak bar. The peak indicator is pure
  // local-clock math (no account data), so it renders even when usage is
  // unavailable (e.g. a profile not signed in to claude.ai).
  // Null only when there's neither usage data nor a visible peak window.
  function buildUsageBars(usage, opts) {
    const hasUsage = !!(usage && usage.session);
    const peak = makePeakBar(opts);
    if (!hasUsage && !peak) return null;
    const wrap = document.createElement('span');
    wrap.style.cssText = 'display:inline-flex; align-items:center; gap:4px; vertical-align:middle; margin:0 2px;';
    if (hasUsage) wrap.appendChild(makeUsageBar(usage, opts));
    if (peak) wrap.appendChild(peak);
    return wrap;
  }

  // Plain-text summary for the right-click copy path (bars don't survive a
  // paste). Mirrors the panel: a "usage n/a" marker when the fetch failed,
  // and the PEAK part even without usage data (both are panel behavior too).
  function usageSummaryText(usage, opts) {
    const bits = [];
    if (usage && usage.session) {
      let s = `session ${clampPct(usage.session.percent)}%`;
      if (usage.session.resetsAt) s += ` (resets in ${fmtLeft(new Date(usage.session.resetsAt) - Date.now())})`;
      if (opts && opts.showWeekly && usage.weekly) {
        s += ` · weekly ${clampPct(usage.weekly.percent)}%`;
        if (usage.weekly.resetsAt) s += ` (resets in ${fmtLeft(new Date(usage.weekly.resetsAt) - Date.now())})`;
      }
      bits.push(s);
    } else if (usage && usage.ok === false) {
      bits.push(`usage n/a (${usage.error || 'fetch failed'})`);
    }
    const p = peakInfo(new Date(), opts && opts.peakLeadMinutes);
    if (p.inPeak) bits.push(`PEAK ${fmtHMS(p.msLeft)} left`);
    else if (p.visible) bits.push(`PEAK in ${fmtLeft(p.msUntil)}`);
    return bits.join(' · ');
  }

  // ---- chart --------------------------------------------------------------
  // Drawn by usage-chart.js (shared with the OpenAI view). Without it the card still
  // renders, with a line saying why there is no chart.
  function buildUsageChart(usage, opts) {
    const UC = globalThis.UsageChart;
    if (UC) return UC.build(usage, { session: true, weekly: !!(opts && opts.showWeekly) });
    const p = document.createElement('div');
    p.style.cssText = 'font-size:10px; color:var(--text-muted, #999); margin:4px 0;';
    p.textContent = 'Usage chart unavailable (usage-chart.js did not load).';
    return p;
  }

  globalThis.AnthropicView = {
    peakInfo, isClaudeCode,
    fmtLeft, fmtHMS,
    buildUsageBars, buildUsageChart, usageSummaryText,
  };
})();
