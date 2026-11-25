---
status: approved
issue: 2233
spec: spec/2026-11-25-2233-independent-review.md
coders:
  1: codex   # identity capture across coder/parallel/fixer: small, cross-cutting, must be exact
  2: codex   # control-plane evidence + fresh-verdict rule: security-sensitive logic
  3: codex   # reviewer pick, failover filter, gateway rules: tricky selection logic
  4: coder   # D2 removal of the QA skips: small, well-bounded edits
  5: agy     # D1 gate across merger, endgame and manual PR route: broad behavioural wiring; codex reviews head selection and retry
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

### Decisions added in revision 2 (from the plan reviews)

These details sit within the approved spec.

- **The gate's pass criteria.** `check` returns `None` only when every one of
  these holds:
  - `qa_evidence.status == "approved"`;
  - `reviewed_head == head_sha`;
  - the sha256 of the protected report copy equals `report_sha256`;
  - every identity has a known model;
  - the reviewer's canonical id equals no identity's canonical id
    (`resolve_model_id` on both sides, so aliases compare equal).

  The other outcomes:

  | Situation | Result |
  |---|---|
  | Evidence missing, malformed or unreadable | `qa_missing` |
  | Status not approved | `qa_missing` |
  | Head or report hash mismatch | `qa_stale` |
  | Unknown identity, or the reviewer equals an identity | `qa_not_independent` |
- **The protected report copy** is `source_spec_dir/qa_evidence_report.md`.
  `qa_report.md` is already in the worktree sync allowlist
  (`agent_worktree_sync.py:91`), so the copy needs its own name. That name
  stays off the allowlist.
- **The head sha differs per PR path:**
  - an existing or adopted PR uses its actual head sha, from `gh pr view
    --json headRefOid` or the provider API;
  - a new PR uses the sha of the task ref the path will push.
- **The manual route** checks before rebasing and pushing. If the route's
  rebase would change the head, it returns `qa_stale` (409) and asks for a
  fresh QA run. The gate does not block an already-merged PR.
- **The solo session** that writes the code is recorded as a **coding**
  identity, even though it is labelled `planning`. Both fixer clients built in
  `qa/loop.py` are recorded: the human-feedback one and the after-rejection
  one.
- **The QA identity is the verdict writer's.** The reviewer's tool fallback
  (`qa/reviewer.py` ~383-574), which can write `qa_signoff`, goes through the
  same independence filter. The evidence records the identity of the session
  that actually wrote the verdict.
- **A denied first candidate.** When the model registry is in `deny` mode,
  the resolved QA candidate is checked in non-raising form, so an initial
  candidate that is denied moves on to the next one. The build preflight QA
  resolve (`build_commands.py` ~322) must not raise for a QA model that
  `pick_reviewer` would replace.
- **Provider preference.** A distinct candidate on another configured
  provider beats a distinct resolved QA model on the coder's provider. Any
  change is logged with the prefix `[QA independence]` and recorded in
  `qa_evidence.override`.
- **`IndependentReviewUnavailable`** is caught in `qa/loop.py`. It writes
  `qa_evidence.json` with `status: "unavailable"` and the reason, and
  returns not-approved. The task then lands in Needs you as `qa_missing`, and
  the log carries the reason. It never reaches the generic fatal handler at
  `build_commands.py:776`.
- **Clearing evidence.**
  - Evidence is deleted at **build start**, before the QA branch, so a
    `--skip-qa` retry at the same sha cannot reuse an old approval.
  - It is deleted again before each QA cycle.
  - It is written before `COMPLETE` is emitted.
- **The `os.devnull` source dir.** On the `--qa` CLI path, `source_spec_dir`
  can be `Path(os.devnull)` (`cli/qa_commands.py:171-180`). The writers fall
  back to `spec_dir` in that case and never write under `os.devnull`.
- **Packed and kubejob builds.**
  - The CLI pushes the evidence, the identities and the protected report copy
    after QA (next to the other pushes in `cli/main.py` ~537-565).
  - The control plane fetches them in `services/completion.py` (~1185-1208)
    before completion orchestration or the merger reads them.
  - The evidence object is keyed per run (task id plus run id), and the fetch
    replaces local evidence with the current run's object. If the run
    uploaded no evidence, the local evidence is deleted.
  - This does not use `maybe_fetch_usage`'s token-total comparison.
- **The completion markers.**
  - `.terminal_side_effects_done` keeps guarding the TFactory hand-off, as
    today.
  - The PR endgame gets its own marker, `.pr_endgame_done`, written only
    after the endgame ran. When the gate fails, the endgame is skipped
    without writing it, so the next completion after a passing QA runs it.
  - The merger still runs and records the gate reason.
- **The identity file lock.** `record_identity` holds an `fcntl.flock` (plus
  a thread lock) around the whole read, append and replace. Parallel workers
  must not lose entries.
- **Shared helpers in the web server.** `qa_gate.py` reuses
  `resolve_model_id` and `read_identities` through the `sys.path` shim
  pattern in `routes/tasks_usage.py:60-67`. It never duplicates alias logic.
- **Human-set status.** The merger keeps the existing guard
  (`merger.py:445`): a status a human set is never overwritten.
- **D5 shapes.**
  - `phaseModels` is `{phase: [model, …]}`, deduped and sorted. The solo
    session's model is listed under `coding`.
  - `qaEvidence` is the evidence object or `null`.
- **The PFactory router.**
  - When the search for QA is exhausted, `apply_cost_routing` explicitly
    removes `qa` from the merged `phase_models` and `phase_routes`.
  - `qa_fixer` is set to the final coding model after routing or a pin.
  - Canonical ids come from the catalog's documented shorthand mapping
    (`model-catalog.json:2`), in `cost_router.py`.
    `cost_router_core.py` is vendored and stays byte-identical.
  - The `_CLAUDE_ROLES` restriction on `qa` (`cost_router.py:61`) stays.
- **Deployment.** Prod k3s through gitops only. No p510.

### Branches and order

| Repo | Branch | Steps | Released as |
|---|---|---|---|
| AIFactory | `fix/2233-independent-review` | 1-6, released together | next AIFactory release |
| PFactory | `fix/2233-qa-seed` | 7, independent of 1-6 | PFactory 0.9.7 |
| Factory | `fix/2233-e2e-s3`, from `origin/main` | 8 | after both releases |

**Dependencies.**

- Steps 1-2 define the files that steps 3, 5 and 6 read.
- Step 3 completes the reviewer identity that the gate trusts.
- Step 4 turns QA on in the normal paths before the gate ships.

## Repo traps (every step)

- **AIFactory tests** run from the worktree root against the root `tests/`
  folder, using the main checkout's venv:

  ```bash
  /mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest tests/<file> -q
  ```

  Web-server tests run from `apps/web-server`:

  ```bash
  /mnt/code/Source-home/GitHub/AIFactory/apps/web-server/.venv/bin/pytest tests/<file> -q
  ```

  Name the test files; never rely on `-k` alone. Run `ruff check` and
  `ruff format --check` on the changed files.
- **`get_phase_model` and `os.devnull`:** `RunGates.model()` calls
  `get_phase_model` with `Path(os.devnull)`. Never write files from inside
  it.
- **`record_turn`:** it defaults `worker_id` to `"main"`, last write wins.
  Always pass a distinct worker id.
- **Worktree sync:** `agent_worktree_sync.py` already syncs `qa_report.md`.
  Never add the evidence files or the protected copy to it.
- **Source vs worktree spec dir:** use the split at `build_commands.py:475`
  and the `source_spec_dir or spec_dir` pattern at `coder.py:865` /
  `parallel_integration.py:451,722`. `coder.py:480` is the worktree client.
- **Implementers** (Codex, agy, coder) never stage, commit, push, deploy or
  restart. The lead commits after each step.

## Steps

1. **Identity capture (D3 identities).**
   - **Add** `apps/backend/agents/model_identity.py` with
     `record_identity(source_spec_dir, *, phase, subtask_id, model, provider,
     served_model=None)` (flock-guarded append, atomic replace),
     `read_identities(source_spec_dir)` and `clear_identities`.
   - **Call it where each session is built:**
     - `agents/coder.py`: the first session (~462-488) is recorded as
       `coding` even when it is labelled planning in solo; the serial coding
       session, including a per-subtask override;
     - `agents/parallel_integration.py` (~493), per worker;
     - `qa/loop.py`: both fixer clients (~249-279 human feedback, ~557-595
       after rejection).
   - **Clearing:** `clear_identities` runs at build start.
   - **Tests** (`tests/test_model_identity.py`, new):
     - an identity is written with no usage payload;
     - a solo run with no later coding session records `coding`;
     - a subtask override is recorded;
     - both fixer sites are recorded;
     - a worktree build writes to the source dir;
     - the `os.devnull` fallback works;
     - concurrent appends lose nothing.
   - **Verify** with `<backend pytest> tests/test_model_identity.py
     tests/test_solo_mode.py tests/test_qa_loop.py -q`.

2. **Control-plane evidence, fresh verdicts and transport (D3 evidence).**
   - **`cli/build_commands.py`:**
     - at build start, before the QA branch (~613), delete
       `qa_evidence.json` and `qa_evidence_report.md` in the source dir;
     - remove the loop's old approval shortcut (`qa/loop.py` ~224).
   - **`qa/loop.py`:**
     - before each cycle, delete the evidence and clear `qa_signoff`;
     - after the reviewer session, on the approved (~438-480) and rejected
       (~482-555) branches, write `qa_evidence.json`;
     - `reviewed_head` is `git rev-parse HEAD` in the build worktree;
     - copy the report to `qa_evidence_report.md` and set `report_sha256`;
     - a missing fresh signoff counts as an error, never an approval;
     - write all of this before `COMPLETE`.
   - **`qa/reviewer.py` and `qa/fixer.py`:** read `ResultMessage` usage and
     duration, and call `record_turn` with worker id `qa-<cycle>` or
     `qa-fixer-<cycle>`.
   - **`core/workspace_fetch.py`:**
     - add per-run-keyed `push_qa_evidence` and `fetch_qa_evidence`;
     - call the push in `cli/main.py` (~537-565) and the fetch in
       `services/completion.py` (~1185-1208);
     - an absent upload deletes local evidence.
   - **Tests** (`tests/test_qa_evidence.py`, new, plus the existing ones):
     - a coder-written worktree `qa_evidence.json` is ignored;
     - evidence is deleted at build start and before each cycle;
     - a `--skip-qa` retry at the same sha leaves no evidence;
     - a stale verdict (changed HEAD, or no fresh signoff) does not count;
     - a sync of `qa_report.md` does not touch the protected copy;
     - QA `record_turn` leaves `workers["main"]` unchanged;
     - the packed push and fetch round trip, with no usage payload and with
       no verdict.
   - **Verify** with `<backend pytest> tests/test_qa_evidence.py
     tests/test_qa_loop.py tests/test_qa_loop_enhancements.py
     tests/test_workspace_fetch_plan.py
     tests/test_workspace_fetch_correction.py -q`.

3. **Reviewer selection (D4).**
   - **Add** `apps/backend/qa/independence.py` with `pick_reviewer(gates,
     identities, *, configured) -> Pick(model, provider, override)`, or raise
     `IndependentReviewUnavailable`.
   - **What it applies:** candidate order, the registry check per candidate
     in non-raising form (`model_registry` ~176; the initial candidate
     included), the configured filter, canonical-id exclusion, provider
     preference, the gateway rules, and the `[QA independence]` log.
   - **Use it in:**
     - `qa/loop.py`, where the QA session is built (~352), and the provider
       failover (~651-676);
     - the reviewer tool fallback (`qa/reviewer.py` ~383-574).
   - **Preflight:** make the build preflight QA resolve
     (`build_commands.py` ~322) non-fatal for a QA model that the pick
     replaces.
   - **Betas:** pass them through `_create_qa_reviewer_provider`
     (`qa/loop.py` ~68-107).
   - **`IndependentReviewUnavailable`:** catch it in `qa/loop.py`, write
     evidence with `status: "unavailable"` and the reason, and return
     not-approved.
   - **Tests** (`tests/test_qa_independence.py`, new):
     - an equal model is re-picked;
     - alias equality;
     - a pin is overridden for QA;
     - the initial candidate denied by the registry;
     - provider preference beats a distinct same-provider QA;
     - same-class routes;
     - two routes with an unknown served model fall back to an explicit
       model;
     - failover is filtered;
     - the verdict-writing fallback is filtered and its identity recorded;
     - unavailable is handled.
   - **Verify** with `<backend pytest> tests/test_qa_independence.py
     tests/test_qa_loop.py tests/test_qa_model_from_source.py -q`.

4. **Remove the QA skips (D2).**
   - **`cli/build_commands.py:311-316`:** delete the solo `skip_qa = True`
     and rewrite the comment: solo keeps one plan-and-implement session, and
     QA still runs.
   - **`build_commands.py:665-750`:** remove the "pre-approved by coder"
     branch.
   - **`qa/criteria.py:53-61,95-109`:** a coder `qa_signoff` is never a
     reason to skip, at any `requires_review` value.
   - **`cli/main.py:204` help text:** say that a `--skip-qa` task cannot
     reach `awaiting_merge`.
   - **Tests:** solo runs QA; a coder signoff is ignored at both
     `requires_review` values. Update the existing assertions that expect the
     skip.
   - **Verify** with `<backend pytest> tests/test_solo_mode.py
     tests/test_qa_criteria.py tests/test_solo_parallel_waves.py -q`.

5. **The gate at every PR entry point (D1).** This is behavioural work, and
   the review covers head selection and retry.
   - **Add** `apps/web-server/server/services/qa_gate.py` with
     `check(source_spec_dir, head_sha)`, applying the pass criteria above,
     with the `sys.path` shim for the backend helpers.
   - **`services/merger.py`:**
     - `_find_pr` (~86/175) also returns `headRefOid`;
     - `_decide` (~343-352) calls the gate for `already_open`, with the PR
       head, and for `opened`, with the selected task ref sha, before the
       push;
     - `MERGED` and `CLOSED` are never gated;
     - `_sync_status` (~427-451) maps the three reasons to
       `("human_review", reason)` and keeps the human-set guard (~445).
   - **`services/completion_orchestration.py`:**
     - add the `.pr_endgame_done` marker;
     - before `run_pr_endgame` (~549-578), skip on gate failure without
       writing it;
     - the TFactory hand-off and `.terminal_side_effects_done` (~207-252)
       are unchanged;
     - the merger call (~592-610) still runs.
   - **`routes/pr.py` `create_pr_from_task`:**
     - for an existing PR (~224-225): gate an `OPEN` PR with its head; never
       gate a `MERGED` one;
     - for a new PR: gate before the rebase and push (~275-382) with the
       task ref sha;
     - return 409 with the reason;
     - a rebase that would move the head returns `qa_stale`.
   - **Tests** (`apps/web-server/tests/test_qa_gate.py`, new, plus the
     existing root tests):
     - all four outcomes, alias equality and a changed report;
     - the merger: `opened` and `already_open` refused, `MERGED` and `CLOSED`
       not blocked, the status mapping, a human-set status kept, a
       remote-only branch;
     - completion: ordinary and adopted, the endgame marker not written on
       failure, and the endgame run on a later pass;
     - the manual route: GitHub and the provider API, an existing `OPEN` PR,
       a `MERGED` PR, and the rebase case.
   - **Verify** with `cd apps/web-server && <web pytest>
     tests/test_qa_gate.py -q`, then, from the root, `<backend pytest>
     tests/test_merge_auto_merger.py tests/test_completion_lands_via_merger.py
     tests/test_completion_evidence_gate.py
     tests/test_create_pr_fetches_branch.py tests/test_pr_endpoints.py -q`.

6. **Read-back (D5).**
   - **`agents/token_attribution.py`:** `read_breakdown(spec_dir,
     source_spec_dir=None)` loads `phaseModels` and `qaEvidence` and passes
     them to `render_breakdown`, which emits them. The internal calls in
     `record_turn` pass nothing.
   - **`routes/tasks_usage.py` (~48-56):** read both from the canonical
     source spec dir, even when `token_usage.json` comes from the worktree.
   - **Tests:** both fields and their shapes; read from the canonical dir;
     the mcp-stdio proxy returns them.
   - **Verify** with `<backend pytest> tests/test_token_attribution.py
     tests/test_tasks_usage_route.py -q`.
   - **Then:** run the full backend suite (`<backend pytest> tests -m "not
     slow" -q`) and the web-server suite (`cd apps/web-server && <web
     pytest> tests -q`).

7. **PFactory QA seed and cost router (D6).**
   - **Where:** a PFactory worktree from `origin/dev`, branch
     `fix/2233-qa-seed`.
   - **`apps/backend/plan/emit/tier_profile.py` (~41-74):** add
     `_QA_MODEL_BY_TIER = {"low": "sonnet", "medium": "opus", "hard":
     "fable"}`, and set `phase_models = {coding: m, qa: …, qa_fixer: m}`.
   - **`cost_router.py` (~92-176, 253-271):**
     - QA candidates exclude the canonical coding id and its aliases (the
       catalog shorthand mapping);
     - a pin applies to coding only;
     - with nothing distinct at the floor, try one class up, else remove
       `qa` from the merged `phase_models` and `phase_routes`;
     - `qa_fixer` is the final coding model;
     - the route and the cost estimate follow the picked QA model;
     - `cost_router_core.py` is untouched.
   - **Tests:** update `tests/test_cost_router.py`, whose assertions expect
     same-model QA and an all-role pin, and `tests/test_tier_profile.py`.
     Add a test for the emitted contract after tier and routing: `qa !=
     coding` by canonical id for every tier, alias forms, a pin, and nothing
     distinct at the floor.
   - **Verify** from the PFactory worktree root with
     `/mnt/code/Source-home/GitHub/PFactory/apps/backend/.venv/bin/pytest
     tests/test_tier_profile.py tests/test_cost_router.py -q`, then the full
     suite with `… pytest tests -q`, plus ruff.

8. **E2E harness (D7),** after both releases are live.
   - **Where:** a Factory worktree from `origin/main`, branch
     `fix/2233-e2e-s3`, file `scripts/e2e/run.py` S3 (~169-203).
   - **Change:**
     - read `phaseModels` and `qaEvidence` from `services.usage`;
     - `runtime_models` requires a known model for every coding and fixer
       identity;
     - the S3 pass requires that the reviewer is not among the identities,
       compared by canonical id;
     - `green` and `review_recorded` require `qaEvidence.status ==
       "approved"` and `reviewed_head` equal to the PR head sha;
     - remove the `workers` read;
     - `.gitignore` drift stays a reported finding (`plan_files_only`
       unchanged).
   - **Tests:** a unit test in `tests/e2e/unit` with a real-shaped payload.
   - **Verify** from the worktree root with `uv run -q --with pytest python
     -m pytest tests/e2e/unit -q`.

## After the steps (lead)

1. **Review:** an Opus review of each PR diff against this plan, plus the
   `reviewers:` above. For step 5, ask explicitly about head selection and
   retry.
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
and web-server suites and the full PFactory suite.

## Rollback

- **AIFactory:** revert the PR on `dev` and cut a patch release. Gitops rolls
  back to the previous image tag.
- **Stuck tasks:** tasks held in `qa_missing` return to normal once the gate
  is gone; nothing else is migrated.
- **PFactory:** revert step 7 and release.
- **The E2E harness change** is independent.

## Deviations recorded during implementation

- **Live E2E: surface packed branch push failures.** The Job uploads a per-run
  `build_push_failed.json` after a rejected push. Completion fetches it and
  parks the task at `human_review/branch_push_failed`; the merger preserves
  that reason and does not attempt a PR for the failed branch.
- **Live E2E: refresh the task head before QA gating.** The merger, completion
  endgame and manual PR route fetch the task branch before resolving its SHA.
  A missing SHA now reports `head_unavailable` instead of `qa_stale`.
- **Live E2E: pin the PR push to the reviewed SHA.** The merger and completion
  endgame pass their checked SHA to `create_pr`, which pushes that commit and
  verifies the remote head before opening a PR.

- **Step 1: preserve identities across resumed builds.** Build startup no
  longer clears `model_identities.json`; the reset could erase earlier coding
  identities before QA selected a reviewer. Keeping them may exclude extra
  models, which is the safe direction for independent review.

- **Step 3: the other-provider candidates.** The plan listed "a frontier
  model from another provider" as the second candidate.
  - **Why it changed:** AIFactory's model catalog has only Claude frontier
    entries, and `check_model_registered` accepts any non-Claude name. A
    guessed id could therefore be picked, and would then fail at runtime.
  - **What it is now:** other-provider candidates come from catalog entries
    with real ids and `AIFACTORY_QA_ALT_MODELS`, a comma-separated list of
    operator-configured ids. Guessed ids are never used. With nothing
    configured, QA uses the Claude ladder: opus, fable, sonnet.

- **Step 3: configured and approved-plan models are excluded.** Reviewer
  selection also excludes canonical coding and fixer models from run settings,
  plus each subtask model override in the approved plan snapshot. Planning
  identities are ignored; the solo coding session remains recorded as coding.
- **Step 5: an unreadable existing PR head is stale.** Adopted and existing
  PRs no longer substitute the local task ref when their head cannot be read.
- **Step 5: claim the endgame before running it.** The marker uses an exclusive
  create to prevent overlapping completion calls from running the endgame
  twice; a refused QA gate removes the claim for a later retry.
- **Step 5: push the reviewed head.** Manual PR creation no longer rebases or
  force-pushes when the base moves. The unchanged task head remains QA-gated;
  GitHub reports merge conflicts.
- **Step 5: recheck QA at auto-merge.** The endgame checks the current PR head
  against the source QA evidence immediately before its merge gate.
- **Step 6: malformed read-back is empty.** Invalid identity or QA evidence
  files produce empty `phaseModels` or null `qaEvidence` instead of raising.
- **Step 6: the QA report endpoint reads the protected copy.** Only
  `qa_evidence_report.md` reaches the project spec dir, so
  `GET /api/tasks/{id}/qa-report` falls back to it when `qa_report.md` is
  absent. Found by the live E2E run (Factory#3882), where it returned 404 for
  an approved task.
