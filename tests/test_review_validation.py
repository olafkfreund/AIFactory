#!/usr/bin/env python3
"""
Tests for Spec Hash Validation
===============================

Tests for hash computation and spec change detection:
- File hash computation
- Spec hash computation (spec.md + implementation_plan.json)
- Approval validation based on hash comparison
"""

import json
from pathlib import Path

import pytest
from review import ReviewState
from review.state import _compute_file_hash, _compute_spec_hash


class TestSpecHashValidation:
    """Tests for spec change detection using hash."""

    def test_compute_file_hash_existing_file(self, tmp_path: Path) -> None:
        """_compute_file_hash() returns hash for existing file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, World!")

        file_hash = _compute_file_hash(test_file)

        # Verify it's a valid MD5 hash
        assert len(file_hash) == 32
        assert all(c in "0123456789abcdef" for c in file_hash)

    def test_compute_file_hash_missing_file(self, tmp_path: Path) -> None:
        """_compute_file_hash() returns empty string for missing file."""
        missing_file = tmp_path / "nonexistent.txt"

        file_hash = _compute_file_hash(missing_file)

        assert file_hash == ""

    def test_compute_file_hash_deterministic(self, tmp_path: Path) -> None:
        """_compute_file_hash() returns same hash for same content."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Consistent content")

        hash1 = _compute_file_hash(test_file)
        hash2 = _compute_file_hash(test_file)

        assert hash1 == hash2

    def test_compute_file_hash_different_content(self, tmp_path: Path) -> None:
        """_compute_file_hash() returns different hash for different content."""
        test_file = tmp_path / "test.txt"

        test_file.write_text("Content A")
        hash_a = _compute_file_hash(test_file)

        test_file.write_text("Content B")
        hash_b = _compute_file_hash(test_file)

        assert hash_a != hash_b

    def test_compute_spec_hash(self, review_spec_dir: Path) -> None:
        """_compute_spec_hash() computes combined hash of spec files."""
        spec_hash = _compute_spec_hash(review_spec_dir)

        # Should be a valid MD5 hash
        assert len(spec_hash) == 32
        assert all(c in "0123456789abcdef" for c in spec_hash)

    def test_compute_spec_hash_changes_on_spec_edit(
        self, review_spec_dir: Path
    ) -> None:
        """_compute_spec_hash() changes when spec.md is modified."""
        hash_before = _compute_spec_hash(review_spec_dir)

        # Modify spec.md
        spec_file = review_spec_dir / "spec.md"
        spec_file.write_text("Modified content")

        hash_after = _compute_spec_hash(review_spec_dir)

        assert hash_before != hash_after

    def test_compute_spec_hash_changes_on_plan_edit(
        self, review_spec_dir: Path
    ) -> None:
        """_compute_spec_hash() changes when plan is modified."""
        hash_before = _compute_spec_hash(review_spec_dir)

        # Modify implementation_plan.json
        plan_file = review_spec_dir / "implementation_plan.json"
        plan_file.write_text('{"modified": true}')

        hash_after = _compute_spec_hash(review_spec_dir)

        assert hash_before != hash_after

    def test_is_approval_valid_with_matching_hash(self, review_spec_dir: Path) -> None:
        """is_approval_valid() returns True when hash matches."""
        state = ReviewState()
        state.approve(review_spec_dir, approved_by="user", auto_save=False)

        assert state.is_approval_valid(review_spec_dir) is True

    def test_is_approval_valid_with_changed_spec(self, review_spec_dir: Path) -> None:
        """is_approval_valid() returns False when spec changed."""
        state = ReviewState()
        state.approve(review_spec_dir, approved_by="user", auto_save=False)

        # Modify spec after approval
        spec_file = review_spec_dir / "spec.md"
        spec_file.write_text("New content after approval")

        assert state.is_approval_valid(review_spec_dir) is False

    def test_is_approval_valid_not_approved(self, review_spec_dir: Path) -> None:
        """is_approval_valid() returns False when not approved."""
        state = ReviewState(approved=False)

        assert state.is_approval_valid(review_spec_dir) is False

    def test_is_approval_valid_rejects_hashless_approval(
        self, review_spec_dir: Path
    ) -> None:
        """is_approval_valid() rejects approvals without a spec hash (#1922)."""
        state = ReviewState(
            approved=True,
            approved_by="legacy_user",
            spec_hash="",  # No hash (legacy approval)
        )

        assert state.is_approval_valid(review_spec_dir) is False

    def test_spec_change_detection_accuracy(self, review_spec_dir: Path) -> None:
        """Test that spec change detection works for various types of changes."""
        # Approve initially
        state = ReviewState()
        state.approve(review_spec_dir, approved_by="user", auto_save=False)
        original_hash = state.spec_hash
        assert state.is_approval_valid(review_spec_dir)

        # Test 1: Whitespace-only change should change hash
        spec_file = review_spec_dir / "spec.md"
        original_content = spec_file.read_text()
        spec_file.write_text(original_content + "\n\n\n")
        assert not state.is_approval_valid(review_spec_dir)

        # Restore
        spec_file.write_text(original_content)
        assert state.is_approval_valid(review_spec_dir)

        # Test 2: Plan modification should invalidate
        import json

        plan_file = review_spec_dir / "implementation_plan.json"
        plan_content = plan_file.read_text()
        plan = json.loads(plan_content)
        plan["phases"][0]["chunks"][0]["description"] = "Something else"
        plan_file.write_text(json.dumps(plan, indent=2))
        assert not state.is_approval_valid(review_spec_dir)

        # Test 3: New hash should be different
        state.approve(review_spec_dir, approved_by="user", auto_save=False)
        assert state.spec_hash != original_hash

    def test_approval_invalidation_on_change(self, review_spec_dir: Path) -> None:
        """Test that spec changes invalidate approval."""
        # 1. Approve initially
        state = ReviewState()
        state.approve(review_spec_dir, approved_by="user")
        assert state.is_approval_valid(review_spec_dir)

        # 2. Modify spec.md
        spec_file = review_spec_dir / "spec.md"
        original_content = spec_file.read_text()
        spec_file.write_text(original_content + "\n## New Section\n\nAdded content.")

        # 3. Approval should now be invalid
        assert not state.is_approval_valid(review_spec_dir)

        # 4. Re-approve with new hash
        state.approve(review_spec_dir, approved_by="user")
        assert state.is_approval_valid(review_spec_dir)


def _rewrite_plan(spec_dir: Path, mutate) -> None:
    plan_file = spec_dir / "implementation_plan.json"
    plan = json.loads(plan_file.read_text())
    mutate(plan)
    plan_file.write_text(json.dumps(plan, indent=2))


def _progress_writes(plan: dict) -> None:
    # What approve-plan, the coder's update_subtask_status and QA write (#1924).
    plan["status"] = "in_progress"
    plan["planStatus"] = "in_progress"
    plan.pop("reviewReason", None)
    plan["last_updated"] = "2026-10-27T00:00:00+00:00"
    plan["qa_signoff"] = {"status": "approved"}
    plan["phases"][0]["chunks"][0].update(
        status="completed",
        notes="done",
        updated_at="2026-10-27T00:00:00+00:00",
        started_at="2026-10-27T00:00:00+00:00",
        completed_at="2026-10-27T00:00:00+00:00",
        actual_output="ok",
    )


class TestApprovalIgnoresProgress:
    """An approval binds the reviewed plan content, not its progress (#1924)."""

    def test_progress_writes_keep_approval_valid(self, review_spec_dir: Path) -> None:
        state = ReviewState()
        state.approve(review_spec_dir, approved_by="web_user", auto_save=False)

        _rewrite_plan(review_spec_dir, _progress_writes)

        assert state.is_approval_valid(review_spec_dir) is True

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p["phases"][0]["chunks"][0].update(description="rm -rf /"),
            lambda p: p["phases"][0]["chunks"][0].update(files_to_modify=["x.py"]),
            lambda p: p["phases"][0]["chunks"].append({"id": "n", "description": "x"}),
            lambda p: p.update(feature="Other Feature"),
            lambda p: p.update(required_commands=["curl"]),
            lambda p: p["phases"][0].update(status="whatever"),
        ],
    )
    def test_reviewed_content_changes_invalidate(
        self, review_spec_dir: Path, mutate
    ) -> None:
        state = ReviewState()
        state.approve(review_spec_dir, approved_by="web_user", auto_save=False)

        _rewrite_plan(review_spec_dir, _progress_writes)
        _rewrite_plan(review_spec_dir, mutate)

        assert state.is_approval_valid(review_spec_dir) is False

    def test_non_json_plan_still_hashed(self, review_spec_dir: Path) -> None:
        plan_file = review_spec_dir / "implementation_plan.json"
        plan_file.write_text("not json")
        state = ReviewState()
        state.approve(review_spec_dir, approved_by="web_user", auto_save=False)

        plan_file.write_text("still not json")

        assert state.is_approval_valid(review_spec_dir) is False
