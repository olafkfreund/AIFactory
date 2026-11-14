---
status: draft
issue: 1701
author: olafkfreund
---

# Intent: request Copilot reviews with the App installation token

## Problem

When `AIFACTORY_PR_REVIEWER=copilot`, the PR endgame asks GitHub to add Copilot
as a reviewer (`request_copilot_review`, `apps/web-server/server/services/pr_endgame.py:873`).
The call runs `gh api` with the process's ambient GitHub token, so after a
successful App start (#1671, #1973) it already uses the App installation
token. Three things are still wrong:

- Nobody knows whether GitHub accepts an installation token for this request.
  The owner has not created the App yet, so the probe
  (`scripts/github_app_setup.py probe-copilot`) has never run.
- The server cannot tell whether the request was accepted. It looks only at
  the exit code of `gh`, and the caller ignores even that
  (`pr_endgame.py:2207`). GitHub can answer 2xx without listing Copilot in
  `requested_reviewers`; the probe checks this, the server does not. A
  refusal with an HTTP error (403 or 422) makes `gh` exit non-zero, and that
  is logged only at INFO with no hint about the fallback. In both cases, when
  nothing else requests the review, the endgame waits up to 20 minutes and
  stops for a human, with nothing in the log that points at the request.
- The documented fallback is a branch ruleset that requests Copilot
  automatically. With that ruleset on, the server still sends a request per
  PR that the operator already knows is not needed.

## Proposed outcome

- An operator can tell from the log whether GitHub confirmed the Copilot
  request for a PR, and is pointed at the ruleset fallback when it did not.
  This covers both an HTTP error and a 2xx answer without Copilot listed. An
  unconfirmed request is reported as unconfirmed, not blamed on the App
  token: a PAT deployment can hit it too, and a ruleset may still request the
  review. The endgame therefore keeps waiting as it does today.
- A repository that relies on the ruleset does not get a needless API request
  per PR.
- Which reviews gate a merge does not change.
- When the App exists, the work left is: run the probe on a test PR opened
  by the App, read a negative result with care (it does not by itself prove
  installation tokens are unsupported), choose the matching configuration,
  and record it on #1701.

## Affected users and systems

- AIFactory web-server: `services/pr_endgame.py`, the Helm chart values, and
  `docs/docs/concepts/github-app.md`.
- Operators who use `AIFACTORY_PR_REVIEWER=copilot`. The default reviewer
  (`aifactory`) is not affected.

## Constraints

- No live App: nothing here may need the App key, and the probe is not run.
- Default behaviour stays the same for PAT deployments: the request is sent,
  and a failed request is never fatal. That includes an answer from `gh` that
  cannot be parsed.
- No token or key in logs.
- Does not change which reviews gate a merge.

## Open questions

None. The owner decided on 2026-10-31 that #1701 closes after one probe run
with the App or with the ruleset fallback; this work makes both outcomes a
configuration choice.
