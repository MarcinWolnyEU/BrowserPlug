#!/usr/bin/env python3
"""The server's persistent settings: ONE directory, ONE JSON file.

Everything the server remembers across restarts lives in a single per-user state
directory:

  Windows: %LOCALAPPDATA%\\browserplug
  POSIX:   $XDG_STATE_HOME/browserplug, else ~/.local/state/browserplug

  config.json   settings (this module; `ws` lives here, `default_browser` in
                default_browser.py, both through read_section/write_section)
  ws-token      the WebSocket secret (ws_token.py)

The token is deliberately NOT in config.json. `POST /ws-bind` and `POST /reset-config`
write settings from the options page, i.e. from a browser; keeping the secret in a file
those paths never open means a bug in the settings writer cannot truncate the one value
whose loss locks out every remote browser. It also keeps config.json a file the user can
paste into a bug report, which the project rule in ws_token.py (no token value is ever
written down) depends on.

JSON, not TOML: the stdlib reads AND writes it. `tomllib` is read-only, and a writer
would mean a dependency in a module that must stay importable by `gen_ws_token.py`
without the server package.

Writes are atomic (temp file in the same directory, then os.replace), so a crash or a
full disk leaves the previous config intact rather than a truncated one. Unreadable or
malformed content reads as "no settings", never as an error: a corrupt file must not
stop the server from starting on its defaults.
"""
import json
import os
import tempfile

# Relocates the whole directory -- config and token together. This is the single knob
# tests use to keep their hands off the developer's real settings.
STATE_DIR_ENV = "BROWSERPLUG_STATE_DIR"

CONFIG_FILE_NAME = "config.json"

# Bumped only for a change old code cannot read; `load()` tolerates anything it can parse.
CONFIG_VERSION = 1

# The two modes a NON-local bind can be in. "local" is never stored: it is derived from
# the bind address being localhost, so the stored values can never contradict each other.
WS_MODE_VALUES = ("lan", "wan")


def state_dir():
    """The per-user directory holding every persistent file this server owns.

    Deliberately not the system temp dir: the token must survive a reboot (every
    configured remote browser breaks otherwise) and temp is world-readable on POSIX.
    """
    override = (os.environ.get(STATE_DIR_ENV) or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "browserplug")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "browserplug")


def config_path():
    """The settings file. There is exactly one, and this is where it is."""
    return os.path.join(state_dir(), CONFIG_FILE_NAME)


def load():
    """The stored settings as a dict; `{}` when there is nothing usable to read.

    A missing file, an unreadable one, malformed JSON and a JSON document that is not
    an object all mean the same thing to every caller: no settings, use the defaults.
    """
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            parsed = json.load(fh)
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def save(config):
    """Write `config` atomically. Returns the path, or None if it could not be written.

    The temp file is created in the TARGET directory (os.replace cannot cross a
    filesystem) and removed if anything goes wrong, so a failed write leaves neither a
    half-written config nor litter.
    """
    target = config_path()
    directory = os.path.dirname(target)
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        payload = dict(config)
        payload["version"] = CONFIG_VERSION
        fd, tmp = tempfile.mkstemp(prefix=CONFIG_FILE_NAME + ".", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        return target
    except OSError:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return None


def reset():
    """Delete the settings file: the server reverts to fresh-install defaults.

    True when the file is gone afterwards, which INCLUDES it never having existed --
    the caller asked for "no stored settings", and that is the state either way. False
    only when something is still there and could not be removed.
    """
    try:
        os.remove(config_path())
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return not os.path.exists(config_path())


def _read_ws(key):
    ws = load().get("ws")
    if not isinstance(ws, dict):
        return None
    value = ws.get(key)
    return value.strip() or None if isinstance(value, str) else None


def _write_ws(key, value):
    """Set (or, with a falsy value, remove) one key of the `ws` section.

    Read-modify-write: this file is the only carrier for every setting, so a writer that
    dropped the keys it doesn't own would silently reset them.
    """
    config = load()
    ws = dict(config.get("ws") or {}) if isinstance(config.get("ws"), dict) else {}
    if value:
        ws[key] = value
    else:
        ws.pop(key, None)
    if ws:
        config["ws"] = ws
    else:
        config.pop("ws", None)
    return save(config)


def read_section(name):
    """One top-level section of the config as a dict; `{}` when it is absent or is not
    an object. Every section reader wants the same tolerance the whole file gets."""
    value = load().get(name)
    return dict(value) if isinstance(value, dict) else {}


def write_section(name, value):
    """Replace one top-level section (a falsy/empty value removes it).

    Read-modify-write, like _write_ws and for the same reason: this file carries every
    setting, so a writer that rebuilt it from its own keys would drop the rest.
    """
    config = load()
    if value:
        config[name] = dict(value)
    else:
        config.pop(name, None)
    return save(config)


def read_ws_host():
    """The persisted WebSocket bind address, or None."""
    return _read_ws("host")


def write_ws_host(host):
    """Persist the bind address (None/'' clears it -> back to the built-in default).

    This is what makes the options page's "allow browsers on other machines" button
    outlive a restart: the env var it would otherwise need cannot be set from inside a
    browser, and the always-on server is launched by a scheduled task/service that never
    sees a shell's environment.
    """
    return _write_ws("host", str(host).strip() if host else None)


def read_ws_mode():
    """The persisted "lan"/"wan" choice, or None. Anything else reads as absent, never
    as an error; the caller's default ("lan") is always a safe answer."""
    value = _read_ws("mode")
    return value if value in WS_MODE_VALUES else None


def write_ws_mode(mode):
    """Persist the LAN/WAN choice (None/'' clears it). A value outside WS_MODE_VALUES is
    refused (returns None, nothing written)."""
    if mode and mode not in WS_MODE_VALUES:
        return None
    return _write_ws("mode", mode or None)


if __name__ == "__main__":   # tiny smoke path: print where the settings live
    print(config_path())
