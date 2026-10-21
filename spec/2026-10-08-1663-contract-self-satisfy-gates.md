---
status: approved
issue: 1663
intent: intent/2026-10-08-1663-contract-self-satisfy-gates.md
---

# Spec: a task contract cannot clear its own system gates

**Depends on #1667.** #1667 makes the merge path re-verify the contract, and it
must land first. This spec assumes `context/task_contract.json` is the signed
contract PFactory issued. It does not defend against the coding agent editing
that file.

## Design

### 1. No evidence comes from the spec directory

Delete `satisfied_system_gates` and `SATISFIED_GATES_KEYS`
(`apps/web-server/server/services/pr_endgame.py:313-341`). Both of its
evidence sources go away: the contract's `deployment.satisfied_gates`
(intent answer 1) and `satisfiedSystemGates` in `task_metadata.json`
(answer 4).

Its two callers change as follows:

- **Path-risk floor** (`pr_endgame.py:256-258`): calls
  `deployment_block_reasons(deployment)` with no `satisfied_gates`. A declared
  system gate then always counts as outstanding there. The floor runs before
  the PR exists (`gather_pr_context`, `pr_endgame.py:1218`), so there is no
  evidence to read yet. This is never looser than today: it is equal or
  stricter for every contract.
- **`merge_disposition`** (`pr_endgame.py:459`): gains a keyword argument
  `satisfied_gates: Iterable[str] = ()`, which replaces the read at `:500` and
  is passed to `decide_merge` as before (`:509-514`). The default is "no
  evidence", so every existing caller holds every declared gate.

### 2. The pre-PR decision splits "held" from "held only for a human"

`run_pr_endgame` (`pr_endgame.py:1299-1313`) currently computes the
disposition once and turns `auto_merge` off if it is not `AUTO_MERGE`. The new
logic:

```
disposition = merge_disposition(spec_dir, tier)                  # no evidence
if disposition != AUTO_MERGE:
    if merge_disposition(spec_dir, tier,
                         satisfied_gates=HUMAN_ONLY_GATES) == AUTO_MERGE:
        human_approval_required = True    # the only blocker is human-approval
    else:
        auto_merge = False                # unchanged: held, a human merges
```

`HUMAN_ONLY_GATES` is `merge_policy._HUMAN_ONLY_GATES`
(`apps/backend/merge/merge_policy.py:55`), renamed to public and exported
through `merge/__init__.py`. The second call asks a hypothetical question:
would the policy allow the merge once a human has approved? It does not
record any approval. Production and `risk_class: high` reasons are not gates
(`merge_policy.py:233-242`), so they still turn `auto_merge` off here, as they
do today.

`human_approval_required` is passed to `watch_and_finish`
(`pr_endgame.py:904`, called at `:1367`) as a new keyword. It defaults
to `False`, which leaves the watcher's behaviour unchanged.

### 3. How the merge path reads the approval

There is no new client. The new function uses the injectable `Runner` /
`_default_runner` (`pr_endgame.py:63-69`) that already drives `gh` here. It
makes the same `gh api /repos/{owner}/{repo}/pulls/{pr}/reviews` call as
`read_review_verdict` (`pr_endgame.py:707-725`), so it uses the gh token
already set up for `create_pr` and `merge_pr`.

New function `human_approval_head(owner, repo, pr, *, runner) -> str | None`.
It makes two calls:

1. `gh api /repos/{o}/{r}/pulls/{pr} --jq '{head: .head.sha, author: .user.login}'`
2. `gh api --paginate /repos/{o}/{r}/pulls/{pr}/reviews --jq '.[] | {state, login: .user.login, type: .user.type, commit_id}'`

It returns the head SHA only when some reviewer meets all of these:

- **Not the bot or the author.** `login` is not the PR author, and `type` is
  not `"Bot"`. The PR author *is* the bot: `create_pr` opens the PR with
  `gh pr create` under the same gh identity (`pr_endgame.py:558`, `:587-600`).
  Excluding the author therefore excludes AIFactory, and excluding
  `type == "Bot"` excludes Copilot (`COPILOT_REVIEWER`, `:40`) and any other
  App. No identity is configured or looked up separately.
- **Latest state is APPROVED.** Only `APPROVED`, `CHANGES_REQUESTED` and
  `DISMISSED` reviews count (`COMMENTED` is skipped), and that reviewer's most
  recent one must be `APPROVED`. A dismissed or superseded approval does not
  count.
- **Not stale.** `commit_id` must equal the current head SHA exactly. An
  approval of an earlier commit does not count, because the auto-fix loop
  (`fix_fn`, `:956-985`) and the conflict fixer push new commits after a human
  may have looked. "Newer than" cannot be judged from a review's timestamp
  anyway. Matching the commit the human saw is the check that means something.

### 4. Where the merge happens, and binding it to the approved commit

In `watch_and_finish`, inside the existing `if approved:` branch, before
`merge_pr` (about `:1000`), when `human_approval_required` is set:

- `sha = human_approval_head(...)`. If it is `None`, `continue` polling
  without merging.
- Otherwise call `merge_pr(..., match_head=sha)`.

`merge_pr` (`pr_endgame.py:755`) gains `match_head: str | None = None`. When it
is set, it adds `--match-head-commit <sha>` to `gh pr merge` (`:774`), so
GitHub refuses the merge if a commit landed between the check and the merge.
It also skips the `update-branch` retry (`:785-787`), because that retry
creates a new head the human never approved, and returns `False`. The watcher
then takes its existing conflict or human-stop path.

### 5. Failure modes, all closed

Any `gh` failure (non-zero exit, timeout, unparsable JSON, missing head SHA)
makes `human_approval_head` return `None`. The watcher then keeps polling and
times out to its existing human-stop (`_MAX_POLL_MINUTES = 20`, `:49`). The PR
is left open and nothing is merged. GitHub being unreachable can never produce
a merge, because the only way to merge is to positively match an approval to
the head commit.

### 6. Autonomy matrix (`wiring.live_overlay`)

- `scripts/gen_autonomy_matrix.py` `_live_overlay_rows` (`:303-338`): add a
  third probe,
  `{"system_gates": ["human-approval"], "satisfied_gates": ["human-approval"]}`,
  whose generated disposition must read `HOLD_BLOCKING`.
- `docs/compliance/control-objectives.toml:75`: reword the objective to: "On
  the live merge path a high risk or production deployment holds the merge for
  a human regardless of the path-floor flag; a required `human-approval` gate
  clears only on a GitHub approval of the head commit by someone other than
  the PR author or a bot; an unreadable contract holds it too."
- Regenerate `docs/docs/compliance/autonomy-matrix.md` and
  `docs/static/compliance/autonomy-matrix.json` with the generator (#1962).
  Do not hand-edit them.

## Alternatives rejected

- **Drop the evidence sources and stop there.** No gate is ever satisfiable,
  every `human-approval` task holds, and the human merges by hand. This is
  smaller, but it breaks the approved outcome: "a real human approval,
  recorded somewhere the merge path reads, clears it."
- **Evaluate everything once, before the PR, using PR reviews.** The PR does
  not exist yet (`merge_disposition` runs at `:1300`, before `create_pr`), so
  there is nothing to read.
- **Re-run the whole `merge_disposition` with real evidence at merge time.**
  This gives the same answer as step 2 plus the head check, but it re-reads the
  contract and metadata on every poll. The pre-PR split already shows that
  `human-approval` is the only blocker, and #1667 owns trusting the contract
  at merge time.
- **Reuse `read_review_verdict`.** Its jq projection drops `user.type`,
  `commit_id` and the PR author, and its callers depend on the `ReviewState`
  shape. Widening it would mix up the Copilot gate and the human gate.
- **Configure the bot login (env or `gh api /user`).** The PR author is
  already the bot identity, so a setting adds another thing to get wrong and
  answers no new question.
- **Accept approvals that are newer than the head commit by timestamp.**
  Reviews carry `submitted_at`, not "seen at", so a timestamp cannot show that
  the human saw the code being merged. Matching the commit can.
- **Rely on GitHub's "dismiss stale approvals" branch protection.** It is a
  repository setting AIFactory does not own, and it is off by default.
- **A UI/API approval action.** Deferred by intent answer 2.

## Risks

- **Every `human-approval` task held, by design or by accident.** If the
  reviews are never read (gh broken, scope missing, wrong repo), every such
  task times out to a human-stop and a human merges it. This fails safe but
  is noisy. It is also today's behaviour for any contract that does not
  self-declare.
- **Solo maintainer.** If gh is authenticated as the only human maintainer,
  that person is the PR author and can never satisfy the gate. GitHub blocks
  self-approval anyway. Every such task then holds for a manual merge.
- **20-minute watch window.** A human who approves after the watcher has
  timed out gets no auto-merge, and merges by hand instead. Extending the
  window is out of scope.
- **Path floor in enforcing mode.** With `AIFACTORY_PATH_RISK_FLOOR` on, any
  declared system gate floors the tier to `blocking` (step 1). That makes
  `decide_merge` hold on the tier, and step 2's hypothetical call cannot clear
  it. In enforcing mode a `human-approval` task therefore always becomes a
  manual merge. This is never looser, but the approval path only works in
  advisory mode.
- **Behind-base PRs.** With `match_head`, `merge_pr` no longer runs
  `update-branch` and retries. A human-approved PR that has fallen behind base
  is handed back for a human merge instead of being auto-updated.
- **Contracts that relied on `satisfied_gates`.** These now hold. That is the
  fix, but PFactory plans that set the field lose their auto-merge. The field
  becomes inert: it is still signed and still parsed, and has no effect.
- **Paginated reviews.** Without `--paginate`, a PR with more than 30 reviews
  could hide a later `CHANGES_REQUESTED`. The flag is required.

## Verification

Write the failing tests first, against the current code, in
`apps/web-server/tests/test_pr_endgame_merge_gate.py`:

1. A contract with `system_gates: ["human-approval"]` and
   `satisfied_gates: ["human-approval"]` makes
   `merge_disposition(...) == HOLD_BLOCKING`. **Fails today.**
2. `satisfiedSystemGates: ["human-approval"]` in `task_metadata.json` with
   the same contract also holds. **Fails today.**
3. Replace `test_satisfied_gates_reads_both_the_contract_and_the_metadata`
   and `test_satisfied_gates_of_an_unreadable_spec_is_empty` (`:241-250`),
   since the function they test is deleted. Rewrite
   `test_a_satisfied_system_gate_clears_only_its_own_hold` and
   `test_a_satisfied_gate_never_clears_production` (`:302-330`) to pass
   `satisfied_gates=` explicitly.

Add new tests for `human_approval_head`, each with a fake `runner`:

4. An approval by a non-author User on the head commit returns the SHA.
5. These all return `None`: an approval by the PR author; one by a
   `type: "Bot"` reviewer; one on an older `commit_id`; an approval followed
   by `CHANGES_REQUESTED` from the same login; an approval followed by
   `DISMISSED`.
6. A non-zero exit or junk JSON from either call returns `None`.

Add new tests for the watcher (`background=False`, fake runner):

7. `human_approval_required` with no qualifying approval never calls
   `gh pr merge` and ends in a human-stop.
8. With a qualifying approval, `gh pr merge` is called with
   `--match-head-commit <sha>`.
9. With `match_head`, a "behind" failure does not call `update-branch`.
10. A production contract keeps `auto_merge` off. Step 2's hypothetical call
    does not clear it.

Then:

- `python scripts/gen_autonomy_matrix.py --check`
  passes after regenerating, and the new probe row reads
  `HOLD_BLOCKING`.
- `pytest apps/web-server/tests/test_pr_endgame*.py apps/backend/merge` is
  green.

## Decisions (approved by olafkfreund, 2026-10-08)

- A. Accepted: with the path floor enforcing, a declared system gate floors the tier to `blocking`, so those tasks merge by hand. The floor is not loosened.
- B. Accepted: AIFactory's gh identity is `olafkfreund` (a User, verified in the pod), so no approval can count and human-approval tasks merge by hand until AIFactory has its own bot identity (follow-up issue). Update (#1671): the PR author is now a GitHub App (type Bot); decision B (do not configure the bot login) stands.
- C. Accepted: an approval after the 20-minute watch window gets no auto-merge; extending the window is out of scope.
- D. Approved 2026-10-09, after review: any non-author reviewer's standing `CHANGES_REQUESTED` (their latest decisive review) blocks the gate, even when another reviewer approved the head commit, as on GitHub.
