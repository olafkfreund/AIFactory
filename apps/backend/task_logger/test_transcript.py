"""Focused checks for best-effort transcript capture."""

import json
import os
import re
from collections.abc import Iterator
from typing import Any

import pytest
from task_logger import LogPhase, TaskLogger, transcript


@pytest.fixture(autouse=True)
def clear_transcript() -> Iterator[None]:
    transcript.reset()
    yield
    transcript.reset()


def entries() -> list[dict[str, Any]]:
    path = transcript.written_files()[0]
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_noop_and_lazy_file(tmp_path):
    transcript.record("tool_start", "Read", "before start")
    assert transcript.written_files() == []
    transcript.start(tmp_path)
    assert transcript.written_files() == []
    assert not (tmp_path / "transcripts").exists()
    transcript.record("tool_start", "Read", {})
    assert re.fullmatch(
        rf"\d{{8}}T\d{{6}}_\d{{6}}Z-{os.getpid():07d}\.jsonl",
        transcript.written_files()[0].name,
    )


def test_redaction_and_full_payload(tmp_path, monkeypatch):
    secret = "opaque test value 12345".replace(" ", "-")
    monkeypatch.setenv("CUSTOM_TOKEN_FOR_TEST", secret)
    key = "-----BEGIN PRIVATE KEY-----\nsecret body\n-----END PRIVATE KEY-----"
    transcript.start(tmp_path)
    transcript.tool_start("Read", {"nested": [key, secret, "x" * 20_000]}, "id-1")
    transcript.tool_end("id-1", {"result": key}, False)
    rows = entries()
    assert len(rows) == 2
    assert rows[0]["payload"]["nested"][2] == "x" * 20_000
    assert rows[0]["payload"]["nested"][:2] == ["***REDACTED***"] * 2
    assert rows[1]["payload"]["result"] == "***REDACTED***"
    assert secret not in transcript.written_files()[0].read_text()
    assert "secret body" not in transcript.written_files()[0].read_text()


def test_tool_ids_and_empty_results(tmp_path):
    transcript.start(tmp_path)
    transcript.tool_start("Read", {}, "read")
    transcript.tool_start("Bash", {}, "bash")
    transcript.tool_end("bash", "", False)
    transcript.tool_end("read", None, True)
    rows = entries()
    assert [(row["tool_use_id"], row["tool"]) for row in rows] == [
        ("read", "Read"),
        ("bash", "Bash"),
        ("bash", "Bash"),
        ("read", "Read"),
    ]
    assert rows[2]["payload"] == ""
    assert rows[2]["success"] is True
    assert rows[3]["payload"] is None
    assert rows[3]["success"] is False


def test_cap_writes_one_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "MAX_BYTES", 700)
    transcript.start(tmp_path)
    transcript.record("tool_start", "Read", "first")
    transcript.record("tool_end", "Read", "x" * 400)
    transcript.record("tool_end", "Read", "still ignored")
    assert [row["kind"] for row in entries()] == ["tool_start", "truncated"]
    assert transcript.written_files()[0].stat().st_size <= 700


def test_unwritable_target_never_raises(tmp_path):
    target = tmp_path / "file"
    target.write_text("not a directory")
    transcript.start(target)
    transcript.record("tool_start", "Read", "data")
    transcript.tool_start("Read", {}, "id")
    transcript.tool_end("id", None, False)
    assert transcript.written_files() == []


def test_task_logger_methods_pair_batched_calls(tmp_path):
    transcript.start(tmp_path)
    log = TaskLogger(tmp_path, emit_markers=False)
    log.transcript_tool_start("Read", {"file_path": "/a" * 200}, "t1", LogPhase.CODING)
    log.transcript_tool_start("Bash", {"command": "ls"}, "t2", LogPhase.CODING)
    log.transcript_tool_end("t1", "x" * 30000, False, LogPhase.CODING)
    log.transcript_tool_end("t2", None, True, LogPhase.CODING)
    ends = [e for e in entries() if e["kind"] == "tool_end"]
    assert [(e["tool"], e["success"]) for e in ends] == [
        ("Read", True),
        ("Bash", False),
    ]
    assert len(ends[0]["payload"]) == 30000
    assert ends[1]["payload"] == ""


def test_anthropic_style_key_is_redacted(tmp_path):
    transcript.start(tmp_path)
    transcript.record(
        "tool_start", "Bash", {"cmd": "echo sk-ant-api03-AbC_dEf-123456789"}
    )
    assert "sk-ant-" not in transcript.written_files()[0].read_text()
