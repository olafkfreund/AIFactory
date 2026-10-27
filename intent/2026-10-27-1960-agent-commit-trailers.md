---
status: approved
issue: 1960
author: olafkfreund
---

# Intent: agent identity, originator and tier as git trailers on factory commits

Epic: olafkfreund/Factory#1960. Parent intent: Factory `intent/2026-10-26-1960-agent-git-identity.md` (merged in Factory #3661), slice 3 ("Git trailers on branch commits"). Builds on AIFactory #1887 (`ApiKey.agent_identity`) and #1897 (`originator` and `agent_identity` recorded in the spec's `requirements.json` at intake).

## Problem

Branch commits a factory agent makes are authored `AIFactory <aifactory@container>`. That names no key, no tier and no human. Since #1897 the spec's `requirements.json` holds `originator` and `agent_identity`, and from-issue intake records `intake.autonomy_tier`, but no commit carries any of them. The record of "which agent, at what tier, started by whom" stops at the task and never reaches git.

## Proposed outcome

- With a new flag on (`AIFACTORY_AGENT_COMMIT_TRAILERS=1`), the commits AIFactory itself writes on a branch carry git trailers taken from the spec's `requirements.json`:
  - `Agent-Identity: agent:aifactory/<key_id>`
  - `Autonomy-Tier: <intake.autonomy_tier>` (`low`/`medium`/`hard`, as recorded)
  - `Originator: <originator>` (as recorded at intake, e.g. `github:<login>`)
- Each trailer is written only when its value is recorded. An unknown value writes no trailer, never a placeholder.
- The originator is written next to the agent identity, never in place of it: the commit author stays the agent.
- Flag off (the default): commits are byte-for-byte what they are today.

## Commits covered by this slice

The two commit paths AIFactory's own code runs:

- `core/worktree.py` `commit_in_worktree` (parallel-wave subtask commits).
- `agents/utils.py` `commit_uncommitted_changes` (the safety-net commit).

Commits the coding agent makes itself inside its session (`git commit -m "aifactory: ..."` from the prompt) are not covered. Git has no environment variable or config that adds trailers to a plain `git commit -m`; covering them needs a `commit-msg` hook in the worktree or the trailers on the squash body at merge. That is the next slice.

## Remaining slices, in order

1. This slice: trailers on AIFactory-written branch commits, default off.
2. Trailers on agent-session commits (worktree `commit-msg` hook) and on the squash-merge body, so they reach the default branch.
3. Hard gate before the key ceiling reaches the merge path: unceiled keys default to `hard` or are refused.
4. Factory GitHub App opens and merges agent PRs (needs the owner to create the App; AIFactory #1701).
5. Required reviews in branch protection on the four repos.
6. Revocation and expiry for agent identities; 6b: lock `_SAFE_GIT_KEYS`.
7. Portal intake originator: last or never, pending the owner's auth decision.

## Affected users and systems

AIFactory backend only: the two commit paths above and their two call sites (`agents/session.py`, `agents/parallel_integration.py`). Target repos see extra trailer lines only when the flag is on. No web-server, database, gitops or cluster change.

## Constraints

- Flag defaults to off; off means no change in behaviour.
- Does not depend on the GitHub App identity, which does not exist yet.
- No new identity fields: reuse `agent_identity`, `originator` and `intake.autonomy_tier` as #1887/#1897 record them.
- No placeholder values. A value containing a line break is not written (it could forge further trailers).
- A missing or unreadable `requirements.json` must not fail a commit, least of all the safety-net commit.

## Open questions

None.
