/*
 * BrowserPlug: the popup's Usage pane
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * One card per provider (Anthropic, OpenAI, GitHub Copilot, DeepSeek), each with the
 * whole width the 38px bars on the client rows never had: a meter per window with its
 * reset time, the peak-hours clock, and Anthropic's usage-since-reset chart. Every
 * number comes from the provider's own view module (anthropic-view.js, openai-view.js,
 * copilot-view.js, deepseek-view.js), the same code that draws the row bars, so the two
 * places cannot disagree.
 *
 * A card says something in every state it is DRAWN in — a pane the user opened on
 * purpose cannot show an empty box. Two things draw no card: a provider turned off, and
 * an OpenAI or Copilot account this browser is signed out of. A signed-out Anthropic card
 * keeps only the peak-hours clock, which is local math and needs no account. Copilot on a
 * different GitHub account than the editor draws a card without numbers (they would be
 * someone else's) that says which account to switch to. In 'tab' mode nothing on the
 * client row says "other account", so this card is the only place the user learns it.
 * When every provider is off popup.js takes the tab off the strip
 * (applyUsageTabVisibility), because a tab whose only content explains its own emptiness
 * is worse than no tab; when they are on but all signed out, the pane keeps one line
 * saying so, since that state ends the moment the user signs in. A view module that did
 * not load is reported as such, not as a sign-out.
 * Loaded in the POPUP; publishes `UsagePane`. No data is fetched here: popup.js asks
 * the background and hands the answers in as `state`.
 */
(function () {
  'use strict';

  const view = (name) => globalThis[name] || null;
  // What a builder returns when its view module did not load: not a sign-out.
  const MISSING = Symbol('view module missing');
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const clampPct = (v) => clamp(Number(v) || 0, 0, 100);
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

  // fmtLeft from any loaded view; they all carry the same copy.
  function fmtLeft(ms) {
    const V = view('AnthropicView') || view('OpenAIView') || view('DeepSeekView');
    if (V && V.fmtLeft) return V.fmtLeft(ms);
    return `${Math.max(0, Math.round(ms / 60000))}m`;
  }

  // ---- DOM helpers ----------------------------------------------------------
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function card(title, pill) {
    const c = el('div', 'card usage-card');
    const head = el('div', 'card-head');
    head.appendChild(el('h2', null, title));
    if (pill) head.appendChild(el('span', `state ${pill.tone}`, pill.text));
    c.appendChild(head);
    return c;
  }

  function note(text) { return el('p', 'usage-note', text); }

  // The colour of each window length, shared with the chart's lines (usage-chart.js).
  const windowColor = (name) => ((view('UsageChart') || {}).COLORS || {})[name] || '';

  // A labelled bar: "Session   34% · resets in 2h 10m". `kind` picks the fill colour
  // (hot, peak); with none, `color` (the window's own) does, else the pane's accent.
  function meter(label, pct, right, kind, color) {
    const m = el('div', 'meter');
    const lbl = el('div', 'lbl');
    lbl.appendChild(el('b', null, label));
    lbl.appendChild(el('span', null, right));
    const track = el('div', 'track');
    const fill = el('div', 'fill' + (kind ? ` ${kind}` : ''));
    fill.style.width = `${clampPct(pct)}%`;
    if (color && !kind) fill.style.background = color;
    track.appendChild(fill);
    m.appendChild(lbl);
    m.appendChild(track);
    return m;
  }

  function windowRight(w) {
    let s = `${clampPct(w.percent)}%`;
    if (w.resetsAt) s += ` · resets in ${fmtLeft(new Date(w.resetsAt) - Date.now())}`;
    return s;
  }

  const heat = (pct) => (clampPct(pct) >= 80 ? 'hot' : '');

  // ---- Anthropic --------------------------------------------------------------
  function anthropicCard(s) {
    const AV = view('AnthropicView');
    const usage = s.usage;
    const opts = s.opts || {};
    if (!usage) {
      const c = card('Anthropic', { tone: 'unknown', text: 'waiting' });
      c.appendChild(note('Waiting for claude.ai… the first reading arrives with the next refresh.'));
      return peakLine(c, AV, opts);
    }
    if (usage.signedOut) {
      const c = peakLine(card('Anthropic', { tone: 'unknown', text: 'signed out' }), AV, opts);
      return c.querySelector('.meter, .usage-note') ? c : null;
    }
    if (usage.ok === false || !usage.session) {
      const c = card('Anthropic', { tone: 'denied', text: usage.ok === false ? 'unavailable' : 'no data' });
      c.appendChild(note(usage.ok === false
        ? `Account usage unavailable: ${usage.error || 'fetch failed'}. Sign in to claude.ai in this browser profile to restore it.`
        : 'No usage data yet.'));
      return peakLine(c, AV, opts);
    }
    const c = card('Anthropic', { tone: 'granted', text: 'signed in' });
    c.appendChild(meter('Session', usage.session.percent, windowRight(usage.session), heat(usage.session.percent),
      windowColor('session')));
    if (usage.weekly) {
      c.appendChild(meter('Week', usage.weekly.percent, windowRight(usage.weekly), heat(usage.weekly.percent),
        windowColor('weekly')));
    }
    if (usage.stale) c.appendChild(note('Last refresh failed; these numbers are the previous reading.'));
    peakLine(c, AV, opts);
    if (AV && AV.buildUsageChart && usage.sessionWindow) {
      const chart = AV.buildUsageChart(usage, { showWeekly: !!usage.weekly });
      chart.classList.add('usage-chart');
      c.appendChild(chart);
    }
    return c;
  }

  // Anthropic's peak hours, in the reader's clock. Appended to whichever Anthropic card
  // is showing: the clock is local math and needs no account.
  function peakLine(c, AV, opts) {
    if (!AV || !AV.peakInfo) return c;
    const p = AV.peakInfo(new Date(), opts && opts.peakLeadMinutes);
    if (p.inPeak) {
      c.appendChild(meter('Peak hours', p.frac * 100,
        `PEAK · ${AV.fmtHMS ? AV.fmtHMS(p.msLeft) : fmtLeft(p.msLeft)} left`, 'peak'));
    } else if (isFinite(p.msUntil)) {
      c.appendChild(note(`Off-peak · next PEAK in ${fmtLeft(p.msUntil)} `
        + `(${p.start.toLocaleTimeString()} to ${p.end.toLocaleTimeString()} your time, weekdays).`));
    }
    return c;
  }

  // ---- OpenAI -------------------------------------------------------------------
  function openaiCard(s) {
    const usage = s.usage;
    if (!usage) {
      const c = card('OpenAI', { tone: 'unknown', text: 'waiting' });
      c.appendChild(note('Waiting for chatgpt.com… the first reading arrives with the next refresh.'));
      return c;
    }
    if (usage.signedOut) return null;
    if (usage.ok === false || !usage.session) {
      const c = card('OpenAI', { tone: 'denied', text: 'unavailable' });
      const n = note(`Account usage unavailable: ${usage.error || 'fetch failed'}. Sign in at `);
      const a = el('a', null, 'chatgpt.com');
      a.href = 'https://chatgpt.com/';
      a.target = '_blank';
      a.rel = 'noreferrer';
      n.appendChild(a);
      n.appendChild(document.createTextNode(' in this browser to see the 5-hour and 7-day windows here.'));
      c.appendChild(n);
      return c;
    }
    const c = card('OpenAI', usage.limitReached
      ? { tone: 'unknown', text: 'limit reached' }
      : { tone: 'granted', text: usage.plan ? String(usage.plan) : 'signed in' });
    const kind = (pct) => (usage.limitReached ? 'hot' : heat(pct));
    c.appendChild(meter(cap(usage.session.windowName || 'session'), usage.session.percent,
      windowRight(usage.session), kind(usage.session.percent), windowColor('session')));
    if (usage.weekly) {
      c.appendChild(meter(cap(usage.weekly.windowName || 'weekly'), usage.weekly.percent,
        windowRight(usage.weekly), kind(usage.weekly.percent), windowColor('weekly')));
    }
    if (usage.limitReached) {
      c.appendChild(note(usage.reachedType
        ? `Limit reached (${usage.reachedType}); requests are being refused.`
        : 'Limit reached; requests are being refused.'));
    }
    if (usage.stale) c.appendChild(note('Last refresh failed; these numbers are the previous reading.'));
    const OV = view('OpenAIView');
    const chart = OV && OV.buildUsageChart ? OV.buildUsageChart(usage) : null;
    if (chart) {
      chart.classList.add('usage-chart');
      c.appendChild(chart);
    }
    c.appendChild(note('ChatGPT and Codex share this allowance.'));
    return c;
  }

  // ---- GitHub Copilot -------------------------------------------------------------
  // s.rows are the connected Copilot Chat rows. Their `copilot` blocks are what VS Code
  // is doing, and each one's account is checked against the account the quota is for.
  function copilotCard(s) {
    const CV = view('CopilotView');
    if (!CV) return MISSING;
    const usage = s.usage;
    const rows = (s.rows || []).filter(r => r && r.copilot);
    if (usage && usage.signedOut) return null;
    const other = usage && usage.ok !== false ? rows.find(r => CV.accountMismatch(usage, r)) : null;
    let c;
    if (other) {
      // The quota read is another account's, so no meters: just which one to switch to.
      c = card('GitHub Copilot', { tone: 'unknown', text: 'other account' });
      c.appendChild(note(CV.mismatchText(usage, other)));
    } else if (!usage) {
      c = card('GitHub Copilot', { tone: 'unknown', text: 'waiting' });
      c.appendChild(note('Waiting for github.com… the first reading arrives with the next refresh.'));
    } else if (usage.ok === false) {
      c = card('GitHub Copilot', { tone: 'denied', text: 'unavailable' });
      const n = note(`Copilot usage unavailable: ${usage.error || 'fetch failed'}. Sign in at `);
      const a = el('a', null, 'github.com');
      a.href = 'https://github.com/login';
      a.target = '_blank';
      a.rel = 'noreferrer';
      n.appendChild(a);
      n.appendChild(document.createTextNode(' in this browser to see the monthly quotas here.'));
      c.appendChild(n);
    } else {
      c = card('GitHub Copilot', { tone: 'granted', text: usage.plan ? String(usage.plan) : 'signed in' });
      // "0.4% · 1 of 200 · resets in 2d 7h": the reset reads like Anthropic's, on the meter.
      const left = CV.resetLeft(usage);
      const quotaRight = (q) => (q.unlimited ? 'unlimited'
        : `${clampPct(q.percent)}% · ${q.used} of ${q.total}` + (left ? ` · resets in ${left}` : ''));
      const p = CV.primaryQuota(usage);
      if (p) c.appendChild(meter(p.name, p.q.percent, quotaRight(p.q), heat(p.q.percent), windowColor('monthly')));
      if (usage.completions) {
        c.appendChild(usage.completions.unlimited
          ? note('Code completions: unlimited.')
          : meter('Code completions', usage.completions.percent, quotaRight(usage.completions),
            heat(usage.completions.percent), windowColor('completions')));
      }
      if (!p && !usage.completions) c.appendChild(note('This account has no metered Copilot quota.'));
      const chart = CV.buildUsageChart ? CV.buildUsageChart(usage) : null;
      if (chart) {
        chart.classList.add('usage-chart');
        c.appendChild(chart);
      }
      if (usage.stale) c.appendChild(note('Last refresh failed; these numbers are the previous reading.'));
    }
    // One line per connected VS Code window: what its Copilot Chat is doing now.
    for (const r of rows) {
      // The workspace is in the heading; the editor's account is only news when it differs,
      // and that card says so itself.
      const lines = CV.sessionLines(r).filter(l => !/^(Workspace|VS Code Copilot account): /.test(l));
      const where = r.copilot.workspace ? `VS Code (${r.copilot.workspace})` : 'VS Code';
      if (lines.length) c.appendChild(note(`${where}: ${lines.join(' · ')}`));
    }
    return c;
  }

  // ---- DeepSeek -----------------------------------------------------------------
  function deepseekCard(s) {
    const DV = view('DeepSeekView');
    if (!DV || !DV.peakInfo) return MISSING;
    const opts = s.opts || {};
    const now = new Date();
    const p = DV.peakInfo(now, opts.peakLeadMinutes);
    let c;
    if (p.inPeak) {
      c = card('DeepSeek', { tone: 'unknown', text: 'peak' });
      c.appendChild(meter('Peak window', p.frac * 100, `double price · ${DV.fmtHMS(p.msLeft)} left`, 'peak'));
    } else {
      c = card('DeepSeek', { tone: 'granted', text: 'off-peak' });
      c.appendChild(note(isFinite(p.msUntil)
        ? `Half price · next PEAK in ${DV.fmtLeft(p.msUntil)}.`
        : 'Half price.'));
    }
    const windows = DV.localWindows ? DV.localWindows(now) : null;
    c.appendChild(note((windows ? `Peak windows: ${windows} your time ` : 'Peak windows: ')
      + '(01:00–04:00 and 06:00–10:00 UTC, Monday to Friday). Off-peak is a flat half price.'));
    return c;
  }

  const BUILDERS = {
    anthropic: anthropicCard, openai: openaiCard, copilot: copilotCard, deepseek: deepseekCard,
  };
  const NAMES = { anthropic: 'Anthropic', openai: 'OpenAI', copilot: 'GitHub Copilot', deepseek: 'DeepSeek' };

  // ---- targeted update ----------------------------------------------------------
  // The pane is redrawn every couple of seconds, and nearly all of it is the same each time:
  // only a countdown, a percentage or the chart's "now" marker moves. Replacing the whole
  // tree made the chart flash and dropped hover, text selection and a link under the
  // pointer. So each tick is built off screen as before, and then laid over the live tree:
  // a node of the same kind stays and gets only the attributes and text that differ.
  function syncAttributes(live, fresh) {
    for (const a of Array.from(fresh.attributes)) {
      if (live.getAttribute(a.name) !== a.value) live.setAttribute(a.name, a.value);
    }
    for (const a of Array.from(live.attributes)) {
      if (!fresh.hasAttribute(a.name)) live.removeAttribute(a.name);
    }
  }

  function patchNode(live, fresh) {
    if (live.nodeType !== fresh.nodeType || live.nodeName !== fresh.nodeName) {
      live.replaceWith(fresh);
    } else if (live.nodeType === 1) {
      syncAttributes(live, fresh);
      patchChildren(live, Array.from(fresh.childNodes));
    } else if (live.nodeValue !== fresh.nodeValue) {
      live.nodeValue = fresh.nodeValue;
    }
  }

  // `wanted` is a snapshot: appending a node moves it out of the tree it was built in.
  function patchChildren(parent, wanted) {
    wanted.forEach((n, i) => {
      const live = parent.childNodes[i];
      if (live) patchNode(live, n); else parent.appendChild(n);
    });
    while (parent.childNodes.length > wanted.length) parent.lastChild.remove();
  }

  /**
   * Render the pane into `host`, changing only what differs from what is already there.
   *
   * @param {HTMLElement} host
   * @param {object} state  { anthropic: {mode, usage, opts}, openai: {mode, usage, opts},
   *                          copilot: {mode, usage, opts, rows}, deepseek: {mode, opts} };
   *                          a missing provider, and one turned off, both draw no card.
   */
  function render(host, state) {
    if (!host) return;
    const out = [];
    const missing = [];
    let hidden = 0;
    for (const key of Object.keys(BUILDERS)) {
      const s = state && state[key];
      if (!s || s.mode === 'disabled') continue;
      const c = BUILDERS[key](s);
      if (c === MISSING) missing.push(NAMES[key]);
      else if (c) out.push(c);
      else hidden++;
    }
    if (!out.length && hidden) {
      out.push(note('Nothing to show: every enabled provider is signed out in this browser.'));
    }
    if (missing.length) {
      out.push(note(`The ${missing.join(' and ')} panel did not load; reopen the popup, or `
        + 'reload the extension if it persists.'));
    }
    if (!out.length) out.push(note('No usage sources are loaded.'));
    patchChildren(host, out);
  }

  globalThis.UsagePane = { render };
})();
