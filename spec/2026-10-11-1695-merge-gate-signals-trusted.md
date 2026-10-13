---
status: approved
issue: 1695
intent: intent/2026-10-11-1695-merge-gate-signals-trusted.md
---

# Spec: merge-gate signals come from a trusted source

## Design

### What the approver decided (intent, 2026-10-11)

1. Scope: verified trusted tasks only (`resolve_contract` returns
   `"verified"`). Legacy and from-issue tasks keep today's behaviour (#1697).
2. For those tasks, hold the merge when no trusted result exists (fail closed).
3. Sources now: TFactory for the verdict, GitHub checks for host-CI. VAL,
   floor and parity stay on metadata and become follow-ups.
4. Metadata values stay, as inputs that can only tighten the gate.
5. AIFactory uses only what TFactory exposes today. No TFactory change.

### The timing fact that shapes the design

`run_pr_endgame` decides the disposition before the PR exists
(`apps/web-server/server/services/pr_endgame.py:1483-1507`). At that moment
neither trusted signal exists yet:

- GitHub has not run any checks, because there is no PR.
- The TFactory handoff was sent a few lines earlier in
  `completion_orchestration.py:235-264` (`maybe_auto_handoff_tfactory`), so
  verification has only just started.

If the PR-open check failed closed on these signals, every verified low-tier
task would hold, and auto-merge would be off. So the trusted signals are
fetched **at merge time**, inside `watch_and_finish`, just before `merge_pr`
(`pr_endgame.py:1174-1183`). The PR-open decision stays as it is. It only
decides whether to arm the watcher, and it can still hold on anything it
already sees: the tier, the deployment overlay, a handback receipt, or a
recorded negative. No merge happens without passing the merge-time check.

Both routes still meet in one function. `merge_gate_signals` is still the only
place that builds the signal dict, and both `merge_disposition` and
`_clears_with_human_approval` reach it. What changes is that the merge-time
call passes it fetched values.

### Change 1: `merge_gate_signals` takes fetched values (`pr_endgame.py:400-451`)

New keyword argument `fetched: Mapping[str, object] | None = None`.

- `fetched is None`: today's behaviour, unchanged. The PR-open call, legacy
  tasks and `scripts/gen_autonomy_matrix.py:408` all take this path, so the
  autonomy matrix output does not change.
- `fetched` given (verified task, merge time):
  - `tfactory_verdict` starts from `fetched["tfactory_verdict"]`; a missing
    key gives `None`, which `decide_merge` treats as not a pass
    (`merge_policy._verdict_pass`, `apps/backend/merge/merge_policy.py:130`).
  - `host_ci_green` is `fetched["host_ci_green"] is True`; anything else is
    `False`.
  - Metadata can only tighten (decision 4). A recorded verdict replaces the
    fetched one only when it is not a pass (`_verdict_pass(recorded)` is
    false). A recorded `hostCiGreen: false` sets `False`. A recorded `true`
    and a recorded `"pass"` change nothing.
  - The handback receipt still forces `"handback"` (`:448-449`, left as is).
  - `achieved_val`, `val_floor` and `ci_parity` keep today's metadata reads
    (decision 3). This leaves the gap that the agent can delete them; that
    gap is for the follow-up issues.

`_verdict_pass` comes from the same lazy `merge.merge_policy` import that
`merge_disposition` already does (`:507`). We do not copy the pass set,
because a second copy of it would drift.

### Change 2: `merge_disposition` passes the fetch only for verified tasks (`:471-577`)

New keyword argument `fetch_signals: Callable[[], Mapping[str, object]] | None = None`.
It is called only inside the existing `if state == "verified":` branch
(`:539`), and its result goes to `merge_gate_signals(spec_dir, fetched=...)`
at `:573`. For legacy tasks it is never called, so no network calls are made
and the decision does not change (decision 1). The fetchers below never
raise. If they did, the existing `except Exception` (`:563-566`) would hold
the merge.

`_clears_with_human_approval` (`:889-902`) gets the same keyword argument
and passes it through, so the human-approval route is decided on the same
signals.

### Change 3: two fetchers that never raise

**Host-CI, `host_ci_green(owner, repo, sha, *, runner) -> bool | None`**, new
in `pr_endgame.py`, next to `human_approval_head`. It goes through the
injected `runner` (the default is `_default_runner`, `:68`, which wraps `gh`
in `github_env(child_env())`) and makes two calls:

- `gh api --paginate repos/{o}/{r}/commits/{sha}/check-runs --jq '.check_runs[] | {status, conclusion}'`
- `gh api repos/{o}/{r}/commits/{sha}/status --jq '{state, total_count}'`

It returns:

- `False` if any check run has completed with a conclusion outside
  `{success, neutral, skipped}`, or if the combined status is `failure` or
  `error`.
- `True` if at least one check run or status exists, and every one has
  completed green.
- `None` otherwise: pending, no checks at all, a non-zero rc, unparseable
  output, or any exception (`_default_runner`'s 120 s timeout raises
  `TimeoutExpired`).

"No checks at all" is not green. That follows decision 2.

**TFactory, `tfactory_verdict(spec_dir, spec_id, *, getter=None) -> str | None`**,
new in `apps/backend/pfactory/tfactory_client.py`, next to `send_pr_attach`
(`:743`), and added to `__all__`. It reuses `tfactory_config()` (`:55`) for
the base URL and bearer token, and the same project-id rule as
`send_pr_attach` (`TFACTORY_PROJECT_ID`, else `_aifactory_project_name`). It
is synchronous, because it runs in the watcher's worker thread. The default
getter is `httpx.get(..., timeout=10.0)`. It makes two GETs on TFactory's
existing read-only routes (TFactory `apps/web-server/server/routes/tfactory_tasks.py`,
mounted at `/api/tfactory/tasks`):

1. `GET /api/tfactory/tasks/{spec_id}` (spec id URL-quoted). The response's
   `project_id` must equal the expected project. TFactory's `_find_spec_dir`
   returns the first spec with that id across all projects, so a mismatch
   means it may be another project's run, and the function returns `None`.
   `status_json.status` must be `"triaged"`. Any other status means the run
   is unfinished, failed or empty, and the function returns `None`.
2. `GET /api/tfactory/tasks/{spec_id}/verdicts.json`. If any entry in
   `verdicts` has verdict `"reject"`, it returns `"fail"`. If the list is
   non-empty with no `"reject"`, it returns `"pass"`. This is the same rule
   TFactory's own handback loop uses (`agents/handback/loop.py`,
   `_FAILING_VERDICTS = {"reject"}`; `"flag"` does not fail).

`None` is the result in every other case: `TFACTORY_BASE_URL` unset, a
non-2xx response, a timeout, a malformed body, or an empty list.

### Change 4: the merge-time check in `watch_and_finish` (`:1064-1260`)

New keyword argument `merge_gate: Callable[[str], bool] | None = None`. When
it is set, on an approval with `auto_merge` on, after `match_head` is
resolved (`:1174-1180`):

- `head = match_head`, or in non-human mode the PR head from a small
  `_pr_head(owner, repo, pr, runner)` (`gh api .../pulls/{pr} --jq .head.sha`,
  which never raises; `None` means keep polling).
- If `fix_cycles or conflict_cycles` is non-zero, the endgame has pushed a
  commit that TFactory did not verify. This holds: it returns
  `reason: "merge_gate_unverified_head"` and does not merge. See Open
  question 1.
- `if not await asyncio.to_thread(merge_gate, head): continue`. A gate that
  is red, pending or unreachable keeps polling, and the existing timeout
  (`_MAX_POLL_MINUTES`, `:51`) is the human-stop. It logs one constant line
  per poll (no fetched value goes in the log, `py/log-injection`).
- `match_head = head`, so `merge_pr` (`:1181-1183`) merges only the commit
  whose checks were read (`--match-head-commit`). In this mode, as in the
  #1663 human mode, `merge_pr` does not run `update-branch`. A PR that is
  behind base goes to the existing `conflict_fixer` path or to a human.

`merge_gate=None` (the default, and every existing test) leaves the watcher
byte-for-byte as it is today.

### Change 5: wiring in `run_pr_endgame` (`:1414-1585`)

When `auto_merge` is still on after the PR-open decision, build the gate as a
closure and pass it to `watch_and_finish` (`:1560-1574`):

```python
def _merge_gate(head: str) -> bool:
    fetch = lambda: {
        "tfactory_verdict": tfactory_verdict(spec_dir, spec_id),
        "host_ci_green": host_ci_green(owner, name, head, runner=runner),
    }
    if human_approval_required:
        return _clears_with_human_approval(spec_dir, review_tier, trusted, fetch_signals=fetch)
    return merge_disposition(spec_dir, review_tier, trusted=trusted, fetch_signals=fetch) == AUTO_MERGE_DISPOSITION
```

`tfactory_verdict` uses the same lazy `pfactory.tfactory_client` import the
web server already uses (`completion_orchestration.py:241`). If it fails
with `ImportError`, the verdict is `None`, which holds. The closure is set
for every task. For legacy tasks `merge_disposition` never calls `fetch`, so
the merge-time check just re-runs today's decision. That decision can only
agree with the PR-open one or be stricter.

### Files touched

- `apps/web-server/server/services/pr_endgame.py`: changes 1, 2, 4, 5, plus
  `host_ci_green` and `_pr_head`.
- `apps/backend/pfactory/tfactory_client.py`: `tfactory_verdict`.
- Tests (see Verification).

Nothing else changes: `merge_policy.decide_merge` (its keyword interface is
kept), `trusted_contract.py` (#1751 is merged, d3aabb16, but no change is
needed), the #1672 signed-tier block, `host_isolated` (#1667), the handback
receipt handling (#1696) and TFactory.

## Alternatives rejected

- **Fetch at PR-open, inside the existing `merge_disposition` call.** Neither
  signal exists yet at that point, so failing closed would hold every
  verified low-tier task.
- **Rely on branch protection's required checks instead of reading host-CI.**
  Not every repo has required checks, and an admin merge can bypass them
  (autonomy matrix row for `gate.server.services.pr_endgame`). It is not a
  value AIFactory has measured.
- **`gh pr checks`.** It reports the PR's current head, not a given SHA, so
  the checks we read could belong to a different commit from the one we
  merge. Reading by SHA and merging with `--match-head-commit` closes that
  gap.
- **Drop the metadata reads.** Rejected by decision 4. Recorded negatives
  keep holding the merge.
- **Use `/api/tfactory/tasks/{spec_id}/verdicts.json` alone.** It is not
  scoped to a project, and it does not say whether the run has finished. The
  status call adds both checks without any TFactory change.
- **Bind the verdict to the head SHA.** TFactory exposes no verified-commit
  SHA today, and decision 5 rules out changing TFactory. The rule that an
  endgame-pushed head holds covers the case we know of (Open question 1).
- **Stop at the first red signal instead of polling until the timeout.** It
  would need a three-state gate. Polling a red result for up to 20 minutes
  is harmless, and either way the PR stays open for a human.

## Risks

- **Verified low-tier auto-merge needs TFactory and CI to finish within the
  20-minute review window.** Otherwise it times out to a human-stop. This
  applies on every isolated host that runs the auto-PR endgame with
  `AIFACTORY_AUTO_MERGE` on. If the window turns out too short, raising
  `_MAX_POLL_MINUTES` is a separate change.
- **A host with no `TFACTORY_BASE_URL`, or a repo with no CI,** never
  auto-merges a verified task again. This is intended by decision 2, but it
  is a visible behaviour change for operators. It needs a CHANGELOG line.
- **Spec-id collision across TFactory projects.** The `project_id` check
  turns this into a hold. It can never turn into a false pass.
- **A verdict about an older commit.** Before the PR opened, the build
  branch could only be changed by the build, and TFactory verified that
  branch. After the PR opened, the endgame's own pushes (fix or conflict
  cycles) are held. A manual push to the PR branch by someone with write
  access is not detected. That is a human acting, and it is outside the
  agent threat model.
- **The behind-base PR no longer gets an automatic `update-branch`** on the
  verified path, because the merge is bound to the head whose checks were
  read. It goes to `conflict_fixer`, and that push then holds under the rule
  above. So in practice a verified PR that is behind base needs a human.
- **API cost:** after approval, up to 3 `gh` calls and 2 HTTP GETs per
  30-second poll, for at most 40 polls. This is negligible.
- **Legacy and from-issue tasks** are unchanged. Their signals stay
  agent-writable until #1697.

## Verification

Unit tests, all with injected runners and getters and no network:

- `apps/web-server/tests/test_pr_endgame_merge_gate.py`:
  - `merge_gate_signals(spec, fetched=...)`: a fetched pass with a recorded
    `"handback"` or `"fail"` holds. A fetched `"fail"` with a recorded
    `"pass"` holds. A fetched `host_ci_green=None`, or a missing key, gives
    `False`. A recorded `hostCiGreen: false` beats a fetched `True`.
  - `merge_gate_signals(spec)` with no `fetched` returns exactly today's dict
    (this is what the autonomy matrix depends on).
- `apps/web-server/tests/test_trusted_contract_merge.py`, using the existing
  `_rec(signed)` fixture for verified tasks:
  - a verified task with fetched `{"pass", True}` → `auto-merge`
  - with fetched `None` on either signal → hold
  - a legacy task: `fetch_signals` is never called (a fetch that raises
    proves it), and the result is unchanged
  - `_clears_with_human_approval` with a red fetch → `False`
- `apps/web-server/tests/test_pr_endgame.py` (`watch_and_finish`):
  - `merge_gate` returns False → no `gh pr merge` in the runner log, and the
    run ends with `review_timeout`
  - `merge_gate` returns True → merges with `--match-head-commit <head>`
  - a fix cycle before approval → `merge_gate_unverified_head`, no merge
  - `merge_gate=None` → the existing tests pass unchanged
- `host_ci_green` tests: all checks green → True. One failure → False. A run
  still in progress → None. Zero runs and zero statuses → None. rc≠0 → None.
  A runner that raises `TimeoutExpired` → None.
- New `apps/backend/pfactory/test_tfactory_verdict.py`:
  - triaged with no reject → `"pass"`
  - one reject → `"fail"`
  - project mismatch, status `running`, status `triaged_empty`, a 404, a
    getter that raises, an unset base URL and an empty list → each `None`

Commands:

```bash
apps/backend/.venv/bin/pytest apps/web-server/tests/test_pr_endgame_merge_gate.py \
  apps/web-server/tests/test_trusted_contract_merge.py \
  apps/web-server/tests/test_pr_endgame.py \
  apps/web-server/tests/test_merge_decision_reaches_the_merge.py \
  apps/backend/pfactory/test_tfactory_verdict.py -v
apps/backend/.venv/bin/python scripts/gen_autonomy_matrix.py --check   # unchanged output
ruff check apps/web-server/server/services/pr_endgame.py apps/backend/pfactory/tfactory_client.py
```

Re-run the #1672 probe against the new code. Editing or deleting
`tfactoryVerdict` or `hostCiGreen` in `task_metadata.json` must not turn a
fetched red or `None` into `auto-merge` for a verified task. CodeQL on the
PR must report no new `py/log-injection`.

## Open questions

1. **Head pushed by the endgame after TFactory verified.** A fix or conflict
   cycle pushes a commit that no trusted verdict covers. This spec holds it
   (`merge_gate_unverified_head`), following decision 2. The alternative is
   to accept the spec-level TFactory verdict for these commits too, which
   gives up the binding but keeps auto-merge after an auto-fix. Confirm that
   it should hold.
2. **`"flag"` verdicts.** This spec follows TFactory's own handback rule:
   only `"reject"` fails, and `"flag"` does not block. The stricter option is
   to treat any `"flag"` as not a pass. Confirm.

## Approver decisions (2026-10-13)

1. Hold the merge after an endgame-pushed fix or conflict commit (fail closed); no verdict covers that commit.
2. A TFactory `"flag"` verdict does not block; only `"reject"` fails, following TFactory's own handback rule.
