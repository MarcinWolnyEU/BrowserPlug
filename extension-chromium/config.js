/*
 * BrowserPlug Chromium Extension - build/test config (imported by background.js)
 * Copyright 2026 Marcin Wolny
 * Licensed under the MIT License - see LICENSE file for details
 */

// Production ships BUILD_CONFIG = null. The chromium test harness OVERWRITES this
// whole file in the per-session baked/unpacked extension dir with
//   const BUILD_CONFIG = { wsPort: <test port>, wsHost: "localhost", testMode: true };
// The SW imports it via importScripts('config.js'); when BUILD_CONFIG is non-null it
// wins over storage config UNCONDITIONALLY (design D12) — the sqlite-stamp analog.
//
// Fail-closed: if the harness bake fails, this file is missing, importScripts throws,
// and the SW is inert — it can never dial the developer's live :8772 with a test
// profile. (The gecko harness gets the same guarantee via storage-sync seeding.)
const BUILD_CONFIG = null;
