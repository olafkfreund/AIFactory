#!/usr/bin/env python3
"""
Tests for Spec-Based Stack Inference (issue #391)
=================================================

A from-scratch / empty repo has no source files for StackDetector to find, so
without seeding the security allowlist from the spec the coder cannot run
python/pip/pytest and the build stalls. These tests cover:

- Inferring the stack from spec.md / requirements.json / context.json text
- Language tooling defaults (python -> pip + pytest)
- Merging the inferred stack into a detected stack (union, never shrink)
- End-to-end: an empty repo with a FastAPI/Python spec allowlists
  python/pip/pytest.
"""

import json
from pathlib import Path

import pytest
from project import get_or_create_profile, is_command_allowed
from project.models import TechnologyStack
from project.stack_inference import (
    infer_stack_from_spec,
    merge_stack,
)


def _write_spec(spec_dir: Path, *, spec_md: str = "", requirements: dict | None = None,
                context: dict | None = None) -> None:
    if spec_md:
        (spec_dir / "spec.md").write_text(spec_md, encoding="utf-8")
    if requirements is not None:
        (spec_dir / "requirements.json").write_text(json.dumps(requirements))
    if context is not None:
        (spec_dir / "context.json").write_text(json.dumps(context))


class TestInferStackFromSpec:
    def test_no_spec_dir_returns_empty(self):
        stack = infer_stack_from_spec(None)
        assert stack.languages == []
        assert stack.frameworks == []

    def test_missing_spec_dir_returns_empty(self, temp_dir: Path):
        stack = infer_stack_from_spec(temp_dir / "does-not-exist")
        assert stack.languages == []

    def test_empty_spec_returns_empty(self, spec_dir: Path):
        stack = infer_stack_from_spec(spec_dir)
        assert stack.languages == []

    def test_infers_python_fastapi_from_spec_md(self, spec_dir: Path):
        _write_spec(
            spec_dir,
            spec_md="Build a FastAPI gateway in Python 3.12 using uv and pytest.",
        )
        stack = infer_stack_from_spec(spec_dir)

        assert "python" in stack.languages
        assert "fastapi" in stack.frameworks
        assert "pytest" in stack.frameworks
        assert "uv" in stack.package_managers

    def test_python_defaults_seed_pip_and_pytest(self, spec_dir: Path):
        # Mentions Python but not pip/pytest explicitly.
        _write_spec(spec_dir, spec_md="A small Python script using Django.")
        stack = infer_stack_from_spec(spec_dir)

        assert "python" in stack.languages
        assert "pip" in stack.package_managers  # seeded default
        assert "pytest" in stack.frameworks  # seeded default

    def test_infers_from_requirements_json(self, spec_dir: Path):
        _write_spec(
            spec_dir,
            requirements={
                "title": "New service",
                "description": "Implement an Express server in Node.js with Jest.",
            },
        )
        stack = infer_stack_from_spec(spec_dir)

        assert "javascript" in stack.languages
        assert "express" in stack.frameworks
        assert "jest" in stack.frameworks
        assert "npm" in stack.package_managers

    def test_infers_rust(self, spec_dir: Path):
        _write_spec(spec_dir, spec_md="Write a CLI in Rust using cargo and axum.")
        stack = infer_stack_from_spec(spec_dir)

        assert "rust" in stack.languages
        assert "cargo" in stack.package_managers
        assert "axum" in stack.frameworks

    def test_does_not_match_substrings(self, spec_dir: Path):
        # "gopher" should not trigger Go; "javascripted" should not trigger JS.
        _write_spec(spec_dir, spec_md="The gopher javascripted nothing here.")
        stack = infer_stack_from_spec(spec_dir)

        assert "go" not in stack.languages
        assert "javascript" not in stack.languages


class TestMergeStack:
    def test_union_adds_without_removing(self):
        base = TechnologyStack(languages=["python"], frameworks=["pytest"])
        extra = TechnologyStack(languages=["go"], frameworks=["gin"])

        merge_stack(base, extra)

        assert set(base.languages) == {"python", "go"}
        assert set(base.frameworks) == {"pytest", "gin"}

    def test_merge_is_idempotent(self):
        base = TechnologyStack(languages=["python"])
        merge_stack(base, TechnologyStack(languages=["python"]))
        assert base.languages == ["python"]


class TestEmptyRepoBuildAllowlist:
    """Issue #391 acceptance: from-scratch Python/FastAPI build can run
    python/pip/pytest."""

    def test_empty_repo_fastapi_spec_allows_python_tooling(
        self, temp_dir: Path, spec_dir: Path
    ):
        # Empty repo: only a README, no source files (simulates from-scratch).
        (temp_dir / "README.md").write_text("# New project\n")
        _write_spec(
            spec_dir,
            spec_md=(
                "Build a FastAPI API gateway in Python 3.12 with uv. "
                "Add pytest tests and keep ruff + mypy clean."
            ),
        )

        profile = get_or_create_profile(temp_dir, spec_dir, force_reanalyze=True)

        # The repo is empty, so detection alone would find nothing.
        for cmd in ("python", "python3", "pip", "pytest", "uv", "uvicorn"):
            allowed, reason = is_command_allowed(cmd, profile)
            assert allowed, f"{cmd} should be allowed: {reason}"

    def test_empty_repo_no_spec_does_not_crash(self, temp_dir: Path):
        (temp_dir / "README.md").write_text("# New project\n")
        # No spec_dir at all — should still produce a valid (base) profile.
        profile = get_or_create_profile(temp_dir, force_reanalyze=True)
        assert profile.get_all_allowed_commands()  # base commands present

    def test_existing_python_repo_still_works(self, temp_dir: Path, spec_dir: Path):
        # Existing repo with real Python files: seeding must not regress it.
        (temp_dir / "requirements.txt").write_text("fastapi\n")
        (temp_dir / "main.py").write_text("print('hi')\n")
        _write_spec(spec_dir, spec_md="Add an endpoint to the FastAPI app.")

        profile = get_or_create_profile(temp_dir, spec_dir, force_reanalyze=True)

        assert "python" in profile.detected_stack.languages
        allowed, _ = is_command_allowed("pytest", profile)
        assert allowed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
