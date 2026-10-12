#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Turn upstream defuddle's `dist/index.full.js` into the bundle BrowserPlug ships.

The vendored bundle is upstream defuddle with three innerHTML assignments rewritten
to DOMParser. They live in two helpers, `decodeHTMLEntities` and `parseHTML`, and
addons-linter reports each as UNSAFE_VAR_ASSIGNMENT ("Unsafe assignment to
innerHTML") on every submission. The rewrite keeps what the helpers do: one decodes
character references, the other parses an HTML string into a DocumentFragment, in
template context so table parts and head-only elements survive as they did under
`<template>.innerHTML`. Nothing else in the bundle changes.

The replacements are exact strings, and each must match exactly once, so a defuddle
upgrade that moves the code fails here instead of shipping a half-patched bundle.
build_source_zip.py pins the sha256 of the OUTPUT and names this script in the
README it writes for AMO reviewers; upgrade defuddle by running this against the new
upstream file and moving both pins together.

Usage:
    python patch_defuddle.py <upstream dist/index.full.js> [--out extension-gecko/vendor/defuddle.js]
    python patch_defuddle.py --check   # the vendored file contains no innerHTML assignment

Reproduce the shipped file from npm:
    npm pack defuddle@<version>
    tar -xzf defuddle-<version>.tgz package/dist/index.full.js
    python patch_defuddle.py package/dist/index.full.js
"""
import argparse
import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(ROOT, "extension-gecko", "vendor", "defuddle.js")

# (upstream text, replacement). Minified, so the anchors are the whole helper bodies.
REPLACEMENTS = (
    (
        # Entity decoding through a <textarea>'s innerHTML. A parsed text node decodes the
        # same references; `<` is escaped first so markup in the input stays literal text,
        # exactly as RCDATA parsing kept it inside the textarea.
        'e.decodeHTMLEntities=function(t,e){const r=t.createElement("textarea");'
        'return r.innerHTML=e,r.value}',
        'e.decodeHTMLEntities=function(t,e){const r=(new DOMParser).parseFromString('
        '"<body>"+String(e).replace(/</g,"&lt;"),"text/html");return r.body.textContent}',
    ),
    (
        # HTML string -> DocumentFragment. Parsing inside a <template> keeps the template
        # insertion mode of the original (`<tr>` without a table, `<style>` without a
        # head), and the children are moved into a fragment owned by the caller's document
        # so ownership matches what callers appended before.
        'e.parseHTML=function(t,e){if(!e)return t.createDocumentFragment();'
        'const r=t.createElement("template");if(r.innerHTML=e,r.content)return r.content;'
        'const n=t.createElement("div");n.innerHTML=e;const o=t.createDocumentFragment();'
        'for(;n.firstChild;)o.appendChild(n.firstChild);return o}',
        'e.parseHTML=function(t,e){const o=t.createDocumentFragment();if(!e)return o;'
        'const r=(new DOMParser).parseFromString("<template>"+e+"</template>","text/html")'
        '.querySelector("template");if(!r)return o;const n=r.content;'
        'for(;n.firstChild;)o.appendChild(n.firstChild);return o}',
    ),
)

INNERHTML_WRITE = re.compile(r"\.innerHTML\s*=(?!=)")


def apply(text):
    for old, new in REPLACEMENTS:
        count = text.count(old)
        if count != 1:
            raise SystemExit(f"ERROR: anchor matched {count} times, expected 1:\n  {old[:90]}...")
        text = text.replace(old, new)
    left = INNERHTML_WRITE.findall(text)
    if left:
        raise SystemExit(f"ERROR: {len(left)} innerHTML assignment(s) remain after patching")
    return text


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("upstream", nargs="?", help="upstream dist/index.full.js")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"output path (default: {DEFAULT_OUT})")
    parser.add_argument("--check", action="store_true",
                        help="only verify that --out carries no innerHTML assignment")
    args = parser.parse_args()

    if args.check:
        with open(args.out, "rb") as fh:
            data = fh.read()
        left = INNERHTML_WRITE.findall(data.decode("utf-8"))
        print(f"{args.out}: sha256 {sha256_of(data)}, innerHTML assignments: {len(left)}")
        return 1 if left else 0

    if not args.upstream:
        parser.error("the upstream dist/index.full.js path is required unless --check")
    with open(args.upstream, "rb") as fh:
        upstream = fh.read()
    patched = apply(upstream.decode("utf-8")).encode("utf-8")
    with open(args.out, "wb") as fh:
        fh.write(patched)
    print(f"upstream sha256 {sha256_of(upstream)}")
    print(f"patched  sha256 {sha256_of(patched)} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
