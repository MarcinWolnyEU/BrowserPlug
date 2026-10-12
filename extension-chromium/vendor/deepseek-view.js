/*
 * BrowserPlug: DeepSeek (dsh) peak pricing: VIEW layer
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Display helpers for the popup's Connections panel: the peak bar on a DeepSeek
 * Harness row, peak-hours math, and formatting. Loaded in the POPUP; publishes
 * `DeepSeekView`. There is no data half — the windows are fixed and the local clock
 * is the whole input, so nothing is fetched, parsed or stored.
 *
 * Laid out as a sibling of anthropic-view.js, section for section, because it is the
 * same widget in the same panel: same bar, same three states (absent outside peak, a
 * grey bar during the user's chosen lead, the filling tan bar during peak). What
 * differs is the schedule and what it costs. Anthropic's peak hours are about
 * capacity and slow responses; DeepSeek's are about money — the API bills DOUBLE
 * inside its windows — and they run twice a day, Monday to Friday. One function
 * answering for both would have to hedge on every word it prints, and the two leads
 * are separately useful: latency you wait out, a doubled bill you plan around.
 */
(function () {
  'use strict';

  // Peak = double price. Minutes into the UTC day, [start, end). Everything outside
  // these two windows is off-peak, and so is the whole weekend: DeepSeek stopped
  // billing peak on Saturdays and Sundays, so the schedule now runs Monday to Friday.
  // Source: DeepSeek's own pricing page, https://api-docs.deepseek.com/quick_start/pricing/
  // (the trailing slash matters; without it the docs site serves a different shell):
  // "Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday (all
  // other hours are off-peak)." Off-peak is a flat half price across input and output.
  const PEAK_WINDOWS = [[60, 240], [360, 600]];
  const isWeekday = (dow) => dow >= 1 && dow <= 5;

  // ---- formatting ---------------------------------------------------------
  // Both formatters are anthropic-view.js's, copied rather than imported: the two
  // views load independently (popup.html lists them separately, and either can be
  // absent), so sharing would make this module fail to render whenever the other one
  // is missing. Keep them in step by hand if that file's change.

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

  // Clock-style "H:MM:SS" (peak "… left").
  function fmtHMS(ms) {
    if (!(ms > 0)) ms = 0;
    let s = Math.floor(ms / 1000);
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); const ss = s % 60;
    return `${h}:${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}`;
  }

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const clampPct = (v) => clamp(Number(v) || 0, 0, 100);
  const hhmm = (d) => `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;

  // ---- peak hours ---------------------------------------------------------
  // DeepSeek peak hours: 01:00–04:00 and 06:00–10:00 UTC (= 03:00–06:00 and
  // 08:00–12:00 Europe/Warsaw), Monday to Friday, per api-docs.deepseek.com.
  // The weekend is off-peak from end to end, which is the one point where this
  // schedule and Anthropic's agree — for opposite reasons: theirs marks when
  // everybody is working, this one marks when DeepSeek charges extra for it.
  // `visible` is true during peak OR within `leadMinutes` before the next peak
  // begins — the same rule, and the same returned keys, as AnthropicView.peakInfo.
  //
  // The weekday test reads the UTC day, because the windows are UTC. Far enough west
  // that matters: 01:00 UTC Monday is Sunday evening locally, and the bar says PEAK
  // there because the bill does.
  const minuteOfUtcDay = (d) => d.getUTCHours() * 60 + d.getUTCMinutes();

  // The UTC instant `minute` minutes into `ref`'s day, rolled to the next day when
  // `forward` and that instant has already passed.
  function utcInstant(ref, minute, forward) {
    const at = new Date(ref);
    at.setUTCHours(0, Math.round(minute), 0, 0);
    if (forward && at <= ref) at.setUTCDate(at.getUTCDate() + 1);
    return at;
  }

  // The first window that opens strictly after `now`, scanned a day at a time and
  // skipping Saturday and Sunday. A modulo of the day length cannot do this any more:
  // the gap is no longer at most 24 hours, and Friday 10:00 has to reach Monday 01:00.
  // Bounded at 8 days so a nonsense clock draws no bar instead of hanging the popup.
  function nextWindow(now) {
    for (let dayOffset = 0; dayOffset <= 8; dayOffset++) {
      let best = null;
      for (const [from, to] of PEAK_WINDOWS) {
        const at = new Date(now);
        at.setUTCHours(0, 0, 0, 0);
        at.setUTCDate(at.getUTCDate() + dayOffset);
        at.setUTCMinutes(from);          // `from` is minutes into the day; Date normalizes
        if (at <= now || !isWeekday(at.getUTCDay())) continue;
        if (!best || at < best.start) best = { start: at, from, to };
      }
      if (best) return best;
    }
    return null;
  }

  function peakInfo(now = new Date(), leadMinutes = 0) {
    const minute = minuteOfUtcDay(now);
    const window = isWeekday(now.getUTCDay())
      ? PEAK_WINDOWS.find(([from, to]) => minute >= from && minute < to)
      : null;
    if (window) {
      const start = utcInstant(now, window[0], false);
      const end = utcInstant(now, window[1], false);
      return { inPeak: true, visible: true, frac: (now - start) / (end - start), start, end, msLeft: end - now };
    }
    const next = nextWindow(now);
    // Unreachable with a working clock (eight days always contain a weekday), and
    // shaped so the callers' `visible` test still answers: no bar, no summary line.
    if (!next) return { inPeak: false, visible: false, frac: 0, start: null, end: null, msUntil: Infinity };
    const end = new Date(next.start.getTime() + (next.to - next.from) * 60000);
    const msUntil = next.start - now;
    const leadMs = Math.max(0, Number(leadMinutes) || 0) * 60000;
    return { inPeak: false, visible: msUntil <= leadMs, frac: 0, start: next.start, end, msUntil };
  }

  // The windows in the reader's OWN clock, which is the only form anybody can act on.
  // Derived from the UTC table rather than written down beside it, so the two can
  // never disagree, and against today's date because a UTC offset is not a constant
  // (DST moves it twice a year).
  function localWindows(now = new Date()) {
    return PEAK_WINDOWS.map(([from, to]) =>
      `${hhmm(utcInstant(now, from, false))}–${hhmm(utcInstant(now, to, false))}`).join(' and ');
  }

  // ---- classification -----------------------------------------------------
  // The row's own name is the short one the panel prints; the long spelling is what
  // servers older than the rename send, and a popup ahead of its server must not
  // silently drop the bar.
  function isDeepSeekHarness(c) {
    if (!c || c.kind !== 'mcp_client') return false;
    const label = c.label || '';
    return c.friendly === 'DSH' || label.startsWith('DSH')
      || c.friendly === 'DeepSeek Harness' || label.startsWith('DeepSeek Harness');
  }

  // ---- bars ---------------------------------------------------------------
  // The first line is the one anthropic-view.js writes, word for word, so the two
  // indicators read alike on hover as well as on screen. The rest is DeepSeek's own:
  // what the window costs, and where it falls in both clocks (the schedule is fixed
  // in UTC and nobody works in it).
  function peakTooltip(p, now) {
    const head = p.inPeak
      ? `PEAK from ${p.start.toLocaleTimeString()} till ${p.end.toLocaleTimeString()}, ${fmtHMS(p.msLeft)} left`
      : `PEAK starts in ${fmtLeft(p.msUntil)} (from ${p.start.toLocaleTimeString()} till ${p.end.toLocaleTimeString()})`;
    return head
      + (p.inPeak ? '\nDouble price until it ends.' : '\nHalf price until it starts.')
      + `\nPeak windows: ${localWindows(now)} your time`
      + '\n(01:00–04:00 and 06:00–10:00 UTC, Monday–Friday)';
  }

  // Byte for byte the bar makePeakBar() builds in anthropic-view.js, tooltip aside:
  // a reader should not have to learn a second shape, or a second rule about when it
  // appears, to read the same kind of fact one row down.
  function makePeakBar(opts) {
    const now = new Date();
    const p = peakInfo(now, opts && opts.peakLeadMinutes);
    if (!p.visible) return null;
    const bar = document.createElement('span');
    const track = p.inPeak ? '#efe0d0' : '#ededed';
    bar.style.cssText = 'display:inline-block; position:relative; width:38px; height:8px; '
      + `border-radius:4px; background:${track}; overflow:hidden; cursor:help; vertical-align:middle;`;
    const fill = document.createElement('span');
    fill.style.cssText = `position:absolute; left:0; top:0; bottom:0; width:${clampPct(p.frac * 100)}%; background:#e67e22;`;
    bar.appendChild(fill);
    bar.title = peakTooltip(p, now);
    return bar;
  }

  // The peak bar in the panel's inline slot. Null outside the peak window (and its
  // lead), so the row renders as if the feature were not there rather than leaving a
  // gap — the same contract buildUsageBars() has when there is nothing to show.
  function buildPeakBars(opts) {
    const peak = makePeakBar(opts);
    if (!peak) return null;
    const wrap = document.createElement('span');
    wrap.style.cssText = 'display:inline-flex; align-items:center; gap:4px; vertical-align:middle; margin:0 2px;';
    wrap.appendChild(peak);
    return wrap;
  }

  // Plain-text summary for the right-click copy path (bars don't survive a paste).
  // Mirrors the panel, including its silence when no window is in view.
  function peakSummaryText(opts) {
    const p = peakInfo(new Date(), opts && opts.peakLeadMinutes);
    if (p.inPeak) return `PEAK ${fmtHMS(p.msLeft)} left`;
    return p.visible ? `PEAK in ${fmtLeft(p.msUntil)}` : '';
  }

  globalThis.DeepSeekView = {
    peakInfo, isDeepSeekHarness,
    fmtLeft, fmtHMS,
    buildPeakBars, peakSummaryText,
    PEAK_WINDOWS, localWindows,
  };
})();
