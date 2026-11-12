#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     # Reads .github/workflows/ci.yml. tests/run.py declares the same specifier, because
#     # tests/unit/test_ci_local.py imports this file (test_declared_dependencies.py pins it).
#     "pyyaml>=6.0.2",
# ]
# ///
"""Run one job of .github/workflows/ci.yml on this machine, the way GitHub runs it.

    uv run --script scripts/ci_local.py --ref HEAD --hammer   # before a push: the job, then the touched tests x20
    uv run --script scripts/ci_local.py --ref HEAD       # the job for this OS, on the commit a push sends
    uv run --script scripts/ci_local.py                  # the same, on the working tree
    uv run --script scripts/ci_local.py --job unit-macos --repeat 2
    uv run --script scripts/ci_local.py --job static
    uv run --script scripts/ci_local.py --dry-run --job build    # the plan, nothing runs
    uv run --script scripts/ci_local.py --list

The workflow file is the only source of versions, environment and commands; none of them is
copied into this script. The runner models what ci.yml uses and stops at the step's name on
anything else (a new action, key, shell or expression), so extending the workflow means
extending this file, and tests/unit/test_ci_local.py fails first. Each `uses:` step is emulated:

  actions/checkout     a fresh shallow clone of the tested source in a temporary directory,
                       made with core.autocrlf set on the command line. Your global git config
                       is never touched. The source is a commit (--ref HEAD is what a push
                       sends) or the working tree, which also holds untracked files. The runner
                       lists those before it starts, because a push would not carry them. A
                       temporary index and object directory keep your index, stash, refs and
                       object database as they were.
  astral-sh/setup-uv   the uv release the workflow names, downloaded from the official release
                       and checked against its published sha256, with the variables and PATH
                       entries setup-uv sets (SETUP_ENV).
  actions/setup-node   the Node release the workflow names, checked against SHASUMS256.txt and
                       put first on PATH.
  actions/upload-artifact  the matched files are copied into the run directory.

Each emulation was checked against one commit of its action (ACTIONS). When the workflow pins
another commit, the runner stops with exit code 2, names the action and what to re-check, unless
--trust-actions is given.

A `run:` step uses the runner's default shell (bash -e on Linux and macOS, pwsh on Windows) and
an environment built from an allowlist: the few variables a hosted runner has (PATH, HOME, the
temp directory, the Windows system variables), the workflow's and the step's `env:`, and what
setup-uv sets. Nothing else from your shell reaches the job. A step that changes the machine
(tzutil, git config --global) is never run: git's autocrlf becomes a clone option and a setting
in the job's own global git config, a timezone is checked (Windows) or set through TZ
(elsewhere), and any other command of that kind stops the run.

Jobs written for another OS run anyway where their commands are portable. A step that needs
something this host lacks (jq, a Linux-only tool) is listed as not reproducible here and the
exit code says so; --allow-unavailable "<step name>" accepts one such step by name.

--stress auto|N (on by default) starts busy-loop processes for the whole job. auto is this
host's logical cores minus the hosted runner's vCPUs. They run at the job's own priority, so
the job competes with them for cores. That does not make local timing match the hosted runner:
on a Mac mini, a flake that failed 1 run in 12 failed 1 in 12 with no stress, with 5 busy loops
and with 16. On Apple Silicon they tend to take the performance cores and leave the job slower
ones than an M1 vCPU has. --stress-priority low (nice 19, BELOW_NORMAL on Windows) makes them
give way instead and models nothing. Neither imitates a slower CPU, less memory or a VM.
--repeat N runs the job N times from fresh clones and fails on any failure.

--hammer [N] is what finds a flake: a local addition, not part of the CI job. When every step of
the job has passed, it runs the tests the change touches N times (default 20) in the same clone,
with the same tools, environment and stress, and reports failures per test. The tests are the unit
test files that changed since the merge base with origin/main (--base moves it; the working tree
counts its uncommitted and untracked files), plus the unit test files that name a changed module
under server/, launcher/ or shared-py/, import a changed test-side module (tests/wait_helper.py and
the like) or sit under a changed conftest.py (all textual matches, not an import graph). The cap
is 8 files (--hammer-max-files) and keeps the most relevant: changed test files first, the most
changed lines first, then the others. The printout says why each was picked and names what the
cap dropped. --hammer-tests FILE_OR_NODE ... names them instead. --hammer-only skips the job's own
test step to re-check a fix; it is not a pre-push run, and it exits 4 when nothing is selected.

Exit code: 0 every step passed in every repeat; 1 a step failed, or a hammer repetition did; 2 the
runner could not set up (unsupported workflow, changed action pin, download, git); 3 everything
that could run passed, but a step that cannot run on this host was not named in --allow-unavailable;
4 --hammer-only selected no tests, so nothing was tested (a plain --hammer with no tests says so and
leaves the job's own verdict alone); 130 or 128+N interrupted (a signal ends the run the way Ctrl-C does and stops everything it started).
Nothing here binds a port or reads the BrowserPlug state directory.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import dataclasses
import glob
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class UnsupportedWorkflow(Exception):
    """The workflow uses something this runner cannot reproduce faithfully."""


class ActionPinChanged(UnsupportedWorkflow):
    """The workflow pins an action to a commit its emulation was not checked against."""


class RunnerError(Exception):
    """The runner could not prepare or start a job (download, git, a missing tool)."""


class NothingTested(Exception):
    """--hammer-only selected no tests, and it skips the job's own test step: nothing would run."""


class Terminated(KeyboardInterrupt):
    """SIGTERM, SIGHUP or SIGBREAK reached the runner; it unwinds like a Ctrl-C."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


# ---------------------------------------------------------------------------------------
# Runners and hosts
# ---------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class RunnerSpec:
    label: str
    os: str          # GitHub's spelling: Linux, Windows, macOS
    arch: str        # X64 or ARM64
    vcpus: int


# vCPU counts of the standard hosted runners for a public repository ("About GitHub-hosted
# runners", docs.github.com/en/actions/concepts/runners/github-hosted-runners). A private
# repository gets 2 on Linux and Windows. macos-15 is arm64 with 3 vCPU and 7 GB: the log of
# run 42915903876 names the image macos-15-arm64, and .tmp/work/macos/ci-failure-2026-11-12.md
# records the Apple M1 VM. One row per label ci.yml uses; any other label stops the run.
_RUNNER_ROWS = (
    RunnerSpec("ubuntu-latest", "Linux", "X64", 4),
    RunnerSpec("windows-latest", "Windows", "X64", 4),
    RunnerSpec("macos-15", "macOS", "ARM64", 3),
)
RUNNERS = {row.label: row for row in _RUNNER_ROWS}


def runner_for(label: object) -> RunnerSpec:
    if not isinstance(label, str) or label not in RUNNERS:
        raise UnsupportedWorkflow(
            f"runs-on {label!r} is not a runner label this script knows "
            f"(known: {', '.join(sorted(RUNNERS))}); add it to _RUNNER_ROWS with its vCPU count")
    return RUNNERS[label]


@dataclasses.dataclass(frozen=True)
class Host:
    os: str
    arch: str
    cores: int
    which: Callable[[str], str | None] = shutil.which


def detect_host() -> Host:
    names = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}
    system = platform.system()
    if system not in names:
        raise RunnerError(f"unsupported host OS {system!r}")
    arch = "ARM64" if platform.machine().lower() in {"arm64", "aarch64"} else "X64"
    return Host(names[system], arch, os.cpu_count() or 1)


# ---------------------------------------------------------------------------------------
# The workflow file
# ---------------------------------------------------------------------------------------

# Every key this runner understands. A key outside these sets changes behavior in a way the
# runner does not model, so it stops the run instead of being ignored.
_WORKFLOW_KEYS = {"name", "on", True, "permissions", "concurrency", "env", "jobs"}
_JOB_KEYS = {"name", "runs-on", "timeout-minutes", "permissions", "steps", "needs", "if"}
_STEP_KEYS = {"name", "uses", "with", "run", "env", "if"}


@dataclasses.dataclass(frozen=True)
class Step:
    index: int
    name: str
    uses: str | None
    run: str | None
    with_: Mapping[str, str]
    env: Mapping[str, str]
    condition: str | None


@dataclasses.dataclass(frozen=True)
class Job:
    id: str
    name: str
    runs_on: str
    timeout_minutes: float | None
    steps: tuple[Step, ...]
    condition: str | None
    needs: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Workflow:
    path: str
    env: Mapping[str, str]
    jobs: Mapping[str, Job]
    inputs: Mapping[str, object]


def _scalar(value: object, where: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, float):
        raise UnsupportedWorkflow(f"{where}: YAML read {value!r} as a number; quote the value")
    raise UnsupportedWorkflow(f"{where}: expected a plain value, found {type(value).__name__}")


def _mapping(raw: object, where: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise UnsupportedWorkflow(f"{where}: expected a mapping")
    return raw


def _string_map(raw: object, where: str) -> dict[str, str]:
    return {str(k): _scalar(v, f"{where}.{k}") for k, v in _mapping(raw, where).items()}


def _check_keys(raw: dict, allowed: set, where: str) -> None:
    for key in raw:
        if key not in allowed:
            raise UnsupportedWorkflow(
                f"{where}: key {key!r} is not one this runner models; teach scripts/ci_local.py "
                "about it, or remove it")


def _minutes(raw: object, where: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise UnsupportedWorkflow(f"{where}: expected a number of minutes, found {raw!r}")
    return float(raw)


def _parse_step(index: int, raw: object, job_id: str) -> Step:
    where = f"job {job_id!r}, step {index}"
    data = _mapping(raw, where)
    _check_keys(data, _STEP_KEYS, where)
    uses, run = data.get("uses"), data.get("run")
    if (uses is None) == (run is None):
        raise UnsupportedWorkflow(f"{where}: a step needs exactly one of uses and run")
    if run is not None:
        text = str(run).strip()
        default_name = f"Run {text.splitlines()[0] if text else ''}"
    else:
        default_name = f"Run {uses}"
    return Step(
        index=index,
        name=str(data.get("name") or default_name),
        uses=str(uses) if uses is not None else None,
        run=str(run) if run is not None else None,
        with_=_string_map(data.get("with"), f"{where}.with"),
        env=_string_map(data.get("env"), f"{where}.env"),
        condition=None if data.get("if") is None else _scalar(data["if"], f"{where}.if"),
    )


def _parse_job(job_id: str, raw: object) -> Job:
    where = f"job {job_id!r}"
    data = _mapping(raw, where)
    _check_keys(data, _JOB_KEYS, where)
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise UnsupportedWorkflow(f"{where}: no steps")
    needs = data.get("needs") or []
    needs = [needs] if isinstance(needs, str) else list(needs)
    return Job(
        id=job_id,
        name=str(data.get("name") or job_id),
        runs_on=data.get("runs-on"),
        timeout_minutes=_minutes(data.get("timeout-minutes"), f"{where}.timeout-minutes"),
        steps=tuple(_parse_step(i, s, job_id) for i, s in enumerate(steps, 1)),
        condition=None if data.get("if") is None else _scalar(data["if"], f"{where}.if"),
        needs=tuple(str(n) for n in needs),
    )


def _declared_inputs(on: object) -> dict[str, object]:
    """Default values of the workflow_call inputs."""
    block = on.get("workflow_call") if isinstance(on, dict) else None
    found: dict[str, object] = {}
    if not isinstance(block, dict):
        return found
    for name, spec in _mapping(block.get("inputs"), "on.workflow_call.inputs").items():
        spec = _mapping(spec, f"on.workflow_call.inputs.{name}")
        default = spec.get("default")
        if default is None:
            default = False if spec.get("type") == "boolean" else ""
        found[str(name)] = default
    return found


def parse_workflow(text: str, source: str = "ci.yml") -> Workflow:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise UnsupportedWorkflow(f"{source}: not a mapping")
    _check_keys(data, _WORKFLOW_KEYS, source)
    jobs_raw = _mapping(data.get("jobs"), f"{source} jobs")
    if not jobs_raw:
        raise UnsupportedWorkflow(f"{source}: no jobs")
    # YAML 1.1 reads the key `on` as the boolean True.
    on = data.get("on", data.get(True))
    return Workflow(
        path=source,
        env=_string_map(data.get("env"), "env"),
        jobs={str(k): _parse_job(str(k), v) for k, v in jobs_raw.items()},
        inputs=_declared_inputs(on),
    )


def load_workflow(path: Path = DEFAULT_WORKFLOW) -> Workflow:
    return parse_workflow(Path(path).read_text(encoding="utf-8"), str(path))


# ---------------------------------------------------------------------------------------
# ${{ }} and if: (only what ci.yml uses; anything else stops the run)
# ---------------------------------------------------------------------------------------

_TRIVIAL_JOB_IF = {"true", "always()", "success()"}
_INPUT_IF = re.compile(r"(!?)\s*inputs\.([A-Za-z_][\w-]*)")


def _unwrap(text: str) -> str:
    m = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", text, re.S)
    return (m.group(1) if m else text).strip()


def check_job_condition(job: Job) -> None:
    """A job-level `if:` is never evaluated here, so only one that is always true may stand."""
    if job.condition is not None and _unwrap(job.condition) not in _TRIVIAL_JOB_IF:
        raise UnsupportedWorkflow(
            f"job {job.id!r}: if: {job.condition!r} is not evaluated by this runner; only "
            f"{', '.join(sorted(_TRIVIAL_JOB_IF))} are accepted")


def step_condition_holds(text: str, inputs: Mapping[str, object], where: str) -> bool:
    """The one step condition ci.yml uses: [!]inputs.NAME."""
    m = _INPUT_IF.fullmatch(_unwrap(text))
    if m is None:
        raise UnsupportedWorkflow(
            f"{where}: if: {text!r} is not modelled; scripts/ci_local.py reads only [!]inputs.NAME")
    if m.group(2) not in inputs:
        raise UnsupportedWorkflow(f"{where}: unknown input {m.group(2)!r} in if: {text!r}")
    return (inputs[m.group(2)] not in (None, False, 0, "")) != bool(m.group(1))


def _no_expressions(text: str, where: str) -> None:
    found = re.search(r"\$\{\{.*?\}\}", text, re.S)
    if found:
        raise UnsupportedWorkflow(
            f"{where}: the expression {found.group(0)!r} is not modelled; scripts/ci_local.py "
            "evaluates expressions only in if:")


# ---------------------------------------------------------------------------------------
# What a job would do: the plan
# ---------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ActionSpec:
    kind: str
    with_keys: frozenset[str]
    # Commits of the action the emulation was checked against. The workflow pins by commit, so
    # a Dependabot bump lands here as an unknown SHA and the runner asks for a re-check.
    checked: frozenset[str] = frozenset()
    recheck: str = ""


ACTIONS: dict[str, ActionSpec] = {
    # checkout v7.0.1, setup-uv v10.3.3 and setup-node v7.1.0 were read against the hosted macOS
    # log (.tmp/logs/macos/gh-run-42915903876-macos.log). No log of upload-artifact is kept; its
    # emulation covers name, path globs and if-no-files-found only.
    "actions/checkout": ActionSpec(
        "checkout", frozenset({"persist-credentials", "fetch-depth"}),
        frozenset({"3d3c42e5aac5ba805825da76410c181273ba90b1"}),
        "the clone it makes: fetch-depth, persist-credentials and how it treats line endings, "
        "from the action's release notes"),
    "astral-sh/setup-uv": ActionSpec(
        "setup-uv", frozenset({"version", "python-version"}),
        frozenset({"68559e99cc807c1eb7e9238fd43a15bba460eec2"}),
        "the variables and PATH entries it sets, from its release notes or the 'Set UV_...' and "
        "'Added ... to the path' lines of a CI log, against SETUP_ENV"),
    "actions/setup-node": ActionSpec(
        "setup-node", frozenset({"node-version", "package-manager-cache"}),
        frozenset({"949feb2413d6458794dcd2491c4babbbce0c15c1"}),
        "the PATH entry and any variable it sets, from its release notes or the env: block "
        "of the step after it in a CI log, against SETUP_ENV"),
    "actions/upload-artifact": ActionSpec(
        "upload-artifact", frozenset({"name", "path", "if-no-files-found", "retention-days"}),
        frozenset({"cf430e030ddbb5b0abf93d22962f4752f3646cd9"}),
        "how it matches path globs and what if-no-files-found does, from its release notes"),
    # The aggregator of the workflow's own job results has nothing to run on one machine.
    "re-actors/alls-green": ActionSpec("aggregator", frozenset({"jobs"})),
}

# What the emulated setup actions put into the job's environment: variable names per action,
# from the hosted macOS job (.tmp/logs/macos/gh-run-42915903876-macos.log):
#   189  Set UV_PYTHON_INSTALL_DIR to /Users/runner/work/_temp/uv-python-dir
#   191  Set UV_PYTHON to 3.12                       (only when python-version is given)
#   192  Set UV_CACHE_DIR to /Users/runner/work/_temp/setup-uv-cache
#   232-239  the env: block of the test step lists these three, the workflow's two and the
#            step's two, and no variable from setup-node
# PATH gets, in this order (lines 187, 188, 190 and the setup-node tool-cache directory): uv's
# tool bin directory (an empty directory stands in for ~/.local/bin, so your own tools in it
# stay out), the uv release directory, the Python install directory, then node's directory.
# setup-uv does not set UV_TOOL_BIN_DIR or UV_TOOL_DIR.
SETUP_ENV: dict[str, tuple[str, ...]] = {
    "astral-sh/setup-uv": ("UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "UV_PYTHON"),
    "actions/setup-node": (),
}

# Commands that change the machine beyond the job's own directories. The two the workflow
# uses today are recognised exactly; any other hit stops the run before it can touch anything.
_AUTOCRLF_STEP = re.compile(r"^\s*git\s+config\s+--global\s+core\.autocrlf\s+(true|false|input)\s*$")
_TIMEZONE_STEP = re.compile(r'^\s*tzutil\s+/s\s+"?([^"]+?)"?\s*$', re.I)
_MACHINE_WIDE = (
    # Reading a value (`git config --global user.name`) is fine; setting or editing one is not.
    (r"\bgit\s+config\s+--(global|system)\s+(--(add|unset|unset-all|replace-all|edit)\b|-e\b"
     r"|(?!-)[\w.-]+[ \t]+[^\s;&|#)])", "changes git's global or system configuration"),
    (r"\btzutil\b", "changes the system timezone"),
    (r"\bsetx\b|\breg(\.exe)?\s+(add|delete|import)\b", "writes the Windows environment or registry"),
    (r"\bsudo\b", "runs as root"),
    (r"\b(apt(-get)?|dpkg|snap|brew|choco(latey)?|winget|scoop)\s+(-i\s+)?"
     r"(install|uninstall|remove|update|upgrade|tap|link)\b", "installs system packages"),
    (r"\bnpm\s+(install|i|add|uninstall)\b[^\n|;&]*\s(-g|--global)\b", "installs a global npm package"),
    (r"\bpip3?\s+install\b|\bpython3?\s+-m\s+pip\s+install\b|\buv\s+pip\s+install\s+--system\b",
     "installs into the host's Python"),
    (r"\bdefaults\s+write\b|\bsystemsetup\b|\bscutil\b|\bnetsh\b|\bSet-ExecutionPolicy\b",
     "changes system settings"),
)
# No step of ci.yml writes a runner file command, and none is provided, so a reference stops the run.
_RUNNER_FILES = re.compile(r"\bGITHUB_(ENV|PATH|OUTPUT|STATE|STEP_SUMMARY)\b")
# Tools a run step may need that a developer machine often lacks. uv, node, npm and npx are
# not here: the job's own setup steps provide them.
_PORTABLE_TOOLS = ("jq", "gh", "docker", "make", "zip", "unzip")
# Commands that start on a host and still cannot work there. Each row is something a local run
# showed: (host OS, pattern over the command, why).
_HOST_LIMITS = (
    ("Windows", r"\bactionlint\b",
     "actionlint-py publishes no Windows wheel, and building it from source fails on Windows"),
)
# The one Windows zone the workflow sets, for hosts that can only express it through TZ.
_WINDOWS_ZONES = {
    "Central European Standard Time": "Europe/Warsaw",
    "W. Europe Standard Time": "Europe/Berlin",
    "UTC": "UTC",
}

DEFAULT_STRESS = "auto"
DEFAULT_PRIORITY = "normal"


@dataclasses.dataclass
class PlannedStep:
    index: int
    name: str
    kind: str                 # checkout, setup-uv, setup-node, upload-artifact, run, git-autocrlf,
    #                           timezone, skipped, aggregator
    status: str               # run, emulate, skip, unavailable
    summary: str
    params: dict = dataclasses.field(default_factory=dict)
    command: str | None = None
    shell: str | None = None
    env: dict = dataclasses.field(default_factory=dict)
    reason: str = ""


@dataclasses.dataclass
class Plan:
    workflow: str
    job_id: str
    name: str
    runner: RunnerSpec
    host: Host
    timeout_minutes: float | None
    env: dict
    steps: list[PlannedStep]
    inputs: dict
    stress: int
    aggregator: bool = False
    notes: list[str] = dataclasses.field(default_factory=list)
    stress_priority: str = DEFAULT_PRIORITY


def is_aggregator(job: Job) -> bool:
    return all(s.uses and ACTIONS.get(s.uses.partition("@")[0], ActionSpec("", frozenset())).kind
               == "aggregator" for s in job.steps)


def default_job(workflow: Workflow, host: Host) -> str:
    """The first job that is not an aggregator and whose runner matches this host's OS."""
    for job in workflow.jobs.values():
        if is_aggregator(job):
            continue
        with contextlib.suppress(UnsupportedWorkflow):
            if runner_for(job.runs_on).os == host.os:
                return job.id
    raise RunnerError(f"no job of {workflow.path} targets {host.os}; pick one with --job")


def resolve_stress(spec: object, host: Host, runner: RunnerSpec) -> int:
    text = str(spec).strip().lower()
    if text in {"", "0", "off", "none"}:
        return 0
    if text == "auto":
        return max(0, host.cores - runner.vcpus)
    if not text.isdigit():
        raise RunnerError(f"--stress takes auto or a number, not {spec!r}")
    count = int(text)
    if count > host.cores * 4:
        raise RunnerError(f"--stress {count} is more than four times this host's {host.cores} cores")
    return count


@dataclasses.dataclass(frozen=True)
class ShellPlan:
    name: str                 # bash, pwsh, powershell
    display: str              # what GitHub would show
    exe: str | None           # resolved executable, None when missing on this host
    args: tuple[str, ...]     # argument template; {0} is the script path
    suffix: str
    note: str = ""


def find_git_bash(host: Host) -> str | None:
    """On Windows, Git for Windows' bash; the one on PATH may be WSL's launcher."""
    if host.os != "Windows":
        return "/bin/bash" if Path("/bin/bash").exists() else host.which("bash")
    git_exe = host.which("git")
    if git_exe:
        for parent in Path(git_exe).resolve().parents:
            candidate = parent / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return None


def resolve_shell(runner_os: str, host: Host) -> ShellPlan:
    """The shell a step gets when it names none: bash -e, or pwsh on a Windows runner."""
    if runner_os != "Windows":
        return ShellPlan("bash", "bash -e {0}", find_git_bash(host), ("-e", "{0}"), ".sh")
    # GitHub runs `pwsh -command ". '{0}'"`. Windows PowerShell stands in when pwsh is missing.
    args = ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ". '{0}'")
    display = "pwsh -command \". '{0}'\""
    pwsh = host.which("pwsh")
    if pwsh:
        return ShellPlan("pwsh", display, pwsh, args, ".ps1")
    legacy = host.which("powershell")
    if legacy:
        return ShellPlan("powershell", display, legacy, args, ".ps1",
                         "pwsh is not installed here; Windows PowerShell 5.1 stands in for it")
    return ShellPlan("pwsh", display, None, args, ".ps1")


def _missing_tool(command: str, host: Host) -> str:
    for tool in _PORTABLE_TOOLS:
        if re.search(rf"(?<![\w./-]){re.escape(tool)}(?![\w-])", command) and not host.which(tool):
            return f"{tool} is not on PATH"
    return ""


def _exact_version(value: str, where: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise UnsupportedWorkflow(
            f"{where}: {value!r} is not an exact X.Y.Z version; this runner downloads one release")
    return value


def _plan_uses(job: Job, step: Step, state: dict, trust_actions: bool) -> PlannedStep:
    where = f"job {job.id!r}, step {step.index} ({step.name!r})"
    action, _, ref = step.uses.partition("@")
    spec = ACTIONS.get(action)
    if spec is None:
        raise UnsupportedWorkflow(
            f"{where}: uses {step.uses!r}, an action scripts/ci_local.py cannot reproduce; "
            "teach it the action (ACTIONS, and a handler in JobExecutor) before the workflow uses it")
    extra = set(step.with_) - spec.with_keys
    if extra:
        raise UnsupportedWorkflow(
            f"{where}: {action} with {sorted(extra)} changes behavior this runner does not model")
    if spec.kind != "aggregator":
        for value in step.with_.values():
            _no_expressions(value, where)
        if ref not in spec.checked and not trust_actions:
            raise ActionPinChanged(
                f"{where}: {action} is pinned to {ref!r}, and its emulation was checked against "
                f"{', '.join(sorted(spec.checked))}. A new pin can change what the action sets. "
                f"Re-check {spec.recheck}; then add the commit to ACTIONS[{action!r}].checked in "
                "scripts/ci_local.py, or pass --trust-actions to run without the check.")
    with_ = dict(step.with_)
    pin = f"{action}@{ref[:7]}" if re.fullmatch(r"[0-9a-f]{40}", ref) else step.uses
    base = dict(index=step.index, name=step.name, kind=spec.kind, status="emulate")
    if spec.kind == "checkout":
        depth = with_.get("fetch-depth", "1")
        if not depth.isdigit():
            raise UnsupportedWorkflow(f"{where}: fetch-depth {depth!r} is not a number")
        autocrlf = state["autocrlf"]
        shallow = "shallow clone (depth %s)" % depth if int(depth) else "full clone"
        return PlannedStep(**base, params={"fetch_depth": int(depth), "autocrlf": autocrlf},
                           summary=f"{pin}: fresh {shallow} of the tested source, "
                                   f"core.autocrlf={autocrlf} on the command line")
    if spec.kind == "setup-uv":
        version = _exact_version(with_.get("version", ""), f"{where}: version")
        python = with_.get("python-version", "")
        return PlannedStep(**base, params={"version": version, "python_version": python},
                           summary=f"{pin}: uv {version} from the official release, sha256 checked"
                                   + (f"; UV_PYTHON={python}" if python else "")
                                   + "; fresh UV_PYTHON_INSTALL_DIR and UV_CACHE_DIR")
    if spec.kind == "setup-node":
        version = _exact_version(with_.get("node-version", ""), f"{where}: node-version")
        if with_.get("package-manager-cache", "false") != "false":
            raise UnsupportedWorkflow(f"{where}: package-manager-cache other than false is not modelled")
        return PlannedStep(**base, params={"version": version},
                           summary=f"{pin}: Node {version} from nodejs.org, checked against "
                                   "SHASUMS256.txt, first on PATH")
    if spec.kind == "upload-artifact":
        patterns = [ln.strip() for ln in with_.get("path", "").splitlines() if ln.strip()]
        if not patterns or any(p.startswith("!") for p in patterns):
            raise UnsupportedWorkflow(f"{where}: path needs plain glob lines (no ! exclusions)")
        missing = with_.get("if-no-files-found", "warn")
        if missing not in {"warn", "error", "ignore"}:
            raise UnsupportedWorkflow(f"{where}: if-no-files-found {missing!r}")
        return PlannedStep(**base, params={"name": with_.get("name", "artifact"),
                                           "patterns": patterns, "if_no_files": missing},
                           summary=f"{pin}: copies {len(patterns)} glob(s) into the run directory "
                                   f"as {with_.get('name', 'artifact')!r} (missing files: {missing})")
    return PlannedStep(**{**base, "status": "skip"},
                       summary=f"{pin}: aggregates other jobs; nothing to run on one machine",
                       reason="aggregator")


def _plan_run(job: Job, step: Step, host: Host, runner: RunnerSpec, state: dict) -> PlannedStep:
    where = f"job {job.id!r}, step {step.index} ({step.name!r})"
    for text in (step.run, *step.env.values()):
        _no_expressions(text, where)
    command = step.run.strip("\n")
    base = dict(index=step.index, name=step.name, kind="run", command=command)
    if _RUNNER_FILES.search(command):
        raise UnsupportedWorkflow(
            f"{where}: the command uses GITHUB_ENV, GITHUB_PATH, GITHUB_OUTPUT or a similar runner "
            "file, which scripts/ci_local.py does not provide")
    m = _AUTOCRLF_STEP.match(command)
    if m:
        state["autocrlf"] = m.group(1)
        return PlannedStep(**{**base, "kind": "git-autocrlf"}, status="emulate",
                           params={"value": m.group(1)},
                           summary=f"git config --global core.autocrlf {m.group(1)}: not run; the job "
                                   "gets its own global git config and the clone the same option")
    m = _TIMEZONE_STEP.match(command)
    if m:
        zone = m.group(1)
        if host.os == "Windows":
            return PlannedStep(**{**base, "kind": "timezone"}, status="emulate", params={"zone": zone},
                               summary=f"tzutil /s {zone!r}: not run; the machine's zone is checked "
                                       "(tzutil /g) and the step fails if it differs")
        iana = _WINDOWS_ZONES.get(zone)
        if iana:
            return PlannedStep(**{**base, "kind": "timezone"}, status="emulate",
                               params={"zone": zone, "tz": iana},
                               summary=f"tzutil /s {zone!r}: not run; TZ={iana} is set for the steps "
                                       "that follow")
        return PlannedStep(**{**base, "kind": "timezone"}, status="unavailable",
                           params={"zone": zone},
                           summary=f"tzutil /s {zone!r}",
                           reason=f"no TZ equivalent known for the Windows zone {zone!r}")
    for pattern, why in _MACHINE_WIDE:
        if re.search(pattern, command, re.I):
            raise UnsupportedWorkflow(
                f"{where}: the command {why}. This runner will not change the machine; "
                "split the step or teach scripts/ci_local.py an emulation for it")
    shell = resolve_shell(runner.os, host)
    reason = _missing_tool(command, host)
    if not reason and shell.exe is None:
        reason = f"{shell.name} is not installed here"
    for limit_os, pattern, why in _HOST_LIMITS:
        if not reason and host.os == limit_os and re.search(pattern, command):
            reason = why
    return PlannedStep(**base, status="unavailable" if reason else "run", shell=shell.display,
                       env=dict(step.env), reason=reason,
                       summary=(command.splitlines()[0] + (" ..." if "\n" in command else "")),
                       params={"shell_note": shell.note})


def build_plan(workflow: Workflow, job_id: str, host: Host, *,
               inputs: Mapping[str, str] | None = None, stress: object = DEFAULT_STRESS,
               stress_priority: str = DEFAULT_PRIORITY, trust_actions: bool = False) -> Plan:
    if job_id not in workflow.jobs:
        raise RunnerError(f"{workflow.path} has no job {job_id!r} (jobs: {', '.join(workflow.jobs)})")
    job = workflow.jobs[job_id]
    runner = runner_for(job.runs_on)
    check_job_condition(job)
    aggregator = is_aggregator(job)
    if job.needs and not aggregator:
        raise UnsupportedWorkflow(
            f"job {job_id!r}: needs {list(job.needs)} is not modelled; this runner runs one job alone")
    typed = dict(workflow.inputs)
    for key, value in (inputs or {}).items():
        if key not in typed:
            raise RunnerError(f"--input {key}: the workflow declares no such input")
        typed[key] = (value.lower() == "true") if isinstance(typed[key], bool) else value
    env = {}
    if not aggregator:
        for key, value in workflow.env.items():
            _no_expressions(value, f"env.{key}")
        env = dict(workflow.env)
    state = {"autocrlf": "true" if runner.os == "Windows" else "false"}
    steps: list[PlannedStep] = []
    for step in job.steps:
        where = f"job {job_id!r}, step {step.index} ({step.name!r})"
        if step.condition is not None and not step_condition_holds(step.condition, typed, where):
            steps.append(PlannedStep(index=step.index, name=step.name, kind="skipped", status="skip",
                                     summary=step.uses or step.run.strip().splitlines()[0],
                                     reason=f"condition is false: {step.condition}"))
            continue
        steps.append(_plan_uses(job, step, state, trust_actions) if step.uses
                     else _plan_run(job, step, host, runner, state))
    notes: list[str] = []
    if host.os != runner.os:
        notes.append(f"{job_id} targets {runner.os} ({runner.label}); this host is {host.os}. A pass "
                     "shows the commands work here, not on the hosted image.")
    elif host.arch != runner.arch:
        notes.append(f"{runner.label} is {runner.arch}; this host is {host.arch}.")
    for planned in steps:
        note = planned.params.get("shell_note")
        if note and note not in notes:
            notes.append(note)
    return Plan(workflow=workflow.path, job_id=job_id, name=job.name, runner=runner, host=host,
                timeout_minutes=job.timeout_minutes, env=env, steps=steps, inputs=typed,
                stress=resolve_stress(stress, host, runner), aggregator=aggregator, notes=notes,
                stress_priority=stress_priority)


def format_header(plan: Plan, source: str = "", warnings: Sequence[str] = ()) -> list[str]:
    r, h = plan.runner, plan.host
    timeout = f", timeout {plan.timeout_minutes:g} min" if plan.timeout_minutes else ""
    lines = [f"Job {plan.job_id}: {plan.name}",
             f"  runs-on {r.label} ({r.os} {r.arch}, {r.vcpus} vCPU){timeout}",
             f"  host {h.os} {h.arch}, {h.cores} cores"]
    if source:
        lines.append(f"  source: {source}")
    lines.extend(warnings)
    if plan.stress:
        how = ("at low priority: they take idle cores and give way to the job, so the job keeps "
               "every core it asks for" if plan.stress_priority == "low"
               else "at the job's own priority: they compete with it for cores")
        lines.append(f"  stress: {plan.stress} busy-loop process(es) for the whole job, {how} "
                     f"({h.cores} cores here, {r.vcpus} on the runner)")
    else:
        lines.append(f"  stress: none; the job has all {h.cores} cores here, the runner has {r.vcpus}")
    for note in plan.notes:
        lines.append(f"  note: {note}")
    if plan.env:
        lines.append("  environment from the workflow: "
                     + ", ".join(f"{k}={v}" for k, v in plan.env.items()))
    if plan.aggregator:
        lines.append("  this job only aggregates the others; run them instead")
    return lines


def format_plan(plan: Plan, source: str = "", warnings: Sequence[str] = ()) -> list[str]:
    lines = format_header(plan, source, warnings)
    lines.append("  steps:")
    for s in plan.steps:
        tag = {"run": "run", "emulate": "emulated", "skip": "skipped",
               "unavailable": "NOT REPRODUCIBLE HERE"}[s.status]
        lines.append(f"   {s.index:>2}. [{tag}] {s.name}")
        if s.kind == "run":
            lines.append(f"       shell: {s.shell}")
            if s.env:
                lines.append("       step env: " + ", ".join(f"{k}={v}" for k, v in s.env.items()))
            for ln in (s.command or "").splitlines():
                lines.append(f"       $ {ln}")
        else:
            lines.append(f"       {s.summary}")
        if s.reason:
            lines.append(f"       reason: {s.reason}")
    return lines


# ---------------------------------------------------------------------------------------
# Git: the source under test
# ---------------------------------------------------------------------------------------

_IDENTITY = {"GIT_AUTHOR_NAME": "ci-local", "GIT_AUTHOR_EMAIL": "ci-local@localhost",
             "GIT_COMMITTER_NAME": "ci-local", "GIT_COMMITTER_EMAIL": "ci-local@localhost",
             "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}


def git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The caller's environment without its GIT_* variables (a pre-push hook sets several)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    env.update(extra or {})
    return env


def git(args: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, env=git_env(env), capture_output=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise RunnerError("git is not on PATH") from exc
    if proc.returncode:
        raise RunnerError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


@dataclasses.dataclass
class Source:
    stage_url: str            # file:// URL of the staging repository the clone reads
    commit: str
    base: str                 # the commit the working tree was built on
    description: str
    changes: list[tuple[str, str]]
    untracked: list[str] = dataclasses.field(default_factory=list)


def worktree_summary(repo: Path, ref: str | None = None) -> str:
    """One line about what would be tested, from read-only git calls."""
    head = git(["rev-parse", "--verify", f"{ref or 'HEAD'}^{{commit}}"], cwd=repo).strip()
    if ref:
        return f"commit {ref} ({head[:7]})"
    porcelain = git(["status", "--porcelain=v1", "-z"], cwd=repo)
    entries = [e for e in porcelain.split("\0") if e]
    untracked = sum(1 for e in entries if e.startswith("??"))
    changed = sum(1 for e in entries if not e.startswith("??") and not e.startswith("!!"))
    return (f"working tree on {head[:7]}: {changed} changed or deleted, {untracked} untracked"
            if entries else f"HEAD {head[:7]} (no uncommitted changes)")


def untracked_files(repo: Path) -> list[str]:
    """Files git does not track and .gitignore does not hide, from a read-only call."""
    toplevel = Path(git(["rev-parse", "--show-toplevel"], cwd=repo).strip())
    raw = git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=toplevel)
    return sorted(name for name in raw.split("\0") if name)


def untracked_notice(files: Sequence[str], limit: int = 15) -> list[str]:
    """The lines that tell the user a working-tree run holds files a push would leave out."""
    if not files:
        return []
    lines = [f"!! {len(files)} UNTRACKED file(s) are part of this run. A push does not carry them, "
             "so CI will not have them:"]
    lines += [f"!!   {name}" for name in files[:limit]]
    if len(files) > limit:
        lines.append(f"!!   ... and {len(files) - limit} more")
    lines.append("!! Test exactly what a push sends with --ref HEAD, or commit these files first.")
    return lines


def _objects_dir(repo: Path) -> Path:
    path = Path(git(["rev-parse", "--git-path", "objects"], cwd=repo).strip())
    return path if path.is_absolute() else (repo / path).resolve()


def prepare_source(repo: Path, work: Path, ref: str | None = None) -> Source:
    """Build the commit to test and a staging repository a shallow clone can read.

    The working tree goes through a temporary index (GIT_INDEX_FILE) and a temporary object
    directory that borrows the real one as an alternate, so the user's index, refs, stash and
    object database are untouched. `git add -A` applies .gitignore and the user's autocrlf
    rules, so the tree is what a commit of the working tree would contain.
    """
    toplevel = Path(git(["rev-parse", "--show-toplevel"], cwd=repo).strip())
    head = git(["rev-parse", "--verify", f"{ref or 'HEAD'}^{{commit}}"], cwd=toplevel).strip()
    real_objects = _objects_dir(toplevel)
    work.mkdir(parents=True, exist_ok=True)
    objects = work / "stage-objects"
    objects.mkdir()
    env = {"GIT_OBJECT_DIRECTORY": str(objects), "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(real_objects)}
    commit, changes, untracked = head, [], []
    description = f"commit {ref} ({head[:7]})" if ref else f"HEAD {head[:7]} (no uncommitted changes)"
    if ref is None:
        untracked = untracked_files(toplevel)
        env_index = {**env, "GIT_INDEX_FILE": str(work / "stage-index")}
        git(["read-tree", head], cwd=toplevel, env=env_index)
        git(["add", "-A"], cwd=toplevel, env=env_index)
        tree = git(["write-tree"], cwd=toplevel, env=env_index).strip()
        if tree != git(["rev-parse", f"{head}^{{tree}}"], cwd=toplevel).strip():
            commit = git(["commit-tree", tree, "-p", head, "-m", f"ci_local: working tree on {head[:7]}"],
                         cwd=toplevel, env={**env_index, **_IDENTITY}).strip()
            raw = git(["diff-tree", "-r", "--no-renames", "--name-status", "-z", head, commit],
                      cwd=toplevel, env=env).split("\0")
            changes = [(raw[i], raw[i + 1]) for i in range(0, len(raw) - 1, 2)]
            kinds = {s: sum(1 for c, _ in changes if c == s) for s in "MAD"}
            description = (f"working tree on {head[:7]}: {kinds['M']} modified, {kinds['A']} added or "
                           f"untracked, {kinds['D']} deleted")
    stage = work / "stage.git"
    git(["init", "--bare", "--quiet", str(stage)], cwd=toplevel)
    (stage / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (stage / "objects" / "info" / "alternates").write_text(
        f"{real_objects.as_posix()}\n{objects.as_posix()}\n", encoding="utf-8", newline="\n")
    git(["update-ref", "refs/heads/main", commit], cwd=stage)
    git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=stage)
    return Source(stage.resolve().as_uri(), commit, head, description, changes, untracked)


# ---------------------------------------------------------------------------------------
# Tools: the uv and Node releases the workflow names
# ---------------------------------------------------------------------------------------

_UV_TRIPLES = {
    ("Windows", "X64"): "x86_64-pc-windows-msvc", ("Windows", "ARM64"): "aarch64-pc-windows-msvc",
    ("macOS", "ARM64"): "aarch64-apple-darwin", ("macOS", "X64"): "x86_64-apple-darwin",
    ("Linux", "X64"): "x86_64-unknown-linux-gnu", ("Linux", "ARM64"): "aarch64-unknown-linux-gnu",
}
_NODE_PLATFORMS = {"Windows": ("win", "zip"), "macOS": ("darwin", "tar.gz"), "Linux": ("linux", "tar.gz")}


def uv_asset(host: Host, version: str) -> tuple[str, str]:
    """(archive name, download URL) of the standalone uv release for this host."""
    triple = _UV_TRIPLES.get((host.os, host.arch))
    if triple is None:
        raise RunnerError(f"no uv build is known for {host.os} {host.arch}")
    name = f"uv-{triple}.{'zip' if host.os == 'Windows' else 'tar.gz'}"
    return name, f"https://github.com/astral-sh/uv/releases/download/{version}/{name}"


def node_asset(host: Host, version: str) -> tuple[str, str]:
    """(archive name, download URL) of the official Node build for this host."""
    plat, ext = _NODE_PLATFORMS[host.os]
    name = f"node-v{version}-{plat}-{host.arch.lower()}.{ext}"
    return name, f"https://nodejs.org/dist/v{version}/{name}"


def parse_checksum(text: str, filename: str) -> str:
    """The sha256 for `filename` from a `<hash>  <name>` list, or a file holding one hash."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 1 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename \
                and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    raise RunnerError(f"no sha256 for {filename} in the published checksums")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": "browserplug-ci-local"})


def fetch_text(url: str) -> str:
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(_request(url), timeout=60) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise RunnerError(f"{url}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            time.sleep(2 * attempt)
    raise RunnerError(f"{url}: {last}")


def download(url: str, dest: Path) -> None:
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(_request(url), timeout=60) as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out, 1 << 20)
            return
        except urllib.error.HTTPError as exc:
            raise RunnerError(f"{url}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            time.sleep(2 * attempt)
    raise RunnerError(f"{url}: {last}")


def _inside(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def check_tar_member(root: Path, member: tarfile.TarInfo, archive: str) -> None:
    """Refuse a member, or a link, that would land outside `root` (an already resolved path)."""
    target = (root / member.name).resolve()
    if not _inside(root, target):
        raise RunnerError(f"{archive}: member {member.name!r} leaves the target directory")
    if member.issym() or member.islnk():
        link = Path(member.linkname)
        resolved = (target.parent / link).resolve() if member.issym() else (root / link).resolve()
        if link.is_absolute() or not _inside(root, resolved):
            raise RunnerError(f"{archive}: link {member.name!r} leaves the target directory")


def safe_extract(archive: Path, dest: Path) -> None:
    """Unpack a zip or tar, refusing members and links that leave `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if not _inside(root, (root / name).resolve()):
                    raise RunnerError(f"{archive.name}: member {name!r} leaves the target directory")
            zf.extractall(dest)
        return
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            check_tar_member(root, member, archive.name)
        try:
            tf.extractall(dest, filter="data")
        except TypeError:       # Python before 3.11.4 has no extraction filters
            tf.extractall(dest)


class ToolCache:
    """Downloads and verifies the uv and Node releases once per cache directory."""

    def __init__(self, cache: Path, host: Host, say: Callable[[str], None] = print):
        self.cache, self.host, self.say = cache, host, say

    def _stage(self, key: str) -> Path:
        scratch = self.cache / "downloads" / key
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        return scratch

    def _fetch_verified(self, archive_url: str, name: str, checksum: str, scratch: Path) -> Path:
        archive = scratch / name
        self.say(f"      downloading {archive_url}")
        download(archive_url, archive)
        expected = parse_checksum(checksum, name)
        actual = sha256_file(archive)
        if actual != expected:
            raise RunnerError(f"sha256 of {name} is {actual}, the release publishes {expected}")
        self.say(f"      sha256 matches the published {expected[:12]}...")
        return archive

    def uv_dir(self, version: str) -> Path:
        name, url = uv_asset(self.host, version)
        final = self.cache / "tools" / "uv" / version / name.split(".")[0]
        exe = final / ("uv.exe" if self.host.os == "Windows" else "uv")
        if exe.exists() and (final / ".verified").exists():
            return final
        scratch = self._stage(f"uv-{version}")
        archive = self._fetch_verified(url, name, fetch_text(url + ".sha256"), scratch)
        safe_extract(archive, scratch / "x")
        found = next((p for p in (scratch / "x").rglob(exe.name) if p.is_file()), None)
        if found is None:
            raise RunnerError(f"{name} holds no {exe.name}")
        shutil.rmtree(final, ignore_errors=True)
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(found.parent, final)
        (final / ".verified").write_text(f"{url}\n", encoding="utf-8")
        shutil.rmtree(scratch, ignore_errors=True)
        return final

    def node_dir(self, version: str) -> Path:
        """The directory to put first on PATH (the bin directory on POSIX)."""
        name, url = node_asset(self.host, version)
        base_url = url.rsplit("/", 1)[0]
        final = self.cache / "tools" / "node" / version / f"{self.host.os}-{self.host.arch}"
        bin_dir = final if self.host.os == "Windows" else final / "bin"
        if (final / ".verified").exists() and any(bin_dir.glob("node*")):
            return bin_dir
        scratch = self._stage(f"node-{version}")
        archive = self._fetch_verified(url, name, fetch_text(f"{base_url}/SHASUMS256.txt"), scratch)
        safe_extract(archive, scratch / "x")
        roots = [p for p in (scratch / "x").iterdir()]
        content = roots[0] if len(roots) == 1 and roots[0].is_dir() else scratch / "x"
        shutil.rmtree(final, ignore_errors=True)
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(content), str(final))
        (final / ".verified").write_text(f"{url}\n", encoding="utf-8")
        shutil.rmtree(scratch, ignore_errors=True)
        return bin_dir


# ---------------------------------------------------------------------------------------
# Cleanup when the runner is stopped
# ---------------------------------------------------------------------------------------

# Whatever the runner starts registers a stop function here while it runs. A signal turns into
# an exception that unwinds the normal way (terminate_on_signals); the atexit hook is the net
# under that for a path that skips the unwinding.
_cleanups: list[Callable[[], None]] = []
_atexit_installed = False


def _arm(stop: Callable[[], None]) -> None:
    global _atexit_installed
    if not _atexit_installed:
        atexit.register(run_cleanups)
        _atexit_installed = True
    _cleanups.append(stop)


def _disarm(stop: Callable[[], None]) -> None:
    with contextlib.suppress(ValueError):
        _cleanups.remove(stop)


def run_cleanups() -> None:
    while _cleanups:
        with contextlib.suppress(Exception):
            _cleanups.pop()()


@contextlib.contextmanager
def terminate_on_signals():
    """SIGTERM, SIGHUP and SIGBREAK (a closed console on Windows) end the run like Ctrl-C.

    The handler raises Terminated in the main thread, so the `finally` blocks and context
    managers that stop the busy loops and kill the step's processes run. A kill -9 or
    TerminateProcess runs nothing: the busy loops then notice the runner is gone, and a
    Windows step dies with the job object.
    """
    previous: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        def handler(signum, _frame):
            raise Terminated(signum)

        for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is not None:
                with contextlib.suppress(ValueError, OSError):
                    previous[number] = signal.signal(number, handler)
    try:
        yield
    finally:
        for number, old in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(number, old)


def parse_ps(text: str) -> list[tuple[int, str]]:
    """(pid, command line) rows from `ps -ww -axo pid=,command=`."""
    rows = []
    for line in text.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), command.strip()))
    return rows


def process_table() -> list[tuple[int, str]]:
    """(pid, command line) of every process this user can see; POSIX only."""
    if Path("/proc/self/cmdline").exists():             # Linux
        rows = []
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                with contextlib.suppress(OSError):
                    raw = Path(entry.path, "cmdline").read_bytes()
                    rows.append((int(entry.name), raw.replace(b"\0", b" ").decode("utf-8", "replace")))
        return rows
    proc = subprocess.run(["ps", "-ww", "-axo", "pid=,command="], capture_output=True,
                          encoding="utf-8", errors="replace")
    return parse_ps(proc.stdout)


def kill_leftovers(marker: str, *, table: Callable[[], list[tuple[int, str]]] = process_table,
                   kill: Callable[[int, int], None] = os.kill) -> list[int]:
    """SIGKILL every process whose command line contains `marker`; return their pids.

    A process group misses a descendant that called setsid (the server spawner does). Everything
    the job starts runs code from the clone under the run directory, so its path in the command
    line finds them. This process and its parent are never touched.
    """
    spare = {os.getpid(), os.getppid()}
    found = [pid for pid, command in table() if marker in command and pid not in spare]
    for pid in found:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    return found


# ---------------------------------------------------------------------------------------
# Hardware parity
# ---------------------------------------------------------------------------------------

# Appears in every busy loop's argv, so a process listing (pgrep -f ci_local.py) shows them.
STRESS_MARKER = "ci_local.py-busy-loop"

# Each process spins until the deadline passes, the flag file disappears or the runner that
# started it is gone (checked once a second), so a killed runner cannot leave busy loops behind.
# A "low" spinner drops to nice 19 on POSIX (Windows gets BELOW_NORMAL at creation), so it takes
# idle capacity and gives way to the job; a "normal" one competes with the job for cores.
_SPIN = """
import os, sys, time
end, flag, priority, parent = float(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
if priority == 'low' and hasattr(os, 'nice'):
    os.nice(19)


def parent_alive():
    if os.name != 'nt':
        return os.getppid() == parent
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x1000, False, parent)      # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() != 87             # ERROR_INVALID_PARAMETER: no such pid
    code = wintypes.DWORD()
    ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
    k32.CloseHandle(handle)
    return not ok or code.value == 259                   # STILL_ACTIVE


n, checked = 0, time.time()
while True:
    for _ in range(200000):
        n += 1
    now = time.time()
    if now > end or not os.path.exists(flag):
        break
    if now - checked > 1.0:
        checked = now
        if not parent_alive():
            break
"""


class StressPool:
    def __init__(self, count: int, workdir: Path, max_seconds: float,
                 spawn: Callable[..., object] = subprocess.Popen, priority: str = DEFAULT_PRIORITY):
        if priority not in {"low", "normal"}:
            raise RunnerError(f"--stress-priority takes low or normal, not {priority!r}")
        self.count, self.workdir, self.max_seconds, self.spawn = count, workdir, max_seconds, spawn
        self.priority = priority
        self.procs: list = []
        self.flag = workdir / "stress.alive"

    def start(self) -> None:
        if not self.count:
            return
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.flag.write_text("running\n", encoding="utf-8")
        deadline = str(time.time() + self.max_seconds)
        flags = {}
        if os.name == "nt":      # CREATE_NO_WINDOW, and BELOW_NORMAL_PRIORITY_CLASS for "low"
            flags = {"creationflags": 0x08000000 | (0x00004000 if self.priority == "low" else 0)}
        _arm(self.stop)
        for _ in range(self.count):
            self.procs.append(self.spawn(
                [sys.executable, "-I", "-S", "-c", _SPIN, deadline, str(self.flag), self.priority,
                 str(os.getpid()), STRESS_MARKER],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags))

    def stop(self) -> None:
        _disarm(self.stop)
        with contextlib.suppress(OSError):
            self.flag.unlink()
        for proc in self.procs:
            with contextlib.suppress(Exception):
                proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
        self.procs = []

    def __enter__(self) -> StressPool:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


# ---------------------------------------------------------------------------------------
# Running processes
# ---------------------------------------------------------------------------------------

def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def _windows_job(pid: int) -> Callable[[], None] | None:
    """Put `pid` in a job object whose termination kills every process it ever started.

    taskkill /T follows parent links only, and an MSYS child of Git's bash (sleep, a test
    server) escapes it while still holding the output pipe. A job object follows membership.
    """
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    process = k32.OpenProcess(0x0001 | 0x0100, False, pid)    # PROCESS_TERMINATE | PROCESS_SET_QUOTA
    ok = bool(process) and k32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) \
        and k32.AssignProcessToJobObject(job, process)
    if process:
        k32.CloseHandle(process)
    if not ok:
        k32.CloseHandle(job)
        return None
    closed = []

    def kill() -> None:
        if not closed:
            closed.append(True)
            k32.TerminateJobObject(job, 1)
            k32.CloseHandle(job)

    return kill


def attach_group(proc: subprocess.Popen, scope: Path | None = None) -> Callable[[], None]:
    """A function that kills `proc` and everything it started, orphans included.

    On Windows a job object follows membership. Elsewhere the process group is killed, and then
    every process whose command line holds the `scope` directory (the run directory), which finds
    the descendants that left the group with setsid.
    """
    job_kill = _windows_job(proc.pid) if os.name == "nt" else None
    marker = f"{scope}{os.sep}" if scope is not None else None

    def kill() -> None:
        if os.name == "nt":
            if job_kill:
                job_kill()
            else:          # no job object (nested restrictions): follow parent links instead
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            if marker:
                with contextlib.suppress(Exception):
                    kill_leftovers(marker)
        with contextlib.suppress(Exception):
            proc.kill()

    return kill


@dataclasses.dataclass
class ProcessResult:
    code: int
    timed_out: bool = False


def run_process(argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, log,
                timeout: float | None, label: str = "", echo: bool = False,
                heartbeat: float = 60.0, say: Callable[[str], None] = print,
                scope: Path | None = None) -> ProcessResult:
    """Run argv, copy its output into `log`, and kill its whole tree on a timeout or an interrupt."""
    flags: dict = {"creationflags": 0x00000200} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **flags)
    last = {"line": ""}

    def pump() -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace")
            log.write(line)
            log.flush()
            last["line"] = line.rstrip()
            if echo:
                sys.stdout.write("      | " + line)
                sys.stdout.flush()

    kill_group = attach_group(proc, scope)
    _arm(kill_group)
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    started = time.monotonic()
    next_beat = started + heartbeat
    timed_out = False
    try:
        while True:
            try:
                code = proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if timeout is not None and now - started > timeout:
                kill_group()
                timed_out = True
                code = proc.wait()
                break
            if now >= next_beat:
                next_beat += heartbeat
                say(f"      ... {label} still running after {fmt_duration(now - started)}; "
                    f"last line: {last['line'][:110]}")
        # What a step leaves running is cleaned up with the step, as the hosted runner does at
        # the end of a job; it also releases the output pipe so the reader sees the end of file.
        kill_group()
    except BaseException:
        kill_group()
        raise
    finally:
        _disarm(kill_group)
    reader.join(timeout=10)
    if not reader.is_alive():
        proc.stdout.close()
    return ProcessResult(code, timed_out)


# ---------------------------------------------------------------------------------------
# Executing a plan
# ---------------------------------------------------------------------------------------

# The variables copied from this machine into every step: the ones a process cannot start
# without, which a hosted runner has too. Nothing else from your shell reaches the job, so a
# stray UV_*, PYTHON*, PYTEST_*, TZ, BROWSERPLUG_* or token variable cannot steer the suite.
# PATH is the machine's, with the setup actions' directories in front.
HOST_ENV_NAMES: dict[str, tuple[str, ...]] = {
    "Windows": (
        "PATH", "PATHEXT", "SystemRoot", "SystemDrive", "windir", "ComSpec", "TEMP", "TMP", "HOME",
        "USERNAME", "USERDOMAIN", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
        "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles",
        "CommonProgramFiles(x86)", "CommonProgramW6432", "ALLUSERSPROFILE", "PUBLIC", "COMPUTERNAME",
        "OS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL",
        "PROCESSOR_REVISION", "NUMBER_OF_PROCESSORS", "PSModulePath"),
    "other": ("PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "SHELL"),
}


def env_key(name: str) -> str:
    return name.upper() if os.name == "nt" else name


def host_passthrough(host_env: Mapping[str, str], host_os: str) -> dict[str, str]:
    """The allowlisted variables of `host_env`, keyed the way this OS compares names."""
    windows = host_os == "Windows"
    names = HOST_ENV_NAMES["Windows" if windows else "other"]
    wanted = {n.upper() for n in names} if windows else set(names)
    return {env_key(k): v for k, v in host_env.items() if (k.upper() if windows else k) in wanted}


def rmtree_force(path: Path) -> bool:
    def onerror(func, p, _exc):
        with contextlib.suppress(OSError):
            os.chmod(p, 0o700)
            func(p)
    for _ in range(3):
        shutil.rmtree(path, onerror=onerror)
        if not path.exists():
            return True
        time.sleep(1)
    return False


_PYTEST_OUTCOME = (r"\d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?|rerun"
                   r"|subtests passed|subtests failed)")
_PYTEST_SUMMARY = re.compile(
    rf"^=*\s*({_PYTEST_OUTCOME}(?:, {_PYTEST_OUTCOME})*|no tests ran) in \d+(?:\.\d+)?s"
    r"(?: \(\d+:\d\d:\d\d\))?\s*=*$")


def pytest_summaries(log_path: Path) -> tuple[str, ...]:
    """The summary line of each pytest run in a step's log: '4123 passed, 66 skipped in 759.12s'."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(m.group(0).strip("= ") for m in map(_PYTEST_SUMMARY.match, text.splitlines()) if m)


@dataclasses.dataclass
class StepResult:
    index: int
    name: str
    status: str               # passed, emulated, failed, skipped, not-reproducible
    seconds: float
    note: str = ""
    pytest: tuple[str, ...] = ()


@dataclasses.dataclass
class JobResult:
    job_id: str
    run_dir: Path
    steps: list[StepResult]
    seconds: float

    @property
    def failed(self) -> bool:
        return any(s.status == "failed" for s in self.steps)

    @property
    def unavailable(self) -> list[str]:
        return [s.name for s in self.steps if s.status == "not-reproducible"]


class StepFailed(Exception):
    pass


@dataclasses.dataclass
class RunOptions:
    cache: Path
    keep_cache: bool = False
    keep: bool = False
    verbose: bool = False
    heartbeat: float = 60.0
    timeout_scale: float = 1.0       # multiplies the job's timeout-minutes
    skip_steps: frozenset[int] = frozenset()     # step indexes to skip (--hammer-only)


class JobExecutor:
    def __init__(self, plan: Plan, source: Source, run_dir: Path, options: RunOptions, *,
                 tools: ToolCache, say: Callable[[str], None] = print,
                 process: Callable[..., ProcessResult] = run_process,
                 host_env: Mapping[str, str] | None = None):
        self.plan, self.source, self.run_dir, self.opt = plan, source, run_dir, options
        self.tools, self.say, self.process = tools, say, process
        self.host_env = os.environ if host_env is None else host_env
        self.logs = run_dir / "logs"
        self.workspace = run_dir / "workspace"
        self.temp = run_dir / "temp"
        self.git_config = self.temp / "gitconfig"
        self.exports: dict[str, str] = {}      # what the setup actions and the timezone step set
        self.path_prepend: list[str] = []
        self.started = 0.0

    # -- environment ----------------------------------------------------------------

    def base_env(self) -> dict[str, str]:
        host, runner = self.plan.host, self.plan.runner
        persistent = self.opt.cache / "keep" if self.opt.keep_cache else self.temp
        env = host_passthrough(self.host_env, host.os)
        env.update({env_key(k): v for k, v in {
            # What a hosted runner sets.
            "CI": "true", "GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": str(self.workspace),
            "RUNNER_OS": host.os, "RUNNER_ARCH": host.arch, "RUNNER_TEMP": str(self.temp),
            # Not on a hosted runner: they keep this machine's own configuration out of the job.
            # The user's git config, the system git config, a uv.toml and a .npmrc do not exist
            # there. The job's global git config stands in for the image's.
            "GIT_CONFIG_GLOBAL": str(self.git_config), "GIT_CONFIG_NOSYSTEM": "1",
            "UV_NO_CONFIG": "1",
            "npm_config_cache": str(persistent / "npm-cache"),
            "npm_config_userconfig": str(self.temp / "empty.npmrc"),
        }.items()})
        if runner.os == "macOS":
            env["LANG"] = "en_US.UTF-8"      # the hosted macOS image sets it
        return env

    def step_env(self, planned: PlannedStep) -> dict[str, str]:
        env = self.base_env()
        for layer in (self.plan.env, self.exports, planned.env):
            for key, value in layer.items():
                env[env_key(key)] = value
        path_key = env_key("PATH")
        env[path_key] = os.pathsep.join([*reversed(self.path_prepend), env.get(path_key, "")])
        return env

    # -- the job ---------------------------------------------------------------------

    def run(self) -> JobResult:
        for d in (self.logs, self.temp, self.workspace):
            d.mkdir(parents=True, exist_ok=True)       # the runner always hands a job a workspace
        (self.temp / "empty.npmrc").write_text("", encoding="utf-8")
        # The Windows runner image ships core.autocrlf=true; its job steps may change that.
        self.git_config.write_text("[core]\n\tautocrlf = true\n" if self.plan.runner.os == "Windows"
                                   else "", encoding="utf-8", newline="\n")
        self.started = time.monotonic()
        results: list[StepResult] = []
        aborted = False
        total = len(self.plan.steps)
        for planned in self.plan.steps:
            label = f"[{self.plan.job_id}] {planned.index}/{total} {planned.name}"
            why = (planned.reason if planned.status == "skip"
                   else "--hammer-only: the hammer runs the tests instead"
                   if planned.index in self.opt.skip_steps
                   else "an earlier step failed" if aborted else "")
            if why:
                self.say(f"{label}\n      skipped ({why})")
                results.append(StepResult(planned.index, planned.name, "skipped", 0.0, why))
                continue
            if planned.status == "unavailable":
                self.say(f"{label}\n      NOT REPRODUCIBLE HERE: {planned.reason}")
                results.append(StepResult(planned.index, planned.name, "not-reproducible", 0.0,
                                          planned.reason))
                continue
            self.say(label)
            t0 = time.monotonic()
            log_path = self.logs / f"{planned.index:02d}-{re.sub(r'[^A-Za-z0-9]+', '-', planned.name)[:50]}.log"
            try:
                with open(log_path, "w", encoding="utf-8", newline="\n") as log:
                    status, note = self.dispatch(planned, log)
            except StepFailed as exc:
                status, note = "failed", str(exc)
            except (RunnerError, OSError) as exc:
                status, note = "failed", f"{type(exc).__name__}: {exc}"
            seconds = time.monotonic() - t0
            summaries = pytest_summaries(log_path) if planned.kind == "run" else ()
            results.append(StepResult(planned.index, planned.name, status, seconds, note, summaries))
            self.say(f"      {status} in {fmt_duration(seconds)}" + (f": {note}" if note else ""))
            for line in summaries:
                self.say(f"      pytest: {line}")
            if status == "failed":
                aborted = True
                self.say(f"      log: {log_path}")
                self.show_tail(log_path)
        return JobResult(self.plan.job_id, self.run_dir, results, time.monotonic() - self.started)

    def show_tail(self, path: Path, lines: int = 25) -> None:
        with contextlib.suppress(OSError):
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
            for ln in tail:
                self.say("      | " + ln)

    def remaining(self) -> float | None:
        """Seconds left of the job's timeout-minutes (scaled), or None without one."""
        if not self.plan.timeout_minutes:
            return None
        scale = self.opt.timeout_scale
        left = self.plan.timeout_minutes * 60 * scale - (time.monotonic() - self.started)
        if left <= 0:
            raise StepFailed(f"the job's timeout-minutes ({self.plan.timeout_minutes:g}"
                             f"{f' x {scale:g}' if scale != 1 else ''}) ran out")
        return left

    def dispatch(self, planned: PlannedStep, log) -> tuple[str, str]:
        handler = getattr(self, "do_" + planned.kind.replace("-", "_"), None)
        if handler is None:
            raise StepFailed(f"no handler for the step kind {planned.kind!r}")
        return handler(planned, log)

    # -- emulated actions -------------------------------------------------------------

    def command(self, argv: Sequence[str], planned: PlannedStep, log, cwd: Path | None = None,
                env: Mapping[str, str] | None = None) -> None:
        if cwd is None:
            cwd = self.workspace
        result = self.process(argv, env=env if env is not None else self.step_env(planned),
                              cwd=cwd, log=log, timeout=self.remaining(), label=planned.name,
                              echo=self.opt.verbose, heartbeat=self.opt.heartbeat, say=self.say,
                              scope=self.run_dir)
        if result.timed_out:
            raise StepFailed("timed out and was killed")
        if result.code:
            raise StepFailed(f"exit code {result.code}")

    def do_checkout(self, planned: PlannedStep, log) -> tuple[str, str]:
        if self.workspace.exists():
            rmtree_force(self.workspace)
        # `clone -c` also writes the setting into the new repository's config.
        argv = ["git", "-c", "advice.detachedHead=false", "clone",
                "-c", f"core.autocrlf={planned.params['autocrlf']}", "--no-tags", "--branch", "main"]
        if planned.params["fetch_depth"]:
            argv += ["--depth", str(planned.params["fetch_depth"])]
        argv += [self.source.stage_url, str(self.workspace)]
        self.command(argv, planned, log, cwd=self.run_dir,
                     env=git_env({"GIT_CONFIG_GLOBAL": str(self.git_config)}))
        head = git(["rev-parse", "HEAD"], cwd=self.workspace).strip()
        if head != self.source.commit:
            raise StepFailed(f"the clone is at {head}, expected {self.source.commit}")
        return "emulated", f"{self.source.description}, clone at {head[:7]}"

    def do_git_autocrlf(self, planned: PlannedStep, log) -> tuple[str, str]:
        """The step's `git config --global` lands in the job's own global config file."""
        value = planned.params["value"]
        proc = subprocess.run(["git", "config", "--file", str(self.git_config), "core.autocrlf", value],
                              capture_output=True, encoding="utf-8", errors="replace",
                              env=git_env())
        log.write(proc.stdout + proc.stderr)
        if proc.returncode:
            raise StepFailed(f"git config --file failed: {proc.stderr.strip()}")
        return "emulated", f"core.autocrlf={value} in the job's git config and on the clone"

    def do_timezone(self, planned: PlannedStep, log) -> tuple[str, str]:
        zone = planned.params["zone"]
        if self.plan.host.os == "Windows":
            proc = subprocess.run(["tzutil", "/g"], capture_output=True, encoding="utf-8",
                                  errors="replace")
            current = proc.stdout.strip()
            log.write(f"machine zone: {current}\nrequired:     {zone}\n")
            if current != zone:
                raise StepFailed(
                    f"this machine is in {current!r} and the job sets {zone!r}. The runner does "
                    f"not change system settings: run tzutil /s \"{zone}\" yourself, or use a host "
                    "in that zone")
            return "emulated", f"the machine is already in {zone}"
        self.exports["TZ"] = planned.params["tz"]
        return "emulated", f"TZ={planned.params['tz']} for the steps that follow"

    def do_setup_uv(self, planned: PlannedStep, log) -> tuple[str, str]:
        version = planned.params["version"]
        uv_dir = self.tools.uv_dir(version)
        persistent = self.opt.cache / "keep" if self.opt.keep_cache else self.temp
        python_dir, cache_dir = persistent / "uv-python-dir", persistent / "setup-uv-cache"
        tool_bin = self.temp / "uv-tool-bin"
        for d in (python_dir, cache_dir, tool_bin):
            d.mkdir(parents=True, exist_ok=True)
        self.path_prepend += [str(tool_bin), str(uv_dir), str(python_dir)]
        exports = {"UV_PYTHON_INSTALL_DIR": str(python_dir), "UV_CACHE_DIR": str(cache_dir)}
        if planned.params["python_version"]:
            exports["UV_PYTHON"] = planned.params["python_version"]
        self.exports.update(exports)
        proc = subprocess.run([str(uv_dir / ("uv.exe" if self.plan.host.os == "Windows" else "uv")),
                               "--version"], capture_output=True, encoding="utf-8", errors="replace")
        log.write(proc.stdout + proc.stderr)
        if not proc.stdout.startswith(f"uv {version}"):
            raise StepFailed(f"uv reports {proc.stdout.strip()!r}, expected version {version}")
        return "emulated", f"uv {version} at {uv_dir}"

    def do_setup_node(self, planned: PlannedStep, log) -> tuple[str, str]:
        version = planned.params["version"]
        node_dir = self.tools.node_dir(version)
        self.path_prepend.append(str(node_dir))
        env = self.step_env(planned)
        # CreateProcess looks `node` up on this process's PATH, so resolve it on the job's.
        node = shutil.which("node", path=env[env_key("PATH")])
        if node is None:
            raise StepFailed(f"no node on the job's PATH after adding {node_dir}")
        found = subprocess.run([node, "--version"], env=env, capture_output=True,
                               encoding="utf-8", errors="replace")
        log.write(found.stdout + found.stderr)
        if found.stdout.strip() != f"v{version}":
            raise StepFailed(f"node on the job PATH reports {found.stdout.strip()!r}, expected v{version}")
        return "emulated", f"node v{version} at {node_dir}"

    def do_upload_artifact(self, planned: PlannedStep, log) -> tuple[str, str]:
        dest = self.run_dir / "artifacts" / planned.params["name"]
        copied = []
        for pattern in planned.params["patterns"]:
            for match in sorted(glob.glob(pattern, root_dir=self.workspace, recursive=True)):
                src = self.workspace / match
                if src.is_file():
                    target = dest / match
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
                    copied.append(match)
        log.write("\n".join(copied) + "\n")
        if not copied:
            if planned.params["if_no_files"] == "error":
                raise StepFailed("no files matched, and if-no-files-found is error")
            return "emulated", "no files matched"
        return "emulated", f"{len(copied)} file(s) copied to {dest}"

    # -- run steps ----------------------------------------------------------------------

    def do_run(self, planned: PlannedStep, log) -> tuple[str, str]:
        shell = resolve_shell(self.plan.runner.os, self.plan.host)
        if shell.exe is None:
            raise StepFailed(f"{shell.name} is not installed here")
        script = self.temp / f"step-{planned.index}{shell.suffix}"
        body = planned.command
        if shell.name in {"pwsh", "powershell"}:
            body = ("$ErrorActionPreference = 'stop'\n" + body +
                    "\nif ((Test-Path -LiteralPath variable:\\LASTEXITCODE)) { exit $LASTEXITCODE }")
            script.write_text(body, encoding="utf-8-sig", newline="\n")
        else:
            script.write_text(body + "\n", encoding="utf-8", newline="\n")
        target = script.as_posix() if shell.name == "bash" and os.name == "nt" else str(script)
        argv = [shell.exe, *[a.replace("{0}", target) for a in shell.args]]
        self.command(argv, planned, log)
        return "passed", ""


# ---------------------------------------------------------------------------------------
# Hammer: the tests a change touches, repeated (a local addition, not part of the CI job)
# ---------------------------------------------------------------------------------------

# A flake that fails 1 run in 12 survives any single pass of the suite, and CPU stress does not
# raise that rate (testing.md, "Flake hunting"). Repetition does, so --hammer reruns the tests
# a change touches in the clone the job already built.
HAMMER_DEFAULT = 20
EXIT_NOTHING_TESTED = 4        # --hammer-only selected no tests: neither a pass nor a failure
HAMMER_MAX_FILES = 8
HAMMER_SHOW_DROPPED = 10       # dropped candidates the selection printout names
_TEST_RUNNER = re.compile(r"tests/run\.py")
RUN_LEVEL = "(pytest run)"      # the "test" a repetition is charged to when pytest died without one


@dataclasses.dataclass
class HammerSelection:
    targets: list[str] = dataclasses.field(default_factory=list)       # files or nodes for tests/run.py
    reasons: dict[str, str] = dataclasses.field(default_factory=dict)  # target -> why it was picked
    dropped: list[str] = dataclasses.field(default_factory=list)       # candidates the cap cut
    explicit: bool = False                                             # named with --hammer-tests
    base: str = ""                                                     # what the changes are measured from
    changed: int = 0                                                   # files in that diff
    limit: int = HAMMER_MAX_FILES


def module_of(path: str) -> tuple[str, str, str] | None:
    """(dotted name, parent package, stem) of a source file the unit tests could import.

    Only server/, launcher/ and shared-py/ count. shared-py/ is a source root of its own, so
    shared-py/browserplug_shared/version.py is browserplug_shared.version. Package __init__ and
    __main__ files map to nothing: their dotted name would match every test that imports a sibling.
    """
    p = path.replace("\\", "/")
    if not p.endswith(".py"):
        return None
    if p.startswith("shared-py/"):
        p = p[len("shared-py/"):]
    elif not p.startswith(("server/", "launcher/")):
        return None
    parts = p[:-3].split("/")
    if parts[-1] in {"__init__", "__main__"}:
        return None
    return ".".join(parts), ".".join(parts[:-1]), parts[-1]


def module_patterns(dotted: str, parent: str, stem: str) -> list[tuple[re.Pattern, int, str]]:
    """(pattern, weight, label) for the ways a test names a module.

    A cheap textual match, not an import graph: the dotted name (weight 2), a one-line
    `from <parent> import <stem>` (weight 2; a parenthesised multi-line import is missed), and the
    file name `<stem>.py` (weight 1; tests that spawn or read the script). Over-picking only costs
    repetitions, and the cap bounds that.
    """
    found = [(re.compile(rf"(?<![\w.]){re.escape(dotted)}(?!\w)"), 2, dotted)]
    if parent:
        found.append((re.compile(rf"\bfrom\s+{re.escape(parent)}\s+import\b[^\n]*\b{re.escape(stem)}\b"),
                      2, dotted))
    found.append((re.compile(rf"(?<![\w-]){re.escape(stem)}\.py\b"), 1, f"{stem}.py as a file name"))
    return found


_CONFTESTS = frozenset({"tests/conftest.py", "tests/unit/conftest.py"})
_SUPPORT_DIRS = ("tests/", "tests/unit/")


def support_module_of(path: str) -> str | None:
    """Import name of a test-side module the unit tests could import, or None.

    The tests put tests/ and tests/unit/ on sys.path, so tests/wait_helper.py is `wait_helper` and a
    tests/unit/test_x.py that others import from is `test_x`. A conftest is no module: it reaches
    its tests without being imported (see _CONFTESTS).
    """
    p = path.replace("\\", "/")
    if not p.endswith(".py") or p in _CONFTESTS:
        return None
    for prefix in _SUPPORT_DIRS:
        stem = p[len(prefix):-3]
        if p.startswith(prefix) and stem and "/" not in stem and stem not in {"__init__", "__main__"}:
            return stem
    return None


def support_patterns(stem: str) -> list[tuple[re.Pattern, int, str]]:
    """(pattern, weight, label) for a test importing `stem`: `import stem`, `from stem import`, the
    package-qualified forms and `from tests.unit import stem` (one line; indented imports count)."""
    name = re.escape(stem)
    return [(re.compile(rf"(?m)^[ ]*(?:from|import)[ ]+(?:tests\.(?:unit\.)?)?{name}(?!\w)"), 2, stem),
            (re.compile(rf"(?m)^[ ]*from[ ]+tests(?:\.unit)?[ ]+import\b[^\n]*\b{name}\b"), 2, stem)]


def select_hammer_tests(changed: Sequence[str], tests: Mapping[str, str], *,
                        max_files: int = HAMMER_MAX_FILES,
                        sizes: Mapping[str, int] | None = None) -> HammerSelection:
    """Unit test files to repeat, most relevant first, cut at `max_files`.

    `tests` maps each unit test file (tests/unit/test_x.py) to its text; `sizes` maps a changed path
    to the lines added plus removed against the base (missing means unknown, counted as 0).
    Ranking, which decides what the cap keeps:
      1. test files that changed, the most changed lines first (a rewritten file is a better lead
         than a one-line edit);
      2. test files that name a changed module under server/, launcher/ or shared-py/, import a
         changed test-side module (tests/wait_helper.py, tests/port_coordinator.py, a test file
         that others import from), or sit under a changed conftest.py. More changed modules named
         first, a dotted name or import above a bare file name, then the more changed lines in
         the modules named.
    Ties break by path. A match is textual, not an import graph.
    """
    sized = {k.replace("\\", "/"): v for k, v in (sizes or {}).items()}
    paths = sorted({c.replace("\\", "/") for c in changed})
    own = sorted((p for p in paths if p in tests), key=lambda p: (-sized.get(p, 0), p))
    # (verb for the reason, patterns, changed lines) per changed path that others could depend on
    sources: list[tuple[str, list[tuple[re.Pattern, int, str]], int]] = []
    for p in paths:
        size = sized.get(p, 0)
        if module := module_of(p):
            sources.append(("names", module_patterns(*module), size))
        elif p in _CONFTESTS:       # autouse fixtures reach every test; no text says which use them
            sources.append(("is under", [(re.compile(""), 1, f"{p} (applies to every test)")], size))
        elif stem := support_module_of(p):
            sources.append(("imports", support_patterns(stem), size))
    ranked: list[tuple[int, int, str, str]] = []
    for name, text in tests.items():
        if name in own:
            continue
        points, lines, said = 0, 0, {}
        for verb, patterns, size in sources:
            hit = max(((w, label) for pat, w, label in patterns if pat.search(text)), default=None)
            if hit:
                points, lines = points + hit[0], lines + size
                said.setdefault(verb, []).append(hit[1])
        if points:
            why = "; ".join(f"{verb} {', '.join(dict.fromkeys(labels))}" for verb, labels in said.items())
            ranked.append((points, lines, name, why + (f" ({lines} line(s) changed there)" if lines else "")))
    ranked.sort(key=lambda r: (-r[0], -r[1], r[2]))
    reasons = {p: f"changed, {sized[p]} line(s) added+removed" if sized.get(p) else "changed" for p in own}
    reasons.update({name: why for _, _, name, why in ranked})
    order = [*own, *(name for _, _, name, _ in ranked)]
    keep, dropped = order[:max_files], order[max_files:]
    return HammerSelection(targets=keep, reasons={t: reasons[t] for t in keep}, dropped=dropped,
                           changed=len(paths), limit=max_files)


def explicit_selection(nodes: Sequence[str]) -> HammerSelection:
    """Files or pytest nodes named with --hammer-tests: taken as given, never capped."""
    targets = list(dict.fromkeys(nodes))
    return HammerSelection(targets=targets, reasons={t: "named with --hammer-tests" for t in targets},
                           explicit=True)


def format_selection(sel: HammerSelection, count: int, only: bool = False) -> list[str]:
    lines = [f"hammer: {count} repetition(s) of the tests below. A local addition, not part of the CI "
             "job: it runs after the job's steps pass, in the same clone with the same tools, "
             "environment and stress."]
    if only:
        lines.append("  --hammer-only: the job's test step is skipped; everything before it still runs.")
    if sel.explicit:
        lines.append("  tests: named with --hammer-tests")
    else:
        lines.append(f"  base: {sel.base}; {sel.changed} file(s) changed since")
    if not sel.targets:
        lines.append("  nothing selected: no unit test file changed and none names a changed module. "
                     "Name tests with --hammer-tests, or move the base with --base.")
        lines.append("  --hammer-only would exit 4 here: nothing would be tested." if only else
                     "  A plain --hammer would skip the hammer and leave the job's own verdict standing.")
        return lines
    order = "" if sel.explicit else ", most relevant first (changed test files by lines changed, then the rest)"
    lines.append(f"  selected {len(sel.targets)} test file(s) or node(s){order}:")
    width = max(len(t) for t in sel.targets)
    lines += [f"    {t:<{width}}  {sel.reasons[t]}" for t in sel.targets]
    if sel.dropped:
        shown = sel.dropped[:HAMMER_SHOW_DROPPED]
        more = (f", and {len(sel.dropped) - len(shown)} more"
                if len(sel.dropped) > len(shown) else "")
        lines.append(f"  CAP: at most {sel.limit} files, so {len(sel.dropped)} candidate(s) are NOT repeated: "
                     f"{', '.join(shown)}{more}")
        lines.append("  Repeat them by naming files with --hammer-tests FILE ... (never capped), "
                     "or raise the cap with --hammer-max-files N.")
    return lines


def resolve_commit(repo: Path, rev: str) -> str:
    return git(["rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=repo).strip()


def hammer_base(repo: Path, tested: str, base: str | None) -> tuple[str, str]:
    """(commit, description) the changes are measured from.

    --base wins. Otherwise the merge base of origin/main and the tested commit, and the tested
    commit's parent when origin/main is unknown (a fresh clone without it, a different default
    branch).
    """
    if base:
        sha = resolve_commit(repo, base)
        return sha, f"{base} ({sha[:7]}, from --base)"
    try:
        sha = git(["merge-base", "origin/main", tested], cwd=repo).strip()
        return sha, f"the merge base with origin/main ({sha[:7]})"
    except RunnerError:
        sha = resolve_commit(repo, f"{tested}~1")
        return sha, f"{tested[:7]}~1 ({sha[:7]}; origin/main is not available here)"


def line_count(path: Path) -> int:
    """Lines in a file (0 when unreadable): what an untracked file adds to the diff."""
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    return data.count(b"\n") + (0 if not data or data.endswith(b"\n") else 1)


def changed_sizes(repo: Path, base_sha: str, ref: str | None) -> dict[str, int]:
    """{path: lines added + removed} for the files that differ from `base_sha`: in the commit `ref`,
    or in the working tree (which holds uncommitted edits and the untracked files this runner also
    packages; an untracked file counts all its lines). Deleted files are left out; a binary file
    counts 0."""
    diff = ["diff", "--numstat", "--no-renames", "--diff-filter=d", "-z", base_sha]
    sizes: dict[str, int] = {}
    for record in git([*diff, *([ref] if ref else [])], cwd=repo).split("\0"):
        added, _, rest = record.partition("\t")
        removed, _, name = rest.partition("\t")
        if name:
            sizes[name] = int(added) + int(removed) if added.isdigit() and removed.isdigit() else 0
    if ref is None:
        for name in untracked_files(repo):
            sizes[name] = line_count(repo / name)
    return dict(sorted(sizes.items()))


_UNIT_TEST = re.compile(r"tests/unit/test_[^/]+\.py")


def read_unit_tests(repo: Path, ref: str | None) -> dict[str, str]:
    """Text of every tests/unit/test_*.py in the tested source: the disk for the working tree,
    one `git cat-file --batch` for a commit."""
    if ref is None:
        return {f"tests/unit/{p.name}": p.read_text(encoding="utf-8", errors="replace")
                for p in sorted((repo / "tests" / "unit").glob("test_*.py"))}
    listing = git(["ls-tree", "-r", "--name-only", "-z", ref, "--", "tests/unit"], cwd=repo)
    names = [n for n in listing.split("\0") if _UNIT_TEST.fullmatch(n)]
    try:
        proc = subprocess.run(["git", "cat-file", "--batch"], cwd=repo, env=git_env(),
                              input="".join(f"{ref}:{n}\n" for n in names).encode(),
                              capture_output=True)
    except FileNotFoundError as exc:
        raise RunnerError("git is not on PATH") from exc
    data, pos, out = proc.stdout, 0, {}
    for name in names:
        eol = data.find(b"\n", pos)
        head = data[pos:eol].split() if eol >= 0 else []
        if proc.returncode or len(head) != 3 or head[1] != b"blob":
            raise RunnerError(f"git cat-file could not read {ref}:{name}")
        size = int(head[2])
        out[name] = data[eol + 1:eol + 1 + size].decode("utf-8", "replace")
        pos = eol + 1 + size + 1
    return out


def hammer_selection(repo: Path, ref: str | None, base: str | None, explicit: Sequence[str],
                     max_files: int = HAMMER_MAX_FILES) -> HammerSelection:
    if explicit:
        return explicit_selection(explicit)
    toplevel = Path(git(["rev-parse", "--show-toplevel"], cwd=repo).strip())
    base_sha, described = hammer_base(toplevel, resolve_commit(toplevel, ref or "HEAD"), base)
    sizes = changed_sizes(toplevel, base_sha, ref)
    sel = select_hammer_tests(list(sizes), read_unit_tests(toplevel, ref), max_files=max_files, sizes=sizes)
    sel.base = described
    return sel


def find_test_step(plan: Plan) -> PlannedStep:
    """The job's last `tests/run.py` step that is not the ruff gate: its environment is the hammer's."""
    steps = [s for s in plan.steps if s.kind == "run" and s.command
             and _TEST_RUNNER.search(s.command) and "--ruff" not in s.command]
    if not steps:
        raise RunnerError(f"--hammer needs a job with a tests/run.py test step; {plan.job_id} has none")
    if steps[-1].status != "run":
        raise RunnerError(f"--hammer: the test step {steps[-1].name!r} cannot run here "
                          f"({steps[-1].reason})")
    return steps[-1]


@dataclasses.dataclass
class HammerRequest:
    count: int
    selection: HammerSelection
    test_step: PlannedStep
    only: bool = False


@dataclasses.dataclass
class HammerFailure:
    repetition: int
    message: str


@dataclasses.dataclass
class HammerResult:
    requested: int
    completed: int = 0
    seconds: float = 0.0
    tests_run: int = 0                                      # test cases in one repetition
    failures: dict[str, list[HammerFailure]] = dataclasses.field(default_factory=dict)
    skipped: str = ""                                       # why no repetition ran

    @property
    def failed(self) -> bool:
        return bool(self.failures)


def hammer_skip_reason(results: Sequence[JobResult], selection: HammerSelection) -> str:
    """Why the hammer does not run, or '' when it should."""
    if not results or any(r.failed for r in results):
        return "the job's steps failed, so there is no clean clone to repeat in"
    if not selection.targets:
        return ("no tests were selected for the hammer (no unit test file changed and none names a changed "
                "module); the job's own verdict stands")
    return ""


def node_id(classname: str, name: str, files: Sequence[str]) -> str:
    """The pytest node id of a JUnit testcase, rebuilt from the selected files.

    JUnit writes `unit.test_x.TestClass` for tests/unit/test_x.py::TestClass.
    """
    for file in files:
        module = file.partition("::")[0].removeprefix("tests/").removesuffix(".py").replace("/", ".")
        if classname == module or classname.startswith(module + "."):
            rest = [p for p in classname[len(module):].split(".") if p]
            return "::".join([file.partition("::")[0], *rest, name])
    return f"{classname}::{name}" if classname else name


def parse_junit(path: Path, files: Sequence[str]) -> tuple[int, dict[str, str]]:
    """(test cases run, {node id: first line of the failure message}) from one JUnit file."""
    root = ET.parse(path).getroot()          # noqa: S314 - pytest wrote it a moment ago
    cases, failing = 0, {}
    for case in root.iter("testcase"):
        cases += 1
        for child in case:
            if child.tag in {"failure", "error"}:
                text = (child.get("message") or child.text or "").strip()
                first = text.splitlines()[0].strip() if text else child.tag
                failing.setdefault(node_id(case.get("classname", ""), case.get("name", ""), files),
                                   first[:300])
    return cases, failing


def runner_command(step: PlannedStep) -> list[str]:
    """The test step's command up to and including tests/run.py, as words: ['uv', 'run', ..., 'tests/run.py']."""
    for line in (step.command or "").splitlines():
        try:
            words = shlex.split(line)
        except ValueError:
            continue
        for i, word in enumerate(words):
            if _TEST_RUNNER.fullmatch(word):
                return words[:i + 1]
    raise RunnerError(f"--hammer could not find tests/run.py in the command of {step.name!r}")


def run_hammer(ex: JobExecutor, request: HammerRequest, *, timeout: float | None = None,
               fail_fast: bool = False) -> HammerResult:
    """Run the selected tests `count` times in the executor's clone, with the test step's environment.

    Each repetition is a fresh pytest process: the workflow's own command up to tests/run.py (the
    workflow decides how uv is called, so the hammer copies none of it), then the selected tests,
    quiet output, no cache plugin and a JUnit file so failures are counted per test.
    """
    sel = request.selection
    result = HammerResult(request.count)
    env = ex.step_env(request.test_step)
    prefix = runner_command(request.test_step)
    exe = shutil.which(prefix[0], path=env[env_key("PATH")])
    if exe is None:
        raise RunnerError(f"no {prefix[0]} on the job's PATH; the hammer needs the setup steps to have run")
    t0 = time.monotonic()
    for n in range(1, request.count + 1):
        junit = ex.logs / f"hammer-{n:02d}.xml"
        started = time.monotonic()
        argv = [exe, *prefix[1:], *sel.targets, "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"]
        with open(ex.logs / f"hammer-{n:02d}.log", "w", encoding="utf-8", newline="\n") as log:
            proc = ex.process(argv, env=env, cwd=ex.workspace, log=log, timeout=timeout,
                              label=f"hammer {n}/{request.count}", echo=ex.opt.verbose,
                              heartbeat=ex.opt.heartbeat, say=ex.say, scope=ex.run_dir)
        failing: dict[str, str] = {}
        try:
            cases, failing = parse_junit(junit, sel.targets)
            result.tests_run = max(result.tests_run, cases)
        except (OSError, ET.ParseError):
            cases = 0
        if proc.timed_out:
            failing[RUN_LEVEL] = f"timed out after {fmt_duration(time.monotonic() - started)} and was killed"
        elif proc.code and not failing:
            failing[RUN_LEVEL] = (f"pytest exited with code {proc.code} and no failing test in its JUnit "
                                  f"file (collection error or crash); see hammer-{n:02d}.log")
        for test, message in failing.items():
            result.failures.setdefault(test, []).append(HammerFailure(n, message))
        result.completed = n
        ex.say(f"  hammer {n}/{request.count}: " + (f"FAILED, {len(failing)} failing" if failing else "passed")
               + f" in {fmt_duration(time.monotonic() - started)} ({cases} test(s))")
        for test in failing:
            ex.say(f"      {test}")
        if failing and fail_fast:
            break
    result.seconds = time.monotonic() - t0
    return result


def format_hammer(result: HammerResult) -> list[str]:
    head = "hammer (local addition, not part of the CI job):"
    if result.skipped:
        return [head, f"  skipped: {result.skipped}"]
    lines = [head]
    ranked = sorted(result.failures.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for test, fails in ranked:
        lines.append(f"  {test}: {len(fails)}/{result.completed} failed")
        lines.append(f"      first failure (repetition {fails[0].repetition}): {fails[0].message}")
    done = f"{result.completed} of {result.requested} repetition(s) in {fmt_duration(result.seconds)}"
    if result.failed:
        lines.append(f"  HAMMER FAILED: {len(ranked)} test(s) failed at least once ({done})")
    else:
        lines.append(f"  HAMMER PASSED: {result.tests_run} test(s) in every one of {done}")
    return lines


# ---------------------------------------------------------------------------------------
# Repeats, verdict and the command line
# ---------------------------------------------------------------------------------------

def exit_code(results: Sequence[JobResult], allow_unavailable: Collection[str] = (),
              hammer: HammerResult | None = None) -> int:
    """1 for a failure (a step, or a hammer repetition), 3 for a step that could not run and was
    not named, else 0."""
    if any(r.failed for r in results) or (hammer is not None and hammer.failed):
        return 1
    if any(name not in allow_unavailable for r in results for name in r.unavailable):
        return 3
    return 0


def run_repeats(count: int, run_once: Callable[[int], JobResult], fail_fast: bool = False) -> list[JobResult]:
    results: list[JobResult] = []
    for number in range(1, count + 1):
        result = run_once(number)
        results.append(result)
        if result.failed and fail_fast:
            break
    return results


def default_cache_dir() -> Path:
    override = os.environ.get("BROWSERPLUG_CI_LOCAL_CACHE")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) \
            / "browserplug-ci-local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "browserplug-ci-local"
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "browserplug-ci-local"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--job", help="job id from the workflow (default: the job for this OS)")
    p.add_argument("--workflow", type=Path, default=DEFAULT_WORKFLOW)
    p.add_argument("--repo", type=Path, default=ROOT, help="repository to test (default: this one)")
    p.add_argument("--ref", help="test this commit (HEAD is what a push sends) instead of the working "
                                 "tree, whose untracked files are included")
    p.add_argument("--stress", default=DEFAULT_STRESS, metavar="auto|N|0",
                   help="busy-loop processes during the job; auto (default) = this host's logical cores "
                        "minus the runner's vCPUs; 0 turns them off")
    p.add_argument("--stress-priority", choices=("low", "normal"), default=DEFAULT_PRIORITY,
                   help="normal (default): the busy loops compete with the job for cores; "
                        "low: they give way to it and take only idle cores")
    p.add_argument("--repeat", type=int, default=1, metavar="N", help="run the job N times")
    p.add_argument("--fail-fast", action="store_true",
                   help="stop repeating after the first failure (also stops the hammer)")
    p.add_argument("--hammer", nargs="?", type=int, const=HAMMER_DEFAULT, default=None, metavar="N",
                   help=f"after the job's steps pass, run the tests the change touches N times "
                        f"(default {HAMMER_DEFAULT}) in the same clone, tools, environment and stress, "
                        "and count failures per test. A local addition, not part of the CI job")
    p.add_argument("--hammer-tests", nargs="+", default=[], metavar="FILE_OR_NODE",
                   help="hammer these test files or pytest nodes instead of the selection (implies --hammer)")
    p.add_argument("--base", metavar="REF",
                   help="--hammer selection: changes since this ref (default: the merge base with "
                        "origin/main, else the tested commit's parent)")
    p.add_argument("--hammer-max-files", type=int, default=HAMMER_MAX_FILES, metavar="N",
                   help=f"cap on selected test files (default {HAMMER_MAX_FILES}); --hammer-tests is not capped")
    p.add_argument("--hammer-only", action="store_true",
                   help="skip the job's test step and hammer straight away; the checkout, tools and "
                        "environment are still set up (implies --hammer). For re-checks, not for pre-push")
    p.add_argument("--dry-run", action="store_true", help="print the plan and run nothing")
    p.add_argument("--list", action="store_true", help="list the workflow's jobs")
    p.add_argument("--input", action="append", default=[], metavar="NAME=VALUE",
                   help="a workflow_call input (for example release=true)")
    p.add_argument("--cache-dir", type=Path, help="tool downloads and runs (default: the user cache)")
    p.add_argument("--keep-cache", action="store_true",
                   help="keep uv's cache, its Python installs and npm's cache between runs")
    p.add_argument("--keep", action="store_true", help="keep the workspace after a passing run")
    p.add_argument("--timeout-scale", type=float, default=1.0, metavar="F",
                   help="multiply the job's timeout-minutes (a cold uv cache downloads inside "
                        "the test step here, but is restored beforehand on a hosted runner)")
    p.add_argument("--allow-unavailable", action="append", default=[], metavar="STEP",
                   help="accept that the step with this exact name cannot run on this host "
                        "(repeatable); any other step that cannot run still gives exit code 3")
    p.add_argument("--trust-actions", action="store_true",
                   help="run although the workflow pins an action to a commit its emulation was "
                        "not checked against (ACTIONS in this script)")
    p.add_argument("-v", "--verbose", action="store_true", help="echo step output as it arrives")
    return p


def _parse_inputs(pairs: Sequence[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise RunnerError(f"--input expects NAME=VALUE, not {pair!r}")
        out[key] = value
    return out


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return _main(args)
    except UnsupportedWorkflow as exc:
        print(f"ci_local: unsupported: {exc}", file=sys.stderr)
        return 2
    except RunnerError as exc:
        print(f"ci_local: {exc}", file=sys.stderr)
        return 2
    except NothingTested as exc:
        print(f"ci_local: nothing was tested: {exc}", file=sys.stderr)
        return EXIT_NOTHING_TESTED
    except KeyboardInterrupt as exc:
        print("ci_local: interrupted", file=sys.stderr)
        return 128 + getattr(exc, "signum", signal.SIGINT)


def _main(args: argparse.Namespace) -> int:
    if args.repeat < 1:
        raise RunnerError("--repeat must be at least 1")
    if args.timeout_scale <= 0:
        raise RunnerError("--timeout-scale must be positive")
    hammering = args.hammer is not None or args.hammer_only or bool(args.hammer_tests)
    count = HAMMER_DEFAULT if args.hammer is None else args.hammer
    if hammering and count < 1:
        raise RunnerError("--hammer must be at least 1")
    if args.hammer_max_files < 1:
        raise RunnerError("--hammer-max-files must be at least 1")
    if not hammering and args.base:
        raise RunnerError("--base only belongs to --hammer")
    host = detect_host()
    workflow = load_workflow(args.workflow)
    if args.list:
        for job in workflow.jobs.values():
            kind = "aggregator, not runnable" if is_aggregator(job) else f"{runner_for(job.runs_on).os} runner"
            print(f"{job.id:<12} {job.runs_on:<16} {kind:<26} {job.name}")
        return 0
    job_id = args.job or default_job(workflow, host)
    plan = build_plan(workflow, job_id, host, inputs=_parse_inputs(args.input), stress=args.stress,
                      stress_priority=args.stress_priority, trust_actions=args.trust_actions)
    if plan.aggregator:
        raise RunnerError(f"{job_id} only aggregates the other jobs; run those instead")
    names = [s.name for s in plan.steps]
    for name in args.allow_unavailable:
        if name not in names:
            raise RunnerError(f"--allow-unavailable {name!r} is not a step of {job_id} "
                              f"(steps: {', '.join(repr(n) for n in names)})")
    others = [j.id for j in workflow.jobs.values() if j.id != job_id and not is_aggregator(j)
              and runner_for(j.runs_on).os == plan.runner.os]
    if not args.job and others:
        plan.notes.append(f"{job_id} was picked by OS; other {plan.runner.os} jobs: {', '.join(others)}")
    repo = args.repo.resolve()
    summary = worktree_summary(repo, args.ref)
    warnings = [] if args.ref else untracked_notice(untracked_files(repo))
    hammer = None
    if hammering:
        test_step = find_test_step(plan)
        selection = hammer_selection(repo, args.ref, args.base, args.hammer_tests, args.hammer_max_files)
        hammer = HammerRequest(count, selection, test_step, args.hammer_only)
    if args.dry_run:
        print("\n".join(format_plan(plan, summary, warnings)))
        if hammer:
            print("\n".join(format_selection(hammer.selection, hammer.count, hammer.only)))
        return 0
    if hammer and hammer.only and not hammer.selection.targets:
        # Skipping the job's test step and then hammering nothing would report PASSED for a run
        # that tested nothing, after minutes of setup: refuse before the first download.
        raise NothingTested("--hammer-only selected no tests and skips the job's own test step. "
                            "Name tests with --hammer-tests, move the base with --base, or drop "
                            "--hammer-only to run the job's tests.")

    cache = (args.cache_dir or default_cache_dir()).resolve()
    group = cache / "runs" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{job_id}"
    group.mkdir(parents=True)
    with open(group / "ci_local.log", "w", encoding="utf-8", newline="\n") as log_file:
        def say(line: str = "") -> None:
            print(line, flush=True)
            log_file.write(line + "\n")
            log_file.flush()

        say(f"run directory: {group}")
        for line in format_header(plan, summary, warnings):
            say(line)
        if hammer:
            for line in format_selection(hammer.selection, hammer.count, hammer.only):
                say(line)
        with terminate_on_signals():
            return _run_job(args, host, plan, repo, group, cache, say, hammer)


def _run_job(args: argparse.Namespace, host: Host, plan: Plan, repo: Path, group: Path,
             cache: Path, say: Callable[[str], None], hammer: HammerRequest | None = None) -> int:
    options = RunOptions(cache=cache, keep_cache=args.keep_cache, keep=args.keep, verbose=args.verbose,
                         timeout_scale=args.timeout_scale,
                         skip_steps=frozenset({hammer.test_step.index}) if hammer and hammer.only
                         else frozenset())
    per_run = (plan.timeout_minutes or 60) * 60 * args.timeout_scale
    hammer_result: HammerResult | None = None
    executors: list[JobExecutor] = []
    tools = ToolCache(cache, host, say)
    try:
        say("preparing the source under test ...")
        source = prepare_source(repo, group / "source", args.ref)
        say(f"  {source.description} (commit {source.commit[:7]})")
        for status, path in source.changes[:40]:
            say(f"    {status} {path}")
        if len(source.changes) > 40:
            say(f"    ... and {len(source.changes) - 40} more")
        max_seconds = per_run * (args.repeat + (hammer.count if hammer else 0)) + 600

        def once(number: int) -> JobResult:
            run_dir = group / f"r{number}"
            say(f"\n=== {plan.job_id}: repeat {number} of {args.repeat} ({run_dir}) ===")
            executor = JobExecutor(plan, source, run_dir, options, tools=tools, say=say)
            executors.append(executor)
            result = executor.run()
            keep_for_hammer = hammer is not None and number == args.repeat
            if not result.failed and not options.keep and not keep_for_hammer:
                for sub in ("workspace", "temp"):
                    if (run_dir / sub).exists() and not rmtree_force(run_dir / sub):
                        say(f"  note: could not delete {run_dir / sub}")
            return result

        with StressPool(plan.stress, group / "stress", max_seconds, priority=plan.stress_priority) as pool:
            if pool.count:
                say(f"stress: {pool.count} busy-loop process(es) running at {pool.priority} priority")
            results = run_repeats(args.repeat, once, args.fail_fast)
            if hammer:
                why = hammer_skip_reason(results, hammer.selection)
                if why:
                    hammer_result = HammerResult(hammer.count, skipped=why)
                else:
                    say(f"\n=== hammer (local addition): {hammer.count} repetition(s) of "
                        f"{len(hammer.selection.targets)} test file(s) or node(s) ===")
                    hammer_result = run_hammer(executors[-1], hammer, timeout=per_run,
                                               fail_fast=args.fail_fast)
                # The last workspace was kept for the hammer. Whatever the hammer did (ran, or had
                # nothing to run), a clean run drops it like any other; a failure keeps it.
                last = executors[-1].run_dir
                if not (options.keep or any(r.failed for r in results) or hammer_result.failed):
                    for sub in ("workspace", "temp"):
                        if (last / sub).exists() and not rmtree_force(last / sub):
                            say(f"  note: could not delete {last / sub}")
    finally:
        rmtree_force(group / "source")
        if os.name != "nt":      # anything of this run left behind, in any repeat
            with contextlib.suppress(Exception):
                kill_leftovers(f"{group}{os.sep}")

    code = exit_code(results, args.allow_unavailable, hammer_result)
    allowed = set(args.allow_unavailable)
    say("")
    for number, result in enumerate(results, 1):
        refused = [name for name in result.unavailable if name not in allowed]
        verdict = ("FAILED" if result.failed
                   else f"INCOMPLETE, cannot run here: {', '.join(refused)}" if refused
                   else f"PASSED, with steps not reproducible here: {', '.join(result.unavailable)}"
                   if result.unavailable else "PASSED")
        say(f"repeat {number}: {verdict} in {fmt_duration(result.seconds)}")
        for s in result.steps:
            note = f"  [{s.note}]" if s.note and s.status in {"failed", "not-reproducible"} else ""
            say(f"   {s.index:>2}. {s.status:<17} {fmt_duration(s.seconds):>8}  {s.name}{note}")
            for line in s.pytest:
                say(f"       pytest: {line}")
    missing = sorted({name for r in results for name in r.unavailable})
    verdict = {0: "PASSED" + (f" (not reproducible here, allowed by name: {', '.join(missing)})" if missing else ""),
               1: "FAILED",
               3: "INCOMPLETE: everything that could run passed, but a step cannot run on this host "
                  "(accept it by name with --allow-unavailable)"}[code]
    if hammer_result:
        for line in format_hammer(hammer_result):
            say(line)
        if code == 1 and not any(r.failed for r in results):
            verdict = "FAILED (the hammer, not the job's steps)"
    say(f"\n{plan.job_id}: {verdict} ({len(results)} of {args.repeat} repeat(s) run)")
    if source.untracked:
        say(f"NOTE: this run included {len(source.untracked)} untracked file(s) that a push would not "
            "carry. Run again with --ref HEAD to test what a push sends.")
    (group / "summary.json").write_text(json.dumps({
        "job": plan.job_id, "exit_code": code, "source": source.description,
        "commit": source.commit, "stress": plan.stress, "stress_priority": plan.stress_priority,
        "untracked": source.untracked, "allow_unavailable": list(args.allow_unavailable),
        "hammer": hammer_result and {
            "requested": hammer_result.requested, "completed": hammer_result.completed,
            "skipped": hammer_result.skipped, "tests": list(hammer.selection.targets) if hammer else [],
            "failures": {t: [dataclasses.asdict(f) for f in fs]
                         for t, fs in hammer_result.failures.items()}},
        "repeats": [{"seconds": round(r.seconds, 1), "steps": [dataclasses.asdict(s) for s in r.steps]}
                    for r in results]}, indent=2), encoding="utf-8")
    say(f"logs and summary: {group}")
    return code


if __name__ == "__main__":
    sys.exit(main())
