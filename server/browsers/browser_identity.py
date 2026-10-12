"""Which browser a connection is, and what to call it.

Firefox forks and Chromium browsers run the extension unchanged and mostly answer the
extension API with the upstream brand ("Firefox", "Google Chrome"). For a local browser
the process is the ground truth, so the brand tables and the runtime-sibling probe below
name it from the executable. A remote browser has no process on this machine, so its own
hello (`browserInfo`, `platform`) is all there is, and claimed_names, fork_from_browser_info
and platform_edition read that.

ExtBrowser is the registry record for one connected browser. It owns the naming
properties (family, edition, display_name, name, describe) built from those pieces.

The only I/O is one cached `os.path.exists` probe next to an executable. Nothing here
imports server.py, and importing the module has no side effects.
"""

import os
from datetime import datetime
from typing import Optional

# --- Which browser is this, really? -----------------------------------------
# Firefox FORKS run the extension unchanged, but most of them still answer
# `runtime.getBrowserInfo()` with name "Firefox"; Zen does (live-verified: it
# reports {name: "Firefox", version: "152.0.5"}). So the extension CANNOT tell us
# which browser it is, and the local PROCESS is the only ground truth. Everything
# that used to test `"firefox" in process_name` goes through here instead, or a
# fork reads as an unidentified browser with no pid/exe at all.
#
# Keys are process-name stems (no extension, no `-bin`/`-esr` suffix).
GECKO_PROCESS_BRANDS = {
    "firefox": "Firefox",
    "zen": "Zen Browser",
    "librewolf": "LibreWolf",
    "waterfox": "Waterfox",
    "floorp": "Floorp",
    "mullvadbrowser": "Mullvad Browser",
    # Tor Browser ships its binary AS firefox(.exe); already covered above.
}

# Chromium-based browsers all run the (MV3) extension unchanged and, like the Gecko
# forks, cannot self-identify their exact brand over the extension API; the local
# PROCESS is the ground truth for a LOCAL one. Keys are process-name stems.
CHROMIUM_PROCESS_BRANDS = {
    "chrome": "Google Chrome",
    "msedge": "Microsoft Edge",
    "brave": "Brave",
    "chromium": "Chromium",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
    "thorium": "Thorium",  # a common Chromium fork; extend as needed
}

# Runtime library, next to the executable: the fallback tell for a browser/fork we
# have never heard of. Positive proof it's a browser, which the pid-merge path
# needs (see _probe_browser_identity) and a bare "not one of our processes" guess
# could never give.
_GECKO_RUNTIME_FILES = ("xul.dll", "libxul.so", "XUL")
# The Chromium analog of xul.dll. Verified against dist/chrome-for-testing/chrome-win64/
# (v151 and v154): chrome_elf.dll, chrome.dll and chrome_100_percent.pak all sit beside chrome.exe.
# chrome_elf.dll / chrome.dll are the durable Windows tells; chrome_crashpad_handler is the
# POSIX one; the .pak is corroboration. icudtl.dat is deliberately EXCLUDED: it is not
# unique to Chromium (other apps ship ICU) so it would false-positive an unknown process.
_CHROMIUM_RUNTIME_FILES = ("chrome_elf.dll", "chrome.dll",
                           "chrome_100_percent.pak", "chrome_crashpad_handler")
# Per-(dir, file-set) cache shared by every runtime-sibling probe. Named _gecko_dir_cache
# for the importer that clears it in tests; it now holds both gecko and chromium entries.
_gecko_dir_cache = {}


def process_stem(proc_name):
    """`firefox.exe` -> `firefox`; `waterfox-bin` -> `waterfox`."""
    stem = os.path.splitext((proc_name or "").strip().lower())[0]
    for suffix in ("-bin", "-esr", "-default"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def _brand_from_stem(stem):
    """Derive a display brand from a process stem: `moon-wolf` -> `Moon Wolf`."""
    return stem.replace("-", " ").replace("_", " ").title()


def _dir_has_file(exe_path, files):
    """Does any of `files` sit beside this executable? Cached per (directory, file-set)."""
    if not exe_path:
        return False
    d = os.path.dirname(exe_path)
    key = (d, files)
    if key not in _gecko_dir_cache:
        try:
            _gecko_dir_cache[key] = any(
                os.path.exists(os.path.join(d, f)) for f in files)
        except Exception:
            _gecko_dir_cache[key] = False
    return _gecko_dir_cache[key]


def browser_brand(proc_name, exe_path=None):
    """(family, engine) of a process (engine in {"gecko", "blink"}), or (None, None)
    if it is not a browser at all (a python test harness faking the extension, say).

    An unknown fork still resolves: if a Gecko OR Chromium runtime sits next to the
    exe, the brand is derived from the executable's own name (and the engine from
    whichever runtime it is) rather than dropped.
    """
    stem = process_stem(proc_name)
    if not stem:
        return None, None
    if stem in GECKO_PROCESS_BRANDS:
        return GECKO_PROCESS_BRANDS[stem], "gecko"
    if stem in CHROMIUM_PROCESS_BRANDS:
        return CHROMIUM_PROCESS_BRANDS[stem], "blink"
    if _dir_has_file(exe_path, _GECKO_RUNTIME_FILES):
        return _brand_from_stem(stem), "gecko"
    if _dir_has_file(exe_path, _CHROMIUM_RUNTIME_FILES):
        return _brand_from_stem(stem), "blink"
    return None, None


def gecko_variant(brand, exe_path):
    """The specific EDITION within a Gecko brand, when the install path names one
    (Firefox ships several side by side). Falls back to the brand itself."""
    path_low = (exe_path or "").lower()
    if brand == "Firefox":
        for marker, edition in (("developer edition", "Firefox Developer Edition"),
                                ("nightly", "Firefox Nightly"),
                                ("esr", "Firefox ESR")):
            if marker in path_low:
                return edition
    return brand


def chromium_variant(brand, exe_path):
    """The specific CHANNEL within a Chromium brand, when the install path names one
    (Chrome/Edge ship Beta/Dev/Canary side by side). Falls back to the brand itself.
    Markers carry a leading space so a stray "dev"/"beta" substring never matches."""
    path_low = (exe_path or "").lower()
    for marker, channel in ((" canary", "Canary"), (" sxs", "Canary"),
                            (" beta", "Beta"), (" dev", "Dev")):
        if marker in path_low:
            return f"{brand} {channel}"
    return brand


def browser_variant(family, engine, exe_path):
    """Edition/channel for a family, routed by engine. Gecko -> gecko_variant,
    Blink -> chromium_variant. Falls back to the family name."""
    if not family:
        return None
    if engine == "blink":
        return chromium_variant(family, exe_path)
    return gecko_variant(family, exe_path)


# What runtime.getBrowserInfo() is DOCUMENTED to return, plus the Chromium port's
# authoritative self-declared `engine` (a plain string, "blink"). Anything ELSE in there
# is the browser adding something of its own, which is exactly what we are looking for.
# `engine` is listed so a future Chromium key carrying a `.version` object isn't misread
# as a gecko fork by fork_from_browser_info.
STANDARD_BROWSER_INFO_KEYS = {"name", "vendor", "version", "buildID", "engine",
                              "userAgentData"}

# Chromium brand names a Blink browser reports in browserInfo.name (from userAgentData).
CHROMIUM_BRAND_NAMES = set(CHROMIUM_PROCESS_BRANDS.values())

# A fork's own key -> the name people call it. An unrecognized key still identifies a
# fork (see fork_from_browser_info); this table only makes the display name nicer.
FORK_INFO_BRANDS = {"zen": "Zen Browser"}


def fork_from_browser_info(browser_info):
    """(brand, fork_version) when a Firefox FORK slips its identity into the one API it
    answers: runtime.getBrowserInfo(). The only self-report a fork actually makes, and
    the only one that reaches us from a REMOTE browser.

    Zen 152 answers (live-verified):
        {name: "Firefox", vendor: "Mozilla", version: "152.0.5",
         buildID: "20260708113753", zen: {version: "1.21.6b"}}
    It lies on `name` AND `vendor`, but adds `zen`, which is both the tell and its real
    version (nothing else exposes that: `version` is the Firefox it is built on).

    An extra key we have never seen still means "not plain Firefox", so it names the
    fork itself. It must LOOK like a fork block though: a version-carrying object, or a
    future Firefox adding an ordinary field would be mistaken for a whole new browser.
    """
    if not isinstance(browser_info, dict):
        return None, None
    for key, value in browser_info.items():
        if key in STANDARD_BROWSER_INFO_KEYS or not isinstance(key, str):
            continue
        version = value.get("version") if isinstance(value, dict) else None
        if version is None and key.lower() not in FORK_INFO_BRANDS:
            continue        # not a fork block, just a field we don't know
        brand = (FORK_INFO_BRANDS.get(key.lower())
                 or key.replace("_", " ").replace("-", " ").title())
        return brand, (str(version) if version is not None else None)
    return None, None


def claimed_names(browser_info):
    """(family, edition) from what the extension CLAIMS about itself. Used when there is
    no process to probe, i.e. for a REMOTE browser.

    A fork is named by its own key (above). Failing that, all we have is `name`, where a
    fork says "Firefox", so an unbranded remote fork reads as Firefox. That is expected:
    the browser's real name lives in chrome://branding, which an extension is FORBIDDEN
    to read ("may not load or link to chrome://branding/…": a platform rule, verified
    against Firefox 153; see background.js). There is nothing else to reach for.
    """
    if not isinstance(browser_info, dict):
        return None, None
    brand, _ = fork_from_browser_info(browser_info)
    if brand:
        return brand, brand
    name = (browser_info.get("name") or "").strip()
    engine = (browser_info.get("engine") or "").strip().lower()
    if engine == "blink" or name in CHROMIUM_BRAND_NAMES:
        # A Blink browser self-declares its engine and names itself correctly
        # (Chrome/Edge/Brave/…): it must NEVER fall through to the "Firefox" default.
        return (name or "Chromium"), (name or "Chromium")
    if not name:
        return None, None
    family = "Firefox" if "firefox" in name.lower() else name
    return family, name


# runtime.getPlatformInfo().os values that mean a phone or a tablet, mapped to the word
# people write. Every other value (win / mac / linux / openbsd / …) is a desktop, where
# the browser's own name is already the name its users say.
MOBILE_PLATFORM_OS = {"android": "Android", "ios": "iOS"}

# What a gecko browser appends to its own name to say which RELEASE TRAIN it is, as
# opposed to which PRODUCT it is. "Firefox Nightly" is Firefox; "Firefox Focus" is a
# different browser that happens to share the first word.
GECKO_CHANNEL_SUFFIXES = {"nightly", "beta", "aurora", "esr", "developer edition"}


def platform_edition(edition, family, platform):
    """Fold a mobile OS into a display name: "Firefox Nightly" on an Android phone is
    "Firefox for Android Nightly".

    A phone browser's `browserInfo.name` says nothing about the phone. Firefox for
    Android Nightly answers "Firefox Nightly", the same string the desktop Nightly
    answers, and getPlatformInfo() is the only place the difference surfaces. For a
    REMOTE browser, with no process here to probe, that is the only material there is.

    The channel word rides on the end, the way Mozilla writes it: "Firefox for Android",
    "Firefox for Android Beta", "Firefox for Android Nightly". A word that is NOT one of
    Firefox's release trains names a separate product, so that name stays whole and the
    OS goes behind it ("Firefox Focus for Android"); rewriting it to "Firefox for
    Android Focus" would file a different browser as a Firefox channel.
    """
    if not edition or not isinstance(platform, dict):
        return edition
    label = MOBILE_PLATFORM_OS.get((platform.get("os") or "").strip().lower())
    if not label:
        return edition
    if f"for {label}".lower() in edition.lower():
        return edition                  # it already says so; never say it twice
    family = family or edition
    if not edition.lower().startswith(family.lower()):
        return f"{edition} for {label}"
    suffix = edition[len(family):].strip()
    if suffix and suffix.lower() not in GECKO_CHANNEL_SUFFIXES:
        return f"{edition} for {label}"
    return f"{family} for {label} {suffix}".strip()


# A Blink browser's UA brand list names the PRODUCT; the process only names the FAMILY.
# Opera and Opera GX are both `opera.exe` under %LOCALAPPDATA%\Programs\, and nothing the
# probe reads tells them apart. `brands` does: the GX build puts its own name in there,
# beside Chromium's. Lying about that is the gecko forks' habit (see claimed_names), not
# Blink's, so here the claim REFINES the probe instead of competing with it.
def subbrand_edition(family, edition, browser_info):
    """`edition` made specific by the browser's own brand, when that brand names a product
    inside the family the process found: ("Opera", "Opera", {name: "Opera GX"}) -> "Opera GX".

    The claim counts only when it EXTENDS the family, which is what keeps it a refinement
    rather than a second opinion: Vivaldi ships a stock Chrome UA and claims "Google
    Chrome", and that must never rename a process the probe knows is Vivaldi. Whatever the
    install path added on top of the family rides along, so a GX on the beta channel reads
    "Opera GX Beta" instead of losing its channel word.
    """
    _, claimed = claimed_names(browser_info)
    claimed = (claimed or "").strip()
    family = (family or "").strip()
    if not claimed or not family:
        return edition
    if not claimed.lower().startswith(family.lower() + " "):
        return edition
    edition = (edition or "").strip()
    suffix = (edition[len(family):].strip()
              if edition.lower().startswith(family.lower()) else "")
    if not suffix or suffix.lower() in claimed.lower():
        return claimed
    return f"{claimed} {suffix}"


class ExtBrowser:
    """One connected browser (extension socket) in the browser registry.

    A Firefox, or any Firefox FORK that runs the extension: Zen, LibreWolf, … The fork
    claims to be "Firefox" over the WebExtension API and cannot be made to say otherwise
    (see claimed_names), so a LOCAL browser is named from its PROCESS (browser_brand).

    Identity is the extension-minted `instance_id` (stable per profile across browser
    restarts and extension reloads, and the only identity a REMOTE browser can offer).
    The pid/exe/brand fields are display enrichment only, never routing.
    """

    def __init__(self, ws, instance_id: str, slot: int,
                 remote_addr: Optional[str] = None, is_remote: bool = False):
        now = datetime.now()
        self.ws = ws
        self.instance_id = instance_id
        self.slot = slot
        self.version = None
        self.file_access = None
        self.proxy_access = None
        # {documentId: bool, ...}: optional platform APIs the extension PROBED for, not
        # inferred from a version. None means the extension never said (a build predating
        # the probe), which is not the same as "the API is absent"; callers must treat the
        # two differently or a silent None turns into a confident false.
        self.api_support = None
        self.platform = None            # {os, arch} from extension_hello, when it sends it
        self.browser_info = None        # {name, version} (runtime.getBrowserInfo), ditto
        # {windows, tabs, active_tabs, loaded_tabs} pushed by the extension (browser_state):
        # what the BROWSER holds, as opposed to what the extension is. Pushed rather than
        # asked for because get_connections_info() is synchronous and cannot await a
        # round-trip. None until the first push; `state_at` ages it.
        self.browser_state = None
        self.browser_state_at = None
        self.connected_at = now
        self.last_activity = now        # last REAL browser command (not heartbeats)
        self.last_action = None
        self.last_seen = now            # last message of any kind (heartbeats included)
        self.connect_count = 1
        # When THIS socket was accepted, and when its first hello landed (both
        # monotonic), so a hello's latency and a connection's length are measured against
        # the SOCKET rather than against a record that may have outlived several of them.
        # The pair is also what stops the periodic hello re-measuring: a hello is the
        # first of its socket only while hello_seen_at predates accepted_at.
        self.accepted_at = None
        self.hello_seen_at = None
        self.hello_latency_ms = None
        # True when this socket carries only sealed records (ws_crypto). False is an
        # extension from before 1.9, or one that has no token yet.
        self.encrypted = False
        # The PREVIOUS drop, kept across the reconnect that replaced it: the question
        # after a browser comes back is always "why did it go?", and by then the socket
        # that could answer is gone.
        self.last_disconnect_at = None
        self.last_close_code = None
        self.last_close_reason = None
        # The extension's own account of what happened since its last hello
        # (connection_log.clean_journal). Its console is gone the moment the browser is
        # force-killed, so this is the only place its side of a drop is readable.
        self.reported_journal = None
        self.browser_pid = None
        self.exe_path = None
        self.proc_started = None
        self.engine = None              # "gecko" / "blink"; from the process probe (local)
                                        # or the hello's browserInfo.engine (remote)
        self.brand = None               # family: "Firefox" / "Zen Browser" / "Google Chrome"
        self.friendly = None            # edition: "Firefox Developer Edition" / "Google Chrome"
        self.remote_addr = remote_addr
        self.is_remote = is_remote
        # Label of the device token the socket logged in with (ws_crypto), '' when it has
        # none. Proof of sameness when a hello claims a live record (identity_claim.py).
        self.device_label = ""
        # True until an extension_hello names this browser: `instance_id` is then still
        # a server-side placeholder, not the extension's own identity.
        self.provisional = True

    @property
    def connected(self) -> bool:
        """Live socket. Mirrors the old `close_code is None` check exactly (a socket
        that never exposes close_code (a test double) reads as NOT connected, as before)."""
        return self.ws is not None and getattr(self.ws, "close_code", None) is None

    @property
    def family(self) -> str:
        """Brand family: "Firefox", "Zen Browser", "Google Chrome", …

        The PROCESS is the authority (browser_brand), because it is the only thing that
        knows: a fork claims to be Firefox over the extension API. A remote browser has
        no process to probe, so it falls back to that claim, which means an unbranded
        remote gecko fork reads as Firefox. The default is engine-aware: a Blink browser
        (engine == "blink") never defaults to "Firefox".
        """
        if self.brand:
            return self.brand
        fam, _ = claimed_names(self.browser_info)
        if fam:
            return fam
        return "Chromium" if self.engine == "blink" else "Firefox"

    @property
    def edition(self) -> str:
        """Most specific name we have: "Firefox Developer Edition", "Zen Browser", …

        A browser on a phone gets its OS folded in ("Firefox for Android Nightly"): the
        name it claims is the same one the desktop build of that channel claims, and on
        a phone that is not the browser anybody means. See platform_edition.

        A LOCAL browser is named by its process, but the process only knows the family
        where the browser knows its own product: `opera.exe` is Opera and Opera GX both.
        So the claim gets to make the process's answer finer, never to replace it. See
        subbrand_edition.
        """
        if self.friendly:
            return subbrand_edition(self.family, self.friendly, self.browser_info)
        _, claimed = claimed_names(self.browser_info)
        return platform_edition(claimed or self.family, self.family, self.platform)

    @property
    def display_name(self) -> str:
        """The name a person reads on the popup's Connections row.

        Naming a browser is the process probe's job, and a REMOTE one has no process on
        this machine, so what it claimed in its hello is everything there is. The claim
        earns the row once it says something the family does not already
        ("Firefox for Android Nightly", "Zen Browser"); when all it amounts to is the
        family, the row keeps the brand-neutral generic, because "Firefox" alone does not
        pick out a machine on the LAN. Its ADDRESS does, and the row carries that in its
        detail line.
        """
        if self.friendly or self.brand:
            return self.edition
        edition = self.edition
        if edition and edition.strip().lower() != self.family.strip().lower():
            return edition
        if self.engine == "blink":
            return "Chromium extension"
        if self.engine == "gecko":
            return "Firefox extension"
        return "browser extension"

    @property
    def fork_version(self) -> Optional[str]:
        """A fork's OWN version (Zen 1.21.6b), when it publishes one. `version` in
        browser_info is the Firefox it is built on, a different number entirely."""
        _, version = fork_from_browser_info(self.browser_info)
        return version

    @property
    def name(self) -> str:
        """Auto-name for display and selection. There are no user-set labels."""
        if self.is_remote and self.remote_addr:
            return f"remote-{self.remote_addr}"
        if self.browser_pid:
            return f"{self.edition} pid {self.browser_pid}"
        return self.edition

    def describe(self) -> str:
        """Agent-facing one-liner: `[1] Firefox Developer Edition pid 28940`, `[2] Zen Browser pid 38288`.
        The family prefix is dropped when the name already opens with it; a browser
        named after its own brand must not stutter ("Zen Browser [2] Zen Browser …")."""
        name = self.name
        if name.lower().startswith(self.family.lower()):
            return f"[{self.slot}] {name}"
        return f"{self.family} [{self.slot}] {name}"
