---
status: draft
issue: 1701
intent: intent/2026-11-14-1701-copilot-review-app-token.md
---

# Spec: request Copilot reviews with the App installation token

## Design

All paths are under `apps/web-server/server/` unless stated.

### 1. Confirm the request (`services/pr_endgame.py:873`)

`request_copilot_review` already runs `gh api --method POST
/repos/{owner}/{repo}/pulls/{pr}/requested_reviewers` through
`_default_runner`, which uses the ambient token (`pr_endgame.py:79-90`). After
a successful `github_app.start()` that is the installation token, so no
change is needed to send the request as the App.

Change: the function returns True only when `gh` exits 0 **and** the JSON
answer lists a reviewer whose login contains `copilot` (compared in lower
case) in `requested_reviewers` (the same test as `copilot_listed`,
`scripts/github_app_setup.py:74`). In every other case it logs one WARNING
and returns False. It never raises. The other cases are:

- the runner raises (`subprocess.SubprocessError`, which includes the
  timeout, or `OSError`): `_default_runner` does not catch these, and the
  call at `pr_endgame.py:2207` has no guard, so today an exception there
  would skip the watcher;
- non-zero exit;
- exit 0 but the output is not JSON, is JSON but not an object, has a
  `requested_reviewers` that is not a list, or has entries that are not
  objects with a string `login`;
- exit 0, well-formed, Copilot not listed.

The warning text states facts only: the repo and PR, whether `gh` failed or
Copilot was not listed, that the review may not arrive and the endgame will
wait and then stop for a human, and the remedy (enable the ruleset
"Automatically request Copilot code review" and set the reviewer to
`copilot-ruleset`; see `docs/docs/concepts/github-app.md`). It does not say
the App token is the cause: a PAT deployment, a repo without Copilot, or an
already-reviewed PR produce the same answer.

What goes into the log line: the repo and PR number and the first 200
characters of `gh` stderr, each through `sanitize_log` (already imported,
`pr_endgame.py:33`) so a newline cannot forge a log line; for a runner
exception only the exception class name. Never `gh` stdout (the response
body) and never the exception text. The token is not in the child's env or
argv (`github_env` puts it in a per-call `hosts.yml`), so stderr cannot echo
it from there; this is the same stderr the INFO line logs today.

The existing INFO line for a non-zero exit is replaced by the WARNING, so
there is one line per unconfirmed request.

The endgame keeps waiting after an unconfirmed request, as today: a ruleset
or a person may still request the review.

### 2. Ruleset mode: reviewer value `copilot-ruleset`

`AIFACTORY_PR_REVIEWER` gains a fourth value, `copilot-ruleset`
(`_VALID_REVIEWERS`, `pr_endgame.py:782`). It means: Copilot's review gates
the merge exactly as with `copilot`, but the server sends no request because
a repository ruleset does.

- `run_pr_endgame` (`pr_endgame.py:2206`): the request is sent only for
  `copilot`. For `copilot-ruleset` nothing is started: the `elif
  on_pr_opened` branch at `:2208` must not run for it either (today
  `copilot` skips that branch; the new value must too).
- `pr_endgame.py:2228`: `require_copilot` is true for `copilot` and
  `copilot-ruleset`.
- `services/completion_orchestration.py:325` only tests for `aifactory`;
  no change.

The setting is already per project (project setting `prReviewer` →
`AIFACTORY_PR_REVIEWER`, `routes/projects.py:934`, read through
`_project_env`) with the process env as the default. So a deployment with
several repos can use the ruleset on some and the API on others. No new env
variable and no chart change.

Frontend (`apps/frontend-web/src/`): add the value to both unions (the
shared type at `shared/types/project.ts:71` and the inline cast at
`components/project-settings/GeneralSettings.tsx:256`), add an
`<option value="copilot-ruleset">` after the Copilot option (`:260-262`),
and one sentence to the help paragraph above the select (`:242-247`) saying
the value needs the repository ruleset.

Comments that list the three values are updated (`pr_endgame.py:778-781`,
`routes/projects.py:879`).

The settings route stores `prReviewer` as a free string
(`routes/projects.py:880`), and an unknown value falls back to `aifactory`
in `resolve_pr_reviewer`. That is existing behaviour and stays.

### 2a. Probe message (`scripts/github_app_setup.py:173-175`)

The probe prints "the App token cannot request Copilot" for every negative
result. Replace it with a neutral line: the request was not confirmed
(status and message are printed), a ruleset can request the review instead,
and the reviewer value for that is `copilot-ruleset`.

### 3. Docs (`docs/docs/concepts/github-app.md`, section "Copilot review")

- The server now confirms the request and what the warning means.
- `copilot-ruleset`: when to use it, and that the ruleset must exist on every
  repo that uses the value, or the endgame waits 20 minutes and stops.
- The ruleset should also review new pushes if the endgame's fix cycles are
  to be re-reviewed (GitHub's ruleset option; not verified here).
- Probe notes: run it on a PR opened by the App, before the ruleset is
  enabled on that repo (with the ruleset on, Copilot is already listed and
  the probe passes for the wrong reason). A negative result does not by
  itself prove installation tokens are unsupported; read the printed status
  and message.

## Alternatives rejected

- **A separate setting (`AIFACTORY_COPILOT_REVIEW_REQUEST=api|ruleset`).**
  A second key to allowlist, store, document and chart for one bit that only
  matters when the reviewer is Copilot.
- **Stop for a human at once when the request is unconfirmed.** A ruleset or
  a person can still request the review; stopping early would break repos
  that work today.
- **Fall back to the ruleset automatically.** The server cannot create
  rulesets (no Administration permission, by the owner's decision on #1973).
- **Call the API with httpx instead of `gh`.** `gh api` already prints the
  response body; a second HTTP client is not needed.

## Risks

- A repo where the request used to "succeed" without Copilot listed now logs
  a WARNING per PR. That is the point, but it is new log volume.
- `copilot-ruleset` on a repo without the ruleset: the endgame waits 20
  minutes and stops for a human. Same end state as today's silent case;
  documented.
- An old frontend build does not know the new value and shows it as the
  first option; the stored value is unchanged until the user saves.
- GitHub's real answer to an installation token is still unknown. Nothing
  here depends on it.

## Verification

- Unit tests (`apps/web-server/tests/`, mocked runner), each asserting the
  return value and exactly one WARNING (none for the first): Copilot listed
  with login `Copilot` → True; exit 0 without Copilot; non-zero exit (and no
  INFO line); output `not json`, `[]`, `null`,
  `{"requested_reviewers": 123}`, `{"requested_reviewers": [{"login": 123}]}`;
  runner raising `subprocess.TimeoutExpired` and `OSError`.
- Warning content: repo and PR present, `copilot-ruleset` named, the words
  "App token" absent, a sentinel placed in stdout and in the exception text
  absent, a newline in stderr not present raw.
- `resolve_pr_reviewer` accepts `copilot-ruleset`; `run_pr_endgame` with it
  sends no request, does not call a supplied `on_pr_opened`, and passes
  `require_copilot=True` to `watch_and_finish`. With `copilot` and an
  unconfirmed request, `watch_and_finish` is still entered.
- Probe: a negative result prints the neutral line
  (`tests/test_github_app_setup.py`).
- Existing tests that stub the runner with empty stdout for this call are
  updated to return a body that lists Copilot where they assert success.
- Existing endgame tests still pass: `pytest apps/web-server/tests -k
  "pr_endgame or merge"`.
- `scripts/cq_ratchet.py` for the touched Python files; frontend type check
  for the two touched files.
- Not verified: a live request with an App token (needs the owner's App).
