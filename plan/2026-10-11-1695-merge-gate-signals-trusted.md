---
status: approved
issue: 1695
spec: spec/2026-10-11-1695-merge-gate-signals-trusted.md
---

# Plan: merge-gate signals come from a trusted source

You can implement this plan without opening the intent or the spec. Line
numbers are for HEAD `af731850` on `fix/1695-merge-gate-signals-trusted`,
checked on 2026-10-13. Edit bottom-up inside `pr_endgame.py` (step 5 first,
step 1 last) or re-grep the anchor named in each step, because earlier edits
move later lines.

## Approved decisions

- **D1: scope is verified trusted tasks only.** A task is verified when
  `server.services.trusted_contract.resolve_contract(spec_dir, trusted)`
  returns state `"verified"`. Legacy and from-issue tasks keep today's
  behaviour; their signals stay agent-writable until #1697.
- **D2: fail closed.** For a verified task, no trusted result means no merge.
  "No checks at all" is not green. TFactory unconfigured or unreachable is not
  a pass.
- **D3: trusted sources now.** The TFactory verdict comes from TFactory's API.
  Host-CI comes from GitHub checks for the exact head SHA. `achieved_val`,
  `val_floor` and `ci_parity` stay on today's metadata reads (follow-ups).
- **D4: metadata can only tighten.** A recorded verdict replaces the fetched
  one only when the recorded one is not a pass. A recorded `hostCiGreen: false`
  sets `False`. A recorded `true` or `"pass"` changes nothing. The handback
  receipt still forces `"handback"`.
- **D5: no TFactory change.** Use only TFactory's existing read-only routes
  (mounted at `/api/tfactory/tasks`, bearer token on `/api/*`):
  - `GET /api/tfactory/tasks/{spec_id}` returns
    `{"task_id", "project_id", "spec_id", "status_json": {...status.json...}, "artefacts"}`.
    It finds the first spec with that id across ALL projects, so the
    `project_id` must be checked.
  - `GET /api/tfactory/tasks/{spec_id}/verdicts.json` returns the evaluator's
    file verbatim: `{"verdicts": [{"verdict": "accept"|"flag"|"reject", ...}]}`.
- **D6: the check runs at merge time, not at PR-open.** At PR-open neither
  signal exists (no PR means no checks; TFactory was only just handed the
  build). The PR-open decision in `run_pr_endgame` is unchanged and only
  decides whether to arm the watcher. The merge-time check runs in
  `watch_and_finish` just before `merge_pr`.
- **D7: one signal builder.** `merge_gate_signals` stays the only place that
  builds the signal dict. With no fetched values it returns exactly today's
  dict, so `scripts/gen_autonomy_matrix.py:408` and the matrix stay
  byte-identical.
- **D8: `_verdict_pass` is reused, not copied.** Import it from
  `merge.merge_policy` (`apps/backend/merge/merge_policy.py:130`); pass set is
  `{"pass", "passed", "green", "ok"}` or bool `True`.
- **D9: TFactory verdict rule (approver decision 2, 2026-10-13).** Only
  `"reject"` fails. `"flag"` does not block. This matches TFactory's own
  `agents/handback/loop.py` `_FAILING_VERDICTS = frozenset({"reject"})`.
  Status must be exactly `"triaged"`; `triaged_empty`, `running`,
  `triager_failed` or anything else is "no result".
- **D10: an endgame-pushed head holds (approver decision 1, 2026-10-13).** If
  the watcher has run a fix cycle or a conflict cycle, the head is a commit no
  verdict covers. It returns `reason: "merge_gate_unverified_head"` and does
  not merge.
- **D11: the merge is bound to the head whose checks were read.** `merge_pr`
  gets `match_head=<head>`, so it uses `--match-head-commit` and never runs
  `gh pr update-branch` (existing behaviour of `merge_pr` when `match_head` is
  set, `pr_endgame.py:936-939`). A behind-base PR goes to `conflict_fixer`,
  whose push then holds under D10.
- **D12: a red, pending or unreachable gate keeps polling.** The existing
  timeout (`_MAX_POLL_MINUTES = 20`, `:51`) is the human-stop. One constant log
  line per poll; no fetched value, SHA or verdict goes into a log
  (CodeQL `py/log-injection`).
- **D13: fetchers never raise.** Every error, non-zero rc, non-2xx, timeout or
  malformed body returns `None`.
- **D14: CHANGELOG.** An operator-visible line under `## [Unreleased]`: hosts
  without `TFACTORY_BASE_URL`, or repos without CI, never auto-merge verified
  tasks.
- **Unchanged:** `merge_policy.decide_merge` and its keyword interface,
  `trusted_contract.py`, the #1672 signed-tier block, `host_isolated` (#1667),
  the handback receipt handling (#1696), TFactory, `standards/`.

### Plan correction that needs approver confirmation (C1)

The spec (Change 5) says the merge gate closure "is set for every task". That
contradicts D1 in two ways, found while checking the code:

1. With a gate, the watcher binds the merge to the head (D11), so legacy tasks
   would lose today's automatic `gh pr update-branch` retry.
2. `apps/web-server/tests/test_pr_endgame.py::test_run_pr_endgame_full_chain`
   and `::test_run_pr_endgame_aifactory_reviewer_uses_engine_verdict` (legacy,
   `trusted=None`) would go red: their `FakeRunner` answers the new
   `gh api .../pulls/11 --jq .head.sha` with `""`, so the watcher would poll
   to timeout instead of merging. The spec requires those tests to pass
   unchanged.

This plan therefore passes `merge_gate` only when `resolve_contract` does not
say `"legacy"` (an exception counts as not legacy, so it fails closed). For a
verified task the behaviour is exactly the spec's. Legacy tasks get no
merge-time re-check; that re-check could only have re-read the same
agent-writable metadata, which is #1697's scope.

## Steps

Step 0 is setup. Steps 1-5 edit `pr_endgame.py`, step 6 edits
`tfactory_client.py`, steps 7-10 are tests and the changelog.

0. **Setup.** `cd /mnt/code/Source-home/GitHub/AIFactory-1695`. Set
   `V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin` (the venv
   lives in the main checkout, not the worktree). Baseline:
   `$V/pytest -q -o asyncio_mode=auto apps/web-server/tests/test_pr_endgame_merge_gate.py apps/web-server/tests/test_trusted_contract_merge.py apps/web-server/tests/test_pr_endgame.py apps/web-server/tests/test_merge_decision_reaches_the_merge.py`
   → `130 passed`.
   Traps: without `-o asyncio_mode=auto` 23 async tests fail on the baseline.
   This is not a regression; CI passes the same flag.

1. **`apps/web-server/server/services/pr_endgame.py:400-451`
   (`merge_gate_signals`): add fetched values (D4, D7, D8).**
   - Signature: `def merge_gate_signals(spec_dir: Path, *, fetched: Mapping[str, object] | None = None) -> dict[str, object]:`.
   - Line 26: `from collections.abc import Callable, Iterable` becomes
     `from collections.abc import Callable, Iterable, Mapping`.
   - Right after the `signals` dict literal (`:420-430`) and before the first
     handback check (`:432`), add:
     ```python
     if fetched is not None:
         from merge.merge_policy import _verdict_pass  # noqa: PLC0415

         signals["tfactory_verdict"] = fetched.get("tfactory_verdict")
         signals["host_ci_green"] = fetched.get("host_ci_green") is True
     ```
     The early `return signals` at `:437` and `:439` (unreadable or non-dict
     metadata) then return the fetched values, which is correct.
   - In the metadata loop (`:440-447`), when `fetched is not None`:
     - `tfactory_verdict`: assign `recorded` only if
       `recorded is not None and not _verdict_pass(recorded)`.
     - `host_ci_green`: assign only if `recorded is False`.
     - `achieved_val`, `val_floor`, `ci_parity`: unchanged.
     When `fetched is None`, the loop is exactly today's.
   - Leave the handback checks (`:432-433`, `:448-450`) as they are.
   - Add one docstring paragraph: with `fetched`, the verdict and host-CI
     start from trusted values and metadata can only tighten them.
   → verify by `$V/python scripts/gen_autonomy_matrix.py --check` (prints
   `ok: ...`, exit 0) and the step 0 pytest command (130 passed).
   Traps: the `merge.merge_policy` import must stay lazy (the module imports
   it lazily everywhere so the web server works without the backend on the
   path). If strict ruff flags the private import (`PLC2701`), add it to the
   `noqa`. Do not copy the pass set.

2. **`pr_endgame.py:471-577` (`merge_disposition`): pass the fetch only for
   verified tasks (D1, D6).**
   - Add the keyword `fetch_signals: Callable[[], Mapping[str, object]] | None = None`
     after `satisfied_gates` (`:476`).
   - Before the `try:` at `:530`, add `fetched: Mapping[str, object] | None = None`.
   - At the end of the `if state == "verified":` block (`:539-562`, after the
     `effective = raise_review_tier(...)` line), add
     `if fetch_signals is not None: fetched = fetch_signals()`. It stays inside
     the `try`, so a raising fetch falls into `except Exception` (`:563`) and
     holds.
   - `:573`: `**merge_gate_signals(spec_dir),` becomes
     `**merge_gate_signals(spec_dir, fetched=fetched),`.
   - Document the new argument in the docstring in one sentence.
   → verify by the step 0 pytest command (130 passed) and the matrix check.
   Traps: `fetch_signals` must never be called for `legacy` or `hold` (D1:
   no network for legacy tasks). Function now has 5 args; `max-args = 5` in
   `standards/ruff.toml`, so no new `PLR0913`.

3. **`pr_endgame.py:889-902` (`_clears_with_human_approval`): pass it
   through.**
   - Signature becomes
     `(spec_dir: Path, tier: str | None, trusted: object, *, fetch_signals: Callable[[], Mapping[str, object]] | None = None) -> bool`.
   - Pass `fetch_signals=fetch_signals` into the `merge_disposition` call at
     `:898-900`.
   → verify by the step 0 pytest command.
   Traps: none.

4. **`pr_endgame.py`, after `human_approval_head` ends (`:886`): two new
   fetchers that never raise (D2, D13).**
   - `_pr_head(owner: str, repo: str, pr: int, runner: Runner) -> str | None`:
     in a `try`, run
     `runner(["gh", "api", f"/repos/{owner}/{repo}/pulls/{pr}", "--jq", ".head.sha"], None)`;
     return `res.out.strip() or None` when `res.ok`, else `None`;
     `except Exception: return None` (`# noqa: BLE001 - fail closed`).
   - `_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})` at
     module level next to it.
   - `host_ci_green(owner: str, repo: str, sha: str, *, runner: Runner = _default_runner) -> bool | None`:
     - In one `try` (catch `Exception`, return `None`; this covers
       `subprocess.TimeoutExpired` from `_default_runner`'s 120 s timeout):
       - `runs_res = runner(["gh", "api", "--paginate", f"repos/{owner}/{repo}/commits/{sha}/check-runs", "--jq", ".check_runs[] | {status, conclusion}"], None)`
       - `st_res = runner(["gh", "api", f"repos/{owner}/{repo}/commits/{sha}/status", "--jq", "{state, total_count}"], None)`
       - If either is not `.ok`, return `None`.
       - `runs` = `json.loads(line)` for each non-blank line of `runs_res.out`;
         `status = json.loads(st_res.out)`; `state = status["state"]`,
         `total = int(status["total_count"])`.
     - Decide, in this order:
       1. `False` if any run has `status == "completed"` and
          `conclusion not in _GREEN_CONCLUSIONS`, or `state in ("failure", "error")`.
       2. `None` if any run has `status != "completed"`.
       3. `None` if `total > 0 and state != "success"` (statuses pending).
       4. `None` if `not runs and total == 0` (no CI at all is not green, D2).
       5. Otherwise `True`.
     - Parsing errors (`ValueError`, `KeyError`, `TypeError`,
       `AttributeError`) also return `None`; the outer `except Exception`
       covers them.
     - No logging of the SHA or any fetched value.
   → verify by the step 9 `host_ci_green` tests.
   Traps: read by SHA, never `gh pr checks` (that reads the current head, not
   the head we merge). GitHub's combined status is `"pending"` with
   `total_count: 0` when a repo has no statuses; rule 3 only applies when
   `total > 0`.

5. **`pr_endgame.py:1064-1260` (`watch_and_finish`): the merge-time check
   (D6, D10, D11, D12).**
   - Add the keyword `merge_gate: Callable[[str], bool] | None = None` after
     `human_approval_required` (`:1082`). One docstring sentence: when set, the
     merge waits until `merge_gate(head)` is true and merges only that head;
     an endgame-pushed head holds.
   - Between the human-mode block (`:1174-1180`, ends with `continue  # no
     human approval of the head commit yet`) and `merged = await
     asyncio.to_thread(` (`:1181`), insert:
     ```python
     if merge_gate is not None:
         if fix_cycles or conflict_cycles:
             logger.info(
                 "[pr-endgame] pr=%d head was pushed by the endgame and no "
                 "verdict covers it; handing to a human",
                 pr,
             )
             return {
                 "pr": pr,
                 "verdict": "approved",
                 "merged": False,
                 "reason": "merge_gate_unverified_head",
                 "fix_cycles": fix_cycles,
                 "conflict_cycles": conflict_cycles,
             }
         head = match_head or await asyncio.to_thread(
             _pr_head, owner, repo, pr, runner
         )
         if head is None or not await asyncio.to_thread(merge_gate, head):
             logger.info("[pr-endgame] pr=%d merge gate not green yet", pr)
             continue
         match_head = head
     ```
   - Nothing else in the function changes. With `merge_gate=None` the watcher
     is byte-for-byte today's.
   → verify by the step 0 pytest command plus
   `$V/pytest -q -o asyncio_mode=auto tests/test_pr_endgame_conflict_loop.py`
   (all pass, unchanged).
   Traps: `pr` is an `int`, so `%d` is log-safe; do not add `head` or the
   gate result to a log line (`py/log-injection`).

6. **`pr_endgame.py:1484-1574` (`run_pr_endgame`): build and pass the gate
   for non-legacy tasks (D1, D6, C1).**
   - After `owner, name = parts` (`:1512`), add:
     ```python
     merge_gate: Callable[[str], bool] | None = None
     if auto_merge:
         try:
             from server.services.trusted_contract import (  # noqa: PLC0415
                 resolve_contract,
             )

             state = resolve_contract(spec_dir, trusted)[0]
         except Exception:  # noqa: BLE001 - unknown is not legacy: fail closed
             state = "hold"
         if state != "legacy":

             def merge_gate(head: str) -> bool:
                 def fetch() -> dict[str, object]:
                     try:
                         from pfactory.tfactory_client import (  # noqa: PLC0415
                             tfactory_verdict,
                         )

                         verdict = tfactory_verdict(spec_dir, spec_id)
                     except ImportError:
                         verdict = None
                     return {
                         "tfactory_verdict": verdict,
                         "host_ci_green": host_ci_green(
                             owner, name, head, runner=runner
                         ),
                     }

                 if human_approval_required:
                     return _clears_with_human_approval(
                         spec_dir, review_tier, trusted, fetch_signals=fetch
                     )
                 return (
                     merge_disposition(
                         spec_dir, review_tier, trusted=trusted, fetch_signals=fetch
                     )
                     == AUTO_MERGE_DISPOSITION
                 )
     ```
     If mypy objects to redefining `merge_gate` as a function, name the inner
     function `_gate` and assign `merge_gate = _gate`.
   - In the `watch_and_finish(...)` call (`:1560-1574`), add
     `merge_gate=merge_gate,` after `human_approval_required=...` (`:1573`).
   → verify by the step 0 pytest command (130 passed, including the two
   legacy full-chain tests named in C1) and the matrix check.
   Traps: `auto_merge` here is the value AFTER the PR-open decision
   (`:1507` may have set it `False`); place the block after `:1512` so that
   holds. The lazy `pfactory` import mirrors
   `completion_orchestration.py:241`.

7. **`apps/backend/pfactory/tfactory_client.py`: `tfactory_verdict` (D5, D9,
   D13).**
   - Add `"tfactory_verdict",` to `__all__` (`:37-45`, keep it sorted: after
     `"tfactory_config"`).
   - Add `from urllib.parse import quote` to the imports (`:22-30`).
   - After `send_pr_attach` ends (`:782`), add:
     - `Getter = Callable[[str, dict[str, str]], tuple[int, object]]` (url,
       headers) -> (status code, parsed JSON body).
     - `_httpx_get(url: str, headers: dict[str, str]) -> tuple[int, object]`:
       lazy `import httpx`; `resp = httpx.get(url, headers=headers, timeout=10.0)`;
       return `(resp.status_code, resp.json() if resp.is_success else None)`.
     - `def tfactory_verdict(spec_dir: Path, spec_id: str, *, getter: Getter | None = None) -> str | None:`
       synchronous (it runs in the watcher's worker thread). Docstring: the
       verdict TFactory itself recorded for this spec, `"pass"`, `"fail"` or
       `None` (no trusted result); never raises.
       1. `config = tfactory_config()`; `base_url = config.get("base_url")`;
          no base URL → `None`.
       2. `project_id = os.environ.get("TFACTORY_PROJECT_ID") or _aifactory_project_name(Path(spec_dir))`;
          empty → `None`.
       3. Headers: `{"Authorization": f"Bearer {token}"}` when
          `config.get("token")`, else `{}`.
       4. `base = f"{base_url}/api/tfactory/tasks/{quote(spec_id, safe='')}"`.
       5. In a `try` / `except Exception: return None`
          (`# noqa: BLE001 - no trusted result is not a pass`):
          - `code, task = (getter or _httpx_get)(base, headers)`; require
            `200 <= code < 300`, `isinstance(task, dict)`,
            `task.get("project_id") == project_id`, and
            `isinstance(task.get("status_json"), dict)` with
            `task["status_json"].get("status") == "triaged"`. Else `None`.
          - `code, doc = getter(f"{base}/verdicts.json", headers)`; require
            2xx and `isinstance(doc, dict)` and
            `isinstance(doc.get("verdicts"), list)`. Else `None`.
          - `entries = [e for e in doc["verdicts"] if isinstance(e, dict)]`;
            empty → `None`; any `e.get("verdict") == "reject"` → `"fail"`;
            otherwise `"pass"`.
       No logging of URLs, bodies or verdicts.
   → verify by step 8's test file.
   Traps: do not touch TFactory. `send_pr_attach` is not in `__all__`; do not
   "fix" that here. Keep the function sync; the existing posters are async
   and must not be reused.

8. **New `apps/backend/pfactory/test_tfactory_verdict.py`.** Style as
   `apps/backend/pfactory/test_tfactory_pr_attach.py` (`import
   pfactory.tfactory_client as tc`, monkeypatch `tc.tfactory_config` to return
   `{"base_url": "http://tf", "token": "tok"}`, `monkeypatch.setenv("TFACTORY_PROJECT_ID", "demo")`).
   A fake getter maps URL → `(status, body)` and records headers. Cases:
   - triaged, project `demo`, verdicts `[accept, flag]` → `"pass"` (D9: flag
     does not block); assert the bearer header and the two URLs
     (`http://tf/api/tfactory/tasks/048-feat` and `.../verdicts.json`).
   - one `reject` among accepts → `"fail"`.
   - each → `None`: project `other`; status `running`; status
     `triaged_empty`; a 404 on the task; a 404 on verdicts; a getter that
     raises; `base_url` `""`; an empty `verdicts` list; a body that is a list.
   - a spec id with `/` is URL-quoted (`a%2Fb`).
   → verify by
   `PYTHONPATH=apps/backend $V/pytest -q -o asyncio_mode=auto apps/backend/pfactory/test_tfactory_verdict.py`
   (all pass).
   Traps: locally `apps/backend/pfactory` tests need `PYTHONPATH=apps/backend`
   (else `ModuleNotFoundError: providers` at collection). No network.

9. **Tests in `apps/web-server/tests/` (all with injected runners, getters
   and fetches; no network).**
   - `test_pr_endgame_merge_gate.py` (append):
     - `merge_gate_signals(spec, fetched={"tfactory_verdict": "pass", "host_ci_green": True})`
       with a recorded `tfactoryVerdict: "fail"` → verdict `"fail"`; with a
       `handback_received.json` receipt → `"handback"`.
     - fetched `"fail"` with recorded `"pass"` → `"fail"`.
     - fetched `host_ci_green=None`, and fetched `{}` (missing keys) →
       `host_ci_green is False` and verdict `None`.
     - recorded `hostCiGreen: false` with fetched `True` → `False`;
       recorded `true` with fetched `None` → `False`.
     - `merge_gate_signals(empty_dir)` equals today's literal dict
       `{"host_ci_green": True, "tfactory_verdict": "pass", "achieved_val": 0, "val_floor": None, "ci_parity": True}`.
   - `test_trusted_contract_merge.py` (append; use `_spec(tmp_path, tier="low")`
     and `_rec(signed)`, the existing autouse `_isolated_host`):
     - verified, `fetch_signals=lambda: {"tfactory_verdict": "pass", "host_ci_green": True}`
       → `pe.AUTO_MERGE_DISPOSITION`.
     - verified, fetch with `None` verdict, and separately `None` host-CI →
       not `AUTO_MERGE_DISPOSITION`.
     - verified, fetch that raises → `pe.HOLD_BLOCKING_DISPOSITION`.
     - legacy (`trusted=None`, a spec dir with no contract), fetch that raises
       → same disposition as without `fetch_signals` (proves it is never
       called).
     - `_clears_with_human_approval(spec, "low", _rec(signed), fetch_signals=<red>)`
       → `False`.
     - `run_pr_endgame` with a capturing `_fake_watch` (pattern from
       `test_pr_endgame_merge_gate.py:53-92`): verified → `merge_gate` kwarg is
       callable; `trusted=None` → `merge_gate` is `None` (C1).
   - `test_pr_endgame.py` (append, `FakeRunner` at `:38-54`,
     `poll_interval=0, max_minutes=1`, review approved):
     - `merge_gate=lambda _h: False` → `not r.saw("pr merge")`,
       `res["reason"].startswith("review_timeout")`.
     - `merge_gate=lambda h: h == "abc"` with route
       `".head.sha": CmdResult(0, "abc", "")` → merged, and the merge argv
       contains `--match-head-commit abc`; `not r.saw("update-branch")`.
     - a `fix_fn` cycle first (changes requested, then approved) with a gate →
       `reason == "merge_gate_unverified_head"`, no `pr merge`.
     - `host_ci_green`: all runs `completed/success` and status
       `{"state": "success", "total_count": 1}` → `True`; one `failure` →
       `False`; one `in_progress` → `None`; zero runs and
       `{"state": "pending", "total_count": 0}` → `None`; rc 1 → `None`;
       a runner raising `subprocess.TimeoutExpired(["gh"], 120)` → `None`;
       runs green and status `pending` with `total_count: 2` → `None`.
   → verify by the step 0 pytest command; expect `130 + new` passed, 0 failed.
   Traps: the two legacy full-chain tests (`:468`, `:499`) must pass
   unchanged; if they fail, C1's legacy guard in step 6 is wrong.

10. **`CHANGELOG.md:3` (`### Security` under `## [Unreleased]`): add as the
    first bullet (D14):**
    ```markdown
    - **Verified tasks auto-merge only on a TFactory verdict and green CI that AIFactory fetched itself (#1695).**
      At merge time the PR endgame reads the TFactory verdict from TFactory's API
      and the host-CI result from GitHub checks for the exact head commit, and
      merges only that commit. `task_metadata.json` can still hold a merge but no
      longer clear one. Operator-visible: a host without `TFACTORY_BASE_URL`, or a
      repo without CI, never auto-merges a verified task; it times out to a human
      after 20 minutes. A head pushed by the endgame's own fix or conflict cycle
      also goes to a human. Legacy and from-issue tasks are unchanged (#1697).
    ```
    → verify by `git diff CHANGELOG.md` (one bullet added, nothing else).
    Traps: none.

11. **Quality gates, with files staged.**
    `git add apps/web-server/server/services/pr_endgame.py apps/backend/pfactory/tfactory_client.py apps/backend/pfactory/test_tfactory_verdict.py apps/web-server/tests/test_pr_endgame_merge_gate.py apps/web-server/tests/test_trusted_contract_merge.py apps/web-server/tests/test_pr_endgame.py CHANGELOG.md`, then
    - `python scripts/cq_ratchet.py --tool ruff --staged --ruff $V/ruff --config standards/ruff.toml`
    - `python scripts/cq_ratchet.py --tool mypy --staged --mypy $V/mypy --config standards/mypy.ini`
    → both exit 0 (no per-file count went up).
    Traps: the ratchets judge only staged files, so stage first.
    `standards/coding-standards.md` (and the rest of `standards/`) is vendored:
    never edit it to pass a gate. Commit only after both ratchets pass;
    commit message cites `#1695` and the plan steps done.

## Files touched

- `apps/web-server/server/services/pr_endgame.py` (steps 1-6)
- `apps/backend/pfactory/tfactory_client.py` (step 7)
- `apps/backend/pfactory/test_tfactory_verdict.py` (new, step 8)
- `apps/web-server/tests/test_pr_endgame_merge_gate.py`,
  `apps/web-server/tests/test_trusted_contract_merge.py`,
  `apps/web-server/tests/test_pr_endgame.py` (step 9)
- `CHANGELOG.md` (step 10)

## Tests

```bash
cd /mnt/code/Source-home/GitHub/AIFactory-1695
V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin

# 1. Endgame + merge gate suites: all pass (130 baseline + new), 0 failed.
$V/pytest -q -o asyncio_mode=auto \
  apps/web-server/tests/test_pr_endgame_merge_gate.py \
  apps/web-server/tests/test_trusted_contract_merge.py \
  apps/web-server/tests/test_pr_endgame.py \
  apps/web-server/tests/test_merge_decision_reaches_the_merge.py \
  apps/web-server/tests/test_pr_endgame_review_tier.py

# 2. Conflict loop (root tests/): all pass, unchanged.
$V/pytest -q -o asyncio_mode=auto tests/test_pr_endgame_conflict_loop.py

# 3. TFactory verdict client: all pass.
PYTHONPATH=apps/backend $V/pytest -q -o asyncio_mode=auto \
  apps/backend/pfactory/test_tfactory_verdict.py \
  apps/backend/pfactory/test_tfactory_pr_attach.py

# 4. Autonomy matrix byte-identical: prints "ok: ..." and exits 0.
$V/python scripts/gen_autonomy_matrix.py --check

# 5. Ratchets with files staged: both exit 0.
python scripts/cq_ratchet.py --tool ruff --staged --ruff $V/ruff --config standards/ruff.toml
python scripts/cq_ratchet.py --tool mypy --staged --mypy $V/mypy --config standards/mypy.ini
```

On the PR: CI green, and CodeQL reports no new `py/log-injection`. Re-run the
#1672 probe: for a verified task, editing or deleting `tfactoryVerdict` or
`hostCiGreen` in `task_metadata.json` must not turn a fetched red or `None`
into `auto-merge` (covered by the step 9 `merge_gate_signals` and
`merge_disposition` cases).

## Rollback

All changes are additive keyword arguments with `None` defaults plus two new
fetchers; there is no data or schema change. Roll back by reverting the
implementation commit(s) (`git revert <sha>`). Verified tasks then return to
metadata-based signals (the #1695 hole reopens); legacy tasks are unaffected
either way. For an emergency without a deploy, operators can turn
`AIFACTORY_AUTO_MERGE` off, which holds every PR for a human.

## Approver decisions (2026-10-13)

1. C1 approved: arm the merge-time signal check only for non-legacy tasks; a failed contract lookup counts as non-legacy so the merge holds. Legacy tasks keep today's behaviour, including the update-branch retry.

## Deviations during implementation

- Step 4: `host_ci_green` merges the last three "unknown" rules into one
  `pending` expression instead of three separate returns (ruff PLR0911, max six
  returns, would regress the ratchet). Behaviour and rule order are unchanged.
- Step 5: the head resolution and gate check live in a small async helper,
  `_gated_head(merge_gate, match_head, pr_head, pr)`, instead of inline.
  `watch_and_finish` carries `# noqa: PLR0911, PLR0915 - merge-gate human-stop
  (#1695)`: the new human-stop return and statements push the already-long
  watcher past ruff's return/statement limits, and the ratchet would regress.
