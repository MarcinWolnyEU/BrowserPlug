"""macOS .app bundle handling in browser identity.

The process that owns a page's socket on a Mac is usually a helper nested inside the
browser's bundle, so these tests build real bundle directories under tmp_path and ask the
identity code about executables inside them. No Mac is needed: the code reads paths and an
Info.plist, never the platform.
"""
import plistlib
from datetime import datetime

import psutil
import pytest

import test_imports  # noqa: F401
from server.browsers import browser_identity as bi
from server.browsers import mac_bundle
from server.reporting import client_identity


@pytest.fixture(autouse=True)
def fresh_caches():
    mac_bundle.clear_caches()
    bi._gecko_dir_cache.clear()
    yield
    mac_bundle.clear_caches()
    bi._gecko_dir_cache.clear()


def make_chromium_bundle(root, name="Google Chrome", version="1"):
    """Chrome's layout. The main executable and the framework's .pak exist on disk; helper
    executables are only paths (nothing reads them), which keeps the tree under Windows'
    path-length limit. Returns (bundle, helpers directory)."""
    app = root / f"{name}.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / name).write_text("")
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleExecutable": name}, fh)
    fw = app / "Contents" / "Frameworks" / f"{name} Framework.framework" / "Versions" / version
    (fw / "Resources").mkdir(parents=True)
    (fw / "Resources" / "chrome_100_percent.pak").write_text("")
    return app, fw / "Helpers"


def make_gecko_bundle(root, name="Firefox"):
    app = root / f"{name}.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "firefox").write_text("")
    (macos / "XUL").write_text("")
    pc = macos / "plugin-container.app" / "Contents" / "MacOS" / "plugin-container"
    pc.parent.mkdir(parents=True)
    pc.write_text("")
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleExecutable": "firefox"}, fh)
    return app, pc


# --- mac_bundle -------------------------------------------------------------------------

def test_outermost_bundle_is_the_browser_not_the_helper_nested_in_it():
    helper = ("/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework."
              "framework/Versions/159/Helpers/Google Chrome Helper.app/Contents/MacOS/"
              "Google Chrome Helper")
    assert mac_bundle.outermost_bundle(helper) == "/Applications/Google Chrome.app"
    assert mac_bundle.bundle_name("/Applications/Google Chrome.app") == "Google Chrome"


@pytest.mark.parametrize("path", [None, "", "/usr/bin/python3", r"C:\Program Files\x\chrome.exe",
                                  "app/chrome", "/opt/.app/chrome"])
def test_a_path_outside_any_bundle_has_no_bundle(path):
    assert mac_bundle.outermost_bundle(path) is None
    assert mac_bundle.is_helper_exe(path) is False


def test_main_executable_is_read_from_info_plist(tmp_path):
    app, _ = make_chromium_bundle(tmp_path, "Brave Browser")
    assert mac_bundle.main_executable_name(str(app)) == "Brave Browser"
    # A plist that names a different file than the bundle does is believed.
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleExecutable": "brave"}, fh)
    mac_bundle.clear_caches()
    assert mac_bundle.main_executable_name(str(app)) == "brave"


def test_main_executable_without_a_plist_falls_back_to_the_only_file(tmp_path):
    app = tmp_path / "Odd Browser.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "oddb").write_text("")
    assert mac_bundle.main_executable_name(str(app)) == "oddb"
    bare = tmp_path / "Bare.app"
    bare.mkdir()
    assert mac_bundle.main_executable_name(str(bare)) == "Bare"


def test_helpers_are_told_from_the_browser_itself(tmp_path):
    app, helpers = make_chromium_bundle(tmp_path)
    main = app / "Contents" / "MacOS" / "Google Chrome"
    renderer = helpers / "Google Chrome Helper (Renderer).app/Contents/MacOS/Google Chrome Helper (Renderer)"
    assert mac_bundle.is_helper_exe(str(main)) is False
    assert mac_bundle.is_helper_exe(str(renderer)) is True
    gapp, plugin = make_gecko_bundle(tmp_path)
    assert mac_bundle.is_helper_exe(str(gapp / "Contents/MacOS/firefox")) is False
    assert mac_bundle.is_helper_exe(str(plugin)) is True


@pytest.mark.parametrize("name,base", [
    ("Google Chrome Helper", "Google Chrome"),
    ("Google Chrome Helper (Renderer)", "Google Chrome"),
    ("Brave Browser Helper (GPU)", "Brave Browser"),
    ("Microsoft Edge Helper (Alerts)", "Microsoft Edge"),
    ("Google Chrome", None), ("firefox", None), ("plugin-container", None), ("", None),
    ("Helper", None),
])
def test_helper_base_name(name, base):
    assert mac_bundle.helper_base_name(name) == base


def test_runtime_engine_tells_chromium_gecko_and_electron_apart(tmp_path):
    chrome, _ = make_chromium_bundle(tmp_path, "Moon Browser")
    assert mac_bundle.runtime_engine(str(chrome)) == "blink"
    ff, _ = make_gecko_bundle(tmp_path, "Moon Fox")
    assert mac_bundle.runtime_engine(str(ff)) == "gecko"
    # An Electron app carries the same .pak, in a framework named for Electron.
    slack = tmp_path / "Slack.app"
    exe = slack / "Contents" / "MacOS" / "Slack"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    pak = slack / "Contents/Frameworks/Electron Framework.framework/Versions/A/Resources"
    pak.mkdir(parents=True)
    (pak / "chrome_100_percent.pak").write_text("")
    assert mac_bundle.runtime_engine(str(slack)) is None
    # …and an unrenamed one is called Electron on both sides of the name.
    bare, _ = make_chromium_bundle(tmp_path, "Electron")
    assert mac_bundle.runtime_engine(str(bare)) is None


# --- browser_brand and friends on bundle paths --------------------------------------------

def test_chrome_and_its_helpers_are_all_google_chrome(tmp_path):
    app, helpers = make_chromium_bundle(tmp_path)
    main = str(app / "Contents/MacOS/Google Chrome")
    assert bi.browser_brand("Google Chrome", main) == ("Google Chrome", "blink")
    for helper in ("Google Chrome Helper", "Google Chrome Helper (Renderer)",
                   "Google Chrome Helper (GPU)"):
        exe = str(helpers / f"{helper}.app/Contents/MacOS/{helper}")
        assert bi.browser_brand(helper, exe) == ("Google Chrome", "blink"), helper
        # …even when only psutil's 16-character p_comm is known.
        assert bi.browser_brand(helper[:16], exe) == ("Google Chrome", "blink"), helper


def test_a_truncated_main_process_name_is_read_from_the_executable(tmp_path):
    app, _ = make_chromium_bundle(tmp_path, "Google Chrome for Testing")
    exe = str(app / "Contents/MacOS/Google Chrome for Testing")
    assert bi.browser_brand("Google Chrome fo", exe) == ("Google Chrome", "blink")
    assert bi.browser_variant("Google Chrome", "blink", exe) == "Google Chrome for Testing"


@pytest.mark.parametrize("bundle,family,variant", [
    ("Google Chrome", "Google Chrome", "Google Chrome"),
    ("Google Chrome Canary", "Google Chrome", "Google Chrome Canary"),
    ("Google Chrome Beta", "Google Chrome", "Google Chrome Beta"),
    ("Google Chrome Dev", "Google Chrome", "Google Chrome Dev"),
    ("Microsoft Edge Beta", "Microsoft Edge", "Microsoft Edge Beta"),
    ("Brave Browser Nightly", "Brave", "Brave Nightly"),
    ("Brave Browser", "Brave", "Brave"),
])
def test_mac_chromium_channels(tmp_path, bundle, family, variant):
    app, _ = make_chromium_bundle(tmp_path, bundle)
    exe = str(app / f"Contents/MacOS/{bundle}")
    assert bi.browser_brand(bundle[:16], exe) == (family, "blink")
    assert bi.browser_variant(family, "blink", exe) == variant


def test_a_folder_called_dev_does_not_make_chrome_a_dev_channel(tmp_path):
    root = tmp_path / "my dev tools"
    root.mkdir()
    app, _ = make_chromium_bundle(root, "Google Chrome")
    exe = str(app / "Contents/MacOS/Google Chrome")
    assert bi.browser_variant("Google Chrome", "blink", exe) == "Google Chrome"


def test_firefox_and_its_plugin_container_are_firefox(tmp_path):
    app, plugin = make_gecko_bundle(tmp_path, "Firefox Developer Edition")
    main = str(app / "Contents/MacOS/firefox")
    assert bi.browser_brand("firefox", main) == ("Firefox", "gecko")
    assert bi.browser_variant("Firefox", "gecko", main) == "Firefox Developer Edition"
    # The child answers for the browser it lives in, not as an unknown binary.
    assert bi.browser_brand("plugin-container", str(plugin)) == ("Firefox", "gecko")
    assert bi.browser_variant("Firefox", "gecko", str(plugin)) == "Firefox Developer Edition"


def test_an_unknown_mac_chromium_fork_is_named_from_its_executable(tmp_path):
    app, helpers = make_chromium_bundle(tmp_path, "Moon Browser")
    helper = "Moon Browser Helper"
    exe = str(helpers / f"{helper}.app/Contents/MacOS/{helper}")
    assert bi.browser_brand(helper, exe) == ("Moon Browser", "blink")


def test_an_unknown_mac_gecko_fork_is_named_from_its_executable(tmp_path):
    app, plugin = make_gecko_bundle(tmp_path, "Moon Fox")
    (app / "Contents/MacOS/firefox").rename(app / "Contents/MacOS/moonfox")
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleExecutable": "moonfox"}, fh)
    assert bi.browser_brand("moonfox", str(app / "Contents/MacOS/moonfox")) == ("Moonfox", "gecko")
    assert bi.browser_brand("plugin-container", str(plugin)) == ("Moonfox", "gecko")


def test_an_electron_app_is_not_a_browser(tmp_path):
    slack = tmp_path / "Slack.app"
    exe = slack / "Contents" / "MacOS" / "Slack"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    pak = slack / "Contents/Frameworks/Electron Framework.framework/Versions/A/Resources"
    pak.mkdir(parents=True)
    (pak / "chrome_100_percent.pak").write_text("")
    assert bi.browser_brand("Slack", str(exe)) == (None, None)
    assert bi.browser_brand("Slack Helper (Renderer)", str(exe)) == (None, None)


def test_a_python_process_is_not_a_browser_on_a_mac(tmp_path):
    py = "/opt/homebrew/Cellar/python@3.15/3.15.2/Frameworks/Python.framework/Versions/3.15/Resources/Python.app/Contents/MacOS/Python"
    assert bi.browser_brand("Python", py) == (None, None)


def test_process_stem_folds_a_chromium_helper_into_its_browser():
    """browser_instance_identity climbs while parent and child stems match; on a Mac the
    network-service helper's parent is `Google Chrome`, so they must match."""
    assert bi.process_stem("Google Chrome Helper") == bi.process_stem("Google Chrome")
    assert bi.process_stem("Google Chrome Helper (Renderer)") == "google chrome"
    assert bi.process_stem("firefox") == "firefox"
    assert bi.process_stem("Microsoft Edge Helper (GPU)") == "microsoft edge"


def test_a_channel_browser_and_its_helpers_share_one_stem():
    """Measured on Canary 159: the main process is `Google Chrome Canary`, its network
    service is `Google Chrome Helper (Aperitif)`, so the product name carries over to the
    helper but the channel does not. The climb from the socket owner to the browser
    compares these stems."""
    main = bi.process_stem("Google Chrome Canary")
    assert main == "google chrome"
    assert bi.process_stem("Google Chrome Helper (Aperitif)") == main
    assert bi.process_stem("Google Chrome Helper (Aperitif Renderer)") == main
    assert bi.process_stem("Microsoft Edge Beta") == bi.process_stem("Microsoft Edge Helper")
    assert bi.process_stem("Brave Browser Nightly") == bi.process_stem("Brave Browser Helper (GPU)")
    # Chrome for Testing names its helpers after itself, so it needs no folding.
    assert bi.process_stem("Google Chrome for Testing") == \
        bi.process_stem("Google Chrome for Testing Helper (Aperitif)")
    # A different product is not folded into Chrome.
    assert bi.process_stem("Google Chrome for Testing") != bi.process_stem("Google Chrome")


def test_a_canary_helper_is_a_canary_browser_by_its_bundle(tmp_path):
    app, helpers = make_chromium_bundle(tmp_path, "Google Chrome Canary")
    net = str(helpers / "Google Chrome Helper (Aperitif).app/Contents/MacOS/"
                        "Google Chrome Helper (Aperitif)")
    assert bi.browser_brand("Google Chrome Helper (Aperitif)", net) == ("Google Chrome", "blink")
    assert bi.browser_variant("Google Chrome", "blink", net) == "Google Chrome Canary"


# --- Chrome's code-signing clone ----------------------------------------------------------

CLONE = ("/private/var/folders/27/1j4l/X/com.google.Chrome.canary.code_sign_clone/"
         "code_sign_clone.CgpbEg/Google Chrome Canary.app.bundle/Contents/MacOS/"
         "Google Chrome Canary")


def test_a_code_sign_clone_is_a_bundle_for_naming_purposes():
    assert mac_bundle.outermost_bundle(CLONE).endswith("/Google Chrome Canary.app.bundle")
    assert mac_bundle.bundle_name(mac_bundle.outermost_bundle(CLONE)) == "Google Chrome Canary"
    assert mac_bundle.is_code_sign_clone(CLONE) is True
    assert mac_bundle.is_code_sign_clone("/Applications/Google Chrome.app/x") is False
    # Measured on Canary 159: the running browser reports the clone as its executable.
    assert bi.browser_brand("Google Chrome Canary", CLONE) == ("Google Chrome", "blink")
    assert bi.browser_variant("Google Chrome", "blink", CLONE) == "Google Chrome Canary"


def test_a_clone_is_never_what_gets_started(monkeypatch, tmp_path):
    """The clone is deleted when the browser exits, so a pin that recorded it launches the
    installed copy of the same name, and nothing when there is none."""
    installed = tmp_path / "Applications" / "Google Chrome Canary.app"
    (installed / "Contents" / "MacOS").mkdir(parents=True)
    (installed / "Contents" / "MacOS" / "Google Chrome Canary").write_text("")
    with open(installed / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleExecutable": "Google Chrome Canary"}, fh)
    monkeypatch.setattr(mac_bundle, "_INSTALL_DIRS", (str(tmp_path / "Nowhere"),
                                                      str(tmp_path / "Applications")))
    want = str(installed).replace("\\", "/")
    assert mac_bundle.installed_bundle(CLONE) == want
    assert mac_bundle.installed_executable(CLONE) == want + "/Contents/MacOS/Google Chrome Canary"
    assert mac_bundle.open_argv(CLONE, ["--profile-directory=Work"]) == \
        ["/usr/bin/open", "-n", "-a", want, "--args", "--profile-directory=Work"]
    monkeypatch.setattr(mac_bundle, "_INSTALL_DIRS", (str(tmp_path / "Nowhere"),))
    assert mac_bundle.installed_bundle(CLONE) is None
    assert mac_bundle.installed_executable(CLONE) is None
    assert mac_bundle.open_argv(CLONE) is None


def test_an_ordinary_path_is_started_as_itself(tmp_path):
    app, _ = make_chromium_bundle(tmp_path)
    exe = str(app / "Contents/MacOS/Google Chrome")
    assert mac_bundle.installed_executable(exe) == exe
    assert mac_bundle.installed_bundle(exe) == str(app).replace("\\", "/")
    assert mac_bundle.installed_executable(None) is None
    assert mac_bundle.installed_executable("/usr/bin/python3") == "/usr/bin/python3"


# --- the socket owner -> browser climb, on process trees measured on a Mac (2026-11-10) ---

class _Proc:
    def __init__(self, table, pid, ppid, name, exe):
        self._t, self.pid, self._ppid, self._name, self._exe = table, pid, ppid, name, exe
        table[pid] = self

    def name(self):
        return self._name

    def exe(self):
        return self._exe

    def parent(self):
        return self._t.get(self._ppid)

    def create_time(self):
        return datetime(2026, 11, 10, 13, 36).timestamp()


def _identify(monkeypatch, table, pid):
    monkeypatch.setattr(psutil, "Process", lambda p: table[p])
    return client_identity.browser_instance_identity(pid)


def _launcher_chain(table):
    """What sits above a browser the server started: a python, a uv, then launchd."""
    _Proc(table, 1, 0, "launchd", "/sbin/launchd")
    _Proc(table, 50, 1, "python3.12", "/opt/homebrew/bin/python3.12")


def test_chrome_network_service_helper_resolves_to_the_main_process(monkeypatch, tmp_path):
    """Chrome for Testing 157: the page's socket belongs to the NetworkService helper
    (`... Helper (Aperitif)`), a child of the main process."""
    app, helpers = make_chromium_bundle(tmp_path, "Google Chrome for Testing")
    table = {}
    _launcher_chain(table)
    # psutil expands its 16-character p_comm from the command line (measured: the probe read
    # the whole name), so the main process arrives under its full name.
    main = _Proc(table, 100, 50, "Google Chrome for Testing",
                 str(app / "Contents/MacOS/Google Chrome for Testing"))
    net = "Google Chrome for Testing Helper (Aperitif)"
    _Proc(table, 110, 100, net, str(helpers / f"{net}.app/Contents/MacOS/{net}"))
    ident = _identify(monkeypatch, table, 110)
    assert (ident["pid"], ident["is_browser"], ident["engine"]) == (main.pid, True, "blink")
    assert ident["brand"] == "Google Chrome"
    assert ident["friendly"] == "Google Chrome for Testing"
    assert ident["exe_path"] == main.exe()


def test_canary_network_service_helper_resolves_to_the_main_process(monkeypatch, tmp_path):
    """Canary 159 names the main process after the channel and its helpers after the
    product, so a name comparison alone stops at the helper (measured)."""
    app, helpers = make_chromium_bundle(tmp_path, "Google Chrome Canary")
    table = {}
    _launcher_chain(table)
    main = _Proc(table, 100, 50, "Google Chrome Canary",
                 str(app / "Contents/MacOS/Google Chrome Canary"))
    net = "Google Chrome Helper (Aperitif)"
    _Proc(table, 110, 100, net, str(helpers / f"{net}.app/Contents/MacOS/{net}"))
    ident = _identify(monkeypatch, table, 110)
    assert (ident["pid"], ident["is_browser"], ident["engine"]) == (main.pid, True, "blink")
    assert ident["friendly"] == "Google Chrome Canary"


def test_firefox_main_process_owning_the_socket_is_the_browser(monkeypatch, tmp_path):
    """Firefox Developer Edition 160, headed and headless: the main `firefox` process owns
    the socket; its plugin-container children never touch it."""
    app, plugin = make_gecko_bundle(tmp_path, "Firefox Developer Edition")
    table = {}
    _launcher_chain(table)
    main = _Proc(table, 100, 50, "firefox", str(app / "Contents/MacOS/firefox"))
    _Proc(table, 110, 100, "plugin-container", str(plugin))
    ident = _identify(monkeypatch, table, 100)
    assert (ident["pid"], ident["is_browser"], ident["engine"]) == (main.pid, True, "gecko")
    assert ident["friendly"] == "Firefox Developer Edition"
    # A child that did own it would still be recognised as a Firefox process.
    assert _identify(monkeypatch, table, 110)["is_browser"] is True


def test_a_firefox_helper_that_owns_the_socket_climbs_to_the_main_process(monkeypatch, tmp_path):
    """`plugin-container` shares no process stem with `firefox`, but lives in the same .app
    bundle, so the climb reaches the main process (and its pid keys the registry)."""
    app, plugin = make_gecko_bundle(tmp_path, "Firefox Developer Edition")
    table = {}
    _launcher_chain(table)
    main = _Proc(table, 100, 50, "firefox", str(app / "Contents/MacOS/firefox"))
    _Proc(table, 110, 100, "plugin-container", str(plugin))
    _Proc(table, 120, 110, "plugin-container", str(plugin))        # a helper of a helper
    for owner in (110, 120):
        ident = _identify(monkeypatch, table, owner)
        assert (ident["pid"], ident["is_browser"], ident["engine"]) == (main.pid, True, "gecko")
        assert ident["exe_path"] == main.exe()


def test_the_climb_does_not_leave_the_bundle_for_the_app_that_started_it(monkeypatch, tmp_path):
    """Another app's process above the browser, even one with a bundle of its own, is not
    the browser."""
    app, plugin = make_gecko_bundle(tmp_path, "Firefox")
    other, _ = make_chromium_bundle(tmp_path, "Some Terminal")
    table = {}
    _Proc(table, 1, 0, "launchd", "/sbin/launchd")
    _Proc(table, 40, 1, "Some Terminal", str(other / "Contents/MacOS/Some Terminal"))
    main = _Proc(table, 100, 40, "firefox", str(app / "Contents/MacOS/firefox"))
    _Proc(table, 110, 100, "plugin-container", str(plugin))
    assert _identify(monkeypatch, table, 110)["pid"] == main.pid


def test_a_parent_whose_executable_cannot_be_read_ends_the_climb(monkeypatch, tmp_path):
    app, plugin = make_gecko_bundle(tmp_path, "Firefox")
    table = {}
    _launcher_chain(table)
    main = _Proc(table, 100, 50, "firefox", str(app / "Contents/MacOS/firefox"))

    def denied():
        raise psutil.AccessDenied(100)

    monkeypatch.setattr(main, "exe", denied)
    child = _Proc(table, 110, 100, "plugin-container", str(plugin))
    assert _identify(monkeypatch, table, 110)["pid"] == child.pid


def test_processes_outside_any_bundle_climb_only_through_one_stem(monkeypatch):
    """Windows and Linux paths: no bundle, so only the stem rule applies, as before."""
    table = {}
    _Proc(table, 1, 0, "explorer.exe", r"C:\Windows\explorer.exe")
    top = _Proc(table, 100, 1, "firefox.exe", r"C:\Program Files\Mozilla Firefox\firefox.exe")
    _Proc(table, 110, 100, "firefox.exe", top.exe())
    _Proc(table, 120, 110, "updater.exe", r"C:\Program Files\Mozilla Firefox\updater.exe")
    assert _identify(monkeypatch, table, 110)["pid"] == top.pid
    assert _identify(monkeypatch, table, 120)["pid"] == 120


def test_a_python_process_owning_the_socket_is_not_a_browser(monkeypatch):
    table = {}
    _launcher_chain(table)
    ident = _identify(monkeypatch, table, 50)
    assert ident["is_browser"] is False


def test_windows_names_are_unchanged():
    assert bi.process_stem("chrome.exe") == "chrome"
    assert bi.process_stem("Firefox.EXE") == "firefox"
    assert bi.process_stem("waterfox-bin") == "waterfox"
    assert bi.browser_brand("chrome.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe") \
        == ("Google Chrome", "blink")
