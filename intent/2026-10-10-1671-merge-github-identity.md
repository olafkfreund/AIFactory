---
status: draft
issue: 1671
author: olafkfreund
---

# Intent: AIFactory opens PRs under its own GitHub identity so a human approval can count

## Problem

Since #1663 (PR #1687), the `human-approval` system gate clears only when
someone approves the PR's head commit who is not the PR author and is not a
Bot (`human_approval_head`, `apps/web-server/server/services/pr_endgame.py:750-820`,
check at `:805-812`). That part works as designed.

The trouble is who opens the PR. AIFactory has one GitHub identity, the
ambient `gh` login / `GITHUB_TOKEN` in the pod
(`charts/aifactory/templates/deployment.yaml:592-598`). In practice that is
the maintainer's own User account. All of these run under it:

- `gh auth setup-git`, `git push` and `gh pr create` in `create_pr`
  (`pr_endgame.py:532-598`). Every call goes through `_default_runner`
  (`:66-68`), which passes no env of its own.
- the manual route `create_pr_from_task` (`routes/pr.py:57`, `:464`), via
  `services/gh.py`.
- the kubejob build Job, which is handed the same `GITHUB_TOKEN`/`GH_TOKEN`
  (`build_backend.py:223-225`, `:288-289`).

So the only human who could approve is also the PR author. GitHub does not
let anyone approve their own PR, and the gate excludes the author anyway.
Every task gated on `human-approval` now waits in `watch_and_finish`
(`pr_endgame.py:1104-1109`) until it times out, and someone has to merge it
by hand. CHANGELOG.md:8-9 already says so: "Until AIFactory has its own bot
identity, every such task merges by hand."

`GITHUB_BOT_TOKEN` exists, but only the separate backend GitHub runner reads
it (`apps/backend/runners/github/runner.py:93`). Nothing under
`apps/web-server` uses it.

## Proposed outcome

- AIFactory opens and pushes its PRs as an identity that is not the
  maintainer.
- When the maintainer approves the head commit, that approval counts. A
  `human-approval` task then merges with no manual step:
  `[pr-endgame] ... approved by <maintainer>`, then the merge.
- The gate stays non-self-satisfiable. Nothing AIFactory or its agents hold
  can produce an approval that counts.
- Installs that configure no separate identity behave exactly as today.
- CHANGELOG, the #1663 spec, `environment-reference.md` and the autonomy
  matrix all describe the new state.

## Affected users and systems

- The maintainer, and anyone running AIFactory with `human-approval` gates
  and `AIFACTORY_AUTO_MERGE` on.
- `pr_endgame.py` (`create_pr`, `merge_pr`, `request_copilot_review`,
  `watch_and_finish`), `merger.py:368`, `routes/pr.py`, `services/gh.py`,
  `build_backend.py`, and the build Job's push.
- Agent env scrubbing (`core/auth.py`), Helm chart values and secrets.
- `merge/merge_policy.py` (`HUMAN_ONLY_GATES`). It reads the result and
  should not need a change.
- Docs and compliance: CHANGELOG.md, `spec/2026-10-08-1663-contract-self-satisfy-gates.md`
  (decision B, the rejected "configure the bot login" alternative),
  `docs/docs/environment-reference.md:144`, the autonomy matrix
  (`scripts/gen_autonomy_matrix.py`, enforced by a required check), and
  `docs/compliance/control-objectives.toml`.
- Every target repo AIFactory opens PRs on. Each needs the new identity to
  have write access.

## Constraints

- **No counting approver inside AIFactory.** If the maintainer's PAT stays in
  the pod for any purpose, the server or an agent can approve as the
  maintainer. That reopens the hole #1663 closed. AIFactory must also not
  hold two User-type identities that could approve each other's PRs.
- **Every PR-opening and push path switches.** That means the endgame,
  merger, the manual route and the build Job. A path left on the maintainer
  fails silently: its PR can never clear.
- **Agents never see the new credential.** The current scrub does not match
  `GITHUB_BOT_TOKEN` or new `*_TOKEN` names, and non-Claude agents inherit
  the runner env (#1692). A GitHub App private key must never reach agents
  or build Jobs.
- **The identity choice cannot come from anything the agent can write**:
  the spec dir, the contract, `task_metadata.json` (#1672) or the project
  `.env`.
- **Least privilege.** The identity needs contents and pull-request write,
  plus merge/update-branch. It does not need admin.
- **Backward compatible.** A single-user install with only `GITHUB_TOKEN`
  keeps today's behaviour. Chart default `providers.github: false` stays.
- **Copilot review keeps working**, or is explicitly scoped out. It is
  unverified whether an App token or an unlicensed machine user can request
  `copilot-pull-request-reviewer[bot]`.
- **Expiry and "last pusher" hold up.** App tokens last one hour, but a PR's
  lifecycle can be longer. Repos that require approval of the most recent
  push must not see the maintainer as the last pusher.
- **CI still runs.** Required checks must trigger on PRs opened by the new
  identity.
- **Correct with several replicas and many build Jobs.** Do not assume
  `replicaCount: 1`.
- **Sequence with open work.** PR #1691 (`child_env` keep-list strips
  unknown `*_TOKEN`s) and #1688 (`GH_TOKEN` moves off env) rewrite the same
  spawn sites. RFC-0020's GitHub App install model should not be forked.
- **Docs and compliance rows change in the same PR**, and a required check
  enforces the autonomy matrix.

## Open questions

1. GitHub App (Bot type, short-lived tokens, private key in the pod), machine
   user (User type, long-lived PAT, an extra account), or either, chosen per
   deployment?
2. Does the maintainer's PAT leave the pod entirely? If something (the MCP
   github provider, intake, UI routes) still needs it, how is approving as
   the maintainer prevented? Or is that risk accepted and recorded in the
   autonomy matrix?
3. Scope of the new identity: only push and `gh pr create`, or also Copilot
   requests, update-branch and merge?
4. Should a PR opened by hand from the UI stay authored by the human on
   purpose?
5. One global identity, or per project/tenant through stored credentials
   (`github_app` kind is reserved, not built), aligned with RFC-0020?
6. Reuse `GITHUB_BOT_TOKEN` and share one identity with the backend GitHub
   runner, or use a new name?
7. Land after PR #1691 and #1688, or ship an env-based interim that #1688
   reworks?
8. With no bot identity configured, keep manual merge silently, or warn or
   refuse when `human-approval` gates are in use?
9. If the bot cannot request Copilot reviews, should `reviewer=copilot` use
   the human identity for that request, be unavailable, or fall back?
10. With a machine user, should that user's own approvals ever be excluded
    explicitly? That would revisit the #1663 decision not to configure the
    bot login.
11. Who provisions the identity: Helm values and secret key name, App
    registration or manifest, per-repo permissions?
12. What counts as done: the maintainer's approval merges a human-approval
    task end to end on AIFactory's own repo only, or on other target repos
    too?
