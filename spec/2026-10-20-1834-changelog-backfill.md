---
status: draft
issue: 1834
intent: intent/2026-10-20-1834-changelog-backfill.md
---

# Spec: changelog entries for PRs that shipped without one in 3.10.2/3.10.3

## Design

Release placement, from `git merge-base --is-ancestor <merge commit> <tag>`:

| PR | Merge commit | In v3.10.2 | In v3.10.3 | Section |
| --- | --- | --- | --- | --- |
| #1810 | c8249321 | yes | yes | 3.10.2 / Security |
| #1824 | afef396c | no | yes | 3.10.3 / Security |
| #1829 | 59724f25 | no | yes | 3.10.3 / Security |
| #1818 | bea91102 | no | yes | 3.10.3 / Security |
| #1823 | b0f55dc9 | no | yes | 3.10.3 / Security |
| #1828 | e7774e76 | no | yes | 3.10.3 / Fixed (new) |
| #1819 | ced84655 | no | yes | 3.10.3 / Fixed (new) |
| #1831 | 194b7708 | no | yes | 3.10.3 / Fixed (new) |
| #1827 | 6ccc00ad | no | yes | 3.10.3 / Fixed (new) |

Each entry follows the existing format: a bold one-line title ending with the
issue number(s) the PR closes, then a short wrapped paragraph saying what
changed for the operator, with breaking behaviour called out. Text is taken
from each PR body (`gh pr view N`). Security entries go after the existing
3.10.3 ones; #1829 (A1) precedes #1818 (A2) as they merged. 3.10.3 has no
`### Fixed` section, so one is added after `### Security`, matching the
section order used in 3.10.2.

The change goes to `main` (branch from `origin/main`), the only branch
with the `## 3.10.x` headings.

## Alternatives rejected

- Base on `dev`: `dev`'s CHANGELOG has no 3.10.x headings (only
  `[Unreleased]`), so the entries could not go under their release.
- Merge `main` into a dev branch: pulls 54 release commits (version bumps)
  into a docs PR.
- Put them under `[Unreleased]`: they have shipped; wrong release.

## Risks

Doc-only. A wording error misdescribes a change; mitigated by writing from the
PR bodies. The next release branch merges `main`, so no conflict beyond the
usual CHANGELOG merge.

## Verification

- `for n in 1810 1818 1819 1823 1824 1827 1828 1829 1831; do grep -c "#$n" CHANGELOG.md; done` gives at least 1 each.
- `awk` check that #1810 sits between `## 3.10.2` and `## 3.10.1`, the
  others between `## 3.10.3` and `## 3.10.2`.
- `git diff --stat origin/main` touches only CHANGELOG.md and the artifacts.
