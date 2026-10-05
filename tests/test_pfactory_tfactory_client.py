"""Tests for the outbound TFactory transport client (epic #327, #337).

Covers ``pfactory.tfactory_client``: config from env, payload shape, and
``send_handoff`` across not-configured / success / http-error / exception —
all with an injected poster so no network is touched.
"""

from __future__ import annotations

import json

from pfactory.taxonomy import classify_labels
from pfactory.tfactory_client import (
    build_handoff_payload,
    load_tfactory_block,
    send_handoff,
    tfactory_config,
)

# ── config ─────────────────────────────────────────────────────────────────


def test_config_reads_env_and_strips_trailing_slash():
    cfg = tfactory_config(
        {"TFACTORY_BASE_URL": "https://tf.example/", "TFACTORY_TOKEN": "t"}
    )
    assert cfg["base_url"] == "https://tf.example"
    assert cfg["token"] == "t"
    assert cfg["path"] == "/api/specs/ingest"  # default (#517)


def test_config_empty_when_unset():
    assert tfactory_config({})["base_url"] == ""


# ── payload ──────────────────────────────────────────────────────────────


def test_build_payload_carries_taxonomy_and_meta():
    req = {
        "title": "Add tests",
        "description": "cover the parser",
        "githubIssue": {"labels": ["pfactory", "handoff:tfactory", "type:testing"]},
    }
    c = classify_labels(req["githubIssue"]["labels"])
    payload = build_handoff_payload("001-x", req, c, {"plan_id": "p1", "citations": []})
    assert payload["source"] == "aifactory"
    assert payload["spec_id"] == "001-x"
    assert payload["handoff"] == "tfactory"
    assert "testing" in payload["types"]
    assert payload["pfactory_meta"]["plan_id"] == "p1"
    assert payload["tfactory"] == {}  # absent => empty, TFactory infers


# ── tfactory block (RFC-0002, #428) ─────────────────────────────────────────


def test_payload_carries_tfactory_block_when_provided():
    c = classify_labels(["pfactory", "handoff:tfactory"])
    tf = {
        "lanes": ["unit", "api"],
        "frameworks": {"unit": "pytest"},
        "coverage_target": 0.85,
    }
    payload = build_handoff_payload("001-x", {"title": "t"}, c, {}, tfactory=tf)
    assert payload["tfactory"] == tf


def test_load_tfactory_block_reads_plan(tmp_path):
    plan = {
        "feature": "x",
        "tfactory": {"lanes": ["unit"], "frameworks": {"unit": "pytest"}},
    }
    (tmp_path / "implementation_plan.json").write_text(json.dumps(plan))
    assert load_tfactory_block(tmp_path) == {
        "lanes": ["unit"],
        "frameworks": {"unit": "pytest"},
    }


def test_load_tfactory_block_absent_returns_empty(tmp_path):
    (tmp_path / "implementation_plan.json").write_text(json.dumps({"feature": "x"}))
    assert load_tfactory_block(tmp_path) == {}


def test_load_tfactory_block_no_plan_returns_empty(tmp_path):
    assert load_tfactory_block(tmp_path) == {}


# ── verify-lane model propagation (Ollama / non-default builds) ──────────────

from pfactory.tfactory_client import _verify_phase_models  # noqa: E402


def test_verify_phase_models_maps_all_lanes_to_qa_model(tmp_path):
    (tmp_path / "task_metadata.json").write_text(
        json.dumps(
            {
                "isAutoProfile": True,
                "phaseModels": {
                    "coding": "openai-compatible:qwen3-coder:480b",
                    "qa": "openai-compatible:gpt-oss:120b",
                    "planning": "openai-compatible:gpt-oss:120b",
                },
            }
        )
    )
    pm = _verify_phase_models(tmp_path)
    # Verify is judgment work → every TFactory lane uses the build's qa model,
    # NOT the coder model used for the build's coding phase.
    assert set(pm) == {"spec", "planning", "coding", "qa", "qa_fixer", "test_gen"}
    assert all(v == "openai-compatible:gpt-oss:120b" for v in pm.values())


def test_verify_phase_models_falls_back_to_planning_then_spec(tmp_path):
    (tmp_path / "task_metadata.json").write_text(
        json.dumps({"phaseModels": {"planning": "openai-compatible:gpt-oss:120b"}})
    )
    assert _verify_phase_models(tmp_path)["coding"] == "openai-compatible:gpt-oss:120b"


def test_verify_phase_models_empty_without_phasemodels(tmp_path):
    # No task_metadata → {} (default behaviour preserved; verify uses its default).
    assert _verify_phase_models(tmp_path) == {}
    (tmp_path / "task_metadata.json").write_text(json.dumps({"model": "sonnet"}))
    assert _verify_phase_models(tmp_path) == {}


# ── send_handoff ────────────────────────────────────────────────────────────


async def test_send_not_configured_is_noop():
    res = await send_handoff({"x": 1}, config=tfactory_config({}))
    assert res == {"sent": False, "reason": "not_configured"}


async def test_send_success_via_injected_poster():
    captured = {}

    async def poster(url, payload, headers):
        captured["url"] = url
        captured["auth"] = headers.get("Authorization")
        return {"status": 202, "ok": True, "body": "queued"}

    cfg = {"base_url": "https://tf.example", "token": "secret", "path": "/api/handoff"}
    res = await send_handoff({"spec_id": "x"}, config=cfg, poster=poster)
    assert res["sent"] is True
    assert res["status"] == 202
    assert captured["url"] == "https://tf.example/api/handoff"
    assert captured["auth"] == "Bearer secret"


async def test_send_http_error_reported_not_raised():
    async def poster(url, payload, headers):
        return {"status": 500, "ok": False, "body": "boom"}

    res = await send_handoff(
        {}, config={"base_url": "https://tf.example"}, poster=poster
    )
    assert res["sent"] is False
    assert res["reason"] == "http_error"
    assert res["status"] == 500


async def test_send_transport_exception_is_caught():
    async def poster(url, payload, headers):
        raise ConnectionError("refused")

    res = await send_handoff(
        {}, config={"base_url": "https://tf.example"}, poster=poster
    )
    assert res["sent"] is False
    assert res["reason"] == "error"
    assert "refused" in res["error"]


# ── the verify models must WIN the merge (#1638) ─────────────────────────────
# The three tests above exercise _verify_phase_models in isolation, and they all
# passed while the defect was live: the function produced the right map and the
# merge then threw most of it away. These test the MERGED result, and the last
# one tests what TFactory would STORE — the assertion whose absence let a wrong
# diagnosis stand (the issue first blamed an unread field; the field is read).

from pfactory.tfactory_client import build_ingest_payload  # noqa: E402

# The real data from spec 025-myfriends-web-remediation-v2-o.
_BUILD_PHASE_MODELS = {
    "coding": "claude-sonnet-4-6",
    "qa": "claude-sonnet-4-6",
    "qa_fixer": "claude-sonnet-4-5-20250929",
    "planning": "gemini",
    "test_gen": "claude-sonnet-4-6",
}
# TFactory agents/tools_pkg/tools/task_control.py:472 copies exactly these five
# keys into its own task_metadata.json. test_gen is deliberately excluded there,
# which is why its presence downstream proves nothing either way.
_TFACTORY_PROJECTED_KEYS = ("spec", "planning", "coding", "qa", "qa_fixer")


def _spec_dir_with_contract_phase_models(tmp_path, incoming: dict) -> object:
    """A spec dir whose build declares _BUILD_PHASE_MODELS and whose stashed
    contract already carries ``incoming`` under execution.phase_models."""
    (tmp_path / "task_metadata.json").write_text(
        json.dumps({"isAutoProfile": True, "phaseModels": _BUILD_PHASE_MODELS})
    )
    (tmp_path / "requirements.json").write_text(json.dumps({"acceptance_criteria": []}))
    ctx = tmp_path / "context"
    ctx.mkdir(exist_ok=True)
    (ctx / "task_contract.json").write_text(
        json.dumps(
            {
                "contract_version": "2",
                "approval": {"approved_by": "pfactory"},
                "execution": {"phase_models": dict(incoming)},
            }
        )
    )
    return tmp_path


def _merged_phase_models(tmp_path) -> dict:
    payload = build_ingest_payload(tmp_path, "025-spec")
    contract = payload.get("contract") or {}
    return ((contract.get("execution") or {}).get("phase_models")) or {}


def test_verify_models_beat_an_incoming_contract_phase_models(tmp_path):
    """Every phase runs on the build's qa model, even when the contract disagrees.

    The incoming contract carries the BUILD's map, including planning=gemini.
    Before the fix the incoming map won and verification planned on gemini.
    """
    _spec_dir_with_contract_phase_models(tmp_path, _BUILD_PHASE_MODELS)
    merged = _merged_phase_models(tmp_path)
    qa_model = _BUILD_PHASE_MODELS["qa"]

    # Named individually rather than asserted in bulk: these two are the keys
    # that carried the defect, and a bulk assertion would not say which broke.
    assert merged.get("planning") == qa_model, (
        f"planning is {merged.get('planning')!r}; the incoming contract won the "
        "merge, so verification would run on the build's planning provider"
    )
    assert merged.get("qa_fixer") == qa_model, (
        f"qa_fixer is {merged.get('qa_fixer')!r} — the build's value, which is "
        "the tell that the incoming map won the merge"
    )
    assert all(v == qa_model for v in merged.values()), merged


def test_test_gen_survives_the_merge(tmp_path):
    """Guards against satisfying the above by dropping keys instead of winning."""
    _spec_dir_with_contract_phase_models(tmp_path, _BUILD_PHASE_MODELS)
    merged = _merged_phase_models(tmp_path)
    assert set(merged) >= {
        "spec",
        "planning",
        "coding",
        "qa",
        "qa_fixer",
        "test_gen",
    }, merged


def test_what_tfactory_would_store_uses_the_qa_model(tmp_path):
    """Assert on the value TFactory ends up with, not the one we send.

    TFactory projects five keys out of the contract into its own
    task_metadata.json, and `get_phase_model` reads that. A test of what
    AIFactory sends passes whether or not the projection yields the right
    planning model — which is exactly how the original defect survived.
    """
    _spec_dir_with_contract_phase_models(tmp_path, _BUILD_PHASE_MODELS)
    merged = _merged_phase_models(tmp_path)
    stored = {
        k: merged[k] for k in _TFACTORY_PROJECTED_KEYS if isinstance(merged.get(k), str)
    }
    assert stored.get("planning") == _BUILD_PHASE_MODELS["qa"], stored
    assert "gemini" not in stored.values(), stored
