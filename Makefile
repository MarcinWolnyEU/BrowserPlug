# BrowserPlug Project Makefile
# Build and manage the browser extension and Python server

# Variables
FIREFOX_PATH ?= firefox

# Every Python command goes through uv. The server's dependencies live in the PEP 723 block
# of server/server.py (locked in server/server.py.lock); the test tools in tests/run.py
# (tests/run.py.lock). --locked makes a stale lock an error instead of a silent rewrite of
# the committed file. The stdlib-only scripts carry a block with no dependencies.
# Override: make UV=/path/to/uv
UV ?= uv
RUN_TESTS = $(UV) run --locked --script tests/run.py

.PHONY: all help setup install dev build build-extension package source-zip run-server start stop restart status test run-tests test-unit test-integration test-parity test-js quick-test sync-common icons check lint clean clean-all ci

# Default target
all: setup build test

help:
	@echo "BrowserPlug Project - Available Commands:"
	@echo ""
	@echo "Setup & Installation:"
	@echo "  setup          - Build the server and test environments (uv sync --locked)"
	@echo "  install        - Build the server environment only"
	@echo ""
	@echo "Building:"
	@echo "  build          - Build the extension packages (XPI for Firefox + ZIP for Chromium)"
	@echo "  package        - build, plus the server zip (build_server_zip.py)"
	@echo ""
	@echo "Development:"
	@echo "  dev            - Setup development environment"
	@echo "  run-server     - Run the server in the foreground"
	@echo "  start          - Start the shared server (server/control.py start)"
	@echo "  stop           - Shut the shared server down (server/control.py shutdown)"
	@echo "  restart        - Restart the shared server (server/control.py restart)"
	@echo "  status         - Print the shared server's /health report"
	@echo "  source-zip     - Build the source archive AMO requires beside a Firefox submission"
	@echo "  sync-common    - Copy parity [shared] files gecko -> chromium (fixes invariant C drift)"
	@echo "  icons          - Re-render the icon set and install it into both extension trees"
	@echo "  check          - Run all quality checks (lint + test)"
	@echo "  lint           - Run ruff on the Python source, tests and root scripts"
	@echo ""
	@echo "Testing:"
	@echo "  test           - Run all tests (Python + JS)"
	@echo "  test-unit      - Run Python unit tests only"
	@echo "  test-integration - Run integration tests only"
	@echo "  test-parity    - Run the gecko<->chromium parity validator (no browser)"
	@echo "  test-js        - Run JS (jsdom) unit tests for automation.js"
	@echo "  run-tests      - Run the Python unit, parity and integration tests (integration takes about 37 minutes)"
	@echo ""
	@echo "Maintenance:"
	@echo "  clean          - Remove the extension and server packages and temporary files"
	@echo "  clean-all      - Same as clean"
	@echo ""

# Setup and Installation
# uv keeps both environments in its cache, keyed on the script path; nothing is installed
# into the checkout and no bare `pip` runs anywhere. The launcher builds the server's own
# environment (<state dir>\server-env) itself, so `make install` does not prepare it.
setup: install
	@echo "Building the test environment..."
	$(UV) sync --script tests/run.py --locked
	@echo "✅ Setup complete!"

install:
	@echo "Building the server environment..."
	$(UV) sync --script server/server.py --locked
	@echo "✅ Server environment ready!"

dev: setup
	@echo "✅ Development environment ready!"

# Building
build: build-extension
	@echo "✅ Build complete!"

# One builder for both browsers: build_xpi.py (stdlib only, no zip), the same script the
# README gives to Windows users.
build-extension:
	@echo "Building the extension packages..."
	$(UV) run --script build_xpi.py
	@echo "✅ Extension build complete!"

package: build
	@echo "Creating the server package..."

	# Package server: build_server_zip.py (stdlib only, no zip) stages server/, launcher/ and
	# shared-py/ without bytecode, logs or the managed Tor install, and writes the same zip the
	# release workflow attests.
	$(UV) run --script build_server_zip.py

	# Clear profile cache since extension has changed
	@echo "Clearing profile cache..."
	@rm -rf dist/profile-cache/*
	@echo "✓ Profile cache cleared"

	@echo "📦 Packages created:"
	@echo "  - dist/firefox/browserplug@marcinwolny.net.xpi    (Firefox MV2)"
	@echo "  - dist/chrome/browserplug-chromium.zip        (Chromium MV3)"
	@echo "  - dist/packages/browserplug-server.zip"

# Development and Running
run-server:
	@echo "Starting BrowserPlug WebSocket server..."
	$(UV) run --locked --script server/server.py

# Testing
test: run-tests test-js

run-tests: build
	@echo "Running all tests..."
	FIREFOX_PATH=$(FIREFOX_PATH) $(RUN_TESTS) unit/ parity/ integration/

test-unit:
	@echo "Running unit tests..."
	FIREFOX_PATH=$(FIREFOX_PATH) $(RUN_TESTS) unit/

test-integration: build
	@echo "Running integration tests..."
	FIREFOX_PATH=$(FIREFOX_PATH) $(RUN_TESTS) integration/

# Gecko<->Chromium parity validator (ArchUnit-style, stdlib only, no browser)
test-parity:
	@echo "Running gecko<->chromium parity validator..."
	$(RUN_TESTS) parity/

# The source archive AMO asks for beside a Firefox submission, because the tree
# carries one upstream bundle (vendor/defuddle.js) a reviewer cannot read as shipped.
source-zip:
	@echo "Building the AMO source archive..."
	$(UV) run --script build_source_zip.py

# Re-sync the parity [shared] files from extension-gecko/ (source of truth) to
# extension-chromium/, the one-command fix when invariant C reports drift.
sync-common:
	@echo "Syncing parity [shared] files gecko -> chromium..."
	$(UV) run --script tests/parity/sync_common.py

# Re-render assets/icons/ from its four masters and install the shipped subset into
# both extension trees. Needs Pillow, which render.py's PEP 723 block declares; the
# `render.py --check` the unit suite runs does not.
icons:
	@echo "Rendering and installing the icon set..."
	$(UV) run --script assets/icons/render.py

# JavaScript unit tests for the extension's automation toolkit (jsdom, no Firefox)
test-js:
	@echo "Running JavaScript (jsdom) unit tests..."
	@command -v node >/dev/null 2>&1 || { echo "⚠️  node not found — skipping JS tests"; exit 0; }
	@[ -d node_modules ] || npm install
	npm run test:js


# Quality Checks
check: lint test
	@echo "✅ All quality checks passed!"

# ruff comes from the test environment (tests/run.py pins it); the rules are ruff.toml's. The
# root scripts are listed by name because the root also holds files that are not Python.
lint:
	@echo "Running Python linting..."
	$(RUN_TESTS) --ruff check server launcher shared-py tests build_xpi.py build_source_zip.py build_server_zip.py gen_ws_token.py patch_defuddle.py
	@echo "✅ Linting complete!"


# Maintenance and Cleanup
# clean removes only what the build writes. dist/chrome-for-testing (a download) and
# dist/profile-cache stay.
clean:
	@echo "Cleaning build artifacts..."
	@rm -rf dist/firefox dist/chrome dist/packages dist/server-package dist/extension-gecko dist/extension-chromium
	@rm -rf tests/htmlcov/
	@rm -rf tests/.coverage
	@rm -rf /tmp/browserplug-test-profile
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@find . -type f -name "*.pyo" -delete 2>/dev/null || true
	@echo "✅ Clean complete!"

# The uv environments live in uv's cache, not here: `uv cache prune` drops unused ones,
# but never while the server runs (it guts the running server's environment).
clean-all: clean
	@echo "✅ Deep clean complete!"

# Development Workflow Helpers. These drive the shared server through server/control.py,
# the way a logon task does; nothing here kills a process.
start:
	$(UV) run --script server/control.py start

stop:
	$(UV) run --script server/control.py shutdown

restart:
	$(UV) run --script server/control.py restart

# Quick development cycle (fast, no Firefox: Python unit + parity + JS unit)
quick-test: build test-unit test-parity test-js
	@echo "✅ Quick test cycle complete!"

# Project status
status:
	@echo "BrowserPlug Project Status:"
	@echo "====================="
	@echo ""
	@echo "📁 Project Structure:"
	@find . -maxdepth 2 -type f \( -name "*.py" -o -name "*.js" -o -name "*.json" -o -name "*.md" \) -not -path "./node_modules/*" -not -path "./.git/*" -not -path "./dist/*" -not -path "./.tmp/*" -not -path "./graft/*" -not -path "*/__pycache__/*" | sort
	@echo ""
	@echo "🐍 Python Dependencies:"
	@echo "Server (server/server.py.lock):" && (grep -A1 -E '^name = "(mcp|websockets|uvicorn)"$$' server/server.py.lock | grep -v '^--' || echo "  no lock")
	@echo "Tests (tests/run.py.lock):" && (grep -A1 -E '^name = "(pytest|ruff)"$$' tests/run.py.lock | grep -v '^--' || echo "  no lock")
	@echo ""
	@echo "🔗 Shared server (server/control.py status):"
	@$(UV) run --script server/control.py status || echo "  not running"

# Continuous Integration simulation
ci: clean setup lint test package
	@echo "🎉 CI pipeline complete - ready for deployment!"
