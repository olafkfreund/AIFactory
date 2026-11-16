---
status: approved
issue: 1973
author: olafkfreund
---

# Intent: refuse an admin-scoped GitHub token for agent children; App manifest and gitops wiring

## Problem

Checked against `origin/dev` on 2026-11-16:

- **#1973 (admin-scoped token in agent children).** #2030 added `AIFACTORY_AGENT_GITHUB_TOKEN` and strips the control plane's token from non-runner children. But `runner_github_env()` (`server/utils/subprocess_env.py`) still falls back to the control plane's own token when that variable is unset, and the live Deployment does not set it. So runner subprocesses, PR review and build Jobs still receive the fleet PAT (Kubernetes spec Jobs already drop it, `spec_job_backend.py:389-392`), which carries `admin` and `delete_repo`. Nothing checks what a token can do before it goes to a child.
- **The App path exists but cannot be adopted.** `scripts/github-app-manifest.json` asks only for contents, pull requests and metadata. The owner's 2026-10-31 decision also wants Issues R/W and Checks R. The Helm chart already has default-off App wiring (`charts/aifactory/templates/deployment.yaml:663-705`), but the gitops aifactory Deployment, which is what runs, has no App variables, so the owner has nothing to switch on.
- **#1700 and #1701** have their code merged per the issue threads (per-org installation tokens: #2039, #2053; Copilot probe tooling shipped); I do not re-verify them here beyond reading `github_app.py`. What is left is owner-only: create the App, store the secret, run one probe with the key on a workstation, run a second-installation live test.

## Proposed outcome

- A classic PAT whose scopes include an admin class (`delete_repo`, `admin:*`) is never passed to an agent child when the operator sets the policy to `refuse`. In the default `warn` mode the same finding is logged once per token, naming scopes, never the token. Installation tokens and fine-grained tokens are not scope-checked (GitHub gives no scope header for them) and pass.
- The manifest requests exactly the owner-decided permissions; the docs list them and the owner steps.
- The gitops aifactory Deployment carries optional, default-off App variable wiring, and a runbook gives the order of the cutover so a restart cannot crash-loop.
- #1973, #1700, #1701 get a decision comment; each stays open until its owner-only step is done, because closing them needs the live App.

## Affected users and systems

AIFactory `apps/web-server/server/utils/subprocess_env.py`, `scripts/github-app-manifest.json`, docs; factory-gitops `apps/aifactory/manifests/manifests.yaml` and a runbook. In-pod agent sessions (build, spec creation, PR review, build Jobs).

## Constraints

- Default behaviour unchanged for the live pod: `warn`, not `refuse`, because the live PAT is admin-scoped and `refuse` would stop agent pushes until the App or a narrow token is in place.
- Never log or return a token; scope names only. A failed scope probe in `refuse` mode fails closed (no token to children).
- `start()` keeps refusing to boot when a PAT and the App are both present; the runbook must make the order explicit.
- Vendored-file and generated-matrix traps: do not touch `artifact_store.py` or hand-merge the autonomy matrix.
- No secret handled by an agent; creating the App, PEM, installation id and secret are the owner's.

## Open questions

None. Decision by owner delegation (2026-11-16): one owner-managed App (per the 2026-10-31 decision), `warn` default with `refuse` as the post-cutover setting, because it is the least risky to operate.
