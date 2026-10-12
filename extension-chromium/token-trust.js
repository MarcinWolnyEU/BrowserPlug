/*
 * token-trust.js: when this browser may replace the token it connects with (review S5).
 *
 * The auto-pulled token is adopted without a click exactly once: the first time, when the
 * browser holds none. Once a handshake has succeeded with it (`autoTokenProven` in
 * storage.local), a later 4401 does not prove the token should change. 8772 is free
 * whenever no MCP client runs, so any local process can bind it, close the extension with
 * 4401 and name its own /ws-token. The extension then reports "token rejected" and waits
 * for the user's Re-authorize click.
 *
 * "Proven" means an encrypted handshake completed with the AUTO token. A socket that
 * authenticated with a manual token from Options, or a server with auth off (no
 * handshake, so no proof of any token), proves nothing about the stored auto token.
 *
 * A token endpoint is honoured only on the loopback origin the extension is configured
 * for, and only on one of: the MCP port an authenticated server last reported, the MCP
 * port the user configured (Options > General, `mcpPort`), or the range the server's port
 * seek uses. A configured remote host never gets a loopback fetch, and a squatter on 8772
 * naming an arbitrary port is refused, first adoption included. The configured port is the
 * user's own setting, which a squatter cannot write; it is how a server pinned outside the
 * range by BROWSERPLUG_MCP_PORT (and the test harness's random ports) bootstraps without
 * a pasted token. The reported port is kept across reconnects: the 4401 that needs it
 * always arrives on a fresh socket, before that socket could report anything.
 *
 * background.js (both engines) keeps the glue: the bootstrap fetch on a 4401, the
 * reconnect after an adoption. This file keeps the state and the rules. It registers
 * `globalThis.BrowserplugTokenTrust`; load it before background.js.
 */
(() => {
  'use strict';

  const PROVEN_KEY = 'autoTokenProven';
  const MCP_PORT_SEEK_MIN = 2830;
  const MCP_PORT_SEEK_MAX = 2942;
  const LOOPBACK_HOSTS = new Set(['127.0.0.1', '::1', 'localhost']);

  const isPort = (n) => Number.isInteger(n) && n >= 1 && n <= 65535;
  const stripHost = (h) => String(h || '').replace(/^\[|\]$/g, '').toLowerCase();

  // opts.storage: () => the storage.local area (read late, so a test can swap it).
  function create(opts) {
    const local = () => opts.storage();
    let proven = null;            // cache of PROVEN_KEY; null = not read yet
    let unsaved = false;          // `proven` is ahead of storage: its last write failed
    let rejected = false;         // a proven token was refused; waiting for the user
    let pendingEndpoint = null;   // where the Re-authorize click would fetch from
    let adoptError = null;        // why the last click failed, kept until the offer changes
    let reportedMcpPort = null;   // from the last authenticated health answer

    // A flag that cannot be read counts as proven: the guard fails closed, so the 4401
    // waits for a click instead of adopting whatever token it names. The failure is not
    // cached, so a later read can still find the stored value.
    async function isProven() {
      if (proven !== null) return proven;
      try {
        const stored = await local().get({ [PROVEN_KEY]: false });
        proven = !!(stored && stored[PROVEN_KEY]);
      } catch (e) {
        return true;
      }
      return proven;
    }

    async function setProven(value) {
      proven = value;
      try {
        await local().set({ [PROVEN_KEY]: value });
        unsaved = false;
      } catch (e) {
        unsaved = true;
        console.error('Failed to store the token-proven flag:', e);
      }
    }

    function clearRejection() {
      rejected = false;
      pendingEndpoint = null;
      adoptError = null;
    }

    function endpointMatches(href, config) {
      let url;
      try {
        url = new URL(href);
      } catch (e) {
        return false;
      }
      if (!LOOPBACK_HOSTS.has(stripHost(config && config.hostname))) return false;
      if (!LOOPBACK_HOSTS.has(stripHost(url.hostname))) return false;
      const port = Number(url.port);
      if (reportedMcpPort !== null && port === reportedMcpPort) return true;
      if (config && isPort(config.mcpPort) && port === config.mcpPort) return true;
      return port >= MCP_PORT_SEEK_MIN && port <= MCP_PORT_SEEK_MAX;
    }

    // A health answer: the server is serving this socket, so nothing is rejected any more.
    // s.encrypted: the socket completed the handshake; s.handshakeToken: the token it used;
    // s.readAutoToken(): the auto-pulled token in storage now, read only while the proof
    // is still owed (every probe calls this); s.mcpPort: what the server reported.
    async function noteAuthenticated(s) {
      clearRejection();
      if (s.encrypted && isPort(s.mcpPort)) reportedMcpPort = s.mcpPort;
      if ((proven === true && !unsaved) || !s.encrypted || !s.handshakeToken) return;
      if (s.handshakeToken !== await s.readAutoToken()) return;
      await setProven(true);
    }

    // A proven token was refused with a 4401 naming `endpoint` (or none). The retry loop
    // meets the same 4401 every few seconds, so a failed click's reason stays until the
    // offered endpoint changes.
    function noteRejected(endpoint, config) {
      // An endpoint off the configured origin or port is not offered for adoption.
      const offered = endpoint && endpointMatches(endpoint, config) ? endpoint : null;
      if (!rejected || offered !== pendingEndpoint) adoptError = null;
      rejected = true;
      pendingEndpoint = offered;
    }

    // The user's Re-authorize click. deps: { fetch, storeToken(token), journal(event,
    // fields) }. Returns { ok: true } or { ok: false, error }; the error also stays in
    // status() so the popup can show it.
    async function adopt(config, deps) {
      const fail = (error) => {
        if (rejected) adoptError = error;
        return { ok: false, error };
      };
      const endpoint = pendingEndpoint;
      if (!rejected || !endpoint) return fail('no rejected token to replace');
      if (!endpointMatches(endpoint, config)) {
        deps.journal('token_fetch', { reason: 'endpoint refused' });
        return fail('the server named an endpoint this browser is not configured for');
      }
      let token = '';
      try {
        const response = await deps.fetch(endpoint, { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const body = await response.json();
        token = (body && typeof body.token === 'string') ? body.token.trim() : '';
      } catch (e) {
        deps.journal('token_fetch', { reason: 'adopt failed' });
        return fail(`could not fetch a token: ${e && e.message}`);
      }
      if (!token) return fail('the token endpoint returned no token');
      try {
        await deps.storeToken(token);
      } catch (e) {
        return fail('could not store the token');
      }
      await setProven(false);
      clearRejection();
      deps.journal('token_fetch', { reason: 'adopted by user' });
      return { ok: true };
    }

    function status() {
      return {
        tokenRejected: rejected,
        tokenAdoptable: rejected && !!pendingEndpoint,
        adoptError: rejected ? adoptError : null
      };
    }

    return {
      isProven, setProven, clearRejection, endpointMatches, noteAuthenticated, noteRejected,
      adopt, status,
      get rejected() { return rejected; },
      get pendingEndpoint() { return pendingEndpoint; }
    };
  }

  globalThis.BrowserplugTokenTrust = Object.freeze({
    create, PROVEN_KEY, MCP_PORT_SEEK_MIN, MCP_PORT_SEEK_MAX
  });
})();
