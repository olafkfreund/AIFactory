---
status: draft
issue: 1973
intent: intent/2026-11-17-1973-github-app-docs.md
---

# Spec: GitHub App and Copilot review docs (#1973, #1701)

## Design

One canonical page, two thin pointers.

1. AIFactory `docs/docs/concepts/github-app.md` (canonical; also cited by `pr_endgame.py`), edits in place:
   - Section 1: the redirect to `http://localhost/?code=...` does not load; copy `code` from the address bar (single use, expires in 1 hour). Add `convert` output and the key mismatch: it writes Secret `aifactory-github-app` / key `private-key`, which is what the Helm `githubApp.secretName` path reads. A deployment that reads `AIFACTORY_GITHUB_APP_*` keys from a shared Secret (factory-gitops: `factory-secrets`) needs the raw PEM: `(umask 077; sed -n 's/^    //p' secret.yaml > private-key.pem)`; the extracted file is as sensitive as the Secret, so create it 0600 and delete it with `secret.yaml` afterwards.
   - Section 2: choose "Only select repositories"; a project whose repo is not covered cannot push; installation id from the settings URL or `GET /app/installations` with an App JWT.
   - Cutover: seed keys without printing them (`jq -n --arg ... --rawfile pem ... | kubectl patch secret ... --patch-file /dev/stdin`), also keep them in the declarative secret source, remove the PAT env line immediately, then verify (pod boots, no PAT in pod env, first task's PR authored by `app/<slug>`), then set `AIFACTORY_AGENT_TOKEN_ADMIN_POLICY=refuse`.
   - Copilot, as OPT-IN (owner decision 2026-11-17): the default reviewer stays `aifactory` (own engine); `copilot-ruleset` is never documented as the default. Record the observed result with its conditions: `probe-copilot` run for one deployment against a test PR with an installation token returned `HTTP 201; copilot_listed: False`, so Copilot was not requested through the App token there. Keep the existing caveats (missing entitlement or an already-reviewed PR give the same answer; negative is not proof for every setup). The probe POSTs a real review request: run it only on a test PR, before any ruleset is active. To opt in: set the project's reviewer to `copilot-ruleset` in Project settings (or `AIFACTORY_PR_REVIEWER`) AND activate the repo ruleset (`copilot_code_review`, `review_on_push=false`, `review_draft_pull_requests=false`, all branches; create with `gh api -X POST repos/OWNER/REPO/rulesets`, or enable an existing one with `gh api -X PUT repos/OWNER/REPO/rulesets/ID -f enforcement=active`). Without the ruleset no Copilot review arrives. Re-review on push is untested.
   - Key hygiene: keep the PEM in a password manager, delete loose copies; narrow the old PAT.
2. AIFactory `docs/docs/getting-started.md`: a short "Connect GitHub (optional)" step linking to the page.
3. factory-gitops `docs/aifactory-github-app-cutover.md`: rewrite as the verified runbook for this deployment (status of #367/#368, #369 reverted by #370, the sequence, gotchas, Copilot result). This is the only place that shows the deployment's App id 5797279 and installation 182861497, marked as this deployment's values.
4. Factory `docs/guides/onboarding.md`: one row/step on GitHub identity that links to the AIFactory page by URL (`https://aifactory.freundcloud.com/concepts/github-app`), no copy.

## Alternatives rejected

- A new standalone how-to page: duplicates the canonical page and drifts.
- Copying the steps into the Factory hub: same drift; link instead.
- Documenting Copilot via App token as supported: the observed probe did not list Copilot.
- Documenting `copilot-ruleset` as the default reviewer: the owner chose the own engine as default.

## Risks

- Docs drift from `scripts/github_app_setup.py`: each command and flag is checked against the script.
- A broken link fails the Docusaurus build (the route is `/concepts/github-app`, `routeBasePath` is `/`).
- Publishing one deployment's probe result as a universal limit: state the conditions.
- Secret values or owner ids in user docs: grep before commit.

## Verification

- AIFactory: `cd docs && npm ci && npm run build` (CI job "Build Docusaurus").
- Factory hub: the site's Jekyll or markdown lint as in its CI; check front matter and links.
- factory-gitops: markdown only; repo CI (`manifest-validate`, `secret-scan`) stays green.
- `grep -rn` for token or PEM patterns and for the ids outside factory-gitops.
