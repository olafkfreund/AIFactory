---
status: draft
issue: 2233
intent: intent/2026-11-25-2233-independent-review.md
---

# Spec: every task gets an independent review

Approved intent decisions:

- the in-build QA review (with its report) is the required review, and the PR
  endgame review stays optional;
- the rule covers all tasks;
- a different model is required, and a different provider is preferred;
- when no distinct reviewer exists, AIFactory overrides the pin for QA only
  and logs it, and refuses only if no second model is configured at all;
- the token figure is filed as a separate issue.

## Design

### D1. One backstop: no QA evidence, no `awaiting_merge` (AIFactory)

**Where:** `apps/web-server/server/services/merger.py` `_decide` /
`_process_spec`, before a PR is opened or adopted.

**The gate.** It reads one evidence file, `qa_evidence.json`, from the spec
dir (see D3) and requires all of the following:

- `status == "approved"`;
- `qa_report.md` exists;
- `reviewer_model` is not in `coder_models`.

**On failure.** The merger raises `_SkipTask("qa_missing")` or
`_SkipTask("qa_not_independent")`, and opens no PR. `_sync_status` writes
`human_review` with that reason, so the task shows in Needs you, not in
awaiting merge.

**Endgame.** The completion orchestration endgame
(`completion_orchestration.py` ~299-385) checks the same gate before
`run_pr_endgame` opens a PR. Both callers share one helper,
`qa_gate.check(spec_dir) -> str | None`.

**Why here.** Every path to a PR goes through these two callers: solo mode,
quick mode / `--skip-qa`, the coder's `qa_signoff`, and every future skip. So
one check covers them all. The source-side fixes (D2) only make the normal
path pass the gate; they are not what enforces it.

### D2. Stop skipping QA in the normal path (AIFactory)

- **Solo mode.** `cli/build_commands.py:311-316` no longer sets
  `skip_qa = True`. Solo keeps its single plan-and-implement session
  (`coder.py:218-256` is unchanged), and only the QA skip goes.
- **The coder's self-approval.** `qa/criteria.py`: `is_qa_approved` and
  `should_run_qa` no longer accept a coder-written `qa_signoff` as approval,
  whatever `requires_review` says. Only a QA session's signoff counts
  (D3 writes the marker). This removes the "QA pre-approved by coder" branch
  (`build_commands.py:665-750`). Its log line becomes "coder signoff ignored;
  running QA".
- **`--skip-qa`.** The flag stays as an operator and debug option, but the
  D1 gate means such a task never reaches `awaiting_merge`. The help text
  says so.

### D3. QA records who reviewed (AIFactory)

When a QA session finishes (`qa/loop.py` ~743-753, where `qa_report.md` is
written), it writes `spec_dir/qa_evidence.json`:

```json
{"status": "approved|rejected", "reviewer_model": "...", "reviewer_provider": "...",
 "coder_models": ["..."], "report": "qa_report.md", "at": "..."}
```

- **`coder_models`** is the set of `model` values in `token_usage.json`
  `workers` whose phase is `coding` or `planning`. Per-subtask coding
  overrides are included, because every coding turn records its model
  through `record_turn`.
- **QA usage.** The QA reviewer and QA fixer sessions call
  `token_attribution.record_turn(..., phase="qa" | "qa_fixer",
  model=..., provider=...)`, just as the coder does at `coder.py:835`. QA cost
  then shows up in the usage figures too.
- **What counts as a QA signoff.** The signoff in `implementation_plan.json`
  counts as QA's only if `qa_evidence.json` also exists with the same status.
  A coder can still write the JSON field, but not the evidence file, because
  only the QA loop writes it after its own session.

### D4. Pick an independent QA model at resolution time (AIFactory)

**Where:** `phase_config.get_phase_model`, after `_resolve_phase_model` and
before `enforce_model_registry`, for `phase in {"qa", "qa_fixer"}`.

**The check.** The coding model is resolved with the same metadata and
compared with the QA model after `resolve_model_id`. If they are equal, QA is
re-picked in this order and the first configured, distinct model wins:

1. `DEFAULT_PHASE_MODELS["qa"]` (opus);
2. `"sonnet"`;
3. a model from another configured provider.

**Logging.** A pick that differs from the input is logged as a warning:
`[QA independence] qa=<a> equals coding=<a>; using <b> (pin overridden for
QA)`. It is also recorded in `task_metadata.json` as `qaModelOverride`.

**When nothing is distinct.** If no distinct model is configured, the call
raises `IndependentReviewUnavailable`. The build fails visibly with that
reason, and the task lands in Needs you.

**Scope.** This one place covers PFactory contract `phase_models`,
`pinnedModel`, `phaseRoutes`, `cli_model`, the single-model fallback and
`AIFACTORY_FALLBACK_MODEL`. `phaseRoutes` resolve to opaque gateway classes,
so for routes the comparison uses the route's resolved model id when the
gateway reports one, and the class name otherwise.

### D5. Read back the models used (AIFactory API)

- **The field.** `token_attribution.render_breakdown` adds
  `phaseModels: {phase: [model, ...]}`, derived from the `workers` rows
  (deduped and sorted), plus `qaEvidence`, the content of `qa_evidence.json`
  or `null`.
- **Where it shows.** It reaches `GET /api/tasks/{id}/token-usage` and
  through it the mcp-stdio proxy. No new route.
- **The `metadata: null` gap.** `routes/task_service.py:809-817` (from-plan
  tasks return `metadata: null` on the task read) is out of scope here. D5 is
  the read the harness needs.

### D6. PFactory stops seeding QA with the coding model

**Where:** `apps/backend/plan/emit/tier_profile.py:62-63`.

**The table.** `phase_models` comes from a new `_QA_MODEL_BY_TIER`:

| Tier | coding | qa | qa_fixer |
|---|---|---|---|
| low | haiku | sonnet | haiku |
| medium | sonnet | opus | sonnet |
| hard | opus | sonnet | opus |

At hard, QA is a different and capable model. AIFactory's D4 still upgrades
it to another provider when one is preferred and configured.

**Cost routing.** `cost_router.py` (~142-176) must not route `qa` onto the
coding model. It skips `qa` when the cheaper candidate equals the coding
model.

**Defense in depth.** D4 already makes AIFactory safe against old contracts.
D6 makes new contracts honest.

### D7. E2E harness follow-up (Factory, separate PR after the AIFactory release)

`scripts/e2e/run.py` S3:

- reads `phaseModels` and `qaEvidence` from the usage read;
- sets `coder = phaseModels.coding` and `reviewer = qaEvidence.reviewer_model`;
- sets `green` from `qaEvidence.status == "approved"`;
- sets `review_recorded` from `qaEvidence.report`.

This replaces the `workers` read that never worked. `.gitignore` drift stays a
reported finding; the plan-files oracle is not relaxed.

## Alternatives rejected

- **Only removing the solo `skip_qa`.** `qa_signoff`, `--skip-qa`, pins and
  routes still get around review (both reviewers found this).
- **Only fixing PFactory's tier profile.** Old contracts, pins and the
  fallback model still collapse QA onto the coding model.
- **Exposing `workers` as-is.** QA never records turns, so the QA model would
  still be missing.
- **Requiring the PR endgame review.** It is off by default, never posts its
  verdict, and is not on the merger path. The owner chose the in-build QA.
- **Requiring a different provider.** Not every deployment has two. It is
  preferred in D4, not required.

## Risks

- **Cost and time:** low and medium tasks gain one QA session, about $0.10-1
  and a few minutes each.
- **Tasks stuck in Needs you:** a QA infrastructure failure now blocks the
  merge. That is intended, and it is visible as `qa_missing`.
- **Old in-flight tasks:** tasks already built without evidence land in
  `qa_missing` at their next merger sweep. A retry runs QA.
- **Gateway routes:** with opaque classes, the comparison may miss two
  classes that resolve to the same model. This is logged, and covered when
  the gateway reports a model id.
- **Hosts:** prod k3s through gitops only. No p510.

## Verification

- **AIFactory unit tests:**
  - the D1 gate, all three failure modes and a pass;
  - D2, solo still runs QA;
  - D2, a coder's `qa_signoff` is ignored;
  - D3, the evidence file is written and QA `record_turn` is called;
  - D4, equal models are re-picked, a pin is overridden for QA, and
    `IndependentReviewUnavailable` is raised when no distinct model exists;
  - D5, `render_breakdown` includes `phaseModels` and `qaEvidence`.
- **PFactory unit tests:** the tier table has `qa != coding` for every tier,
  and the cost router never routes QA onto the coding model.
- **Live:** after both releases, rerun E2E P1:cli:1. S3 passes with
  `reviewer_model != coder_model` read through the API, and a QA report
  exists.
