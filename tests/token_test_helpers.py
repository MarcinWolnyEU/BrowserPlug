"""Helpers the tests use to play the client side of the token and handshake protocol.

Each of these had no caller in `server/`: the extension is JavaScript, and the server checks
tokens only through `hmac.compare_digest` on its own derived secrets.
"""

import hmac

from browserplug_shared import device_tokens
from server.ws.ws_crypto import T_REQ, VERSION


def is_valid_device_token(master: str, token) -> bool:
    """True if `token` is a device token derived from `master`."""
    parsed = device_tokens.parse(token)
    if not parsed:
        return False
    expected = device_tokens.secret_for(master, parsed[0])
    return expected is not None and hmac.compare_digest(expected, parsed[1])


def request_frame(label: str) -> bytes:
    """The extension's opening frame of the encrypted handshake, naming its device label."""
    raw = label.encode("ascii")
    return bytes([VERSION, T_REQ, len(raw)]) + raw

