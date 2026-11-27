---
status: approved
issue: 2258
intent: intent/2026-11-26-2258-tf-writeback-head.md
---

# Spec: accept TFactory's own test write-back commit as the verified head

Revised after the Codex and agy reviews of the first draft:

- The QA gate binds to the head too (agy, confirmed: run `o` `reviewed_head = db5fa75`).
- A write-back commit can carry staged non-test changes, so its changed files are checked (Codex).
- `dry_run is False`, and the parents list is parsed rather than joined (both).

## Design

One resolver maps a head to the build SHA it extends, but only when the head is TFactory's own
test write-back. Both exact-head gates use it. Host CI and the merge pin stay on the real head.

### 1. `tfactory_task` (`apps/backend/pfactory/tfactory_client.py`)

The project-checked task fetch moves out of `tfactory_verdict` into a public helper:

```python
tfactory_task(spec_dir, spec_id, *, getter=None) -> dict | None
```

It covers, as today:

- the config, and the project from the recorded id, then the env, then the derived name;
- the scoped URL;
- a 2xx response;
- `task["project_id"] == project_id`.

`tfactory_verdict` calls it. Its behaviour, the URLs and the tests are unchanged.

### 2. `writeback_base` (new `apps/web-server/server/services/tf_writeback.py`)

```python
writeback_base(spec_dir, head, *, getter=None, reader=None) -> str | None
```

It returns `source_sha` only when all of these hold, and otherwise `None`. It never raises.

- `head` is a 40-hex SHA.
- `task = tfactory_task(spec_dir, spec_dir.name)` is a dict.
- `status_json.status == "triaged"`.
- `source = task["source_sha"]` is a 40-hex SHA and differs from `head`.
- `w = status_json["git_writer"]` is a dict with:
  - `ok is True`;
  - `dry_run is False`;
  - `commit_sha == head`;
  - `committed_paths`, a non-empty list of `str`.
- `reader(head)` returns `(parents, files)`, and:
  - `parents == [source]`, exactly one parent;
  - `files` is non-empty;
  - every file is in `committed_paths` and is test-shaped.

**`files`** holds each changed `filename` plus any `previous_filename`. More than 299 files means
GitHub may have truncated the list, so the result is `None`.

**Test-shaped** means either of these, and never anything under `.github/`:

- a path component is `tests`, `test`, `__tests__` or `spec`;
- the basename matches `test_*.py`, `*_test.py`, `*_test.go`, or `*.test.*` / `*.spec.*` with a
  `.js`, `.jsx`, `.ts` or `.tsx` extension.

**The default `reader`:**

- owner and repo come from `git -C <project dir> remote get-url origin`. It accepts an https or
  ssh GitHub URL, or else returns `None`;
- then `gh api repos/{o}/{r}/commits/{head}` runs under `project_github_env`, with a 30 s
  timeout, and the JSON is parsed in Python.

### 3. `qa_gate.check` (`apps/web-server/server/services/qa_gate.py`)

When `reviewed_head != head_sha`, it calls `writeback_base(spec_dir, head_sha)`. If that equals
`reviewed_head`, the check continues as if the heads matched: the same report-hash and
independence checks follow. Otherwise it returns `qa_stale` as today.

All callers inherit the fix without edits: `pr_endgame._gated_head`, `routes/pr.py` (create-pr,
CFactory approve) and `completion_orchestration`.

### 4. The merge gate (`_build_merge_gate.fetch` in `apps/web-server/server/services/pr_endgame.py`)

```python
verified = writeback_base(spec_dir, head) or head
verdict = tfactory_verdict(spec_dir, spec_id, head=verified, vote=vote)
```

Host CI and required checks are still read for `head`, and the merge stays pinned to `head`
(`--match-head-commit`). The `verdict_only` (adopted PR) path gets the same verdict. That path only
marks the PR ready, and the QA gate still applies to it.

## Alternatives rejected

- **Trust `git_writer.commit_sha` alone.** A stale fetch or staged changes would let unverified
  code in.
- **Change only `tfactory_verdict`** (the first draft). The QA gate still refuses, including
  CFactory approve.
- **Edit each `qa_gate.check` call site.** That is four edits, and a future caller would miss it.
- **TFactory records the parent and the diff.** That needs a TFactory release, and AIFactory would
  trust a report instead of reading the commit.
- **Re-verify after the write-back.** It loops.

## Risks

- **Extra calls on a head mismatch only:** one TFactory GET and one GitHub API call per check. The
  exact-match path is unchanged.
- **Pre-existing:** the task GET and the `verdicts.json` GET are separate reads, with no shared run
  id (Codex finding 4). This is unchanged and noted.
- **A test-shaped file can still run code**, for example a `conftest.py` under `tests/`. It is
  TFactory's own file, from the run that verified the build, and host CI runs on the head.
  Accepted.
- **Projects with an unusual test layout** are not matched by the test-shaped rule. They keep
  today's stall, failing closed.

## Verification

Unit tests:

- **`writeback_base`:**
  - accepted: an exact write-back gives `source`;
  - each rule failing gives `None`: wrong parent, two parents, zero parents, a non-test file, a
    file not in `committed_paths`, a rename from a non-test path, more than 299 files,
    `dry_run` missing or true, `ok` false, `commit_sha` differs, not triaged, `reader` raises, a
    non-SHA head;
  - the remote-URL parsing.
- **`qa_gate.check`:**
  - a mismatch with a matching `writeback_base` gives `None` (the remaining checks pass);
  - a mismatch with `None` gives `qa_stale`;
  - an exact match never calls the resolver.
- **The merge gate:** with a write-back head, `tfactory_verdict` receives the base, and CI is read
  for the head.
- **`tfactory_verdict` / `tfactory_task`:** the existing tests pass unchanged.

The full pfactory and web-server endgame and QA suites pass, and CI is green.

Live: release 3.21.18 and rerun E2E P1. S4 merges and S5 runs. CFactory approve on run `n`'s task
no longer gives `qa_stale`.
