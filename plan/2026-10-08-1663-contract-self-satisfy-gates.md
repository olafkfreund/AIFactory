---
status: draft
issue: 1663
spec: spec/2026-10-08-1663-contract-self-satisfy-gates.md
---

# Plan: a task contract cannot clear its own system gates

**Do not start until #1667 has merged to `dev`.** #1667 re-verifies the signed
contract at merge time. This plan assumes `context/task_contract.json` can be
trusted, and does nothing to defend it. Before step 1, rebase the branch:
`git fetch origin && git rebase origin/dev`. Line numbers below are from
`e60adf22`. After the rebase, find each anchor with `grep -n` before you edit.

## Approved decisions (self-contained summary)

1. **Gate evidence no longer comes from the spec directory.** Today
   `satisfied_system_gates` (`apps/web-server/server/services/pr_endgame.py:313-341`)
   counts a gate as cleared when it appears in the contract's own
   `deployment.satisfied_gates` or in `satisfiedSystemGates` /
   `satisfied_system_gates` in `task_metadata.json`. A contract can therefore
   declare `system_gates: ["human-approval"]` and
   `satisfied_gates: ["human-approval"]` and auto-merge without a human. The
   coding agent can also write `task_metadata.json`. Both sources are removed:
   delete the function and `SATISFIED_GATES_KEYS`. The contract field becomes
   inert: it is still signed and parsed, and ignored.
2. **The only evidence is a GitHub PR review.** A `human-approval` gate clears
   only when a GitHub PR review meets all of these:
   - its author is not the PR author and does not have `user.type == "Bot"`;
   - it is that reviewer's latest review, counting only APPROVED,
     CHANGES_REQUESTED and DISMISSED (COMMENTED is ignored), and its state is
     APPROVED;
   - its `commit_id` equals the PR's current head SHA exactly. Approvals of
     older commits are stale.
   The PR author *is* AIFactory's gh identity, because `create_pr` runs
   `gh pr create` under the same login. That is how the bot is excluded, with
   no extra lookup or config.
3. **Reuse the existing `gh` access.** Do not add a new client. Use the
   injectable `Runner` / `_default_runner` (`pr_endgame.py:63-69`), and the
   same `gh api .../pulls/{pr}/reviews` endpoint as `read_review_verdict`
   (`:707-752`). Pass `--paginate`.
4. **Pre-PR split.** `run_pr_endgame` evaluates the disposition with no
   evidence. If the result is not AUTO_MERGE, it asks a hypothetical: would it
   be AUTO_MERGE with `HUMAN_ONLY_GATES` satisfied?
   - If yes, keep `auto_merge` on and pass `human_approval_required=True` to
     the watcher.
   - If no, turn `auto_merge` off (today's behaviour).
   Production and `risk_class: high` are not gates, so they still turn
   `auto_merge` off.
5. **Bind the merge to the approved commit.** Just before `merge_pr`, the
   watcher calls `human_approval_head()`. If it returns `None`, the watcher
   keeps polling and does not merge. Otherwise it calls
   `merge_pr(..., match_head=sha)`, which adds `--match-head-commit <sha>` and
   skips the `update-branch` retry (that retry would create a commit nobody
   approved).
6. **Fail closed.** Any `gh` failure or bad JSON returns `None`, so there is
   no merge. The watcher times out after `_MAX_POLL_MINUTES = 20` into its
   existing human-stop, and the PR is left open.
7. **The path floor is not loosened** (spec decision A).
   `apply_path_risk_floor` calls `deployment_block_reasons(deployment)` with
   no evidence. When enforcing, any declared system gate floors the tier to
   `blocking`, so those tasks merge by hand.
8. **Accepted consequences.** Today AIFactory's gh identity is `olafkfreund`,
   a User account, and the PR author. No approval can count, so every
   `human-approval` task merges by hand until a separate bot identity exists
   (follow-up issue; decision B). Approvals after the 20-minute window do not
   auto-merge (decision C).
9. **Matrix.**
   - Add a probe to `_live_overlay_rows` for a contract that declares and
     self-satisfies `human-approval`. It must read `hold-blocking`.
   - Reword `wiring.live_overlay` in `docs/compliance/control-objectives.toml`.
   - Regenerate with the generator and verify with `--check`.

## Repo traps (apply to every step)

- **Venv.** The venv lives in the main checkout, not the worktree:
  `V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv`.
- **Web-server tests.** Run them from the worktree root:
  `$V/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto`. Add `-k`
  or a file path to narrow the run.
- **Pre-commit.** It needs the venv on `PATH`:
  `PATH="$V/bin:$PATH" git commit ...`.
- **Commits.** Messages go through `git commit -F - <<'MSG'` and end with the
  session trailers. One commit per step, naming the step:
  `fix(merge): <what> (#1663, plan step N)`.
- **`FakeRunner`** (`apps/web-server/tests/test_pr_endgame.py:24-40`) matches
  argv by substring, and the first matching route wins. `"/pulls/5/reviews"`
  also contains `"/pulls/5"`, so list the `"reviews"` route before a
  `"pulls/5 "` route, or key the PR call on `".head.sha"`. Any unmatched call
  returns rc 0 with empty output, so assert on `r.saw(...)` and do not rely
  on defaults.
- **Log injection.** Never interpolate review logins or SHAs from GitHub into
  `logger` calls raw. Wrap them in `sanitize_log(...)`, as the file already
  does.
- **The matrix is generated.** Never hand-edit
  `docs/docs/compliance/autonomy-matrix.md` or
  `docs/static/compliance/autonomy-matrix.json`.
- **The plan is the contract.** If an edit must deviate from it, update this
  file in the same commit.

## Steps

1. **Write the failing tests first.** Edit
   `apps/web-server/tests/test_pr_endgame_merge_gate.py` and
   `apps/web-server/tests/test_pr_endgame.py`.
   - **In the merge-gate file,** using the existing `_contracted` /
     `_deployment` helpers (`:258-279`), add:
     - `test_a_contract_cannot_satisfy_its_own_gate`:
       `_deployment(system_gates=["human-approval"], satisfied_gates=["human-approval"])`
       gives `merge_disposition(spec, "low") == pe.HOLD_BLOCKING_DISPOSITION`.
     - `test_task_metadata_is_not_gate_evidence`: the same contract without
       `satisfied_gates`, plus `satisfiedSystemGates=["human-approval"]` as
       metadata, also holds.
     - `test_merge_disposition_takes_explicit_evidence`:
       `merge_disposition(spec, "low", satisfied_gates=["human-approval"])`
       on the `system_gates`-only contract returns AUTO_MERGE.
     - Async `test_a_human_only_hold_keeps_the_watcher_armed`: extend
       `_endgame_auto_merge` (`:43-72`) so `_fake_watch` also captures
       `kwargs.get("human_approval_required")`. A `system_gates`-only contract
       with tier `"auto"` gives `auto_merge is True` and
       `human_approval_required is True`. A production contract gives
       `auto_merge is False`.
     - Replace `test_satisfied_gates_reads_both_the_contract_and_the_metadata`
       and `test_satisfied_gates_of_an_unreadable_spec_is_empty` (`:241-250`)
       with nothing. The function they test is deleted, and the two tests
       above take over.
     - Rewrite `test_a_satisfied_system_gate_clears_only_its_own_hold`
       (`:302-311`) to clear via `satisfied_gates=["human-approval"]` passed
       to `merge_disposition`, not via metadata.
     - Rewrite `test_a_satisfied_gate_never_clears_production` (`:322-331`)
       the same way.
   - **In `test_pr_endgame.py`,** next to the `read_review_verdict` tests
     (around `:140-160`), add tests for `human_approval_head`. Use
     `FakeRunner` with a PR route returning
     `{"head": "abc", "author": "olafkfreund"}` and a reviews route returning
     newline-separated JSON objects (`--paginate` with `--jq '.[] | {...}'`
     emits one object per line):
     - approved by `alice` (`type: "User"`, `commit_id: "abc"`) returns `"abc"`;
     - approved by the PR author returns `None`;
     - approved by a `type: "Bot"` reviewer returns `None`;
     - approved on `commit_id: "old"` returns `None`;
     - APPROVED then CHANGES_REQUESTED by `alice` returns `None`;
     - APPROVED then DISMISSED by `alice` returns `None`;
     - APPROVED then COMMENTED by `alice` returns `"abc"`;
     - rc 1 on either call, or junk JSON, returns `None`.
   - **Also in `test_pr_endgame.py`,** add watcher tests: copilot approved,
     `auto_merge=True`, `human_approval_required=True`, `poll_interval=0`,
     `max_minutes=1`:
     - with no qualifying review, `not r.saw("pr merge")` and
       `res["merged"] is False`;
     - with a qualifying review, there is a call containing
       `"--match-head-commit abc"`;
     - with a qualifying review and `"pr merge"` returning rc 1
       `"not mergeable"`, `not r.saw("update-branch")`.

   Verify with
   `$V/bin/pytest apps/web-server/tests/test_pr_endgame_merge_gate.py apps/web-server/tests/test_pr_endgame.py -q -o asyncio_mode=auto`:
   the new tests **fail** (or error with `AttributeError` /
   `TypeError: unexpected keyword`) and every pre-existing test still passes.
   Commit as `test(merge): ... failing (#1663, plan step 1)`. Pre-commit may
   reject failing tests. If it does, commit steps 1 and 2 together and record
   in the PR that step 1's tests were run red before step 2.

   Traps: the `FakeRunner` ordering, and async tests need
   `-o asyncio_mode=auto`.

2. **Remove the spec-directory evidence and add the `merge_disposition`
   argument.** Edit `apps/web-server/server/services/pr_endgame.py` and
   `apps/backend/merge/merge_policy.py` / `merge/__init__.py`.
   - Delete the comment, `SATISFIED_GATES_KEYS` and `satisfied_system_gates`
     (`pr_endgame.py:313-341`).
   - Path floor, `:251-260`: change the call to
     `deployment_block_reasons(deployment)`. Replace the comment above it with
     one line saying the floor sees no gate evidence, since the PR does not
     exist yet (#1663).
   - `merge_disposition`, `:459`: change the signature to
     `def merge_disposition(spec_dir: Path, tier: str | None, *, satisfied_gates: Iterable[str] = ()) -> str:`.
     Drop the line `gates = satisfied_system_gates(spec_dir, deployment)`
     (`:500`), and pass `satisfied_gates=list(satisfied_gates)` to
     `decide_merge` (`:513`). Add one sentence to the docstring: "Gate
     evidence comes only from the caller (a GitHub review, #1663), never from
     the contract or task_metadata."
   - In `merge_policy.py:55`, rename `_HUMAN_ONLY_GATES` to
     `HUMAN_ONLY_GATES`, keep `_HUMAN_ONLY_GATES = HUMAN_ONLY_GATES` as an
     alias, add `"HUMAN_ONLY_GATES"` to `__all__` (`:40-47`), and re-export it
     from `merge/__init__.py` (import list `:6-12`, `__all__` around `:43`).

   Verify with `$V/bin/pytest apps/web-server/tests/test_pr_endgame_merge_gate.py apps/web-server/tests/test_pr_endgame_path_risk_floor.py apps/backend/merge -q -o asyncio_mode=auto`.
   The step-1 disposition tests pass, `test_an_outstanding_gate_still_floors_the_tier`
   still passes, and only the watcher, `human_approval_head` and the
   armed-watcher tests still fail. Also check that
   `grep -rn "satisfied_system_gates\|SATISFIED_GATES_KEYS" apps scripts`
   finds nothing.

   Traps: `Iterable` comes from `collections.abc`, and the file may already
   import it. Do not change `deployment_block_reasons` itself.

3. **Add `human_approval_head` and `match_head` on `merge_pr`.** Edit
   `apps/web-server/server/services/pr_endgame.py`.
   - **`human_approval_head`.** Add it after `read_review_verdict`
     (`:752`):
     ```python
     def human_approval_head(
         owner: str, repo: str, pr: int, *, runner: Runner = _default_runner
     ) -> str | None:
     ```
     - Call 1: `["gh", "api", f"/repos/{owner}/{repo}/pulls/{pr}", "--jq", "{head: .head.sha, author: .user.login}"]`.
     - Call 2: `["gh", "api", "--paginate", f"/repos/{owner}/{repo}/pulls/{pr}/reviews", "--jq", ".[] | {state, login: .user.login, type: .user.type, commit_id}"]`.
     - Parse call 2 one JSON object per non-empty line.
     - Keep the latest state per login, only for states in
       `{"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}`. The API returns
       reviews oldest first.
     - Return `head` if any login meets all of: `login != author`,
       `type != "Bot"`, latest state `APPROVED`, and that review's
       `commit_id == head`. Otherwise return `None`.
     - Return `None` on `not res.ok`, `ValueError` / `TypeError`, a missing
       or empty `head`, or a non-dict row.
     - The docstring states the three rules and "fails closed: any error
       means no approval".
   - **`merge_pr`** (`:755`): add `match_head: str | None = None`. Build
     `extra = ["--match-head-commit", match_head] if match_head else []` and
     append it to both `gh pr merge` argv lists (`:774`, `:787`). In the
     non-clean branch (`:780`), when `match_head` is set, log and
     `return False` before `update-branch`. The comment should say that a
     branch update creates a head nobody approved.

   Verify with `$V/bin/pytest apps/web-server/tests/test_pr_endgame.py -q -o asyncio_mode=auto -k "human_approval or merge"`:
   the `human_approval_head` tests pass.

   Traps: the `FakeRunner` ordering, and `sanitize_log` on any logged login
   or SHA.

4. **Wire the pre-PR split and the watcher gate.** Edit
   `apps/web-server/server/services/pr_endgame.py`.
   - **`watch_and_finish`** (`:904`): add the keyword
     `human_approval_required: bool = False`, and document it in the
     docstring. In the `if approved:` branch, after the
     `if not auto_merge: return {...}` block and before
     `merged = await asyncio.to_thread(merge_pr, ...)` (`:1004`):
     ```python
     match_head = None
     if human_approval_required:
         match_head = await asyncio.to_thread(
             human_approval_head, owner, repo, pr, runner=runner
         )
         if match_head is None:
             continue  # no human approval of the head commit yet
     merged = await asyncio.to_thread(
         merge_pr, owner, repo, pr, runner=runner, match_head=match_head
     )
     ```
     The loop's timeout then returns the existing human-stop result
     unchanged.
   - **`run_pr_endgame`** (`:1299-1313`): initialise
     `human_approval_required = False` before the `if auto_merge:` block.
     Inside it, after `disposition = merge_disposition(spec_dir, review_tier)`:
     ```python
     if disposition != AUTO_MERGE_DISPOSITION and merge_disposition(
         spec_dir, review_tier, satisfied_gates=HUMAN_ONLY_GATES
     ) == AUTO_MERGE_DISPOSITION:
         human_approval_required = True
     elif disposition != AUTO_MERGE_DISPOSITION:
         ... existing log + auto_merge = False ...
     ```
     Import `HUMAN_ONLY_GATES` inside the function, in the same guarded
     `try: from merge.merge_policy import ...` style that `merge_disposition`
     uses (`:484-497`). If the import fails, do not set the flag and turn
     `auto_merge` off, so it fails closed. Log the human-approval case with a
     constant message (`"auto-merge waits for a human GitHub approval of the
     head commit"`) and `sanitize_log(spec_id)`.
   - Pass `human_approval_required=human_approval_required` to
     `watch_and_finish(...)` (`:1367`).

   Verify with `$V/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto`:
   the whole web-server suite is green, including every step-1 test.

   Traps: `continue` must stay inside the poll `for` loop. Do not change the
   `review_fn` / `require_copilot` logic. The human gate is in addition to the
   Copilot or engine approval and never replaces it.

5. **Autonomy matrix.** Edit `scripts/gen_autonomy_matrix.py`,
   `docs/compliance/control-objectives.toml`, and regenerate the outputs.
   - In `_live_overlay_rows` (`scripts/gen_autonomy_matrix.py:303-338`),
     append a third probe to `probes`:
     `{"system_gates": ["human-approval"], "satisfied_gates": ["human-approval"]}`.
     The row label formats the lists with `", ".join(f"{k}={v}" ...)`, which
     is fine. If the `--check` row-count floors (`:857`) assert an exact live
     row count, raise the count by 2: one probe times the two
     `PATH_RISK_FLOOR` values.
   - In `docs/compliance/control-objectives.toml:75`, set `objective` to:
     "On the live merge path a high risk or production deployment holds the
     merge for a human regardless of the path-floor flag; a required
     human-approval gate clears only on a GitHub approval of the head commit by
     someone other than the PR author or a bot; an unreadable contract holds it
     too."
   - Regenerate with `$V/bin/python scripts/gen_autonomy_matrix.py`, then
     check with `$V/bin/python scripts/gen_autonomy_matrix.py --check`.

   Verify: `--check` exits 0. In `docs/docs/compliance/autonomy-matrix.md`,
   the two new live-overlay rows (flag unset / `1`) both read
   `hold-blocking`. `git diff --stat` shows only the generator, the TOML and
   the two generated files.

   Traps: never hand-edit the generated files. If a matrix test exists
   (`grep -rln gen_autonomy_matrix apps scripts tests`), run it too.

## Tests

- `$V/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto` is all green.
  Run on unmodified `dev` (+#1667), the step-1 tests fail.
- `$V/bin/pytest apps/backend/merge -q` is green.
- `$V/bin/python scripts/gen_autonomy_matrix.py --check` exits 0.
- `grep -rn "satisfied_system_gates\|SATISFIED_GATES_KEYS\|satisfiedSystemGates" apps scripts`
  has no hits outside tests that assert it is ignored.
- `PATH="$V/bin:$PATH" pre-commit run --files <changed files>` passes.

## Rollback

Each step is its own commit, so `git revert` the step commits in reverse order
(5, 4, 3, 2, 1). Reverting restores the old behaviour, where a contract can
self-satisfy its gates. The safer emergency lever is to leave the code in
place and turn `AIFACTORY_AUTO_MERGE` off for the project. That stops every
auto-merge, and nothing in this change can loosen it. After a revert, re-run
the generator so the matrix matches the code again.
