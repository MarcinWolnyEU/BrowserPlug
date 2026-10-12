"""build_server_zip.py: the Python side as one reproducible zip."""
import importlib.util
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("build_server_zip", REPO_ROOT / "build_server_zip.py")
bsz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsz)

KEPT = [
    "README.md", "LICENSE", "NOTICE", "THIRD-PARTY-NOTICES",
    "server/server.py", "server/server.py.lock", "server/tor/__init__.py",
    "launcher/launcher.py", "shared-py/browserplug_shared/version.py",
]
DROPPED = [
    "server/__pycache__/server.cpython-312.pyc",
    "server/stale.pyc", "server/stale.pyo",
    "server/browserplug-server.log", "server/browserplug-server.log.1",
    "server/tor/runtime/tor.exe", "server/tor/runtime/data/state",
    "server/config.json", "server/ws-token", "server/.env", "server/.env.local",
    "launcher/__pycache__/launcher.cpython-312.pyc",
]


def _tree(root, with_junk=True):
    names = KEPT + (DROPPED if with_junk else [])
    for rel in names:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel == "shared-py/browserplug_shared/version.py":
            path.write_text('__version__ = "9.8.7"\n', encoding="utf-8")
        elif rel == "launcher/launcher.py":
            path.write_text("#!/usr/bin/env python3\nprint('launcher')\n", encoding="utf-8")
        else:
            path.write_text(f"{rel}\n", encoding="utf-8")
    return root


def _members(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        return zf.namelist()


def test_everything_sits_under_one_top_folder_and_the_required_files_ship(tmp_path):
    tree = _tree(tmp_path / "repo")
    members = bsz.build(str(tmp_path / "out.zip"), root=str(tree))
    assert members == _members(tmp_path / "out.zip")
    assert all(m.startswith("server-package/") for m in members)
    for rel in KEPT:
        assert f"server-package/{rel}" in members


def test_bytecode_logs_the_tor_install_and_local_state_stay_out(tmp_path):
    tree = _tree(tmp_path / "repo")
    members = set(bsz.build(str(tmp_path / "out.zip"), root=str(tree)))
    leaked = [rel for rel in DROPPED if f"server-package/{rel}" in members]
    assert leaked == []


def test_two_builds_are_byte_identical_even_when_mtimes_differ(tmp_path):
    tree = _tree(tmp_path / "repo", with_junk=False)
    bsz.build(str(tmp_path / "a.zip"), root=str(tree))
    for path in tree.rglob("*"):
        if path.is_file():
            path.touch()  # a later build on another day must not change the bytes
    bsz.build(str(tmp_path / "b.zip"), root=str(tree))
    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()


def test_entries_are_sorted_with_a_fixed_timestamp_and_shebang_decides_the_mode(tmp_path):
    tree = _tree(tmp_path / "repo", with_junk=False)
    bsz.build(str(tmp_path / "out.zip"), root=str(tree))
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        names = zf.namelist()
        assert names == sorted(names)
        assert {i.date_time for i in zf.infolist()} == {bsz.FIXED_TIME}
        assert zf.getinfo("server-package/launcher/launcher.py").external_attr >> 16 == 0o100755
        assert zf.getinfo("server-package/server/server.py").external_attr >> 16 == 0o100644


@pytest.mark.parametrize("missing", ["server/server.py", "server/server.py.lock",
                                     "launcher/launcher.py", "LICENSE", "README.md"])
def test_a_missing_source_is_refused_and_leaves_no_archive(tmp_path, missing):
    tree = _tree(tmp_path / "repo", with_junk=False)
    (tree / missing).unlink()
    out = tmp_path / "out.zip"
    with pytest.raises(FileNotFoundError, match=missing.replace(".", r"\.")):
        bsz.build(str(out), root=str(tree))
    assert not out.exists()


def test_a_rebuild_replaces_the_old_archive_instead_of_updating_it(tmp_path):
    tree = _tree(tmp_path / "repo", with_junk=False)
    out = tmp_path / "out.zip"
    bsz.build(str(out), root=str(tree))
    (tree / "server" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    bsz.build(str(out), root=str(tree))
    (tree / "server" / "extra.py").unlink()
    bsz.build(str(out), root=str(tree))
    assert "server-package/server/extra.py" not in _members(out)


def test_the_real_tree_builds_a_clean_package_at_the_current_version(tmp_path):
    out = tmp_path / "browserplug-server.zip"
    members = bsz.build(str(out))
    assert "server-package/server/server.py.lock" in members
    assert "server-package/launcher/launcher.py" in members
    assert "server-package/shared-py/browserplug_shared/version.py" in members
    for legal in ("LICENSE", "NOTICE", "THIRD-PARTY-NOTICES", "README.md"):
        assert f"server-package/{legal}" in members
    junk = [m for m in members
            if "__pycache__" in m or ".log" in m or "/tor/runtime/" in m or m.endswith((".pyc", ".pyo"))]
    assert junk == []
    assert bsz.version_of() != "unknown"
