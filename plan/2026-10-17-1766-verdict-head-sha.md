---
status: draft
issue: 1766
spec: spec/2026-10-17-1766-verdict-head-sha.md
---

# Plan: bind the TFactory verdict to the head SHA, bound the watcher in time, require the branch's required checks

## Approved decisions (from the spec)

- All three parts of #1766 now (user, 2026-10-17). Fail closed throughout.
- Part 1 (1a): TFactory's task GET returns `source_sha` (TFactory PR, merged
  and deployed first). AIFactory `tfactory_verdict` takes a required
  keyword `head` and returns `None` unless `task["source_sha"] == head` and
  `head` is non-empty. Checked with the first response, before verdicts are
  read, so a stale reject is `None` too.
- Part 2: `watch_and_finish` breaks at the top of the loop once
  `time.monotonic()` passes `start + max_minutes*60`. The poll count stays as
  a second bound.
- Part 3 (3a): new `required_checks_green(owner, repo, base, sha, *, runner)`
  reading classic protection (`branches/{base}` `.protection`) and rulesets
  (`rules/branches/{base}`). Unreadable or null protection: `None` plus one
  warning naming the reason. No contexts: `True`. Else each required context
  on the head: red `False`, missing/pending `None`, all green `True`. The gate
  calls it only when `host_ci_green` is `True`. `_build_merge_gate` gains
  `base`.
- Docs: a short "Merge gate" section in `guides/pr-endgame.md`. No live
  branch protection change.

## Steps

0. TFactory (own PR on TFactory `dev`, done first):
   `apps/web-server/server/routes/tfactory_tasks.py:281-289` `get_task` adds
   `"source_sha": (_read_json(spec_dir / "context" / "source.json") or {}).get("source_sha") or None`;
   test in `apps/web-server/tests/test_task_source_sha.py`.
   → verify by `cd apps/web-server && python -m pytest tests/test_task_source_sha.py -q`
   Traps: TFactory has no intent/spec/plan folders; its PR links this plan.

1. `apps/backend/pfactory/tfactory_client.py:901-930`: signature
   `tfactory_verdict(spec_dir, spec_id, *, head: str, getter=None)`; docstring
   names #1766; add `and bool(head) and task.get("source_sha") == head` to the
   first `if not (...)` block. `apps/backend/pfactory/test_tfactory_verdict.py`:
   `_task()` gains `sha="abc"` put in the payload as `source_sha` (pass
   `sha=None` to omit it); `_run()` passes `head="abc"` (overridable); add
   tests: mismatch → None, missing → None, empty head → None, mismatching
   reject → None.
   → verify by `cd apps/backend && python -m pytest pfactory/test_tfactory_verdict.py -q`
   Traps: other callers: only `pr_endgame.py:1687` (step 3 updates it).

2. `apps/web-server/server/services/pr_endgame.py`: `import time`; at
   `:1293` add `deadline = time.monotonic() + max_minutes * 60`; first line in
   the `for` body: `if time.monotonic() >= deadline: break` (comment: #1766 a
   slow gate must not stretch the stop past max_minutes). Test in
   `apps/web-server/tests/test_pr_endgame.py`: monkeypatch
   `pe.time.monotonic` with a clock that advances 600 s per call; pending
   reviews, `max_minutes=20`, `poll_interval=0` → result reason is a timeout
   and the review was read fewer than 5 times.
   → verify by `cd apps/web-server && python -m pytest tests/test_pr_endgame.py -q`
   Traps: `poll_interval=0` in existing tests; keep the `polls` bound.

3. `pr_endgame.py`: add `required_checks_green` after `host_ci_green`
   (`:1030`) as the spec describes, same `try/except Exception -> None`
   style. `_build_merge_gate` (`:1655`) gains a `base: str` parameter after
   `runner`; `fetch` passes `head=head` to `tfactory_verdict` and does
   `ci = host_ci_green(...)`, then `if ci is True: ci = required_checks_green(owner, name, base, head, runner=runner)`.
   `run_pr_endgame` (`:1811`) passes `base`. Tests:
   `test_pr_endgame.py` parametrized table for `required_checks_green`
   (FakeRunner routes: put `rules/branches` before `branches/`, since
   matching is by substring in insertion order);
   `test_trusted_contract_merge.py` `_gate` helper and
   `test_1695_gate_is_set_when_resolve_contract_raises` pass `"main"` as
   base, monkeypatch `pe.required_checks_green`, assert the verdict got
   `head` and the required check got `base`, and that a `None` from the
   required checks makes the gate `False`.
   → verify by `cd apps/web-server && python -m pytest tests/test_pr_endgame.py tests/test_trusted_contract_merge.py tests/test_pr_endgame_merge_gate.py -q`
   Traps: check-run names and status contexts both count as contexts.

4. `guides/pr-endgame.md`: after the Copilot section (`:64`), a
   `### Merge gate: head-bound verdict, deadline, required checks` section
   (5-8 lines) per the spec's Docs paragraph.
   → verify by reading it.

## Tests

From `apps/backend`: `python -m pytest pfactory/test_tfactory_verdict.py -q`.
From `apps/web-server`: `python -m pytest tests/test_pr_endgame.py
tests/test_trusted_contract_merge.py tests/test_pr_endgame_merge_gate.py -q`.
`ruff check` and `ruff format --check` on touched files. All pass.

## Rollback

Revert the AIFactory PR. The TFactory field is additive and can stay.
