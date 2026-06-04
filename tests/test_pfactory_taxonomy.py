"""Tests for the PFactory tag-taxonomy classifier (epic #327, issue #329).

Covers ``pfactory.taxonomy``: the governed-spec decision (``pfactory`` +
``handoff:aifactory``), epic detection, descriptive-taxonomy extraction, the
``requirements.json`` paths, and graceful tolerance of malformed input.
"""

from __future__ import annotations

from pfactory.taxonomy import (
    classify_labels,
    classify_requirements,
    is_governed_requirements,
)

# ── governed-spec decision (the core of #329) ──────────────────────────────


def test_pfactory_plus_handoff_aifactory_is_governed():
    c = classify_labels(["pfactory", "handoff:aifactory", "epic"])
    assert c.is_pfactory is True
    assert c.handoff == "aifactory"
    assert c.governed is True
    assert c.is_epic is True


def test_pfactory_without_aifactory_handoff_is_not_governed():
    # Routed to TFactory, not AIFactory — AIFactory must not skip its gate.
    c = classify_labels(["pfactory", "handoff:tfactory"])
    assert c.is_pfactory is True
    assert c.handoff == "tfactory"
    assert c.governed is False


def test_aifactory_handoff_without_pfactory_marker_is_not_governed():
    # Missing the mandatory marker → treat as ordinary work, no skip.
    c = classify_labels(["handoff:aifactory"])
    assert c.is_pfactory is False
    assert c.governed is False


def test_non_pfactory_issue_is_inert():
    c = classify_labels(["bug", "backend", "priority:high"])
    assert c.is_pfactory is False
    assert c.handoff is None
    assert c.governed is False
    assert c.is_epic is False


# ── descriptive taxonomy (feeds #330/#331) ─────────────────────────────────


def test_extracts_type_plan_type_priority_sev():
    c = classify_labels(
        [
            "pfactory",
            "handoff:aifactory",
            "type:infra",
            "type:cicd",
            "plan-type:infra-change",
            "priority:p0",
            "sev:high",
        ]
    )
    assert set(c.types) == {"infra", "cicd"}
    assert c.plan_type == "infra-change"
    assert c.priority == "p0"
    assert c.sev == "high"


def test_priority_p_scheme_distinct_from_legacy():
    # The PFactory p0..p3 scheme is surfaced verbatim; legacy high/medium/low
    # is just whatever the first priority:* label says.
    assert classify_labels(["priority:p2"]).priority == "p2"
    assert classify_labels(["priority:high"]).priority == "high"


# ── input tolerance (pickup must never crash) ──────────────────────────────


def test_accepts_label_dicts_like_gh_json():
    c = classify_labels([{"name": "pfactory"}, {"name": "handoff:aifactory"}])
    assert c.governed is True


def test_label_matching_is_case_insensitive():
    c = classify_labels(["PFactory", "Handoff:AIFactory"])
    assert c.governed is True


def test_tolerates_none_and_garbage():
    for bad in (None, "pfactory", {"name": "pfactory"}, 42, [None, "", {"x": 1}]):
        c = classify_labels(bad)  # must not raise
        assert c.governed is False


# ── requirements.json paths ────────────────────────────────────────────────


def test_classify_requirements_reads_github_labels():
    req = {"githubIssue": {"labels": ["pfactory", "handoff:aifactory"]}}
    assert classify_requirements(req).governed is True


def test_classify_requirements_falls_back_to_metadata_labels():
    # PFactory-direct-write path: no githubIssue, labels under metadata.
    req = {"metadata": {"labels": ["pfactory", "handoff:aifactory"]}}
    assert classify_requirements(req).governed is True


def test_is_governed_prefers_explicit_flag():
    # Explicit persisted flag wins over (absent) labels.
    assert is_governed_requirements({"governed": True}) is True
    assert (
        is_governed_requirements(
            {
                "governed": False,
                "githubIssue": {"labels": ["pfactory", "handoff:aifactory"]},
            }
        )
        is False
    )


def test_is_governed_reads_pfactory_block():
    assert is_governed_requirements({"pfactory": {"governed": True}}) is True


def test_is_governed_falls_back_to_label_classification():
    req = {"githubIssue": {"labels": ["pfactory", "handoff:aifactory"]}}
    assert is_governed_requirements(req) is True


def test_is_governed_non_pfactory_requirements_false():
    req = {"githubIssue": {"labels": ["bug"]}}
    assert is_governed_requirements(req) is False
    assert is_governed_requirements({}) is False
    assert is_governed_requirements(None) is False
