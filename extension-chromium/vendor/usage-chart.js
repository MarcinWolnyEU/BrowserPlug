/*
 * BrowserPlug: usage chart (the drawing half of the period history)
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Loaded in the POPUP; publishes `UsageChart`. anthropic-view.js and openai-view.js build
 * their expandable chart through it. ONE plot carries both windows, each line on its own
 * time axis, because a weekly percentage drawn on the 5-hour axis is a short flat stub:
 *   - the session line (blue) spans the 5-hour window; its axis labels read HH:MM;
 *   - the weekly line (green, the plug in the logo) spans the current weekly period, which
 *     period-history.js shortens after a reset granted mid-week; its labels read Day HH:MM.
 *   - the monthly line (violet, GitHub Copilot's quota) spans the current month's period;
 *     its labels read D Mon HH:MM.
 *   - the completions line (teal, Copilot's code-completions quota) rides the same month.
 * Lines whose windows are the same share ONE axis row and one "now" marker, so Copilot's two
 * quotas do not print the same dates twice.
 * One colour per window length, everywhere: the usage pane paints each meter with the
 * colour of the same window's line (COLORS), so a meter and its line read as one thing.
 * Red stays the "nearly full" signal and orange the peak-hours clock, so neither is a
 * window colour. Each axis sits under the plot in its line's colour, with its own "now" marker. Both axes
 * run over the same width, so the dashed diagonal is the steady pace for either.
 */
(function () {
  'use strict';

  const SVGNS = 'http://www.w3.org/2000/svg';
  const SESSION_COLOR = 'var(--accent, #2b6cb0)';
  const WEEKLY_COLOR = '#43a047';   // the connected plug in assets/icons (render.py GREEN)
  const MONTHLY_COLOR = '#8e5fd0';  // amber is taken by the heat colours, orange by the peak clock
  const COMPLETIONS_COLOR = '#00897b';  // teal: green is the weekly line, blue the session one
  const COLORS = { session: SESSION_COLOR, weekly: WEEKLY_COLOR, monthly: MONTHLY_COLOR, completions: COMPLETIONS_COLOR };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const clampPct = (v) => clamp(Number(v) || 0, 0, 100);

  function svgEl(name, attrs) {
    const el = document.createElementNS(SVGNS, name);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }
  function svgLine(x1, y1, x2, y2, stroke, w, dash) {
    const a = { x1, y1, x2, y2, stroke, 'stroke-width': w, 'vector-effect': 'non-scaling-stroke' };
    if (dash) a['stroke-dasharray'] = dash;
    return svgEl('line', a);
  }

  const hhmm = (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const dayHhmm = (t) => `${new Date(t).toLocaleDateString([], { weekday: 'short' })} ${hhmm(t)}`;

  const dayMonthHhmm = (t) => `${new Date(t).toLocaleDateString([], { day: 'numeric', month: 'short' })} ${hhmm(t)}`;

  const usable = (win) => !!win && win.end > win.start;

  // A window that is not running now (the session ended and no new one has started, or the
  // stored window is not yet current) must not print its hours as if it were: the axis shows
  // "-" and skips the "now" marker, which would sit pinned to an edge.
  const running = (win, now) => now >= win.start && now < win.end;

  function span(text, css) {
    const s = document.createElement('span');
    s.textContent = text;
    if (css) s.style.cssText = css;
    return s;
  }

  // One line of the plot and its axis: `sel` is the history field, `fmt` the axis label.
  function seriesOf(name, win, hist, sel, color, fmt) {
    return { name, win, hist, sel, color, fmt };
  }

  /**
   * @param {object} usage  a store's reading: history/sessionWindow, weeklyHistory/weeklyWindow,
   *        monthlyHistory/monthlyWindow, completionsHistory/completionsWindow
   * @param {{session?: boolean, weekly?: boolean, monthly?: boolean, monthlyLabel?: string,
   *        completions?: boolean}} o  which lines (`monthlyLabel` names the monthly one, which
   *        is whichever quota the provider charts); the weekly one also needs `usage.weekly`,
   *        and any is skipped while it has no window yet
   */
  function build(usage, o) {
    o = o || {};
    const all = [];
    if (o.session) {
      all.push(seriesOf('Session', usage && usage.sessionWindow,
        (usage && Array.isArray(usage.history)) ? usage.history : [], 'session', SESSION_COLOR, hhmm));
    }
    if (o.weekly && usage && usage.weekly) {
      all.push(seriesOf('Weekly', usage.weeklyWindow,
        Array.isArray(usage.weeklyHistory) ? usage.weeklyHistory : [], 'weekly', WEEKLY_COLOR, dayHhmm));
    }
    if (o.monthly && usage) {
      all.push(seriesOf(o.monthlyLabel || 'Monthly', usage.monthlyWindow,
        Array.isArray(usage.monthlyHistory) ? usage.monthlyHistory : [], 'monthly', MONTHLY_COLOR, dayMonthHhmm));
    }
    if (o.completions && usage) {
      all.push(seriesOf('Code completions', usage.completionsWindow,
        Array.isArray(usage.completionsHistory) ? usage.completionsHistory : [], 'completions', COMPLETIONS_COLOR,
        dayMonthHhmm));
    }
    const series = all.filter(s => usable(s.win));
    // One axis row (and one "now" marker) per distinct window; the first line to claim it
    // lends its colour.
    const axes = [];
    for (const s of series) {
      let ax = axes.find(a => a.win.start === s.win.start && a.win.end === s.win.end);
      if (!ax) axes.push(ax = { win: s.win, fmt: s.fmt, color: s.color, samples: 0 });
      ax.samples = Math.max(ax.samples, s.hist.length);
    }

    const wrap = document.createElement('div');
    wrap.style.cssText = 'margin:4px auto 2px; width:90%; box-sizing:border-box; '
      + 'border:1px solid var(--border, #e0e0e0); border-radius:4px; padding:6px; background:var(--card-bg, #fafafa);';

    const head = document.createElement('div');
    head.style.cssText = 'font-size:10px; color:var(--text-muted, #666); margin-bottom:3px; display:flex; gap:8px; flex-wrap:wrap;';
    head.appendChild(span('Usage since reset'));
    for (const s of all) head.appendChild(span(`● ${s.name}`, `color:${s.color}; font-weight:600;`));
    wrap.appendChild(head);

    if (!series.length) {
      wrap.appendChild(span('No usage window yet. Will fill in after the next refresh.',
        'display:block; font-size:10px; color:var(--text-muted, #999);'));
      return wrap;
    }

    const W = 300, H = 100, pad = 4;
    const x = (win, t) => pad + (W - 2 * pad) * clamp((t - win.start) / (win.end - win.start), 0, 1);
    const y = (pct) => pad + (H - 2 * pad) * (1 - clampPct(pct) / 100);

    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none' });
    svg.style.cssText = 'display:block; width:100%; height:96px;';
    [25, 50, 75].forEach(g => svg.appendChild(svgLine(pad, y(g), W - pad, y(g), 'var(--border, #ececec)', 1)));
    svg.appendChild(svgEl('rect', { x: pad, y: pad, width: W - 2 * pad, height: H - 2 * pad, fill: 'none', stroke: 'var(--border-strong, #ddd)', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke' }));
    svg.appendChild(svgLine(pad, y(0), W - pad, y(100), 'var(--text-muted, #bbb)', 1, '4 3'));

    for (const s of series) {
      const pts = s.hist.filter(h => h[s.sel] != null);
      if (pts.length) {
        svg.appendChild(svgEl('polyline', {
          points: pts.map(h => `${x(s.win, h.t).toFixed(1)},${y(h[s.sel]).toFixed(1)}`).join(' '),
          fill: 'none', stroke: s.color, 'stroke-width': 1.6, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke',
        }));
        const last = pts[pts.length - 1];
        svg.appendChild(svgEl('circle', { cx: x(s.win, last.t), cy: y(last[s.sel]), r: 2.5, fill: s.color }));
      }
    }
    const now = Date.now();
    for (const a of axes) {
      if (!running(a.win, now)) continue;
      const nowX = x(a.win, now);
      svg.appendChild(svgLine(nowX, pad, nowX, H - pad, a.color, 1, '2 2'));
    }
    wrap.appendChild(svg);

    // One axis row per window, in its line's colour: where it starts and ends.
    for (const a of axes) {
      const foot = document.createElement('div');
      foot.className = 'usage-axis';
      foot.style.cssText = `display:flex; justify-content:space-between; gap:6px; font-size:9px; color:${a.color}; margin-top:2px;`;
      const live = running(a.win, now);
      foot.appendChild(span(live ? a.fmt(a.win.start) : '-'));
      foot.appendChild(span(a.samples < 2 ? 'collecting samples…' : `${a.samples} samples`, 'text-align:center;'));
      foot.appendChild(span(live ? a.fmt(a.win.end) : '-'));
      wrap.appendChild(foot);
    }
    return wrap;
  }

  globalThis.UsageChart = { build, COLORS };
})();
