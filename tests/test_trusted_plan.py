#!/usr/bin/env python3
"""
Tests for Trusted Plan Handoff (issue #390)
===========================================

A signed, checklist-complete plan from an upstream authority (PFactory/CFactory)
must verify and install without the spec pipeline; an unsigned, tampered, or
incomplete plan must be rejected.
"""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from trusted_plan import (
    APPROVAL_KEY,
    CONTRACT_VERSION,
    baseline_drift_rejects,
    check_baseline_drift,
    check_plan_completeness,
    ingest_trusted_plan,
    load_expiry_from_env,
    load_keyring_from_env,
    load_retired_kids_from_env,
    sign_plan,
    verify_plan_signature,
    verify_trusted_plan,
)

KEY = "super-secret-cfactory-key"
KEYRING = {"cfactory": KEY}
TS = "2026-06-06T10:00:00Z"


def _complete_plan() -> dict:
    """A wave-ready plan: 2 disjoint subtasks with file footprints + deps."""
    return {
        "feature": "API gateway endpoints",
        "workflow_type": "feature",
        "phases": [
            {
                "id": "p1",
                "name": "Endpoints",
                "parallel_safe": True,
                "subtasks": [
                    {
                        "id": "st1",
                        "description": "version endpoint",
                        "status": "pending",
                        "files_to_create": ["app/routers/version.py"],
                    },
                    {
                        "id": "st2",
                        "description": "uptime endpoint",
                        "status": "pending",
                        "files_to_create": ["app/routers/uptime.py"],
                        "depends_on": ["st1"],
                    },
                ],
            }
        ],
    }


def _signed(plan: dict, *, key: str = KEY, approved_by: str = "cfactory") -> dict:
    plan = json.loads(json.dumps(plan))  # deep copy
    plan[APPROVAL_KEY] = sign_plan(
        plan, key=key, approved_by=approved_by, approval_timestamp=TS
    )
    return plan


# --------------------------------------------------------------------------
# Keyring
# --------------------------------------------------------------------------


class TestKeyring:
    def test_loads_authority_keys(self):
        env = {
            "AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY": "k1",
            "AIFACTORY_TRUSTED_PLAN_KEY_PFACTORY": "k2",
            "UNRELATED": "x",
        }
        kr = load_keyring_from_env(env)
        assert kr == {"cfactory": "k1", "pfactory": "k2"}

    def test_ignores_empty_values(self):
        assert load_keyring_from_env({"AIFACTORY_TRUSTED_PLAN_KEY_X": ""}) == {}


# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------


class TestSignature:
    def test_valid_signature_verifies(self):
        plan = _signed(_complete_plan())
        ok, reason = verify_plan_signature(plan, keyring=KEYRING)
        assert ok, reason

    def test_unsigned_plan_rejected(self):
        ok, reason = verify_plan_signature(_complete_plan(), keyring=KEYRING)
        assert not ok
        assert "no approval envelope" in reason

    def test_tampered_plan_rejected(self):
        plan = _signed(_complete_plan())
        # Mutate plan content after signing.
        plan["phases"][0]["subtasks"][0]["files_to_create"] = ["evil.py"]
        ok, reason = verify_plan_signature(plan, keyring=KEYRING)
        assert not ok
        assert "tampered" in reason

    def test_tampered_metadata_rejected(self):
        plan = _signed(_complete_plan())
        plan[APPROVAL_KEY]["approved_by"] = "cfactory"  # same key...
        plan[APPROVAL_KEY]["approval_timestamp"] = "1999-01-01T00:00:00Z"  # ...new ts
        ok, _ = verify_plan_signature(plan, keyring=KEYRING)
        assert not ok

    def test_unknown_authority_rejected(self):
        plan = _signed(_complete_plan())
        ok, reason = verify_plan_signature(plan, keyring={"pfactory": "other"})
        assert not ok
        assert "no verification key" in reason

    def test_wrong_key_rejected(self):
        plan = _signed(_complete_plan())
        ok, _ = verify_plan_signature(plan, keyring={"cfactory": "WRONG"})
        assert not ok

    def test_contract_version_mismatch_rejected(self):
        plan = _signed(_complete_plan())
        plan[APPROVAL_KEY]["plan_contract_version"] = "999"
        ok, reason = verify_plan_signature(plan, keyring=KEYRING)
        assert not ok
        assert "contract_version" in reason

    def test_signature_is_stable_for_key_reordering(self):
        # Canonicalization sorts keys, so semantically-identical plans with
        # different key order verify against the same signature.
        plan = _complete_plan()
        env = sign_plan(plan, key=KEY, approved_by="cfactory", approval_timestamp=TS)
        reordered = {
            "workflow_type": plan["workflow_type"],
            "phases": plan["phases"],
            "feature": plan["feature"],
            APPROVAL_KEY: env,
        }
        ok, reason = verify_plan_signature(reordered, keyring=KEYRING)
        assert ok, reason


# --------------------------------------------------------------------------
# Completeness checklist
# --------------------------------------------------------------------------


class TestCompleteness:
    def test_complete_plan_passes(self):
        ok, reasons = check_plan_completeness(_complete_plan())
        assert ok, reasons

    def test_missing_file_footprint_allowed(self):
        # File footprints are OPTIONAL (#517): a plan without them is still
        # trusted-complete (the executor falls back to serial scheduling).
        # PFactory plans before code exists and can't declare footprints.
        plan = _complete_plan()
        del plan["phases"][0]["subtasks"][0]["files_to_create"]
        ok, reasons = check_plan_completeness(plan)
        assert ok, reasons
        assert not any("files_to_create or files_to_modify" in r for r in reasons)

    def test_duplicate_subtask_id_rejected(self):
        plan = _complete_plan()
        plan["phases"][0]["subtasks"][1]["id"] = "st1"
        ok, reasons = check_plan_completeness(plan)
        assert not ok
        assert any("duplicate subtask id" in r for r in reasons)

    def test_unknown_dependency_rejected(self):
        plan = _complete_plan()
        plan["phases"][0]["subtasks"][1]["depends_on"] = ["does-not-exist"]
        ok, reasons = check_plan_completeness(plan)
        assert not ok
        assert any("unknown subtask" in r for r in reasons)

    def test_dependency_cycle_rejected(self):
        plan = _complete_plan()
        plan["phases"][0]["subtasks"][0]["depends_on"] = ["st2"]  # st1<->st2
        ok, reasons = check_plan_completeness(plan)
        assert not ok
        assert any("cycle" in r for r in reasons)

    def test_no_phases_rejected(self):
        ok, reasons = check_plan_completeness(
            {"feature": "x", "workflow_type": "feature", "phases": []}
        )
        assert not ok


# --------------------------------------------------------------------------
# Top-level verification + ingestion
# --------------------------------------------------------------------------


class TestVerifyAndIngest:
    def _handoff_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        git("init", "-q", "-b", "main")
        git("remote", "add", "origin", str(origin))
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "approved",
        )
        approved = git("rev-parse", "HEAD")
        git(
            "push", "origin", "HEAD:refs/heads/dev", "HEAD:refs/heads/factory/2172-slug"
        )
        git("update-ref", "-d", "refs/remotes/origin/factory/2172-slug")
        git("update-ref", "-d", "refs/remotes/origin/dev")
        plan = _complete_plan()
        plan["baseline"] = {"commit": approved}
        plan["provenance"] = {
            "base_ref": "dev",
            "source_branch": "factory/2172-slug",
            "approved_head_sha": approved,
        }
        return repo, approved, plan, git

    def test_adopted_head_fetches_origin_only_branch(self, tmp_path):
        repo, approved, plan, git = self._handoff_repo(tmp_path)
        assert (
            git("for-each-ref", "--format=%(objectname)", "refs/remotes/origin") == ""
        )
        assert check_baseline_drift(plan, repo).status == "current"
        assert git("rev-parse", "refs/remotes/origin/factory/2172-slug") == approved
        assert git("rev-parse", "refs/remotes/origin/dev") == approved
        git("remote", "remove", "origin")
        git("update-ref", "refs/remotes/origin/factory/2172-slug", approved)
        assert check_baseline_drift(plan, repo).status == "adopted_head_mismatch"

    def test_adopted_head_rejects_changed_origin_and_allows_recorded_retry(
        self, tmp_path
    ):
        repo, _, plan, git = self._handoff_repo(tmp_path)
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "build",
        )
        newer = git("rev-parse", "HEAD")
        git("push", "origin", "HEAD:refs/heads/factory/2172-slug")
        git("update-ref", "-d", "refs/remotes/origin/factory/2172-slug")
        drift = check_baseline_drift(plan, repo)
        assert drift.status == "adopted_head_mismatch"
        assert drift.head_commit == newer
        assert baseline_drift_rejects(drift, "warn")
        assert not baseline_drift_rejects(drift, "off")
        spec_dir = tmp_path / "spec"
        (spec_dir / "memory").mkdir(parents=True)
        (spec_dir / "memory" / "build_commits.json").write_text(
            json.dumps({"commits": [newer]})
        )
        assert check_baseline_drift(plan, repo, spec_dir).status == "current"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("source_branch", "../unsafe"),
            ("base_ref", "main.lock"),
            ("approved_head_sha", "1234"),
            ("pr_number", True),
            ("pr_number", 0),
        ],
    )
    def test_invalid_signed_handoff_rejected(self, field, value):
        plan = _complete_plan()
        plan["provenance"] = {field: value}
        verdict = verify_trusted_plan(_signed(plan), keyring=KEYRING)
        assert not verdict.ok
        assert any(field in reason for reason in verdict.reasons)

    def test_signed_handoff_is_persisted(self, tmp_path):
        plan = _complete_plan()
        plan["provenance"] = {
            "project_id": "aifactory",
            "repo": "olafkfreund/AIFactory",
            "base_ref": "dev",
            "source_branch": "factory/2172-slug",
            "approved_head_sha": "a" * 40,
            "pr_number": 42,
            "trusted_plan": False,
        }
        spec_dir = tmp_path / "spec"
        assert ingest_trusted_plan(spec_dir, _signed(plan), keyring=KEYRING).ok
        req = json.loads((spec_dir / "requirements.json").read_text())
        meta = json.loads((spec_dir / "task_metadata.json").read_text())
        assert req["provenance"]["source_branch"] == "factory/2172-slug"
        assert req["provenance"]["trusted_plan"] is True
        assert meta["base_branch"] == "dev"
        assert meta["source_branch"] == "factory/2172-slug"
        assert meta["approved_head_sha"] == "a" * 40
        assert meta["pr_number"] == 42
        assert meta["github_repo"] == "olafkfreund/AIFactory"

    def test_signed_complete_plan_accepted(self):
        result = verify_trusted_plan(_signed(_complete_plan()), keyring=KEYRING)
        assert result.ok, result.reasons
        assert result.approved_by == "cfactory"
        assert result.approval_timestamp == TS
        assert bool(result) is True

    def test_signed_but_incomplete_rejected(self):
        # A genuinely incomplete plan (subtask missing its description) is
        # rejected even when correctly signed. (File footprints are optional —
        # see test_missing_file_footprint_allowed.)
        plan = _complete_plan()
        del plan["phases"][0]["subtasks"][0]["description"]
        result = verify_trusted_plan(_signed(plan), keyring=KEYRING)
        assert not result.ok

    def test_complete_but_unsigned_rejected(self):
        result = verify_trusted_plan(_complete_plan(), keyring=KEYRING)
        assert not result.ok

    def test_ingest_success_writes_plan_and_provenance(self, tmp_path: Path):
        spec_dir = tmp_path / "spec"
        result = ingest_trusted_plan(
            spec_dir, _signed(_complete_plan()), keyring=KEYRING
        )

        assert result.ok, result.reasons
        plan_file = spec_dir / "implementation_plan.json"
        assert plan_file.exists()
        written = json.loads(plan_file.read_text())
        assert written["feature"] == "API gateway endpoints"

        req = json.loads((spec_dir / "requirements.json").read_text())
        assert req["provenance"]["trusted_plan"] is True
        assert req["provenance"]["approved_by"] == "cfactory"

    def test_ingest_writes_spec_md_for_executor(self, tmp_path: Path):
        # #483: the executor's spec resolution (cli.utils.find_spec) and
        # validate_environment both require spec.md to exist. A trusted-plan
        # ingest skips the spec pipeline, so it must synthesize spec.md itself —
        # otherwise run.py can't find the spec and the build never codes.
        spec_dir = tmp_path / "spec"
        plan = _complete_plan()
        plan["final_acceptance"] = ["GET /version returns 200."]
        result = ingest_trusted_plan(spec_dir, _signed(plan), keyring=KEYRING)

        assert result.ok, result.reasons
        spec_md = spec_dir / "spec.md"
        assert spec_md.exists()
        body = spec_md.read_text()
        assert body.startswith("# API gateway endpoints")
        assert "## Acceptance Criteria" in body
        assert "GET /version returns 200." in body

    def test_ingest_failure_writes_nothing(self, tmp_path: Path):
        spec_dir = tmp_path / "spec"
        result = ingest_trusted_plan(
            spec_dir, _complete_plan(), keyring=KEYRING
        )  # unsigned
        assert not result.ok
        assert not (spec_dir / "implementation_plan.json").exists()

    def test_round_trip_sign_then_verify(self):
        # End-to-end: sign with the authority key, verify with the keyring.
        plan = _complete_plan()
        plan[APPROVAL_KEY] = sign_plan(
            plan, key=KEY, approved_by="CFactory", approval_timestamp=TS
        )
        result = verify_trusted_plan(
            plan, keyring=KEYRING
        )  # authority match is case-insensitive
        assert result.ok, result.reasons
        assert plan[APPROVAL_KEY]["plan_contract_version"] == CONTRACT_VERSION


# --------------------------------------------------------------------------
# Key rotation (kid + keyring) — #323, #310
# --------------------------------------------------------------------------


NEW_KEY = "rotating-cfactory-key-2026q3"
KID = "2026Q3"
# Keyed entry: authority/kid, both lowercased (kid is case-insensitive).
KEYED_RING = {"cfactory/2026q3": NEW_KEY}


class TestKeyRotation:
    def _signed_kid(self, *, key: str, kid: str) -> dict:
        plan = _complete_plan()
        plan[APPROVAL_KEY] = sign_plan(
            plan, key=key, approved_by="cfactory", approval_timestamp=TS, kid=kid
        )
        return plan

    def test_keyed_env_var_builds_kid_entry(self):
        env = {"AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY__2026Q3": NEW_KEY}
        assert load_keyring_from_env(env) == KEYED_RING

    def test_sign_with_new_kid_verifies(self):
        # A plan signed with a new rotating key + kid verifies against the
        # keyring entry for that kid.
        plan = self._signed_kid(key=NEW_KEY, kid=KID)
        assert plan[APPROVAL_KEY]["kid"] == KID
        ok, reason = verify_plan_signature(plan, keyring=KEYED_RING)
        assert ok, reason

    def test_new_and_legacy_keys_active_together(self):
        # No downtime during rotation: one keyring verifies BOTH a legacy
        # (no-kid) envelope and a new keyed envelope.
        both = {**KEYRING, **KEYED_RING}
        legacy_plan = _signed(_complete_plan())  # no kid
        new_plan = self._signed_kid(key=NEW_KEY, kid=KID)
        assert verify_plan_signature(legacy_plan, keyring=both)[0]
        assert verify_plan_signature(new_plan, keyring=both)[0]

    def test_retired_kid_rejected(self):
        # A retired kid is rejected even though its key material is present.
        plan = self._signed_kid(key=NEW_KEY, kid=KID)
        ok, reason = verify_plan_signature(
            plan, keyring=KEYED_RING, retired_kids={"cfactory/2026q3"}
        )
        assert not ok
        assert "retired" in reason

    def test_retired_bare_kid_rejected(self):
        plan = self._signed_kid(key=NEW_KEY, kid=KID)
        ok, reason = verify_plan_signature(
            plan, keyring=KEYED_RING, retired_kids={"2026q3"}
        )
        assert not ok
        assert "retired" in reason

    def test_kid_swap_breaks_signature(self):
        # kid is bound into the signed bytes — re-labelling the envelope's kid
        # (to point at a different key) invalidates the signature.
        plan = self._signed_kid(key=NEW_KEY, kid=KID)
        plan[APPROVAL_KEY]["kid"] = "someotherkid"
        ring = {**KEYED_RING, "cfactory/someotherkid": NEW_KEY}
        ok, reason = verify_plan_signature(plan, keyring=ring)
        assert not ok
        assert "tampered" in reason

    def test_unknown_kid_rejected(self):
        plan = self._signed_kid(key=NEW_KEY, kid=KID)
        ok, reason = verify_plan_signature(plan, keyring=KEYRING)  # legacy ring only
        assert not ok
        assert "key-id" in reason

    def test_legacy_single_key_still_verifies(self):
        # The pre-rotation handshake (no kid) is byte-identical and still
        # verifies against the authority-only keyring entry.
        legacy_plan = _signed(_complete_plan())
        assert "kid" not in legacy_plan[APPROVAL_KEY]
        ok, reason = verify_plan_signature(legacy_plan, keyring=KEYRING)
        assert ok, reason

    def test_retired_kids_env_parsing(self):
        env = {"AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS": "cfactory/old, 2025q1 ,"}
        assert load_retired_kids_from_env(env) == {"cfactory/old", "2025q1"}


class TestKeyExpiry:
    NOW = datetime(2026, 12, 1, tzinfo=UTC)

    def _kid_plan(self):
        plan = _complete_plan()
        plan[APPROVAL_KEY] = sign_plan(
            plan, key=NEW_KEY, approved_by="cfactory", approval_timestamp=TS, kid=KID
        )
        return plan

    def test_before_then_after_expiry(self):
        plan = self._kid_plan()
        exp = load_expiry_from_env(
            {"AIFACTORY_TRUSTED_PLAN_EXPIRY": "cfactory/2026q3=2027-01-01"}
        )
        assert verify_plan_signature(
            plan, keyring=KEYED_RING, expiry=exp, now=self.NOW
        )[0]
        late = datetime(2027, 1, 1, tzinfo=UTC)
        ok, reason = verify_plan_signature(
            plan, keyring=KEYED_RING, expiry=exp, now=late
        )
        assert not ok
        assert "expired" in reason

    def test_legacy_key_expires(self):
        exp = {"cfactory": datetime(2026, 1, 1, tzinfo=UTC)}
        ok, reason = verify_plan_signature(
            _signed(_complete_plan()), keyring=KEYRING, expiry=exp, now=self.NOW
        )
        assert not ok
        assert "expired" in reason

    def test_other_key_unaffected_and_unset_means_no_expiry(self):
        exp = {"cfactory/other": datetime(2026, 1, 1, tzinfo=UTC)}
        plan = self._kid_plan()
        assert verify_plan_signature(
            plan, keyring=KEYED_RING, expiry=exp, now=self.NOW
        )[0]
        assert verify_plan_signature(plan, keyring=KEYED_RING, expiry={}, now=self.NOW)[
            0
        ]

    def test_malformed_date_fails_closed(self):
        exp = load_expiry_from_env(
            {"AIFACTORY_TRUSTED_PLAN_EXPIRY": "cfactory/2026q3=soon"}
        )
        assert exp == {"cfactory/2026q3": None}
        ok, reason = verify_plan_signature(
            self._kid_plan(), keyring=KEYED_RING, expiry=exp, now=self.NOW
        )
        assert not ok
        assert "expired" in reason

    def test_expiry_var_is_not_a_signing_key(self):
        env = {"AIFACTORY_TRUSTED_PLAN_EXPIRY": "cfactory=2027-01-01"}
        assert load_keyring_from_env(env) == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
