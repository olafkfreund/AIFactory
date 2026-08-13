"""Tests for output-side DLP on agent-authored git output (#323)."""

from __future__ import annotations

import logging

import commit_message
import pytest
from agents.tools_pkg.tools import task_control
from security.output_dlp import DLP_ENV, dlp_mode, scan_outbound

# A fake AWS Access Key ID (AKIA + 16). Not a real credential; shaped to trip the
# existing scan_secrets pattern without hitting its example/placeholder filter.
AKIA_FIXTURE = "AKIA1234567890ABCDEF"
DIRTY_COMMIT = f"feat: add exporter\n\nDebug creds left in: {AKIA_FIXTURE}\n\nFixes #7"
CLEAN_COMMIT = "feat: add todo endpoint\n\nAdds POST /todos and tests.\n\nFixes #12"


def test_default_mode_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DLP_ENV, raising=False)
    assert dlp_mode() == "warn"


def test_unknown_mode_falls_back_to_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DLP_ENV, "banana")
    assert dlp_mode() == "warn"


def test_dirty_text_detected_but_warn_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DLP_ENV, "warn")
    result = scan_outbound(DIRTY_COMMIT, "commit-message:001")
    assert result.has_hit is True
    assert result.blocked is False
    # The audit summary is redacted — the raw secret must never appear in it.
    assert AKIA_FIXTURE not in result.summary()


def test_warning_log_carries_no_span_of_the_credential(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Assert on the log line the SIEM actually receives, window by window.

    ``AKIA_FIXTURE not in result.summary()`` above is too weak on its own: the
    summary used ``mask_secret(m.matched_text, 12)``, so the first twelve
    characters of the key were written to a WARNING while a whole-value
    assertion stayed green. Checking every 6-character window is what fails
    when someone reintroduces a prefix.
    """
    monkeypatch.setenv(DLP_ENV, "warn")
    with caplog.at_level(logging.WARNING):
        scan_outbound(DIRTY_COMMIT, "commit-message:003")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    # The line must still be emitted and still be triageable.
    assert "Output DLP" in logged
    assert "commit-message:003" in logged
    for i in range(len(AKIA_FIXTURE) - 5):
        window = AKIA_FIXTURE[i : i + 6]
        assert window not in logged, f"leaked {window!r} to the log sink"


def test_dirty_text_blocks_in_block_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DLP_ENV, "block")
    result = scan_outbound(DIRTY_COMMIT, "pr-body:42")
    assert result.has_hit is True
    assert result.blocked is True


def test_clean_text_passes_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DLP_ENV, "block")
    result = scan_outbound(CLEAN_COMMIT, "commit-message:002")
    assert result.has_hit is False
    assert result.blocked is False


def test_off_mode_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DLP_ENV, "off")
    result = scan_outbound(DIRTY_COMMIT, "commit-message:003")
    assert result.has_hit is False


def test_commit_message_screen_blocks_and_withholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DLP_ENV, "block")

    out = commit_message._screen_commit_message(DIRTY_COMMIT, "001-add-x")
    assert AKIA_FIXTURE not in out
    assert "withheld by output DLP" in out


def test_commit_message_screen_warn_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DLP_ENV, "warn")

    out = commit_message._screen_commit_message(CLEAN_COMMIT, "001-add-x")
    assert out == CLEAN_COMMIT


def test_pr_screen_blocks_in_block_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DLP_ENV, "block")

    resp = task_control._screen_pr_text("clean title", DIRTY_COMMIT, "task-9")
    assert resp is not None
    assert resp.get("isError") is True
    assert AKIA_FIXTURE not in resp["content"][0]["text"]


def test_pr_screen_allows_clean_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DLP_ENV, "block")

    assert task_control._screen_pr_text("feat: add x", CLEAN_COMMIT, "task-9") is None
