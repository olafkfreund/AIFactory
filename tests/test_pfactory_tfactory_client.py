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
    tf = {"lanes": ["unit", "api"], "frameworks": {"unit": "pytest"},
          "coverage_target": 0.85}
    payload = build_handoff_payload("001-x", {"title": "t"}, c, {}, tfactory=tf)
    assert payload["tfactory"] == tf


def test_load_tfactory_block_reads_plan(tmp_path):
    plan = {"feature": "x", "tfactory": {"lanes": ["unit"], "frameworks": {"unit": "pytest"}}}
    (tmp_path / "implementation_plan.json").write_text(json.dumps(plan))
    assert load_tfactory_block(tmp_path) == {"lanes": ["unit"], "frameworks": {"unit": "pytest"}}


def test_load_tfactory_block_absent_returns_empty(tmp_path):
    (tmp_path / "implementation_plan.json").write_text(json.dumps({"feature": "x"}))
    assert load_tfactory_block(tmp_path) == {}


def test_load_tfactory_block_no_plan_returns_empty(tmp_path):
    assert load_tfactory_block(tmp_path) == {}


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
