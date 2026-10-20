---
status: approved
issue: 1834
author: olafkfreund
---

# Intent: changelog entries for PRs that shipped without one in 3.10.2/3.10.3

## Problem

The 3.10.3 release (#1833) lists PRs that shipped with no `CHANGELOG.md`
entry: #1824, #1818, #1829, #1828, #1823, #1819, #1831 and #1827. #1810
shipped in 3.10.2 and has no entry either. Operators reading the changelog
miss behaviour changes, some of them breaking (gated merges hold until
TFactory returns `source_sha`, package-runner script shorthands are refused,
runners exit when prctl fails).

## Proposed outcome

Each of the nine PRs has an entry, in the existing style, under the release
it actually shipped in (checked with `git merge-base --is-ancestor` against
the `v3.10.2` and `v3.10.3` tags).

## Affected users and systems

`CHANGELOG.md` only. Readers of the release notes; no code, no runtime.

## Constraints

- Docs only. Do not reword existing entries.
- The versioned sections (`## 3.10.2`, `## 3.10.3`) exist only on `main`;
  `dev` still carries everything under `[Unreleased]`. So this change targets
  `main`, not `dev` (the release flow merges `main` into each release branch,
  so the entries carry forward).

## Open questions

None.
