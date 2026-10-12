#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Make BrowserPlug device tokens, or rotate the server's master token.

You do not need this to get started. The server MINTS its master token the first time it
runs and persists it (owner-only); a browser on the same machine then fetches a device
token of its own over localhost with no user action at all.

Every browser holds its OWN device token, derived from the master
(shared-py/browserplug_shared/device_tokens.py). The master never leaves the server, and one
browser's token cannot be used to impersonate the server to another.

    python gen_ws_token.py --device   # a new device token for a browser on another machine
    python gen_ws_token.py --write    # rotate the master: revokes EVERY device token
    python gen_ws_token.py            # print a fresh master value, e.g. to pin in the env

After --write, RESTART the server (it reads the file at startup) and give every REMOTE
browser a new device token. Same-machine browsers fetch a new one themselves. Pinning
BROWSERPLUG_WS_TOKEN in the environment overrides the file entirely; then rotation means
changing that variable, and --device derives from it.

This script is the only place a token value ever comes from: never type or invent one.
"""
import argparse
import os
import sys

# Import from shared-py/ (stdlib only), NOT from the server package: this tool must run
# under any Python, with none of the server's dependencies installed.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared-py"))
from browserplug_shared import device_tokens, ws_token  # noqa: E402

# The server mints with exactly this function; one implementation, one place.
generate_token = ws_token.generate_token


def current_master():
    """The master the server would use: the env var, else the token file."""
    return ws_token.env_token() or ws_token.read_token_file()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--device", action="store_true",
                      help="print a new device token for a browser on another machine")
    mode.add_argument("--write", action="store_true",
                      help="replace the server's master token file (revokes every device "
                           "token; refused while the environment variable pins a token; "
                           "default: only print a fresh master)")
    args = parser.parse_args()

    if args.device:
        master = current_master()
        if not master:
            print("No master token yet: start the server once (it mints one), or set "
                  f"{ws_token.WS_TOKEN_ENV}.")
            return 1
        print("New BrowserPlug device token (one per browser; a fresh one on every run):")
        print()
        print(f"    {device_tokens.issue(master)}")
        print()
        print("Paste it into the REMOTE browser's extension Options -> Server connection ->")
        print("Connection token. Give each browser its own; never share one between two.")
        return 0

    token = ws_token.generate_token()
    path = ws_token.default_token_path()

    if args.write and ws_token.env_token():
        # The environment value beats the file, so a rotation here would revoke nothing
        # and any device token derived from the new file master would be rejected.
        print(f"{ws_token.WS_TOKEN_ENV} is set in this environment, and it overrides the token")
        print(f"file entirely. Nothing was written to {path}; the old token is still in force.")
        print()
        print("To rotate, change that variable: pin a new value (python gen_ws_token.py prints")
        print("one), restart the server, then give every REMOTE browser a new device token:")
        print("    python gen_ws_token.py --device")
        print("To rotate the file instead, unset the variable first and run --write again.")
        return 1

    if args.write:
        written = ws_token.write_token_file(token, path)
        if not written:
            print(f"FAILED to write {path} -- the old token is still in force.")
            return 1
        owner_only = ws_token.perms_are_owner_only(written)
        print(f"Rotated the master token in: {written}")
        print("  Permissions: owner-only (0600)." if owner_only else
              "  Permissions: whatever that directory grants -- on Windows there is no "
              "0600;\n  the file inherits the per-user ACL of its folder.")
        print()
        print("Every device token issued so far is now revoked. Next:")
        print("  1. RESTART the server -- it reads this file at startup.")
        print("  2. Give every REMOTE browser a new device token:")
        print("         python gen_ws_token.py --device")
        print("     A browser on the SAME machine as the server fetches one by itself.")
        print()
        print("A device token for one remote browser, derived from the new master:")
        print()
        print(f"    {device_tokens.issue(token)}")
    else:
        print("New BrowserPlug master token (random -- a fresh one on every run):")
        print()
        print(f"    {token}")
        print()
        print("Nothing was changed. The master stays on the server; browsers get device")
        print("tokens derived from it (python gen_ws_token.py --device). To use this value:")
        print()
        print("    python gen_ws_token.py --write     # replaces " + path)
        print()
        print("Or pin it in the environment instead of the file (this wins over it):")
        print(f"     PowerShell:  $env:{ws_token.WS_TOKEN_ENV} = '<the token above>'")
        print(f"     bash:        export {ws_token.WS_TOKEN_ENV}='<the token above>'")

    print()
    print("Notes:")
    print(f"  * To expose the WebSocket to another machine, set {ws_token.WS_HOST_ENV} "
          f"(or --ws-host).")
    print("  * The connection is encrypted with the device token, but treat tokens like")
    print("    passwords: don't commit them, don't paste them in chat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
