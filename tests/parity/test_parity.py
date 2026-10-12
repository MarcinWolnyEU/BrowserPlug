"""
ArchUnit-style parity validator for extension-gecko/ <-> extension-chromium/ (design.md D14).

Plain pytest, stdlib only, NO browser. Mechanically enforces that the Chrome MV3 port stays
in 1:1 correspondence with the Firefox MV2 extension and that the test suites cover both
engines, so future development cannot silently drift. The declarative registry is
tests/parity/parity.toml; every allowed divergence is recorded there with a reason.

Invariants (each its own test; each failure message tells a future agent exactly what to do):
  A  action-string set equality between the two background.js
  B  __browserplugAuto method-set equality (automation.js)
  C  shared files byte-identical + exhaustive tree file classification
  D  JS-test twin coverage, exhaustive in both directions
  E  slice-anchor existence for every anchor-slicing JS test
  F  manifest capability correspondence
  G  integration-module engine coverage
  H  manifest version lockstep
  I  verbatim-ported blocks stay code-identical between the trees
  J  every literal wire action the server sends is handled by BOTH background.js

Every extractor floor-guards against a vacuous parse: an empty match FAILS loudly rather
than passing by matching nothing.
"""
from __future__ import annotations

import pytest

import parity_lib as P


@pytest.fixture(scope="module")
def reg():
    return P.load_registry()


@pytest.fixture(scope="module")
def gecko_root(reg):
    return P.tree_dir(reg, "gecko")


@pytest.fixture(scope="module")
def chromium_root(reg):
    return P.tree_dir(reg, "chromium")


# =====================================================================================
# A. Action-string set parity between the two background.js
# =====================================================================================
def test_A_action_string_parity(reg, gecko_root, chromium_root):
    g = P.parse_action_strings(P.read_text(gecko_root / "background.js"))
    c = P.parse_action_strings(P.read_text(chromium_root / "background.js"))
    floor = reg["action_strings"]["floor"]

    assert len(g) >= floor, (
        f"Only {len(g)} action strings parsed from extension-gecko/background.js (floor "
        f"{floor}). The `case 'area.verb':` regex in parity_lib.parse_action_strings likely "
        f"rotted, or actions were removed. Do NOT lower the floor to paper over it."
    )
    assert len(c) >= floor, (
        f"Only {len(c)} action strings parsed from extension-chromium/background.js (floor "
        f"{floor}). Same diagnosis as the gecko side above."
    )

    exempt = set(reg["action_strings"].get("exempt", []))
    only_gecko = (g - c) - exempt
    only_chromium = (c - g) - exempt
    assert not only_gecko and not only_chromium, (
        "Action-string drift between the two background.js.\n"
        f"  only in gecko:    {sorted(only_gecko)}\n"
        f"  only in chromium: {sorted(only_chromium)}\n"
        "Either implement the missing action in the other tree's handleMessage switch, or — "
        "if it is legitimately engine-specific — add it to [action_strings].exempt in "
        "parity.toml WITH a reason in [action_strings.reasons]."
    )


# =====================================================================================
# B. __browserplugAuto method-set parity (automation.js is shared; this guards a future fork)
# =====================================================================================
def test_B_automation_method_parity(reg, gecko_root, chromium_root):
    g = P.parse_automation_methods(P.read_text(gecko_root / "automation.js"))
    c = P.parse_automation_methods(P.read_text(chromium_root / "automation.js"))
    floor = reg["automation_methods"]["floor"]

    assert len(g) >= floor, (
        f"Only {len(g)} __browserplugAuto methods parsed from extension-gecko/automation.js "
        f"(floor {floor}). The self.invoke switch anchors ({P._INVOKE_START!r} .. "
        f"{P._INVOKE_END!r}) or the `case 'name':` shape changed — fix the extractor, not "
        f"the floor."
    )
    assert g == c, (
        "The __browserplugAuto method set differs between trees:\n"
        f"  only in gecko:    {sorted(g - c)}\n"
        f"  only in chromium: {sorted(c - g)}\n"
        "automation.js is a shared byte-identical file (invariant C) — if these differ, the "
        "trees have forked. Re-sync automation.js so both trees carry the same invoke registry."
    )


# =====================================================================================
# C. Shared files byte-identical + exhaustive file classification
# =====================================================================================
def test_C_shared_files_and_classification(reg, gecko_root, chromium_root):
    shared_g = P.expand_shared(reg, gecko_root)
    shared_c = P.expand_shared(reg, chromium_root)

    assert shared_g == shared_c, (
        "The shared-file set differs between trees (a [shared] file/glob resolves in one tree "
        "but not the other):\n"
        f"  only in gecko:    {sorted(shared_g - shared_c)}\n"
        f"  only in chromium: {sorted(shared_c - shared_g)}\n"
        "Add the missing file to the other tree, or reclassify it in parity.toml."
    )
    assert len(shared_g) >= 30, (
        f"Only {len(shared_g)} shared files resolved (expected >= 30: 10 explicit + the "
        f"_locales/**/*.json set). The [shared].globs expansion in parity_lib.expand_shared "
        f"probably matched nothing — do not lower this floor."
    )

    mismatches = []
    for rel in sorted(shared_g):
        gp, cp = gecko_root / rel, chromium_root / rel
        if not gp.is_file() or not cp.is_file():
            mismatches.append(f"{rel}: missing in one tree")
        elif P.sha256_file(gp) != P.sha256_file(cp):
            mismatches.append(f"{rel}: sha256 differs")
    assert not mismatches, (
        "Shared files are NOT byte-identical between the trees:\n  " + "\n  ".join(mismatches) +
        "\nA [shared] file must be identical in both trees. Edits go in extension-gecko/ (the "
        "source of truth); to re-sync, run:  uv run --script tests/parity/sync_common.py"
        "  (copies gecko -> chromium; then re-run this suite). If it must instead diverge by "
        "engine, move it to [platform_twins] (present in both, allowed to differ) with a reason."
    )

    twins = set(reg["platform_twins"].keys())
    chromium_only = set(reg["chromium_only"].keys())
    gecko_only = set(reg["gecko_only"].keys()) if "gecko_only" in reg else set()

    # [gecko_only_globs] / [chromium_only_globs]: whole generated asset trees, classified by
    # pattern rather than by relpath. Same contract as the named platform-only files, and the
    # same two things have to hold — the pattern matches something, and nothing it matches
    # exists in the other tree.
    glob_only = {}
    for side, section, own_root, other_root, other_name in (
        ("gecko", "gecko_only_globs", gecko_root, chromium_root, "extension-chromium/"),
        ("chromium", "chromium_only_globs", chromium_root, gecko_root, "extension-gecko/"),
    ):
        matched: set[str] = set()
        for pattern, hits in P.expand_globs(reg, section, own_root).items():
            assert reg[section][pattern], f"[{section}] {pattern!r} needs a reason string."
            assert hits, (
                f"[{section}] {pattern!r} matched no file in the {side} tree. A pattern that "
                f"matches nothing classifies nothing — the files it was written for were "
                f"renamed or removed. Fix the pattern, or drop the row."
            )
            for rel in sorted(hits):
                assert not (other_root / rel).is_file(), (
                    f"[{section}] {pattern!r} matches {rel!r}, which ALSO exists in "
                    f"{other_name} — it is not {side}-only. Reclassify it (icons/app/*.png "
                    f"is the shared-art precedent)."
                )
            matched |= hits
        glob_only[side] = matched

    for rel in twins:
        assert (gecko_root / rel).is_file() and (chromium_root / rel).is_file(), (
            f"[platform_twins] lists {rel!r} but it is missing from one tree. A platform twin "
            f"must exist in BOTH trees (it just may differ)."
        )
    for rel in chromium_only:
        assert (chromium_root / rel).is_file(), f"[chromium_only] {rel!r} missing from extension-chromium/."
        assert not (gecko_root / rel).is_file(), (
            f"[chromium_only] {rel!r} also exists in extension-gecko/ — it is not chromium-only. "
            f"Reclassify it in parity.toml."
        )
        assert reg["chromium_only"][rel], f"[chromium_only] {rel!r} needs a reason string."
    for rel in gecko_only:
        assert (gecko_root / rel).is_file(), f"[gecko_only] {rel!r} missing from extension-gecko/."
        assert not (chromium_root / rel).is_file(), f"[gecko_only] {rel!r} also exists in extension-chromium/."

    gecko_files = P.list_tree_files(gecko_root)
    chromium_files = P.list_tree_files(chromium_root)
    unclassified_gecko = gecko_files - (shared_g | twins | gecko_only | glob_only["gecko"])
    unclassified_chromium = chromium_files - (shared_c | twins | chromium_only
                                              | glob_only["chromium"])
    assert not unclassified_gecko, (
        f"Unclassified files in extension-gecko/: {sorted(unclassified_gecko)}\n"
        "Every file must be in parity.toml as [shared], a [platform_twins] entry, or "
        "[gecko_only] / [gecko_only_globs] with a reason. Classify these before the port "
        "can be trusted."
    )
    assert not unclassified_chromium, (
        f"Unclassified files in extension-chromium/: {sorted(unclassified_chromium)}\n"
        "Every file must be in parity.toml as [shared], a [platform_twins] entry, or "
        "[chromium_only] / [chromium_only_globs] with a reason. Classify these before "
        "the port can be trusted."
    )


# =====================================================================================
# D. JS-test twin coverage — exhaustive in both directions
# =====================================================================================
def test_D_js_test_coverage(reg, gecko_root, chromium_root):
    gecko_tests = P.list_js_tests(P.JS_GECKO_DIR)
    chromium_tests = P.list_js_tests(P.JS_CHROMIUM_DIR)
    assert gecko_tests and chromium_tests, "no *.test.mjs discovered — glob rotted?"

    reg_gecko = reg["js_tests"]["gecko"]
    reg_chromium = reg["js_tests"]["chromium"]

    assert set(reg_gecko.keys()) == gecko_tests, (
        "tests/js/ and [js_tests.gecko] are out of sync:\n"
        f"  on disk but unclassified: {sorted(gecko_tests - set(reg_gecko))}\n"
        f"  classified but missing:   {sorted(set(reg_gecko) - gecko_tests)}\n"
        "Every tests/js/*.test.mjs needs a scope (shared/twin/gecko_only) in parity.toml."
    )
    assert set(reg_chromium.keys()) == chromium_tests, (
        "tests/js-chromium/ and [js_tests.chromium] are out of sync:\n"
        f"  on disk but unclassified: {sorted(chromium_tests - set(reg_chromium))}\n"
        f"  classified but missing:   {sorted(set(reg_chromium) - chromium_tests)}\n"
        "Every tests/js-chromium/*.test.mjs needs a scope (twin/chromium_only) in parity.toml."
    )

    shared_g = P.expand_shared(reg, gecko_root)
    platform_twins = set(reg["platform_twins"].keys())

    # shared-scope gecko tests must only read shared or platform-twin extension files.
    for name, info in reg_gecko.items():
        scope = info.get("scope")
        if scope == "shared":
            reads = P.parse_ext_reads(P.read_text(P.JS_GECKO_DIR / name))
            for _var, _tree, rel in reads:
                assert rel in shared_g or rel in platform_twins, (
                    f"{name} is scope=shared but reads {rel!r}, which is neither a [shared] "
                    f"file nor a [platform_twins] file. A shared test must cover both engines "
                    f"by construction — reclassify the test as twin/gecko_only, or make {rel} "
                    f"shared."
                )
        elif scope == "gecko_only":
            assert info.get("reason"), f"{name} is scope=gecko_only but has no reason= in parity.toml."
        elif scope != "twin":
            pytest.fail(f"{name}: unknown scope {scope!r} in [js_tests.gecko].")

    for name, info in reg_chromium.items():
        scope = info.get("scope")
        if scope == "chromium_only":
            assert info.get("reason"), f"{name} is scope=chromium_only but has no reason= in parity.toml."
        elif scope != "twin":
            pytest.fail(f"{name}: unknown scope {scope!r} in [js_tests.chromium].")

    # Twin map: keys == gecko twin-scoped tests; values == chromium twin-scoped tests.
    twins = reg["twins"]
    gecko_twin_scoped = {n for n, i in reg_gecko.items() if i.get("scope") == "twin"}
    chromium_twin_scoped = {n for n, i in reg_chromium.items() if i.get("scope") == "twin"}
    assert set(twins.keys()) == gecko_twin_scoped, (
        "[twins] keys must match the twin-scoped gecko tests:\n"
        f"  in map, not twin-scoped: {sorted(set(twins) - gecko_twin_scoped)}\n"
        f"  twin-scoped, not mapped: {sorted(gecko_twin_scoped - set(twins))}"
    )
    assert set(twins.values()) == chromium_twin_scoped, (
        "[twins] values must match the twin-scoped chromium tests:\n"
        f"  in map, not twin-scoped: {sorted(set(twins.values()) - chromium_twin_scoped)}\n"
        f"  twin-scoped, not mapped: {sorted(chromium_twin_scoped - set(twins.values()))}"
    )

    for g_name, c_name in twins.items():
        gpath = P.JS_GECKO_DIR / g_name
        cpath = P.JS_CHROMIUM_DIR / c_name
        assert gpath.is_file(), f"[twins] gecko test {g_name} missing from tests/js/."
        assert cpath.is_file(), f"[twins] chromium twin {c_name} missing from tests/js-chromium/."
        g_trees = {t for _v, t, _r in P.parse_ext_reads(P.read_text(gpath))}
        c_trees = {t for _v, t, _r in P.parse_ext_reads(P.read_text(cpath))}
        assert "extension-gecko" in g_trees, f"{g_name} does not read extension-gecko/."
        assert "extension-chromium" in c_trees, f"{c_name} does not read extension-chromium/."


# =====================================================================================
# E. Slice-anchor existence — every anchor a slicing test depends on exists in the source
# =====================================================================================
def test_E_slice_anchor_existence(reg):
    src_cache: dict[str, str] = {}

    def source(tree: str, rel: str) -> str:
        key = f"{tree}/{rel}"
        if key not in src_cache:
            src_cache[key] = P.read_text(P.ROOT / tree / rel)
        return src_cache[key]

    reg_gecko = reg["js_tests"]["gecko"]
    total_anchors = 0
    checked_tests = 0

    for directory, side in ((P.JS_GECKO_DIR, "gecko"), (P.JS_CHROMIUM_DIR, "chromium")):
        for path in sorted(directory.glob("*.test.mjs")):
            text = P.read_text(path)
            reads = P.parse_ext_reads(text)
            # Only tests that slice a background.js are anchor-bearing.
            if not any(rel == "background.js" for _v, _t, rel in reads):
                continue
            checked_tests += 1
            source_vars = {var for var, _t, _r in reads}
            anchors = P.parse_anchors(text, source_vars)
            assert anchors, (
                f"{path.name} ({side}) reads a background.js but no slice anchors were "
                f"extracted. The slicing shape changed (indexOf / the local slice() helper), "
                f"or parity_lib.parse_anchors rotted — fix the extractor, not this assert."
            )
            total_anchors += len(anchors)

            read_contents = [source(t, rel) for _v, t, rel in reads]
            for anchor in anchors:
                assert any(anchor in c for c in read_contents), (
                    f"Slice anchor {anchor!r} used by {path.name} ({side}) is NOT present in "
                    f"the source it reads ({[t + '/' + r for _v, t, r in reads]}). The anchor "
                    f"drifted — restore the exact comment/function text in the source, or update "
                    f"the anchor in the test. This is the brittle-slicer guard."
                )

            # Cross-tree guard: a SHARED gecko test that slices background.js (a diverging
            # file) — today popup.test.mjs (updateActionIndicator..setConnectionPhase) — must
            # have its anchors in the chromium background too, since no chromium twin covers it.
            if side == "gecko" and reg_gecko.get(path.name, {}).get("scope") == "shared":
                chromium_bg = source("extension-chromium", "background.js")
                for anchor in anchors:
                    assert anchor in chromium_bg, (
                        f"{path.name} is a shared test that slices extension-gecko/background.js "
                        f"by {anchor!r}, but extension-chromium/background.js lacks that anchor. "
                        f"The shared test only runs against gecko, so the chromium badge/handler "
                        f"logic would drift uncaught. Restore the anchor in the chromium port."
                    )

    assert checked_tests >= 10, (
        f"Only {checked_tests} background-slicing tests were found (expected >= 10). The "
        f"tests/js + tests/js-chromium glob or the readFileSync parse rotted."
    )
    assert total_anchors >= 20, (
        f"Only {total_anchors} total slice anchors extracted across all slicing tests "
        f"(expected >= 20). parity_lib.parse_anchors is under-matching — fix it."
    )


# =====================================================================================
# F. Manifest capability correspondence
# =====================================================================================
def test_F_manifest_capabilities(reg, gecko_root, chromium_root):
    gm = P.parse_manifest(gecko_root)
    cm = P.parse_manifest(chromium_root)
    g_perms = P.manifest_permissions(gm)
    c_perms = set(cm.get("permissions", []))
    c_hosts = set(cm.get("host_permissions", []))

    m = reg["manifest"]
    gmap = m["gecko_permissions"]
    additions = m["chromium_additions"]
    host_additions = m["chromium_host_additions"]

    assert len(g_perms) >= 10, (
        f"Only {len(g_perms)} gecko permissions parsed (floor 10). manifest.json parse broke."
    )
    assert set(gmap.keys()) == g_perms, (
        "[manifest.gecko_permissions] and extension-gecko/manifest.json are out of sync:\n"
        f"  in manifest, not mapped: {sorted(g_perms - set(gmap))}\n"
        f"  mapped, not in manifest: {sorted(set(gmap) - g_perms)}\n"
        "Every gecko permission (permissions + optional_permissions) needs a row mapping it to "
        "its chromium disposition."
    )

    mapped_targets: set[str] = set()
    for perm, info in gmap.items():
        target = info["chromium"]
        if target == "absent":
            assert info.get("reason"), f"gecko permission {perm} maps to absent but has no reason=."
            assert perm not in c_perms, (
                f"{perm} is declared absent-by-design but IS present in the chromium manifest. "
                f"Remove it from extension-chromium/manifest.json or reclassify it in parity.toml."
            )
        elif target == "host_permissions":
            assert info.get("reason"), f"gecko permission {perm} maps to host_permissions but has no reason=."
            assert "<all_urls>" in c_hosts, (
                f"{perm} should move to chromium host_permissions:<all_urls>, but <all_urls> is "
                f"not in the chromium manifest host_permissions."
            )
        else:
            assert target in c_perms, (
                f"gecko permission {perm} maps to chromium {target!r}, but extension-chromium/"
                f"manifest.json does not declare it. Add {target} or fix the mapping."
            )
            mapped_targets.add(target)

    for cp in c_perms:
        assert cp in mapped_targets or cp in additions, (
            f"chromium permission {cp!r} is unaccounted for: it is neither the target of a "
            f"gecko-permission mapping nor listed in [manifest.chromium_additions]. Add it as a "
            f"chromium addition WITH a reason, or map a gecko permission to it."
        )
    for ap, reason in additions.items():
        assert ap in c_perms, f"[manifest.chromium_additions] {ap!r} is not in the chromium manifest."
        assert ap not in mapped_targets, f"[manifest.chromium_additions] {ap!r} is also a gecko map target."
        assert reason, f"[manifest.chromium_additions] {ap!r} needs a reason string."

    non_allurls_hosts = c_hosts - {"<all_urls>"}
    assert non_allurls_hosts == set(host_additions.keys()), (
        "chromium host_permissions beyond <all_urls> are out of sync with "
        "[manifest.chromium_host_additions]:\n"
        f"  in manifest, not registered: {sorted(non_allurls_hosts - set(host_additions))}\n"
        f"  registered, not in manifest: {sorted(set(host_additions) - non_allurls_hosts)}"
    )
    assert "<all_urls>" in c_hosts, "chromium manifest host_permissions is missing <all_urls>."
    for h, reason in host_additions.items():
        assert reason, f"[manifest.chromium_host_additions] {h!r} needs a reason string."

    for key, info in m["key_mapping"].items():
        assert info.get("reason"), f"[manifest.key_mapping] {key!r} needs a reason string."
    assert "scripts" in gm["background"] and "service_worker" in cm["background"], (
        "background key mismatch: gecko must use background.scripts (MV2), chromium must use "
        "background.service_worker (MV3)."
    )
    assert "browser_action" in gm and "action" in cm, "browser_action (gecko) / action (chromium) key missing."
    assert "sidebar_action" in gm and "side_panel" in cm, "sidebar_action (gecko) / side_panel (chromium) key missing."

    assert gm["manifest_version"] == m["gecko_manifest_version"] == 2
    assert cm["manifest_version"] == m["chromium_manifest_version"] == 3


# =====================================================================================
# G. Integration-module engine coverage
# =====================================================================================
def test_G_integration_coverage(reg):
    modules = P.list_integration_modules()
    assert modules, "no tests/integration/test_*.py discovered — glob rotted?"

    integ = reg["integration"]
    for mod in integ:
        assert mod in modules, (
            f"parity.toml [integration] lists {mod!r} but it is not a real integration module "
            f"(stale entry). Remove or fix it."
        )
        assert integ[mod].get("reason"), f"[integration] {mod!r} needs a reason=."

    used_fixture = 0
    for mod in sorted(modules):
        text = P.read_text(P.INTEGRATION_DIR / mod)
        uses = P.module_uses_swe(text)
        if uses:
            used_fixture += 1
        mlvl = P.module_level_engine_marker(text)
        exempt = mod in integ

        if mlvl is not None:
            assert exempt and integ[mod].get("scope") == "module", (
                f"{mod} carries a MODULE-LEVEL {mlvl}_only marker (it does NOT run under the "
                f"other engine) but is not registered as a module exemption in parity.toml "
                f"[integration] (scope=\"module\"). Add it with a reason."
            )

        assert uses or exempt, (
            f"{mod} neither consumes the parametrized server_with_extension fixture nor is "
            f"registered in parity.toml [integration]. Parametrize it over the browser engine "
            f"(use server_with_extension / mcp / full_mcp_system-that-chains-to-it), or add an "
            f"[integration] exemption with a reason (e.g. server-only, or inline-Firefox)."
        )

        for name, has_reason in P.engine_marker_calls(text):
            assert has_reason, (
                f"a @pytest.mark.{name} in {mod} carries no reason string. The convention "
                f"(pytest.ini) is a REASON as the first positional arg, e.g. "
                f"@pytest.mark.{name}(\"why this engine only\")."
            )

        # Engine-bound BY CONSTRUCTION: a test that builds its own <Engine>TestManager runs
        # on one engine with no marker to deselect on, so neither the module-level check
        # above nor pytest's deselect count can see it. Declare each one explicitly.
        detected = P.inline_engine_bound_tests(text)
        declared = set(integ.get(mod, {}).get("inline_engine_tests", []))
        assert set(detected) == declared, (
            f"{mod}: inline_engine_tests in parity.toml [integration] does not match reality.\n"
            f"  builds its own browser (detected): {sorted(detected) or '(none)'}\n"
            f"  declared in parity.toml:           {sorted(declared) or '(none)'}\n"
            f"  undeclared (runs on ONE engine, invisibly): {sorted(set(detected) - declared) or '(none)'}\n"
            f"  stale declarations (no longer build a browser): {sorted(declared - set(detected)) or '(none)'}\n"
            f"Either parametrize the test over the engine (take server_with_extension), or "
            f"list it in [integration].\"{mod}\".inline_engine_tests with an engine= and a reason=."
        )
        if detected:
            engines = set(detected.values())
            assert len(engines) == 1 and integ[mod].get("engine") in engines, (
                f"{mod}: its inline_engine_tests build {sorted(engines)} browsers but the "
                f"[integration] entry says engine={integ[mod].get('engine')!r}. They must agree "
                f"(split the entry if a module ever pins tests to both engines)."
            )

    assert used_fixture >= 1, (
        "No integration module was detected as using server_with_extension — the "
        "module_uses_swe grep rotted (it should match many modules)."
    )


# =====================================================================================
# H. Component version lockstep (both manifests + the Python side)
# =====================================================================================
def test_H_version_lockstep(gecko_root, chromium_root):
    gv = P.parse_manifest(gecko_root)["version"]
    cv = P.parse_manifest(chromium_root)["version"]
    assert gv == cv, (
        f"Manifest version mismatch: gecko {gv!r} vs chromium {cv!r}. Both extensions must move "
        f"in lockstep (design.md D15/AGENTS.md bump rule). Set both manifest.json \"version\" "
        f"fields to the same value."
    )

    # The server and the launcher share ONE number (shared-py/browserplug_shared/version.py), and it moves with
    # the extensions: they speak a single wire contract, so a build where they disagree is
    # exactly the skew /health warns about — it must never be the state of the repo itself.
    sv = P.parse_python_version()
    assert sv == gv, (
        f"Python/extension version mismatch: shared-py/browserplug_shared/version.py {sv!r} vs manifests {gv!r}. "
        f"All THREE carriers move together — set __version__ in shared-py/browserplug_shared/version.py and the "
        f"\"version\" field in both manifest.json files to the same value."
    )

    # package.json is a fourth carrier. Nothing consumes it, so it can only drift silently.
    pv = P.parse_package_json_version()
    assert pv == gv, (
        f"package.json version {pv!r} differs from the shared version {gv!r}. Set \"version\" "
        f"in package.json to the same value as the other three carriers."
    )


# =====================================================================================
# I. Verbatim-ported blocks stay code-identical between the trees
# =====================================================================================
def test_I_identical_blocks_have_not_drifted(reg, gecko_root, chromium_root):
    """Blocks that were ported to be identical must still BE identical.

    Invariant E proves a slice anchor still exists; it says nothing about what sits
    between the anchors. A block that drifts there gives one engine different semantics
    while BOTH trees' twin suites stay green — each twin only ever reads its own copy.
    """
    entries = reg.get("identical_blocks") or []
    assert entries, (
        "parity.toml declares no [[identical_blocks]]. At least the content.act deadline "
        "block is supposed to be listed — the registry was gutted, or the loader stopped "
        "reading array-of-tables."
    )
    for entry in entries:
        rel = entry["file"]
        gsrc = P.read_text(gecko_root / rel)
        csrc = P.read_text(chromium_root / rel)
        gblock = P.slice_block(gsrc, entry["start"], entry["end"])
        cblock = P.slice_block(csrc, entry["start"], entry["end"])
        for side, blk in (("gecko", gblock), ("chromium", cblock)):
            assert blk.strip(), (
                f"identical_block {entry['name']!r}: the slice {entry['start']!r} .. "
                f"{entry['end']!r} came back EMPTY in the {side} {rel}. The anchors moved or "
                f"were renamed — restore them (invariant E depends on the same text), or "
                f"update parity.toml if the block genuinely went away."
            )
        gl, cl = P.code_lines(gblock), P.code_lines(cblock)
        # Floor-guard: a comparison of two near-empty blocks proves nothing.
        assert len(gl) >= 20, (
            f"identical_block {entry['name']!r} reduced to only {len(gl)} code lines in gecko — "
            f"the slice or parity_lib.code_lines is under-matching. Fix the extractor."
        )
        if gl != cl:
            import difflib
            diff = "\n".join(difflib.unified_diff(gl, cl, "gecko", "chromium", lineterm="", n=2))
            pytest.fail(
                f"identical_block {entry['name']!r} has DRIFTED between the trees:\n{diff}\n\n"
                f"Reason it must not: {entry['reason']}\n"
                f"Fix the code in whichever tree is wrong (this block is ported verbatim; only "
                f"whole-line // comments may differ, so an engine-specific note needs its own "
                f"comment line). If the divergence is genuinely required, move the block out of "
                f"[[identical_blocks]] in parity.toml WITH a reason."
            )


# =====================================================================================
# J. Every wire action the server sends is handled by both extensions
# =====================================================================================
def test_J_server_sent_actions_are_handled_by_both_extensions(reg, gecko_root, chromium_root):
    """Invariant A compares the two trees with each other, never with the server.

    A misspelt action in `server/tools/` would ride through every unit test (the fakes
    used to answer anything) and fail only in a real browser, as an unknown-action error.
    """
    sent = P.parse_server_sent_actions()
    floor = reg["server_sent_actions"]["floor"]
    assert len(sent) >= floor, (
        f"Only {len(sent)} literal wire actions found in server/ (floor {floor}). The "
        f"extractor in parity_lib.parse_server_sent_actions stopped matching; fix it, do "
        f"not lower the floor."
    )
    handled = {
        "gecko": P.parse_action_strings(P.read_text(gecko_root / "background.js")),
        "chromium": P.parse_action_strings(P.read_text(chromium_root / "background.js")),
    }
    exempt = set(reg["action_strings"].get("exempt", []))  # fail closed, so still handled
    problems = []
    for action, where in sorted(sent.items()):
        for tree, actions in handled.items():
            if action not in actions and action not in exempt:
                problems.append(f"  {action!r} (sent at {', '.join(where)}) has no case in "
                                f"extension-{tree}/background.js")
    assert not problems, (
        "The server sends wire actions the extensions do not handle:\n" + "\n".join(problems)
        + "\nFix the spelling in server/, or add the `case` to both background.js files "
        "(.tmp/dev-docs/parity-enforcement.md, 'Adding a wire action end to end')."
    )


def test_J_dynamic_request_sites_are_declared(reg):
    """A `.request(<variable>)` call is invisible to the extractor; each must be listed."""
    declared = set(reg["server_sent_actions"]["dynamic_sites"])
    found = {site.rsplit(":", 1)[0] for site in P.parse_dynamic_request_sites()}
    assert found == declared, (
        f"Call sites whose action is a variable changed.\n"
        f"  undeclared: {sorted(found - declared)}\n"
        f"  declared but gone: {sorted(declared - found)}\n"
        "Check every value the new site can send is a literal the extractor reads, then "
        "list the file in [server_sent_actions.dynamic_sites] in parity.toml with a reason."
    )
