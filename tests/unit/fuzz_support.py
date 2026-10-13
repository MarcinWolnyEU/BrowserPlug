"""Shared Hypothesis settings for the tests/unit/test_fuzz_*.py files.

Every property sets `max_examples` and `deadline=None` through `fuzz`, so each one costs
about one to three seconds and a slow machine cannot turn a pass into a flake. The
example database is Hypothesis's default, `.hypothesis/` under the directory pytest runs
from (git-ignored): a failing example is saved there and replayed first on the next run.

`BROWSERPLUG_FUZZ_SCALE=20` multiplies every example count, for a longer local soak
before a release. Unset, the suite runs at the default size.
"""

import os

from hypothesis import HealthCheck, settings

SCALE = max(1, int(os.environ.get("BROWSERPLUG_FUZZ_SCALE") or 1))


def fuzz(max_examples=150):
    """A settings decorator: `max_examples` cases (times the scale), no per-example deadline."""
    return settings(max_examples=max_examples * SCALE, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])
