---
status: approved
issue: 1624
spec: spec/2026-10-10-1624-naive-datetime-now.md
---

# Plan: naive `datetime.now()` timestamps in web-server, and the #1619 stale-sweep skew

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1624`, branch
`fix/1624-naive-datetime-now`, base `origin/dev`, spec approved at `71aa436f`.
All paths are repo-relative. Line numbers are from `71aa436f` (before the
rebase in step 0).

## Approved decisions (self-contained summary)

**Problem in one line.** Web-server writes naive local `datetime.now()` values.
Since #1619, task `updated_at` is aware UTC (`task_service.py:1033` via
`_iso_from_timestamp`, `:927`), but the stale sweep's clock
(`apps/web-server/server/routes/stale.py:70` `_now()`) is still naive local.
`find_stale` strips tzinfo from both sides, so on any host east of UTC a
task that went idle 1h ago reads as 1h + offset idle and live work gets reaped.

- **D1 Contract.** Write aware UTC only: `datetime.now(UTC)` and
  `datetime.fromtimestamp(x, UTC)`, serialised with plain `.isoformat()`
  (`+00:00`). No `Z` formatter, no `utcnow_iso()` helper, no separate
  contract document.
- **D2 Readers.** A reader that compares a stored value with an aware now
  treats a naive legacy value as UTC inline:
  `dt if dt.tzinfo else dt.replace(tzinfo=UTC)`. No reader in this diff needs
  it. It goes into the standard only.
- **D3 Legacy rows.** No migration or rewrite of naive rows already on disk.
- **D4 Wire format.** `+00:00` from `isoformat()`, following #1619's
  `_iso_from_timestamp` (`task_service.py:927`).
- **D5 Staged scope.** This change lands the stale-sweep fix, all 62
  web-server DTZ005/DTZ006 calls and a blocking lint gate. `apps/backend`
  (98 calls in 41 files with `--isolated`; 77 in 36 files under the repo
  config, which excludes the vendored mirror) and
  `scripts/provider_benchmark.py` are carved out with per-file-ignores and
  left to a follow-up issue.
- **D6 CFactory.** `upstream_ws.py`'s reader is not fixed here. A separate
  CFactory issue covers it (treat naive as UTC before comparing).
- **D7 Local-time exceptions.** Exactly three sites keep `datetime.now()`
  with `# noqa: DTZ005 - <reason>`: `terminal.py:585` (session filename) and
  `settings.py:2128`, `:2129` (today and 7-day keys, matched against
  local-date `dailyActivity` keys). Not `.astimezone()`.
- **D8** `logging_config.py:191` and `terminal.py:592` (session timestamp,
  persisted and returned by the API) become UTC. `stale.py` is not a
  local-time use.
- **D9 Stale fix in this change.** `stale.py` `_now()` returns
  `datetime.now(UTC)`. `services/stale_tasks.py` does not change: its
  tzinfo strip is correct once both sides are UTC, and `_parse` already maps
  `Z` to `+00:00`.
- **D10 Contract location.** One sentence appended to the existing rule at
  `standards/coding-standards.md:70` (1.9 Hygiene).
- **D11 Same-line edits.** Import edits stay on their line
  (`from datetime import UTC, datetime`) so no line numbers shift. Local
  (function-scope) imports get the same in-place edit.
- **D12** DTZ006 sites use `datetime.fromtimestamp(x, UTC)` inline. Do not
  import the private `_iso_from_timestamp`.
- **D13** Persisted, API and in-memory/log sites all get the same treatment.
  The class only matters for the three D7 exceptions.
- **D14 Lint gate.** Add only `DTZ005` and `DTZ006` (not all `DTZ`) to root
  `ruff.toml` `[lint] select`, with a #1624 comment. The carve-out names
  `scripts/provider_benchmark.py` exactly (no `scripts/**`). The test globs
  keep `S101`, `PLR2004` and add `DTZ005`, `DTZ006`.
- **D15 One test.** A single stale-sweep regression test with a `TZ=JST-9`
  POSIX fixture (ahead of UTC, needs no tzdata), a `tm_gmtoff` assertion and
  `monkeypatch.undo()` + `time.tzset()` restore. No per-writer tests: the
  lint gate proves the writers.
- **D16 Follow-ups**, opened outside the diff: backend +
  `provider_benchmark.py` (must name the `bot_detection.py:266-267`
  reader/writer pairing trap), and CFactory `upstream_ws.py`.
- **D17 Docs.** `CHANGELOG.md` entry under `## [Unreleased]`. Run
  `scripts/gen_autonomy_matrix.py` and `--check`. Regenerate on rebase, never
  hand-merge.
- **D18** Commit scope has no `#`, e.g. `fix(web-server): ...`. Mutation
  checks are done by editing the file, not by stash or checkout.

**Findings carried in from planning.**

- **F1 (spec gap).** The spec lists local imports only at `context.py:745`,
  `settings.py:1643` and `:2105`. Three more exist: `execution.py:448`,
  `projects.py:1235`, `settings.py:2844`. `settings.py` has **no**
  module-level datetime import, so `:2844` **must** become
  `from datetime import UTC, datetime` or `:2854` raises `NameError` at
  runtime. `execution.py:448` and `projects.py:1235` would resolve `UTC` from
  the edited module import, but D11 says edit them too.
- **F2 (drift).** The worktree is 4 commits behind `origin/dev`. #1688
  (PR #1726, `bf4fd672`) moved the `pr_data_service.py` site from `:306` to
  `:307`. `CHANGELOG.md` and the autonomy matrix md/json also changed
  upstream. No other target file differs. Rebase first (step 0).
- **F3.** `apps/web-server/tests/test_stale_tasks.py` is already 401 lines
  (standards 1.8 cap is 400). The new test grows it. Ruff does not gate this;
  say so in the PR and do not split the file.
- **F4 (not in the spec, not fixed here).** `routes/context.py:207`
  `"lastRefreshed": __import__("datetime").datetime.now().isoformat()` is a
  naive web-server writer that DTZ005 cannot see. The spec's 62 sites do not
  include it; raise it in the PR and the backend follow-up issue rather than
  widening this diff.
- **In-flight overlap.** Only #1633 (PR #1735) shares a file:
  `services/agent_service.py`, hunks at 366-382 and after 407. It does not
  touch our lines; after it merges `:1264` becomes `:1265`, the import at
  `:19` stays. **Merge order: #1633 first, then #1624 rebases.** #1627,
  #1620, #1720, #1719, #1717, #1708, #1631, #1632 and #1692 touch none of the
  target files. `CHANGELOG.md` was not checked; on conflict keep both
  entries.

**Measured baseline** (ruff 0.14.10, repo config, at `71aa436f`):
web-server 62 (57 DTZ005 + 5 DTZ006) in 20 files, `apps/web-server/tests` 0,
`apps/backend` 77, `scripts` 1 (`provider_benchmark.py:234`), root `tests`
26. `tests/test_stale_tasks.py` 22 passed. `apps/web-server/tests` collects
791. `stale.py` is clean today only because of its `noqa`. No target file
already defines `UTC` or imports `timezone`.

**Handoff.** Six file-editing steps over 20+ files, so steps 0-5 go to the
`coder` agent. Step 6 stays with the session.

**Rule for every step.** Find each site with
`grep -n 'datetime.now()\|fromtimestamp(' <file>`; do not jump to the line
number. Edit on the same line only (D11).

**Environment.** The worktree has no venv. Use
`PY=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/python` and
put its `bin` on `PATH` before committing so the pre-commit hook runs.

## Steps

0. (no file edits): `git fetch origin && git rebase origin/dev`; after #1633
   merges, rebase again → verify by `git rev-list --count HEAD..origin/dev`
   prints 0 and `grep -n 'datetime.now()' apps/web-server/server/services/pr_data_service.py`
   shows `:307`.
   Traps: regenerate the autonomy matrix md and json on every rebase, never
   hand-merge (D17). On a `CHANGELOG.md` conflict keep both entries.

1. `apps/web-server/tests/test_stale_tasks.py:10-18, EOF after :401`: red
   test (D15). In the import block add `import time` and
   `from collections.abc import Iterator` (`:15` already imports
   `UTC, datetime, timedelta`; `find_stale` is already imported at `:24-31`).
   Append:

   ```python
   @pytest.fixture
   def non_utc_local_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
       """Force local time to UTC+9 so a naive-local clock is 9h off UTC."""
       monkeypatch.setenv("TZ", "JST-9")
       time.tzset()
       yield
       monkeypatch.undo()
       time.tzset()


   @pytest.mark.usefixtures("non_utc_local_tz")
   def test_route_clock_is_utc_so_fresh_task_is_not_stale_off_utc() -> None:
       """#1624: updated_at is aware UTC since #1619; a naive-local _now() made a
       1h-old task look 10h idle on any host east of UTC and reaped live work."""
       assert time.localtime().tm_gmtoff == 9 * 3600  # fixture took effect
       stale = pytest.importorskip("server.routes.stale")  # pattern at :149

       def task(hours_ago: float) -> dict[str, str]:
           ts = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
           return {"id": "p:001-x", "status": "in_progress", "updated_at": ts}

       now = stale._now()
       assert find_stale([task(1)], now=now, stale_after=timedelta(hours=2)) == []
       # Positive control: proves the [] above is not vacuous.
       assert len(find_stale([task(3)], now=now, stale_after=timedelta(hours=2))) == 1
   ```

   → verify by `$PY -m pytest -o asyncio_mode=auto apps/web-server/tests/test_stale_tasks.py -k off_utc`
   **fails** (1 StaleTask, about 10h idle).
   Traps: file passes the 400-line cap (F3), mention in PR, do not split.
   mypy `--strict` ratchet covers this file: annotate every parameter and
   return. Use the POSIX TZ string; no tzdata, no `ZoneInfo`. Use
   `@pytest.mark.usefixtures`, not a `non_utc_local_tz: None` parameter: the
   strict ruff ratchet flags the unused parameter as ARG001 (0 -> 1,
   regression; checked in a scratch worktree).

2. `apps/web-server/server/routes/stale.py:29, 63-70`: the fix (D9).
   `:29` → `from datetime import UTC, datetime, timedelta`. `:63-69`
   docstring: the clock is UTC because `updated_at` has been aware UTC since
   #1619 (`task_service.py` `_iso_from_timestamp`); drop the "naive local
   mtime" text. `:70` → `return datetime.now(UTC)` with the `noqa` removed.
   `services/stale_tasks.py` is unchanged.
   → verify by the step 1 command passing, then
   `$PY -m pytest -o asyncio_mode=auto apps/web-server/tests/test_stale_tasks.py -q`
   (23 passed). Mutation M1 (see Tests).
   Traps: mutate by editing the file, not stash/checkout (D18).

3. `ruff.toml:107-127, ~231, 254-256`: red lint gate (D14, D5).
   - `[lint] select`, before the closing `]` at `:127`: add
     `"DTZ005", "DTZ006",` with a comment `# #1624: aware UTC timestamps;
     web-server clean, backend/scripts carved out below`.
   - `[lint.per-file-ignores]`, after the TID251 block (~`:231`): add
     `"apps/backend/**" = ["DTZ005", "DTZ006"]` and
     `"scripts/provider_benchmark.py" = ["DTZ005", "DTZ006"]`, with a comment
     naming the follow-up issue (number filled in at step 6, or "follow-up
     to #1624" until then).
   - `:254-256`: each of `**/test_*.py`, `**/*_test.py`, `**/tests/**`
     becomes `["S101", "PLR2004", "DTZ005", "DTZ006"]`.

   → verify by `ruff check apps/backend apps/web-server scripts tests --select DTZ005,DTZ006 --statistics`
   reporting exactly 62 (57 DTZ005 + 5 DTZ006), all under
   `apps/web-server/server`; backend, scripts and tests report 0.
   Traps: existing F841 (`:184-203`) and TID251 (`:210-231`) globs still
   apply because ruff unions matching entries; do not fold them together.
   No `scripts/**` glob.

4. Web-server writers, 20 files, 62 sites (D1, D7, D8, D12, D13): each
   import becomes `from datetime import UTC, datetime[, timedelta]` on the
   same line; `datetime.now()` → `datetime.now(UTC)`;
   `datetime.fromtimestamp(x)` → `datetime.fromtimestamp(x, UTC)`.
   Paths under `apps/web-server/server/`.

   | File | Import line | Sites |
   |---|---|---|
   | `logging_config.py` | 29 | 191 (structlog already `TimeStamper(utc=True)` at :81) |
   | `routes/context.py` | 745 (local) | 747 |
   | `routes/execution.py` | 10, and 448 (local, F1) | 513, 524, 1218, 1410 |
   | `routes/files.py` | 16 | 382, 442, 608, 672 (DTZ006) |
   | `routes/from_issue.py` | 31 | 175 |
   | `routes/projects.py` | 10, and 1235 (local, F1) | 321, 322, 639, 727, 786, 992, 1290, 1474 |
   | `routes/settings.py` | 1643 (local) | 1652 |
   | `routes/settings.py` | 2105 (local, keeps `timedelta`) | 2117, 2165 |
   | `routes/settings.py` | **2844 (local, required, F1)** | 2854 |
   | `routes/tasks.py` | 11 | 290 |
   | `routes/tasks_logs.py` | 16 | 159-161 (DTZ006, `qa_report.stat().st_mtime`) |
   | `routes/terminal.py` | 11 | 592 |
   | `services/agent_credential.py` | 15 | 357, 436 |
   | `services/agent_service.py` | 19 | 1264 (1265 after #1633) |
   | `services/agent_spec_creation.py` | 16 | 314 |
   | `services/agent_task_models.py` | 12 | 25, 64 (`default_factory` lambdas) |
   | `services/changelog_service.py` | 12 | 68, 141 |
   | `services/insights_providers/claude_provider.py` | 14 | 300 |
   | `services/insights_service.py` | 16 | 97, 98, 126, 189, 190, 357, 397 |
   | `services/pr_data_service.py` | 16 | 307 (306 before rebase) |
   | `services/pr_review_service.py` | 22 | 69, 105, 106, 115, 129, 141, 147, 152, 293 |
   | `services/task_log_writer.py` | 14 | 84, 117, 173, 192, 255, 291, 293 |

   Keep local time at exactly three sites (D7), adding a reason:
   - `routes/terminal.py:585`: `# noqa: DTZ005 - local wall-clock session filename shown to users`
   - `routes/settings.py:2128`, `:2129`: `# noqa: DTZ005 - matched against local-date dailyActivity keys in the Claude stats file`

   → verify by `ruff check apps/backend apps/web-server scripts tests` exit 0;
   `ruff format --check apps/backend apps/web-server scripts tests` exit 0;
   `grep -rn 'noqa: DTZ00' apps/web-server/server` prints exactly 3 lines;
   `grep -n 'from datetime' apps/web-server/server/routes/settings.py` shows
   `UTC` in all three local imports;
   `$PY -m pytest -o asyncio_mode=auto apps/web-server/tests -q` all pass
   (794 passed at `origin/dev` `0bf19434` with the step 1 test; 791 + 1
   before the rebase).
   Traps: run `ruff format` on the touched files: `pr_review_service.py:141`
   (`completedAt`) is over 88 columns with `(UTC)` and wraps to 3 lines,
   moving `:147`, `:152`, `:293` down by 2. That is the only format change.
   Do **not** blanket-replace `datetime.now()`: `routes/context.py:207`
   (`__import__("datetime").datetime.now()`, invisible to DTZ) has no `UTC`
   in scope, so a blind edit there is an F821 (undefined name) the ratchet
   rejects; it is not one of the 62 sites (see F4). **`settings.py` has no module-level datetime import** — missing
   `:2844` is a runtime `NameError` lint will not catch (F1). Same-line edits
   for the imports (D11). `docs/compliance/control-objectives.toml:88`
   cites `pr_review_service.py` as a whole file, with no line number, so the
   format wrap above does not touch the matrix. No new log lines (CodeQL). Do not
   import `_iso_from_timestamp` (D12). `tests/test_security.py`
   GitCommitValidator fails while files are staged; that is environmental.

5. `standards/coding-standards.md:70-71`, `CHANGELOG.md:1-3`, autonomy
   matrix: docs (D2, D10, D17).
   - `standards/coding-standards.md:70` (1.9 Hygiene): append one sentence:
     write aware UTC (`datetime.now(UTC)`) serialised with `isoformat()`
     (`+00:00`); a reader comparing a stored value against an aware now
     treats a naive legacy value as UTC:
     `dt if dt.tzinfo else dt.replace(tzinfo=UTC)`.
   - `CHANGELOG.md` `## [Unreleased]` → Fixed: stale sweep no longer skewed
     by the host's UTC offset; web-server timestamps are aware UTC; DTZ005 /
     DTZ006 lint gate on (backend carved out). Keep upstream entries.
   - `python scripts/gen_autonomy_matrix.py` then
     `python scripts/gen_autonomy_matrix.py --check`.

   → verify by `--check` exit 0, and `git diff --stat origin/dev` lists only
   the planned files (the matrix md/json show no diff: the matrix cites
   `pr_review_service.py` by file, not by line; verified in a scratch
   worktree).
   Traps: never hand-edit `docs/docs/compliance/autonomy-matrix.md` or
   `docs/static/compliance/autonomy-matrix.json`.

6. (session, not the coder) Follow-ups and PR (D6, D16, D18).
   - Open the backend + `scripts/provider_benchmark.py` issue: 98 calls in
     41 files (`--isolated`; 77 under repo config) plus
     `provider_benchmark.py:234`. It must name the reader/writer pairing trap
     at `apps/backend/runners/github/bot_detection.py:266-267` and remove the two
     per-file-ignores when done. Put its number in the `ruff.toml` comment.
   - Open a CFactory issue: `upstream_ws.py` reader treats naive as UTC
     before comparing.
   - Commit `fix(web-server): write aware UTC timestamps and gate DTZ005/DTZ006 (#1624)`
     (no `#` in the scope).
   - PR to `dev` links intent, spec and plan, lists which steps the coder
     did, and mentions F3 (test file over 400 lines).

   → verify by `gh issue view <n>` for both issues and
   `git log -1 --format=%s` matching the commit above.
   Traps: rebase after #1633 merges before opening/merging the PR.

## Tests

Run from the worktree root after step 0. Bare `python`/`mypy` below must be
the venv's, so put its `bin` on `PATH` first.

```bash
export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH
PY=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/python
$PY -m pytest apps/web-server/tests/test_stale_tasks.py -q -o asyncio_mode=auto   # 23 passed
$PY -m pytest apps/web-server/tests -q -o asyncio_mode=auto                      # all pass (794 at 0bf19434)
ruff check --select DTZ005,DTZ006 apps/web-server                                 # 0 (was 62)
ruff check apps/backend apps/web-server scripts tests                            # exit 0
ruff format --check apps/backend apps/web-server scripts tests                   # exit 0
grep -rn "noqa: DTZ005" apps/web-server/server                                   # 3: settings.py 2128, 2129; terminal.py 585
# strict ratchet (git add the changed files first; --staged reads the index)
python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'          # exit 0
python scripts/cq_ratchet.py --staged --tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py' # exit 0
python scripts/gen_autonomy_matrix.py && python scripts/gen_autonomy_matrix.py --check  # exit 0
$PY -m pytest tests/test_no_unscrubbed_spawn.py -q                                # pass
```

`standards/ruff.toml` already selects all `DTZ`; the touched files only lose
findings, so the ratchet passes. Checked by applying steps 1-4 in a scratch
worktree on `origin/dev` `0bf19434`: ruff ratchet 22 files, 20 improved,
0 regressed; mypy ratchet 22 unchanged, 0 regressed; default `ruff check`
and `ruff format --check` exit 0; matrix `--check` exit 0; M1 and M2 fail
as stated.

Mutation checks (edit the file, re-run, restore by re-editing, re-run green):

| # | Edit | Expected |
|---|---|---|
| M1 | `stale.py` `_now()` back to `datetime.now()` | new test fails on the first `== []` (1 StaleTask, ~10h idle); positive control still holds |
| M2 | delete the first `time.tzset()` in the fixture | fails on `tm_gmtoff == 32400` (unless the host is already +9) |
| M3 | append `def _probe() -> None:` / `    _x = datetime.now()` at the end of `routes/tasks.py` | `ruff check apps/web-server` exits 1 with exactly 1 DTZ005 |
| M4 | comment out the `"apps/backend/**"` ignore in `ruff.toml` | `ruff check apps/backend` exits 1 with 74 DTZ findings (73 DTZ005 + 1 DTZ006): 77 minus 3 in backend `test_*.py` files that the test globs already ignore |

Not tested per site: the other ~60 call-site edits are mechanical; DTZ = 0
plus the full suite is their check (D15).

## Rollback

1. `git revert <merge-sha>` as one PR. No schema change, no migration (D3).
2. Aware `+00:00` values already persisted (task logs,
   `review_state.approved_at`, PR `posted_at`, terminal
   `session_data.timestamp`, `fetchedAt`/`createdAt`) stay readable:
   `find_stale` strips tzinfo on both sides and `_parse` accepts `Z` and
   `+00:00`. No data cleanup.
3. A revert brings back the naive-local `_now()` skew on non-UTC hosts; while
   reverted, run the reaper's host with `TZ=UTC`.
4. If only the lint gate causes trouble, revert just the `ruff.toml` hunk
   (drop DTZ005/DTZ006 from select) and keep the code fix.

## Approver decisions (2026-10-11)

- The backend + provider_benchmark follow-up is filed before step 3 as **#1740**; put #1740 in the `ruff.toml` comment (step 6 no longer opens it).
- Start now; rebase again after #1633 (PR #1735) merges.
- F4 (`routes/context.py:207`) goes to #1740, not this change.

## Deviations (recorded during implementation)

- None in the code. The coder did steps 1-5; the session model did step 0,
  filed #1740 before step 3 (approver decision) and does step 6.
- Measured: stale tests 23 passed; DTZ count 62 (57 DTZ005 + 5 DTZ006); web-server
  suite 794 passed; ruff ratchet 20 improved, 0 regressed; M1-M4 as tabled.
- F3: `apps/web-server/tests/test_stale_tasks.py` is now 430 lines.
