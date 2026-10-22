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
- **ENVIRONMENT** — `environment` (RFC-0005): the per-task toolchain manifest
  (`language`, `toolchain`, `system_packages`, `build_commands`,
  `verify_commands`, `serve_command`, `provisioning.method` = `nix`, `network`,
  `proof.verify`). AIFactory builds in this env so build and verify cannot drift —
  see [Nix Reproducible Build](./nix-reproducible-build.md).
- **ACCESS** — `access` (RFC-0007): discovered auth requirements for any external
  resource the tests must reach (broker refs only, never secrets).

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

## 5. Supported isolated host

A trusted-plan build counts as isolated only on the **kubejob** build backend
with the durable job store (`DATABASE_URL` set). The agent then runs in its own
Job pod and cannot reach the per-call GitHub token in `hosts.yml`. This is the
decided policy (#1760, #1724), checked by `host_isolated()` in
`apps/web-server/server/services/trusted_contract.py`.

On every other host (subprocess, the agent sandbox, the PID-namespaced sandbox)
the agent runs under the same uid as the process holding the token and could
read it through `/proc/<child>`. There the signed contract is not trusted:
TFactory gets an empty test profile and the merge gate and path floor hold the
task. Use kubejob for trusted plans.

Running agents under a separate uid would remove this limit, but it changes the
chart and runtime privileges and needs its own spec.

---

## 6. Status

The contract schema and RFC are merged (Factory RFC-0002). Wiring the full
`execution` + `tfactory` ingest into `trusted_plan` is tracked by the AIFactory
epic "Ingest Task Contract v2 (skip planning)".
