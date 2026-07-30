#!/usr/bin/env python3
"""
Tests for Trusted Plan Handoff (issue #390)
===========================================

A signed, checklist-complete plan from an upstream authority (PFactory/CFactory)
must verify and install without the spec pipeline; an unsigned, tampered, or
incomplete plan must be rejected.
"""

import json
from pathlib import Path

import pytest
from trusted_plan import (
    APPROVAL_KEY,
    CONTRACT_VERSION,
    check_plan_completeness,
    ingest_trusted_plan,
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
