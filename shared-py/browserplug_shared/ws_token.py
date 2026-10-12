#!/usr/bin/env python3
"""The extension WebSocket's shared token: where it comes from and where it lives.

Deliberately stdlib-only and free of any project import: `gen_ws_token.py` loads this
module directly (no `server/__init__.py`, hence no websockets/uvicorn/mcp) so the
rotation tool runs under any Python.

Resolution order (see `resolve_token`):

  1. BROWSERPLUG_WS_TOKEN  : explicit configuration; wins, and turns on strict auth.
  2. the token FILE        : what the server minted for itself on an earlier run.
  3. mint one              : `secrets.token_urlsafe(32)`, persisted to that file.

Step 3 is what makes the WebSocket authenticable out of the box. It MUST persist: a
token re-minted on every restart would silently invalidate every remote browser the
user configured.

The token is the ONE thing that does not live in `config.json`: it gets its own file in
the same state directory, so the settings writers (the options page's /ws-bind and
/reset-config) never hold the secret open. See config_store.py for the directory itself
and for every other persisted setting.

PROJECT RULE: no token value is ever hand-written, not here, not in tests, not in the
docs. Everything mints one at runtime. A test in tests/unit/test_ws_token.py greps the
whole tree to enforce it.
"""
import os
import secrets
import stat
import sys

# The state directory is config_store's to define; both files live in it.
from browserplug_shared import device_tokens
from browserplug_shared.config_store import state_dir

# Shared secret an extension presents in its extension_hello.
WS_TOKEN_ENV = "BROWSERPLUG_WS_TOKEN"
# Where the auto-generated token is persisted. Overridable so tests (and anyone with an
# unusual layout) never touch the real one.
WS_TOKEN_FILE_ENV = "BROWSERPLUG_WS_TOKEN_FILE"
# Opt in to a non-localhost WebSocket bind (server.py re-exports it; it lives here so the
# stdlib-only rotation tool can name it without importing the server).
WS_HOST_ENV = "BROWSERPLUG_WS_HOST"

# 32 bytes -> a 43-character URL-safe string (256 bits of entropy).
TOKEN_BYTES = 32

# Sources, in the order resolve_token() tries them.
SOURCE_EXPLICIT = "explicit"    # passed to BrowserPlugServer(ws_token=...)
SOURCE_ENV = "env"              # BROWSERPLUG_WS_TOKEN
SOURCE_FILE = "file"            # persisted by an earlier run
SOURCE_GENERATED = "generated"  # minted (and persisted) just now

# The two that mean "a human configured this on purpose". They enable STRICT auth: the
# user who sets a token is asking for one to be demanded, of every peer.
EXPLICIT_SOURCES = (SOURCE_EXPLICIT, SOURCE_ENV)


def generate_token():
    """A fresh, cryptographically random token. Different on every call."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def env_token():
    """The explicitly configured token, or None. A blank/whitespace value is NOT a
    token: an exported-but-empty shell var must never read as "auth configured"."""
    return (os.environ.get(WS_TOKEN_ENV) or "").strip() or None


def default_token_path():
    """Where the auto-generated token is persisted (BROWSERPLUG_WS_TOKEN_FILE overrides)."""
    override = (os.environ.get(WS_TOKEN_FILE_ENV) or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(state_dir(), "ws-token")


def read_token_file(path=None):
    """The persisted token, or None when there isn't one (or it can't be read)."""
    path = path or default_token_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def write_token_file(token, path=None):
    """Persist `token` with owner-only permissions. Returns the path, or None on failure.

    POSIX: created 0600 via O_CREAT|O_EXCL-less open with a mode, then chmod'd, so an
    existing file (rotation) is tightened too.

    WINDOWS: os.chmod only flips the read-only bit; NTFS permissions are ACLs and Python
    cannot express them. The file therefore relies on the ACL it INHERITS from
    %LOCALAPPDATA% (owner + SYSTEM + Administrators, i.e. not other, non-admin users).
    That is weaker than 0600 and it is not pretended otherwise: `perms_are_owner_only()`
    reports the truth for the caller/tests to assert on.
    """
    path = path or default_token_path()
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
            if os.name != "nt":
                try:
                    os.chmod(directory, 0o700)
                except OSError:
                    pass
        # Open with 0600 from the start on POSIX: a plain open() would briefly leave the
        # secret at the umask's mercy.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        if os.name != "nt":
            os.chmod(path, 0o600)   # an existing (rotated) file may have looser bits
        return path
    except OSError:
        return None


def perms_are_owner_only(path):
    """True when the OS really does keep this file to its owner.

    POSIX: mode is exactly 0600. Windows: NTFS has no such bit; always False, honestly
    (the file is protected only by the ACL it inherits from the per-user state dir).
    """
    if os.name == "nt":
        return False
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return False
    return mode == 0o600


def resolve_token(explicit=None, path=None, log=None, out=None):
    """(token, source): the token this server will require. Never returns None.

    Minting is the last resort. The first mint shows a device token for a remote browser
    (shared-py/browserplug_shared/device_tokens.py), but only on `out` (default: this process's stdout) and only
    when that is a terminal. `log` gets the location of the token file and how to read it,
    never a token: the log is a file that outlives the process and gets shared. The master
    itself is never shown: no browser should hold it.

    If it cannot be PERSISTED (read-only home, no permission), the minted token still
    stands for this process; a local browser can pull it from the /ws-token endpoint and
    keep working, but it will differ after a restart, so that case is logged as the
    warning it is.
    """
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip(), SOURCE_EXPLICIT

    from_env = env_token()
    if from_env:
        return from_env, SOURCE_ENV

    path = path or default_token_path()
    stored = read_token_file(path)
    if stored:
        return stored, SOURCE_FILE

    token = generate_token()
    written = write_token_file(token, path)
    if written:
        stream = out if out is not None else sys.stdout
        try:
            on_tty = bool(stream and stream.isatty())
        except (AttributeError, ValueError):
            on_tty = False
        if on_tty:
            print("\n".join([
                "",
                "  " + "-" * 68,
                "  BrowserPlug generated a WebSocket token for this server (first run).",
                "  Stored in: " + written,
                "",
                "  A browser on THIS machine configures itself automatically -- nothing to do.",
                "  A browser on ANOTHER machine needs a device token of its own, pasted into",
                "  its extension Options -> Server connection -> Connection token. Here is one:",
                "",
                "      " + device_tokens.issue(token),
                "",
                "  Make one per browser: python gen_ws_token.py --device",
                "  Treat them like passwords. To revoke them all: python gen_ws_token.py --write",
                "  " + "-" * 68,
                "",
            ]), file=stream, flush=True)
        if log:
            log("BrowserPlug generated a WebSocket token (first run) and stored it in %s. "
                "Device tokens are not logged. To get one for a remote browser run "
                "`python gen_ws_token.py --device`; the master itself is the plain-text "
                "file above." % written)
    elif log:
        log("Could not persist the WebSocket token to %s -- using a token that only "
            "lasts for this process. Remote browsers configured with the old value "
            "will be rejected after this restart; set %s to a fixed value (generate "
            "one with `python gen_ws_token.py`) to make it stable."
            % (path, WS_TOKEN_ENV))
    return token, SOURCE_GENERATED


if __name__ == "__main__":   # tiny smoke path: print where the token lives
    print(default_token_path())
    sys.exit(0)
