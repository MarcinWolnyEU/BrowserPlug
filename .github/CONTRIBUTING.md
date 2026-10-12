# Contributing

Open an issue before starting anything non-trivial, and keep each change small.

Run these before you send a change (details in `AGENTS.md`, "Build and test"):

```bash
uv run --locked --script tests/run.py -q
uv run --locked --script tests/run.py --ruff check server launcher shared-py tests
npm run test:js
```

Edit `extension-gecko/` first, then run `uv run --script tests/parity/sync_common.py` to copy shared files to `extension-chromium/`. Don't bump versions unless asked.

User guides are in `docs/`.
