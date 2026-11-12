"""scripts/ci_local.py: the local runner for .github/workflows/ci.yml.

The first group reads the real workflow and fails when someone adds a step, an action, a
shell or a key the runner does not know how to reproduce, so CI cannot drift from the local
check without a red test. A new commit pin of an action is not one of those: the runner
refuses it at run time (exit 2) and the tests check that mechanism on a fake workflow, so a
Dependabot bump does not turn this suite red. The rest covers the runner's own parts with
fakes: the plan per job, conditions, shells, the working-tree packaging (against throwaway git
repositories), tool downloads (no network), stress, cleanup when the runner is stopped, the
environment a step gets, and the executor (real shell processes on small synthetic
workflows). Nothing here downloads, binds a port or touches the real state directory.
"""
import _thread
import contextlib
import importlib.util
import io
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import textwrap
import threading
import time
import zipfile
from pathlib import Path

import psutil
import pytest
import yaml
from wait_helper import assert_never_within, wait_until

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci_local.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_spec = importlib.util.spec_from_file_location("ci_local", SCRIPT)
cil = importlib.util.module_from_spec(_spec)
sys.modules["ci_local"] = cil          # dataclasses resolve their annotations through sys.modules
_spec.loader.exec_module(cil)

REAL_TEXT = WORKFLOW.read_text(encoding="utf-8")
REAL_RAW = yaml.safe_load(REAL_TEXT)
REAL_JOBS = list(REAL_RAW["jobs"])
HOST_OSES = ("Windows", "macOS", "Linux")
CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
CHECKOUT = f"actions/checkout@{CHECKOUT_SHA}"


class FakeHost(cil.Host):
    """A host that answers `which` from a list and has no files on disk."""


def fake_host(os_name, *, arch="X64", cores=16, have=("jq", "pwsh", "powershell", "git", "bash")):
    return FakeHost(os_name, arch, cores, which=lambda name: f"/fake/{name}" if name in have else None)


@pytest.fixture(autouse=True)
def git_bash_exists(monkeypatch):
    """A fake Windows host has no Git for Windows on disk; say it has. Real hosts stay real."""
    real = cil.find_git_bash
    monkeypatch.setattr(cil, "find_git_bash",
                        lambda host: "bash.exe" if isinstance(host, FakeHost) and host.os == "Windows"
                        else real(host))


def real_plan(job, os_name="Linux", text=REAL_TEXT, **kwargs):
    """A plan of the workflow. Commit pins are not checked here: a Dependabot bump must not turn
    the unit job red, so the pin mechanism is tested on a fake workflow below."""
    kwargs.setdefault("trust_actions", True)
    return cil.build_plan(cil.parse_workflow(text), job, fake_host(os_name), **kwargs)


def synthetic(steps, runs_on="ubuntu-latest", job_extra="", top=""):
    body = textwrap.indent(textwrap.dedent(steps).strip("\n"), "      ")
    extra = textwrap.indent(textwrap.dedent(job_extra).strip("\n"), "    ") + "\n" if job_extra else ""
    return f"name: T\non: [push]\n{top}\njobs:\n  j:\n    runs-on: {runs_on}\n{extra}    steps:\n{body}\n"


def plan_of(text, os_name="Linux", **kwargs):
    return cil.build_plan(cil.parse_workflow(text), "j", fake_host(os_name), **kwargs)


def only_run_commands(raw_job):
    return [s["run"].strip("\n") for s in raw_job["steps"] if "run" in s]


def action_with(job, prefix):
    step = next(s for s in REAL_RAW["jobs"][job]["steps"] if s.get("uses", "").startswith(prefix))
    return step["with"]


def expected_kind(step):
    """The kind of step the workflow's own text says this is."""
    if "uses" in step:
        return {"actions/checkout": "checkout", "astral-sh/setup-uv": "setup-uv",
                "actions/setup-node": "setup-node", "actions/upload-artifact": "upload-artifact",
                "re-actors/alls-green": "aggregator"}[step["uses"].partition("@")[0]]
    if step["run"].startswith("git config --global core.autocrlf"):
        return "git-autocrlf"
    return "timezone" if step["run"].startswith("tzutil") else "run"


def string_env(raw):
    return {k: str(v) for k, v in (raw or {}).items()}


# ---- the real workflow ------------------------------------------------------------------------

@pytest.mark.parametrize("os_name", HOST_OSES)
@pytest.mark.parametrize("job", REAL_JOBS)
def test_every_step_of_every_job_is_understood_on_every_host(job, os_name):
    """No unknown `uses:`, key, shell or machine-wide command anywhere in ci.yml."""
    plan = real_plan(job, os_name)
    assert len(plan.steps) == len(REAL_RAW["jobs"][job]["steps"])
    for step in plan.steps:
        if step.status == "skip":
            continue
        assert hasattr(cil.JobExecutor, "do_" + step.kind.replace("-", "_")), (
            f"{job}: step {step.index} ({step.name!r}) has kind {step.kind!r} and no handler")


@pytest.mark.parametrize("job", REAL_JOBS)
def test_the_plan_runs_the_workflows_own_commands(job):
    plan = real_plan(job)
    assert [s.command for s in plan.steps if s.command] == only_run_commands(REAL_RAW["jobs"][job])


def test_versions_and_environment_come_from_the_workflow_not_from_the_script():
    uv = action_with("unit", "astral-sh/setup-uv")
    node = action_with("unit", "actions/setup-node")["node-version"]
    base = real_plan("unit", "Windows")
    kinds = {s.kind: s for s in base.steps}
    assert kinds["setup-uv"].params == {"version": uv["version"], "python_version": uv["python-version"]}
    assert kinds["setup-node"].params == {"version": node}
    # Change a value in the file and the plan follows it: versions, the Python version, the
    # workflow env, a step env, the timezone, the commands.
    changes = {f'version: "{uv["version"]}"': 'version: "9.8.7"',
               f'node-version: "{node}"': 'node-version: "8.7.6"',
               f'python-version: "{uv["python-version"]}"': 'python-version: "3.9"',
               'PYTHONUTF8: "1"': 'PYTHONUTF8: "2"',
               "TZ: Europe/Warsaw": "TZ: Asia/Tokyo",
               "UV_PYTHON_PREFERENCE: only-managed": "UV_PYTHON_PREFERENCE: system",
               "Central European Standard Time": "UTC",
               "uv run --locked --script tests/run.py -q": "echo changed"}
    changed = REAL_TEXT
    for old, new in changes.items():
        assert old in REAL_TEXT, old
        changed = changed.replace(old, new)
    unit = {s.kind: s for s in real_plan("unit", "Windows", text=changed).steps}
    assert unit["setup-uv"].params == {"version": "9.8.7", "python_version": "3.9"}
    assert unit["setup-node"].params["version"] == "8.7.6"
    assert unit["timezone"].params["zone"] == "UTC"
    assert unit["run"].command == "echo changed" and unit["run"].env == {"UV_PYTHON_PREFERENCE": "system"}
    assert real_plan("unit", "Windows", text=changed).env["PYTHONUTF8"] == "2"
    mac = real_plan("unit-macos", "macOS", text=changed).steps[-1]
    assert mac.env == {"TZ": "Asia/Tokyo", "UV_PYTHON_PREFERENCE": "system"} and mac.command == "echo changed"
    # The script itself holds no copy of what the workflow pins, sets or runs.
    source = SCRIPT.read_text(encoding="utf-8")
    literals = (uv["version"], node, "only-managed", "run.py -q", "--locked", *REAL_RAW["env"],
                f'"{uv["python-version"]}"', f"'{uv['python-version']}'")
    for literal in literals:
        assert literal not in source, f"{literal!r} is spelled in scripts/ci_local.py"


def test_the_unit_job_plan():
    raw = REAL_RAW["jobs"]["unit"]
    plan = real_plan("unit", "Windows")
    assert [s.kind for s in plan.steps] == [expected_kind(s) for s in raw["steps"]]
    autocrlf = next(s["run"].split()[-1] for s in raw["steps"] if expected_kind(s) == "git-autocrlf")
    assert next(s for s in plan.steps if s.kind == "checkout").params["autocrlf"] == autocrlf
    assert plan.env == string_env(REAL_RAW["env"])
    run, last = plan.steps[-1], raw["steps"][-1]
    assert run.command == last["run"].strip("\n") and run.env == string_env(last["env"])
    assert run.shell.startswith("pwsh -command")
    assert plan.runner.label == raw["runs-on"] and plan.runner.os == "Windows"
    assert plan.timeout_minutes == raw["timeout-minutes"]


def test_the_macos_job_plan():
    raw = REAL_RAW["jobs"]["unit-macos"]
    plan = real_plan("unit-macos", "macOS")
    assert [s.kind for s in plan.steps] == [expected_kind(s) for s in raw["steps"]]
    run, last = plan.steps[-1], raw["steps"][-1]
    assert run.env == string_env(last["env"]) and run.command == last["run"].strip("\n")
    assert run.shell == "bash -e {0}"
    assert plan.steps[0].params["autocrlf"] == "false"            # macOS runners have no autocrlf
    assert plan.runner.label == raw["runs-on"] and plan.runner.vcpus == 3 and plan.runner.arch == "ARM64"


@pytest.mark.parametrize("os_name", ["macOS", "Linux"])
def test_the_static_job_plan_runs_every_step_on_a_host_with_the_tools(os_name):
    plan = real_plan("static", os_name)
    assert [s.kind for s in plan.steps][:3] == ["checkout", "setup-uv", "setup-node"]
    assert all(s.status in {"emulate", "run"} for s in plan.steps)
    assert all(s.shell == "bash -e {0}" for s in plan.steps if s.kind == "run")


def test_on_windows_the_workflow_lint_step_is_the_one_step_that_cannot_run():
    plan = real_plan("static", "Windows")
    stuck = [s for s in plan.steps if s.status == "unavailable"]
    assert len(stuck) == 1 and "actionlint" in stuck[0].command
    assert "no Windows wheel" in stuck[0].reason


def test_the_build_job_uploads_unless_the_release_input_is_true():
    assert "upload-artifact" in [s.kind for s in real_plan("build").steps]
    released = real_plan("build", inputs={"release": "true"})
    skipped = [s for s in released.steps if s.status == "skip"]
    assert len(skipped) == 1 and "inputs.release" in skipped[0].reason
    with pytest.raises(cil.RunnerError, match="no such input"):
        real_plan("build", inputs={"nope": "1"})


def test_a_step_needing_a_missing_tool_is_listed_as_not_reproducible_here():
    plan = cil.build_plan(cil.parse_workflow(REAL_TEXT), "build", fake_host("Windows", have=("git",)),
                          trust_actions=True)
    missing = [s for s in plan.steps if s.status == "unavailable"]
    assert missing and all("jq" in s.reason for s in missing)
    text = "\n".join(cil.format_plan(plan))
    assert "NOT REPRODUCIBLE HERE" in text and "jq is not on PATH" in text


def test_the_aggregator_job_is_not_runnable():
    workflow = cil.parse_workflow(REAL_TEXT)
    assert [j.id for j in workflow.jobs.values() if cil.is_aggregator(j)] == ["ci-ok"]
    plan = cil.build_plan(workflow, "ci-ok", fake_host("Linux"))
    assert plan.aggregator and all(s.status == "skip" for s in plan.steps)
    assert "ci-ok" not in {cil.default_job(workflow, fake_host(o)) for o in HOST_OSES}


def test_the_default_job_is_the_first_one_for_this_os():
    workflow = cil.parse_workflow(REAL_TEXT)
    assert cil.default_job(workflow, fake_host("Windows")) == "unit"
    assert cil.default_job(workflow, fake_host("macOS")) == "unit-macos"
    assert cil.default_job(workflow, fake_host("Linux")) == "static"


def test_a_job_for_another_os_carries_a_note_and_a_missing_pwsh_is_named():
    assert any("this host is Windows" in n for n in real_plan("unit-macos", "Windows").notes)
    plan = cil.build_plan(cil.parse_workflow(REAL_TEXT), "unit", fake_host("macOS", have=("git", "bash")),
                          trust_actions=True)
    run = plan.steps[-1]
    assert run.status == "unavailable" and "pwsh is not installed" in run.reason


def test_windows_powershell_stands_in_for_a_missing_pwsh_with_a_note():
    plan = cil.build_plan(cil.parse_workflow(REAL_TEXT), "unit",
                          fake_host("Windows", have=("git", "powershell")), trust_actions=True)
    assert plan.steps[-1].status == "run"
    assert any("Windows PowerShell 5.1" in n for n in plan.notes)


def test_the_timezone_step_is_checked_on_windows_and_becomes_TZ_elsewhere():
    on_windows = {s.kind: s for s in real_plan("unit", "Windows").steps}["timezone"]
    assert on_windows.status == "emulate" and "tz" not in on_windows.params
    elsewhere = {s.kind: s for s in real_plan("unit", "macOS").steps}["timezone"]
    assert elsewhere.params["tz"] == "Europe/Warsaw"
    text = REAL_TEXT.replace("Central European Standard Time", "Nowhere Standard Time")
    unknown = {s.kind: s for s in real_plan("unit", "macOS", text=text).steps}["timezone"]
    assert unknown.status == "unavailable" and "Nowhere Standard Time" in unknown.reason


# ---- action pins ------------------------------------------------------------------------------

MIN_WITH = {"astral-sh/setup-uv": "{version: '1.2.3'}", "actions/setup-node": "{node-version: '1.2.3'}",
            "actions/upload-artifact": "{path: x}"}


def pinned(action, sha):
    with_ = f"\n  with: {MIN_WITH[action]}" if action in MIN_WITH else ""
    return synthetic(f"- uses: {action}@{sha}{with_}")


def test_every_emulated_action_records_the_commits_it_was_checked_against():
    for action, spec in cil.ACTIONS.items():
        if spec.kind == "aggregator":
            continue
        assert spec.checked and spec.recheck, action
        assert all(len(sha) == 40 and set(sha) <= set("0123456789abcdef") for sha in spec.checked), action
        assert action in cil.SETUP_ENV or spec.kind in {"checkout", "upload-artifact"}, action


@pytest.mark.parametrize("action", [a for a, s in cil.ACTIONS.items() if s.kind != "aggregator"])
def test_an_action_pinned_to_an_unchecked_commit_is_refused_by_name(action):
    known = sorted(cil.ACTIONS[action].checked)[0]
    plan_of(pinned(action, known))                      # a checked commit passes
    new = "0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(cil.ActionPinChanged) as raised:
        plan_of(pinned(action, new))
    message = str(raised.value)
    assert action in message and new in message and known in message
    assert cil.ACTIONS[action].recheck in message and "--trust-actions" in message
    with pytest.raises(cil.ActionPinChanged):
        plan_of(pinned(action, "v9"))                   # a tag says nothing about what runs
    assert plan_of(pinned(action, new), trust_actions=True).steps[0].kind == cil.ACTIONS[action].kind


def test_a_changed_pin_stops_the_command_with_exit_code_2_unless_trusted(tmp_path, capsys):
    flipped = REAL_TEXT.replace(CHECKOUT_SHA, "f" * 40)
    changed = tmp_path / "ci.yml"
    changed.write_text(flipped, encoding="utf-8")
    assert cil.main(["--dry-run", "--job", "unit", "--workflow", str(changed)]) == 2
    err = capsys.readouterr().err
    assert "actions/checkout" in err and "--trust-actions" in err
    assert cil.main(["--dry-run", "--job", "unit", "--workflow", str(changed), "--trust-actions"]) == 0


def test_the_real_workflows_pins_do_not_fail_this_suite_when_they_move():
    """A bump changes the SHAs in ci.yml; only the runner's run-time check reacts to it."""
    bumped = REAL_TEXT.replace(CHECKOUT_SHA, "e" * 40)
    assert real_plan("unit", "Windows", text=bumped).steps[1].kind == "checkout"
    with pytest.raises(cil.ActionPinChanged, match="actions/checkout"):
        cil.build_plan(cil.parse_workflow(bumped), "unit", fake_host("Windows"))


# ---- what must stop the run ---------------------------------------------------------------------

def test_an_unknown_action_stops_the_run_naming_the_step():
    text = synthetic("""
        - name: Cache things
          uses: actions/cache@0123456789012345678901234567890123456789
          with: {path: x}
    """)
    with pytest.raises(cil.UnsupportedWorkflow, match=r"step 1 \('Cache things'\).*actions/cache"):
        plan_of(text)


SETUP_UV = f"astral-sh/setup-uv@{sorted(cil.ACTIONS['astral-sh/setup-uv'].checked)[0]}"
SETUP_NODE = f"actions/setup-node@{sorted(cil.ACTIONS['actions/setup-node'].checked)[0]}"
UPLOAD = f"actions/upload-artifact@{sorted(cil.ACTIONS['actions/upload-artifact'].checked)[0]}"


@pytest.mark.parametrize("step, message", [
    (f"- uses: {CHECKOUT}\n  with: {{ref: other}}", "ref"),
    (f"- uses: {SETUP_UV}\n  with: {{version: '0.12.18', enable-cache: true}}", "enable-cache"),
    (f"- uses: {SETUP_UV}\n  with: {{version: '>=0.12'}}", "exact"),
    (f"- uses: {SETUP_NODE}\n  with: {{node-version: '24.x'}}", "exact"),
    (f"- uses: {SETUP_NODE}\n  with: {{node-version: '24.15.0', package-manager-cache: true}}",
     "package-manager-cache"),
    (f"- uses: {SETUP_UV}\n  with: {{version: 0.12}}", "number"),
    (f"- uses: {UPLOAD}\n  with: {{path: '!x'}}", "exclusions"),
    (f"- uses: {UPLOAD}\n  with: {{path: x, name: '${{{{ github.sha }}}}'}}", "github.sha"),
    ("- run: echo hi\n  shell: python {0}", "shell"),
    ("- run: echo hi\n  shell: bash", "shell"),
    ("- run: echo hi\n  working-directory: sub", "working-directory"),
    ("- run: echo hi\n  timeout-minutes: 1", "timeout-minutes"),
    ("- run: echo hi\n  continue-on-error: true", "continue-on-error"),
    ("- run: echo hi\n  id: x", "'id'"),
    ("- run: echo hi\n  surprise: 1", "surprise"),
    ("- run: echo hi\n  if: always()", "always"),
    ("- run: echo hi\n  if: contains(github.ref, 'x')", "contains"),
    ("- run: echo hi\n  if: inputs.nope", "nope"),
    ("- run: echo ${{ steps.a.outputs.b }}", "steps.a.outputs.b"),
    ("- run: echo hi\n  env: {A: '${{ secrets.X }}'}", "secrets.X"),
    ("- run: echo \"x=1\" >> \"$GITHUB_ENV\"", "GITHUB_ENV"),
    ("- run: echo /x >> $GITHUB_PATH", "GITHUB_PATH"),
    ("- run: sudo apt-get install -y jq", "root"),
    ("- run: brew install jq", "system packages"),
    ("- run: git config --global user.name x", "git's global"),
    ("- run: |\n    echo before\n    tzutil /s \"UTC\"", "timezone"),
    ("- run: pip install requests", "Python"),
    ("- run: npm install -g typescript", "global npm"),
    ("- run: setx FOO bar", "environment or registry"),
])
def test_what_the_runner_does_not_model_is_refused_loudly(step, message):
    with pytest.raises(cil.UnsupportedWorkflow, match=message):
        plan_of(synthetic(step))


def test_reading_a_global_git_setting_is_allowed():
    text = synthetic("""
        - run: |
            test -z "$(git config --global user.name)"
            git config --global --get core.autocrlf || true
            git config --global --list
    """)
    assert plan_of(text).steps[0].status == "run"


@pytest.mark.parametrize("job_extra, message", [
    ("strategy: {matrix: {os: [a]}}", "strategy"),
    ("container: node:20", "container"),
    ("services: {db: {image: x}}", "services"),
    ("env: {A: b}", "'env'"),
    ("defaults: {run: {shell: bash}}", "defaults"),
    ("concurrency: x", "concurrency"),
])
def test_a_job_key_the_runner_does_not_model_is_refused(job_extra, message):
    with pytest.raises(cil.UnsupportedWorkflow, match=message):
        cil.parse_workflow(synthetic("- run: echo hi", job_extra=job_extra))


@pytest.mark.parametrize("condition", ["true", "always()", "success()", "${{ always() }}", "${{ true }}"])
def test_a_job_condition_that_is_always_true_is_accepted(condition):
    assert plan_of(synthetic("- run: echo hi", job_extra=f"if: {condition}")).steps[0].status == "run"


@pytest.mark.parametrize("condition", [
    "github.event_name == 'push'", "${{ github.ref == 'refs/heads/main' }}", "failure()", "false",
    "inputs.release", "${{ !cancelled() }}"])
def test_a_job_condition_that_is_not_always_true_is_refused_not_ignored(condition):
    with pytest.raises(cil.UnsupportedWorkflow, match=r"job 'j': if: .*not evaluated"):
        plan_of(synthetic("- run: echo hi", job_extra=f"if: {condition}"))


def test_a_runnable_job_that_needs_another_is_refused_but_the_aggregator_is_listed():
    with pytest.raises(cil.UnsupportedWorkflow, match="needs"):
        plan_of(synthetic("- run: echo hi", job_extra="needs: [other]"))
    assert cil.build_plan(cil.parse_workflow(REAL_TEXT), "ci-ok", fake_host("Linux")).aggregator


def test_an_unknown_runner_label_and_a_top_level_key_are_refused():
    with pytest.raises(cil.UnsupportedWorkflow, match="self-hosted"):
        plan_of(synthetic("- run: echo", runs_on="self-hosted"))
    with pytest.raises(cil.UnsupportedWorkflow, match="macos-latest"):
        plan_of(synthetic("- run: echo", runs_on="macos-latest"))
    with pytest.raises(cil.UnsupportedWorkflow, match="matrix"):
        cil.parse_workflow(synthetic("- run: echo hi", top="matrix: {}"))
    with pytest.raises(cil.UnsupportedWorkflow, match="defaults"):
        cil.parse_workflow(synthetic("- run: echo hi", top="defaults: {run: {shell: bash}}"))


def test_the_exact_machine_state_steps_are_recognised_and_not_run():
    text = synthetic(f"""
        - run: git config --global core.autocrlf input
        - uses: {CHECKOUT}
    """)
    plan = plan_of(text)
    assert [s.kind for s in plan.steps] == ["git-autocrlf", "checkout"]
    assert plan.steps[1].params["autocrlf"] == "input"


def test_a_windows_job_without_the_autocrlf_step_clones_the_way_the_runner_image_does():
    plan = plan_of(synthetic(f"- uses: {CHECKOUT}", runs_on="windows-latest"), "Windows")
    assert plan.steps[0].params["autocrlf"] == "true"


# ---- conditions ---------------------------------------------------------------------------------

INPUTS = {"release": False, "name": "x", "empty": ""}


@pytest.mark.parametrize("expr, expected", [
    ("${{ !inputs.release }}", True),
    ("!inputs.release", True),
    ("inputs.release", False),
    ("${{ inputs.release }}", False),
    ("inputs.name", True),
    ("!inputs.name", False),
    ("inputs.empty", False),
])
def test_step_conditions_read_one_input(expr, expected):
    assert cil.step_condition_holds(expr, INPUTS, "step") is expected


@pytest.mark.parametrize("expr", ["always()", "success() && inputs.release", "inputs.release == false",
                                  "runner.os == 'Linux'", "inputs.missing"])
def test_any_other_step_condition_is_refused(expr):
    with pytest.raises(cil.UnsupportedWorkflow):
        cil.step_condition_holds(expr, INPUTS, "step")


def test_expressions_in_commands_and_values_are_refused_and_named():
    for bad in ("${{ secrets.X }}", "${{ toJSON(needs) }}", "a ${{ runner.os }} b"):
        with pytest.raises(cil.UnsupportedWorkflow, match=r"\$\{\{"):
            cil._no_expressions(bad, "where")
    cil._no_expressions("plain text with $HOME and ${VAR}", "where")


# ---- shells -----------------------------------------------------------------------------------

def test_default_shells_follow_the_runner_os_not_the_host():
    assert cil.resolve_shell("Linux", fake_host("Windows")).display == "bash -e {0}"
    assert cil.resolve_shell("macOS", fake_host("Linux")).name == "bash"
    pwsh = cil.resolve_shell("Windows", fake_host("Linux"))
    assert (pwsh.name, pwsh.suffix) == ("pwsh", ".ps1")
    assert "-NoProfile" in pwsh.args and "Bypass" in pwsh.args       # a local profile or policy must not matter
    legacy = cil.resolve_shell("Windows", fake_host("Windows", have=("powershell",)))
    assert legacy.name == "powershell" and "5.1" in legacy.note
    assert cil.resolve_shell("Windows", fake_host("Windows", have=())).exe is None


def test_git_for_windows_bash_is_found_beside_git_not_through_path(tmp_path, monkeypatch):
    monkeypatch.undo()                       # the real function, not the fixture's stand-in
    (tmp_path / "Git" / "cmd").mkdir(parents=True)
    (tmp_path / "Git" / "bin").mkdir()
    git_exe = tmp_path / "Git" / "cmd" / "git.exe"
    git_exe.write_bytes(b"")
    (tmp_path / "Git" / "bin" / "bash.exe").write_bytes(b"")
    host = cil.Host("Windows", "X64", 4, which=lambda n: str(git_exe) if n == "git"
                    else r"C:\Windows\system32\bash.exe")
    found = cil.find_git_bash(host)
    assert Path(found).resolve() == (tmp_path / "Git" / "bin" / "bash.exe").resolve()


# ---- hardware parity: busy loops ------------------------------------------------------------------

# A process ends after a kill or a signal: on a CI VM a job object, a slow reaper or a
# starved runner can take far longer than the 15 s default, and the wait ends the moment it dies.
PROCESS_DEATH_TIMEOUT = 30.0


def process_gone(pid):
    """True once `pid` is dead: gone, or a zombie nobody has reaped yet."""
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def wait_until_gone(pid, what):
    wait_until(lambda: process_gone(pid), what, timeout=PROCESS_DEATH_TIMEOUT, interval=0.2)


def test_stress_counts():
    runner = cil.RUNNERS["macos-15"]
    assert cil.resolve_stress("auto", fake_host("macOS", cores=8), runner) == 5
    assert cil.resolve_stress("auto", fake_host("macOS", cores=3), runner) == 0
    assert cil.resolve_stress("auto", fake_host("macOS", cores=2), runner) == 0
    assert cil.resolve_stress("7", fake_host("macOS", cores=8), runner) == 7
    assert cil.resolve_stress(0, fake_host("macOS", cores=8), runner) == 0
    assert cil.resolve_stress("off", fake_host("macOS", cores=8), runner) == 0
    for bad in ("many", "-1", "99"):
        with pytest.raises(cil.RunnerError):
            cil.resolve_stress(bad, fake_host("macOS", cores=8), runner)
    assert real_plan("unit-macos", "macOS", stress="auto").stress == 13      # 16 cores, 3 on the runner
    assert real_plan("unit", "Windows", stress="auto").stress == 12          # 16 cores, 4 on the runner


def test_auto_is_the_hosts_logical_cores_minus_the_runners_and_never_more():
    box, mini = fake_host("Windows", cores=16), fake_host("macOS", cores=8)
    assert cil.resolve_stress("auto", box, cil.RUNNERS["windows-latest"]) == 12
    assert cil.resolve_stress("auto", box, cil.RUNNERS["ubuntu-latest"]) == 12
    assert cil.resolve_stress("auto", mini, cil.RUNNERS["macos-15"]) == 5
    for cores in range(1, 40):
        for runner in cil.RUNNERS.values():
            n = cil.resolve_stress("auto", fake_host("Linux", cores=cores), runner)
            assert 0 <= n <= max(0, cores - runner.vcpus)


def test_the_default_stress_sizes_the_loops_to_the_runner_at_normal_priority():
    assert (cil.DEFAULT_STRESS, cil.DEFAULT_PRIORITY) == ("auto", "normal")
    args = cil.build_parser().parse_args([])
    assert (args.stress, args.stress_priority) == ("auto", "normal")
    workflow = cil.parse_workflow(REAL_TEXT)
    mini = cil.build_plan(workflow, "unit-macos", fake_host("macOS", cores=8), trust_actions=True)
    assert (mini.stress, mini.stress_priority) == (5, "normal")          # 8 logical cores less the runner's 3
    box = real_plan("unit", "Windows")
    assert (box.stress, box.stress_priority) == (12, "normal")           # 16 less 4
    header = "\n".join(cil.format_header(box))
    assert "own priority" in header and "compete" in header and "give way" not in header


def test_low_priority_loops_are_an_opt_in_that_the_plan_describes_as_giving_way():
    low = real_plan("unit", "Windows", stress_priority="low")
    assert low.stress == 12 and low.stress_priority == "low"
    header = "\n".join(cil.format_header(low))
    assert "low priority" in header and "give way" in header and "compete" not in header


def test_no_stress_is_one_switch_away_and_the_plan_says_so():
    off = real_plan("unit", "Windows", stress="0")
    assert off.stress == 0
    assert "stress: none" in "\n".join(cil.format_header(off))


class FakeProc:
    def __init__(self):
        self.terminated = 0

    def terminate(self):
        self.terminated += 1

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def test_the_stress_pool_starts_n_processes_and_stops_them_even_when_the_job_raises(tmp_path):
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append((argv, kwargs))
        return FakeProc()

    pool = cil.StressPool(3, tmp_path, 100, spawn=spawn)
    with pytest.raises(RuntimeError):
        with pool:
            assert len(spawned) == 3 and pool.flag.exists()
            procs = list(pool.procs)
            raise RuntimeError("the job blew up")
    assert not pool.flag.exists() and pool.procs == []
    assert all(p.terminated == 1 for p in procs)
    argv = spawned[0][0]
    assert argv[0] == sys.executable
    assert argv[-4:] == [str(pool.flag), "normal", str(os.getpid()), cil.STRESS_MARKER]


@pytest.mark.parametrize("priority", ["low", "normal"])
def test_stress_competes_with_the_job_unless_asked_to_give_way(tmp_path, priority):
    spawned = []
    with cil.StressPool(1, tmp_path, 100, priority=priority,
                        spawn=lambda argv, **kw: spawned.append((argv, kw)) or FakeProc()):
        pass
    argv, kwargs = spawned[0]
    assert argv[-3] == priority
    if os.name == "nt":
        below_normal = 0x00004000
        assert bool(kwargs["creationflags"] & below_normal) == (priority == "low")
    assert "os.nice(19)" in argv[argv.index("-c") + 1]          # POSIX spinners lower themselves
    with pytest.raises(cil.RunnerError, match="low or normal"):
        cil.StressPool(1, tmp_path, 100, priority="high")


def test_a_pool_without_a_priority_argument_competes(tmp_path):
    assert cil.StressPool(1, tmp_path, 100).priority == "normal"


def test_a_zero_stress_pool_starts_nothing(tmp_path):
    spawned = []
    with cil.StressPool(0, tmp_path, 10, spawn=lambda *a, **k: spawned.append(1)):
        pass
    assert spawned == [] and not (tmp_path / "stress.alive").exists()


def test_a_real_busy_loop_exits_when_its_flag_file_goes(tmp_path):
    pool = cil.StressPool(1, tmp_path, 120)
    pool.start()
    try:
        proc = pool.procs[0]
        assert proc.poll() is None
        pool.flag.unlink()
        assert proc.wait(timeout=60) == 0
    finally:
        pool.stop()


HELPER_PARENT = (
    "import os, subprocess, sys, time\n"
    "spin = subprocess.Popen([sys.executable, '-I', '-S', '-c', sys.argv[1], sys.argv[2], sys.argv[3],\n"
    "                         'normal', str(os.getpid()), sys.argv[4]])\n"
    "print(spin.pid, flush=True)\n"
    "time.sleep(300)\n")


def test_a_real_busy_loop_exits_when_the_runner_that_started_it_is_gone(tmp_path):
    flag = tmp_path / "alive"
    flag.write_text("running\n")
    parent = subprocess.Popen([sys.executable, "-c", HELPER_PARENT, cil._SPIN, str(time.time() + 300),
                               str(flag), cil.STRESS_MARKER], stdout=subprocess.PIPE, text=True)
    spinner = None
    try:
        spinner = int(parent.stdout.readline())
        time.sleep(2.5)                                    # several parent checks pass while it lives
        assert psutil.pid_exists(spinner)
        assert_never_within(lambda: process_gone(spinner), "the busy loop ended while its parent lived",
                            within=0.5, interval=0.2)
        parent.kill()
        parent.wait()
        wait_until_gone(spinner, "the busy loop ended after its parent died")
    finally:
        parent.kill()
        parent.stdout.close()
        if spinner and psutil.pid_exists(spinner):
            psutil.Process(spinner).kill()


def test_busy_loops_carry_a_marker_in_their_argv_that_pgrep_finds(tmp_path):
    assert "ci_local.py" in cil.STRESS_MARKER          # the pattern mac_ci.sh's busy check already uses
    with cil.StressPool(1, tmp_path, 120) as pool:
        command = psutil.Process(pool.procs[0].pid).cmdline()
        assert cil.STRESS_MARKER in command and str(pool.flag) in command


# ---- cleanup when the runner is stopped ----------------------------------------------------------

def test_a_termination_signal_unwinds_like_ctrl_c_and_the_old_handlers_come_back():
    numbers = [getattr(signal, n) for n in ("SIGTERM", "SIGHUP", "SIGBREAK") if hasattr(signal, n)]
    before = {n: signal.getsignal(n) for n in numbers}
    with cil.terminate_on_signals():
        for number in numbers:
            handler = signal.getsignal(number)
            assert handler is not before[number]
            with pytest.raises(cil.Terminated) as raised:
                handler(number, None)
            assert raised.value.signum == number and isinstance(raised.value, KeyboardInterrupt)
    assert {n: signal.getsignal(n) for n in numbers} == before


def test_a_termination_signal_during_a_step_kills_its_tree_and_stops_the_busy_loops(tmp_path):
    """The glue: signal, then Terminated, then the step's tree and the pool's loops are gone.

    `interrupt_main(SIGTERM)` runs the handler the context manager installed, in this thread, the
    way the signal would; it works the same on Windows, where SIGTERM cannot be sent.
    """
    marker = tmp_path / "grandchild.pid"
    code = GRANDCHILD.format(marker=str(marker), tail="time.sleep(120)")
    pool = cil.StressPool(2, tmp_path / "stress", 120)
    fire = threading.Timer(3.0, lambda: _thread.interrupt_main(signal.SIGTERM))
    loops = []
    try:
        with cil.terminate_on_signals():
            fire.start()
            with pytest.raises(cil.Terminated) as raised:
                with pool:
                    loops = [proc.pid for proc in pool.procs]
                    cil.run_process([sys.executable, "-c", code], env=dict(os.environ), cwd=tmp_path,
                                    log=io.StringIO(), timeout=60, label="signal", heartbeat=3600,
                                    scope=tmp_path)
    finally:
        fire.cancel()
    assert raised.value.signum == signal.SIGTERM and len(loops) == 2
    wait_until_gone(int(marker.read_text()), "the step's grandchild ended after the signal")
    for pid in loops:
        wait_until_gone(pid, f"busy loop {pid} ended after the signal")
    assert pool.procs == []
    assert cil._cleanups == []


def test_main_exits_with_128_plus_the_signal_number_or_130_for_ctrl_c(monkeypatch, capsys):
    def interrupted(number):
        def run(args):
            raise (cil.Terminated(number) if number != signal.SIGINT else KeyboardInterrupt())
        return run

    for number in (signal.SIGTERM, signal.SIGINT):
        monkeypatch.setattr(cil, "_main", interrupted(number))
        assert cil.main(["--dry-run"]) == 128 + int(number)
    assert "interrupted" in capsys.readouterr().err


def test_the_cleanup_hook_stops_busy_loops_and_empties_the_registry(tmp_path):
    pool = cil.StressPool(2, tmp_path, 100, spawn=lambda argv, **kw: FakeProc())
    pool.start()
    procs = list(pool.procs)
    assert pool.stop in cil._cleanups
    cil.run_cleanups()
    assert all(p.terminated == 1 for p in procs) and pool.procs == [] and not pool.flag.exists()
    assert cil._cleanups == []
    pool.stop()                                            # stopping twice is harmless
    assert cil._cleanups == []


def test_parse_ps_reads_pid_and_command_lines():
    text = ("  101 /usr/bin/python3 /Users/x/run/r1/workspace/server/server.py --port 1\n"
            "    7 /sbin/launchd\n"
            "bad line\n"
            " 9999 sleep 1\n")
    assert cil.parse_ps(text) == [(101, "/usr/bin/python3 /Users/x/run/r1/workspace/server/server.py --port 1"),
                                  (7, "/sbin/launchd"), (9999, "sleep 1")]


def test_leftovers_are_found_by_the_run_directory_in_their_command_line_and_the_runner_is_spared():
    table = [(11, "python /cache/runs/a-unit/r1/workspace/server/server.py"),
             (12, "python /cache/runs/a-unit/r10/workspace/server/server.py"),
             (13, "python /somewhere/else.py"),
             (os.getpid(), "python /cache/runs/a-unit/r1/ runner"),
             (os.getppid(), "bash /cache/runs/a-unit/r1/ parent")]
    killed = []
    found = cil.kill_leftovers("/cache/runs/a-unit/r1/", table=lambda: table,
                               kill=lambda pid, sig: killed.append(pid))
    assert found == killed == [11]                          # r10 is another repeat, not r1


posix_only = pytest.mark.skipif(os.name == "nt", reason="Windows ends a step's tree with a job object")

SETSID_CHILD = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', sys.argv[1]],\n"
    "                         start_new_session=True)\n"
    "open(sys.argv[2], 'w').write(str(child.pid))\n")


@posix_only
def test_a_descendant_that_left_the_process_group_is_found_by_the_run_directory(tmp_path):
    """The server spawner calls setsid, so killpg misses what it starts."""
    scope = tmp_path / "run"
    marker = tmp_path / "setsid.pid"
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        res = cil.run_process([sys.executable, "-c", SETSID_CHILD, str(scope / "workspace" / "server.py"),
                               str(marker)], env=dict(os.environ), cwd=tmp_path, log=io.StringIO(),
                              timeout=60, label="setsid", heartbeat=3600, scope=scope)
        assert res.code == 0
        wait_until_gone(int(marker.read_text()), "the setsid child ended with the step")
        assert bystander.poll() is None                     # a process outside the run directory is left alone
    finally:
        bystander.kill()
        bystander.wait()


@posix_only
def test_the_real_process_table_lists_this_process_with_its_command_line():
    table = dict(cil.process_table())
    command = table[os.getpid()].lower()
    assert "python" in command or "pytest" in command


# ---- tool downloads and checksums -----------------------------------------------------------------

SUM = "a" * 64


def test_checksum_files_parse_in_both_published_shapes():
    assert cil.parse_checksum(f"{SUM}  uv-x.zip\n", "uv-x.zip") == SUM
    assert cil.parse_checksum(f"{SUM} *uv-x.zip\n", "uv-x.zip") == SUM
    assert cil.parse_checksum(f"{'b' * 64}  other.zip\n{SUM}  node-v1.zip\n", "node-v1.zip") == SUM
    assert cil.parse_checksum(f"{SUM}\n", "whatever") == SUM
    with pytest.raises(cil.RunnerError):
        cil.parse_checksum(f"{SUM}  other.zip\n", "node-v1.zip")


@pytest.mark.parametrize("os_name, arch, uv, node", [
    ("Windows", "X64", "uv-x86_64-pc-windows-msvc.zip", "node-v1.2.3-win-x64.zip"),
    ("macOS", "ARM64", "uv-aarch64-apple-darwin.tar.gz", "node-v1.2.3-darwin-arm64.tar.gz"),
    ("Linux", "X64", "uv-x86_64-unknown-linux-gnu.tar.gz", "node-v1.2.3-linux-x64.tar.gz"),
])
def test_download_assets(os_name, arch, uv, node):
    host = fake_host(os_name, arch=arch)
    assert cil.uv_asset(host, "1.2.3")[0] == uv
    assert cil.uv_asset(host, "1.2.3")[1].endswith(f"/releases/download/1.2.3/{uv}")
    assert cil.node_asset(host, "1.2.3")[0] == node
    assert cil.node_asset(host, "1.2.3")[1] == f"https://nodejs.org/dist/v1.2.3/{node}"


def zip_with(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_archives_that_escape_their_directory_are_refused(tmp_path):
    bad = zip_with(tmp_path / "bad.zip", {"../evil.txt": "x"})
    with pytest.raises(cil.RunnerError, match="leaves"):
        cil.safe_extract(bad, tmp_path / "out")
    for member in ("../evil.txt", "/abs.txt"):
        tar_path = tmp_path / "bad.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            info = tarfile.TarInfo(member)
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
        with pytest.raises(cil.RunnerError, match="leaves"):
            cil.safe_extract(tar_path, tmp_path / "out2")
    with tarfile.open(tmp_path / "link.tar.gz", "w:gz") as tf:
        info = tarfile.TarInfo("ok/link")
        info.type, info.linkname = tarfile.SYMTYPE, "../../outside"
        tf.addfile(info)
    with pytest.raises(cil.RunnerError, match="link"):
        cil.safe_extract(tmp_path / "link.tar.gz", tmp_path / "out3")
    assert not (tmp_path / "evil.txt").exists()


def test_the_links_and_members_a_node_tarball_holds_are_accepted(tmp_path):
    """npm and npx are relative symlinks inside the tree; the check must let them through."""
    root = tmp_path.resolve()

    def member(name, kind=tarfile.REGTYPE, link=""):
        info = tarfile.TarInfo(name)
        info.type, info.linkname = kind, link
        return info

    cil.check_tar_member(root, member("node-v1/bin/node"), "a.tar.gz")
    cil.check_tar_member(root, member("node-v1/bin/npm", tarfile.SYMTYPE,
                                      "../lib/node_modules/npm/bin/npm-cli.js"), "a.tar.gz")
    cil.check_tar_member(root, member("node-v1/bin/copy", tarfile.LNKTYPE, "node-v1/bin/node"), "a.tar.gz")
    for bad in (member("../x"), member("a/l", tarfile.SYMTYPE, "../../x"),
                member("a/l", tarfile.SYMTYPE, "/etc/passwd"), member("a/l", tarfile.LNKTYPE, "../x")):
        with pytest.raises(cil.RunnerError, match="leaves"):
            cil.check_tar_member(root, bad, "a.tar.gz")


def serve(monkeypatch, tmp_path, name, members, *, published=None):
    """Replace the network: `download` copies a prepared archive, `fetch_text` publishes its sum."""
    (tmp_path / "served").mkdir(exist_ok=True)
    blob = zip_with(tmp_path / "served" / name, members)
    real_sum = cil.sha256_file(blob)
    calls = []
    monkeypatch.setattr(cil, "download", lambda url, dest: (calls.append(url), shutil.copyfile(blob, dest)))
    monkeypatch.setattr(cil, "fetch_text", lambda url: f"{published or real_sum}  {name}\n")
    return calls


def test_uv_is_downloaded_once_checked_against_the_published_sha256_and_cached(tmp_path, monkeypatch):
    host = fake_host("Windows")
    name = cil.uv_asset(host, "1.2.3")[0]
    calls = serve(monkeypatch, tmp_path, name, {"uv.exe": "uv-binary", "uvx.exe": "x"})
    tools = cil.ToolCache(tmp_path / "cache", host, say=lambda line: None)
    uv_dir = tools.uv_dir("1.2.3")
    assert (uv_dir / "uv.exe").read_text() == "uv-binary" and len(calls) == 1
    assert tools.uv_dir("1.2.3") == uv_dir and len(calls) == 1               # the second call hits the cache
    assert not (tmp_path / "cache" / "downloads" / "uv-1.2.3").exists()       # scratch is cleaned up


def test_a_download_that_does_not_match_the_published_sha256_installs_nothing(tmp_path, monkeypatch):
    host = fake_host("Windows")
    name = cil.uv_asset(host, "1.2.3")[0]
    serve(monkeypatch, tmp_path, name, {"uv.exe": "tampered"}, published="0" * 64)
    with pytest.raises(cil.RunnerError, match="sha256"):
        cil.ToolCache(tmp_path / "cache", host, say=lambda line: None).uv_dir("1.2.3")
    assert not (tmp_path / "cache" / "tools").exists()


def test_node_is_unpacked_to_its_bin_directory_after_the_checksum_matches(tmp_path, monkeypatch):
    host = fake_host("Windows")
    name = cil.node_asset(host, "1.2.3")[0]
    serve(monkeypatch, tmp_path, name, {"node-v1.2.3-win-x64/node.exe": "n",
                                        "node-v1.2.3-win-x64/npm.cmd": "c"})
    node_dir = cil.ToolCache(tmp_path / "cache", host, say=lambda line: None).node_dir("1.2.3")
    assert (node_dir / "node.exe").read_text() == "n" and (node_dir / "npm.cmd").exists()


# ---- the working tree, packaged without touching the user's repository ---------------------------

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.org", "-c", "commit.gpgsign=false"]


def run_git(repo, *args):
    proc = subprocess.run(["git", *GIT_ID, *args], cwd=repo, capture_output=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "core.autocrlf", "true")
    write(root / ".gitignore", "ignored.txt\nbuild/\n")
    for name, text in {"a.txt": "a1\n", "b.txt": "b1\n", "sub/c.txt": "c1\n", "e.txt": "e1\n"}.items():
        write(root / name, text)
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "base")
    return root


def snapshot_of(repo):
    """Everything a packaging run must leave alone, read without refreshing the index."""
    git_dir = Path(run_git(repo, "rev-parse", "--absolute-git-dir").strip())
    state = {
        "refs": run_git(repo, "for-each-ref"),
        "status": run_git(repo, "--no-optional-locks", "status", "--porcelain=v1"),
        "stash": run_git(repo, "stash", "list"),
        "objects": run_git(repo, "count-objects", "-v"),
        "files": {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(repo).parts},
    }
    state["index"] = (git_dir / "index").read_bytes()
    return state


def clone_of(source, dest):
    subprocess.run(["git", "clone", "-c", "core.autocrlf=false", "--no-tags", "--branch", "main", "-q",
                    "--depth", "1", source.stage_url, str(dest)], check=True, capture_output=True)
    return dest


def test_the_working_tree_reaches_a_fresh_clone_and_the_repository_is_left_as_it_was(repo, tmp_path):
    write(repo / "a.txt", "a2\n")                                                      # modified
    (repo / "b.txt").unlink()                                                          # deleted
    write(repo / "new.txt", "new\n")                                                   # untracked
    write(repo / "sub" / "deep" / "d.txt", "d\n")                                      # untracked, nested
    write(repo / "ignored.txt", "secret\n")                                            # ignored
    write(repo / "build" / "out.bin", "x")                                             # ignored directory
    write(repo / "sub" / "c.txt", "c2\n")
    run_git(repo, "add", "sub/c.txt")                                                  # staged ...
    write(repo / "sub" / "c.txt", "c3\n")                                              # ... then edited again
    (repo / "e.txt").write_bytes(b"e2\r\n")                                            # CRLF in the working file
    before = snapshot_of(repo)

    source = cil.prepare_source(repo, tmp_path / "stage")

    assert snapshot_of(repo) == before           # index, refs, stash, loose objects and files all unchanged
    assert sorted(source.changes) == sorted([("M", "a.txt"), ("D", "b.txt"), ("A", "new.txt"),
                                             ("A", "sub/deep/d.txt"), ("M", "sub/c.txt"),
                                             ("M", "e.txt")])
    assert "3 modified, 2 added or untracked, 1 deleted" in source.description
    clone = clone_of(source, tmp_path / "ws")

    def read(name):
        return (clone / name).read_bytes()

    assert read("a.txt") == b"a2\n" and read("new.txt") == b"new\n" and read("sub/deep/d.txt") == b"d\n"
    assert read("sub/c.txt") == b"c3\n"                       # the working tree wins over the index
    assert read("e.txt") == b"e2\n"                            # CRLF in the working file is LF in the clone
    assert not (clone / "b.txt").exists() and not (clone / "ignored.txt").exists()
    assert not (clone / "build").exists()
    assert run_git(clone, "log", "--format=%H").split() == [source.commit]       # shallow: one commit
    assert run_git(clone, "config", "core.autocrlf").strip() == "false"
    assert run_git(clone, "rev-parse", "HEAD").strip() == source.commit


def test_intent_to_add_entries_untracked_files_and_odd_names_are_all_packaged(repo, tmp_path):
    """The main checkout has `git add -N` entries beside plain untracked files."""
    write(repo / "ita.txt", "intent\n")
    run_git(repo, "add", "-N", "ita.txt")                          # in the index as an empty placeholder
    write(repo / "ita2" / "nested.txt", "nested\n")
    run_git(repo, "add", "-N", "ita2/nested.txt")
    write(repo / "a.txt", "a2\n")
    write(repo / "plain untracked.txt", "plain\n")
    write(repo / "umläut" / "ü.txt", "umlaut\n")
    (repo / "b.txt").unlink()
    before = snapshot_of(repo)
    assert "ita.txt" in before["status"]                           # the placeholder shows as an addition

    source = cil.prepare_source(repo, tmp_path / "stage")

    assert snapshot_of(repo) == before                             # the placeholders stay placeholders
    assert sorted(source.changes) == sorted([
        ("M", "a.txt"), ("D", "b.txt"), ("A", "ita.txt"), ("A", "ita2/nested.txt"),
        ("A", "plain untracked.txt"), ("A", "umläut/ü.txt")])
    assert "1 modified, 4 added or untracked, 1 deleted" in source.description
    clone = clone_of(source, tmp_path / "ws")
    assert (clone / "ita.txt").read_bytes() == b"intent\n"          # real content, not the empty blob
    assert (clone / "ita2" / "nested.txt").read_bytes() == b"nested\n"
    assert (clone / "plain untracked.txt").read_bytes() == b"plain\n"
    assert (clone / "umläut" / "ü.txt").read_bytes() == b"umlaut\n"
    assert not (clone / "b.txt").exists()
    assert "4 changed or deleted, 2 untracked" in cil.worktree_summary(repo)    # read-only, counts both kinds
    assert snapshot_of(repo) == before


def test_the_temporary_commit_is_not_written_into_the_users_object_database(repo, tmp_path):
    write(repo / "new.txt", "new\n")
    source = cil.prepare_source(repo, tmp_path / "stage")
    probe = subprocess.run(["git", "cat-file", "-e", source.commit], cwd=repo, capture_output=True)
    assert probe.returncode != 0


def test_a_clean_tree_is_tested_as_head_itself(repo, tmp_path):
    head = run_git(repo, "rev-parse", "HEAD").strip()
    source = cil.prepare_source(repo, tmp_path / "stage")
    assert source.commit == head and source.changes == [] and "no uncommitted changes" in source.description


def test_ref_tests_a_commit_and_ignores_the_working_tree(repo, tmp_path):
    first = run_git(repo, "rev-parse", "HEAD").strip()
    write(repo / "a.txt", "a2\n")
    run_git(repo, "commit", "-q", "-am", "second")
    write(repo / "a.txt", "dirty\n")
    source = cil.prepare_source(repo, tmp_path / "stage", ref=first)
    assert source.commit == first
    assert (clone_of(source, tmp_path / "ws") / "a.txt").read_bytes() == b"a1\n"
    with pytest.raises(cil.RunnerError):
        cil.prepare_source(repo, tmp_path / "stage2", ref="no-such-ref")


def test_a_linked_worktree_is_packaged_like_any_checkout(repo, tmp_path):
    linked = tmp_path / "linked"
    run_git(repo, "worktree", "add", "-q", "--detach", str(linked))
    write(linked / "wt.txt", "wt\n")
    source = cil.prepare_source(linked, tmp_path / "stage")
    assert (clone_of(source, tmp_path / "ws") / "wt.txt").read_bytes() == b"wt\n"
    assert source.changes == [("A", "wt.txt")]


def test_git_variables_from_a_hook_do_not_redirect_the_packaging(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    run_git(other, "init", "-q")
    write(repo / "x.txt", "x\n")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "bogus-index"))
    assert cil.prepare_source(repo, tmp_path / "stage").changes == [("A", "x.txt")]


def test_worktree_summary_is_read_only_and_counts_what_changed(repo):
    assert "no uncommitted changes" in cil.worktree_summary(repo)
    write(repo / "a.txt", "a2\n")
    write(repo / "u.txt", "u\n")
    before = snapshot_of(repo)
    assert "1 changed or deleted, 1 untracked" in cil.worktree_summary(repo)
    assert snapshot_of(repo) == before



# ---- untracked files in a working-tree run -----------------------------------------------------------

def test_untracked_files_are_listed_read_only_and_tracked_or_ignored_ones_are_not(repo):
    write(repo / "new.txt", "n\n")
    write(repo / "sub" / "deep" / "d.txt", "d\n")
    write(repo / "ignored.txt", "x\n")
    write(repo / "a.txt", "changed\n")                       # tracked, so not listed
    before = snapshot_of(repo)
    assert cil.untracked_files(repo) == ["new.txt", "sub/deep/d.txt"]
    assert snapshot_of(repo) == before


def test_the_source_remembers_the_untracked_files_it_includes_and_a_commit_has_none(repo, tmp_path):
    write(repo / "helper.py", "x = 1\n")
    assert cil.prepare_source(repo, tmp_path / "stage").untracked == ["helper.py"]
    assert cil.prepare_source(repo, tmp_path / "stage2", ref="HEAD").untracked == []


def test_the_untracked_notice_is_loud_names_the_files_and_points_at_ref_head():
    lines = cil.untracked_notice(["a.py", "b/c.py"])
    assert all(line.startswith("!!") for line in lines) and "2 UNTRACKED" in lines[0]
    assert "a.py" in "\n".join(lines) and "b/c.py" in "\n".join(lines) and "--ref HEAD" in lines[-1]
    assert "and 25 more" in "\n".join(cil.untracked_notice([f"f{i}.py" for i in range(40)]))
    assert cil.untracked_notice([]) == []


def test_a_dry_run_lists_untracked_files_unless_it_tests_a_commit(repo, capsys):
    write(repo / "helper.py", "x\n")
    base = ["--dry-run", "--job", "build", "--repo", str(repo), "--trust-actions"]
    assert cil.main(base) == 0
    out = capsys.readouterr().out
    assert "UNTRACKED" in out and "helper.py" in out and "--ref HEAD" in out
    assert cil.main([*base, "--ref", "HEAD"]) == 0
    assert "UNTRACKED" not in capsys.readouterr().out


def test_a_real_run_prints_the_untracked_notice_before_it_starts_anything(repo, tmp_path, capsys, monkeypatch):
    write(repo / "helper.py", "x\n")
    printed = []
    monkeypatch.setattr(cil, "_run_job", lambda *a, **k: printed.append(capsys.readouterr().out) or 0)
    code = cil.main(["--job", "build", "--repo", str(repo), "--cache-dir", str(tmp_path / "cache"),
                     "--trust-actions"])
    assert code == 0 and "UNTRACKED" in printed[0] and "helper.py" in printed[0]


# ---- the environment a job sees ------------------------------------------------------------------

JUNK = {"PYTHONPATH": "z", "PYTHONUTF8": "0", "UV_CACHE_DIR": "u", "UV_PYTHON": "3.13", "UV_TOOL_BIN_DIR": "t",
        "VIRTUAL_ENV": "v", "TZ": "UTC", "PYTEST_ADDOPTS": "-x", "npm_config_registry": "r",
        "GITHUB_TOKEN": "g", "FIREFOX_PATH": "f", "CHROME_PATH": "c", "NODE_OPTIONS": "o", "GIT_DIR": "d",
        "BROWSERPLUG_STATE_DIR": "x", "HTTPS_PROXY": "p", "LANG": "C", "CI": "false", "CLAUDE_CODE": "1",
        "HOMEBREW_PREFIX": "h"}


def test_only_allowlisted_variables_are_copied_from_the_callers_environment():
    posix_names = cil.HOST_ENV_NAMES["other"]
    for needed in ("PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "SHELL"):
        assert needed in posix_names
    posix = {**{name: f"v-{name}" for name in posix_names}, **JUNK}
    assert cil.host_passthrough(posix, "macOS") == {name: f"v-{name}" for name in posix_names}
    for needed in ("PATH", "SystemRoot", "ComSpec", "PATHEXT", "TEMP", "TMP", "USERNAME"):
        assert needed.upper() in {n.upper() for n in cil.HOST_ENV_NAMES["Windows"]}
    windows = {"Path": "p", "systemroot": "r", "ComSpec": "c", "PATHEXT": ".exe", "temp": "t",
               "ProgramFiles(x86)": "pf", **JUNK}
    got = cil.host_passthrough(windows, "Windows")
    assert {k.upper() for k in got} == {"PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "PROGRAMFILES(X86)"}


def test_the_test_steps_environment_is_exactly_the_allowlist_plus_the_workflow_the_step_and_the_setup_actions(
        tmp_path):
    tools = Tools()
    host = cil.detect_host()
    plan = cil.build_plan(cil.parse_workflow(REAL_TEXT), "unit-macos", host, trust_actions=True)
    for step in plan.steps:                      # the local uv and node stand in for the pinned releases
        if step.kind == "setup-uv":
            step.params["version"] = tools.uv_version
        if step.kind == "setup-node":
            step.params["version"] = tools.node_version
    allowlist = cil.HOST_ENV_NAMES["Windows" if host.os == "Windows" else "other"]
    host_env = {**{name: os.environ.get(name, f"host-{name}") for name in allowlist}, **JUNK}
    executor = cil.JobExecutor(plan, None, tmp_path / "run", cil.RunOptions(cache=tmp_path / "cache"),
                               tools=tools, say=lambda line: None, host_env=host_env)
    for step in plan.steps:
        if step.kind in {"setup-uv", "setup-node"}:
            executor.dispatch(step, io.StringIO())
    env = executor.step_env(plan.steps[-1])

    raw = REAL_RAW["jobs"]["unit-macos"]
    workflow_env, step_env = string_env(REAL_RAW["env"]), string_env(raw["steps"][-1]["env"])
    hosted = {"CI": "true", "GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": str(executor.workspace),
              "RUNNER_OS": host.os, "RUNNER_ARCH": host.arch, "RUNNER_TEMP": str(executor.temp),
              "GIT_CONFIG_GLOBAL": str(executor.git_config), "GIT_CONFIG_NOSYSTEM": "1",
              "UV_NO_CONFIG": "1", "npm_config_userconfig": str(executor.temp / "empty.npmrc"),
              "LANG": "en_US.UTF-8"}
    setup = {name for names in cil.SETUP_ENV.values() for name in names}
    assert setup == {"UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "UV_PYTHON"}      # the CI log's three
    expected = (set(allowlist) | set(hosted) | {"npm_config_cache"} | set(workflow_env) | set(step_env)
                | setup)

    def norm(names):
        return {cil.env_key(n) for n in names}

    assert norm(env) == norm(expected), (sorted(norm(env) - norm(expected)), sorted(norm(expected) - norm(env)))
    for name, value in {**workflow_env, **step_env, **hosted}.items():
        assert env[cil.env_key(name)] == value, name
    assert env["UV_PYTHON"] == action_with("unit-macos", "astral-sh/setup-uv")["python-version"]
    assert Path(env[cil.env_key("npm_config_cache")]).is_relative_to(executor.temp)
    for name in allowlist:
        if name != "PATH":
            assert env[cil.env_key(name)] == host_env[name], name
    path = env["PATH"].split(os.pathsep)
    assert Path(path[0]) == tools.node and path[-1] == host_env["PATH"].split(os.pathsep)[-1]
    assert [Path(p).name for p in path[1:4]] == ["uv-python-dir", tools.uv.name, "uv-tool-bin"]
    for gone in ("UV_TOOL_BIN_DIR", "UV_TOOL_DIR", "GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT",
                 "PYTHONPATH", "VIRTUAL_ENV", "HOMEBREW_PREFIX", "HTTPS_PROXY"):
        assert cil.env_key(gone) not in env, gone


# ---- the executor, on small synthetic workflows with real shells --------------------------------------

class Tools:
    """uv and node from this machine, presented as the versions the synthetic workflow names."""

    def __init__(self):
        self.uv = Path(shutil.which("uv")).parent
        self.node = Path(shutil.which("node")).parent
        self.uv_version = subprocess.run(["uv", "--version"], capture_output=True,
                                         encoding="utf-8").stdout.split()[1]
        self.node_version = subprocess.run([shutil.which("node"), "--version"], capture_output=True,
                                           encoding="utf-8").stdout.strip().lstrip("v")

    def uv_dir(self, version):
        assert version == self.uv_version
        return self.uv

    def node_dir(self, version):
        assert version == self.node_version
        return self.node


def run_synthetic(tmp_path, repo, text, *, host=None, keep_cache=False):
    plan = cil.build_plan(cil.parse_workflow(text), "j", host or cil.detect_host())
    source = cil.prepare_source(repo, tmp_path / "source")
    lines = []
    options = cil.RunOptions(cache=tmp_path / "cache", keep_cache=keep_cache, heartbeat=3600)
    executor = cil.JobExecutor(plan, source, tmp_path / "run", options, tools=Tools(), say=lines.append)
    return executor, executor.run(), lines


def statuses(res):
    return [(s.name, s.status) for s in res.steps]


def test_steps_run_in_order_with_layered_environment_and_stop_at_the_first_failure(tmp_path, repo,
                                                                                    monkeypatch):
    monkeypatch.setenv("BROWSERPLUG_STATE_DIR", "must-not-leak")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--must-not-leak")
    text = synthetic(f"""
        - uses: {CHECKOUT}
          with: {{persist-credentials: false}}
        - name: environment
          env: {{STEP_ONLY: s, FROM_WORKFLOW: step-wins}}
          run: |
            test -f a.txt
            test "$FROM_WORKFLOW" = step-wins
            test "$KEPT" = kept
            test "$STEP_ONLY" = s
            test "$CI" = true
            test "$UV_NO_CONFIG" = 1
            test "$GIT_CONFIG_NOSYSTEM" = 1
            test -z "$BROWSERPLUG_STATE_DIR"
            test -z "$PYTEST_ADDOPTS"
            test -z "$UV_TOOL_BIN_DIR"
            test "$(cat sub/c.txt)" = c1
            test "$(git rev-parse --is-shallow-repository)" = true
        - name: fails
          run: exit 3
        - name: never runs
          run: echo unreachable > unreachable.txt
        - name: not even this
          run: echo unreachable > unreachable.txt
    """, top="env: {FROM_WORKFLOW: wf, KEPT: kept}")
    executor, res, lines = run_synthetic(tmp_path, repo, text)
    assert statuses(res) == [(f"Run {CHECKOUT}", "emulated"), ("environment", "passed"), ("fails", "failed"),
                             ("never runs", "skipped"), ("not even this", "skipped")]
    assert res.failed and cil.exit_code([res]) == 1
    assert "exit code 3" in res.steps[2].note
    assert not (executor.workspace / "unreachable.txt").exists()
    assert (executor.logs / "03-fails.log").exists()
    assert sum("an earlier step failed" in ln for ln in lines) == 2


def test_a_job_runs_on_the_checkout_it_made_and_not_in_the_users_repository(tmp_path, repo):
    text = synthetic(f"""
        - run: git config --global core.autocrlf input
        - uses: {CHECKOUT}
        - run: |
            echo dropped > from-the-job.txt
            test "$(git config core.autocrlf)" = input
            test "$(git config --global core.autocrlf)" = input
            test -z "$(git config --global user.name)"
    """)
    executor, res, _ = run_synthetic(tmp_path, repo, text)
    assert [s.status for s in res.steps] == ["emulated", "emulated", "passed"], res.steps
    assert (executor.workspace / "from-the-job.txt").exists()
    assert not (repo / "from-the-job.txt").exists()


def test_the_job_timeout_kills_the_running_step_and_skips_the_rest(tmp_path, repo):
    text = synthetic("""
        - name: too slow
          run: sleep 60
        - name: after
          run: echo ok
    """, job_extra="timeout-minutes: 0.03")
    started = time.monotonic()
    _, res, _ = run_synthetic(tmp_path, repo, text)
    assert time.monotonic() - started < 45                 # not the 60 s the step asked for
    assert [s.status for s in res.steps] == ["failed", "skipped"] and "timed out" in res.steps[0].note


GRANDCHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open({marker!r}, 'w').write(str(child.pid))\n"
    "{tail}\n")


def test_a_timeout_kills_the_whole_process_tree(tmp_path):
    marker = tmp_path / "grandchild.pid"
    code = GRANDCHILD.format(marker=str(marker), tail="time.sleep(120)")
    res = cil.run_process([sys.executable, "-c", code], env=dict(os.environ), cwd=tmp_path,
                          log=io.StringIO(), timeout=8, label="tree", heartbeat=3600)
    assert res.timed_out
    wait_until_gone(int(marker.read_text()), "the grandchild ended with the timed-out step")


def test_what_a_step_leaves_running_is_cleaned_up_when_the_step_ends(tmp_path):
    marker = tmp_path / "grandchild.pid"
    code = GRANDCHILD.format(marker=str(marker), tail="")
    res = cil.run_process([sys.executable, "-c", code], env=dict(os.environ), cwd=tmp_path,
                          log=io.StringIO(), timeout=60, label="orphan", heartbeat=3600)
    assert res.code == 0 and not res.timed_out
    wait_until_gone(int(marker.read_text()), "what the step left running was cleaned up")


def test_output_is_copied_to_the_log_and_the_exit_code_is_returned(tmp_path):
    log = io.StringIO()
    res = cil.run_process([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); "
                           "sys.exit(7)"], env=dict(os.environ), cwd=tmp_path, log=log, timeout=60,
                          label="io", heartbeat=3600)
    assert res.code == 7 and sorted(log.getvalue().split()) == ["err", "out"]


def test_the_job_timeout_is_the_workflows_and_can_be_scaled():
    plan = real_plan("unit", "Windows")

    def executor(scale, spent):
        ex = cil.JobExecutor(plan, None, Path("."), cil.RunOptions(cache=Path("."), timeout_scale=scale),
                             tools=None)
        ex.started = time.monotonic() - spent
        return ex

    assert 1790 < executor(1.0, 5).remaining() <= 1795              # 30 minutes less the 5 s spent
    assert 3590 < executor(2.0, 5).remaining() <= 3595
    with pytest.raises(cil.StepFailed, match="timeout-minutes"):
        executor(1.0, 1801).remaining()
    assert executor(2.0, 1801).remaining() > 1700


# ---- steps that cannot run here: only the ones named may be missing ---------------------------------------

def result(failed=False, unavailable=()):
    steps = [cil.StepResult(1, "a", "failed" if failed else "passed", 1.0)]
    steps += [cil.StepResult(2 + i, name, "not-reproducible", 0.0) for i, name in enumerate(unavailable)]
    return cil.JobResult("j", Path("."), steps, 1.0)


def test_repeat_runs_every_time_and_fails_on_any_failure():
    outcomes = iter([result(), result(failed=True), result()])
    results = cil.run_repeats(3, lambda n: next(outcomes))
    assert len(results) == 3 and cil.exit_code(results) == 1


def test_fail_fast_stops_at_the_first_failure():
    seen = []
    outcomes = iter([result(), result(failed=True), result()])

    def run_once(n):
        seen.append(n)
        return next(outcomes)

    assert len(cil.run_repeats(3, run_once, fail_fast=True)) == 2 and seen == [1, 2]


def test_exit_codes_and_the_steps_that_may_be_unavailable():
    assert cil.exit_code([result(), result()]) == 0
    assert cil.exit_code([result(), result(failed=True)]) == 1
    assert cil.exit_code([result(unavailable=["lint"])]) == 3
    assert cil.exit_code([result(unavailable=["lint"])], ["lint"]) == 0
    assert cil.exit_code([result(unavailable=["lint", "tests"])], ["lint"]) == 3      # "tests" never ran
    assert cil.exit_code([result(unavailable=["lint", "tests"])], ["lint", "tests"]) == 0
    assert cil.exit_code([result(failed=True, unavailable=["lint"])], ["lint"]) == 1
    assert cil.exit_code([result(unavailable=["lint"]), result()], ["lint"]) == 0
    assert cil.exit_code([result(unavailable=["lint"]), result(unavailable=["other"])], ["lint"]) == 3


REAL_DETECT_HOST = cil.detect_host


def no_docker_host():
    real = REAL_DETECT_HOST()
    return cil.Host(real.os, real.arch, real.cores,
                    which=lambda name: None if name == "docker" else shutil.which(name))


def test_a_step_that_cannot_run_here_is_reported_and_the_exit_code_says_so(tmp_path, repo):
    text = synthetic("""
        - run: echo fine
        - name: needs docker
          run: docker ps
    """)
    _, res, _ = run_synthetic(tmp_path, repo, text, host=no_docker_host())
    assert [s.status for s in res.steps] == ["passed", "not-reproducible"]
    assert cil.exit_code([res]) == 3
    assert cil.exit_code([res], ["needs docker"]) == 0
    assert cil.exit_code([res], ["echo fine"]) == 3             # naming another step accepts nothing


def test_the_command_line_accepts_a_missing_step_by_name_only(tmp_path, repo, capsys, monkeypatch):
    workflow = tmp_path / "wf.yml"
    workflow.write_text(synthetic(f"""
        - uses: {CHECKOUT}
        - name: tests
          run: docker ps
        - name: lint
          run: docker ps
    """), encoding="utf-8")
    monkeypatch.setattr(cil, "detect_host", no_docker_host)
    base = ["--workflow", str(workflow), "--job", "j", "--repo", str(repo), "--ref", "HEAD", "--stress", "0",
            "--trust-actions"]

    caches = itertools.count()

    def run(*extra):
        code = cil.main([*base, "--cache-dir", str(tmp_path / f"cache{next(caches)}"), *extra])
        return code, capsys.readouterr()

    code, captured = run()
    assert code == 3 and "INCOMPLETE" in captured.out and "PASSED" not in captured.out
    code, captured = run("--allow-unavailable", "lint")
    assert code == 3 and "INCOMPLETE" in captured.out and "PASSED" not in captured.out   # "tests" did not run
    code, captured = run("--allow-unavailable", "lint", "--allow-unavailable", "tests")
    assert code == 0 and "j: PASSED (" in captured.out and "allowed" in captured.out
    assert "tests" in captured.out.split("j: PASSED")[1]
    code, captured = run("--allow-unavailable", "nope")
    assert code == 2 and "'nope' is not a step" in captured.err and "'tests'" in captured.err


# ---- a step's pytest summary ----------------------------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("=========== 2 failed, 4027 passed, 39 skipped in 477.12s (0:07:57) ===========",
     "2 failed, 4027 passed, 39 skipped in 477.12s (0:07:57)"),
    ("4123 passed, 66 skipped in 759.12s (0:12:39)", "4123 passed, 66 skipped in 759.12s (0:12:39)"),
    ("1 passed in 0.01s", "1 passed in 0.01s"),
    ("12 passed, 3 warnings, 1 error in 5.50s", "12 passed, 3 warnings, 1 error in 5.50s"),
    ("======= no tests ran in 0.02s =======", "no tests ran in 0.02s"),
])
def test_pytest_summary_lines_are_recognised_with_and_without_the_banner(tmp_path, line, expected):
    log = tmp_path / "step.log"
    log.write_text(f"collected 3 items\n......  [100%]\n{line}\n", encoding="utf-8")
    assert cil.pytest_summaries(log) == (expected,)


def test_other_lines_are_not_taken_for_a_pytest_summary(tmp_path):
    log = tmp_path / "step.log"
    log.write_text("3 files in 2s\nFAILED tests/x.py::t - assert 1 == 2\nDownloaded 5 passed in 3s\n"
                   "Installed 43 packages in 58ms\n", encoding="utf-8")
    assert cil.pytest_summaries(log) == ()
    assert cil.pytest_summaries(tmp_path / "missing.log") == ()


def test_a_step_with_two_pytest_runs_reports_both_and_a_failed_step_still_reports(tmp_path, repo):
    text = synthetic("""
        - name: suites
          run: |
            echo "10 passed in 1.00s"
            echo "=== 2 failed, 3 passed, 1 skipped in 2.00s (0:00:02) ==="
            exit 1
    """)
    _, res, lines = run_synthetic(tmp_path, repo, text)
    step = res.steps[0]
    assert step.status == "failed"
    assert step.pytest == ("10 passed in 1.00s", "2 failed, 3 passed, 1 skipped in 2.00s (0:00:02)")
    assert any(ln.strip() == "pytest: 2 failed, 3 passed, 1 skipped in 2.00s (0:00:02)" for ln in lines)


def test_emulated_steps_report_no_pytest_line(tmp_path, repo):
    text = synthetic(f"""
        - uses: {CHECKOUT}
        - run: echo "1 passed in 0.10s"
    """)
    _, res, _ = run_synthetic(tmp_path, repo, text)
    assert [s.pytest for s in res.steps] == [(), ("1 passed in 0.10s",)]


def test_a_whole_run_reports_untracked_files_pytest_lines_and_the_verdict(tmp_path, repo, capsys):
    write(repo / "helper.py", "x = 1\n")                  # untracked
    workflow = tmp_path / "wf.yml"
    workflow.write_text(synthetic(f"""
        - uses: {CHECKOUT}
        - name: tests
          run: |
            test -f a.txt
            echo "5 passed, 1 skipped in 0.10s (0:00:00)"
    """), encoding="utf-8")
    common = ["--workflow", str(workflow), "--repo", str(repo), "--stress", "0", "--job", "j",
              "--trust-actions"]
    assert cil.main([*common, "--cache-dir", str(tmp_path / "cache")]) == 0
    out = capsys.readouterr().out
    assert out.index("UNTRACKED") < out.index("preparing the source")
    assert "pytest: 5 passed, 1 skipped in 0.10s (0:00:00)" in out
    assert "j: PASSED" in out and "NOTE: this run included 1 untracked file(s)" in out
    summary = json.loads(next((tmp_path / "cache" / "runs").glob("*/summary.json")).read_text("utf-8"))
    assert summary["untracked"] == ["helper.py"] and summary["stress"] == 0
    assert summary["repeats"][0]["steps"][-1]["pytest"] == ["5 passed, 1 skipped in 0.10s (0:00:00)"]
    assert cil.main([*common, "--ref", "HEAD", "--cache-dir", str(tmp_path / "cache2")]) == 0
    out = capsys.readouterr().out
    assert "UNTRACKED" not in out and "NOTE:" not in out


@pytest.mark.parametrize("keep_cache", [False, True])
def test_setup_actions_set_what_setup_uv_and_setup_node_set(tmp_path, repo, keep_cache):
    tools = Tools()
    text = synthetic(f"""
        - uses: {SETUP_UV}
          with: {{version: "{tools.uv_version}", python-version: "3.12"}}
        - uses: {SETUP_NODE}
          with: {{node-version: "{tools.node_version}", package-manager-cache: false}}
        - name: check
          run: |
            test "$UV_PYTHON" = 3.12
            test -n "$UV_PYTHON_INSTALL_DIR"
            test -z "$UV_TOOL_BIN_DIR" && test -z "$UV_TOOL_DIR"
            case "$UV_CACHE_DIR" in *setup-uv-cache) ;; *) exit 1;; esac
            test "$(node --version)" = "v{tools.node_version}"
    """)
    executor, res, _ = run_synthetic(tmp_path, repo, text, keep_cache=keep_cache)
    assert [s.status for s in res.steps] == ["emulated", "emulated", "passed"], res.steps
    assert set(executor.exports) == set(cil.SETUP_ENV["astral-sh/setup-uv"])
    assert cil.SETUP_ENV["actions/setup-node"] == ()
    root = tmp_path / ("cache" if keep_cache else "run")
    assert Path(executor.exports["UV_CACHE_DIR"]).is_relative_to(root)
    assert ("keep" in Path(executor.exports["UV_CACHE_DIR"]).parts) == keep_cache
    first = executor.step_env(executor.plan.steps[-1])["PATH"].split(os.pathsep)[0]
    assert Path(first) == tools.node                    # setup-node ran last, so node is first


def test_the_timezone_step_fails_on_a_windows_machine_in_another_zone_and_sets_TZ_elsewhere(
        tmp_path, monkeypatch):
    step = cil.PlannedStep(1, "zone", "timezone", "emulate", "s",
                           params={"zone": "Z", "tz": "Europe/Warsaw"})

    class Log:
        def write(self, text):
            pass

    def build(os_name):
        plan = cil.Plan("w", "j", "j", cil.RUNNERS["windows-latest"], fake_host(os_name), None, {},
                        [step], {}, 0)
        return cil.JobExecutor(plan, None, tmp_path, cil.RunOptions(cache=tmp_path), tools=None)

    posix = build("macOS")
    assert posix.do_timezone(step, Log())[0] == "emulated" and posix.exports["TZ"] == "Europe/Warsaw"

    monkeypatch.setattr(cil.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "Other\n", ""))
    with pytest.raises(cil.StepFailed, match="job sets 'Z'"):
        build("Windows").do_timezone(step, Log())
    monkeypatch.setattr(cil.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "Z\n", ""))
    assert build("Windows").do_timezone(step, Log())[0] == "emulated"


def test_upload_artifact_copies_matches_and_fails_when_the_workflow_says_error(tmp_path, repo):
    text = synthetic(f"""
        - uses: {CHECKOUT}
        - run: mkdir -p dist/x && echo a > dist/x/one.zip && echo b > dist/two.txt
        - uses: {UPLOAD}
          with:
            name: pkgs
            path: |
              dist/x/*.zip
              dist/*.txt
            if-no-files-found: error
        - uses: {UPLOAD}
          with: {{name: none, path: "nothing/*", if-no-files-found: error}}
    """)
    _, res, _ = run_synthetic(tmp_path, repo, text)
    assert [s.status for s in res.steps] == ["emulated", "passed", "emulated", "failed"]
    assert (tmp_path / "run" / "artifacts" / "pkgs" / "dist" / "x" / "one.zip").exists()
    assert (tmp_path / "run" / "artifacts" / "pkgs" / "dist" / "two.txt").exists()
    assert "if-no-files-found" in res.steps[3].note


# ---- stopping a real run --------------------------------------------------------------------------------

SLEEPER = "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(300)\n"


def stress_loops_of(runner_pid):
    loops = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            tagged = cil.STRESS_MARKER in (proc.info["cmdline"] or [])
            if tagged and runner_pid in [parent.pid for parent in proc.parents()]:
                loops.append(proc)
        except psutil.Error:
            continue
    return loops


@pytest.mark.parametrize("how", [pytest.param("sigterm", marks=posix_only), "kill"])
def test_a_stopped_runner_leaves_no_busy_loop_or_step_process_behind(tmp_path, repo, how):
    """The real runner, a real step and two real busy loops, stopped from outside.

    SIGTERM unwinds the runner, which kills the step and the loops. A hard kill runs no Python
    at all: the loops notice that the runner is gone, and on Windows the job object ends the
    step. On POSIX the step's own processes are not ended by a kill -9 (nothing is left to kill
    them), so that case only requires the busy loops to go and removes the step itself.
    """
    write(repo / "sleeper.py", SLEEPER)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "sleeper")
    pidfile = tmp_path / "step.pid"
    workflow = tmp_path / "wf.yml"
    workflow.write_text(synthetic(f"""
        - uses: {CHECKOUT}
        - run: |
            "{Path(sys.executable).as_posix()}" sleeper.py "{pidfile.as_posix()}"
    """), encoding="utf-8")
    output = open(tmp_path / "runner.out", "wb")
    runner = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--workflow", str(workflow), "--repo", str(repo), "--ref", "HEAD",
         "--cache-dir", str(tmp_path / "cache"), "--stress", "2", "--job", "j", "--trust-actions"],
        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
    step_pid, loops, top = None, [], []
    try:
        def up():
            nonlocal loops, top
            assert runner.poll() is None, (f"the runner exited early with {runner.returncode}: "
                                           + (tmp_path / "runner.out").read_text("utf-8", "replace"))
            loops = stress_loops_of(runner.pid)        # behind a launcher stub a loop is two processes
            top = [p for p in loops if p.ppid() not in {q.pid for q in loops}]
            return pidfile.exists() and pidfile.read_text() and len(top) == 2

        try:
            # 120 s: the runner packages the working tree and starts the step before any of this
            # exists. interval 0.3: each poll scans the whole process table.
            wait_until(up, "the step and two busy loops came up", timeout=120, interval=0.3)
        except AssertionError as e:
            pytest.fail(f"{e}: {pidfile.exists()=} {len(loops)=}; "
                        + (tmp_path / "runner.out").read_text("utf-8", "replace"))
        step_pid = int(pidfile.read_text())
        loop_pids = [p.pid for p in loops]
        real_runner = psutil.Process(top[0].ppid())           # behind a launcher stub, the interpreter
        if how == "sigterm":
            runner.send_signal(signal.SIGTERM)
            assert runner.wait(timeout=60) == 128 + signal.SIGTERM
        else:
            real_runner.kill()
        for pid in loop_pids:
            wait_until_gone(pid, f"busy loop {pid} ended after the {how}")
        if how == "sigterm" or os.name == "nt":
            wait_until_gone(step_pid, f"the step's process ended after the {how}")
    finally:
        for pid in [step_pid, *[p.pid for p in loops]]:
            if pid and psutil.pid_exists(pid):
                with contextlib.suppress(psutil.Error):
                    psutil.Process(pid).kill()
        with contextlib.suppress(psutil.Error):
            for child in psutil.Process(runner.pid).children(recursive=True):
                child.kill()
        runner.kill()
        runner.wait()
        output.close()


# ---- the command line ---------------------------------------------------------------------------------

def test_dry_run_prints_the_plan_and_runs_nothing(capsys, tmp_path):
    code = cil.main(["--dry-run", "--job", "unit-macos", "--stress", "2", "--cache-dir",
                     str(tmp_path / "cache"), "--trust-actions"])
    out = capsys.readouterr().out
    last = REAL_RAW["jobs"]["unit-macos"]["steps"][-1]
    assert code == 0
    assert last["run"].strip() in out and f"TZ={last['env']['TZ']}" in out
    assert "busy-loop" in out and "source:" in out
    assert not (tmp_path / "cache").exists()              # nothing downloaded, nothing created


def test_without_a_stress_option_the_run_sizes_the_loops_to_the_runner(capsys):
    assert cil.main(["--dry-run", "--job", "unit-macos", "--trust-actions"]) == 0
    out = capsys.readouterr().out
    wanted = cil.resolve_stress("auto", cil.detect_host(), cil.RUNNERS["macos-15"])
    if wanted:
        assert f"{wanted} busy-loop" in out and "own priority" in out
    else:
        assert "stress: none" in out
    assert cil.main(["--dry-run", "--job", "unit-macos", "--trust-actions", "--stress", "0"]) == 0
    assert "stress: none" in capsys.readouterr().out


def test_list_and_error_exit_codes(capsys, tmp_path):
    assert cil.main(["--list"]) == 0
    assert "ci-ok" in capsys.readouterr().out
    assert cil.main(["--dry-run", "--job", "nope"]) == 2
    assert "no job" in capsys.readouterr().err
    assert cil.main(["--dry-run", "--job", "ci-ok"]) == 2
    assert "aggregates" in capsys.readouterr().err
    bad = tmp_path / "bad.yml"
    bad.write_text(synthetic("- uses: some/new-action@v1"), encoding="utf-8")
    assert cil.main(["--dry-run", "--job", "j", "--workflow", str(bad)]) == 2
    assert "some/new-action" in capsys.readouterr().err
    assert cil.main(["--dry-run", "--job", "unit", "--repeat", "0"]) == 2
    conditional = tmp_path / "conditional.yml"
    conditional.write_text(synthetic("- run: echo hi", job_extra="if: github.ref == 'refs/heads/main'"),
                           encoding="utf-8")
    assert cil.main(["--dry-run", "--job", "j", "--workflow", str(conditional)]) == 2
    assert "not evaluated" in capsys.readouterr().err


def test_the_runner_binds_no_port_and_does_not_read_the_servers_state():
    source = SCRIPT.read_text(encoding="utf-8")
    for needle in ("import socket", "8772", "2830", "BROWSERPLUG_STATE_DIR", "ws-token", "config.json"):
        assert needle not in source, needle


# ---- --hammer: repeat the tests a change touches (a local addition to the CI job) ----------------------

def test_modules_map_to_the_dotted_names_tests_import():
    assert cil.module_of("server/runtime/watchdog.py") == ("server.runtime.watchdog", "server.runtime", "watchdog")
    assert cil.module_of("server\\web\\http_endpoints.py") == ("server.web.http_endpoints", "server.web",
                                                              "http_endpoints")
    assert cil.module_of("server/server.py") == ("server.server", "server", "server")
    assert cil.module_of("launcher/stdio_relay.py") == ("launcher.stdio_relay", "launcher", "stdio_relay")
    assert cil.module_of("shared-py/browserplug_shared/version.py") == (
        "browserplug_shared.version", "browserplug_shared", "version")
    for path in ("server/runtime/__init__.py", "server/__main__.py", "scripts/ci_local.py",
                 "tests/unit/test_x.py", "server/data.json", "docs/server.py.md"):
        assert cil.module_of(path) is None, path


UNIT_TESTS = {
    "tests/unit/test_watchdog.py": "from server.runtime import watchdog\n",
    "tests/unit/test_dotted.py": "import server.runtime.watchdog as w\nfrom server.web.http_endpoints import X\n",
    "tests/unit/test_spawn.py": "SCRIPT = ROOT / 'server' / 'server.py'\nrun('server.py')\n",
    "tests/unit/test_import_form.py": "from server.web import other, http_endpoints\n",
    "tests/unit/test_lookalike.py": "import server.runtime.watchdog_extra\nfrom server.runtime import watchdog_extra\nmy_server.py\n",
    "tests/unit/test_unrelated.py": "def test_nothing(): pass\n",
}


def test_changed_test_files_come_first_then_the_tests_that_name_a_changed_module():
    sel = cil.select_hammer_tests(["server/runtime/watchdog.py", "tests/unit/test_unrelated.py",
                                   "README.md"], UNIT_TESTS)
    assert sel.targets == ["tests/unit/test_unrelated.py", "tests/unit/test_dotted.py",
                           "tests/unit/test_watchdog.py"]
    assert sel.reasons["tests/unit/test_unrelated.py"] == "changed"
    assert sel.reasons["tests/unit/test_watchdog.py"] == "names server.runtime.watchdog"
    assert sel.reasons["tests/unit/test_dotted.py"] == "names server.runtime.watchdog"
    assert sel.dropped == [] and sel.changed == 3 and not sel.explicit


def test_a_module_is_found_by_its_dotted_name_a_one_line_import_or_its_file_name():
    def pick(path):
        return cil.select_hammer_tests([path], UNIT_TESTS).reasons

    assert pick("server/web/http_endpoints.py") == {
        "tests/unit/test_dotted.py": "names server.web.http_endpoints",
        "tests/unit/test_import_form.py": "names server.web.http_endpoints"}
    assert pick("server/server.py") == {"tests/unit/test_spawn.py": "names server.py as a file name"}
    assert pick("server/runtime/watchdog_extra.py") == {
        "tests/unit/test_lookalike.py": "names server.runtime.watchdog_extra"}
    assert "tests/unit/test_lookalike.py" not in pick("server/runtime/watchdog.py")     # a longer name is not a match


def test_a_test_that_names_two_changed_modules_outranks_one_that_names_one():
    tests = {"tests/unit/test_a.py": "import server.a\n", "tests/unit/test_b.py": "import server.a, server.b\n",
             "tests/unit/test_c.py": "run('b.py')\n"}
    sel = cil.select_hammer_tests(["server/a.py", "server/b.py"], tests)
    assert sel.targets == ["tests/unit/test_b.py", "tests/unit/test_a.py", "tests/unit/test_c.py"]
    assert sel.reasons["tests/unit/test_b.py"] == "names server.a, server.b"
    assert sel.reasons["tests/unit/test_c.py"] == "names b.py as a file name"


def test_the_cap_cuts_from_the_end_says_what_it_dropped_and_prints_a_cap_line():
    tests = {f"tests/unit/test_{i}.py": "import server.core\n" for i in range(6)}
    tests["tests/unit/test_changed.py"] = "pass\n"
    sel = cil.select_hammer_tests(["server/core.py", "tests/unit/test_changed.py"], tests, max_files=3)
    assert sel.targets == ["tests/unit/test_changed.py", "tests/unit/test_0.py", "tests/unit/test_1.py"]
    assert sel.dropped == [f"tests/unit/test_{i}.py" for i in range(2, 6)]
    text = "\n".join(cil.format_selection(sel, 20))
    assert "CAP: at most 3 files" in text and "4 candidate(s) are NOT repeated" in text
    assert "--hammer-max-files" in text and "--hammer-tests FILE" in text
    assert all(f"test_{i}.py" in text for i in range(2, 6))        # a short list is named in full
    many = {f"tests/unit/test_{i}.py": "pass\n" for i in range(5)}
    capped = cil.select_hammer_tests(list(many), many, max_files=2)
    assert capped.targets == ["tests/unit/test_0.py", "tests/unit/test_1.py"] and len(capped.dropped) == 3
    default = cil.select_hammer_tests(list(many), many)
    assert default.limit == cil.HAMMER_MAX_FILES == 8 and default.dropped == []


def test_changed_test_files_rank_by_lines_changed_not_by_name():
    names = [f"tests/unit/test_{c}.py" for c in "abcdefghijkl"]
    tests = dict.fromkeys(names, "pass\n")
    sizes = {n: 3 for n in names}
    sizes["tests/unit/test_l.py"] = 400
    sizes["tests/unit/test_k.py"] = 90
    sizes["tests/unit/test_h.py"] = 5
    sel = cil.select_hammer_tests(list(sizes), tests, max_files=4, sizes=sizes)
    assert sel.targets == ["tests/unit/test_l.py", "tests/unit/test_k.py", "tests/unit/test_h.py",
                           "tests/unit/test_a.py"]                  # equal sizes fall back to the path
    assert sel.reasons["tests/unit/test_l.py"] == "changed, 400 line(s) added+removed"
    assert sel.dropped == [f"tests/unit/test_{c}.py" for c in "bcdefgij"]
    assert cil.select_hammer_tests(list(sizes), tests, max_files=4).targets == names[:4]     # no sizes: by path


def test_a_changed_test_file_outranks_one_picked_through_a_module_however_much_the_module_changed():
    tests = {"tests/unit/test_big_consumer.py": "import server.core\n", "tests/unit/test_tiny.py": "pass\n"}
    sel = cil.select_hammer_tests(["server/core.py", "tests/unit/test_tiny.py"], tests, max_files=1,
                                  sizes={"server/core.py": 5000, "tests/unit/test_tiny.py": 1})
    assert sel.targets == ["tests/unit/test_tiny.py"] and sel.dropped == ["tests/unit/test_big_consumer.py"]


def test_tests_that_name_a_more_changed_module_come_first_among_the_indirect_ones():
    tests = {"tests/unit/test_a.py": "import server.small\n", "tests/unit/test_b.py": "import server.big\n"}
    sel = cil.select_hammer_tests(["server/small.py", "server/big.py"], tests,
                                  sizes={"server/small.py": 2, "server/big.py": 300})
    assert sel.targets == ["tests/unit/test_b.py", "tests/unit/test_a.py"]
    assert sel.reasons["tests/unit/test_b.py"] == "names server.big (300 line(s) changed there)"


SHARED_TESTS = {
    "tests/unit/test_a.py": "from wait_helper import wait_until\n",
    "tests/unit/test_b.py": "import wait_helper\n",
    "tests/unit/test_c.py": "def test_x():\n    from wait_helper import (\n        wait_until,\n    )\n",
    "tests/unit/test_d.py": "from port_coordinator import allocate_port\nimport tests.wait_helper as w\n",
    "tests/unit/test_e.py": "from tests.unit import test_a\nfrom test_c import helper\n",
    "tests/unit/test_f.py": "from wait_helper_extra import x\n# wait_helper is only mentioned here\n",
    "tests/unit/test_g.py": "def test_nothing(): pass\n",
}


def test_a_changed_shared_test_module_selects_the_tests_that_import_it():
    sel = cil.select_hammer_tests(["tests/wait_helper.py"], SHARED_TESTS, sizes={"tests/wait_helper.py": 40})
    assert sel.targets == ["tests/unit/test_a.py", "tests/unit/test_b.py", "tests/unit/test_c.py",
                           "tests/unit/test_d.py"]                  # a lookalike name and a comment are no import
    assert sel.reasons["tests/unit/test_a.py"] == "imports wait_helper (40 line(s) changed there)"
    only_port = cil.select_hammer_tests(["tests/port_coordinator.py"], SHARED_TESTS)
    assert only_port.targets == ["tests/unit/test_d.py"] and only_port.reasons == {
        "tests/unit/test_d.py": "imports port_coordinator"}
    helper = cil.select_hammer_tests(["tests/unit/proxy_doubles.py", "tests/facade_double.py"],
                                     {"tests/unit/test_p.py": "from proxy_doubles import Fake\n",
                                      "tests/unit/test_q.py": "    from facade_double import FacadeDouble\n"})
    assert helper.targets == ["tests/unit/test_p.py", "tests/unit/test_q.py"]


def test_a_changed_test_file_that_others_import_from_selects_its_importers_too():
    sel = cil.select_hammer_tests(["tests/unit/test_a.py"], SHARED_TESTS, sizes={"tests/unit/test_a.py": 9})
    assert sel.targets == ["tests/unit/test_a.py", "tests/unit/test_e.py"]
    assert sel.reasons == {"tests/unit/test_a.py": "changed, 9 line(s) added+removed",
                           "tests/unit/test_e.py": "imports test_a (9 line(s) changed there)"}


def test_a_changed_conftest_reaches_every_unit_test_but_ranks_last():
    tests = {"tests/unit/test_a.py": "pass\n", "tests/unit/test_b.py": "import server.core\n"}
    sel = cil.select_hammer_tests(["tests/unit/conftest.py", "server/core.py"], tests)
    assert sel.targets == ["tests/unit/test_b.py", "tests/unit/test_a.py"]
    assert sel.reasons["tests/unit/test_a.py"] == "is under tests/unit/conftest.py (applies to every test)"
    assert sel.reasons["tests/unit/test_b.py"] == ("names server.core; "
                                                   "is under tests/unit/conftest.py (applies to every test)")
    assert cil.select_hammer_tests(["tests/conftest.py"], tests).targets == list(tests)
    for path in ("tests/integration/test_x.py", "tests/unit/golden/tools_list.json", "tests/run.py.lock",
                 "tests/unit/__init__.py"):
        assert cil.select_hammer_tests([path], tests).targets == [], path


def test_support_modules_map_to_the_names_tests_import():
    assert cil.support_module_of("tests/wait_helper.py") == "wait_helper"
    assert cil.support_module_of("tests\\unit\\proxy_doubles.py") == "proxy_doubles"
    assert cil.support_module_of("tests/unit/test_x.py") == "test_x"
    for path in ("tests/unit/conftest.py", "tests/conftest.py", "tests/integration/test_x.py",
                 "tests/js/x.js", "server/server.py", "tests/unit/__init__.py", "tests/unit/golden/a.py"):
        assert cil.support_module_of(path) is None, path


def test_the_dropped_list_is_shortened_in_the_printout():
    sel = cil.HammerSelection(targets=["tests/unit/test_a.py"], reasons={"tests/unit/test_a.py": "changed"},
                              dropped=[f"tests/unit/test_{i}.py" for i in range(30)], limit=1)
    text = "\n".join(cil.format_selection(sel, 5))
    assert "30 candidate(s)" in text and "and 20 more" in text and "test_29.py" not in text
    assert "test_9.py" in text and "test_10.py" not in text


def test_nothing_changed_selects_nothing_and_says_how_to_ask_for_tests():
    sel = cil.select_hammer_tests(["README.md", "docs/install.md"], UNIT_TESTS)
    assert sel.targets == [] and sel.dropped == []
    text = "\n".join(cil.format_selection(sel, 20))
    assert "nothing selected" in text and "--hammer-tests" in text and "--base" in text
    assert "leave the job's own verdict standing" in text and "exit 4" not in text
    only = "\n".join(cil.format_selection(sel, 20, only=True))
    assert "--hammer-only would exit 4 here: nothing would be tested" in only


def test_named_tests_are_taken_as_given_deduplicated_and_never_capped():
    nodes = [f"tests/unit/test_{i}.py" for i in range(12)] + ["tests/unit/test_0.py",
                                                              "tests/unit/test_x.py::test_y"]
    sel = cil.explicit_selection(nodes)
    assert sel.explicit and sel.targets == nodes[:12] + ["tests/unit/test_x.py::test_y"] and not sel.dropped
    assert set(sel.reasons.values()) == {"named with --hammer-tests"}
    text = "\n".join(cil.format_selection(sel, 10))
    assert text.startswith("hammer: 10 repetition(s)") and "not part of the CI job" in text
    assert "tests: named with --hammer-tests" in text and "CAP" not in text


# The selection against real git: a throwaway repository with a base commit, a second commit,
# an uncommitted edit and an untracked test file.

@pytest.fixture
def hammer_repo(tmp_path):
    root = tmp_path / "hammer-repo"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    write(root / "server" / "runtime" / "watchdog.py", "VALUE = 1\n")
    write(root / "server" / "other.py", "VALUE = 1\n")
    write(root / "tests" / "unit" / "test_watchdog.py", "from server.runtime import watchdog\n")
    write(root / "tests" / "unit" / "test_names_other.py", "import server.other\n")
    write(root / "tests" / "unit" / "test_unrelated.py", "def test_x(): pass\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "base")
    first = run_git(root, "rev-parse", "HEAD").strip()
    write(root / "server" / "other.py", "VALUE = 2\n")
    run_git(root, "commit", "-q", "-am", "second")
    write(root / "server" / "runtime" / "watchdog.py", "VALUE = 2\n")                  # uncommitted
    write(root / "tests" / "unit" / "test_new.py", "def test_new(): pass\n")             # untracked
    return root, first


def test_the_working_tree_selection_counts_uncommitted_and_untracked_files_since_the_parent(hammer_repo):
    root, _ = hammer_repo
    sel = cil.hammer_selection(root, None, None, [])
    assert sel.targets == ["tests/unit/test_new.py", "tests/unit/test_names_other.py",
                           "tests/unit/test_watchdog.py"]
    assert sel.reasons["tests/unit/test_new.py"] == "changed, 1 line(s) added+removed"      # untracked: all its lines
    assert sel.reasons["tests/unit/test_names_other.py"] == "names server.other (2 line(s) changed there)"
    assert sel.reasons["tests/unit/test_watchdog.py"] == "names server.runtime.watchdog (2 line(s) changed there)"
    assert sel.changed == 3
    assert "origin/main is not available" in sel.base            # no origin/main in this repository


def test_change_sizes_count_added_plus_removed_lines_and_all_lines_of_untracked_files(hammer_repo):
    root, first = hammer_repo
    write(root / "tests" / "unit" / "test_new.py", "a\nb\nc")                       # 3 lines, no final newline
    (root / "tests" / "unit" / "blob.bin").write_bytes(b"\x00\x01\x02")
    assert cil.changed_sizes(root, first, None) == {
        "server/other.py": 2, "server/runtime/watchdog.py": 2, "tests/unit/blob.bin": 1,
        "tests/unit/test_new.py": 3}
    assert cil.changed_sizes(root, first, "HEAD") == {"server/other.py": 2}             # only what HEAD holds
    (root / "tests" / "unit" / "test_unrelated.py").unlink()
    assert "tests/unit/test_unrelated.py" not in cil.changed_sizes(root, first, None)      # deleted: left out


def test_the_working_tree_selection_ranks_changed_tests_by_lines_changed(hammer_repo):
    root, _ = hammer_repo
    write(root / "tests" / "unit" / "test_new.py", "".join(f"def test_{i}(): pass\n" for i in range(30)))
    write(root / "tests" / "unit" / "test_unrelated.py", "def test_x(): pass\ndef test_y(): pass\n")
    sel = cil.hammer_selection(root, None, None, [])
    assert sel.targets[:2] == ["tests/unit/test_new.py", "tests/unit/test_unrelated.py"]
    assert sel.reasons["tests/unit/test_new.py"] == "changed, 30 line(s) added+removed"
    assert sel.targets[2:] == ["tests/unit/test_names_other.py", "tests/unit/test_watchdog.py"]


def test_a_commit_selection_ignores_the_working_tree_and_reads_the_committed_tests(hammer_repo):
    root, _ = hammer_repo
    sel = cil.hammer_selection(root, "HEAD", None, [])
    assert sel.targets == ["tests/unit/test_names_other.py"]            # only server/other.py changed in HEAD
    assert sel.changed == 1
    assert sel.reasons["tests/unit/test_names_other.py"] == "names server.other (2 line(s) changed there)"
    write(root / "tests" / "unit" / "test_names_other.py", "pass\n")             # not what HEAD holds
    assert cil.hammer_selection(root, "HEAD", None, []).targets == ["tests/unit/test_names_other.py"]
    assert cil.hammer_selection(root, None, None, []).reasons["tests/unit/test_names_other.py"].startswith("changed")


def test_the_merge_base_with_origin_main_is_the_default_base_and_base_overrides_it(hammer_repo):
    root, first = hammer_repo
    run_git(root, "update-ref", "refs/remotes/origin/main", first)
    sel = cil.hammer_selection(root, "HEAD", None, [])
    assert sel.base.startswith("the merge base with origin/main") and first[:7] in sel.base
    assert sel.targets == ["tests/unit/test_names_other.py"]
    here = cil.hammer_selection(root, "HEAD", "HEAD", [])             # --base HEAD: nothing since
    assert here.targets == [] and "from --base" in here.base
    back = cil.hammer_selection(root, None, "HEAD", [])               # only the uncommitted edits
    assert back.targets == ["tests/unit/test_new.py", "tests/unit/test_watchdog.py"]
    with pytest.raises(cil.RunnerError):
        cil.hammer_selection(root, None, "no-such-ref", [])


def test_named_tests_skip_git_entirely(tmp_path):
    sel = cil.hammer_selection(tmp_path / "not-a-repository", None, None, ["tests/unit/test_x.py"])
    assert sel.targets == ["tests/unit/test_x.py"] and sel.explicit


def test_the_test_step_is_the_last_tests_run_py_step_that_is_not_the_ruff_gate():
    assert cil.find_test_step(real_plan("unit", "Windows")).name == "Unit and parity suites"
    assert cil.find_test_step(real_plan("unit-macos", "macOS")).env == {
        "TZ": "Europe/Warsaw", "UV_PYTHON_PREFERENCE": "only-managed"}
    assert cil.find_test_step(real_plan("static")).name == "Parity invariants"      # not the ruff step
    with pytest.raises(cil.RunnerError, match="tests/run.py test step"):
        cil.find_test_step(real_plan("build"))


# JUnit parsing and the repetitions themselves, with a fake process in place of pytest.

def junit_text(cases):
    from xml.sax.saxutils import quoteattr
    rows = "".join(
        f'<testcase classname="{c}" name="{n}" time="0.1">'
        + (f"<failure message={quoteattr(m)}>traceback</failure>" if m else "") + "</testcase>"
        for c, n, m in cases)
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{rows}</testsuite></testsuites>'


FILES = ["tests/unit/test_proxy_e2e.py", "tests/unit/test_watchdog.py::test_one"]


def test_junit_class_names_become_pytest_node_ids(tmp_path):
    assert cil.node_id("unit.test_proxy_e2e", "test_a", FILES) == "tests/unit/test_proxy_e2e.py::test_a"
    assert cil.node_id("unit.test_proxy_e2e.TestK", "test_a[x]", FILES) == (
        "tests/unit/test_proxy_e2e.py::TestK::test_a[x]")
    assert cil.node_id("unit.test_watchdog", "test_one", FILES) == "tests/unit/test_watchdog.py::test_one"
    assert cil.node_id("unit.test_other", "test_z", FILES) == "unit.test_other::test_z"
    path = tmp_path / "j.xml"
    path.write_text(junit_text([("unit.test_proxy_e2e", "test_ok", None),
                                ("unit.test_proxy_e2e", "test_bad", "AssertionError: timed out\nsecond line"),
                                ("unit.test_proxy_e2e", "test_skip", None)]), encoding="utf-8")
    assert cil.parse_junit(path, FILES) == (3, {
        "tests/unit/test_proxy_e2e.py::test_bad": "AssertionError: timed out"})


class FakeHammerProcess:
    """Stands in for run_process: writes the JUnit file each scripted repetition asks for."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, argv, *, env, cwd, log, timeout, **kwargs):
        step = self.script[len(self.calls)]
        self.calls.append({"argv": list(argv), "env": dict(env), "cwd": cwd, "timeout": timeout})
        log.write(f"repetition {len(self.calls)}\n")
        if step.get("cases") is not None:
            junit = next(a for a in argv if a.startswith("--junitxml=")).split("=", 1)[1]
            Path(junit).write_text(junit_text(step["cases"]), encoding="utf-8")
        return cil.ProcessResult(step.get("code", 0), step.get("timed_out", False))


PROXY = "unit.test_proxy_e2e"
PASS = {"cases": [(PROXY, "test_survives_restart", None), (PROXY, "test_other", None)]}
FAIL_RESTART = {"cases": [(PROXY, "test_survives_restart", "AssertionError: timed out waiting for the hop"),
                          (PROXY, "test_other", None)], "code": 1}


def hammer_executor(tmp_path, process, lines):
    plan = real_plan("unit", "Windows")
    source = cil.Source("file:///unused", "c" * 40, "b" * 40, "test", [])
    options = cil.RunOptions(cache=tmp_path / "cache", heartbeat=3600)
    ex = cil.JobExecutor(plan, source, tmp_path / "run", options, tools=None, say=lines.append,
                         process=process)
    ex.logs.mkdir(parents=True)
    ex.workspace.mkdir(parents=True)
    ex.path_prepend.append(str(Path(shutil.which("uv")).parent))
    ex.exports["UV_PYTHON"] = "3.12"
    request = cil.HammerRequest(4, cil.explicit_selection(["tests/unit/test_proxy_e2e.py"]),
                                cil.find_test_step(plan))
    return ex, request


def test_the_hammer_runs_the_selected_files_n_times_with_the_jobs_environment_and_counts_per_test(tmp_path):
    other = {"cases": [(PROXY, "test_survives_restart", "AssertionError: other")], "code": 1}
    fake = FakeHammerProcess([PASS, FAIL_RESTART, other, PASS])
    lines = []
    ex, request = hammer_executor(tmp_path, fake, lines)
    res = cil.run_hammer(ex, request, timeout=99.0)
    assert (res.requested, res.completed, res.tests_run) == (4, 4, 2)
    [(test, fails)] = res.failures.items()
    assert test == "tests/unit/test_proxy_e2e.py::test_survives_restart"
    assert [(f.repetition, f.message) for f in fails] == [
        (2, "AssertionError: timed out waiting for the hop"), (3, "AssertionError: other")]
    call = fake.calls[0]
    argv = call["argv"]
    assert Path(argv[0]).stem.lower() == "uv"
    assert argv[1:8] == ["run", "--locked", "--script", "tests/run.py", "tests/unit/test_proxy_e2e.py",
                         "-q", "-p"] and argv[8] == "no:cacheprovider"
    assert argv[9].startswith("--junitxml=") and argv[9].endswith("hammer-01.xml")
    assert call["cwd"] == ex.workspace and call["timeout"] == 99.0
    assert call["env"]["UV_PYTHON"] == "3.12" and call["env"]["UV_PYTHON_PREFERENCE"] == "only-managed"
    assert call["env"]["CI"] == "true" and call["env"]["PYTHONUTF8"] == "1"          # the job's environment
    assert [c["argv"][9].rsplit("hammer-", 1)[1] for c in fake.calls] == [
        f"{n:02d}.xml" for n in range(1, 5)]                                          # one JUnit file each
    assert any("hammer 2/4: FAILED, 1 failing" in line for line in lines)
    assert any("hammer 1/4: passed" in line for line in lines)
    report = "\n".join(cil.format_hammer(res))
    assert "tests/unit/test_proxy_e2e.py::test_survives_restart: 2/4 failed" in report
    assert "first failure (repetition 2): AssertionError: timed out waiting for the hop" in report
    assert "HAMMER FAILED: 1 test(s) failed at least once (4 of 4 repetition(s)" in report


def test_a_hammer_that_never_fails_passes(tmp_path):
    ex, request = hammer_executor(tmp_path, FakeHammerProcess([PASS] * 4), [])
    res = cil.run_hammer(ex, request)
    assert not res.failed and res.failures == {}
    assert "HAMMER PASSED: 2 test(s) in every one of 4 of 4 repetition(s)" in "\n".join(cil.format_hammer(res))


def test_a_pytest_that_died_without_a_failing_test_or_timed_out_counts_as_a_failed_repetition(tmp_path):
    fake = FakeHammerProcess([{"code": 2}, {"timed_out": True, "code": 1}, {"cases": [], "code": 5}, PASS])
    ex, request = hammer_executor(tmp_path, fake, [])
    res = cil.run_hammer(ex, request)
    fails = res.failures[cil.RUN_LEVEL]
    assert [f.repetition for f in fails] == [1, 2, 3]
    assert "pytest exited with code 2" in fails[0].message and "hammer-01.log" in fails[0].message
    assert "timed out" in fails[1].message and "code 5" in fails[2].message
    assert res.failed and "(pytest run): 3/4 failed" in "\n".join(cil.format_hammer(res))


def test_fail_fast_stops_the_hammer_at_the_first_failing_repetition(tmp_path):
    fake = FakeHammerProcess([PASS, FAIL_RESTART, PASS, PASS])
    ex, request = hammer_executor(tmp_path, fake, [])
    res = cil.run_hammer(ex, request, fail_fast=True)
    assert len(fake.calls) == 2 and res.completed == 2 and res.requested == 4
    assert "(2 of 4 repetition(s)" in "\n".join(cil.format_hammer(res))


def test_a_job_without_uv_on_its_path_is_a_runner_error(tmp_path, monkeypatch):
    ex, request = hammer_executor(tmp_path, FakeHammerProcess([PASS]), [])
    monkeypatch.setattr(cil.shutil, "which", lambda *a, **k: None)
    with pytest.raises(cil.RunnerError, match="no uv"):
        cil.run_hammer(ex, request)


def test_the_hammer_is_skipped_when_a_step_failed_or_nothing_was_selected():
    some = cil.explicit_selection(["tests/unit/test_x.py"])
    assert cil.hammer_skip_reason([result()], some) == ""
    assert cil.hammer_skip_reason([result(), result(failed=True)], some).startswith("the job's steps failed")
    assert cil.hammer_skip_reason([], some).startswith("the job's steps failed")
    none = cil.hammer_skip_reason([result()], cil.HammerSelection())
    assert none.startswith("no tests were selected for the hammer") and "job's own verdict stands" in none
    skipped = cil.HammerResult(20, skipped="the job's steps failed")
    assert "skipped: the job's steps failed" in "\n".join(cil.format_hammer(skipped))
    assert not skipped.failed


def test_a_failing_hammer_makes_the_exit_code_one_and_a_skipped_or_clean_one_does_not():
    failing = cil.HammerResult(3, completed=3, failures={"t": [cil.HammerFailure(1, "boom")]})
    assert cil.exit_code([result()], hammer=failing) == 1
    assert cil.exit_code([result()], hammer=cil.HammerResult(3, completed=3)) == 0
    assert cil.exit_code([result()], hammer=cil.HammerResult(3, skipped="why")) == 0
    assert cil.exit_code([result(unavailable=["lint"])], hammer=failing) == 1          # a failure beats 3
    assert cil.exit_code([result(unavailable=["lint"])], hammer=cil.HammerResult(3, completed=3)) == 3


# The command line.

def test_hammer_options_parse_and_imply_each_other():
    parse = cil.build_parser().parse_args
    assert parse([]).hammer is None and parse([]).hammer_tests == [] and parse([]).hammer_max_files == 8
    assert parse(["--hammer"]).hammer == cil.HAMMER_DEFAULT == 20
    assert parse(["--hammer", "7"]).hammer == 7
    args = parse(["--hammer", "--ref", "HEAD", "--hammer-tests", "a.py", "b.py::t", "--stress", "0"])
    assert args.hammer == 20 and args.ref == "HEAD" and args.hammer_tests == ["a.py", "b.py::t"]
    assert parse(["--hammer-only"]).hammer_only and parse(["--base", "main"]).base == "main"


def hammer_workflow(tmp_path, tail="true"):
    path = tmp_path / "wf.yml"
    path.write_text(synthetic(f"""
        - uses: {CHECKOUT}
        - name: Unit tests
          run: |
            echo tests/run.py
            {tail}
    """), encoding="utf-8")
    return path


def test_a_dry_run_prints_the_hammer_selection_and_runs_nothing(tmp_path, repo, capsys):
    common = ["--dry-run", "--workflow", str(hammer_workflow(tmp_path)), "--repo", str(repo), "--job", "j",
              "--trust-actions", "--stress", "0"]
    assert cil.main([*common, "--hammer", "5", "--hammer-tests", "tests/unit/test_a.py",
                     "tests/unit/test_b.py::test_c"]) == 0
    out = capsys.readouterr().out
    assert "hammer: 5 repetition(s)" in out and "not part of the CI job" in out
    assert "tests/unit/test_b.py::test_c  named with --hammer-tests" in out
    assert cil.main([*common, "--hammer-only"]) == 2          # no origin/main and no parent commit here
    capsys.readouterr()
    assert cil.main([*common, "--hammer-only", "--base", "HEAD"]) == 0
    out = capsys.readouterr().out
    assert "--hammer-only: the job's test step is skipped" in out and "nothing selected" in out
    assert "--hammer-only would exit 4 here: nothing would be tested" in out
    assert cil.main([*common, "--base", "HEAD"]) == 2          # --base without --hammer
    assert "--base only belongs to --hammer" in capsys.readouterr().err
    assert cil.main([*common, "--hammer", "0", "--hammer-tests", "x.py"]) == 2
    assert cil.main([*common, "--hammer", "3", "--hammer-tests", "x.py", "--hammer-max-files", "0"]) == 2
    capsys.readouterr()
    assert cil.main(["--dry-run", "--job", "build", "--hammer", "--hammer-tests", "x.py"]) == 2
    assert "test step" in capsys.readouterr().err


def hammer_run(tmp_path, repo, monkeypatch, capsys, *, tail="true", fake=None, extra=(),
               tests=("tests/unit/test_a.py",)):
    """A whole main() run on a synthetic job whose test step prints 'tests/run.py', run_hammer faked.

    `tests` are the --hammer-tests; none means the selection comes from the diff (add --base HEAD in
    `extra` for an empty one)."""
    calls = []

    def fake_hammer(ex, request, *, timeout, fail_fast):
        calls.append({"workspace_exists": ex.workspace.exists(), "targets": request.selection.targets,
                      "count": request.count, "only": request.only})
        return fake or cil.HammerResult(request.count, completed=request.count, tests_run=4)

    monkeypatch.setattr(cil, "run_hammer", fake_hammer)
    code = cil.main(["--workflow", str(hammer_workflow(tmp_path, tail)), "--repo", str(repo), "--job", "j",
                     "--trust-actions", "--stress", "0", "--ref", "HEAD", "--cache-dir", str(tmp_path / "cache"),
                     "--hammer", "3", *(["--hammer-tests", *tests] if tests else []), *extra])
    return code, calls, capsys.readouterr().out


def summary_of(tmp_path):
    return json.loads(next((tmp_path / "cache" / "runs").glob("*/summary.json")).read_text("utf-8"))


def test_the_hammer_runs_after_a_passing_job_in_its_clone_and_a_clean_hammer_exits_0(tmp_path, repo,
                                                                                    monkeypatch, capsys):
    code, calls, out = hammer_run(tmp_path, repo, monkeypatch, capsys)
    assert code == 0 and len(calls) == 1
    assert calls[0]["workspace_exists"] and calls[0]["targets"] == ["tests/unit/test_a.py"]
    assert calls[0]["count"] == 3 and not calls[0]["only"]
    assert "=== hammer (local addition)" in out and "HAMMER PASSED" in out and "j: PASSED" in out
    assert "hammer (local addition, not part of the CI job):" in out
    [run] = (tmp_path / "cache" / "runs").glob("*/r1")
    assert not (run / "workspace").exists()                    # a clean hammer cleans up like a clean job
    summary = summary_of(tmp_path)
    assert summary["hammer"]["requested"] == 3 and summary["hammer"]["failures"] == {}
    assert summary["hammer"]["tests"] == ["tests/unit/test_a.py"]


def test_a_failing_hammer_exits_1_names_the_hammer_and_keeps_the_clone(tmp_path, repo, monkeypatch, capsys):
    failing = cil.HammerResult(3, completed=3, tests_run=4, failures={
        "tests/unit/test_a.py::test_x": [cil.HammerFailure(2, "AssertionError: boom")]})
    code, calls, out = hammer_run(tmp_path, repo, monkeypatch, capsys, fake=failing)
    assert code == 1 and len(calls) == 1
    assert "tests/unit/test_a.py::test_x: 1/3 failed" in out
    assert "first failure (repetition 2): AssertionError: boom" in out
    assert "j: FAILED (the hammer, not the job's steps)" in out
    [run] = (tmp_path / "cache" / "runs").glob("*/r1")
    assert (run / "workspace").exists()
    summary = summary_of(tmp_path)
    assert summary["exit_code"] == 1
    assert summary["hammer"]["failures"]["tests/unit/test_a.py::test_x"][0]["repetition"] == 2


def test_the_hammer_is_skipped_when_the_jobs_steps_failed(tmp_path, repo, monkeypatch, capsys):
    code, calls, out = hammer_run(tmp_path, repo, monkeypatch, capsys, tail="exit 1")
    assert code == 1 and calls == []
    assert "skipped: the job's steps failed" in out and "=== hammer (local addition)" not in out
    assert "j: FAILED" in out and "the hammer, not" not in out


def test_hammer_only_skips_the_jobs_test_step_but_still_sets_up_and_hammers(tmp_path, repo, monkeypatch, capsys):
    code, calls, out = hammer_run(tmp_path, repo, monkeypatch, capsys, tail="exit 1", extra=["--hammer-only"])
    assert code == 0 and len(calls) == 1 and calls[0]["only"]            # the failing step never ran
    assert "--hammer-only: the hammer runs the tests instead" in out
    assert "1/2 Run actions/checkout" in out and calls[0]["workspace_exists"]    # the checkout still happened


def test_hammer_only_with_nothing_selected_exits_4_before_any_setup(tmp_path, repo, monkeypatch, capsys):
    code, calls, out = hammer_run(tmp_path, repo, monkeypatch, capsys, tail="exit 1", tests=(),
                                  extra=["--hammer-only", "--base", "HEAD"])
    assert code == cil.EXIT_NOTHING_TESTED == 4 and calls == []
    assert "PASSED" not in out and "=== hammer" not in out
    assert not (tmp_path / "cache" / "runs").exists()                  # no clone, no tool setup
    # the message reaches the user and names the way out
    code = cil.main(["--workflow", str(hammer_workflow(tmp_path)), "--repo", str(repo), "--job", "j",
                     "--trust-actions", "--stress", "0", "--ref", "HEAD", "--cache-dir", str(tmp_path / "cache"),
                     "--hammer-only", "--base", "HEAD"])
    err = capsys.readouterr().err
    assert code == 4 and "nothing was tested" in err
    assert "--hammer-tests" in err and "--base" in err and "drop --hammer-only" in err


def test_a_plain_hammer_with_nothing_selected_says_so_keeps_the_jobs_verdict_and_cleans_up(
        tmp_path, repo, monkeypatch, capsys):
    code, calls, out = hammer_run(tmp_path, repo, monkeypatch, capsys, tests=(), extra=["--base", "HEAD"])
    assert code == 0 and calls == []
    assert "skipped: no tests were selected for the hammer" in out and "job's own verdict stands" in out
    assert "j: PASSED" in out and "HAMMER PASSED" not in out
    [run] = (tmp_path / "cache" / "runs").glob("*/r1")
    assert not (run / "workspace").exists() and not (run / "temp").exists()       # the kept clone is dropped
    assert summary_of(tmp_path)["hammer"]["skipped"].startswith("no tests were selected")


def test_a_skipped_hammer_after_a_failed_job_keeps_the_clone_and_with_keep_so_does_a_clean_one(
        tmp_path, repo, monkeypatch, capsys):
    code, _, _ = hammer_run(tmp_path, repo, monkeypatch, capsys, tail="exit 1", tests=(), extra=["--base", "HEAD"])
    [run] = (tmp_path / "cache" / "runs").glob("*/r1")
    assert code == 1 and (run / "workspace").exists()
    kept = tmp_path / "kept"
    kept.mkdir()
    code, _, _ = hammer_run(kept, repo, monkeypatch, capsys, tests=(), extra=["--base", "HEAD", "--keep"])
    [run] = (kept / "cache" / "runs").glob("*/r1")
    assert code == 0 and (run / "workspace").exists()


def test_the_hammer_takes_its_command_from_the_workflows_test_step():
    step = cil.find_test_step(real_plan("unit", "Windows"))
    assert cil.runner_command(step) == ["uv", "run", "--locked", "--script", "tests/run.py"]
    multi = cil.PlannedStep(1, "t", "run", "run", "s", command="echo hi\nuv run --script 'tests/run.py' parity/ -q")
    assert cil.runner_command(multi) == ["uv", "run", "--script", "tests/run.py"]
    with pytest.raises(cil.RunnerError, match="could not find tests/run.py"):
        cil.runner_command(cil.PlannedStep(1, "t", "run", "run", "s", command="echo hi"))
