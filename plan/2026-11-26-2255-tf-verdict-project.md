---
status: draft
issue: 2255
spec: spec/2026-11-26-2255-tf-verdict-project.md
coders:
  1: codex   # small change across the poster, the handoff record and the verdict lookup, with exact URL tests
reviewers:
  - codex    # checks the URL scoping and the fail-closed paths against the spec
  - agy      # independent read of the marker write and the parsing
---

# Plan: record TFactory's project id at handoff and scope the verdict lookup with it

## Approved decisions (from the spec)

All changes are in `apps/backend/pfactory/tfactory_client.py`.

- **`_httpx_poster` (~line 968):** on `resp.is_success`, add `workspace_project_id` to the return
  dict, parsed from the **full** `resp.json()` (best-effort: an exception adds nothing). It is
  added only when it is a `str`. `body` is still truncated to 2000 characters and never logged.
- **`send_handoff` (~line 1017):** when `ok` is true and `result.get("workspace_project_id")`
  matches `re.fullmatch(r"[A-Za-z0-9._-]{1,128}", v)`, add `"tfactory_project_id": v` to the
  returned dict.
- **`maybe_auto_handoff_tfactory` (~line 1375):** write `tfactory_handoff.json` atomically.
  - Write `json.dumps(result, indent=2)` to `tfactory_handoff.json.tmp`.
  - Then call `os.replace` over the real file.
  - Keep the existing `contextlib.suppress(OSError)`.
- **New `_recorded_tfactory_project(spec_dir) -> str | None`.** It reads the marker and returns
  `tfactory_project_id` only when:
  - the JSON is a dict;
  - `sent is True`;
  - the id is a string matching the pattern.

  It never raises.
- **`tfactory_verdict` (~line 1255):**
  - `recorded = _recorded_tfactory_project(Path(spec_dir))`
  - `project_id = recorded or os.environ.get("TFACTORY_PROJECT_ID") or _aifactory_project_name(...)`
  - `qs = f"?project_id={quote(recorded, safe='')}" if recorded else ""`
  - The task GET is `f"{base}{qs}"` and the verdicts GET is `f"{base}/verdicts.json{qs}"`.
  - The equality check `task.get("project_id") == project_id` is unchanged.
  - There is no bare retry after a scoped failure.
  - Without a record, everything is byte-for-byte as today, env path included.

## Steps

1. Implement the decisions above, and add tests to `apps/backend/pfactory/test_tfactory_verdict.py`
   (and to the handoff tests file next to it, if one exists).

   Test cases:
   - `_httpx_poster`, with a fake `httpx` client or response, returns a success JSON longer than
     2000 characters that ends with `workspace_project_id`. The result carries the field, and
     `body` stays truncated.
   - `send_handoff` with an injected poster that returns `workspace_project_id`. Expect
     `tfactory_project_id`. A bad value, a missing key, or `ok=False` adds nothing.
   - The marker write goes through `.tmp` and `os.replace`, so the final file is complete JSON.
   - `tfactory_verdict` with a recorded marker:
     - it requests `TASK?project_id=2b2e…` and `TASK/verdicts.json?project_id=2b2e…`;
     - it gives `pass` for accept/flag and `fail` with a `reject`;
     - a response `project_id` other than the recorded id gives `None`.
   - A marker with `sent: false`, a malformed marker, or a bad id: the legacy bare URLs are used.
   - The existing tests (env `TFACTORY_PROJECT_ID=demo` with bare URLs) pass **unchanged**.

   Verify:
   - `cd apps/backend && .venv/bin/pytest -q pfactory/test_tfactory_verdict.py -p no:cacheprovider`.
     If the worktree has no venv, use
     `/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest`.
   - Then run every test that references `tfactory_verdict`:
     `pytest -q merge/test_merge_policy.py` from `apps/backend`, and, from `apps/web-server`,
     `tests/test_pr_endgame_merge_gate.py`, `tests/test_merge_decision_reaches_the_merge.py`,
     `tests/test_pr_verify_evidence.py` and `tests/test_trusted_contract_merge.py`.
     Compare the result with a clean `origin/dev` run. The local venv lacks pytest-asyncio, so
     some async tests already fail there.
   - `ruff check`, `ruff format --check` and `mypy` on the file show no new errors.

   Traps:
   - Never log or store the response body beyond today's truncated `body`.
   - Keep "never raises".
   - URL-encode the id.
   - Don't add a bare fallback.
   - Don't change the env path.

2. **Lead only:**
   - review the diff;
   - commit, and open a PR to `dev`;
   - merge with `--match-head-commit` after CI passes;
   - release AIFactory 3.21.17;
   - confirm `/api/health` reports it;
   - rerun E2E P1 and check that `tfactory_handoff.json` holds `tfactory_project_id`, the PR merges
     (S4) and S5 runs.

## Tests

- The step 1 commands pass, with no new failures compared with the dev baseline.
- CI is green.
- Live: P1 reaches S4 and S5.

## Rollback

Revert the PR on `dev` and release a patch. Old markers that carry `tfactory_project_id` are
ignored by the old code, so no data needs cleaning.
