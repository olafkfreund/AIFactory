#!/usr/bin/env python3
"""
Tests for build artifacts + async review feedback (Phase 6 of #397)
===================================================================

ArtifactWriter writes reviewable artifacts + a manifest (mirrored to the source
spec); post_artifact_feedback routes a human comment into the inbox so the agent
picks it up between turns (async review). Reuses the inbox post_message added in
this phase.
"""

from pathlib import Path

import pytest
from agents.artifacts import (
    KIND_PLAN,
    ArtifactWriter,
    list_artifacts,
    post_artifact_feedback,
)
from agents.inbox import drain_unread, post_message


class TestArtifactWriter:
    def test_write_creates_file_and_index(self, tmp_path: Path):
        w = ArtifactWriter(tmp_path, now=lambda: "T")
        art = w.write(KIND_PLAN, "Implementation Plan", "do x then y")

        f = tmp_path / art.path
        assert f.exists()
        body = f.read_text()
        assert "# Implementation Plan" in body
        assert "do x then y" in body

        index = list_artifacts(tmp_path)
        assert len(index) == 1
        assert index[0]["kind"] == "plan"
        assert index[0]["path"] == "artifacts/plan.md"

    def test_wave_diff_named_per_wave(self, tmp_path: Path):
        w = ArtifactWriter(tmp_path, now=lambda: "T")
        w.add_wave_diff(1, "+app/a.py +app/b.py")
        w.add_wave_diff(2, "+app/c.py")
        names = {e["name"] for e in list_artifacts(tmp_path)}
        assert names == {"wave-1-diff", "wave-2-diff"}

    def test_rewrite_replaces_not_duplicates_index(self, tmp_path: Path):
        w = ArtifactWriter(tmp_path, now=lambda: "T")
        w.write(KIND_PLAN, "Plan v1", "a")
        w.write(KIND_PLAN, "Plan v2", "b")
        index = list_artifacts(tmp_path)
        assert len(index) == 1  # same name → replaced
        assert index[0]["title"] == "Plan v2"
        assert "b" in (tmp_path / "artifacts/plan.md").read_text()

    def test_mirrors_to_source_spec(self, tmp_path: Path):
        worktree = tmp_path / "wt" / "001"
        main = tmp_path / "main" / "001"
        worktree.mkdir(parents=True)
        main.mkdir(parents=True)
        ArtifactWriter(worktree, source_spec_dir=main, now=lambda: "T").write(
            KIND_PLAN, "Plan", "x"
        )
        assert (worktree / "artifacts/plan.md").exists()
        assert (main / "artifacts/plan.md").exists()  # mirrored for the UI
        assert len(list_artifacts(main)) == 1


class TestAsyncFeedback:
    def test_feedback_lands_in_inbox(self, tmp_path: Path):
        post_artifact_feedback(tmp_path, "plan", "use a factory here, not a singleton")
        msgs = drain_unread(tmp_path)
        assert len(msgs) == 1
        assert "[review:plan]" in msgs[0]["text"]
        assert "factory" in msgs[0]["text"]
        assert msgs[0]["source"] == "artifact-review"

    def test_feedback_is_drained_exactly_once(self, tmp_path: Path):
        post_artifact_feedback(tmp_path, "wave-1-diff", "rename this module")
        assert len(drain_unread(tmp_path)) == 1
        assert drain_unread(tmp_path) == []  # already read

    def test_post_message_public_api(self, tmp_path: Path):
        post_message(tmp_path, "hello", sender="cfactory")
        msgs = drain_unread(tmp_path)
        assert msgs[0]["from"] == "cfactory"
        assert msgs[0]["text"] == "hello"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
