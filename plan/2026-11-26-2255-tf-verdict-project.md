---
status: approved
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

- **`_httpx_poster` (line 968):**
  - Build the existing result exactly as today, from `status_code`, `is_success` and
    `text[:2000]`.
  - Then, only when `resp.is_success`, call `resp.json()` synchronously inside a `try`.
  - If the result is a dict and `workspace_project_id` is a `str`, add it to the return dict.
  - Any exception adds nothing. `body` is unchanged and never logged.
- **`send_handoff` (line 1017):** take `v = result.get("workspace_project_id")`. When `ok` is true,
  `isinstance(v, str)` holds, and `re.fullmatch(r"[A-Za-z0-9._-]{1,128}", v)` matches, add
  `"tfactory_project_id": v` to the returned dict. Checking the type first preserves "never
  raises".
- **`maybe_auto_handoff_tfactory` (starts at line 1333; the write is at line 1375):** write
  `tfactory_handoff.json` atomically, so overlapping handoffs never share a temp file.
  - `fd, tmp = tempfile.mkstemp(dir=spec_dir, prefix=".tfactory_handoff.", suffix=".tmp")`.
  - Write the JSON through `os.fdopen(fd, "w")`, then `os.replace(tmp, spec_dir / "tfactory_handoff.json")`.
  - On an error, remove `tmp`. Keep the existing `contextlib.suppress(OSError)`.
  - A failed result still replaces the prior marker, as today.
- **New `_recorded_tfactory_project(spec_dir) -> str | None`.** It reads the marker and returns
  `tfactory_project_id` only when:
  - the JSON is a dict;
  - `sent is True`;
  - the id is a string matching the pattern.

  It never raises.
- **`tfactory_verdict` (starts at line 1252; the edits are at about lines 1271, 1283 and 1295):**
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

   Test cases. Async functions are called through `asyncio.run(...)` inside synchronous tests,
   because the local venv lacks pytest-asyncio.
   - **`_httpx_poster`:** monkeypatch `httpx.AsyncClient` with a fake whose response has
     `is_success=True`, a `text` longer than 2000 characters, and a `json()` that ends with
     `workspace_project_id`. The result carries the field and `body` is still truncated.
     - A non-JSON success response (`json()` raises) adds nothing.
     - A failed response adds nothing.
   - **`send_handoff`** with an injected poster:
     - `ok=True` with `workspace_project_id="2b2e"` gives `tfactory_project_id`;
     - a non-string, a value outside the pattern, a missing key, or `ok=False` adds nothing;
     - none of these raises.
   - **Marker write:** the marker is complete JSON, it is written through `tempfile.mkstemp` and
     `os.replace`, and no `.tmp` file is left behind.
   - **`tfactory_verdict`:** give `_run` an optional `spec_dir` parameter, keeping today's default.
     Marker tests use `tmp_path`. **Every** test asserts the exact list of requested URLs.
     - Recorded id, with `TFACTORY_PROJECT_ID=demo` still set: the calls are exactly
       `[TASK?project_id=2b2e, TASK/verdicts.json?project_id=2b2e]`. That gives `pass` for
       accept/flag and `fail` with a `reject`, which also proves the recorded id wins over env.
     - Recorded id, where the task response's `project_id` is a different value: `None`, and the
       call list contains only the scoped task URL.
     - Recorded id, where the scoped task URL returns 404 or 409 while a valid bare route is also
       routed: `None`, and the bare URL was never requested.
     - A marker with `sent: false`, a malformed or unreadable marker, or a bad id: the legacy bare
       URLs.
     - No marker and no env: unset `TFACTORY_PROJECT_ID` with `monkeypatch.delenv`, and use a
       `tmp_path/workspaces/<name>/.aifactory/specs/048-feat` spec dir, so the derived name works.
       The bare URLs are used, and the equality check uses `<name>`.
   - **Existing tests** (env `demo`, bare URLs) pass **unchanged**.

   Verify:
   - Use `P=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest` (the main clone's
     venv) for the branch and for a clean `origin/dev` worktree alike.
   - From `apps/backend`:
     `$P -q pfactory/ merge/test_merge_policy.py -p no:cacheprovider`. The `pfactory` suite
     includes `test_tfactory_verdict.py` and the handoff tests.
   - From `apps/web-server`:
     `$P -q tests/test_pr_endgame_merge_gate.py tests/test_merge_decision_reaches_the_merge.py tests/test_pr_verify_evidence.py tests/test_trusted_contract_merge.py -p no:cacheprovider`.
   - Compare each run with the same command on clean `origin/dev`, and report only new failures.
   - From the repo root:
     `ruff check apps/backend/pfactory/tfactory_client.py apps/backend/pfactory/test_tfactory_verdict.py`
     and `ruff format --check` on the same two files.

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
   - rerun E2E P1 and check that `tfactory_handoff.json` holds `tfactory_project_id`, the scoped
     verdict GET returns HTTP 200, the PR merges (S4) and S5 runs.

## Tests

- The step 1 commands pass, with no new failures compared with the dev baseline.
- CI is green.
- Live: P1 reaches S4 and S5.

## Rollback

Revert the PR on `dev` and release a patch. The old code ignores the extra marker field, so no data
needs cleaning. After a rollback, lookups are bare again, even for tasks with a recorded id. When
spec ids collide (as in E2E), the merge gate returns `None` again.
