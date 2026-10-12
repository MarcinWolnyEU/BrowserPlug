/*
 * body-censor.js: optional censoring of captured request and response bodies.
 *
 * `requests(action="get")` returns bodies RAW by default (the user's decision: a
 * debugging tool that hides the payload is not much of a debugging tool). Options ▸
 * General adds an opt-in way to change that, and this module is all of the logic:
 *
 *   1. "Censor secrets in captured bodies" (`censorSecrets`, default off). Masks the
 *      VALUE of every credential-looking key (password, secret, token, api_key,
 *      authorization, cookie, session, otp, code_verifier, private_key ...) with
 *      "<redacted>". It reads the body by shape:
 *        - JSON: a scanner walks the text as a structure and replaces value spans, so
 *          formatting, key order and big numbers elsewhere survive untouched. A value
 *          under a credential key is masked whole: a string or number becomes
 *          "<redacted>", and inside an object or array under such a key every leaf is
 *          masked (the structure stays). true, false, null and "" carry no secret and
 *          are left alone.
 *        - application/x-www-form-urlencoded: `key=value` pairs.
 *        - multipart/form-data: text parts by their Content-Disposition name; a part
 *          with a filename is a file and is left alone.
 *        - anything else, and any JSON the scanner rejects (a body cut off at
 *          max_body_size is the common case): a text scan for `key: value` and
 *          `key=value` shapes.
 *      Gecko reports a form body as JSON text (`{"password":["x"]}`) whatever its
 *      declared content type, so the shape is sniffed, not trusted.
 *
 *   2. "Custom patterns" (`censorPatterns`, a list of regular expressions). Applied to
 *      the text of every captured request and response body, each match replaced with
 *      "<redacted>". They belong to the toggle: the field is only shown while
 *      `censorSecrets` is on, and stored patterns are ignored while it is off, so a
 *      pattern the page hides never runs. Matching is case-sensitive; `(?i:...)` in the
 *      pattern itself makes part of it insensitive. Invalid, oversized and dangerous
 *      patterns are reported (analyzePatterns) and ignored, never thrown.
 *
 * Applied when `requests.get_content` BUILDS its result (background.js,
 * censorRequestBodies), not when traffic is captured: flipping the setting therefore
 * covers traffic that is already captured. Header redaction is a separate, always-on
 * mechanism and is not touched here.
 *
 * Only utf-8 text bodies are censored. A base64 body (include_binary) is never decoded;
 * it comes back marked as not censored.
 *
 * Catastrophic backtracking. A user pattern runs over bodies of up to ~1 MB (the
 * server clamps max_body_size to 1,000,000 bytes), and JavaScript cannot interrupt a
 * regex. So a group that contains an unbounded quantifier and is itself repeated,
 * `(a+)+`, `(\w+\s*)*`, `(.*)*`, `(a+){1,50}`, is rejected up front
 * (hasNestedUnboundedQuantifier). A bounded outer count does not make it safe: `{1,50}`
 * lets the engine try every way of cutting the text into up to 50 runs. The check is a
 * heuristic over the pattern text: it catches the classic exponential shapes and
 * nothing else. `(a|a)*`, `(a|aa)+`, `(a{2,5})+` and quadratic patterns such as `.*.*x`
 * pass, and it over-rejects some harmless patterns. The bounds that are always enforced
 * are 20 patterns and 200 characters per pattern.
 *
 * Load it before background.js (gecko: background.scripts; chromium: importScripts)
 * and before options.js (options.html). It registers `globalThis.BrowserplugBodyCensor`.
 */
(() => {
  'use strict';

  const REDACTED = '<redacted>';
  const MAX_PATTERNS = 20;
  const MAX_PATTERN_LENGTH = 200;
  const MAX_JSON_DEPTH = 100;

  // The storage.sync keys and their defaults; background.js and options.js both read
  // exactly this object, so the two cannot disagree about a default.
  const SETTINGS_DEFAULTS = Object.freeze({
    censorSecrets: false,
    censorPatterns: Object.freeze([])
  });

  // ---- credential-looking keys ------------------------------------------------------

  // camelCase and separators are folded to snake_case first, so `accessToken`,
  // `access-token`, `Access Token` and `ACCESS_TOKEN` are one key. The list matches
  // whole snake_case segments, so `author` does not trip `auth` but `csrf_token` and
  // `x_auth_token` do. A false positive costs one <redacted> in a debug session.
  const CREDENTIAL_KEY = new RegExp(
    '(?:^|_)(?:password|passwd|pwd|passphrase|passcode|pass|secret|token|apikey|' +
    'authorization|auth|cookie|cookies|session|sessionid|sid|otp|credential|' +
    'credentials|signature|bearer|jwt|csrf|xsrf)(?:_|$)' +
    '|(?:^|_)(?:api|access|secret|private|signing|encryption|client|auth)_key(?:_|$)' +
    '|(?:^|_)code_verifier(?:_|$)' +
    '|(?:^|_)set_cookie(?:_|$)');

  function normalizeKey(key) {
    return String(key)
      .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
      .replace(/([A-Z]+)([A-Z][a-z])/g, '$1_$2')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '');
  }

  function isCredentialKey(key) {
    return CREDENTIAL_KEY.test(normalizeKey(key));
  }

  // ---- JSON: walk the structure, replace value spans ----------------------------------

  const JSON_LITERAL = /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null/y;

  // Returns [[start, end], ...] value spans to replace, in ascending order. Throws
  // SyntaxError on anything that is not one complete JSON value (the caller falls back
  // to the text scan).
  function scanJson(text) {
    const n = text.length;
    const spans = [];
    let i = 0;

    const fail = () => { throw new SyntaxError('not JSON'); };
    const skipWs = () => {
      while (i < n) {
        const c = text.charCodeAt(i);
        if (c === 32 || c === 9 || c === 10 || c === 13) i++; else break;
      }
    };
    const readString = () => {
      const start = i;
      i++;
      while (i < n) {
        const ch = text[i];
        if (ch === '\\') i += 2;
        else if (ch === '"') { i++; return [start, i]; }
        else if (ch < ' ') fail();
        else i++;
      }
      return fail();
    };
    const readValue = (secret, depth) => {
      if (depth > MAX_JSON_DEPTH) fail();
      skipWs();
      if (i >= n) fail();
      const c = text[i];
      if (c === '{') {
        i++;
        skipWs();
        if (text[i] === '}') { i++; return; }
        for (;;) {
          skipWs();
          if (text[i] !== '"') fail();
          const [ks, ke] = readString();
          let key;
          try { key = JSON.parse(text.slice(ks, ke)); } catch (e) { fail(); }
          skipWs();
          if (text[i] !== ':') fail();
          i++;
          readValue(secret || isCredentialKey(key), depth + 1);
          skipWs();
          if (text[i] === ',') { i++; continue; }
          if (text[i] === '}') { i++; return; }
          fail();
        }
      }
      if (c === '[') {
        i++;
        skipWs();
        if (text[i] === ']') { i++; return; }
        for (;;) {
          readValue(secret, depth + 1);
          skipWs();
          if (text[i] === ',') { i++; continue; }
          if (text[i] === ']') { i++; return; }
          fail();
        }
      }
      if (c === '"') {
        const [s, e] = readString();
        if (secret && e - s > 2) spans.push([s, e]);
        return;
      }
      JSON_LITERAL.lastIndex = i;
      const m = JSON_LITERAL.exec(text);
      if (!m) fail();
      const s = i;
      i += m[0].length;
      if (secret && m[0] !== 'true' && m[0] !== 'false' && m[0] !== 'null') spans.push([s, i]);
    };

    readValue(false, 0);
    skipWs();
    if (i !== n) fail();
    return spans;
  }

  function censorJson(text) {
    const spans = scanJson(text);
    if (spans.length === 0) return { content: text, count: 0 };
    let out = '';
    let last = 0;
    for (const [s, e] of spans) {
      out += text.slice(last, s) + '"' + REDACTED + '"';
      last = e;
    }
    return { content: out + text.slice(last), count: spans.length };
  }

  // ---- application/x-www-form-urlencoded ------------------------------------------------

  function censorForm(text) {
    let count = 0;
    const out = text.split('&').map((pair) => {
      const eq = pair.indexOf('=');
      if (eq < 1) return pair;
      const rawKey = pair.slice(0, eq);
      const value = pair.slice(eq + 1);
      if (!value || value === REDACTED) return pair;
      let key = rawKey;
      try { key = decodeURIComponent(rawKey.replace(/\+/g, ' ')); } catch (e) { /* keep raw */ }
      if (!isCredentialKey(key)) return pair;
      count++;
      return rawKey + '=' + REDACTED;
    });
    return { content: out.join('&'), count };
  }

  // ---- multipart/form-data ---------------------------------------------------------------

  function boundaryOf(contentType, text) {
    const m = /boundary=(?:"([^"]+)"|([^;\s]+))/i.exec(contentType || '');
    if (m) return m[1] || m[2];
    // No content type (or none with a boundary): the body opens with the delimiter line.
    const first = /^--([^\r\n]{1,200})\r?\n/.exec(text);
    return first ? first[1] : null;
  }

  function censorMultipart(text, contentType) {
    const boundary = boundaryOf(contentType, text);
    if (!boundary) return null;
    const delim = '--' + boundary;
    const spans = [];
    let pos = text.indexOf(delim);
    while (pos !== -1) {
      const partStart = pos + delim.length;
      if (text.startsWith('--', partStart)) break;
      const next = text.indexOf(delim, partStart);
      const partEnd = next === -1 ? text.length : next;
      let headEnd = text.indexOf('\r\n\r\n', partStart);
      let gap = 4;
      const lfEnd = text.indexOf('\n\n', partStart);
      if (headEnd === -1 || (lfEnd !== -1 && lfEnd < headEnd)) { headEnd = lfEnd; gap = 2; }
      if (headEnd !== -1 && headEnd < partEnd) {
        const head = text.slice(partStart, headEnd);
        const disposition = /content-disposition:[^\r\n]*/i.exec(head);
        const name = disposition && /[;\s]name=(?:"([^"]*)"|([^;\s]+))/i.exec(disposition[0]);
        const isFile = disposition && /[;\s]filename\*?=/i.test(disposition[0]);
        if (name && !isFile && isCredentialKey(name[1] !== undefined ? name[1] : name[2])) {
          const bodyStart = headEnd + gap;
          let bodyEnd = partEnd;
          if (text.startsWith('\r\n', bodyEnd - 2) && bodyEnd - 2 >= bodyStart) bodyEnd -= 2;
          else if (text[bodyEnd - 1] === '\n' && bodyEnd - 1 >= bodyStart) bodyEnd -= 1;
          if (bodyEnd > bodyStart && text.slice(bodyStart, bodyEnd) !== REDACTED) {
            spans.push([bodyStart, bodyEnd]);
          }
        }
      }
      pos = next;
    }
    if (spans.length === 0) return { content: text, count: 0 };
    let out = '';
    let last = 0;
    for (const [s, e] of spans) { out += text.slice(last, s) + REDACTED; last = e; }
    return { content: out + text.slice(last), count: spans.length };
  }

  // ---- text fallback ---------------------------------------------------------------------

  // `key: value` and `key=value`, the key optionally quoted, the value quoted or bare.
  // Every quantifier is bounded, so a hostile body cannot make this run long. The third
  // value alternative takes an unterminated quoted value, which is what a body cut off
  // mid-string looks like.
  const TEXT_PAIR = new RegExp(
    '(["\']?)([A-Za-z_][\\w.\\-\\[\\]]{0,63})\\1(\\s{0,8}[:=]\\s{0,8})' +
    '("(?:[^"\\\\\\r\\n]|\\\\.){0,4096}"|\'[^\'\\r\\n]{0,4096}\'|' +
    '[^\\s&,;"\'{}\\[\\]<>]{1,4096}|"(?:[^"\\\\\\r\\n]|\\\\.){1,4096})', 'g');

  // A header-style line ("Authorization: Bearer abc", "Cookie: a=1; b=2") holds its
  // secret in everything after the colon, not just the first token, so this one takes
  // the whole line.
  const TEXT_HEADER_LINE = new RegExp(
    '^([ \t]{0,16}(?:(?:proxy[-_])?authorization|(?:set[-_])?cookie))' +
    '([ \t]{0,8}:[ \t]{0,8})([^\r\n]{1,4096})', 'gim');

  function censorTextPairs(text) {
    let count = 0;
    const lines = text.replace(TEXT_HEADER_LINE, (whole, key, sep, value) => {
      if (value.trim() === REDACTED) return whole;
      count++;
      return key + sep + REDACTED;
    });
    const content = lines.replace(TEXT_PAIR, (whole, q, key, sep, value) => {
      if (!isCredentialKey(key)) return whole;
      const quote = value[0] === '"' || value[0] === "'" ? value[0] : '';
      const inner = quote ? value.slice(1, value.endsWith(quote) && value.length > 1 ? -1 : undefined) : value;
      if (!inner || inner === REDACTED) return whole;
      count++;
      return q + key + q + sep + quote + REDACTED + quote;
    });
    return { content, count };
  }

  // ---- built-in censoring ------------------------------------------------------------------

  function censorSecretsIn(text, contentType) {
    const ct = String(contentType || '').toLowerCase();
    if (/json/.test(ct) || /^\s*[\[{]/.test(text.slice(0, 64))) {
      try { return censorJson(text); } catch (e) { /* not JSON after all; scan as text */ }
    }
    if (ct.includes('multipart/form-data')) {
      const r = censorMultipart(text, contentType);
      if (r) return r;
    }
    if (ct.includes('x-www-form-urlencoded')) return censorForm(text);
    return censorTextPairs(text);
  }

  // ---- custom patterns ---------------------------------------------------------------------

  // See the header for what this does and does not catch. Walks the source once,
  // tracking whether each group contains an unbounded quantifier (`*`, `+`, `{n,}`).
  // A group that does, followed by any quantifier that can repeat it (`*`, `+`, `{n,}`,
  // `{n,m}` with m > 1, `{n}` with n > 1), is the exponential shape.
  function hasNestedUnboundedQuantifier(src) {
    const n = src.length;
    const NONE = { len: 0, unbounded: false, repeats: false };
    const quantifierAt = (j) => {
      const c = src[j];
      if (c === '*' || c === '+') return { len: 1, unbounded: true, repeats: true };
      if (c === '{') {
        const m = /^\{(\d+)(,(\d*))?\}/.exec(src.slice(j, j + 24));
        if (!m) return NONE;
        const max = m[2] === undefined ? Number(m[1]) : m[3] === '' ? Infinity : Number(m[3]);
        return { len: m[0].length, unbounded: max === Infinity, repeats: max > 1 };
      }
      return NONE;
    };
    const stack = [{ inner: false }];
    let i = 0;
    while (i < n) {
      const c = src[i];
      let atomEnd;
      if (c === '\\') {
        atomEnd = i + 2;
      } else if (c === '[') {
        let j = i + 1;
        while (j < n && src[j] !== ']') j += src[j] === '\\' ? 2 : 1;
        atomEnd = Math.min(j + 1, n);
      } else if (c === '(') {
        stack.push({ inner: false });
        i++;
        if (src[i] === '?') {
          if (src[i + 1] === '<' && src[i + 2] !== '=' && src[i + 2] !== '!') {
            const close = src.indexOf('>', i);
            i = close === -1 ? n : close + 1;
          } else {
            i += src[i + 1] === '<' ? 3 : 2;
          }
        }
        continue;
      } else if (c === ')') {
        const frame = stack.length > 1 ? stack.pop() : { inner: false };
        i++;
        const q = quantifierAt(i);
        if (q.repeats && frame.inner) return true;
        const parent = stack[stack.length - 1];
        parent.inner = parent.inner || frame.inner || q.unbounded;
        i += q.len;
        continue;
      } else if (c === '|') {
        i++;
        continue;
      } else {
        atomEnd = i + 1;
      }
      i = atomEnd;
      const q = quantifierAt(i);
      if (q.unbounded) stack[stack.length - 1].inner = true;
      i += q.len;
    }
    return false;
  }

  // null when the pattern is usable, otherwise the reason it is not.
  function patternProblem(src) {
    if (typeof src !== 'string' || src.trim() === '') return 'empty pattern';
    if (src.length > MAX_PATTERN_LENGTH) {
      return `too long (${src.length} characters, the limit is ${MAX_PATTERN_LENGTH})`;
    }
    try { new RegExp(src, 'g'); } catch (e) { return `invalid regular expression: ${e.message}`; }
    if (hasNestedUnboundedQuantifier(src)) {
      return 'rejected: a repeated group containing an unbounded quantifier, such as (a+)+ or (a+){1,50}, can hang the browser';
    }
    return null;
  }

  // One entry per non-empty line of the textarea (or per stored string): {pattern, ok,
  // storable, reason}. This is the UI's feedback list and the single source of "which
  // patterns apply" (the ok ones). `storable` is false for a pattern past the 20th and
  // for one over 200 characters: the page does not save those, which also keeps the
  // list inside storage.sync's 8 KB per-item quota. An invalid or dangerous pattern IS
  // storable, so the user can come back and fix it.
  function analyzePatterns(list) {
    const out = [];
    let storable = 0;
    for (const raw of Array.isArray(list) ? list : []) {
      if (typeof raw !== 'string') continue;
      const pattern = raw.trim();
      if (pattern === '') continue;
      let reason;
      let canStore = true;
      if (pattern.length > MAX_PATTERN_LENGTH) {
        canStore = false;
        reason = patternProblem(pattern);
      } else if (storable >= MAX_PATTERNS) {
        canStore = false;
        reason = `over the limit of ${MAX_PATTERNS} patterns`;
      } else {
        reason = patternProblem(pattern);
      }
      if (canStore) storable++;
      out.push({ pattern, ok: reason === null, storable: canStore, reason });
    }
    return out;
  }

  function splitPatternLines(text) {
    return String(text || '').split(/\r?\n/).map((s) => s.trim()).filter((s) => s !== '');
  }

  function compilePatterns(list) {
    return analyzePatterns(list).filter((a) => a.ok).map((a) => new RegExp(a.pattern, 'g'));
  }

  // ---- entry points ------------------------------------------------------------------------

  // Storage values (possibly missing, possibly the wrong type) -> what censorResult needs.
  function prepareSettings(raw) {
    const s = raw && typeof raw === 'object' ? raw : {};
    const censorSecrets = s.censorSecrets === true;
    const regexes = censorSecrets ? compilePatterns(s.censorPatterns) : [];
    return { censorSecrets, regexes, active: censorSecrets || regexes.length > 0 };
  }

  function censorText(text, contentType, prepared) {
    let content = text;
    let count = 0;
    if (prepared.censorSecrets) {
      const r = censorSecretsIn(content, contentType);
      content = r.content;
      count += r.count;
    }
    for (const re of prepared.regexes) {
      content = content.replace(re, (m) => {
        if (!m || m === REDACTED) return m;
        count++;
        return REDACTED;
      });
    }
    return { content, count };
  }

  function noteOf(n) {
    return `${n} value(s) censored by your Options > General setting`;
  }

  // Censors result.request_body and result.response_body in place, and adds the visible
  // marker (values_censored + values_censored_note) when anything was censored. Returns
  // the total. A body that cannot be censored is withheld whole instead of returned raw:
  // the user asked for censoring, so failing open would be the wrong direction.
  function censorResult(result, prepared) {
    if (!result || !prepared || !prepared.active) return 0;
    let total = 0;
    for (const field of ['request_body', 'response_body']) {
      const body = result[field];
      if (!body || body.included !== true || typeof body.content !== 'string') continue;
      if (body.encoding && body.encoding !== 'utf-8') {
        body.note = (body.note ? body.note + ' ' : '') + 'Binary body: not censored.';
        continue;
      }
      let count;
      try {
        const r = censorText(body.content, body.content_type, prepared);
        count = r.count;
        if (count > 0) body.content = r.content;
      } catch (e) {
        body.content = REDACTED;
        count = 1;
        body.note = (body.note ? body.note + ' ' : '') + 'Body withheld: censoring failed.';
      }
      if (count > 0) {
        body.censored_values = count;
        body.note = (body.note ? body.note + ' ' : '') + noteOf(count) + '.';
        total += count;
      }
    }
    if (total > 0) {
      result.values_censored = total;
      result.values_censored_note = noteOf(total);
    }
    return total;
  }

  globalThis.BrowserplugBodyCensor = {
    REDACTED,
    MAX_PATTERNS,
    MAX_PATTERN_LENGTH,
    SETTINGS_DEFAULTS,
    isCredentialKey,
    normalizeKey,
    hasNestedUnboundedQuantifier,
    patternProblem,
    analyzePatterns,
    splitPatternLines,
    compilePatterns,
    prepareSettings,
    censorText,
    censorResult
  };
})();
