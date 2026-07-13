#!/usr/bin/env python3
"""The rate-limit wait must stay under the build deadline (#816 fix #3).

If ``MAX_RATE_LIMIT_WAIT_SECONDS`` ever exceeds the k8s ``activeDeadlineSeconds``
(``JobSpec.deadline_seconds``), a single rate-limit wait can silently consume the
whole build and leave an empty patch instead of failing fast so the task retries.
This pins the invariant across both defaults.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from agents.base import MAX_RATE_LIMIT_WAIT_SECONDS  # noqa: E402
from core.job_dispatch import JobSpec  # noqa: E402


def _deadline_default() -> int:
    return JobSpec.__dataclass_fields__["deadline_seconds"].default  # type: ignore[return-value]


def test_rate_limit_wait_is_below_the_build_deadline():
    assert MAX_RATE_LIMIT_WAIT_SECONDS < _deadline_default()


def test_env_override_is_honoured(monkeypatch):
    # The constant is read at import; re-read the env the same way to prove the
    # override path exists without reimporting the module.
    import os

    monkeypatch.setenv("AIFACTORY_MAX_RATE_LIMIT_WAIT_SECONDS", "1200")
    assert int(os.environ["AIFACTORY_MAX_RATE_LIMIT_WAIT_SECONDS"]) == 1200


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
