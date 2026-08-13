"""Operator-supplied extra allowed commands (Settings field backend hook).

`SecurityProfile.get_all_allowed_commands()` merges
`AIFACTORY_EXTRA_ALLOWED_COMMANDS` (comma/space separated) so an operator can
grant tooling the auto-detection/plan-grant missed — without editing code or
the per-project profile file.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from project.models import SecurityProfile  # noqa: E402


def _profile() -> SecurityProfile:
    p = SecurityProfile()
    p.base_commands = {"ls", "cat"}
    return p


def test_env_extra_commands_merged(monkeypatch):
    monkeypatch.setenv("AIFACTORY_EXTRA_ALLOWED_COMMANDS", "go, terraform  kubectl")
    allowed = _profile().get_all_allowed_commands()
    assert {"go", "terraform", "kubectl"} <= allowed
    assert {"ls", "cat"} <= allowed  # base preserved


def test_unset_is_noop(monkeypatch):
    monkeypatch.delenv("AIFACTORY_EXTRA_ALLOWED_COMMANDS", raising=False)
    assert _profile().get_all_allowed_commands() == {"ls", "cat"}


def test_malformed_and_denied_dropped(monkeypatch):
    monkeypatch.setenv(
        "AIFACTORY_EXTRA_ALLOWED_COMMANDS", "go, sudo, rm -rf, ../evil, , ssh"
    )
    allowed = _profile().get_all_allowed_commands()
    assert "go" in allowed
    # hard-denied + malformed (contains space/slash) are dropped
    assert "sudo" not in allowed
    assert "rm -rf" not in allowed
    assert "../evil" not in allowed
