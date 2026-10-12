"""tests/run.py: which arguments it rebases onto the caller's cwd before pytest runs from
tests/. A test path given from the repo root must still work; an option VALUE that happens
to name a top-level directory (`-k launcher`, `-m server`) must reach pytest unchanged."""

import importlib.util
import os
from pathlib import Path

import pytest

import test_imports  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def run_py():
    spec = importlib.util.spec_from_file_location("browserplug_tests_run", ROOT / "tests" / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rebased(run_py, *args, cwd=ROOT):
    return run_py._from_caller(list(args), str(cwd))


def test_a_k_expression_naming_a_top_level_directory_is_left_alone(run_py):
    """From the repo root ./launcher exists and tests/launcher does not; `-k launcher` is an
    expression, and rewriting it to an absolute path made it select nothing."""
    assert (ROOT / "launcher").is_dir() and not (ROOT / "tests" / "launcher").exists()
    out = rebased(run_py, "-k", "launcher", "tests/unit/test_client_pid.py")
    assert out[:2] == ["-k", "launcher"]
    assert out[2] == str(ROOT / "tests" / "unit" / "test_client_pid.py")


def test_other_option_values_pass_as_given(run_py):
    args = ["-m", "server", "-p", "scripts", "-o", "addopts=", "--tb", "short", "-W", "docs"]
    assert rebased(run_py, *args) == args


@pytest.mark.parametrize("args", [
    ["--deselect", "tests/unit/test_client_pid.py::test_x"],
    ["--ignore=tests/unit/test_client_pid.py"],
], ids=["separate", "equals"])
def test_path_valued_options_are_rebased_like_test_paths(run_py, args):
    out = " ".join(rebased(run_py, *args))
    assert str(ROOT / "tests" / "unit" / "test_client_pid.py") in out


def test_a_path_that_exists_under_tests_is_not_rebased(run_py):
    assert rebased(run_py, "unit/test_client_pid.py") == ["unit/test_client_pid.py"]
    assert rebased(run_py, "unit/test_client_pid.py::test_x", cwd=ROOT / "tests") == \
        ["unit/test_client_pid.py::test_x"]


def test_a_node_id_from_the_repo_root_is_rebased(run_py):
    out = rebased(run_py, os.path.join("tests", "unit", "test_client_pid.py") + "::test_x")
    assert out == [str(ROOT / "tests" / "unit" / "test_client_pid.py") + "::test_x"]
