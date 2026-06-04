"""Tests for the PFactory metadata parser + planner surfacing (epic #327, #330).

Covers ``pfactory.metadata`` (parse / load / render) and the round-trip into
``planner_lib.context.ContextLoader`` so citations reach the planner context.
"""

from __future__ import annotations

import json
from pathlib import Path

from pfactory.metadata import (
    load_pfactory_metadata,
    parse_pfactory_meta,
    render_pfactory_context,
)

META_BLOCK = """\
Some issue body describing the work.

<!-- pfactory:meta
plan_id: plan-abc-123
plan_type: software-service
category: software
priority: p1
risk: medium
cost_monthly_usd: 2492.58
effort_points: 39
effort_days: [15.6, 39.0]
access_verified: true
citations:
  - why: "Load balancer needs an unauthenticated health probe"
    uri: "https://example.com/runbook#health"
    source: "Ops runbook"
taxonomy: v1
-->
"""


# ── parse_pfactory_meta ────────────────────────────────────────────────────


def test_parse_round_trips_all_fields():
    meta = parse_pfactory_meta(META_BLOCK)
    assert meta is not None
    assert meta["plan_id"] == "plan-abc-123"
    assert meta["plan_type"] == "software-service"
    assert meta["priority"] == "p1"
    assert meta["risk"] == "medium"
    assert meta["cost_monthly_usd"] == 2492.58
    assert meta["effort_points"] == 39
    assert meta["effort_days"] == [15.6, 39.0]
    assert meta["access_verified"] is True
    assert meta["taxonomy"] == "v1"
    assert meta["citations"][0]["source"] == "Ops runbook"
    assert meta["citations"][0]["uri"].endswith("#health")


def test_parse_returns_none_without_block():
    assert parse_pfactory_meta("just a normal issue body, no meta") is None


def test_parse_tolerates_malformed_and_nonstring():
    # Unterminated flow sequence → YAML raises → None, never propagates.
    assert parse_pfactory_meta("<!-- pfactory:meta\neffort_days: [1, 2 -->") is None
    # Valid YAML that isn't a mapping (a list / scalar) → None.
    assert parse_pfactory_meta("<!-- pfactory:meta\n- a\n- b\n-->") is None
    assert parse_pfactory_meta(None) is None
    assert parse_pfactory_meta(42) is None
    # Empty block.
    assert parse_pfactory_meta("<!-- pfactory:meta -->") is None


def test_parse_is_case_insensitive_on_marker():
    assert parse_pfactory_meta("<!-- PFactory:Meta\nplan_id: x\n-->")["plan_id"] == "x"


# ── load_pfactory_metadata (source resolution) ─────────────────────────────


def test_load_prefers_requirements_metadata(tmp_path: Path):
    req = {"metadata": {"plan_id": "from-metadata", "taxonomy": "v1"}}
    (tmp_path / "requirements.json").write_text(json.dumps(req))
    # spec.md also has a (different) block — metadata must win.
    (tmp_path / "spec.md").write_text(META_BLOCK)
    meta = load_pfactory_metadata(tmp_path)
    assert meta["plan_id"] == "from-metadata"


def test_load_falls_back_to_description_body(tmp_path: Path):
    req = {"description": META_BLOCK}  # no pfactory-ish metadata key
    (tmp_path / "requirements.json").write_text(json.dumps(req))
    meta = load_pfactory_metadata(tmp_path)
    assert meta["plan_id"] == "plan-abc-123"


def test_load_falls_back_to_spec_md(tmp_path: Path):
    (tmp_path / "spec.md").write_text(META_BLOCK)
    meta = load_pfactory_metadata(tmp_path)
    assert meta["plan_id"] == "plan-abc-123"


def test_load_ignores_non_pfactory_requirements_metadata(tmp_path: Path):
    # requirements.json["metadata"] holds AIFactory phase config, not PFactory.
    req = {"metadata": {"phaseModels": {"coder": "sonnet"}}}
    (tmp_path / "requirements.json").write_text(json.dumps(req))
    assert load_pfactory_metadata(tmp_path) is None


def test_load_returns_none_when_absent(tmp_path: Path):
    assert load_pfactory_metadata(tmp_path) is None


# ── render_pfactory_context ────────────────────────────────────────────────


def test_render_includes_cost_effort_and_citations():
    meta = parse_pfactory_meta(META_BLOCK)
    out = render_pfactory_context(meta)
    assert "PFactory Governance Context" in out
    assert "2492.58" in out
    assert "39" in out
    assert "Access verified" in out
    assert "### Citations" in out
    assert "Load balancer needs an unauthenticated health probe" in out
    assert "Ops runbook" in out


def test_render_partial_metadata_only_shows_present_fields():
    out = render_pfactory_context({"plan_id": "x", "priority": "p0"})
    assert "Plan ID" in out and "Priority" in out
    assert "Estimated monthly cost" not in out
    assert "### Citations" not in out


def test_render_empty_is_blank():
    assert render_pfactory_context({}) == ""
    assert render_pfactory_context(None) == ""


# ── round-trip into the planner context ────────────────────────────────────


def test_context_loader_surfaces_citations_into_planner(tmp_path: Path):
    from planner_lib.context import ContextLoader

    (tmp_path / "spec.md").write_text("# Add /healthz\n\nAdd the endpoint.\n")
    (tmp_path / "requirements.json").write_text(
        json.dumps({"metadata": parse_pfactory_meta(META_BLOCK)})
    )

    ctx = ContextLoader(tmp_path).load_context()

    # Programmatic metadata is attached for #331.
    assert ctx.pfactory_metadata is not None
    assert ctx.pfactory_metadata["plan_id"] == "plan-abc-123"
    # Citations + cost are surfaced in the spec content the planner sees.
    assert "PFactory Governance Context" in ctx.spec_content
    assert "Ops runbook" in ctx.spec_content
    assert "2492.58" in ctx.spec_content
    # Original spec text is preserved.
    assert "Add the endpoint." in ctx.spec_content


def test_context_loader_without_pfactory_metadata_is_unchanged(tmp_path: Path):
    from planner_lib.context import ContextLoader

    (tmp_path / "spec.md").write_text("# Plain task\n\nDo the thing.\n")
    ctx = ContextLoader(tmp_path).load_context()
    assert ctx.pfactory_metadata is None
    assert "PFactory Governance Context" not in ctx.spec_content
