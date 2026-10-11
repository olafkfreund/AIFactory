---
status: approved
issue: 1624
intent: intent/2026-10-10-1624-naive-datetime-now.md
---

# Spec: naive `datetime.now()` timestamps written across web-server and backend

## Design

This change fixes the stale sweep and all web-server writers, and makes ruff
DTZ005/DTZ006 a blocking check for web-server. `apps/backend` and
`scripts/provider_benchmark.py` are carved out with per-file-ignores and left
to a follow-up issue. That staging is the proposed answer to the intent's
open question 3, not an approved decision. Until the follow-up lands, two
intent outcomes hold for web-server only: "every writer brought under the
contract" and "the blocking lint fails on a new naive `datetime.now()`".
Everything below was checked at 1481e274 with ruff
0.14.10. `ruff check --isolated --select DTZ005,DTZ006 apps/web-server` finds
62 calls in 20 files, and `scripts/` has one (`provider_benchmark.py`).

### Contract

Write aware UTC: `datetime.now(UTC)` or `datetime.fromtimestamp(x, UTC)`,
serialised with plain `.isoformat()` (`+00:00`). When a reader compares a
stored value with an aware now, it treats a naive legacy value as UTC:
`dt if dt.tzinfo else dt.replace(tzinfo=UTC)`. This adds one sentence to the
existing rule at `standards/coding-standards.md:70` (1.9 Hygiene). There is
no separate contract document.

### 1. Stale sweep (intent problem 3)

- `apps/web-server/server/routes/stale.py`
  - Line 29: change the import to `from datetime import UTC, datetime, timedelta`
    on the same line.
  - Line 70: `_now()` returns `datetime.now(UTC)`. Drop the `# noqa: DTZ005`.
  - Lines 63-69: rewrite the docstring. It says `updated_at` is naive local
    mtime, which has been false since #1619 (`task_service.py:1033` sets it
    with `_iso_from_timestamp`, which is aware UTC). The new text says the
    clock is UTC because `updated_at` is UTC.
- `apps/web-server/server/services/stale_tasks.py` does not change. Its
  tzinfo strip at :142-143 gives the right answer once both sides are UTC.
- `apps/web-server/tests/test_stale_tasks.py`: add one test that fails today.
  - A fixture `non_utc_local_tz(monkeypatch) -> Iterator[None]` (import
    `Iterator` from `collections.abc`, and import `time`). It runs
    `monkeypatch.setenv("TZ", "JST-9")` and `time.tzset()`, yields, then runs
    `monkeypatch.undo()` and `time.tzset()`.
    - Use the POSIX string `JST-9`, not `Asia/Tokyo`. It needs no tzdata, so
      a minimal or Nix CI image cannot fall back to UTC without anyone
      noticing and pass the test without testing anything.
    - The zone must be ahead of UTC. A zone behind UTC makes tasks look
      younger, and the test would pass today.
  - The test first asserts `time.localtime().tm_gmtoff == 9 * 3600`, which
    proves the fixture took effect.
  - It gets the module with `stale = pytest.importorskip("server.routes.stale")`
    (the pattern at :149). It builds a task with a machine-owned status
    (`in_progress`) and
    `updated_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()`, then
    asserts `find_stale([task], now=stale._now(), stale_after=timedelta(hours=2)) == []`.
    Today the code measures 10h idle and reaps the task. After the fix it
    measures 1h.
  - The test stays aware throughout and returns no `Any`, so the DTZ and
    mypy --strict ratchets stay clean.

### 2. Convert 59 web-server writers

- Imports: change the existing `from datetime import datetime` to
  `from datetime import UTC, datetime` on the same line, so no line numbers
  move. Local imports get the same edit in place: `context.py:745` and
  `settings.py:1643`, `:2105`.
- DTZ006 (5 sites): `files.py:382,442,608,672` and `tasks_logs.py:159` become
  `datetime.fromtimestamp(x, UTC)`. This is inlined, not imported from
  `_iso_from_timestamp`, which is private to another route module.
- Classification (intent outcome 2). Every web-server site is persisted
  state (task logs, project, settings and profile records, PR review logs,
  insights sessions, terminal sessions), an API response field (`fetchedAt`,
  `startedAt`, `TaskProgress.timestamp`), or in-memory/log. All three classes
  get the same treatment, so the class decides nothing except for the three
  local-time sites in part 3.
- DTZ005: every other site becomes `datetime.now(UTC)`. This includes:
  - `logging_config.py:191`. structlog already stamps with
    `TimeStamper(utc=True)` (:81).
  - `terminal.py:592`. This is `session_data["timestamp"]`, which is
    persisted and returned by the API at :614. It is API data, not a filename
    (see Q5).
  - `changelog_service.py:141`, the default `date`. Near midnight on a
    non-UTC host the default can shift by a day. Pods run UTC, so nothing
    changes there.
- Files: `pr_review_service.py` (9), `projects.py` (8), `task_log_writer.py`
  (7), `insights_service.py` (7), `settings.py` (4 of 6), `execution.py` (4),
  `files.py` (4), `agent_task_models.py` (2), `agent_credential.py` (2),
  `changelog_service.py` (2), `terminal.py` (1 of 2), and one each in
  `logging_config.py`, `agent_service.py`, `agent_spec_creation.py`,
  `claude_provider.py`, `from_issue.py`, `tasks.py`, `tasks_logs.py`,
  `context.py` and `pr_data_service.py`.

### 3. Keep 3 local-time sites, each with a reason

Each of these gets `# noqa: DTZ005 - <reason>`:

- `terminal.py:585`: the session filename (local wall clock, seen by users).
- `settings.py:2128` and `:2129`: the "today" and 7-day window keys. These
  are matched against the local-date `dailyActivity` keys in the Claude stats
  file.

### 4. Blocking lint in the root `ruff.toml`

- `[lint] select` (lines 107-127): add `"DTZ005"` and `"DTZ006"`, with a
  comment that cites #1624.
- `[lint.per-file-ignores]`:
  - Add `"apps/backend/**" = ["DTZ005", "DTZ006"]`.
  - Add `"scripts/provider_benchmark.py" = ["DTZ005", "DTZ006"]`. This names
    the one file, where a `scripts/**` glob would let new scripts through.
  - The three test globs (lines 254-256) become
    `["S101", "PLR2004", "DTZ005", "DTZ006"]`. Keep S101 and PLR2004: the
    table replaces the baseline's, it does not merge with it. This covers the
    root `tests/` hits (26 in 6 files, e.g. `test_merge_auto_merger.py` with
    9), which build the backend's naive types on purpose.
  - When several globs match a file, ruff applies all of them, so the
    existing backend F841 and TID251 entries still apply.

### 5. Docs

- `standards/coding-standards.md:70`: add the contract sentence.
- `CHANGELOG.md`: add an entry under `## [Unreleased]`.

### Outside the diff

Open two issues:

- **Backend and `scripts/provider_benchmark.py`.** That issue deletes both
  carve-outs. It must note one trap: `runners/github/bot_detection.py:266-267`
  computes `datetime.now() - fromisoformat(...)`, so that reader must change
  in the same commit as its writer. `services/recovery.py:184-193` already
  keeps a naive and an aware cutoff and tolerates either form.
- **CFactory `upstream_ws.py`.** Treat a naive value as UTC before comparing.

### Reader side: nothing to change in web-server

All 62 sites produce a string through `.isoformat()` or `.strftime()`. The
in-memory `_task_start_times`, `_review_start_times` and `_tool_start_time`
are only passed through (`agent_emit.py:266`, `pr_review_service.py:202`).
No web-server code parses a converted field and compares it with now.

The readers that do exist already normalise: `stale_tasks.py:100` and
`:142-143`, `delegation_tracker.py:190-197`, `agent_kubejob.py:999-1003`.
In the backend, `review_state.json` `approved_at` (written at
`execution.py:524`) is read by `review/formatters.py:297` and
`review/reviewer.py:218`. Both only call `strftime`, so the worst case is UTC
wall time on a non-UTC dev host, with no crash. `frontend-web` never appends
`'Z'` to a timestamp, so a `+00:00` value cannot become the invalid
`+00:00Z`.

### Proposed answers to the intent's open questions

These are proposed defaults for the approver to confirm.

1. **Legacy rows.** Read naive values as UTC and do no migration. Where a
   reader compares a stored value with an aware now, it attaches UTC inline.
   Pods run UTC (`task_service.py:931-934`), so naive rows written in
   production are already UTC, and only dev-machine rows are skewed. The repo
   already uses this rule (`runners/github/providers/protocol.py:130-132`
   `to_utc`, `agent_kubejob.py:1002-1003`, `qa/review_cycle.py:246-268`).
   This diff has no reader site that needs the rule, so it goes into the
   standard only.
2. **Wire format.** `+00:00`, written by plain `isoformat()`, with no custom
   formatter. This follows the #1619 precedent (`task_service.py:927`).
   `stale_tasks.py:100` rewrites Z to +00:00, `fromisoformat` takes
   `+00:00`, and JS `new Date` accepts either form.
3. **Scope.** Staged, as set out above: the stale sweep, the 62 web-server
   calls and the gate land now, with backend and scripts carved out. The
   follow-up issue covers the backend (98 calls in 41 files, for example
   `runners/github/models.py` with 11 and `merge/orchestrator.py` with 9) and
   deletes the carve-outs.
4. **CFactory reader.** Handle it in a separate CFactory issue. Once this
   lands, new web-server values carry an offset, so the TypeError can come
   only from legacy naive rows.
5. **Local-time uses.**
   - Keep `terminal.py:585` and `settings.py:2128-2129` local, each with a
     `noqa` reason.
   - Convert `logging_config.py:191` to UTC.
   - Convert `terminal.py:592` to UTC as well. The intent named only the
     filename at :585; :592 is persisted API data, not a filename.
   - `stale.py` is not a local-time use. The intent listed it as one, but it
     compares against `updated_at`, which is aware UTC.
6. **Stale sweep.** Fix it in this change (part 1). A separate PR would
   need its own intent, spec and plan to land the same line.
7. **Contract location.** Add one sentence to the existing rule at
   `standards/coding-standards.md:70`.
8. **In-flight work.** Only #1688 (PR #1726) overlaps, on `pr_data_service.py`
   (1 call). Rebase after it merges. Running `git diff --name-only` and
   `git status` in each AIFactory-<n> worktree showed that no other worktree
   touches the target files or `ruff.toml`.

## Alternatives rejected

- **One PR for all ~160 sites.** The backend's ~92 calls would triple the diff
  and the rebase surface, and the outcome does not need them now.
- **A custom `Z` formatter or a `utcnow_iso()` helper.** `isoformat()`
  already writes `+00:00`, and every reader accepts it.
- **Migrating legacy rows on disk.** Production rows are already UTC, and a
  rewrite pass risks data for no gain.
- **Changing `stale_tasks.py` to convert instead of strip.** Its behaviour is
  right once both sides are UTC, and the new blocking DTZ005 catches anyone
  who brings back a naive `_now()`.
- **Shipping the stale fix in its own PR.** It would need a second gate cycle
  for one line.
- **`datetime.now().astimezone()` for the local-time sites.** It gives the
  same wall-clock value and needs no suppression, but it hides from the lint
  that the choice is deliberate. A `noqa` with a reason records that choice
  where the next reader sees it.
- **A `scripts/**` glob.** It would let new naive calls into any new script.
  The named file covers the only hit.
- **Selecting all of `DTZ`.** That would pull in DTZ001/007/011 hits nobody
  has counted. DTZ005 and DTZ006 are what the intent asks for.
- **A test for every writer.** The blocking lint proves the writers
  statically. Only the stale sweep has behaviour that needs a test.

## Risks

1. **Mixed strings in the same files.** `task_logs.json`, project records, PR
   review logs and insights sessions will hold both naive and `+00:00`
   values. The backend `task_logger` keeps writing naive values to
   `task_logs.json` until the follow-up. Python readers handle both forms.
   String sorts can be out of order by the dev host's offset for a while.
   `new Date()` shows old naive rows wrong until they age out. This follows
   from the proposed answer to Q1 and stands only if that is approved.
2. **Backend writers stay naive** until the follow-up, so backend-written
   views (merge timeline, GitHub runner) keep the one-hour skew under BST.
   This follows from the proposed staging in Q3, pending approval.
3. **The test changes the process-wide `TZ`.** The fixture must restore it
   and call `tzset()` again, or later tests in the same worker run in JST.
4. **Conflict with #1688 (PR #1726)** at `pr_data_service.py:306`. Rebase
   after it merges, and expect a `CHANGELOG.md` conflict (keep both
   entries).
5. **Autonomy matrix.** `pr_review_service.py` is cited (`autonomy-matrix.md`
   and `control-objectives.toml:88`). The import edits stay on the same
   line, so no lines shift, but run the generator and `--check` anyway. On a
   rebase, regenerate the matrix rather than merging it by hand.

## Verification

- **Mutation check.** Without the `stale.py:70` change, the new test fails.
  Make that change and undo it by editing the file, not with stash or
  checkout. With the fix, `cd apps/web-server && pytest -o asyncio_mode=auto tests/test_stale_tasks.py`
  passes.
- **Full web-server suite:** `pytest -o asyncio_mode=auto apps/web-server/tests`.
- **Lint:** `ruff format --check` and
  `ruff check apps/backend apps/web-server scripts tests` (ci.yml:84) pass
  with DTZ selected.
- **The gate blocks.** Add a `datetime.now()` to a web-server file and
  `ruff check` fails. Add one to `apps/backend` and it passes. Revert both.
- **No DTZ left in web-server.**
  `ruff check --isolated --select DTZ005,DTZ006 apps/web-server` reports only
  the 3 sites from part 3.
- **Ratchet.** Run
  `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
  and the `--tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini`
  variant. DTZ counts go down and the test file adds no mypy errors.
- **Autonomy matrix:** `python scripts/gen_autonomy_matrix.py --check`.
- **Unaffected checks.** The diff adds no logging, so CodeQL's log rules are
  not touched. It adds no spawns, so `tests/test_no_unscrubbed_spawn.py` is
  not affected. `tests/test_security.py` GitCommitValidator fails while
  files are staged; that is an environment issue, so run it unstaged.
- **Manual check.** Under `TZ=Europe/London`, a fresh
  `TaskProgress().timestamp` ends in `+00:00`, and the cockpit shows it as
  about 0 minutes old.
- **Commit scope** contains no `#`, e.g. `fix(web-server): ...`.
