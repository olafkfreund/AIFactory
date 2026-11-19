"""Credential scrubbing for persisted investigation output."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "web-server"))

from server.services.investigation_scrub import scrub_lines, scrub_text  # noqa: E402


@pytest.mark.parametrize(
    "secret",
    [
        "AKIA" + "A" * 16,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature",
        "ghp_" + "a" * 36,
        "sk-ant-" + "a" * 48,
        "sk-" + "b" * 48,
        "dGhpc0lzQVJlYWxpc3RpY1NlY3JldFZhbHVlMTIzNDU2Nzg5MA",
        "a" * 40,
        "claude-token-for-this-run",
    ],
)
def test_secret_shapes_are_removed(secret):
    assert secret not in scrub_text(
        f"before {secret} after\n", claude_token="claude-token-for-this-run"
    )


def test_pem_state_survives_batches():
    first, opened = scrub_lines(
        ["before\n", "-----BEGIN RSA PRIVATE KEY-----\n", "private material\n"],
        claude_token="token",
        pem_open=False,
    )
    second, opened = scrub_lines(
        ["more private material\n", "-----END RSA PRIVATE KEY-----\n", "after token\n"],
        claude_token="token",
        pem_open=opened,
    )
    assert first + second == ["before\n", "after [REDACTED]\n"]
    assert opened is False


def test_ordinary_pytest_output_survives():
    output = "tests/test_example.py::test_ok PASSED\n=== 1 passed in 0.02s ===\n"
    assert scrub_text(output, claude_token="token") == output


def test_long_paths_survive_and_mixed_runs_are_removed():
    path = "src/main/java/com/example/projectname/service/impl/OrderServiceImpl.java:42"
    secret = "Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1u"
    lines, _ = scrub_lines([path, f"key={secret}"], claude_token="", pem_open=False)
    assert lines[0] == path
    assert secret not in lines[1]
