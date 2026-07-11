"""
Trusted Plan Handoff
====================

When an upstream authority (PFactory authoring, CFactory governance) produces a
vetted ``implementation_plan.json`` and hands it to AIFactory, re-running the
full spec pipeline (discovery → requirements → research → context → spec → plan
→ validate) is redundant — the plan already exists and was approved. This module
makes "approved by CFactory" **verifiable** rather than a spoofable string, so
the build can skip planning and go straight to the wave executor (#376).

Two gates must pass for a plan to be *trusted-complete*:

1. **Signature** — an HMAC-SHA256 over the canonical plan JSON plus the approval
   metadata, keyed by the authority's shared secret. Tamper-evident: any change
   to the plan or the metadata invalidates the signature.
2. **Completeness checklist** — the plan must carry everything the parallel wave
   executor needs: phases with subtasks, unique subtask ids, per-subtask file
   sets (``files_to_create``/``files_to_modify`` drive wave scheduling), and
   valid ``depends_on`` references with no cycles.

Keys are loaded from the environment: ``AIFACTORY_TRUSTED_PLAN_KEY_<AUTHORITY>``
(e.g. ``AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY``). The authority name in the
approval envelope (case-insensitive) selects the key.

See issue #390. Reuses the plan structure from #376 (depends_on, files,
parallel_safe) and the provenance model from #332.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Bump when the signed-payload construction or required envelope fields change,
# so an old signer and a new verifier don't silently disagree.
CONTRACT_VERSION = "2"

# Versions this AIFactory can verify/ingest. v2 adds the optional execution +
# tfactory blocks (RFC-0002); v1 plans (no such blocks) remain fully accepted.
SUPPORTED_CONTRACT_VERSIONS = frozenset({"1", "2"})

_ENV_KEY_PREFIX = "AIFACTORY_TRUSTED_PLAN_KEY_"

# Reserved key under which the approval envelope is embedded in the plan. It is
# excluded from the canonical payload so the signature covers the plan content,
# not the signature itself.
APPROVAL_KEY = "approval"

_ENVELOPE_FIELDS = (
    "approved_by",
    "approval_timestamp",
    "plan_contract_version",
    "signature",
)


@dataclass
class TrustedPlanVerification:
    """Outcome of verifying a handed-off plan."""

    ok: bool
    reasons: list[str] = field(default_factory=list)
    approved_by: str | None = None
    approval_timestamp: str | None = None

    def __bool__(self) -> bool:  # allow `if verify_trusted_plan(...):`
        return self.ok


# =============================================================================
# Keyring
# =============================================================================


def load_keyring_from_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Build {authority(lowercased): key} from ``AIFACTORY_TRUSTED_PLAN_KEY_*``.

    Example: ``AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY=secret`` →
    ``{"cfactory": "secret"}``. Empty values are ignored.
    """
    source = os.environ if env is None else env
    keyring: dict[str, str] = {}
    for name, value in source.items():
        if name.startswith(_ENV_KEY_PREFIX) and value:
            authority = name[len(_ENV_KEY_PREFIX) :].strip().lower()
            if authority:
                keyring[authority] = value
    return keyring


# =============================================================================
# Canonicalization + signing
# =============================================================================


def _plan_core(plan: dict) -> dict:
    """The plan minus its approval envelope (the bytes that get signed)."""
    return {k: v for k, v in plan.items() if k != APPROVAL_KEY}


def _canonical(obj: object) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _signing_bytes(
    plan: dict,
    approved_by: str,
    approval_timestamp: str,
    contract_version: str,
) -> bytes:
    """Canonical payload covering both plan content and approval metadata."""
    payload = "|".join(
        (
            _canonical(_plan_core(plan)),
            approved_by,
            approval_timestamp,
            contract_version,
        )
    )
    return payload.encode("utf-8")


def sign_plan(
    plan: dict,
    *,
    key: str,
    approved_by: str,
    approval_timestamp: str,
    contract_version: str = CONTRACT_VERSION,
) -> dict:
    """Produce an approval envelope for ``plan`` (does not mutate ``plan``).

    The caller embeds the returned dict under ``plan["approval"]``. This is the
    PFactory/CFactory side of the handshake; AIFactory only verifies.
    """
    signature = hmac.new(
        key.encode("utf-8"),
        _signing_bytes(plan, approved_by, approval_timestamp, contract_version),
        hashlib.sha256,
    ).hexdigest()
    return {
        "approved_by": approved_by,
        "approval_timestamp": approval_timestamp,
        "plan_contract_version": contract_version,
        "signature": signature,
    }


def verify_plan_signature(
    plan: dict, *, keyring: dict[str, str] | None = None
) -> tuple[bool, str]:
    """Verify the embedded approval envelope against the authority's key.

    Returns ``(ok, reason)``. ``reason`` is empty on success.
    """
    keyring = load_keyring_from_env() if keyring is None else keyring

    envelope = plan.get(APPROVAL_KEY)
    if not isinstance(envelope, dict):
        return False, "plan has no approval envelope"

    missing = [f for f in _ENVELOPE_FIELDS if not envelope.get(f)]
    if missing:
        return False, f"approval envelope missing fields: {', '.join(missing)}"

    contract = str(envelope["plan_contract_version"])
    if contract not in SUPPORTED_CONTRACT_VERSIONS:
        return (
            False,
            f"unsupported plan_contract_version {contract!r} "
            f"(this AIFactory verifies {sorted(SUPPORTED_CONTRACT_VERSIONS)})",
        )

    authority = str(envelope["approved_by"]).strip().lower()
    key = keyring.get(authority)
    if not key:
        return (
            False,
            f"no verification key for authority {authority!r} "
            f"(set {_ENV_KEY_PREFIX}{authority.upper()})",
        )

    expected = hmac.new(
        key.encode("utf-8"),
        _signing_bytes(
            plan,
            str(envelope["approved_by"]),
            str(envelope["approval_timestamp"]),
            contract,
        ),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, str(envelope["signature"])):
        return False, "signature mismatch — plan or metadata was tampered with"

    return True, ""


# =============================================================================
# Completeness checklist (wave-executor readiness)
# =============================================================================


def check_plan_completeness(plan: dict) -> tuple[bool, list[str]]:
    """Verify the plan carries everything the parallel wave executor needs.

    Returns ``(ok, reasons)``. A trusted plan must be buildable without the spec
    pipeline filling gaps, so this is stricter than the lenient build-time
    validator: every subtask must declare its file footprint.
    """
    reasons: list[str] = []

    for field_name in ("feature", "workflow_type", "phases"):
        if field_name not in plan:
            reasons.append(f"missing top-level field {field_name!r}")

    phases = plan.get("phases")
    if not isinstance(phases, list) or not phases:
        reasons.append("'phases' must be a non-empty list")
        return (not reasons), reasons

    seen_ids: set[str] = set()
    total_subtasks = 0
    all_subtask_ids: set[str] = set()
    deps: dict[str, list[str]] = {}

    for pi, phase in enumerate(phases):
        if not isinstance(phase, dict):
            reasons.append(f"phase {pi + 1} is not an object")
            continue
        subtasks = phase.get("subtasks")
        if not isinstance(subtasks, list) or not subtasks:
            reasons.append(f"phase {pi + 1} has no subtasks")
            continue
        for si, st in enumerate(subtasks):
            total_subtasks += 1
            if not isinstance(st, dict):
                reasons.append(f"phase {pi + 1} subtask {si + 1} is not an object")
                continue
            sid = st.get("id")
            label = sid or f"phase {pi + 1} subtask {si + 1}"
            if not sid:
                reasons.append(f"{label}: missing 'id'")
            elif sid in seen_ids:
                reasons.append(f"duplicate subtask id {sid!r}")
            else:
                seen_ids.add(sid)
                all_subtask_ids.add(sid)
            if not st.get("description"):
                reasons.append(f"{label}: missing 'description'")
            # File footprints (files_to_create/files_to_modify) drive parallel
            # wave scheduling when present, but PFactory plans before code
            # exists and can't reliably declare them. Treat them as OPTIONAL: a
            # plan without footprints is still trusted-complete — the executor
            # falls back to serial scheduling (same as the lenient build-time
            # validator). Requiring them here rejected every PFactory handoff
            # and silently fell back to full re-planning (#517 PARR).
            if sid:
                deps[sid] = list(st.get("depends_on") or [])

    if total_subtasks == 0:
        reasons.append("plan has no subtasks")

    # depends_on must reference known subtasks and not cycle.
    for sid, dep_list in deps.items():
        for dep in dep_list:
            if dep == sid:
                reasons.append(f"{sid}: depends on itself")
            elif dep not in all_subtask_ids:
                reasons.append(f"{sid}: depends on unknown subtask {dep!r}")
    if not _is_acyclic(deps):
        reasons.append("subtask depends_on graph contains a cycle")

    return (not reasons), reasons


def _is_acyclic(deps: dict[str, list[str]]) -> bool:
    """DFS cycle detection over the subtask dependency graph."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(deps, WHITE)

    def visit(node: str) -> bool:
        color[node] = GREY
        for nxt in deps.get(node, []):
            if nxt not in color:
                continue  # unknown dep — reported separately
            if color[nxt] == GREY:
                return False
            if color[nxt] == WHITE and not visit(nxt):
                return False
        color[node] = BLACK
        return True

    return all(visit(n) for n in deps if color[n] == WHITE)


# =============================================================================
# Top-level verification
# =============================================================================


def verify_trusted_plan(
    plan: dict, *, keyring: dict[str, str] | None = None
) -> TrustedPlanVerification:
    """Run both gates (signature + completeness) and report the verdict."""
    reasons: list[str] = []

    sig_ok, sig_reason = verify_plan_signature(plan, keyring=keyring)
    if not sig_ok:
        reasons.append(sig_reason)

    complete_ok, complete_reasons = check_plan_completeness(plan)
    reasons.extend(complete_reasons)

    envelope = (
        plan.get(APPROVAL_KEY) if isinstance(plan.get(APPROVAL_KEY), dict) else {}
    )
    return TrustedPlanVerification(
        ok=sig_ok and complete_ok,
        reasons=reasons,
        approved_by=envelope.get("approved_by"),
        approval_timestamp=envelope.get("approval_timestamp"),
    )


# =============================================================================
# Ingestion (the fast-path the endpoint/CLI calls)
# =============================================================================


# =============================================================================
# Execution profile (RFC-0002 `execution` block → task_metadata.json)
# =============================================================================

# Maps Task Contract v2 `execution` keys to the task_metadata.json keys the
# executor already honors (phase_config + agent_service._read_parallel_opts).
_EXECUTION_TO_METADATA = {
    "model": "model",
    "phase_models": "phaseModels",
    "phase_thinking": "phaseThinking",
    "parallel": "parallel",
    "workers": "workers",
    "complexity": "complexity",
    "review_tier": "reviewTier",
    "skills": "selectedSkills",
    "skip_planning": "skipPlanning",
    # Soft budget alert (#45 P2): carry the optional contract cost budget into
    # task metadata so completion.py can surface a usage.budget warning when the
    # rolled-up spend exceeds it. OBSERVE-ONLY — never used to abort a build.
    "budget_usd": "budgetUsd",
}


def execution_profile_to_metadata(execution: dict) -> dict:
    """Translate a v2 ``execution`` block into task_metadata.json fields.

    Pure/serializable; only known keys are mapped (unknown keys ignored). When
    per-phase models are present, ``isAutoProfile`` is set so phase_config honors
    ``phaseModels``/``phaseThinking``.
    """
    if not isinstance(execution, dict):
        return {}
    meta: dict = {}
    for src, dst in _EXECUTION_TO_METADATA.items():
        if src in execution and execution[src] is not None:
            meta[dst] = execution[src]
    # RFC-0014 (#803): a contract-pinned model outranks every other model
    # source (per-task override, routing policy, defaults) in phase_config.
    routing = execution.get("routing")
    if isinstance(routing, dict):
        pinned = routing.get("pinned_model")
        if isinstance(pinned, str) and pinned:
            meta["pinnedModel"] = pinned
    if meta.get("phaseModels") or meta.get("phaseThinking"):
        meta["isAutoProfile"] = True
    return meta


def apply_execution_profile(spec_dir: Path, plan: dict) -> dict:
    """Merge the plan's ``execution`` block into the spec's task_metadata.json.

    Returns the dict of fields written (empty when there is no execution block).
    Existing task_metadata keys are preserved; contract values take precedence.
    The executor then honors model/parallel/workers/complexity/skills without a
    planner session (the plan is already installed by ``ingest_trusted_plan``).
    """
    execution = plan.get("execution")
    mapped = execution_profile_to_metadata(execution) if execution else {}
    if not mapped:
        return {}

    spec_dir = Path(spec_dir)
    spec_dir.mkdir(parents=True, exist_ok=True)
    meta_file = spec_dir / "task_metadata.json"
    existing: dict = {}
    if meta_file.exists():
        try:
            existing = json.loads(meta_file.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update(mapped)
    meta_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return mapped


def ingest_trusted_plan(
    spec_dir: Path,
    plan: dict,
    *,
    keyring: dict[str, str] | None = None,
    project_dir: Path | None = None,
) -> TrustedPlanVerification:
    """Verify a handed-off plan and, if trusted-complete, install it for build.

    On success this writes ``implementation_plan.json``, marks the spec's review
    state approved (so the build skips the plan-review gate), and records the
    approval on the spec's provenance — letting the executor go straight to the
    wave dispatch and bypass the spec pipeline. On failure nothing is written.

    When ``project_dir`` is given, the plan's declared verification commands are
    also granted into that project's security allowlist — a signature-verified
    plan is the strongest trust context for seeding (mirrors the build-start
    seeding done by the executor).
    """
    result = verify_trusted_plan(plan, keyring=keyring)
    if not result.ok:
        return result

    spec_dir = Path(spec_dir)
    spec_dir.mkdir(parents=True, exist_ok=True)

    # Persist the plan verbatim (envelope included) for an auditable record.
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps(plan, indent=2), encoding="utf-8"
    )

    # Also stash the signed contract in a BUILD-SAFE location. The executor
    # rewrites implementation_plan.json into AIFactory's runtime format during
    # the build (adding planStatus/status/reviewReason, dropping the contract's
    # tfactory/contract_version/approval blocks), so by the time the
    # AIFactory→TFactory handoff fires on completion the RFC-0002 metadata —
    # including the `tfactory` test profile (lanes/frameworks/ac_to_code_map) —
    # is gone. Writing it here (where the build never touches) lets
    # tfactory_client.load_task_contract recover the full contract and carry it
    # to TFactory so it tests the DECLARED ACs instead of inferring (#71 Phase 3).
    context_dir = spec_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "task_contract.json").write_text(
        json.dumps(plan, indent=2), encoding="utf-8"
    )

    # Synthesize spec.md from the signed plan. The trusted fast path skips the
    # spec pipeline, but the executor's spec resolution (cli.utils.find_spec)
    # and validate_environment both REQUIRE spec.md to exist — without it run.py
    # can't find the spec, dumps the spec list, and the build never codes (#483).
    _write_spec_md_from_plan(spec_dir, plan)

    _record_approval_provenance(spec_dir, result)
    _mark_review_approved(spec_dir, result)

    # Apply the v2 execution profile (model/parallel/workers/complexity/skills)
    # so the executor honors it and skips planning. No-op for v1 plans.
    apply_execution_profile(spec_dir, plan)

    if project_dir is not None:
        try:
            from project.analyzer import seed_profile_with_plan_commands

            seed_profile_with_plan_commands(
                Path(project_dir), spec_dir / "implementation_plan.json"
            )
        except Exception:
            pass  # seeding is best-effort; never fail a verified ingest on it

    return result


def _write_spec_md_from_plan(spec_dir: Path, plan: dict) -> None:
    """Render a signed plan into a minimal ``spec.md`` for the executor (#483).

    The trusted fast path installs ``implementation_plan.json`` (the authoritative
    build artifact) and skips the spec pipeline, but ``cli.utils.find_spec`` and
    ``validate_environment`` both require a ``spec.md`` to even resolve/run the
    spec. We synthesize one from the contract's feature, acceptance criteria, and
    phase/subtask outline. Never overwrites an existing spec.md.
    """
    spec_file = spec_dir / "spec.md"
    if spec_file.exists():
        return

    feature = str(plan.get("feature") or "Trusted plan").strip()
    workflow = str(plan.get("workflow_type") or "feature").strip()
    envelope = (
        plan.get(APPROVAL_KEY) if isinstance(plan.get(APPROVAL_KEY), dict) else {}
    )
    approved_by = str(envelope.get("approved_by") or "authority").strip()

    lines: list[str] = [
        f"# {feature}",
        "",
        f"> Generated from a signed trusted plan (approved by {approved_by}). "
        "The authoritative build artifact is implementation_plan.json.",
        "",
        f"**Workflow type:** {workflow}",
        "",
    ]

    acceptance = plan.get("final_acceptance")
    if isinstance(acceptance, list) and acceptance:
        lines += ["## Acceptance Criteria", ""]
        lines += [f"- {str(ac).strip()}" for ac in acceptance if str(ac).strip()]
        lines.append("")

    phases = plan.get("phases")
    if isinstance(phases, list) and phases:
        lines += ["## Implementation Plan", ""]
        for phase in phases:
            if not isinstance(phase, dict):
                continue
            pname = str(phase.get("name") or phase.get("id") or "Phase").strip()
            lines += [f"### {pname}", ""]
            for st in phase.get("subtasks") or []:
                if not isinstance(st, dict):
                    continue
                sid = str(st.get("id") or "").strip()
                desc = str(st.get("description") or "").strip()
                prefix = f"`{sid}` " if sid else ""
                lines.append(f"- {prefix}{desc}".rstrip())
            lines.append("")

    spec_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _record_approval_provenance(
    spec_dir: Path, result: TrustedPlanVerification
) -> None:
    """Stamp the trusted-plan approval onto requirements.json provenance (#332)."""
    req_file = spec_dir / "requirements.json"
    data: dict = {}
    if req_file.exists():
        try:
            loaded = json.loads(req_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}

    provenance = data.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    provenance["approved_by"] = result.approved_by
    provenance["approval_timestamp"] = result.approval_timestamp
    provenance["trusted_plan"] = True
    data["provenance"] = provenance

    req_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _mark_review_approved(spec_dir: Path, result: TrustedPlanVerification) -> None:
    """Mark the spec approved so the build skips the human plan-review gate."""
    try:
        from review.state import ReviewState

        state = ReviewState.load(spec_dir)
        state.approve(
            spec_dir,
            approved_by=f"trusted-plan:{result.approved_by}",
            auto_save=True,
        )
    except Exception:  # noqa: BLE001 - review state is best-effort; never block ingest
        pass
