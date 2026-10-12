"""May a new socket take over the registry record of an instance_id that is still live?

The extension_hello names its browser with an `instance_id` it mints itself, and `/health`
publishes every id. Left unchecked, any authenticated peer could copy one, and the registry
would move that browser's record (its slot, its history, the agents' selection) onto the
peer's socket: tool calls, typed text and eval code would then go to the peer, which could
answer with forged pages.

So a takeover of a record whose socket is still open must be proved. Nothing is checked
when the old socket is already dead: that is the ordinary restart or reload, and there is
nothing left to displace.

Proof, strongest first:
  * both sockets logged in with the same device token (`device_label`, which the encrypted
    handshake verified): the same device. Two different labels never match.
  * both peers local and the process probe found the same pid on both: the same browser
    process (an extension reload keeps it).
  * both peers remote, neither with a device label (a build or setup that has none), from
    the same address: weak, and the only thing left.
Anything else is refused. A local and a remote peer never match, and unknown pids on a
local peer are not a match: an unproven claim loses.

Pure and stdlib only, like request_queue.py; the registry calls it from `promote`.
"""


def _label(record):
    value = getattr(record, "device_label", "")
    return value if isinstance(value, str) else ""


def claim_verdict(established, newcomer):
    """(allowed, reason). `established` is the live record, `newcomer` the provisional
    record of the socket that sent the hello. `reason` is log text when refused."""
    old_label, new_label = _label(established), _label(newcomer)
    if old_label and new_label:
        if old_label == new_label:
            return True, ""
        return False, "it logged in with a different device token than the live connection"

    old_remote = bool(getattr(established, "is_remote", False))
    new_remote = bool(getattr(newcomer, "is_remote", False))
    if old_remote != new_remote:
        return False, "the live connection is " + ("remote" if old_remote else "local") \
            + " and this one is " + ("remote" if new_remote else "local")

    if new_remote:
        old_addr = getattr(established, "remote_addr", None)
        if not old_label and not new_label and old_addr and old_addr == getattr(newcomer, "remote_addr", None):
            return True, ""
        return False, "it cannot show it is the same device as the live connection"

    old_pid = getattr(established, "browser_pid", None)
    new_pid = getattr(newcomer, "browser_pid", None)
    if old_pid and new_pid and old_pid == new_pid:
        return True, ""
    return False, "it is not the same browser process as the live connection"
