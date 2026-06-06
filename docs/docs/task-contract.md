# Task Contract v2 — Ingesting a Pre-Computed Plan (Skip Planning)

AIFactory can **skip its entire spec + planning pipeline** when an upstream
system (PFactory) hands it a complete, signed **Task Contract v2**. This is the
fast-path: the analysis was already done and governed upstream, so AIFactory goes
straight to the wave executor.

> Canonical contract: **Factory [RFC-0002](https://github.com/olafkfreund/Factory/blob/main/docs/rfc/0002-task-contract.md)**
> · JSON Schema: [`apis/task-contract.schema.json`](https://github.com/olafkfreund/Factory/blob/main/apis/task-contract.schema.json).
> For the full set of task options when *not* using the fast-path, see
> [Task Lifecycle](./task-lifecycle.md).

---

## 1. What the contract carries

A superset of `implementation_plan.json` with two upstream-computed blocks plus a
signature envelope:

- **WHAT** — the plan (`feature`, `workflow_type`, `phases[].subtasks[]` with
  `depends_on`, `files_to_create/modify`, `verification`), `final_acceptance`,
  `required_commands`.
- **HOW** — `execution`: `model`/`provider`, `phase_models`, `phase_thinking`,
  `parallel`, `workers`, `review_tier`, `agents`/`subagents`, `skills`,
  `skip_planning`.
- **VERIFY** — `tfactory`: `lanes`, `frameworks`, `endpoints`, `coverage_target`,
  `mutation_scope`, `security_scope`, `ac_to_code_map` (carried through to
  TFactory).
- **TRUST** — `approval`: `approved_by`, `approval_timestamp`,
  `plan_contract_version`, `signature` (HMAC-SHA256).

---

## 2. How AIFactory ingests it

**`POST /api/tasks/from-plan`** (`apps/web-server/server/routes/execution.py`)
→ `trusted_plan.ingest_trusted_plan` (`apps/backend/trusted_plan.py`):

1. **Verify signature** — HMAC over the canonical contract + approval metadata,
   keyed by `AIFACTORY_TRUSTED_PLAN_KEY_<AUTHORITY>` (the authority = `approved_by`).
2. **Verify completeness** — `feature`, `workflow_type`, non-empty `phases`,
   unique subtask ids, file footprints, valid `depends_on` (no unknown deps, no
   cycles).
3. **Install** — write `implementation_plan.json`, mark the spec review-approved,
   record provenance, and (when `project_dir` is supplied) seed the plan's
   `required_commands` into the security allowlist.
4. **Apply the execution profile** — `execution.model`/`phase_models`/
   `phase_thinking` → `phase_config`; `parallel`/`workers` → `task_metadata`;
   `review_tier` → the gate. With `skip_planning: true` the build goes straight to
   the wave executor — no planner session.

On failure it returns **HTTP 422** with reasons; the caller can fall back to the
normal `create-and-run` path.

---

## 3. Handover to TFactory

When the build completes, AIFactory carries the `tfactory` block into the spec/
context it hands to TFactory, so TFactory builds its `test_plan.json` from
declared `lanes`/`frameworks`/`endpoints`/scope instead of inferring them. Status
flows back via the RFC-0001 completion-event envelope keyed by `correlation_key`;
TFactory failures return through the existing QA-fixer handback path.

---

## 4. Backward compatibility

The contract is **additive**. A `contract_version: "1"` plan (no `execution`/
`tfactory`) still verifies and installs; a v2 contract without those blocks
behaves like v1 plus richer correlation. The legacy `requirements.json` +
`create-and-run` path is unchanged. v2 with all blocks is the full skip-planning,
no-guessing fast-path.

---

## 5. Status

The contract schema and RFC are merged (Factory RFC-0002). Wiring the full
`execution` + `tfactory` ingest into `trusted_plan` is tracked by the AIFactory
epic "Ingest Task Contract v2 (skip planning)".
