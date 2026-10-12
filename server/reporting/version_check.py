#!/usr/bin/env python3
"""Version-skew detection across the three BrowserPlug components (server, launcher, extension).

Pure functions, stdlib only, no server state: the caller collects who is connected and
what each of them reports, this module turns that into the ONE plain-text warning
/health carries (`version_warning`, null when everything matches).

Plain text on purpose — the audience is an agent reading /health (or server_info),
so the sentence tells it what to do rather than making it interpret a structure:

    Browser Zen Browser (PID 38288) has a newer version (1.7.6) than the server (1.7.5),
    Launcher for Claude Code (PID 12345) has an older version (1.7.4) than the server
    (1.7.5), after finishing the current tasks notify the user that he should update the
    server to 1.7.6, version mismatch can cause unpredictable issues

(one line; wrapped here only for reading). The closing "update the server to X" clause
appears ONLY when something is NEWER than the server — that is the case where the fix is
to move the server, and X is the newest version seen anywhere.

Parties that would repeat a clause word for word are merged, because a machine running
seven Claude Code sessions carries seven launchers and the per-party sentence buried
what a reader is after (which versions are out there, and which pids to restart):

    Launchers for Claude Code (PIDs 1464, 36620) have an older version (1.7.31) ...
    Browsers Firefox (PID 1) and Zen Browser (PID 2) have a newer version (1.7.6) ...
    Launchers for Claude Code have older versions than the server (1.7.32):
        1.7.31 (PIDs 1464, 36620), 1.7.28 (PID 24616)

The last shape is for one name whose own copies disagree; there the versions carry the
pids, so the mapping from a pid to its version survives the merge in every case.

A party whose version we cannot read is skipped rather than guessed at, with ONE
deliberate exception: a launcher that reports no version at all (`unversioned=True`)
predates version reporting itself, so it is *known* to be older and says so.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Wording, in one place: these strings are the contract with whoever reads /health.
_UNVERSIONED_TEXT = "unknown — predates version reporting"
_TAIL = ("after finishing the current tasks notify the user that he should update the "
         "server to {newest}, version mismatch can cause unpredictable issues")

_LEADING_INT = re.compile(r"^(\d+)")


def parse_version(value: Any) -> Optional[Tuple[int, ...]]:
    """"1.7.5" -> (1, 7, 5). None when there is nothing comparable to read.

    Tolerant of a leading "v" and of a trailing pre-release tag ("1.7.5-beta", "1.7.5b"
    both read as (1, 7, 5)): the components ship plain dotted numbers, and guessing at
    tag ordering would be less honest than treating those two as the same release.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("vV")
    if not text:
        return None
    out: List[int] = []
    for part in text.replace("-", ".").split("."):
        m = _LEADING_INT.match(part)
        if not m:
            break
        out.append(int(m.group(1)))
    return tuple(out) or None


def compare_versions(a: Any, b: Any) -> Optional[int]:
    """-1 / 0 / 1 comparing version strings a and b; None if either is unreadable."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    width = max(len(pa), len(pb))
    pa += (0,) * (width - len(pa))
    pb += (0,) * (width - len(pb))
    return -1 if pa < pb else (1 if pa > pb else 0)


def _pids_text(pids: Sequence[Any]) -> str:
    """"PID 12345", or "PIDs 1464, 6904, 36620" once a group holds more than one."""
    labels = [str(p) if p not in (None, "") else "unknown" for p in pids]
    return ("PID " if len(labels) == 1 else "PIDs ") + ", ".join(labels)


def _pid_sort_key(pid: Any) -> Tuple[int, int, str]:
    """Numeric where a pid is numeric, so 6904 lands before 36620 rather than after it."""
    try:
        return (0, int(pid), "")
    except (TypeError, ValueError):
        return (1, 0, str(pid or ""))


def _prefix(kind: str, plural: bool, named: bool) -> str:
    """The noun a clause opens with: "Browser", "Browsers", "Launchers for", ..."""
    if kind == "launcher":
        base = "Launchers" if plural else "Launcher"
        return f"{base} for" if named else base
    return "Browsers" if plural else "Browser"


def _join_and(items: Sequence[str]) -> str:
    """"A, B and C" — the last separator is a word because clauses are comma-joined."""
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _same_version_clause(kind: str, relation: str, version_text: str,
                         entries: Sequence[Tuple[str, Sequence[Any]]],
                         server_version: str) -> str:
    """Everyone of one kind sitting on the SAME version, in one clause.

        Browser Zen Browser (PID 38288) has a newer version (1.7.6) than the server (1.7.5)
        Launchers for Claude Code (PIDs 1464, 36620) have an older version (1.7.31) than
        the server (1.7.32)
        Browsers Firefox (PID 1) and Zen Browser (PID 2) have a newer version (1.7.6) than
        the server (1.7.5)
    """
    plural = len(entries) > 1 or sum(len(pids) for _, pids in entries) > 1
    who = _prefix(kind, plural, any(name for name, _ in entries))
    listed = _join_and([f"{name} ({_pids_text(pids)})".strip() for name, pids in entries])
    article = "a newer" if relation == "newer" else "an older"
    verb = "have" if plural else "has"
    return (f"{who} {listed} {verb} {article} version ({version_text}) than the "
            f"server ({server_version})")


def _spread_clause(kind: str, relation: str, name: str,
                   versions: Sequence[Tuple[str, Sequence[Any]]],
                   server_version: str) -> str:
    """One name whose copies disagree with each other — the versions carry the pids.

        Launchers for Claude Code have older versions than the server (1.7.32):
        1.7.31 (PIDs 1464, 36620), 1.7.28 (PID 24616)
    """
    who = f"{_prefix(kind, True, bool(name))} {name}".strip()
    listed = ", ".join(f"{v} ({_pids_text(pids)})" for v, pids in versions)
    return (f"{who} have {relation} versions than the server ({server_version}): {listed}")


def build_version_warning(server_version: str,
                          parties: Sequence[Dict[str, Any]]) -> Optional[str]:
    """The /health `version_warning` string, or None when nothing is out of step.

    server_version: this process's version (shared-py/browserplug_shared/version.py).
    parties: dicts of {kind: "browser"|"launcher", name, pid, version, unversioned?}.
        Order does not matter — the output is sorted (newer first, then browsers before
        launchers, then newest version first) so the same fleet always produces the same
        sentence.

    Parties that would otherwise repeat a clause word for word are merged into one:
    seven Claude Code launchers behind seven Claude Code sessions is the ordinary state
    of this machine, and seven near-identical sentences buried the two facts a reader
    needs — which versions are out there, and which pids to restart.
    """
    if not parse_version(server_version):
        return None  # nothing to compare against; never warn on our own bad input

    # (relation, kind) -> name -> version text -> [pid, ...]
    groups: Dict[Tuple[str, str], Dict[str, Dict[str, List[Any]]]] = {}
    newest = server_version

    for party in parties or []:
        kind = "launcher" if party.get("kind") == "launcher" else "browser"
        name = str(party.get("name") or "").strip()
        version = party.get("version")
        if parse_version(version):
            cmp = compare_versions(version, server_version)
            if cmp is None or cmp == 0:
                continue
            relation, version_text = ("newer" if cmp > 0 else "older"), version
            if cmp > 0 and compare_versions(version, newest) == 1:
                newest = version
        elif party.get("unversioned") and kind == "launcher":
            # Only a launcher old enough to have no version at all is reported blind:
            # its silence IS the evidence that it is older than this server.
            relation, version_text = "older", _UNVERSIONED_TEXT
        else:
            continue
        by_name = groups.setdefault((relation, kind), {})
        by_name.setdefault(name, {}).setdefault(version_text, []).append(party.get("pid"))

    if not groups:
        return None

    clauses: List[str] = []
    for relation, kind in sorted(groups, key=lambda k: (0 if k[0] == "newer" else 1,
                                                        0 if k[1] == "browser" else 1)):
        by_name = groups[(relation, kind)]
        pending: List[Tuple[Tuple[int, ...], str, str]] = []  # (top version, name, clause)

        # A name whose own copies disagree gets its own clause; the rest are collected
        # by version, so two browsers on one version read as one sentence.
        same_version: Dict[str, List[Tuple[str, List[Any]]]] = {}
        for name in sorted(by_name):
            versions = [(v, sorted(dict.fromkeys(pids), key=_pid_sort_key))
                        for v, pids in by_name[name].items()]
            # Newest first; an unversioned launcher parses to nothing and lands last.
            versions.sort(key=lambda kv: parse_version(kv[0]) or (), reverse=True)
            if len(versions) > 1:
                pending.append((parse_version(versions[0][0]) or (), name,
                                _spread_clause(kind, relation, name, versions,
                                               server_version)))
            else:
                version_text, pids = versions[0]
                same_version.setdefault(version_text, []).append((name, pids))
        for version_text, entries in same_version.items():
            pending.append((parse_version(version_text) or (), entries[0][0],
                            _same_version_clause(kind, relation, version_text, entries,
                                                 server_version)))

        pending.sort(key=lambda item: item[1])            # by name, then
        pending.sort(key=lambda item: item[0], reverse=True)  # newest version first
        clauses.extend(clause for _, _, clause in pending)

    if any(relation == "newer" for relation, _ in groups):
        clauses.append(_TAIL.format(newest=newest))
    return ", ".join(clauses)
