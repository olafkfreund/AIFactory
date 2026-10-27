---
status: approved
issue: 1960
author: olafkfreund
---

# Intent: agent identity trailers on the commits that reach the default branch

Factory#1960 (agent identity), next slice after AIFactory #1907.

## Problem

#1907 puts `Agent-Identity` / `Autonomy-Tier` / `Originator` trailers on the
two commits AIFactory itself makes inside a task worktree, behind
`AIFACTORY_AGENT_COMMIT_TRAILERS`. None of them reach the default branch:

- the PR is squash-merged (`server/services/approval.py` approve button,
  `server/services/pr_endgame.py` auto-merge) with GitHub's default message,
  which by repo default concatenates the branch's commit messages: the
  attribution is lost, and any trailer line the agent typed into its own
  commit message lands in the squash body and reads as genuine;
- the local `merge --no-ff` in `core/worktree.py` writes
  `aifactory: Merge <branch>` with no trailers.

## Proposed outcome

With the flag on, every task merge commit AIFactory creates carries the three
trailers, taken from the canonical spec copy's `requirements.json`, never from
the agent: the local merge commit, and the squash commit of both PR merge paths
(the squash body is set explicitly to the trailer block, so agent-typed
trailers in branch commits do not reach the default branch). Flag off: argv
byte-identical to today.

## Affected users and systems

AIFactory backend (`core/commit_trailers.py`, `core/worktree.py`) and
web-server (`services/approval.py`, `services/pr_endgame.py`,
`routes/worktree_merge.py`). No config, gitops or cluster change.

## Constraints

- Same flag, default off; off = no behaviour change.
- Values only from the canonical spec dir (`<project>/.aifactory/specs/<id>`),
  the same validation as #1907 (printable, non-empty strings).
- Must not lock out logins, touch credentials, gitops or deploy.

## Open questions

None for the owner. Decided by the lane (recorded in the spec): no commit-msg
hook in the agent session. AIFactory pins `core.hooksPath=/dev/null` for every
git it runs (`core/child_env.py` `_GIT_PINS`, a deliberate #1689 hardening),
and a client-side hook in a worktree the agent writes to is bypassable
(`--no-verify`, `commit-tree`, `--amend`, rewriting `.git/hooks`). With the
squash body set by AIFactory, agent-session commit messages no longer reach the
default branch on the PR paths, so a hook would add attack surface without
adding attribution.

## Remaining slices (Factory#1960, in order)

1. Branch protection: required reviews + code-owner reviews on the four repos
   (owner decision; interacts with `watch_and_finish` auto-merge).
2. Originator on the from-issue lane (cross-repo `FromIssueBody`) and the
   portal lane (needs an auth decision).
3. Agent as git author / GitHub App identity per agent (needs the App).
4. Generic `POST /github/pr/{n}/merge` (`routes/github.py`) carries no task
   context, so no trailers; covered only if it gains a task id.
5. Revocation and expiry of agent identities; evidence-store write check.
