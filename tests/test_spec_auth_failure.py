"""#483: spec creation must surface a hard auth error, not retry silently.

When the provider credential is expired/invalid, the spec agent returns a 401
("Invalid authentication credentials"). Previously the spec phases retried
MAX_RETRIES times and collapsed into a generic "Agent did not create spec.md".
Now they detect the auth failure, stop immediately, and return an actionable
error.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from spec.phases.models import MAX_RETRIES  # noqa: E402
from spec.phases.spec_phases import (  # noqa: E402
    SpecPhaseMixin,
    _auth_error_message,
    _auth_failure_detail,
)

_AUTH_OUT = "Failed to authenticate. API Error: 401 Invalid authentication credentials"


def test_detail_detects_auth_failure():
    assert _auth_failure_detail(_AUTH_OUT)
    assert _auth_failure_detail("noise\nfailed to authenticate\nmore") == "failed to authenticate"
    assert _auth_failure_detail("all good, spec written") is None
    assert _auth_failure_detail("") is None
    assert _auth_failure_detail(None) is None


def test_message_is_actionable():
    msg = _auth_error_message("API Error: 401")
    assert "setup-token" in msg and "401" in msg


class _Stub(SpecPhaseMixin):
    def __init__(self, spec_dir: Path):
        self.spec_dir = spec_dir
        self.calls = 0
        self.ui = types.SimpleNamespace(print_status=lambda *a, **k: None)
        self.spec_validator = types.SimpleNamespace(
            validate_spec_document=lambda: types.SimpleNamespace(valid=False, errors=[])
        )

    async def run_agent_fn(self, *a, **k):
        self.calls += 1
        return (False, _AUTH_OUT)


@pytest.mark.asyncio
async def test_spec_writing_stops_on_auth_failure(tmp_path):
    stub = _Stub(tmp_path)
    result = await stub.phase_spec_writing()
    assert result.success is False
    assert stub.calls == 1, "must stop after the first attempt, not retry"
    assert MAX_RETRIES > 1  # proves we short-circuited the retry loop
    assert "authentication failed" in result.errors[0].lower()
