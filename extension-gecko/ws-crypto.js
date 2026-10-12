// BrowserPlug's encrypted extension WebSocket, the extension's half.
//
// The server's half is server/ws/ws_crypto.py, whose docstring is the wire format; the two
// must agree byte for byte (tests/unit/test_ws_crypto_interop.py runs one against the
// other). .tmp/dev-docs/ws-encryption.md explains the design. In short: the extension asks for
// the channel, the server sends fresh X25519 and ML-KEM-768 keys, the extension answers
// with its own X25519 key, an ML-KEM ciphertext and a proof keyed by its device token
// (bp1.<label>.<secret>, shared-py/browserplug_shared/device_tokens.py), and the server proves it back. Every
// browser has its own device token, so none can pose as the server to another. Every later frame is one AES-256-GCM
// record.
//
// Shared by both trees and loaded into three contexts: the Firefox background page, the
// Chrome service worker, and the options page (which only uses signRequest). Every
// primitive is WebCrypto except ML-KEM-768, which is native where the browser has it
// and globalThis.browserplugMlKem768 (vendor/mlkem.js) everywhere else. The server
// cannot tell the two apart.
(function () {
  'use strict';

  const PROTOCOL_NAME = 'BrowserPlug_NNhfs+psk2_25519+MLKEM768_AESGCM_SHA256';
  const PROLOGUE = 'browserplug ws v1';
  const PSK_LABEL = 'browserplug psk v1';
  const HTTP_LABEL = 'browserplug http v2';
  const HTTP_LABEL_V1 = 'browserplug http v1';
  const AUTH_HEADER = 'x-browserplug-auth';

  const VERSION = 1;
  const T_REQ = 0, T_M1 = 1, T_M2 = 2, T_M3 = 3;
  const DH_LEN = 32, EK_LEN = 1184, CT_LEN = 1088, TAG_LEN = 16;
  const M1_LEN = 2 + DH_LEN + EK_LEN;
  const M3_LEN = 2 + TAG_LEN;
  const CLOSE_PROTOCOL = 4400;   // browsers may only send 1000 or 3000-4999
  // Longer than the server's hello window, so a slow handshake is closed by the server
  // with the `hello timeout:` reason the extension already knows how to read.
  const HANDSHAKE_TIMEOUT_MS = 70000;
  const OUTBOX_LIMIT = 256;

  const utf8 = new TextEncoder();
  const fromUtf8 = new TextDecoder();
  const subtle = () => globalThis.crypto.subtle;
  const EMPTY = new Uint8Array(0);

  const concat = (...parts) => {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let at = 0;
    for (const p of parts) { out.set(p, at); at += p.length; }
    return out;
  };
  const bytesOf = (data) => {
    if (typeof data === 'string') return utf8.encode(data);
    if (data instanceof Uint8Array) return data;
    if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    return new Uint8Array(data);
  };
  const sha256 = async (bytes) => new Uint8Array(await subtle().digest('SHA-256', bytes));
  const hmac = async (key, data) => {
    const k = await subtle().importKey('raw', key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    return new Uint8Array(await subtle().sign('HMAC', k, data));
  };
  const b64url = (bytes) => {
    let s = '';
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  };

  // Noise's HKDF: every output is one SHA-256 block.
  async function hkdf(ck, ikm, n) {
    const temp = await hmac(ck, ikm);
    const out = [];
    let prev = EMPTY;
    for (let i = 1; i <= n; i++) {
      prev = await hmac(temp, concat(prev, [i]));
      out.push(prev);
    }
    return out;
  }

  // 4 zero bytes, then the counter as a big-endian 64-bit integer.
  function nonce(counter) {
    const n = new Uint8Array(12);
    const view = new DataView(n.buffer);
    view.setUint32(4, Math.floor(counter / 0x100000000));
    view.setUint32(8, counter >>> 0);
    return n;
  }

  const aesKey = (raw) => subtle().importKey('raw', raw, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);

  // Noise's SymmetricState, reduced to what this handshake uses.
  class Symmetric {
    static async create(label) {
      const s = new Symmetric();
      s.h = await sha256(utf8.encode(PROTOCOL_NAME));
      s.ck = s.h;
      s.k = null;
      s.n = 0;
      await s.mixHash(utf8.encode(PROLOGUE));
      await s.mixHash(utf8.encode(label));
      return s;
    }
    async mixHash(data) { this.h = await sha256(concat(this.h, data)); }
    async mixKey(ikm) { [this.ck, this.k] = await hkdf(this.ck, ikm, 2); this.n = 0; }
    async mixKeyAndHash(ikm) {
      const [ck, tempH, k] = await hkdf(this.ck, ikm, 3);
      this.ck = ck;
      await this.mixHash(tempH);
      this.k = k;
      this.n = 0;
    }
    async encryptAndHash(pt) {
      const ct = new Uint8Array(await subtle().encrypt(
        { name: 'AES-GCM', iv: nonce(this.n++), additionalData: this.h }, await aesKey(this.k), pt));
      await this.mixHash(ct);
      return ct;
    }
    async decryptAndHash(ct) {
      const pt = new Uint8Array(await subtle().decrypt(
        { name: 'AES-GCM', iv: nonce(this.n++), additionalData: this.h }, await aesKey(this.k), ct));
      await this.mixHash(ct);
      return pt;
    }
    split() { return hkdf(this.ck, EMPTY, 2); }
  }

  // --- ML-KEM-768: native WebCrypto when the browser has it, the bundle otherwise ---
  // Chrome turns native ML-KEM on in 155; Firefox has it only in Nightly so far.

  let nativeKem = null;   // null = not probed yet
  async function encapsulate(ek) {
    const s = subtle();
    if (nativeKem !== false && typeof s.encapsulateBits === 'function') {
      try {
        const pub = await s.importKey('raw-public', ek, { name: 'ML-KEM-768' }, false, ['encapsulateBits']);
        const r = await s.encapsulateBits({ name: 'ML-KEM-768' }, pub);
        nativeKem = true;
        return { ss: new Uint8Array(r.sharedKey), ct: new Uint8Array(r.ciphertext), path: 'native' };
      } catch (e) {
        if (nativeKem === true) throw e;   // it worked before: this key is bad, not the API
        nativeKem = false;                 // the method exists but the algorithm does not
      }
    }
    const lib = globalThis.browserplugMlKem768;
    if (!lib) throw new Error('no ML-KEM-768 implementation (vendor/mlkem.js did not load)');
    const r = lib.encapsulate(ek);   // runs the FIPS 203 modulus check on the key
    return { ss: r.sharedSecret, ct: r.cipherText, path: 'library' };
  }

  const pskFromSecret = (secret) => hmac(utf8.encode(PSK_LABEL), utf8.encode(secret));

  // bp1.<label>.<secret>. Anything else (a token from a server older than 1.9.1) is sent
  // with an empty label and fails at the proof like any other wrong token, which sends
  // the extension to fetch a device token.
  const DEVICE_TOKEN_RE = /^bp1\.([A-Za-z0-9_-]{1,32})\.([A-Za-z0-9_-]{43})$/;
  function parseDeviceToken(token) {
    const m = DEVICE_TOKEN_RE.exec(String(token || '').trim());
    return m ? { label: m[1], secret: m[2] } : { label: '', secret: String(token || '') };
  }

  // --- The wire: one per WebSocket --------------------------------------------------
  //
  // Owns everything that crosses the socket, so background.js never calls ws.send itself.
  // The caller forwards the socket's events (receive, socketClosed) rather than the wire
  // registering listeners: that keeps the MV2 and MV3 glue identical and lets the JS twin
  // tests drive it with a plain fake socket.
  //
  // start(token, buildHello) runs once per socket. With a token it sends REQ at once (on
  // the same tick, so a starved browser still speaks inside the server's window), runs the
  // handshake, and sends the hello as the first sealed record. Without one it sends the
  // hello in plaintext: that is the token bootstrap, and the hello then carries no secret.
  // Frames sent before the hello is out wait in an outbox and follow it in order.
  function createWire(ws, handlers) {
    const onMessage = (handlers && handlers.onMessage) || (() => {});
    const onFailure = (handlers && handlers.onFailure) || (() => {});
    let started = false;
    let open = false;          // the hello is out; frames flow
    let dead = null;
    let outbox = [];
    let channel = null;        // { sendKey, recvKey, sendN, recvN }
    let sendChain = Promise.resolve();
    let recvChain = Promise.resolve();
    const inbox = [];          // handshake frames not yet consumed
    let waiter = null;
    let handshaking = false;   // REQ is out and the channel is not up yet

    const wire = {
      get encrypted() { return !!channel; },
      kemPath: null,
      get started() { return started; },
      // The hello is out and frames flow (in plaintext, unless `encrypted`).
      get open() { return open; },
      start,
      send,
      receive,
      socketClosed,
    };

    function fail(err, closeCode, closeReason) {
      if (dead) return dead;
      dead = err instanceof Error ? err : new Error(String(err));
      outbox = [];
      if (waiter) { const w = waiter; waiter = null; w.reject(dead); }
      if (closeCode) {
        try { ws.close(closeCode, closeReason || dead.message); } catch (e) { /* already closing */ }
      }
      try { onFailure(dead); } catch (e) { /* the caller's problem */ }
      return dead;
    }

    function socketClosed(code, reason) {
      const err = new Error(`socket closed (${code}${reason ? ' ' + reason : ''})`);
      err.code = code;
      err.reason = reason;
      if (!dead) {
        dead = err;
        outbox = [];
        if (waiter) { const w = waiter; waiter = null; w.reject(err); }
      }
    }

    function nextFrame(deadline) {
      if (dead) return Promise.reject(dead);
      if (inbox.length) return Promise.resolve(inbox.shift());
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          waiter = null;
          reject(fail(new Error('handshake timed out'), CLOSE_PROTOCOL, 'handshake timeout'));
        }, Math.max(0, deadline - Date.now()));
        if (timer && typeof timer === 'object' && typeof timer.unref === 'function') timer.unref();
        waiter = {
          resolve: (f) => { clearTimeout(timer); resolve(f); },
          reject: (e) => { clearTimeout(timer); reject(e); },
        };
      });
    }

    function receive(data) {
      if (dead) return;
      if (typeof data === 'string') {
        if (channel || handshaking) {
          fail(new Error('plaintext frame on an encrypted channel'), CLOSE_PROTOCOL, 'bad record');
          return;
        }
        onMessage(data);
        return;
      }
      if (channel) {
        const n = channel.recvN++;
        const opened = subtle().decrypt({ name: 'AES-GCM', iv: nonce(n) }, channel.recvKey, bytesOf(data));
        opened.catch(() => {});
        recvChain = recvChain.then(() => opened).then((pt) => {
          if (!dead) onMessage(fromUtf8.decode(pt));
        }, () => {
          fail(new Error('record failed authentication'), CLOSE_PROTOCOL, 'bad record');
        });
        return;
      }
      if (!handshaking) return;   // a binary frame nobody asked for; ignore it
      if (waiter) { const w = waiter; waiter = null; w.resolve(bytesOf(data)); } else inbox.push(bytesOf(data));
    }

    function sealAndSend(text) {
      const n = channel.sendN++;
      const sealed = subtle().encrypt({ name: 'AES-GCM', iv: nonce(n) }, channel.sendKey, bytesOf(text));
      sealed.catch(() => {});
      sendChain = sendChain.then(() => sealed).then((ct) => {
        if (dead) return;
        ws.send(new Uint8Array(ct));
      }).catch((e) => { fail(e, CLOSE_PROTOCOL, 'send failed'); });
    }

    function transmit(text) {
      if (channel) sealAndSend(text);
      else ws.send(text);
    }

    // Returns true when the frame was sent or queued behind the hello.
    function send(text) {
      if (dead) return false;
      if (!open) {
        if (outbox.length >= OUTBOX_LIMIT) return false;
        outbox.push(text);
        return true;
      }
      transmit(text);
      return true;
    }

    function openFlow(helloText) {
      transmit(helloText);
      open = true;
      const queued = outbox;
      outbox = [];
      for (const t of queued) transmit(t);
    }

    // Resolves once the hello is on the wire (or sealed and queued behind the send chain).
    // buildHello(encrypted) returns the hello's JSON text; the caller leaves the token out
    // of an encrypted hello, where the handshake has already proved it.
    function start(token, buildHello, opts) {
      if (started) return Promise.reject(new Error('the wire is already started'));
      started = true;
      if (!token) {
        try {
          openFlow(buildHello(false));
        } catch (e) {
          return Promise.reject(fail(e));
        }
        return Promise.resolve({ encrypted: false });
      }
      handshaking = true;
      const deadline = Date.now() + ((opts && opts.timeoutMs) || HANDSHAKE_TIMEOUT_MS);
      try {
        const label = utf8.encode(parseDeviceToken(token).label);
        // Synchronously, before any await.
        ws.send(concat([VERSION, T_REQ, label.length], label));
      } catch (e) {
        return Promise.reject(fail(e));
      }
      return handshake(token, deadline).then(() => {
        handshaking = false;
        openFlow(buildHello(true));
        return { encrypted: true, kem: wire.kemPath };
      }, (e) => {
        handshaking = false;
        throw fail(e, CLOSE_PROTOCOL, 'handshake failed');
      });
    }

    async function handshake(token, deadline) {
      const s = subtle();
      const xPromise = s.generateKey({ name: 'X25519' }, false, ['deriveBits']);
      xPromise.catch(() => {});
      const device = parseDeviceToken(token);
      const pskPromise = pskFromSecret(device.secret);
      pskPromise.catch(() => {});

      const m1 = await nextFrame(deadline);
      if (m1.length !== M1_LEN || m1[0] !== VERSION || m1[1] !== T_M1) {
        throw fail(new Error('malformed handshake message'), CLOSE_PROTOCOL, 'malformed handshake');
      }
      const eS = m1.slice(2, 2 + DH_LEN);
      const ek = m1.slice(2 + DH_LEN);
      const sym = await Symmetric.create(device.label);
      await sym.mixHash(eS);
      await sym.mixHash(ek);

      const x = await xPromise;
      const eC = new Uint8Array(await s.exportKey('raw', x.publicKey));
      await sym.mixHash(eC);
      const peer = await s.importKey('raw', eS, { name: 'X25519' }, false, []);
      const dh = new Uint8Array(await s.deriveBits({ name: 'X25519', public: peer }, x.privateKey, 256));
      // A low-order point. WebCrypto refuses it itself; the check costs nothing.
      if (dh.every((b) => b === 0)) {
        throw fail(new Error('degenerate X25519 share'), CLOSE_PROTOCOL, 'malformed handshake');
      }
      await sym.mixKey(dh);
      const kem = await encapsulate(ek);
      await sym.mixHash(kem.ct);
      await sym.mixKey(kem.ss);
      await sym.mixKeyAndHash(await pskPromise);
      const tag = await sym.encryptAndHash(EMPTY);
      ws.send(concat([VERSION, T_M2], eC, kem.ct, tag));

      const m3 = await nextFrame(deadline);
      if (m3.length !== M3_LEN || m3[0] !== VERSION || m3[1] !== T_M3) {
        throw fail(new Error('malformed handshake message'), CLOSE_PROTOCOL, 'malformed handshake');
      }
      try {
        await sym.decryptAndHash(m3.slice(2));
      } catch (e) {
        // The server accepted OUR proof and could not produce its own: whoever answered
        // does not hold the token. Nothing is sent on this socket again.
        throw fail(new Error('the server could not prove it holds the token'),
                   CLOSE_PROTOCOL, 'server proof mismatch');
      }
      const [toExt, toSrv] = await sym.split();
      channel = { sendKey: await aesKey(toSrv), recvKey: await aesKey(toExt), sendN: 0, recvN: 0 };
      wire.kemPath = kem.path;
      // Anything the server sent after M3 arrived as a handshake frame; it is a record.
      const early = inbox.splice(0);
      for (const f of early) receive(f);
    }

    return wire;
  }

  // --- HTTP requests to the server's own port --------------------------------------
  //
  // The header proves the token without sending it: HMAC over the method, the path, a
  // timestamp, a random nonce and a hash of the body, under a key derived from the token.
  // The server rejects a stale timestamp, a nonce it has already seen, and a body that is
  // not the one hashed here (shared-py/browserplug_shared/request_signing.py). Pass the
  // exact body fetch() will send, a string or bytes; omit it for a request without one.
  // The scheme version is the header's first word. A request with a body is signed v2,
  // which covers the body. One without is signed v1, as the server's own signer does: v1
  // covers all a bodyless request carries, and a server older than v2 accepts it, so
  // Restart and the WAN diagnosis keep working against an older server after the
  // extension auto-updates. The bind panels' POST has a body; options.js postSignedBody
  // re-sends it as v1 when such a server refuses v2.
  async function signRequest(token, method, url, body) {
    const device = parseDeviceToken(token);
    const path = new URL(url).pathname;
    const ts = Math.floor(Date.now() / 1000);
    const nonceText = b64url(globalThis.crypto.getRandomValues(new Uint8Array(16)));
    const head = `${String(method).toUpperCase()}\n${path}\n${ts}\n${nonceText}`;
    if (body == null) {
      const key = await hmac(utf8.encode(HTTP_LABEL_V1), utf8.encode(device.secret));
      const mac = b64url(await hmac(key, utf8.encode(`${HTTP_LABEL_V1}\n${head}`)));
      return `v1 ${device.label || '-'} ${ts} ${nonceText} ${mac}`;
    }
    const key = await hmac(utf8.encode(HTTP_LABEL), utf8.encode(device.secret));
    const bodyHash = b64url(await sha256(bytesOf(body)));
    const mac = b64url(await hmac(key, utf8.encode(`${HTTP_LABEL}\n${head}\n${bodyHash}`)));
    return `v2 ${device.label || '-'} ${ts} ${nonceText} ${bodyHash} ${mac}`;
  }

  globalThis.BrowserplugWsCrypto = {
    createWire, signRequest, parseDeviceToken, DEVICE_TOKEN_RE, AUTH_HEADER, CLOSE_PROTOCOL
  };
})();
