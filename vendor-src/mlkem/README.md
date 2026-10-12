# vendor/mlkem.js

`extension-gecko/vendor/mlkem.js` (shared with chromium) is ML-KEM-768 from
`@noble/post-quantum` 0.7.1, bundled by esbuild 0.28.2 into one readable IIFE that sets
`globalThis.browserplugMlKem768`. `ws-crypto.js` uses it only where the browser has no
native ML-KEM in WebCrypto. Rebuild it from this directory:

    npm ci
    npx esbuild entry.js --bundle --format=iife --target=es2020 \
        --outfile=mlkem.js --legal-comments=eof
    sha256sum mlkem.js

The hash must match `MLKEM_BUNDLE_SHA256` in `tests/unit/test_ws_crypto.py`. When you
upgrade, move the lock file, the shipped file and that pin together, then run
`tests/parity/sync_common.py`.
