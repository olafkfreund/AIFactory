---
status: draft
issue: 1766
intent: intent/2026-10-17-1766-verdict-head-sha.md
---

# Spec: bind the TFactory verdict to the head SHA, bound the watcher in time, require the branch's required checks

## Decisions (user, 2026-10-17)

- Do **all three parts** of #1766 now (the decision brief recommended
  deferring part 3; the user chose to include it).
- Part 1 is option **1a**: TFactory returns `source_sha` on its task GET;
  AIFactory passes only when it equals the PR head. A missing `source_sha`
  means not passed (fail closed). TFactory ships first, as its own PR on
  TFactory `dev`; this PR is ordered after it.
- Part 2: the watcher ends on a `time.monotonic()` deadline.
- Part 3 is option **3a**: when the token can read a branch's required
  checks, check them before merging; when it cannot, fail closed with a
  clear reason. No change to live branch protection; code and docs only.

## Design

**1. Verdict bound to the head.**
TFactory (separate PR): `get_task` in
`apps/web-server/server/routes/tfactory_tasks.py:266-289` adds
`"source_sha": <context/source.json source_sha, or None>` to the response.
The value is the commit `_add_spec_worktree` checked out and verified
(TFactory `task_control.py:487-535`); a rerun re-pins it.

AIFactory: `tfactory_verdict(spec_dir, spec_id, *, head, getter=None)` in
`apps/backend/pfactory/tfactory_client.py:901`. `head` is a required
keyword, so no caller can forget it. The existing first-response check
(`:922-929`) gains `head and task.get("source_sha") == head`. On a mismatch
or a missing field the function returns `None` before reading verdicts, so
neither "pass" nor "fail" is reported for a commit TFactory did not test.
The gate (`apps/web-server/server/services/pr_endgame.py:1687`) passes
`head=head`.

**2. Deadline.** In `watch_and_finish` (`pr_endgame.py:1291-1295`) set
`deadline = time.monotonic() + max_minutes * 60` before the loop and break
at the top of each iteration once it has passed. The existing poll count
stays as a second upper bound: tests with a zero-second fake sleep still
terminate, and real runs now stop on whichever comes first. A poll already
in progress finishes, so the overshoot is at most one poll. The timeout
return (`:1460-1469`) is unchanged.

**3. Required checks.** New `required_checks_green(owner, repo, base, sha,
*, runner) -> bool | None` next to `host_ci_green` (`pr_endgame.py:969`):

- Reads the base branch's required contexts from both sources an
  installation token with read access can see:
  `gh api repos/{o}/{r}/branches/{base} --jq .protection` (classic
  protection: `required_status_checks.contexts`) and
  `gh api --paginate repos/{o}/{r}/rules/branches/{base}` (rulesets: rules of
  type `required_status_checks`, `parameters.required_status_checks[].context`).
  Probed read-only on this repo's `dev`: rules returns `[]`, the branch
  returns `protection.enabled` and `required_status_checks.contexts`.
- Either call failing, or `protection` missing/null (cannot tell whether the
  branch is protected), returns `None` and logs one warning:
  `cannot read the required checks of <base>; holding the merge (#1766)`.
- No required contexts: `True` (nothing beyond `host_ci_green` to check).
- Otherwise reads the head's check runs (`commits/{sha}/check-runs`, name,
  status, conclusion) and combined status (`commits/{sha}/status`,
  `.statuses[]` context and state). A required context that is red
  (completed with a conclusion outside success/neutral/skipped, or a status
  of failure/error) gives `False`; one missing or not finished gives `None`
  (keep waiting); all green gives `True`.

`_build_merge_gate` gets a new `base` argument (passed from
`run_pr_endgame`'s `base`, `:1811`). Its `fetch` computes
`ci = host_ci_green(...)` and, only when that is `True`, replaces it with
`required_checks_green(...)`. So red stays red, unknown stays unknown, and
the extra calls run only on an otherwise green head. The gate's consumers
(`merge_disposition`, `_clears_with_human_approval`) already treat anything
but `True` as not green.

**Docs.** `guides/pr-endgame.md` gets a short "Merge gate" note: the gate
needs TFactory's tested SHA to equal the head, ends at a wall-clock deadline,
checks the base branch's required checks and holds if it cannot read them;
and the recommended GitHub setting is to list the CI checks (and TFactory's
check, where it posts one) as required checks in branch protection or a
ruleset, so GitHub enforces the same rule for every merge path.

## Alternatives rejected

- 1b, fetch `context/source.json` as a TFactory artefact: needs a new route
  anyway; no gain over one field.
- 1c, compare to the SHA AIFactory sent: proves what was sent, not tested.
- Deadline only, dropping the poll count: tests that use a zero-second fake
  sleep would spin on the real clock for `max_minutes`; keeping both bounds
  needs no test fake clock beyond the one new test.
- `GET .../branches/{b}/protection`: needs admin, which the App lacks.
- Folding the required-check reads into `host_ci_green`: changes its
  signature and every existing test fixture; a separate function keeps
  `host_ci_green` untouched.
- Surfacing the "cannot read" reason as a new endgame `reason` value: the
  gate returns a bool, so that needs plumbing through `_gated_head`; the
  warning log plus the existing `merge_gate_timeout` reason is enough.

## Risks

- Until TFactory with `source_sha` is deployed, every gated auto-merge
  holds (`merge_gate_timeout`). Deploy TFactory first.
- Any head change after TFactory tested (update-branch, a human push) holds
  until TFactory re-tests. Endgame pushes already hold.
- Installation-token visibility of `protection` and the rules API is
  unverified live (probed only with a maintainer token). If the App cannot
  read them, gated merges hold with the logged reason: fail closed, not open.
- Two to four extra `gh api` calls per green gate evaluation; bounded by
  the new deadline.
- Legacy tasks have no merge gate (`pr_endgame.py:1676`) and are not
  affected (tracked separately, #1697).

## Verification

- TFactory: a route test where `get_task` returns `source_sha` from
  `context/source.json`, and `None` without it.
- AIFactory `apps/backend/pfactory/test_tfactory_verdict.py`: matching SHA
  passes, mismatching, missing or empty head gives `None`, and a mismatching
  reject also gives `None`.
- `apps/web-server/tests/test_pr_endgame.py`: `required_checks_green` table
  (none required, all green, one red, one missing, one pending, unreadable,
  null protection, ruleset-only context); a watcher test with a fake
  monotonic clock stops before the poll count runs out.
- `apps/web-server/tests/test_trusted_contract_merge.py`: the gate passes
  `head` to the verdict and `base` to the required checks.
- `ruff check`, `ruff format --check`, mypy on touched files.
