"""macOS `.app` bundle facts that browser identity needs.

A Mac browser is a bundle, and its helper processes live inside it: Chrome keeps
`Google Chrome Helper (Renderer).app` under
`Contents/Frameworks/Google Chrome Framework.framework/Versions/<v>/Helpers/`, Firefox keeps
`plugin-container.app` under `Contents/MacOS/`. The process that owns a page's socket is one
of those helpers, so the question "which browser is this?" is answered by the OUTERMOST
bundle, not by the helper's own name or directory.

Pure stdlib, no side effects on import. The only I/O is one cached read of a bundle's
Info.plist and a few cached `exists` probes. Every function accepts a non-Mac path and
answers "no bundle", so callers may pass any executable path on any OS.
"""

import glob
import os
import plistlib
import re
from typing import Optional

# `Google Chrome Helper`, `Google Chrome Helper (Renderer)`, `Brave Browser Helper (GPU)`.
# The parenthesised part is Chromium's sandbox role: Renderer, GPU, Plugin, Alerts.
_HELPER_RE = re.compile(r"^(?P<base>.+?) helper(?: \([^)]*\))?$", re.IGNORECASE)

# The program that starts a bundle through LaunchServices. An absolute path: the server
# inherits the MCP client's PATH, and a different `open` earlier on it would run instead.
OPEN = "/usr/bin/open"

# Chrome runs from an APFS clone of its own bundle so an update can replace the installed
# copy under a running browser. psutil then reports the clone as the executable:
# `/private/var/folders/..../com.google.Chrome.canary.code_sign_clone/code_sign_clone.X/
# Google Chrome Canary.app.bundle/Contents/MacOS/Google Chrome Canary` (measured on Canary
# 159). The clone is deleted when the browser exits, so it is a bundle for naming purposes
# and no use for starting the browser again.
_BUNDLE_SUFFIXES = (".app.bundle", ".app")
_CLONE_MARKER = ".code_sign_clone"
_INSTALL_DIRS = ("/Applications", "~/Applications")

_exe_name_cache = {}
_runtime_cache = {}


def outermost_bundle(exe_path: Optional[str]) -> Optional[str]:
    """The outermost `.app` directory an executable sits in, or None.

    `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` and a helper nested ten
    directories deeper both answer `/Applications/Google Chrome.app`. Matched on the path
    text: the Windows spelling of a Mac path (a test fixture) answers the same way. Chrome's
    code-signing clone (`Google Chrome Canary.app.bundle`) counts as a bundle too.
    """
    if not exe_path:
        return None
    parts = exe_path.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        # i > 0: a path that BEGINS with "x.app" is a relative name, not a bundle.
        if i > 0 and _bundle_suffix(part):
            return "/".join(parts[: i + 1])
    return None


def _bundle_suffix(name):
    low = name.lower()
    for suffix in _BUNDLE_SUFFIXES:
        if low.endswith(suffix) and len(name) > len(suffix):
            return suffix
    return None


def bundle_name(bundle: Optional[str]) -> Optional[str]:
    """`/Applications/Google Chrome Canary.app` -> `Google Chrome Canary`."""
    if not bundle:
        return None
    leaf = bundle.replace("\\", "/").rsplit("/", 1)[-1]
    suffix = _bundle_suffix(leaf)
    return leaf[: -len(suffix)] if suffix else leaf


def is_code_sign_clone(path: Optional[str]) -> bool:
    """Is this path inside one of Chrome's temporary code-signing clones?"""
    return _CLONE_MARKER in (path or "")


def installed_bundle(exe_path: Optional[str]) -> Optional[str]:
    """The bundle to START for the browser that runs from `exe_path`, or None.

    An ordinary bundle answers itself. A code-signing clone answers the installed copy of
    the same name, looked for in /Applications and ~/Applications, because the clone
    disappears with the process.
    """
    bundle = outermost_bundle(exe_path)
    if not bundle or not is_code_sign_clone(bundle):
        return bundle
    for base in _INSTALL_DIRS:
        candidate = os.path.join(os.path.expanduser(base), (bundle_name(bundle) or "") + ".app")
        if os.path.isdir(candidate):
            return candidate.replace("\\", "/")
    return None


def installed_executable(exe_path: Optional[str]) -> Optional[str]:
    """`exe_path` itself, or for a code-signing clone the same program in the installed
    bundle (None when that copy cannot be found)."""
    if not exe_path or not is_code_sign_clone(outermost_bundle(exe_path)):
        return exe_path
    bundle = installed_bundle(exe_path)
    if not bundle:
        return None
    name = main_executable_name(bundle)
    return os.path.join(bundle, "Contents", "MacOS", name).replace("\\", "/") if name else None


def main_executable_name(bundle: Optional[str]) -> Optional[str]:
    """CFBundleExecutable of a bundle: the name of the browser's own process.

    Read from Info.plist; a bundle whose plist cannot be read falls back to its only file
    in Contents/MacOS, then to the bundle's own name. Cached per bundle.
    """
    if not bundle:
        return None
    if bundle in _exe_name_cache:
        return _exe_name_cache[bundle]
    name = None
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
            name = (plistlib.load(fh).get("CFBundleExecutable") or "").strip() or None
    except Exception:
        name = None
    if not name:
        try:
            entries = [e for e in os.listdir(os.path.join(bundle, "Contents", "MacOS"))
                       if not e.startswith(".")]
            name = entries[0] if len(entries) == 1 else None
        except Exception:
            name = None
    _exe_name_cache[bundle] = name or bundle_name(bundle)
    return _exe_name_cache[bundle]


def is_helper_exe(exe_path: Optional[str]) -> bool:
    """True for an executable inside a bundle that is NOT the bundle's main executable.

    Nested bundles are always helpers (`Google Chrome Helper.app`, `plugin-container.app`).
    Only the first level, `<bundle>/Contents/MacOS/<main>`, is the browser itself.
    """
    bundle = outermost_bundle(exe_path)
    if not bundle:
        return False
    rest = exe_path.replace("\\", "/")[len(bundle):].strip("/").split("/")
    if len(rest) == 3 and rest[0] == "Contents" and rest[1] == "MacOS":
        main = main_executable_name(bundle)
        return bool(main) and rest[2] != main
    return True


def helper_base_name(proc_name: Optional[str]) -> Optional[str]:
    """`Google Chrome Helper (Renderer)` -> `Google Chrome`; None when it is no helper name."""
    m = _HELPER_RE.match((proc_name or "").strip())
    return m.group("base") if m else None


def runtime_engine(bundle: Optional[str]) -> Optional[str]:
    """`"gecko"` / `"blink"` when the bundle carries a browser runtime, else None.

    The macOS counterpart of the runtime file beside a Windows executable, for a browser
    the name tables have never heard of. Gecko keeps `XUL` beside its executable. A Chromium
    browser keeps `<exe name> Framework.framework` holding `chrome_100_percent.pak`. Electron
    apps carry the same .pak but name the framework `Electron Framework` whatever the app is
    called, so a framework named for the app is the tell, and a bare `Electron` is no browser.
    """
    if not bundle:
        return None
    if bundle in _runtime_cache:
        return _runtime_cache[bundle]
    engine = None
    try:
        exe_name = main_executable_name(bundle) or ""
        if os.path.exists(os.path.join(bundle, "Contents", "MacOS", "XUL")):
            engine = "gecko"
        elif exe_name.lower() != "electron" and glob.glob(os.path.join(
                glob.escape(bundle), "Contents", "Frameworks",
                glob.escape(f"{exe_name} Framework.framework"), "Versions", "*", "Resources",
                "chrome_100_percent.pak")):
            engine = "blink"
    except Exception:
        engine = None
    _runtime_cache[bundle] = engine
    return engine


def open_argv(exe_path: Optional[str], args=()) -> Optional[list]:
    """argv that starts the browser owning `exe_path` through LaunchServices, or None when
    the path is no bundle.

    `-n` asks for a new process, so a browser that is already running behaves as it does
    when its binary is started directly: Chrome's process singleton and Firefox's remoting
    hand the request to the running instance and the new process exits. Without `-n`, `open`
    only brings the running app forward and drops `args`, so a launch_args profile would be
    ignored exactly when it matters. Arguments follow `--args` and reach the browser as is.
    """
    bundle = installed_bundle(exe_path)
    if not bundle:
        return None
    argv = [OPEN, "-n", "-a", bundle]
    if args:
        argv += ["--args", *args]
    return argv


def clear_caches():
    """For tests: forget every cached plist and runtime probe."""
    _exe_name_cache.clear()
    _runtime_cache.clear()
