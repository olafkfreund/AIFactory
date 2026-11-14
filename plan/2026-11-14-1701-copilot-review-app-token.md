---
status: draft
issue: 1701
spec: spec/2026-11-14-1701-copilot-review-app-token.md
coders:
  1: codex   # small, tricky logic: exception and JSON-shape handling in one function, with its tests
  2: coder   # plain edits: probe message, frontend option, docs
reviewers:
  - codex    # independent read of the diff against this plan
  - agy      # second independent read; it found the on_pr_opened fall-through in the spec review
---

# Plan: request Copilot reviews with the App installation token

## Approved decisions (from the spec)

- The Copilot review request already uses the ambient token, which is the
  App installation token after a successful App start. Nothing changes in
  how the request is sent.
- `request_copilot_review` returns True only when `gh` exits 0 and the JSON
  answer lists a reviewer whose login contains `copilot` (lower-cased) in
  `requested_reviewers`. Every other case logs exactly one WARNING and
  returns False. It never raises. The endgame keeps waiting afterwards.
- The warning names the repo and PR, says whether `gh` failed or Copilot was
  not listed, says the review may not arrive, and names the remedy: the
  repository ruleset "Automatically request Copilot code review" plus
  reviewer value `copilot-ruleset`. It does not blame the App token. It
  never contains `gh` stdout or exception text. Repo, PR and the first 200
  characters of stderr go through `sanitize_log`.
- New reviewer value `copilot-ruleset`: Copilot's review gates the merge as
  with `copilot`, but the server sends no request and starts nothing else.
- No new env variable, no chart change, no change to which reviews gate a
  merge. The settings route keeps storing `prReviewer` as a free string.

## Steps

1. `apps/web-server/server/services/pr_endgame.py` and a new test file
   (coder: codex).
   - `:778-782`: add `"copilot-ruleset"` to `_VALID_REVIEWERS`; extend the
     comment above it with one line for the new value.
   - `:873-893` `request_copilot_review`: wrap the `runner(...)` call in
     `try/except (subprocess.SubprocessError, OSError)`; on exception log
     the warning with the exception class name only and return False. On
     non-zero exit log the warning with `sanitize_log(res.err[:200])` and
     return False (this replaces the INFO line). On exit 0 parse `res.out`
     with `json.loads` inside `try/except ValueError`; confirmed only if
     the result is a dict, `requested_reviewers` is a list, and some entry
     is a dict whose `login` is a str containing `copilot` after
     `.lower()`. Otherwise log the warning and return False. One private
     helper for the warning so there is one message format.
   - `:2206-2208`: keep `if reviewer == "copilot":` for the request; change
     the next branch to `elif reviewer != "copilot-ruleset" and
     on_pr_opened is not None:`.
   - `:2228`: `require_copilot=(reviewer in ("copilot", "copilot-ruleset"))`.
   - New `apps/web-server/tests/test_copilot_review_request.py` with the
     tests listed under Tests. Use a fake runner (a function returning
     `CmdResult`), `caplog` for the warning count and content.
   - `apps/web-server/tests/test_pr_endgame.py:537`: the stubbed answer for
     `requested_reviewers` becomes
     `CmdResult(0, '{"requested_reviewers": [{"login": "Copilot"}]}', "")`.
   → verify by `apps/backend/.venv/bin/pytest apps/web-server/tests/test_copilot_review_request.py apps/web-server/tests/test_pr_endgame.py apps/web-server/tests/test_pr_endgame_review_tier.py apps/web-server/tests/test_pr_endgame_merge_gate.py -q -o asyncio_mode=auto`
   Traps: `json` and `subprocess` are already imported at the top of the
   file (check, do not add duplicates). `sanitize_log` is imported at `:33`.
   The logger prefix is `[pr-endgame]`. The cq ratchet counts ruff findings
   per rule per touched file against `origin/dev`; do not add a `noqa`
   without a rule code. Tests under `apps/web-server/tests` run with
   `-o asyncio_mode=auto`. Do not stage or commit.

2. Probe message, frontend and docs (coder: coder).
   - `scripts/github_app_setup.py:173-176`: replace the "the App token
     cannot request Copilot; ..." line with a neutral one: the request was
     not confirmed; a repository ruleset can request the review instead
     (reviewer value `copilot-ruleset`); see
     `docs/docs/concepts/github-app.md`. Keep printing the status line and
     the message excerpt as today.
   - `tests/test_github_app_setup.py`: if a test asserts the old wording,
     update it; add an assertion that a negative probe prints
     `copilot-ruleset` and not "cannot request" (follow the existing probe
     test's mocking style in that file; if there is no probe test with a
     mocked `httpx`, assert on the message constant instead).
   - `apps/frontend-web/src/shared/types/project.ts:71`: add
     `'copilot-ruleset'` to the union.
   - `apps/frontend-web/src/components/project-settings/GeneralSettings.tsx`:
     `:256` add `'copilot-ruleset'` to the inline cast; after the Copilot
     `<option>` (`:261`) add
     `<option value="copilot-ruleset">GitHub Copilot (requested by ruleset)</option>`;
     in the help paragraph (`:242-247`) add one sentence: the ruleset choice
     waits for Copilot too but sends no request, so the repository needs a
     ruleset that requests Copilot review.
   - `apps/web-server/server/routes/projects.py:879`: add the value to the
     comment.
   - `docs/docs/concepts/github-app.md`, section "Copilot review"
     (`:90-106`): rewrite per the spec's section 3: the server confirms the
     request and what the warning means; `copilot-ruleset` and that the
     ruleset must exist on every repo using it, else a 20 minute wait and a
     human stop; enable review of new pushes in the ruleset if fix cycles
     should be re-reviewed (say this is GitHub's option and was not tested
     here); run the probe on a PR opened by the App and before the ruleset
     is enabled on that repo; a negative probe does not by itself prove
     installation tokens are unsupported.
   → verify by `apps/backend/.venv/bin/pytest tests/test_github_app_setup.py -q`
   and `cd apps/frontend-web && npm run typecheck` (needs `node_modules`; if
   absent, say so in the handback and the lead runs it in CI).
   Traps: docs are plain Markdown under Docusaurus; no em-dash-style
   rewrites of untouched text. Do not stage or commit.

## Tests

`apps/backend/.venv/bin/pytest apps/web-server/tests/test_copilot_review_request.py -q -o asyncio_mode=auto`
must cover, each asserting the return value and the number of WARNING
records from the `pr_endgame` logger:

- answer `{"requested_reviewers": [{"login": "Copilot"}]}`, exit 0 → True,
  0 warnings;
- exit 0 with `{"requested_reviewers": []}` → False, 1 warning;
- exit 1 with stderr `gh: Resource not accessible by integration (HTTP 403)`
  → False, 1 warning, no INFO record;
- exit 0 with stdout `not json`, `[]`, `null`,
  `{"requested_reviewers": 123}`, `{"requested_reviewers": [{"login": 123}]}`
  → False, 1 warning each, no exception;
- runner raising `subprocess.TimeoutExpired("gh", 1)` and `OSError("boom")`
  → False, 1 warning each;
- warning content: contains `owner/repo`, the PR number and
  `copilot-ruleset`; does not contain "App token"; a sentinel string placed
  in stdout and in the exception message is absent; stderr `a\nb` does not
  appear with a raw newline;
- `resolve_pr_reviewer` returns `copilot-ruleset` for that env value;
- `run_pr_endgame(reviewer="copilot-ruleset", on_pr_opened=<spy>)`: no
  `requested_reviewers` call, the spy is not called, `watch_and_finish`
  receives `require_copilot=True` (follow the stubbing in
  `test_pr_endgame_review_tier.py:160-200`);
- `run_pr_endgame(reviewer="copilot")` with `request_copilot_review`
  returning False still calls `watch_and_finish`.

Then the existing suites: `apps/backend/.venv/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto -k "pr_endgame or merge or copilot"`
and `tests/test_github_app_setup.py`; all pass.

Ratchet, as CI runs it (`.github/workflows/cq-ratchet.yml:109-135`), with
`--base origin/dev`: no regression for touched files.

Not tested: a live request with an App token (the App does not exist yet).

## Rollback

Revert the PR. No data or schema change. A project that stored
`copilot-ruleset` falls back to the `aifactory` reviewer after a revert,
because unknown values resolve to it.
