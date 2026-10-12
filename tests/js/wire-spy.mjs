/*
 * A spy on ws-crypto.js createWire for the JS twin tests that slice background.js.
 *
 * With a token, the extension no longer puts the token in its hello: it opens the
 * encrypted handshake and the hello leaves as the first sealed record. The fake sockets in
 * those harnesses have no server to finish a handshake with, so these tests read what the
 * wire was STARTED with instead: the token it proves, and the hello it would seal.
 * The cryptography itself is covered against the real server by
 * tests/unit/test_ws_crypto_interop.py.
 */
export const REQ_FRAME = [1, 0];

export function spyWires(api) {
  const started = new WeakMap();   // socket -> { token, buildHello }
  const createWire = api.createWire;
  api.createWire = (ws, handlers) => {
    const wire = createWire(ws, handlers);
    const start = wire.start;
    wire.start = (token, buildHello, opts) => {
      started.set(ws, { token, buildHello });
      return start(token, buildHello, opts);
    };
    return wire;
  };
  return {
    tokenOf: (ws) => (started.get(ws) || {}).token,
    // The hello this socket's wire seals once the handshake completes.
    encryptedHelloOf: (ws) => {
      const s = started.get(ws);
      return s ? JSON.parse(s.buildHello(true)) : undefined;
    },
    // 01 00 | label length | label (the device token's label, empty for any other token).
    isRequestFrame: (frame) => frame instanceof Uint8Array && frame.length >= 3
      && frame[0] === REQ_FRAME[0] && frame[1] === REQ_FRAME[1] && frame.length === 3 + frame[2],
  };
}
