"""scripts/check_release_version.py: the release tag against the four version carriers."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_release_version.py"
_spec = importlib.util.spec_from_file_location("check_release_version", SCRIPT)
crv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crv)


@pytest.mark.parametrize("tag, expected", [
    ("v1.0.14", ("1.0.14", "")),
    ("v1.0.14.2", ("1.0.14.2", "")),
    ("v1.0.14a1", ("1.0.14", "a1")),
    ("v1.0.14b12", ("1.0.14", "b12")),
    ("v1.0.14rc3", ("1.0.14", "rc3")),
    ("v1.0.14.dev4", ("1.0.14", ".dev4")),
])
def test_parse_tag_splits_base_and_suffix(tag, expected):
    assert crv.parse_tag(tag) == expected


@pytest.mark.parametrize("tag", ["1.0.14", "v1.0", "v1.0.14-rc1", "v1.0.14rc", "v1.0.14x1", "release-1"])
def test_parse_tag_rejects_other_shapes(tag):
    with pytest.raises(ValueError):
        crv.parse_tag(tag)


def test_the_repo_carriers_agree():
    version, base, prerelease, problems = crv.check(None)
    assert problems == []
    assert version == base and not prerelease


def test_a_matching_tag_passes_and_a_prerelease_suffix_is_flagged():
    base = crv.check(None)[1]
    assert crv.check(f"v{base}")[3] == []
    version, _, prerelease, problems = crv.check(f"v{base}rc1")
    assert problems == [] and prerelease and version == f"{base}rc1"


def test_a_tag_for_another_version_fails():
    problems = crv.check("v0.0.1")[3]
    assert len(problems) == 1 and "v0.0.1" in problems[0]


def test_disagreeing_carriers_fail(monkeypatch):
    found = crv.carriers()
    found["package.json"] = "9.9.9"
    monkeypatch.setattr(crv, "carriers", lambda: found)
    problems = crv.check(None)[3]
    assert problems and "disagree" in problems[0]


def test_github_output_is_appended(tmp_path):
    out = tmp_path / "out.txt"
    base = crv.check(None)[1]
    assert crv.main(["--tag", f"v{base}a2", "--github-output", str(out)]) == 0
    assert out.read_text(encoding="utf-8").splitlines() == [
        f"version={base}a2", f"base_version={base}", "prerelease=true"]
