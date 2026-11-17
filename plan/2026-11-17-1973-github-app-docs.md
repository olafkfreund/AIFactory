---
status: approved
issue: 1973
spec: spec/2026-11-17-1973-github-app-docs.md
reviewers:
  - codex   # read-only review of the three diffs against this plan (agy is off)
---

# Plan: GitHub App and Copilot review docs (#1973, #1701)

Approved spec decisions, carried over. Docs only; no code or cluster change.

- Canonical page: AIFactory `docs/docs/concepts/github-app.md`, edited in place. Others link to it.
- Gotchas to add: manifest redirect `http://localhost/?code=...` does not load (copy `code` from the address bar; single use; 1 hour); `convert` writes Secret `aifactory-github-app` key `private-key`, but a deployment reading `AIFACTORY_GITHUB_APP_*` from a shared Secret needs the raw PEM: `(umask 077; sed -n 's/^    //p' secret.yaml > private-key.pem)`, deleted with `secret.yaml`; install on "Only select repositories" covering every project repo; installation id from the settings URL or `GET /app/installations` with an App JWT; `--name` if "AIFactory" is taken, `--org` for an org.
- Cutover in one window: seed keys without printing them (`jq -n --arg id .. --arg inst .. --rawfile pem private-key.pem '{stringData:{...}}' | kubectl -n NS patch secret SECRET --type merge --patch-file /dev/stdin`), also keep them in the declarative secret source, remove the PAT env line at once. Server refuses boot with a partial App set, any PAT (GITHUB_TOKEN, GH_TOKEN, GITHUB_PERSONAL_ACCESS_TOKEN, `github.tokenEnv` alias) or a `gh` hosts.yml token. It mints 1 h installation tokens, refreshed every 30 min, and exports GH_TOKEN/GITHUB_TOKEN in-process.
- Verify: pod boots, logs clean, no PAT env, first task's PR authored by `app/<slug>`. Then `AIFACTORY_AGENT_TOKEN_ADMIN_POLICY=refuse`. Reduce the old classic PAT to `repo, workflow, copilot, read:org` with an expiry. PEM in a password manager; delete loose copies.
- Copilot is OPT-IN (owner decision 2026-11-17). Default reviewer stays `aifactory`; never document `copilot-ruleset` as default. Observed: `probe-copilot` on one deployment, with an App installation token on a test PR, returned `HTTP 201; copilot_listed: False` (keep caveats; probe POSTs a real request, run on a test PR before any ruleset is active). Opt in = project reviewer `copilot-ruleset` (Project settings or `AIFACTORY_PR_REVIEWER`) AND repo ruleset active (`copilot_code_review`, `review_on_push=false`, `review_draft_pull_requests=false`, all branches; `gh api -X POST repos/OWNER/REPO/rulesets`, or `-X PUT .../rulesets/ID -f enforcement=active`). Re-review on push untested.
- Owner ids (App 5797279, installation 182861497) only in the factory-gitops runbook, marked as this deployment's values. No secrets anywhere.

## Steps

1. AIFactory `docs/docs/concepts/github-app.md` (sections 1, 2, 3, "Cutover order", "Copilot review"): apply the decisions above; fix the existing "ruleset ... default"-style wording if any → verify: compare every command and flag in the page with `scripts/github_app_setup.py` (`manifest --out --org --workflows --name`, `convert CODE --out`, `probe-copilot --repo --pr --app-id --installation-id --key-file`); `cd docs && npm ci && npm run build`; `git grep -nE '5797279|182861497|BEGIN .*PRIVATE|ghp_|github_pat_'` in docs is empty.
   Traps: Docusaurus fails on broken links; keep front matter; the page is cited by `pr_endgame.py`, keep its headings (grep `github-app` in `apps/` first).
2. AIFactory `docs/docs/getting-started.md`: add "Connect GitHub (optional)" before "What's next", link `./concepts/github-app` → verify: docs build.
   Traps: relative link style matches the existing `./demo` links.
3. factory-gitops `docs/aifactory-github-app-cutover.md`: rewrite as the verified runbook (done state of #367, #368; #369 reverted by #370; sequence; gotchas; Copilot opt-in; deployment ids marked) → verify: `git grep -nE 'BEGIN .*PRIVATE|ghp_|github_pat_'` empty; repo CI (`secret-scan`, `manifest-validate`) green.
   Traps: link the AIFactory page by full URL; do not touch manifests. The ids may appear here only.
4. Factory `docs/guides/onboarding.md`: add a GitHub identity row/step, link `https://aifactory.freundcloud.com/concepts/github-app` → verify: the site's CI or `bundle exec jekyll build` if available; links resolve; the same id/PEM/token grep over the changed file is empty.
   Traps: Jekyll Liquid: use `relative_url` for in-site links only.
5. Open three PRs (AIFactory base dev, factory-gitops base main, Factory base main); each body links intent, spec, plan by full URL; wait for required checks green on the head SHA; do not merge.

## Tests

Docs build passes; id/PEM/token greps empty over every changed file except the two ids in the runbook; PR checks green on the head SHA.

## Rollback

Revert the doc commits; nothing else changed.
