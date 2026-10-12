/*
 * BrowserPlug: GitHub Copilot usage and session: VIEW layer
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 *
 * Display helpers for a GitHub Copilot Chat row in the popup's Connections panel, for
 * its Usage tab card, and for the plain-text copy path. Loaded in the POPUP; publishes
 * `CopilotView`. Two sources meet here:
 *
 *   - usage: the github.com account's monthly quotas, fetched in the background by
 *     copilot-data.js with this browser's cookies;
 *   - the row's `copilot` block: what VS Code itself is doing (model, account, current
 *     chat, workspace), read server-side from the editor's own log and session store
 *     by server/usage_panels/copilot_data.py, and only while "Load session details" is on.
 *
 * They can describe different GitHub accounts, since a browser can hold several and
 * the editor signs in on its own. When both logins are known and differ, the row shows
 * an "other account" marker instead of the bar: one account's quota drawn on another
 * account's row reads as a fact and is wrong.
 *
 * The bar is anthropic-view.js's widget (same size, colours and split) so the same
 * kind of fact reads the same way one row down.
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
  const SIGNED_OUT_TEXT = 'not signed in to github.com';
  const fmtNum = (n) => (Math.round((Number(n) || 0) * 10) / 10).toLocaleString('en-US');

  // ---- classification -------------------------------------------------------
  // The server names the row (client_identity.friendly_from_cmdline); the label prefix
  // is accepted too, for a popup running ahead of its server.
  function isCopilotClient(c) {
    if (!c || c.kind !== 'mcp_client') return false;
    return c.friendly === 'GitHub Copilot Chat' || (c.label || '').startsWith('GitHub Copilot Chat');
  }

  // ---- quotas -----------------------------------------------------------------

  // The quota the bar shows. Paid plans meter premium requests and leave chat
  // unlimited; Copilot Free has no premium quota and meters chat messages instead.
  function primaryQuota(usage) {
    if (!usage) return null;
    if (usage.premium && !usage.premium.unlimited) return { name: 'Premium requests', q: usage.premium };
    if (usage.chat && !usage.chat.unlimited) return { name: 'Chat', q: usage.chat };
    return null;
  }

  // Code completions ride a second half of the bar only when the option asks for it
  // and the plan actually caps them.
  function secondaryQuota(usage, opts) {
    if (!(opts && opts.showCompletions) || !usage) return null;
    const q = usage.completions;
    return q && !q.unlimited ? { name: 'Code completions', q } : null;
  }

  function quotaLine(name, q) {
    if (q.unlimited) return `${name}: unlimited`;
    return `${name} ${clampPct(q.percent)}% (${fmtNum(q.used)} of ${fmtNum(q.total)})`;
  }

  // How long until the monthly quotas reset ('2d 7h'), or '' when GitHub gave no date.
  function resetLeft(usage) {
    return usage && usage.resetsAt ? fmtLeft(new Date(usage.resetsAt) - Date.now()) : '';
  }

  function resetText(usage) {
    const left = resetLeft(usage);
    return left ? `resets in ${left}` : '';
  }

  // ---- accounts ---------------------------------------------------------------

  const editorAccount = (conn) => (conn && conn.copilot && conn.copilot.account) || null;

  // True only when BOTH logins are known and differ. An unknown side is not a
  // mismatch: session details may be off, or the editor has not signed in yet.
  function accountMismatch(usage, conn) {
    const editor = editorAccount(conn);
    const browser = usage && usage.ok !== false ? usage.login : null;
    return !!(editor && browser && editor.toLowerCase() !== String(browser).toLowerCase());
  }

  function mismatchText(usage, conn) {
    return `This browser is signed in to github.com as ${usage.login}, but VS Code's Copilot `
      + `uses ${editorAccount(conn)}, so the quota here is not the editor's. Switch to `
      + `${editorAccount(conn)} on github.com (account menu) to read it.`;
  }

  // ---- bar --------------------------------------------------------------------

  function usageTooltip(usage, opts) {
    const lines = [];
    const p = primaryQuota(usage);
    if (p) lines.push(quotaLine(p.name, p.q));
    if (usage.completions && (opts && opts.showCompletions)) lines.push(quotaLine('Code completions', usage.completions));
    const left = resetLeft(usage);
    if (left) lines.push(`Monthly quotas reset in ${left}`);
    if (usage.plan) lines.push(`Plan: ${usage.plan}`);
    if (usage.login) lines.push(`github.com account: ${usage.login}`);
    if (usage.stale) lines.push('Last refresh failed; this is the previous reading.');
    return lines.join('\n');
  }

  function makeUsageBar(usage, opts) {
    const p = primaryQuota(usage);
    const sec = secondaryQuota(usage, opts);
    const bar = document.createElement('span');
    bar.style.cssText = 'display:inline-block; position:relative; width:38px; height:8px; '
      + 'border-radius:4px; background:#e6e6e6; overflow:hidden; cursor:help; vertical-align:middle;';
    const fill = (pct, css) => {
      const f = document.createElement('span');
      f.style.cssText = `position:absolute; left:0; width:${pct}%; background:${pctColor(pct)}; ${css}`;
      return f;
    };
    const pPct = clampPct(p.q.percent);
    if (sec) {
      bar.appendChild(fill(pPct, 'top:0; height:4px;'));
      bar.appendChild(fill(clampPct(sec.q.percent), 'bottom:0; height:4px; opacity:.9;'));
      const div = document.createElement('span');
      div.style.cssText = 'position:absolute; left:0; right:0; top:3px; height:1px; background:rgba(255,255,255,.7);';
      bar.appendChild(div);
    } else {
      bar.appendChild(fill(pPct, 'top:0; bottom:0;'));
    }
    bar.title = usageTooltip(usage, opts);
    return bar;
  }

  function marker(text, title) {
    const na = document.createElement('span');
    na.className = 'conn-na';
    na.textContent = text;
    na.title = title;
    return na;
  }

  // ---- session ----------------------------------------------------------------

  // What VS Code is doing, one fact per line, for the badge tooltip and the pane.
  function sessionLines(conn) {
    const cs = (conn && conn.copilot) || null;
    if (!cs) return [];
    const lines = [];
    const model = (conn.model_name || conn.model || cs.model);
    if (model) lines.push(`Model: ${model}`);
    if (cs.chat_title) {
      lines.push(`Chat: "${cs.chat_title}"` + (cs.chat_turns ? ` · ${cs.chat_turns} turn${cs.chat_turns === 1 ? '' : 's'}` : ''));
    }
    if (cs.requests) {
      lines.push(`${cs.requests} chat request${cs.requests === 1 ? '' : 's'} in this window`
        + (cs.last_request_hms ? `, last at ${cs.last_request_hms}` : ''));
    }
    if (cs.account) lines.push(`VS Code Copilot account: ${cs.account}` + (cs.sku ? ` (${cs.sku})` : ''));
    if (cs.workspace) lines.push(`Workspace: ${cs.workspace}`);
    return lines;
  }

  // "◆ gpt-6-luna", with everything else about the session in its tooltip. Null when
  // the server sent no session block (details off, or nothing found).
  function buildSessionBadge(conn) {
    const cs = conn && conn.copilot;
    if (!cs) return null;
    const model = conn.model_name || conn.model || cs.model;
    const span = document.createElement('span');
    span.style.cssText = 'display:inline-flex; align-items:center; vertical-align:middle; '
      + 'margin:0 2px; padding:0 5px; border-radius:8px; background:var(--pill-neutral-bg, #eef1f6); '
      + 'color:var(--pill-neutral-text, #334); font-size:10px; line-height:14px; cursor:help; white-space:nowrap;';
    span.textContent = '◆ ' + (model || 'Copilot');
    span.title = sessionLines(conn).join('\n');
    return span;
  }

  // ---- the row ----------------------------------------------------------------

  // Everything a Copilot row carries inline: the usage bar (when the display mode
  // puts it on the row) and the session badge. Null when there is neither, so the row
  // renders as plain text with no gap.
  function buildRowInline(conn, usage, opts, showUsage) {
    const parts = [];
    if (showUsage && usage) {
      if (usage.ok === false) {
        parts.push(marker(usage.signedOut ? 'signed out' : 'usage n/a',
          (usage.signedOut ? 'No github.com session in this browser profile.'
            : 'Copilot usage unavailable: ' + (usage.error || 'fetch failed'))
          + '\nSign in to github.com in this browser profile to see the quota.'
          + '\nDetails: Options ▸ Advanced ▸ GitHub Copilot sessions.'));
      } else if (accountMismatch(usage, conn)) {
        parts.push(marker('other account', mismatchText(usage, conn)));
      } else if (primaryQuota(usage)) {
        parts.push(makeUsageBar(usage, opts));
      }
    }
    const badge = buildSessionBadge(conn);
    if (badge) parts.push(badge);
    if (!parts.length) return null;
    const wrap = document.createElement('span');
    wrap.style.cssText = 'display:inline-flex; align-items:center; gap:4px; vertical-align:middle; margin:0 2px;';
    for (const p of parts) wrap.appendChild(p);
    return wrap;
  }

  // The monthly chart: a line for the quota the bar follows and one for code completions
  // (usage-chart.js draws it), matching the two meters on the Usage tab. Null when nothing
  // is plottable: both unlimited or absent, no window yet, or no chart module.
  function buildUsageChart(usage) {
    const UC = globalThis.UsageChart;
    if (!UC || !usage) return null;
    const p = primaryQuota(usage);
    const comp = usage.completions && !usage.completions.unlimited;
    if (!((p && usage.monthlyWindow) || (comp && usage.completionsWindow))) return null;
    return UC.build(usage, { monthly: !!p, monthlyLabel: p && p.name, completions: !!comp });
  }

  // Plain text for the right-click copy path: the same facts as the row, in the same
  // order, since a copied row that disagrees with the screen is worse than one that
  // says less.
  function rowSummaryText(conn, usage, opts, showUsage) {
    const out = [];
    if (showUsage && usage) {
      if (usage.ok === false) out.push(usage.signedOut ? SIGNED_OUT_TEXT : `usage n/a (${usage.error || 'fetch failed'})`);
      else if (accountMismatch(usage, conn)) out.push(`usage is for ${usage.login}, not ${editorAccount(conn)}`);
      else {
        const p = primaryQuota(usage);
        if (p) {
          let s = quotaLine(p.name, p.q);
          const sec = secondaryQuota(usage, opts);
          if (sec) s += ` · ${quotaLine(sec.name, sec.q)}`;
          const reset = resetText(usage);
          if (reset) s += ` (${reset})`;
          out.push(s);
        }
      }
    }
    const cs = conn && conn.copilot;
    if (cs) {
      const model = conn.model_name || conn.model || cs.model;
      if (model) out.push(model);
      if (cs.chat_turns) out.push(`${cs.chat_turns} turn${cs.chat_turns === 1 ? '' : 's'}`);
    }
    return out.join(' · ');
  }

  // ---- Options card -------------------------------------------------------------

  // The sign-in line on Options ▸ Advanced ▸ GitHub Copilot sessions, from one forced
  // read. Pure, so options.js only paints it: { tone: 'ok'|'warn'|'bad', text, help,
  // signIn } where signIn says whether to offer the "Open github.com" button.
  function authState(data, failure) {
    const reason = String((data && data.error) || failure || '');
    if (data && data.ok) {
      const p = primaryQuota(data);
      const now = [p ? quotaLine(p.name, p.q) : null,
        data.completions ? quotaLine('code completions', data.completions) : null].filter(Boolean).join(', ');
      const who = `github.com as ${data.login}` + (data.plan ? ` (plan: ${data.plan})` : '');
      if (data.stale) {
        return {
          tone: 'warn', signIn: true,
          text: `Signed in to ${who}, but the last refresh failed.`,
          help: `github.com did not answer the latest quota read (${reason}). The popup keeps showing the `
            + 'last numbers it got until a refresh succeeds.',
        };
      }
      return {
        tone: 'ok', signIn: false,
        text: `Signed in to ${who} in this browser.`,
        help: (now ? `Your quotas are readable (right now: ${now}), so the settings below apply. ` : '')
          + 'If VS Code\'s Copilot is signed in to a different GitHub account, its row says "other '
          + 'account" instead of showing this account\'s quota; switch accounts from the github.com '
          + 'account menu to read the editor\'s.',
      };
    }
    if (data && data.signedOut) {
      return {
        tone: 'warn', signIn: true,
        text: 'Not signed in to github.com in this browser.',
        help: 'The Copilot quotas can only be read with a github.com session. Until you sign in, every '
          + 'GitHub Copilot Chat row shows "signed out" instead of a usage bar; the model and chat badge, '
          + 'which come from VS Code, are unaffected. Sign in to github.com in THIS browser, then Re-check.',
      };
    }
    return {
      tone: 'bad', signIn: true,
      text: 'Could not read your Copilot quotas.',
      help: `The quota read failed${reason ? ` (${reason})` : ''}. While it keeps failing, the bar stays `
        + 'hidden on every GitHub Copilot Chat row; the rest of BrowserPlug is unaffected.',
    };
  }

  globalThis.CopilotView = {
    isCopilotClient, fmtLeft, primaryQuota, secondaryQuota, quotaLine, resetLeft, resetText,
    accountMismatch, mismatchText, sessionLines, buildSessionBadge,
    buildRowInline, rowSummaryText, makeUsageBar, buildUsageChart, authState,
  };
})();
