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
- when no distinct reviewer exists, QA overrides the pin for QA only and logs
  it, and the task is refused only if no second model is configured at all;
- the token figure is a separate issue (AIFactory#2234).

Revision 2 follows the Codex and agy spec reviews. It adds the manual PR
route, makes the evidence control-plane-owned and tied to a commit, captures
coding identities when a session starts, and moves QA model selection to QA
session construction.

## Design

### D1. One gate before any PR counts as the task's PR (AIFactory)

**The helper.** `services/qa_gate.py` holds
`check(source_spec_dir, head_sha) -> None | "qa_missing" | "qa_stale" | "qa_not_independent"`.

**What it reads.**

- `qa_evidence.json` (D3);
- `model_identities.json` (D3);
- the report.

**When it passes.** All of the following must hold:

- `status == "approved"`;
- `reviewed_head == head_sha`;
- the report's sha256 matches `report_sha256`;
- every identity in `model_identities.json` has a known model;
- `reviewer` is not equal to any of those identities (compared by canonical
  model id).

**It is called at every point where a PR becomes the task's PR.**

1. **Merger** `merger._decide`.
   - It is called after `_find_pr`, only for the `opened` and `already_open`
     outcomes.
   - It never blocks `MERGED → done` or `CLOSED → pr_closed`.
   - For `opened`, it runs before the push and create.
   - For `already_open`, it runs before the mapping to `awaiting_merge`.
2. **Completion endgame**, before `run_pr_endgame` in
   `completion_orchestration.py` (~549-578). This one check covers both
   auto-PR and an adopted PR.
   - For an adopted PR that fails the gate, the fire-once marker is not set,
     so a later successful QA resumes the endgame.
3. **Manual PR route** `routes/pr.py` `create_pr_from_task` (~97-534).
   - It is called before it returns an existing PR or opens one (GitHub, `gh`
     or the provider API).
   - This covers the UI dialog, the mcp-stdio `proxy_create_pr` and the
     `task_create_pr` agent tool.
   - On failure it returns 409 with the reason.

**Status.** `merger._sync_status` maps `qa_missing`, `qa_stale` and
`qa_not_independent` to `("human_review", reason)` explicitly. They replace
`awaiting_merge` or `None`. A status a human set is never overwritten
(unchanged rule).

**Out of scope.** Copilot dispatch (`execution.py` ~1925-2019): GitHub's agent
writes that code, and AIFactory runs no QA there. A follow-up issue covers it.

### D2. Stop skipping QA in the normal path (AIFactory)

- **Solo mode.** `cli/build_commands.py:315-316` no longer sets
  `skip_qa = True`. Solo keeps its single plan-and-implement session.
- **The coder's self-approval.** `qa/criteria.py`: `is_qa_approved` and
  `should_run_qa` ignore `qa_signoff` in `implementation_plan.json` as a skip
  signal, at any `requires_review` value. The "QA pre-approved by coder"
  branch (`build_commands.py:665-750`) is removed.
- **`--skip-qa`.** The flag stays as an operator and debug option. D1 means
  such a task never reaches `awaiting_merge`, and the help text says so.

### D3. Control-plane evidence, bound to the reviewed commit (AIFactory)

**Writer and location.**

- The QA loop's own Python code (`qa/loop.py`, approved branch ~438-480 and
  rejected branch ~482-555), not an agent session, writes
  `qa_evidence.json` and `model_identities.json`.
- They go to the **source spec dir** (`source_spec_dir`), which worktree
  sessions can only read (`core/client.py` ~773-785, the same pattern as
  `injection_scan.json`).
- They are never synced back from a worktree.
- The QA loop deletes any `qa_evidence.json` at run start and before each QA
  cycle.

**`qa_evidence.json` fields.**

- `status`;
- `reviewer` (model, provider, served model if known);
- `reviewed_head` (git HEAD of the worktree at the verdict);
- `report_sha256`, plus a copy of `qa_report.md` in the source spec dir;
- `qa_cycle`;
- `override` (D4's re-pick reason or null);
- `at`.

**Only a fresh verdict counts.** It must be a verdict the reviewer session
wrote during the current cycle. The loop clears `qa_signoff` before the cycle
and reads it after. A missing fresh verdict counts as an error, not as an
approval.

**`model_identities.json`.**

- It is an append-only list of `{phase, subtask_id, model, provider,
  served_model}`.
- It is written when the session is built, by `coder.py`,
  `parallel_integration.py` and `qa/fixer.py`, through one helper
  `record_identity(source_spec_dir, ...)`.
- It does not depend on billing or usage payloads.
- QA fixer identities are included: QA re-reviews fixer output, so the
  reviewer must differ from the fixer too.

**Packed and kubejob builds.** `core/workspace_fetch.py` pushes and fetches
`qa_evidence.json`, `model_identities.json` and the `qa_report.md` copy
alongside `token_usage.json`. The control plane writes them to its source spec
dir on fetch.

**QA usage.** The QA reviewer and fixer call `record_turn` with their own
`worker_id` (`qa-<cycle>` or `qa-fixer-<cycle>`), so they never overwrite
`workers["main"]`. The usage and duration plumbing is added to
`qa/reviewer.py` and `qa/fixer.py`. This is for cost only: the D1 gate does
not read it.

### D4. QA picks an independent model when its session is built (AIFactory)

**Where:** `qa/loop.py` (~352, and the failover ~651-676), through a new
`qa/independence.py` `pick_reviewer(gates, identities) -> Pick`.
`phase_config.get_phase_model` is left alone, because it is called with
`Path(os.devnull)` and carries no per-session identity.

**The candidates, in order:**

1. the resolved QA model (`gates.model("qa")`);
2. a frontier model from another configured provider (preferred provider
   first);
3. `DEFAULT_PHASE_MODELS["qa"]` (opus);
4. fable;
5. sonnet.

**The filter.** A candidate is kept only if it is:

- eligible in the model registry (checked per candidate, so a `deny`-mode
  rejection moves on to the next one);
- configured, meaning provider credentials or a gateway exist;
- not equal to any coding or fixer identity, by canonical id.

**Provider preference.** When the resolved QA model is already distinct but
on the same provider as coding, a distinct candidate on another configured
provider wins.

**Gateway routes.**

- **Same class:** a QA class equal to a coding class means re-pick.
- **Unknown served model:** if the gateway does not report the served model
  and QA and coding are both routes, independence is unverified. QA moves to
  an explicit, non-route candidate.
- **Nothing distinct:** if no such candidate exists, the result is
  `IndependentReviewUnavailable`. The task lands in Needs you with that
  reason.

**Failover.** `next_provider` on QA errors uses the same filter. Failover
never lands on a coding identity.

**Betas.** The beta headers and the thinking config are derived from the
picked model, not re-resolved through `get_phase_model_betas`.

**Logging.** Any change from the resolved model is logged as
`[QA independence] …` and recorded in `qa_evidence.override`.

### D5. Read back the models used (AIFactory API)

- **The fields.** `read_breakdown(spec_dir)` passes to `render_breakdown`:
  - `phaseModels`: `{phase: [model…]}` from `model_identities.json`;
  - `qaEvidence`: the content of `qa_evidence.json`, or null.
- **Where they are read.** `routes/tasks_usage.py` reads both from the
  canonical source spec dir, even when `token_usage.json` falls back to the
  worktree copy.
- **Where they show.** In `GET /api/tasks/{id}/token-usage`, including the
  mcp-stdio proxy.

### D6. PFactory stops seeding QA with the coding model

**Where:** `apps/backend/plan/emit/tier_profile.py` (~72) and `cost_router.py`.

**Seeds.**

| Tier | coding | qa | qa_fixer |
|---|---|---|---|
| low | haiku | sonnet | haiku |
| medium | sonnet | opus | sonnet |
| hard | opus | fable | opus |

At hard, QA stays at the frontier floor and differs from the coder.

**Cost router.** When it selects `qa`, the router removes the canonical
coding id (and its aliases) from the candidates. `phase_models.qa`,
`phase_routes.qa` and the cost estimate stay populated and consistent.

**A pin** applies to coding. QA is not pinned onto the same model.

**Nothing distinct at the floor.** The router emits `qa` one class up. If
that also yields nothing, it leaves `qa` unset and AIFactory D4 decides.

`qa_fixer` stays equal to coding by design: it writes fixes, and D3 treats it
as a coding identity.

### D7. E2E harness follow-up (Factory, separate PR after the releases)

`scripts/e2e/run.py` S3 reads `phaseModels` and `qaEvidence`:

- **`runtime_models`:** every coding and fixer identity is known;
- **independence:** `reviewer ∉ identities`;
- **`green` / `review_recorded`:** `qaEvidence.status == "approved"`, and
  `reviewed_head` equals the PR head.

`.gitignore` drift stays a reported finding.

## Alternatives rejected

- **Only removing the solo `skip_qa`.** `qa_signoff`, `--skip-qa`, pins,
  routes and the manual PR route still get around review.
- **Evidence in the worktree spec dir.** The coder can write it (Write, Edit
  and Bash are granted there).
- **Choosing at `get_phase_model`.** It runs with `os.devnull` and no session
  identity, and cannot see per-subtask overrides or failover.
- **Exposing `workers` as-is.** It is last-write-wins and billing-dependent,
  and QA does not write to it.
- **Requiring the PR endgame review.** It is off by default and never posts.
- **Requiring a different provider.** Not every deployment has two.

## Risks

- **Direct (`--direct`) and packed builds share one filesystem.** There, an
  agent with `Bash(*)` could still write the source spec dir.
  - D3 narrows the gap: the file is deleted before each cycle, and a forged
    verdict would also have to match `reviewed_head` and `report_sha256`.
  - D3 does not close it. Closing it needs evidence signed outside the pod.
    The follow-up issue is named in the plan.
  - Prod runs isolated worktrees.
- **Cost and time:** low and medium tasks gain one QA session.
- **Tasks stuck in Needs you:** a QA infrastructure failure now blocks the
  merge. That is intended, and visible as `qa_missing`.
- **In-flight tasks without evidence** land in `qa_missing` at their next
  sweep. A retry runs QA.
- **Weak reviewers:** QA is never weaker than the coder by tier: sonnet over
  haiku, opus over sonnet, fable over opus. Reviewing at hard costs more.
- **Hosts:** prod k3s through gitops only. No p510.

## Verification

**AIFactory unit tests.**

- **D1, every entry point:**
  - the merger, for `opened` and `already_open` (refused), and for
    `MERGED`/`CLOSED` (not blocked);
  - the completion endgame, ordinary and adopted, including the marker not
    being set;
  - the manual PR route, on GitHub and on the provider API (409);
  - all four gate outcomes;
  - `_sync_status` mapping.
- **D2:** solo runs QA, and a coder `qa_signoff` is ignored.
- **D3:**
  - a coder-written worktree `qa_evidence.json` is ignored;
  - the evidence is deleted before each cycle;
  - a stale verdict, with a changed HEAD or no fresh signoff, does not count;
  - identities are captured with no usage payload;
  - the workspace_fetch round trip works.
- **D4:**
  - an equal model is re-picked;
  - a pin is overridden for QA;
  - a `deny`-mode registry rejection moves to the next candidate;
  - provider preference applies;
  - two different gateway classes with an unknown served model fall back to
    an explicit model;
  - failover is filtered;
  - `IndependentReviewUnavailable` is raised.
- **D5:** `phaseModels` and `qaEvidence` are read from the canonical dir.

**PFactory unit tests.**

- The emitted contract, after the tier profile and cost routing, has
  `qa != coding` by canonical id, with shorthand and full-id aliases, with a
  pin, and with no distinct model at the floor.
- `phase_routes.qa` and the cost estimate are populated.

**Live.** After both releases, rerun E2E P1:cli:1. S3 must pass with:

- the reviewer not among any coding identity, read through the API;
- `reviewed_head` equal to the PR head;
- a QA report present.
