"""How a local caller proves the master token to the running server.

`signed_headers` is the one place that knows how a request from this machine is signed
(server/control.py and the launcher both call it), so it lives where both can import it.
Stdlib only, no import of the server.
"""

from browserplug_shared import device_tokens, request_signing, ws_token

# The device label local callers sign under. Any valid label works: the server derives the
# secret from the master and the label, and keeps no list of issued ones.
LABEL = "local-ctl"

_master_cache = None


def master_token():
    """The master token this machine's server uses, or None. Cached: the launcher signs
    every tools/call and the token only changes when the user rotates it."""
    global _master_cache
    if _master_cache is None:
        _master_cache = ws_token.env_token() or ws_token.read_token_file()
    return _master_cache


def signed_headers(method, path, body=None):
    """{header: value} proving the token for one request, or {} when there is no token
    to prove (the server would refuse the call, and the caller reports that). A request
    that sends a body must pass its exact bytes: the signature covers them (S7)."""
    master = master_token()
    if not master:
        return {}
    secret = device_tokens.secret_for(master, LABEL)
    if secret is None:
        return {}
    return {request_signing.AUTH_HEADER: request_signing.sign(LABEL, secret, method, path, body=body)}
