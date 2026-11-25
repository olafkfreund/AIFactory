---
status: draft
issue: 2233
spec: spec/2026-11-25-2233-independent-review.md
coders:
  1: codex   # identity capture across coder/parallel/fixer: small, cross-cutting, must be exact
  2: codex   # control-plane evidence + fresh-verdict rule: security-sensitive logic
  3: codex   # reviewer pick, failover filter, gateway rules: tricky selection logic
  4: coder   # D2 removal of the QA skips: small, well-bounded edits
  5: agy     # D1 gate wired into merger, endgame and manual PR route, plus entry-point tests: broad mechanical wiring
  6: coder   # D5 read-back fields: small API change
  7: codex   # PFactory tier table + cost-router exclusion: small routing logic
  8: coder   # Factory E2E harness S3 read-back: small, after the releases
reviewers:
  - codex    # reviews the agy and coder steps
  - agy      # reviews the codex steps
---

# Plan: every task gets an independent review (#2233)

## Approved decisions

Everything below is carried over from the spec, so the plan can be followed
without it.

### General

- **The required review** is the in-build QA review. The PR endgame review
  stays optional.
- **Scope:** every task.
- **Independence:** the reviewer must be a different model from every coding
  and fixer identity, compared by canonical model id. A different provider is
  preferred.
- **No distinct reviewer:** QA overrides a pin for QA only and logs it.
  `IndependentReviewUnavailable` is raised (the task goes to Needs you) only
  when no distinct, registry-eligible, configured model exists.

### The gate (D1)

- **Helper:** `services/qa_gate.check(source_spec_dir, head_sha)` returns
  `None`, `qa_missing`, `qa_stale` or `qa_not_independent`.
- **It is called at three points:**
  - in `merger._decide`, only for the `opened` and `already_open` outcomes,
    never for merged or closed;
  - before `run_pr_endgame`, which covers auto-PR and adopted PRs. The
    fire-once marker is not set when the gate fails;
  - in `routes/pr.create_pr_from_task`, which returns 409 on failure.
- **Status:** `_sync_status` maps the three reasons to
  `("human_review", reason)`.

### Evidence (D3)

- **Who writes it:** only the QA loop's Python code, into the **source**
  spec dir, which the coder can only read. It is deleted at run start and
  before each QA cycle, and never synced back from the worktree.
- **`qa_evidence.json`** records:
  - `status`, `reviewer` (model, provider, served model);
  - `reviewed_head`, `report_sha256`, `qa_cycle`, `override`, `at`.

  A copy of `qa_report.md` sits next to it.
- **Fresh verdicts only:** `qa_signoff` is cleared before each cycle, and
  only a verdict written during the cycle counts.
- **`model_identities.json`** is append-only. Each entry is
  `{phase, subtask_id, model, provider, served_model}`, written when the
  session is built by the coder, the parallel coder and the QA fixer.
- **Packed builds:** `workspace_fetch` carries both files and the report
  copy.
- **QA usage:** `record_turn` is called with worker id `qa-<cycle>` or
  `qa-fixer-<cycle>`.

### Reviewer selection (D4)

- **Where:** `qa/independence.pick_reviewer` is called where the QA session
  is built and on failover, not in `get_phase_model`.
- **Candidates, in order:**
  1. the resolved QA model;
  2. a frontier model from another provider;
  3. opus;
  4. fable;
  5. sonnet.
- **Filter:** a candidate must be eligible in the registry (checked one by
  one), configured, and not a coding identity.
- **Gateway routes:** two equal route classes means re-pick. Two routes with
  an unknown served model means move to an explicit model; if none exists,
  raise `IndependentReviewUnavailable`.
- **Betas and thinking config** come from the picked model.

### The other decisions

- **D2:**
  - solo mode no longer sets `skip_qa`;
  - a coder's `qa_signoff` never skips QA, and the pre-approved branch is
    removed;
  - `--skip-qa` stays, but such a task can never pass the gate.
- **D5:** `read_breakdown` adds `phaseModels`, from `model_identities.json`,
  and `qaEvidence`. Both come from the canonical source spec dir.
- **D6 (PFactory):**
  - the seeds are: low haiku/sonnet/haiku, medium sonnet/opus/sonnet, hard
    opus/fable/opus (coding/qa/qa_fixer);
  - the cost router drops the canonical coding id, with its aliases, from
    QA's candidates;
  - QA is not pinned;
  - with no distinct model at the floor, QA goes one class up, otherwise it
    is left unset.
- **D7 (Factory):** the S3 read-back uses `phaseModels` and `qaEvidence`,
  with `reviewer ∉ identities` and `reviewed_head` equal to the PR head.

### Branches and order

| Repo | Branch | Steps | Released as |
|---|---|---|---|
| AIFactory | `fix/2233-independent-review` | 1-6 | AIFactory release |
| PFactory | `fix/2233-qa-seed` | 7 | PFactory 0.9.7 |
| Factory | — | 8 | after both releases |

## Repo traps (every step)

- **Tests:** run them with `apps/backend/.venv/bin/pytest`, plus
  `apps/web-server` tests where the step touches the web server. Run ruff
  check and `ruff format --check` on the changed files.
- **`get_phase_model` and `os.devnull`:** `RunGates.model()` calls
  `get_phase_model` with `Path(os.devnull)`. Never write files from inside it.
- **`record_turn`:** it defaults `worker_id` to `"main"`, last write wins.
  Always pass a distinct worker id.
- **Worktree sync:** `agent_worktree_sync.py` has an allowlist. Do not add
  the evidence files to it.
- **Implementers** (Codex, agy, coder) never stage, commit, push, deploy or
  restart. The lead commits after each step.

## Steps

1. **Identity capture (D3 identities).**
   - **Add** `apps/backend/agents/model_identity.py` with
     `record_identity(source_spec_dir, *, phase, subtask_id, model, provider,
     served_model=None)` (append-only JSON, with a lock like
     token_attribution's) and `read_identities(source_spec_dir)`.
   - **Call it** where each session is built:
     - `agents/coder.py` (~459-473, the serial session's model, including a
       per-subtask override);
     - `agents/parallel_integration.py` (~493, per worker);
     - `qa/fixer.py` (the fixer session).
   - **Source dir:** pass the source spec dir, not the worktree one. Find how
     `build_commands.py:475` and `coder.py:480` separate the two.
   - **Tests:** identities are written with no usage payload; a subtask
     override is recorded; a worktree build writes to the source dir.
   - **Verify** with `apps/backend/.venv/bin/pytest
     apps/backend/tests -k "model_identity" -q`.

2. **Control-plane QA evidence and fresh verdicts (D3 evidence).**
   - **`qa/loop.py`:**
     - delete `source_spec_dir/qa_evidence.json` at the start of the loop and
       before each cycle;
     - clear `qa_signoff` before the reviewer session;
     - after the session, write `qa_evidence.json` on both the approved
       branch (~438-480) and the rejected branch (~482-555), with
       `reviewed_head` set to `git rev-parse HEAD` in the build worktree;
     - copy `qa_report.md` and compute `report_sha256`;
     - if no fresh signoff exists, count it as an error, not an approval.
   - **`qa/reviewer.py` and `qa/fixer.py`:** read `ResultMessage` usage and
     duration, and call `record_turn` with worker id `qa-<cycle>` or
     `qa-fixer-<cycle>`.
   - **`core/workspace_fetch.py`:** push and fetch `qa_evidence.json`,
     `model_identities.json` and the report copy next to `token_usage.json`.
   - **Tests:**
     - a coder-written worktree `qa_evidence.json` is ignored;
     - the file is deleted before each cycle;
     - a stale verdict (changed HEAD, or no fresh signoff) does not count;
     - QA `record_turn` leaves `workers["main"]` unchanged;
     - the workspace_fetch round trip works.
   - **Verify** with `pytest apps/backend/tests -k "qa_loop or qa_evidence or
     workspace_fetch" -q`.

3. **Reviewer selection (D4).**
   - **Add** `apps/backend/qa/independence.py` with
     `pick_reviewer(gates, identities, *, exclude_providers=()) -> Pick`
     (model, provider, override reason), or raise
     `IndependentReviewUnavailable`.
   - **Use it** in `qa/loop.py` where the QA session is built (~352) and in
     the provider failover (~651-676), replacing the bare `next_provider`
     result.
   - **Registry:** check eligibility per candidate with `model_registry`
     (~176), in non-raising form.
   - **Betas and thinking:** derive them from the picked model.
   - **Record** the override in `qa_evidence.override`.
   - **Tests:**
     - an equal model is re-picked;
     - a pin is overridden for QA;
     - a `deny`-mode registry rejection moves to the next candidate;
     - provider preference applies;
     - same-class routes are re-picked;
     - two routes with an unknown served model fall back to an explicit
       model;
     - failover is filtered;
     - the unavailable case raises.
   - **Verify** with `pytest apps/backend/tests -k "independence or qa_loop"
     -q`.

4. **Remove the QA skips (D2).**
   - **`cli/build_commands.py:311-316`:** delete the solo `skip_qa = True`.
     Keep the comment explaining that solo keeps its single session.
   - **`build_commands.py:665-750`:** remove the "pre-approved by coder"
     branch.
   - **`qa/criteria.py:53-61,95-109`:** `is_qa_approved` and `should_run_qa`
     no longer treat a coder `qa_signoff` as a reason to skip.
   - **`cli/main.py:204` help text:** say that a `--skip-qa` task cannot
     reach `awaiting_merge`.
   - **Tests:** solo runs QA; a coder signoff is ignored at both
     `requires_review` values.
   - **Verify** with `pytest apps/backend/tests -k "solo or criteria or
     build_commands" -q`.

5. **The gate at every PR entry point (D1).**
   - **Add** `apps/web-server/server/services/qa_gate.py` with
     `check(source_spec_dir, head_sha)`, reading `qa_evidence.json`,
     `model_identities.json` and the report copy.
   - **Wire it in:**
     - `services/merger.py` `_decide` (~343-352): after `_find_pr`, for the
       `opened` (before push and create) and `already_open` outcomes, raise
       `_SkipTask(reason)`; leave `MERGED` and `CLOSED` alone;
     - `_sync_status` (~427-451): map the three reasons to
       `("human_review", reason)`;
     - `services/completion_orchestration.py`: before `run_pr_endgame`
       (~549-578), skip the endgame and do not set the fire-once marker
       (~207) when the gate fails;
     - `routes/pr.py` `create_pr_from_task` (~97-534): before returning an
       existing PR (~224) or creating one (~432, ~464-507), return 409 with
       the reason.
   - **Head sha:** use the branch head the PR would carry.
   - **Tests:** every entry point, refused and passing, `MERGED`/`CLOSED` not
     blocked, the adopted marker not set, the provider-API route, and the
     `_sync_status` mapping.
   - **Verify** with `cd apps/web-server && ../backend/.venv/bin/pytest
     tests -k "qa_gate or merger or completion or create_pr" -q`.

6. **Read-back (D5).**
   - **`agents/token_attribution.py`:** `read_breakdown(spec_dir)` loads
     `phaseModels` from `model_identities.json` and `qaEvidence`, and passes
     them to `render_breakdown`, which emits them.
   - **Other callers:** `record_turn`'s internal `render_breakdown` calls
     pass nothing.
   - **`routes/tasks_usage.py` (~48-56):** read both from the canonical
     source spec dir even when `token_usage.json` falls back to the worktree.
   - **Tests:** both fields appear, and are read from the canonical dir.
   - **Verify** with `pytest -k "token_attribution or tasks_usage" -q`.
   - **Then:** the full backend suite
     (`apps/backend/.venv/bin/pytest tests -m "not slow" -q`) and the
     web-server suite.

7. **PFactory QA seed and cost router (D6).**
   - **Where:** a PFactory worktree from `origin/dev`, branch
     `fix/2233-qa-seed`.
   - **`apps/backend/plan/emit/tier_profile.py` (~41-74):**
     - add `_QA_MODEL_BY_TIER = {"low": "sonnet", "medium": "opus", "hard":
       "fable"}`;
     - set `phase_models = {coding: m, qa: _QA_MODEL_BY_TIER[tier],
       qa_fixer: m}`.
   - **`cost_router.py` (~92-176, 253-271):**
     - for `qa`, remove the canonical coding id and its aliases from the
       candidates (use `cost_router_core` / `model-catalog.json` aliases);
     - a pin applies to coding only;
     - if nothing distinct exists at the floor, try one class up, else leave
       `qa` unset;
     - keep `phase_routes.qa` and the cost estimate filled.
   - **Tests:** the emitted contract after tier plus routing has `qa !=
     coding` by canonical id for every tier, with alias forms, a pin, and no
     distinct model at the floor.
   - **Verify** with PFactory `apps/backend/.venv/bin/pytest tests -k
     "tier_profile or cost_router" -q`, plus ruff.

8. **E2E harness (D7)**, after both releases are live.
   - **Where:** Factory `scripts/e2e/run.py` S3 (~169-203).
   - **Change:**
     - read `phaseModels` and `qaEvidence` from `services.usage`;
     - `runtime_models` requires every coding identity to be known;
     - independence means the reviewer is not among the identities;
     - `green` and `review_recorded` come from `qaEvidence.status ==
       "approved"`, with `reviewed_head` equal to the PR head sha;
     - remove the `workers` read.
   - **Tests:** a unit test in `tests/e2e/unit` with a real-shaped payload.
   - **Verify** with `uv run -q --with pytest python -m pytest tests/e2e/unit
     -q`.

## After the steps (lead)

1. **Review:** an Opus review of each PR diff against this plan, plus the
   `reviewers:` above.
2. **AIFactory:**
   - open the PR to `dev` with the 3 artifact links;
   - merge it with `--match-head-commit` after the checks pass;
   - promote with the release PR and the sync PR;
   - verify the version, the pod code and Argo.
3. **PFactory:** the same for step 7, released as 0.9.7.
4. **Factory:** the step 8 PR.
5. **Live:** rerun E2E P1:cli:1. S3 must pass with:
   - the reviewer outside the coding identities, read through the API;
   - `reviewed_head` equal to the PR head;
   - a QA report present.
6. **Follow-up issues:**
   - Copilot dispatch QA;
   - evidence signed outside the pod for direct and packed builds.

## Tests

Each step's verify command must pass, and so must the full AIFactory backend
and web-server suites and the PFactory suite.

## Rollback

- **AIFactory:** revert the PR on `dev` and cut a patch release. Gitops rolls
  back to the previous image tag.
- **Stuck tasks:** tasks held in `qa_missing` return to normal once the gate
  is gone; nothing else is migrated.
- **PFactory:** revert step 7 and release.
- **The E2E harness change** is independent.
