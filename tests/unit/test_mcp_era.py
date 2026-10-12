"""shared-py/browserplug_shared/mcp_era.py: the `mcp.era` setting.

BROWSERPLUG_MCP_ERA wins over the `era` key of config.json's `mcp` section, which wins over
the default (`all`, the user's decision). An unknown value is skipped, never fatal, and is
named in `Setting.ignored` so the server can say so. The module is read by launchers on the
bare interpreter, so the last test imports it with site-packages switched off.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import test_imports  # noqa: F401  (path setup)
from browserplug_shared import config_store, mcp_era

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """A state directory of this test's own and no era in the environment: the suite's
    session-wide one may already hold settings, and the developer's shell may set one."""
    monkeypatch.setenv(config_store.STATE_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(mcp_era.ERA_ENV, raising=False)
    return tmp_path


def store(value):
    config_store.write_section(mcp_era.CONFIG_SECTION, {mcp_era.CONFIG_KEY: value})


# --- the order of precedence -----------------------------------------------------------

def test_the_default_is_all():
    assert mcp_era.ERA_DEFAULT == "all"
    assert mcp_era.resolve() == mcp_era.Setting("all", "default", ())
    assert mcp_era.era() == "all"


@pytest.mark.parametrize("value", mcp_era.ERA_VALUES)
def test_config_json_sets_the_era(value):
    store(value)
    assert mcp_era.resolve() == mcp_era.Setting(value, "config", ())


@pytest.mark.parametrize("value", mcp_era.ERA_VALUES)
def test_the_environment_sets_the_era(monkeypatch, value):
    monkeypatch.setenv(mcp_era.ERA_ENV, value)
    assert mcp_era.resolve() == mcp_era.Setting(value, "env", ())


def test_the_environment_outranks_config_json(monkeypatch):
    store("all")
    monkeypatch.setenv(mcp_era.ERA_ENV, "handshake")
    assert mcp_era.resolve().era == "handshake"
    assert mcp_era.resolve().source == "env"


def test_an_explicit_mapping_replaces_the_process_environment(monkeypatch):
    monkeypatch.setenv(mcp_era.ERA_ENV, "handshake")
    assert mcp_era.era({mcp_era.ERA_ENV: "direct"}) == "direct"
    assert mcp_era.era({}) == "all"


@pytest.mark.parametrize("raw", ["  Direct ", "HANDSHAKE", "All\t"])
def test_case_and_surrounding_space_do_not_matter(monkeypatch, raw):
    monkeypatch.setenv(mcp_era.ERA_ENV, raw)
    assert mcp_era.era() == raw.strip().lower()


@pytest.mark.parametrize("raw", ["", "   "])
def test_a_blank_environment_value_is_absent(monkeypatch, raw):
    store("direct")
    monkeypatch.setenv(mcp_era.ERA_ENV, raw)
    assert mcp_era.resolve() == mcp_era.Setting("direct", "config", ())


# --- values that are not one of the three ----------------------------------------------

def test_an_unknown_environment_value_falls_through_to_config_and_is_reported(monkeypatch):
    store("direct")
    monkeypatch.setenv(mcp_era.ERA_ENV, "modern")
    assert mcp_era.resolve() == mcp_era.Setting(
        "direct", "config", (f"{mcp_era.ERA_ENV}=modern",))


def test_an_unknown_value_everywhere_gives_the_default_and_names_both(monkeypatch):
    store("2026")
    monkeypatch.setenv(mcp_era.ERA_ENV, "modern")
    setting = mcp_era.resolve()
    assert (setting.era, setting.source) == ("all", "default")
    assert setting.ignored == (f"{mcp_era.ERA_ENV}=modern", "mcp.era='2026'")


@pytest.mark.parametrize("stored", [True, 3, ["all"], {"a": 1}])
def test_a_config_value_of_the_wrong_type_is_skipped(stored):
    store(stored)
    setting = mcp_era.resolve()
    assert (setting.era, setting.source) == ("all", "default")
    assert setting.ignored == (f"mcp.era={stored!r}",)


@pytest.mark.parametrize("text", ["{ not json", "[1, 2]", '{"mcp": "handshake"}',
                                  '{"mcp": ["era"]}'])
def test_an_unreadable_or_oddly_shaped_config_reads_as_absent(isolated, text):
    (isolated / config_store.CONFIG_FILE_NAME).write_text(text, encoding="utf-8")
    assert mcp_era.resolve() == mcp_era.Setting("all", "default", ())


def test_other_keys_of_the_mcp_section_do_not_disturb_the_lookup():
    config_store.write_section("mcp", {"era": "direct", "other": 1})
    assert mcp_era.era() == "direct"


# --- what each value means --------------------------------------------------------------

@pytest.mark.parametrize("value,server,relay", [
    ("handshake", False, False),
    ("direct", True, False),
    ("all", True, True),
])
def test_where_each_value_carries_2026_07_28(value, server, relay):
    assert mcp_era.serves_modern(value) is server
    assert mcp_era.relay_carries_modern(value) is relay


def test_the_three_values_are_exactly_these():
    assert mcp_era.ERA_VALUES == ("handshake", "direct", "all")


# --- the connection id ------------------------------------------------------------------

@pytest.mark.parametrize("value", ["0" * 32, "abcdef0123456789" * 2])
def test_a_connection_id_is_32_lowercase_hex(value):
    assert mcp_era.is_connection_id(value)


@pytest.mark.parametrize("value", [
    "", "0" * 31, "0" * 33, "A" * 32, "g" * 32, " " + "0" * 31, "0" * 32 + "\n", None, 32, b"0" * 32])
def test_anything_else_is_not_a_connection_id(value):
    assert not mcp_era.is_connection_id(value)


def test_the_header_name_is_the_one_the_relay_will_send():
    assert mcp_era.CONNECTION_ID_HEADER == "X-BrowserPlug-Connection-Id"


# --- the launcher's interpreter ---------------------------------------------------------

def test_the_module_imports_and_resolves_with_site_packages_off(isolated):
    """The launcher runs on whatever interpreter its client started, with none of the
    server's dependencies. `-S` leaves only the standard library, so a third-party import
    anywhere in mcp_era or what it imports fails here."""
    (isolated / config_store.CONFIG_FILE_NAME).write_text(
        json.dumps({"mcp": {"era": "direct"}}), encoding="utf-8")
    code = ("import sys; sys.path[:0] = [sys.argv[1], sys.argv[2]]\n"
            "from browserplug_shared import mcp_era\n"
            "print(mcp_era.resolve().era, mcp_era.resolve().source)\n")
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(ROOT), str(ROOT / "shared-py")],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, **{config_store.STATE_DIR_ENV: str(isolated)}))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["direct", "config"]
