"""Tests for the RFC-0001 completion-event emitter (AIFactory side, #342)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

from server.services.completion import (  # noqa: E402
    SERVICE_NAME,
    build_completion_event,
    correlation_key,
    emit_terminal_completion,
    notify_completion,
    read_issue_number,
    read_usage,
)

_RFC_CORE = {"correlation_key", "service", "task_id", "status", "phase", "updated_at"}


def _spec_with_issue(tmp_path: Path, issue=None, *, key="metadata") -> Path:
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    reqs: dict = {}
    if issue is not None:
        if key == "metadata":
            reqs["metadata"] = {"githubIssueNumber": issue}
        else:
            reqs["githubIssue"] = {"number": issue}
    (spec / "requirements.json").write_text(json.dumps(reqs))
    return spec


# ── issue-number reading ─────────────────────────────────────────────────────


def test_read_issue_from_metadata(tmp_path):
    assert read_issue_number(_spec_with_issue(tmp_path, 412)) == 412


def test_read_issue_from_github_issue_block(tmp_path):
    assert read_issue_number(_spec_with_issue(tmp_path, 77, key="githubIssue")) == 77


def test_read_issue_absent_is_none(tmp_path):
    assert read_issue_number(_spec_with_issue(tmp_path)) is None
    assert read_issue_number(tmp_path / "nope") is None   # no requirements.json


# ── correlation key + envelope ───────────────────────────────────────────────


def test_correlation_key_issue_vs_synthetic():
    assert correlation_key("spec-9", 412) == "412"
    assert correlation_key("spec-9", None) == "af-spec-9"


def test_envelope_has_rfc_core_fields():
    ev = build_completion_event(
        task_id="proj:spec-9", spec_id="spec-9", status="done", issue_number=412,
        updated_at="2026-06-04T16:00:00+00:00",
    )
    assert _RFC_CORE <= set(ev)
    assert ev["service"] == SERVICE_NAME == "aifactory"
    assert ev["correlation_key"] == "412"
    assert ev["task_id"] == "proj:spec-9"
    assert ev["correlation"]["issue_number"] == 412


def test_envelope_synthetic_key_when_no_issue():
    ev = build_completion_event(
        task_id="proj:spec-9", spec_id="spec-9", status="done", issue_number=None,
    )
    assert ev["correlation_key"] == "af-spec-9"
    assert ev["correlation"]["issue_number"] is None


# ── transport (best-effort) ──────────────────────────────────────────────────


def test_webhook_posts_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_COMPLETION_WEBHOOK", "http://hook.test/c")
    monkeypatch.delenv("AIFACTORY_COMPLETION_SENTINEL", raising=False)
    captured = {}

    class _Resp:
        def close(self):
            pass

    def _fake(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake)

    ev = build_completion_event(task_id="t", spec_id="s", status="done", issue_number=1)
    notify_completion(ev)
    assert captured["url"] == "http://hook.test/c"
    assert captured["body"]["service"] == "aifactory"


def test_webhook_failure_is_swallowed(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_COMPLETION_WEBHOOK", "http://hook.test/c")

    def _boom(req, timeout=None):
        raise OSError("refused")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    # Must not raise.
    notify_completion(build_completion_event(task_id="t", spec_id="s", status="done", issue_number=1))


def test_sentinel_written_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_COMPLETION_SENTINEL", "1")
    monkeypatch.delenv("AIFACTORY_COMPLETION_WEBHOOK", raising=False)
    spec = tmp_path / "spec"
    ev = build_completion_event(task_id="t", spec_id="s", status="done", issue_number=1)
    notify_completion(ev, spec_dir=spec)
    assert json.loads((spec / "COMPLETED.json").read_text())["correlation_key"] == "1"


# ── end-to-end ───────────────────────────────────────────────────────────────


def test_failed_terminal_emits_failed_status(tmp_path, monkeypatch):
    """A failed build (never marked 'done') still emits a conformant event."""
    monkeypatch.delenv("AIFACTORY_COMPLETION_WEBHOOK", raising=False)
    monkeypatch.delenv("AIFACTORY_COMPLETION_SENTINEL", raising=False)
    spec = _spec_with_issue(tmp_path, 412)
    ev = emit_terminal_completion(
        spec, task_id="proj:spec-9", project_id="proj", spec_id="spec-9", status="failed",
    )
    assert _RFC_CORE <= set(ev)
    assert ev["service"] == "aifactory"
    assert ev["status"] == "failed"
    assert ev["correlation_key"] == "412"


def test_emit_terminal_completion_builds_from_spec(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFACTORY_COMPLETION_WEBHOOK", raising=False)
    monkeypatch.delenv("AIFACTORY_COMPLETION_SENTINEL", raising=False)
    spec = _spec_with_issue(tmp_path, 412)
    ev = emit_terminal_completion(
        spec, task_id="proj:spec-9", project_id="proj", spec_id="spec-9", status="done",
    )
    assert ev["service"] == "aifactory"
    assert ev["correlation_key"] == "412"
    assert ev["status"] == "done"
    assert ev["task_id"] == "proj:spec-9"


# ── RFC-0001 v1.1 usage block ────────────────────────────────────────────────


def _write_usage(spec_dir: Path, **fields) -> None:
    (spec_dir / "token_usage.json").write_text(json.dumps(fields))


def test_read_usage_maps_token_attribution_fields(tmp_path):
    spec = tmp_path / "spec"
    spec.mkdir()
    _write_usage(
        spec,
        totalInputTokens=2400,
        outputTokens=100,
        totalTokens=2500,
        totalCostUsd=1.25,
        model="claude-sonnet-4-6",
    )
    assert read_usage(spec) == {
        "input_tokens": 2400,
        "output_tokens": 100,
        "total_tokens": 2500,
        "cost_usd": 1.25,
        "model": "claude-sonnet-4-6",
    }


def test_read_usage_none_when_absent_or_empty(tmp_path):
    spec = tmp_path / "spec"
    spec.mkdir()
    assert read_usage(spec) is None  # no token_usage.json
    _write_usage(spec, totalInputTokens=0, outputTokens=0)
    assert read_usage(spec) is None  # zero tokens → omit the block


def test_read_usage_derives_total_when_missing(tmp_path):
    spec = tmp_path / "spec"
    spec.mkdir()
    _write_usage(spec, totalInputTokens=10, outputTokens=5)  # no totalTokens
    assert read_usage(spec)["total_tokens"] == 15


def test_envelope_includes_usage_when_supplied():
    ev = build_completion_event(
        task_id="t", spec_id="s", status="done", issue_number=1,
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
               "cost_usd": 0.01, "model": "claude-sonnet-4-6"},
    )
    assert ev["usage"]["total_tokens"] == 12
    assert ev["schema_version"] == "1.2"


def test_envelope_omits_usage_when_absent():
    ev = build_completion_event(task_id="t", spec_id="s", status="done", issue_number=1)
    assert "usage" not in ev  # additive — omitted when there's nothing to report


def test_emit_reads_usage_from_token_usage_json(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFACTORY_COMPLETION_WEBHOOK", raising=False)
    monkeypatch.delenv("AIFACTORY_COMPLETION_SENTINEL", raising=False)
    spec = _spec_with_issue(tmp_path, 412)
    _write_usage(
        spec, totalInputTokens=2400, outputTokens=100, totalTokens=2500,
        totalCostUsd=1.25, model="claude-sonnet-4-6",
    )
    ev = emit_terminal_completion(
        spec, task_id="proj:spec-9", project_id="proj", spec_id="spec-9", status="done",
    )
    assert ev["usage"] == {
        "input_tokens": 2400, "output_tokens": 100, "total_tokens": 2500,
        "cost_usd": 1.25, "model": "claude-sonnet-4-6",
    }


# ── #466 additive envelope upgrade: id + CloudEvents-core + trace context ─────

import re  # noqa: E402

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def _base_event(**overrides):
    kw = dict(task_id="proj:spec-9", spec_id="spec-9", status="done", issue_number=412)
    kw.update(overrides)
    return build_completion_event(**kw)


def test_envelope_keeps_all_legacy_fields():
    """Additive: nothing the old consumers read was removed or renamed."""
    ev = _base_event(updated_at="2026-06-04T16:00:00+00:00")
    for legacy in ("correlation_key", "service", "task_id", "status", "phase",
                   "updated_at", "correlation", "schema_version", "event"):
        assert legacy in ev, legacy
    assert ev["service"] == "aifactory"
    assert ev["event"] == "completion"


def test_envelope_has_idempotency_id():
    ev = _base_event()
    assert _UUID_RE.match(ev["id"]), ev["id"]


def test_event_id_unique_per_call_but_pinnable():
    # Distinct events get distinct ids…
    assert _base_event()["id"] != _base_event()["id"]
    # …but an explicit id makes a rebuild reproduce the same event (relay dedup).
    pinned = _base_event(event_id="11111111-1111-4111-8111-111111111111")
    again = _base_event(event_id="11111111-1111-4111-8111-111111111111")
    assert pinned["id"] == again["id"] == "11111111-1111-4111-8111-111111111111"


def test_envelope_has_cloudevents_core_fields(monkeypatch):
    monkeypatch.delenv("AIFACTORY_EVENT_SOURCE", raising=False)
    ev = _base_event(updated_at="2026-06-04T16:00:00+00:00")
    assert ev["specversion"] == "1.0"
    assert ev["type"] == "io.factory.aifactory.completion"
    assert ev["source"] == "/aifactory"
    # CloudEvents `time` mirrors the occurrence time.
    assert ev["time"] == ev["updated_at"] == "2026-06-04T16:00:00+00:00"


def test_source_overridable_by_env(monkeypatch):
    monkeypatch.setenv("AIFACTORY_EVENT_SOURCE", "/aifactory/prod-eu")
    assert _base_event()["source"] == "/aifactory/prod-eu"


def test_traceparent_is_valid_w3c_and_propagates():
    ev = _base_event()
    assert _TRACEPARENT_RE.match(ev["traceparent"]), ev["traceparent"]
    assert "tracestate" not in ev  # omitted unless supplied
    inbound = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    propagated = _base_event(traceparent=inbound, tracestate="rojo=00f067aa0ba902b7")
    assert propagated["traceparent"] == inbound
    assert propagated["tracestate"] == "rojo=00f067aa0ba902b7"


def test_envelope_validates_against_published_schema():
    """AC #466: the event validates against the published RFC-0001/CloudEvents
    schema (run in CI)."""
    jsonschema = __import__("pytest").importorskip("jsonschema")
    schema = json.loads(
        (_WS / "server" / "services" / "completion_event.schema.json").read_text()
    )
    ev = _base_event(
        updated_at="2026-06-04T16:00:00+00:00",
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
               "cost_usd": 0.01, "model": "claude-sonnet-4-6"},
    )
    jsonschema.validate(ev, schema)  # raises on non-conformance


def test_synthetic_correlation_event_validates_against_schema():
    jsonschema = __import__("pytest").importorskip("jsonschema")
    schema = json.loads(
        (_WS / "server" / "services" / "completion_event.schema.json").read_text()
    )
    ev = build_completion_event(
        task_id="proj:spec-9", spec_id="spec-9", status="failed", issue_number=None,
    )
    jsonschema.validate(ev, schema)
