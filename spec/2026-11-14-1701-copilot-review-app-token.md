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
answer lists a reviewer whose login contains `copilot` in
`requested_reviewers` (the same test as `copilot_listed`,
`scripts/github_app_setup.py:74`). In every other case (non-zero exit, 2xx
without Copilot, output that is not a JSON object) it logs one WARNING and
returns False. It never raises.

The warning text states facts only: the repo and PR, whether `gh` failed or
Copilot was not listed, that the review may not arrive and the endgame will
wait and then stop for a human, and the remedy (enable the ruleset
"Automatically request Copilot code review" and set the reviewer to
`copilot-ruleset`; see `docs/docs/concepts/github-app.md`). It does not say
the App token is the cause: a PAT deployment, a repo without Copilot, or an
already-reviewed PR produce the same answer. `gh` stderr is cut to 200
characters as today; no token is in it (`github_env` keeps the token out of
env and argv).

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
  `copilot`.
- `pr_endgame.py:2228`: `require_copilot` is true for `copilot` and
  `copilot-ruleset`.
- `services/completion_orchestration.py:325` only tests for `aifactory`;
  no change.

The setting is already per project (project setting `prReviewer` →
`AIFACTORY_PR_REVIEWER`, `routes/projects.py:934`, read through
`_project_env`) with the process env as the default. So a deployment with
several repos can use the ruleset on some and the API on others. No new env
variable and no chart change.

Frontend: add the option to the reviewer select
(`apps/frontend-web/src/components/project-settings/GeneralSettings.tsx:251-256`)
and the union type (`apps/frontend-web/src/shared/types/project.ts:71`).

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

- Unit tests (`apps/web-server/tests/`, mocked runner): Copilot listed →
  True, no warning; exit 0 without Copilot → False, one warning; non-zero
  exit → False, one warning; non-JSON output → False, no exception.
- `resolve_pr_reviewer` accepts `copilot-ruleset`; `run_pr_endgame` with it
  sends no request and passes `require_copilot=True` to `watch_and_finish`.
- Existing endgame tests still pass: `pytest apps/web-server/tests -k
  "pr_endgame or merge"`.
- `scripts/cq_ratchet.py` for the touched Python files; frontend type check
  for the two touched files.
- Not verified: a live request with an App token (needs the owner's App).
