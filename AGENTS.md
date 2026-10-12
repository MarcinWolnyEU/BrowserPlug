# BrowserPlug: working notes for Claude

BrowserPlug exposes the user's live, logged-in browser to MCP clients: a browser extension
holds one WebSocket to a shared Python MCP server, which turns nine MCP tools into requests
to that extension. Driving the real session, cookies and all, is the point. Protect it in
every change, and never let a test profile or a browser you started reach the user's live
server.

Two extension trees share one wire contract: `extension-gecko/` (Firefox and forks such as
Zen and LibreWolf; Manifest V2, persistent background page) and `extension-chromium/`
(Chrome, Edge, Brave; Manifest V3, service worker). The server keys browsers by
`instance_id` and reads `engine` (`"gecko"` or `"blink"`) where behaviour differs.

## Where knowledge lives

This file is loaded into every session, so it holds only what most sessions need.
Everything else goes where it will be read:

- `.tmp/dev-docs/`: one mid-level file per subsystem (how it works, contracts, why). A doc
  wins where it and this file disagree; read it before changing its subsystem. `.tmp/` is
  git-ignored, so these files exist only in this checkout.
- `docs/`: guides for people who install and use BrowserPlug, no developer-only facts. A
  change to something a user sees or sets updates the guide and the dev doc.
- Code comments: low-level detail beside the code, stating intent, constraint or trap,
  never narrating the next line.
- `.tmp/`: dated research and investigation write-ups. History goes there or nowhere, never
  into `.tmp/dev-docs/`, `docs/` or this file.

The dev docs (a bare `name.md` in this file means `.tmp/dev-docs/name.md`):

| File | Owns |
|---|---|
| `ports.md` | every port, the seek, Windows socket rules, stray ports |
| `server-operations.md` | repo layout, process pair, `server_env`, `control.py`, restarts, watchdog, logging, state, HTTP endpoints, env vars, version skew |
| `launcher.md` | stdio relay, deadlines, recovery, spawn and adopt, proxy transport, tool-list refresh |
| `mcp-clients.md` | client naming; this machine's client registrations |
| `ws-token-auth.md` | token auth and bootstrap, hello window, pre-auth limits, HTTP gate, bind panels |
| `ws-encryption.md` | encrypted extension WebSocket, signed HTTP requests |
| `predefined-scripts.md` | `page_eval`'s `script_name` mode, samples, tests |
| `multi-browser.md` | registry, routing, default browser, routing-change notice, `browsers` tool, fork identification |
| `connection-diagnostics.md` | why a browser dropped: departure log line, journal, cached hello |
| `tool-surface.md` | tools and wire, dispatch, validation, response hints, failures, admission and retry, per-tool behaviour |
| `tool-economy.md` | description token budget: measuring, the Qwen 3.5 9B floor, where a fix goes, the tool eval harness, validation texts |
| `gecko-platform.md` | Firefox (MV2): injection, capture, body capture, net errors, focus, `file://` |
| `chromium-port.md` | Chromium (MV3) mechanics, test harness |
| `firefox-android.md` | what Firefox for Android lacks, how the extension copes |
| `gecko-chromium-feature-parity.md` | parity ledger: matches, degradations, how each gap fails |
| `parity-enforcement.md` | `tests/parity`: ten invariants, shared files, twin tests, adding an action |
| `proxy-connections.md` | SOCKS routing per engine, the launcher's proxy-blind transport |
| `tor.md` | `server/tor/`: managed Tor client, bundles, Linux Docker test |
| `extension-ui.md` | popup and options structure, UI surfaces, `storage.sync` traps, toolbar icon |
| `extension-popup-js.md` | the popup's Connections panel |
| `usage-panels.md` | provider usage panels, background sampler |
| `connection-map.md` | Options ▸ Health connection map |
| `testing.md` | test layers, runner, MCP harness, e2e tests, static gate, Firefox harness, running rules, blind spots |
| `dependencies.md` | PEP 723 blocks, locks, floors checker, mcp ceiling, Python versions, upgrading |
| `firefox-release.md` | AMO: lint bar, vendored defuddle, source zip |
| `chrome-web-store.md` | Chromium release |
| `store-listing.md` | store listing text, Chrome dashboard answers, AMO reviewer notes, store images |
| `browser-versions.md` | minimum browser versions, how to re-derive them |
| `ci-release.md` | GitHub Actions: CI, security checks, release and attestation, rulesets, first-push checklist |

Icon art: `assets/icons/README.md`. `README.md` is the public front page and quick start,
and `docs/install.md` the full install guide; do not duplicate either here.

## Working with this user

These are the user's standing instructions.

- **This project keeps zero agent memories.** Architecture, lessons, gotchas, decisions and
  preferences go into this file, `docs/`, code comments or `.tmp/*.md`, where the user and
  every tool can see them. If you are about to save a memory, write it into the repo.
- A new feature gets its own module; a core file gets a minimal hook. On the server the
  substance is a pure, stdlib-only policy module unit-tested without a browser (pattern:
  `server/reporting/wan_diagnosis.py`, `server/browsers/request_queue.py`,
  `server/tools/url_diff.py`); `server/server.py`, `server/tools/mcp_tools.py`,
  `options.js` and `background.js` get an import and a line or two of dispatch. In the
  extension, a shared file that self-registers on `globalThis` beats a block of logic in
  `options.js`; register it in `tests/parity/parity.toml` and sync it to chromium.
- Check a capability against the user's actual browser before designing around it: take a
  screenshot of the real screen or ask, and gate version-specific guidance on the version.
  A design built on a settings screen the user's Firefox does not have gets thrown away.
- Verify a subagent's claimed work on disk before acting on it. Subagents sometimes return
  junk after completing real work.
- Subagents run on Sonnet or Haiku (the current 5.5 generation), never Opus, with medium,
  high or xhigh reasoning. The orchestrating session delegates rather than doing the work
  itself.
- Prose goes through the `avoid-ai-writing` skill before it lands: anything longer than a
  line or two in this file, `docs/`, `.tmp/` reports, README changes, code comments, tool
  docstrings, commit and MR text. Chat replies are exempt.
- Before any run over about a minute, quote a finish time, not a duration ("integration
  sweep, ~12 min, results around 14:05"). The user cannot see the progress bar.
- Never write a literal token value anywhere: code, comments, docs, examples, configs,
  tests. Tokens come from `gen_ws_token.py` or the server's own minting, never from a
  model. `tests/unit/test_ws_token.py` scans the tree for token-shaped literals; a name the
  code spells as an identifier is exempt, so a new constant or variable name passes once the
  code defines it (a hyphenated name goes in `_HYPHENATED_NAMES`).
- Bump all four version carriers (`shared-py/browserplug_shared/version.py`, both
  `manifest.json` files, `package.json`) on every build you ask the user to install. Two
  builds with one number make "I installed the latest one" unverifiable, and one stale
  extension among several browsers looks exactly like a broken feature.

### Decisions that stand

Do not re-propose these; the user settled each one. Item codes (S10, T11, A6) are from
`.tmp/server-launcher-review-2026-09-29-resolution.md`.

- No new tools, and no new parameters where an option on an existing tool will do. The
  surface is nine tools plus a conditional tenth. An addition to a tool description must be
  paid for by a deletion, and advocacy inside a description is measurably inert: steer from
  the response instead (`server/tools/response_hints.py`).
- No `browser` argument on any tool. The active browser is server-side per-client state;
  `browsers` is the one management tool, shown only while two or more browsers are connected.
- No custom per-browser labels ("nickname", "friendly name", an options text field).
- No configurable commit budget on `tabs open`; `navigate` already covers waiting.
- No `/startup` probe (the window it would describe measures 0 to 62 ms).
- No `extension-common/` source-generation tree: shared files are byte-copied and checked
  by a loud validator.
- No `page_console`, no `history`, no clipboard read and no `save_to` on `page_screenshot`:
  a usage census found them unused. Read `.tmp/agent-economy-fx5-17.md` before reinstating
  any; gecko clipboard mechanics are in `.tmp/clipboard-read-focus-findings.md`.
- S10 is closed: no plaintext token auth exists. No peer authenticates over a plaintext
  hello and the HTTP gate ignores the raw token header, so a "require encryption" switch
  would refuse nothing.
- T11: `requests(action="get")` returns bodies raw by default. Options > General has a
  censor toggle and custom patterns; header redaction is always on. The user decided this.
- The usage panels (Kilo, pi, Copilot detectors) stay, behind their module boundary
  (`server/usage_panels/usage_panels.py`) and the popup's switch (A6).
- Tool text must work on `qwen/qwen3.5-9b`, the floor among supported models, even when
  that costs tokens in descriptions; Mistral Large 4 and DeepSeek 4.1 Flash add variety,
  Sonnet 5.5 is the ceiling. A fix goes in this order: validation error text showing a
  complete correct call, a response hint on a success, a parameter description, the tool
  description last. Responses may be tuned to raise the success rate, but every change
  states a general rule and is judged across all scenarios and models, never fitted to one
  (`tool-economy.md`).

Decisions scoped to one subsystem live in its doc: no `file://` workaround on Android, a
zero-library connection map, `userScripts` submitted as is to the Chrome Web Store.

## How it runs

```
MCP client ──stdio──▶ launcher/launcher.py (stdlib relay; launcher/stdio_relay.py)
                         │  seeks the shared server on :2830..:2942, spawns it DETACHED
                         │  if none answers, then relays JSON-RPC frames to POST /mcp
                         ▼
                      server/server.py   (the ONE shared server)
                         ├─ MCP Streamable HTTP on :2830   (+ /health, /ready, /restart …)
                         └─ WebSocket on :8772   ◀── N browser extensions (gecko and/or blink)
```

- Each stdio client spawns its own launcher, which dies with it; the detached server
  outlives every client. HTTP clients call `/mcp` directly.
- `server/server.py` is a composition root; the substance is in its sub-packages
  (`server-operations.md`, "Repository layout"). `launcher/` and
  `shared-py/browserplug_shared/` are stdlib-only, `server/` never imports `launcher/`, and
  sub-packages form no import cycle (`tests/unit/test_import_boundaries.py`). Importing a
  module has no side effects; only entry points configure logging. `server/launcher.py` is
  a forwarding stub that MCP client configs name.
- Interpreter (README, step 1): `BROWSERPLUG_SERVER_PYTHON` (the README's pip venv,
  packages unchecked), or, unset, a uv environment in `<state dir>\server-env` that
  `server_env.py` builds and repairs from `server/server.py`'s PEP 723 block and lock
  before every spawn. The checkout has no project venv.
- 8772 never moves: a busy 8772 means another BrowserPlug owns it, and a second server
  refuses to start. The MCP port walks 2830 to 2942, strictly ascending, so a client finds a
  relocated server by walking the same sequence; `BROWSERPLUG_MCP_PORT` pins it. Never set
  `SO_REUSEADDR` on Windows (`ports.md`).
- On Windows the server is two processes: the environment's `Scripts\python.exe`
  trampoline and the CPython child that binds the ports (`/health`'s `server_pid`). Treat
  the pair as one server; never kill one of them on its own. Use `python server/control.py
  start|restart|shutdown|status`. `/restart` and `/shutdown` need a signed request, so a
  bare `curl -X POST` gets 403.
- An edit is live once `/health`'s `started` postdates it and `server_pid` is new. A failed
  restart leaves the old server running and says why in `last_restart` (`control.py status`
  prints it). `/restart` copies the server's environment as is, so a variable its spawner
  lacked stays missing.
- Never run blocking work on an event loop. The watchdog (`server/runtime/watchdog.py`)
  restarts a server whose loop freezes for about 30 s and dumps every stack to
  `<log base>.hang.log`; `BROWSERPLUG_WATCHDOG=0` turns it off.
- Run by hand (venv setup: `<venv python> server/server.py`); stop with `control.py
  shutdown` or by killing `server_pid`:
  `BROWSERPLUG_LOG_FILE=<repo>\browserplug-server.log uv run --locked --script server/server.py`
- State lives in `%LOCALAPPDATA%\browserplug\` (`config.json`, `ws-token`, `server-env\`).
  `BROWSERPLUG_STATE_DIR` relocates it; the unit conftest sets it, so tests never touch the
  real files.
- `python server/preflight.py` diagnoses an install even with the server down; `GET
  /doctor` and the head of `server_info` run the same checks.

### Live checks

- Several browsers may be connected, a phone included; tab ids and snapshot refs belong to
  one. Before trusting a live check, confirm which browser answered (`browsers(action="use",
  …)` replies "Already on …" and changes nothing).
- After an extension change, rebuild, bump the version, have the user reload it, and check
  each browser's extension version in `server_info` before testing.
- "No extension connection available" means no browser is connected: ask the user to press
  Reconnect in the popup. Do not start a browser yourself. A fresh profile has no extension,
  and a profile that has it would join the user's live server. On Windows even `chrome.exe
  --version` launches the browser; read the vendored Chrome for Testing's version from
  `dist/chrome-for-testing/.cft-version`.

## The tool surface

- Nine tools: `tabs`, `navigate`, `page_read`, `page_snapshot`, `page_act`, `page_eval`,
  `page_screenshot`, `requests`, `server_info`; `browsers` appears only while two or more
  browsers are connected. A tool function's docstring in `server/tools/` is its
  agent-facing description (`page.py` holds all five `page_*` tools); `mcp_tools.py` only
  registers them. BrowserPlug, not the SDK, owns schemas, argument validation and every
  error text; `tests/unit/golden/tools_list.json` pins them byte for byte.
- A tool call becomes `area.verb` WebSocket requests, which both `background.js` files
  handle through a browser API or the shared `automation.js` toolkit.
- The `tools/list` payload (about 16 KB by default, 19 KB with every conditional part;
  descriptions alone are about 9 KB) is re-sent on every `tools/list`, hence the budget in
  "Decisions that stand". Cross-tool guidance goes
  in `server/tools/instructions.py`, sent once at `initialize`.
- Failures are structural: a tool records what its prose means (`tool_outcome.py`) and the
  dispatcher turns a recorded failure into an `isError` result. Apart from cancellation,
  nothing a tool raises becomes a JSON-RPC error (`tool-surface.md`).

## Build and test

```bash
# edited a SHARED extension file? sync gecko -> chromium first, or parity invariant C fails
uv run --script tests/parity/sync_common.py
# rebuild BOTH packages after ANY extension change (test profile cache keys on the XPI hash)
uv run --script build_xpi.py      # -> dist/firefox/*.xpi and dist/chrome/*.zip

uv run --locked --script tests/run.py -q                # unit + parity, ~9 min
uv run --locked --script tests/run.py parity/ -q        # <1 s
uv run --locked --script tests/run.py --ruff check server launcher shared-py tests   # static gate, enforced by unit/test_static_analysis.py
npm run test:js                                         # ~15 s; npm install once

# INTEGRATION, both engines, ~12 min: 4 parallel workers, then the `timing` tests
# serially (tests/run.py --help). Tell the user a finish TIME first.
FIREFOX_PATH="C:/Program Files/Firefox Developer Edition/firefox.exe" \
  CHROME_PATH="<repo>/dist/chrome-for-testing/chrome-win64/chrome.exe" \
  PYTHONIOENCODING=utf-8 uv run --locked --script tests/run.py --integration
# the same suite in one serial process, ~23 min; the form for one module or a -k subset
#   ... uv run --locked --script tests/run.py integration/ -q
```

- One integration invocation at a time on a machine, and `timing` tests only on a quiet
  one. Runs share no state or ports, but they share the CPU, and the `timing` tests
  (wall-clock bounds, deliberate races, fixed sleeps) fail under load; the one
  "cross-contamination" flake on record was such a test. `--integration` gives them a
  serial phase of their own. Another session may be running one: ask before starting
  yours (`testing.md`, "Parallel runs").
- An unset `FIREFOX_PATH` silently skips the Firefox half; a missing browser binary is the
  one sanctioned skip. `pytest -q` prints nothing until it exits, so a 0-byte log is not a
  stall.
- Tests use `example.org` or the local fixture server, never external services.
- Every `uv run` or `uv sync` of `server/server.py` or `tests/run.py` passes `--locked`;
  without it uv silently rewrites the lock when block and lock disagree. The runner points
  `BROWSERPLUG_SERVER_PYTHON` at its own interpreter, so no server a test spawns touches
  the real server environment. With no path it runs only `unit` and `parity`, no browser.
- After editing a PEP 723 block (`server/server.py`, `tests/run.py`), relock it with
  `uv lock --script <script>`; the two locks must agree on every shared package. A floor
  that is too low passes the unit suite; `tests/check_dependency_floors.py` catches it
  (`dependencies.md`).

### Test rules

- Do not add a `pytest.skip` to quiet a flake. A skip can hide a regression behind a green
  run; the harness asserts loudly instead.
- Test the glue. The recurring failure is arming code (a listener registration, an
  `apply()` call) that silently stops running while hundreds of helper tests stay green.
  The JS twin tests slice the real glue out of `background.js` for that reason.
- An agent-facing message is a contract that mostly only the integration sweep enforces
  (the unit `tools/list` golden pins descriptions and schemas). Before rewording one, grep
  `tests/integration/` for the old wording.
- On Chromium, code behind the "Allow user scripts" gate runs in no test. After editing it,
  re-grep for renamed identifiers: `node --check` accepts a call to an undefined function.

## Two trees, one contract

- Shared files (`automation.js`, `popup/*`, `options.*`, `vendor/*`, `_locales/**` and a
  few more; `[shared]` in `tests/parity/parity.toml`) are byte-identical in both trees.
  Edit `extension-gecko/` first, then run `sync_common.py`. Never hand-edit the chromium copy.
- Platform twins (`background.js`, `content.js`, `manifest.json`) are ported by hand with
  the same wire contract and error codes; only the mechanism adapts. A capability one engine
  lacks fails closed with a structured error, never a silent no-op.
- JS twin tests cut functions out of each `background.js` by exact comment and signature
  anchors. Treat those strings as an API: renaming one breaks the slicer.
- `tests/parity/` (ten invariants, no browser, under a second) names what you forgot.
  Adding an action end to end: checklist in `parity-enforcement.md`.

## Environment

- Windows 11. `<repo>` is the folder holding this file. MCP client configs and launcher
  scripts outside the repo name its path, so moving it has a tail (`mcp-clients.md`,
  `server-operations.md`). Prefer absolute paths in the Bash tool; its cwd can reset.
- Tracked files are LF in the index (`i/lf`). `core.autocrlf` is `true` on this box (user
  config), so git warns "LF will be replaced by CRLF" for an LF working file and writes
  CRLF on checkout; the warning is harmless. Gecko `background.js` is LF now, but some
  working files are still CRLF or mixed. git-bash `grep` hides CRs: check
  `git ls-files --eol <file>` (`w/` is the working copy) before editing, keep each file's
  own ending, and write new files LF.
- Never run a bare `pip` against a uv-built environment; `pip install
  --requirements-from-script` into the README's venv is the one supported pip use. Server
  and suite need Python `>=3.11,<3.13`, the launcher 3.10+ (`dependencies.md`).
- Never sync the environment a running server uses: a package swap leaves old modules in
  memory, and an interpreter change fails halfway with "Access is denied" and guts it;
  `/restart` refuses with `deps_changed` instead. After a dependency change: `python
  server/control.py shutdown`, then `python server/control.py start` or reconnect a client
  (the launcher syncs before it spawns; another session's may do it first). Venv setup:
  shut down, re-run the `pip install`, start (`dependencies.md`).

## The name

The Firefox extension id is `browserplug@marcinwolny.net`; every new identifier uses
`BROWSERPLUG_`. The former product name, LiveBrowser, stays on purpose in the dated
research under `.tmp/` and `feedback*/`, in `server/livebrowser-restart.startup.log` and in
`%LOCALAPPDATA%\livebrowser` (a backup of the state directory); leave those alone.
`live_browsers()` is unrelated to the brand.



<!-- graft:start -->
## Graft — repo context graph

This repo is indexed in `graft/`: small linked markdown nodes that explain each
system and carry exact file:line spans, kept in sync with the code through git.

For ANY task here — understanding how something works, finding where code lives,
or scoping a change — get context from the graph before grepping or opening
source files. Re-ask freely (it's cheap) and reuse literal identifiers you
already have (symbol, error string, file name) as the query. New to this repo?
Run `graft map` first — a token-budgeted orientation (dir clusters, hubs,
hotspots), no LLM, no key.

- Run `graft ask "<your question>" --source` → ranked nodes with the relevant
  code spans inlined (each hit's ≤8-line crux by default; `--full` for whole
  definitions when the crux isn't enough). Match the tool to the task shape:
  for understanding or editing, the top node IS the answer — cite its
  `covers:` file:line spans and edit straight from `--source`. For
  exhaustive tasks ("every occurrence / every caller of this pattern"), ranked
  results are top-N, not complete — run `graft grep "<literal>"` instead
  (exhaustive over indexed files, grouped by enclosing symbol), falling back
  to raw `grep -rn` only for unindexed files.
- `graft skeleton <file>` → every definition's signature + span, ~10× cheaper
  than reading the file; use it to skim an API surface.
- `graft callers <symbol>` gives precomputed, exact edges — who calls this.
  Add `--direction out` for what it calls, or `--depth N` to walk
  transitively for the full blast radius. For structural questions, skip
  ranking and use this directly.
- Or browse: `graft/INDEX.md` lists every node; follow the links.
- Monorepos and folders of multiple repos rank fairly across sub-projects —
  hits carry `[scope/]` labels naming which one they're from. Narrow with
  `graft ask "<task>" --in <scope>/` once you know where you're working.

If a returned span is truncated ("+N more lines"), open the file at that exact
range before finalizing. Only open source files when a node genuinely lacks a
needed detail, and then at the exact file:line the node points to — never
re-read whole files.

After big code changes, refresh the graph with `graft build` (deterministic,
no API key, $0).
<!-- graft:end -->
