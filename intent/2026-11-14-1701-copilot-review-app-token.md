---
status: draft
issue: 1701
author: olafkfreund
---

# Intent: request Copilot reviews with the App installation token

## Problem

When `AIFACTORY_PR_REVIEWER=copilot`, the PR endgame asks GitHub to add Copilot
as a reviewer (`request_copilot_review`, `apps/web-server/server/services/pr_endgame.py:873`).
The call runs `gh api` with the process's ambient GitHub token, so once the
GitHub App is configured (#1671, #1973) it already uses the App installation
token. Three things are still wrong:

- Nobody knows whether GitHub accepts an installation token for this request.
  The owner has not created the App yet, so the probe
  (`scripts/github_app_setup.py probe-copilot`) has never run.
- The server cannot tell a refusal from a success. It treats any zero exit
  code from `gh` as "requested", but GitHub can answer 2xx without listing
  Copilot in `requested_reviewers` (the probe checks this; the server does
  not). The endgame then waits 20 minutes for a review that will never come
  and stops for a human, with nothing in the log that says why.
- If the answer turns out to be "refused", the documented fallback is a branch
  ruleset that requests Copilot automatically. With that ruleset on, the
  server still sends a request per PR that is known to fail.

## Proposed outcome

- The server checks that GitHub lists Copilot after the request. If it does
  not, it logs one warning that names the cause and the ruleset fallback.
- An operator setting chooses how Copilot gets requested: by the API call
  (default, today's behaviour) or by the repository ruleset (the server sends
  no request and only waits for the review).
- Both paths are covered by tests with a mocked `gh` runner. When the App
  exists, the only work left is: run the probe once, set the setting to match
  the result, record it on #1701.

## Affected users and systems

- AIFactory web-server: `services/pr_endgame.py`, the Helm chart values, and
  `docs/docs/concepts/github-app.md`.
- Operators who use `AIFACTORY_PR_REVIEWER=copilot`. The default reviewer
  (`aifactory`) is not affected.

## Constraints

- No live App: nothing here may need the App key, and the probe is not run.
- Default behaviour stays the same for PAT deployments: the request is sent,
  and a failed request is never fatal.
- No token or key in logs.
- Does not change which reviews gate a merge.

## Open questions

None. The owner decided on 2026-10-31 that #1701 closes after one probe run
with the App or with the ruleset fallback; this work makes both outcomes a
configuration choice.
