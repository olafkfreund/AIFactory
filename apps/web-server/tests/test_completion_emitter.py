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
    build_worker_event,
    correlation_key,
    emit_terminal_completion,
    emit_worker_completion,
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
    assert ev["schema_version"] == "1.3"


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


# ── #45 P1: v1.3 per-worker usage (workers[] + by_provider/by_model) ──────────


def _worker(wid, provider, model, *, in_t, out_t, cost, dur=0):
    return {
        "worker_id": wid,
        "phase": "coding",
        "subtask_id": wid,
        "provider": provider,
        "model": model,
        "input_tokens": in_t,
        "output_tokens": out_t,
        "total_tokens": in_t + out_t,
        "cost_usd": cost,
        "duration_ms": dur,
    }


def test_read_usage_serial_back_compat_one_main_worker(tmp_path):
    """BACK-COMPAT: a serial single-model task still produces the same scalar
    usage block, plus a one-entry 'main' workers list."""
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "token_usage.json").write_text(json.dumps({
        "totalInputTokens": 1200, "outputTokens": 50, "totalTokens": 1250,
        "totalCostUsd": 0.5, "model": "claude-sonnet-4-6",
        "workers": {
            "main": _worker("main", "claude", "claude-sonnet-4-6",
                            in_t=1200, out_t=50, cost=0.5),
        },
    }))
    usage = read_usage(spec)
    # Scalar block is exactly the pre-#45 shape.
    assert usage["input_tokens"] == 1200
    assert usage["output_tokens"] == 50
    assert usage["total_tokens"] == 1250
    assert usage["cost_usd"] == 0.5
    assert usage["model"] == "claude-sonnet-4-6"
    # Additive: one main worker + rollups.
    assert len(usage["workers"]) == 1
    assert usage["workers"][0]["worker_id"] == "main"
    assert usage["by_provider"]["claude"]["total_tokens"] == 1250
    assert usage["by_model"]["claude-sonnet-4-6"]["total_tokens"] == 1250


def test_read_usage_two_workers_rollups(tmp_path):
    """Two workers on two providers/models → workers[] + correct rollups."""
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "token_usage.json").write_text(json.dumps({
        "totalInputTokens": 800, "outputTokens": 100, "totalTokens": 900,
        "totalCostUsd": 0.30, "model": "ollama:llama3",  # scalar = last-writer
        "workers": {
            "sub-A": _worker("sub-A", "claude", "claude-sonnet-4-6",
                             in_t=300, out_t=40, cost=0.30, dur=1500),
            "sub-B": _worker("sub-B", "ollama", "ollama:llama3",
                             in_t=500, out_t=60, cost=0.0, dur=2200),
        },
    }))
    usage = read_usage(spec)
    assert len(usage["workers"]) == 2
    # Deterministic order by worker_id.
    assert [w["worker_id"] for w in usage["workers"]] == ["sub-A", "sub-B"]

    bp = usage["by_provider"]
    assert set(bp) == {"claude", "ollama"}
    assert bp["claude"]["total_tokens"] == 340
    assert bp["claude"]["cost_usd"] == 0.30
    assert bp["ollama"]["total_tokens"] == 560
    assert bp["ollama"]["cost_usd"] == 0.0  # local → no cost
    assert bp["claude"]["workers"] == 1

    bm = usage["by_model"]
    assert set(bm) == {"claude-sonnet-4-6", "ollama:llama3"}
    assert bm["ollama:llama3"]["total_tokens"] == 560

    # Scalar fields preserved untouched.
    assert usage["input_tokens"] == 800
    assert usage["total_tokens"] == 900


def test_read_usage_no_workers_map_omits_new_fields(tmp_path):
    """An old token_usage.json without a workers map → scalar block only, no
    workers[]/by_provider/by_model keys (additive omission)."""
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "token_usage.json").write_text(json.dumps({
        "totalInputTokens": 10, "outputTokens": 5, "totalTokens": 15,
        "totalCostUsd": 0.01, "model": "claude-sonnet-4-6",
    }))
    usage = read_usage(spec)
    assert usage["total_tokens"] == 15
    assert "workers" not in usage
    assert "by_provider" not in usage
    assert "by_model" not in usage


def test_v13_usage_round_trips_through_build_completion_event(tmp_path):
    """The v1.3 usage block (workers[]/by_provider/by_model) survives
    build_completion_event and validates against the published schema."""
    jsonschema = __import__("pytest").importorskip("jsonschema")
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "token_usage.json").write_text(json.dumps({
        "totalInputTokens": 800, "outputTokens": 100, "totalTokens": 900,
        "totalCostUsd": 0.30, "model": "ollama:llama3",
        "workers": {
            "sub-A": _worker("sub-A", "claude", "claude-sonnet-4-6",
                             in_t=300, out_t=40, cost=0.30, dur=1500),
            "sub-B": _worker("sub-B", "ollama", "ollama:llama3",
                             in_t=500, out_t=60, cost=0.0, dur=2200),
        },
    }))
    ev = build_completion_event(
        task_id="t", spec_id="s", status="done", issue_number=1,
        usage=read_usage(spec),
    )
    assert ev["schema_version"] == "1.3"
    assert len(ev["usage"]["workers"]) == 2
    assert "claude" in ev["usage"]["by_provider"]
    assert "ollama:llama3" in ev["usage"]["by_model"]
    # Scalar usage fields still present for back-compat consumers.
    assert ev["usage"]["total_tokens"] == 900
    assert ev["usage"]["model"] == "ollama:llama3"

    schema = json.loads(
        (_WS / "server" / "services" / "completion_event.schema.json").read_text()
    )
    jsonschema.validate(ev, schema)  # additive fields must validate


def test_old_consumer_ignores_new_usage_fields_still_validates(tmp_path):
    """An old producer's event (scalar usage only, no workers) still validates
    against the v1.3 schema — the new fields are optional, not required."""
    jsonschema = __import__("pytest").importorskip("jsonschema")
    ev = build_completion_event(
        task_id="t", spec_id="s", status="done", issue_number=1,
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
               "cost_usd": 0.01, "model": "claude-sonnet-4-6"},
    )
    schema = json.loads(
        (_WS / "server" / "services" / "completion_event.schema.json").read_text()
    )
    jsonschema.validate(ev, schema)


# ── #45 P1: LIVE per-worker sub-event (build_worker_event / emit_worker_*) ─────


def _sample_worker(**overrides):
    rec = {
        "worker_id": "sub-A",
        "subtask_id": "sub-A",
        "phase": "coding",  # token_usage.json names it `phase`
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "input_tokens": 300,
        "output_tokens": 40,
        "total_tokens": 340,
        "cost_usd": 0.30,
        "duration_ms": 1500,
    }
    rec.update(overrides)
    return rec


def test_build_worker_event_exact_shape():
    """The live sub-event matches the EXACT v1.3 worker contract CFactory is
    being built against (status/phase + the worker{} object fields)."""
    ev = build_worker_event(
        task_id="proj:spec-9", spec_id="spec-9", issue_number=412,
        project_id="proj", worker=_sample_worker(),
        updated_at="2026-06-13T12:00:00+00:00",
    )
    assert _RFC_CORE <= set(ev)
    assert ev["service"] == "aifactory"
    assert ev["correlation_key"] == "412"
    assert ev["task_id"] == "proj:spec-9"
    assert ev["schema_version"] == "1.3"
    assert ev["status"] == "worker_done"
    assert ev["phase"] == "worker"
    assert ev["time"] == ev["updated_at"] == "2026-06-13T12:00:00+00:00"
    assert _UUID_RE.match(ev["id"])
    assert _TRACEPARENT_RE.match(ev["traceparent"])
    assert ev["worker"] == {
        "worker_id": "sub-A",
        "subtask_id": "sub-A",
        "agent_phase": "coding",
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "input_tokens": 300,
        "output_tokens": 40,
        "total_tokens": 340,
        "cost_usd": 0.30,
        "duration_ms": 1500,
    }


def test_build_worker_event_defaults_to_main_and_derives_total():
    ev = build_worker_event(
        task_id="proj:s", spec_id="s", issue_number=None,
        worker={"input_tokens": 10, "output_tokens": 5},  # no id, no total
    )
    assert ev["correlation_key"] == "af-s"  # synthetic key when no issue
    assert ev["worker"]["worker_id"] == "main"
    assert ev["worker"]["total_tokens"] == 15  # derived
    assert ev["worker"]["cost_usd"] == 0.0


def test_build_worker_event_prefers_agent_phase_over_phase():
    ev = build_worker_event(
        task_id="proj:s", spec_id="s", issue_number=1,
        worker=_sample_worker(agent_phase="qa", phase="coding"),
    )
    assert ev["worker"]["agent_phase"] == "qa"


def test_worker_event_validates_against_published_schema():
    jsonschema = __import__("pytest").importorskip("jsonschema")
    schema = json.loads(
        (_WS / "server" / "services" / "completion_event.schema.json").read_text()
    )
    ev = build_worker_event(
        task_id="proj:spec-9", spec_id="spec-9", issue_number=412,
        project_id="proj", worker=_sample_worker(provider="ollama", cost_usd=0.0),
        updated_at="2026-06-13T12:00:00+00:00",
    )
    jsonschema.validate(ev, schema)  # phase:"worker" + worker{} must validate


def test_emit_worker_completion_posts_via_same_transport(tmp_path, monkeypatch):
    """The live sub-event uses the SAME webhook transport as the terminal event."""
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

    spec = _spec_with_issue(tmp_path, 412)
    ev = emit_worker_completion(
        spec_dir=spec, task_id="proj:spec-9", spec_id="spec-9",
        project_id="proj", worker=_sample_worker(),
    )
    assert captured["url"] == "http://hook.test/c"
    assert captured["body"]["status"] == "worker_done"
    assert captured["body"]["phase"] == "worker"
    assert captured["body"]["worker"]["worker_id"] == "sub-A"
    assert ev["worker"]["worker_id"] == "sub-A"


def test_emit_worker_completion_is_best_effort_on_raising_transport(tmp_path, monkeypatch):
    """A raising transport MUST NOT propagate — emission is best-effort."""
    monkeypatch.setenv("AIFACTORY_COMPLETION_WEBHOOK", "http://hook.test/c")

    def _boom(req, timeout=None):
        raise OSError("refused")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    spec = _spec_with_issue(tmp_path, 1)
    # Must not raise — returns the built event regardless of transport failure.
    ev = emit_worker_completion(
        spec_dir=spec, task_id="t", spec_id="s", project_id="p",
        worker=_sample_worker(),
    )
    assert ev["status"] == "worker_done"


def test_worker_sentinel_written_to_per_worker_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_COMPLETION_SENTINEL", "1")
    monkeypatch.delenv("AIFACTORY_COMPLETION_WEBHOOK", raising=False)
    spec = _spec_with_issue(tmp_path, 412)
    emit_worker_completion(
        spec_dir=spec, task_id="proj:spec-9", spec_id="spec-9",
        project_id="proj", worker=_sample_worker(worker_id="sub-A"),
    )
    # Per-worker sentinel — does NOT clobber the terminal COMPLETED.json.
    sentinel = spec / "WORKER.sub-A.json"
    assert sentinel.exists()
    assert json.loads(sentinel.read_text())["phase"] == "worker"
    assert not (spec / "COMPLETED.json").exists()


def test_terminal_event_unchanged_and_serial_main_worker_subevent_fires(tmp_path, monkeypatch):
    """The terminal event still fires unchanged; the serial 'main' worker also
    gets a live sub-event on completion (different status/phase)."""
    monkeypatch.setenv("AIFACTORY_COMPLETION_WEBHOOK", "http://hook.test/c")
    monkeypatch.delenv("AIFACTORY_COMPLETION_SENTINEL", raising=False)
    posts = []

    class _Resp:
        def close(self):
            pass

    def _fake(req, timeout=None):
        posts.append(json.loads(req.data.decode()))
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake)

    spec = _spec_with_issue(tmp_path, 412)
    (spec / "token_usage.json").write_text(json.dumps({
        "totalInputTokens": 1200, "outputTokens": 50, "totalTokens": 1250,
        "totalCostUsd": 0.5, "model": "claude-sonnet-4-6",
        "workers": {
            "main": _worker("main", "claude", "claude-sonnet-4-6",
                            in_t=1200, out_t=50, cost=0.5),
        },
    }))
    ev = emit_terminal_completion(
        spec, task_id="proj:spec-9", project_id="proj", spec_id="spec-9",
        status="completed",
    )
    # Terminal event itself is UNCHANGED: completion event, with usage rollups,
    # NOT a worker sub-event.
    assert ev["status"] == "completed"
    assert ev["phase"] == "act"
    assert "worker" not in ev
    assert ev["usage"]["total_tokens"] == 1250
    assert len(ev["usage"]["workers"]) == 1
    # Two POSTs landed: the live 'main' sub-event AND the terminal event.
    statuses = sorted(p["status"] for p in posts)
    assert statuses == ["completed", "worker_done"]
    worker_post = next(p for p in posts if p["status"] == "worker_done")
    assert worker_post["phase"] == "worker"
    assert worker_post["worker"]["worker_id"] == "main"
    assert worker_post["correlation_key"] == ev["correlation_key"] == "412"


def test_terminal_event_no_serial_subevent_for_multiple_workers(tmp_path, monkeypatch):
    """When >1 worker (parallel) is present, the terminal emit does NOT also fire
    a 'main' sub-event — parallel workers already emitted theirs live."""
    monkeypatch.setenv("AIFACTORY_COMPLETION_WEBHOOK", "http://hook.test/c")
    monkeypatch.delenv("AIFACTORY_COMPLETION_SENTINEL", raising=False)
    posts = []

    class _Resp:
        def close(self):
            pass

    def _fake(req, timeout=None):
        posts.append(json.loads(req.data.decode()))
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake)

    spec = _spec_with_issue(tmp_path, 412)
    (spec / "token_usage.json").write_text(json.dumps({
        "totalInputTokens": 800, "outputTokens": 100, "totalTokens": 900,
        "totalCostUsd": 0.30, "model": "ollama:llama3",
        "workers": {
            "sub-A": _worker("sub-A", "claude", "claude-sonnet-4-6",
                             in_t=300, out_t=40, cost=0.30),
            "sub-B": _worker("sub-B", "ollama", "ollama:llama3",
                             in_t=500, out_t=60, cost=0.0),
        },
    }))
    emit_terminal_completion(
        spec, task_id="proj:spec-9", project_id="proj", spec_id="spec-9",
        status="completed",
    )
    # Only the terminal event — no extra worker sub-event from the terminal path.
    assert [p["status"] for p in posts] == ["completed"]
