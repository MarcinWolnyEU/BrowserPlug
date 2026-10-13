"""Property-based fuzzing of the redaction code that stands between a tool call and /health.

Tool arguments and client command lines are untrusted text: they reach `redact_args`,
`redact_url` and `sanitized_cmdline` before anything is recorded or shown. Hand-written
examples (test_call_redaction.py) pin known secrets; these properties assert the rules for
any input Hypothesis can build: no raise, redacting twice equals redacting once, and a
planted secret never survives.
"""

import json
import time

from hypothesis import assume, given, strategies as st

from fuzz_support import fuzz
from server.reporting import arg_redaction, client_identity

# Planted secrets start with a marker no placeholder or enum value can spell.
_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
secrets_ = st.text(alphabet=_ALNUM, min_size=6, max_size=40).map(lambda s: "Zq7" + s)

json_leaf = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=60)
json_value = st.recursive(
    json_leaf, lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=12), inner, max_size=4),
    max_leaves=12)
# Keys that exercise every branch: enum, URL, content, and arbitrary names.
arg_keys = st.sampled_from(sorted(arg_redaction.ENUM_KEYS | arg_redaction.URL_KEYS
                                  | arg_redaction.CONTENT_KEYS)) | st.text(max_size=12)
arguments = st.dictionaries(arg_keys, json_value, max_size=6)


@fuzz()
@given(arguments)
def test_redact_args_never_raises_and_is_json_safe(args):
    out = arg_redaction.redact_args(args)
    json.dumps(out)


@fuzz()
@given(arguments)
def test_redact_args_is_idempotent(args):
    once = arg_redaction.redact_args(args)
    assert arg_redaction.redact_args(once) == once


@fuzz()
@given(secrets_, st.sampled_from(sorted(arg_redaction.CONTENT_KEYS)), st.booleans())
def test_a_secret_under_a_content_key_never_survives(secret, key, nested):
    """A string value is replaced by its length, however it is wrapped."""
    value = [{"action": "fill", "value": secret}] if nested else secret
    args = {key: value, "steps": [{"action": "fill", "value": secret}], "extra": {"k": secret}}
    assert secret not in json.dumps(arg_redaction.redact_args(args))


_hosts = st.from_regex(r"[a-z][a-z0-9-]{0,15}(\.[a-z][a-z0-9-]{0,10}){1,2}", fullmatch=True)
_path_parts = st.from_regex(r"(/[A-Za-z0-9._~-]{0,12}){0,5}", fullmatch=True)


@fuzz()
@given(st.sampled_from(["http", "https", "ftp"]), secrets_, secrets_, secrets_, secrets_,
       _hosts, st.none() | st.integers(1, 65535), _path_parts)
def test_url_loses_userinfo_query_and_fragment(scheme, user, password, query, frag, host, port, path):
    netloc = f"{user}:{password}@{host}" + (f":{port}" if port else "")
    url = f"{scheme}://{netloc}{path}?k={query}#{frag}"
    out = arg_redaction.redact_url(url)
    for secret in (user, password, query, frag):
        assert secret not in out
    assert out.startswith(f"{scheme}://{host}")
    assert arg_redaction.redact_url(out) == out


@fuzz()
@given(st.text(max_size=300))
def test_redact_url_on_arbitrary_text_is_total_and_stable(text):
    out = arg_redaction.redact_url(text)
    assert isinstance(out, str)
    assert "?" not in out and "#" not in out and "@" not in out
    if not out.startswith("<"):    # a URL is stable; a placeholder is what redact_value keeps
        assert arg_redaction.redact_url(out) == out
    # Free text goes through the same function per URL.
    assert isinstance(arg_redaction.redact_urls_in_text(text), str)


_argv_text = st.text(max_size=80)
_secret_flags = st.sampled_from(["--api-key", "--token", "--password", "--client-secret", "--auth"])


@fuzz()
@given(st.lists(_argv_text, max_size=8), st.integers(0, 8), secrets_, _secret_flags)
def test_the_value_after_a_secret_flag_is_masked(argv, at, secret, flag):
    assume(all(secret not in part for part in argv))
    argv = argv[:at] + [flag, secret] + argv[at:]
    assert secret not in (client_identity.sanitized_cmdline(argv, limit=10_000) or "")


@fuzz()
@given(st.lists(_argv_text, max_size=6), secrets_,
       st.sampled_from(["--api-key={}", "API_KEY={}", "PGPASSWORD={}", "Authorization: Bearer {}",
                        "--header=Authorization: Bearer {}", "https://u:{}@example.org/p"]))
def test_secret_shaped_arguments_are_masked(argv, secret, template):
    assume(all(secret not in part for part in argv))
    line = client_identity.sanitized_cmdline(argv + [template.format(secret)], limit=10_000)
    assert secret not in (line or "")


@fuzz()
@given(st.lists(st.text(max_size=3000), max_size=6), st.integers(20, 600))
def test_sanitized_cmdline_is_total_and_bounded(argv, limit):
    line = client_identity.sanitized_cmdline(argv, limit=limit)
    assert line is None or (isinstance(line, str) and len(line) <= limit + 1)


@fuzz(max_examples=25)
@given(st.text(alphabet=st.sampled_from("abcKEY_.-=: 0"), min_size=500, max_size=4000))
def test_sanitized_cmdline_stays_linear_on_identifier_like_input(text):
    """Review finding of 2026-10-02: a 4000-character identifier-like argument once took
    51 s and froze the server. Allow generous slack; a quadratic or cubic pattern is far over."""
    start = time.perf_counter()
    client_identity.sanitized_cmdline([text])
    assert time.perf_counter() - start < 3.0
