---
status: draft
issue: 1834
spec: spec/2026-10-20-1834-changelog-backfill.md
---

# Plan: changelog entries for PRs that shipped without one in 3.10.2/3.10.3

Approved decisions (from the spec):

- Branch from `origin/main`, PR against `main`: only `main` has the
  `## 3.10.x` headings. `dev` is not touched.
- Placement (checked with `git merge-base --is-ancestor` against the tags):
  #1810 in 3.10.2; #1824, #1829, #1818, #1823, #1828, #1819, #1831, #1827 in 3.10.3.
- 3.10.3 Security gains #1824, #1829, #1818, #1823 (in that order, #1829
  before #1818 as they merged). A new `### Fixed` after 3.10.3's Security
  holds #1828, #1819, #1831, #1827. 3.10.2 Security gains #1810 after #1696.
- Format: `- **<title> (#<issue>).**` then a wrapped paragraph from the PR
  body; breaking behaviour named. Existing entries are not reworded.

## Steps

1. CHANGELOG.md:37 (end of the #1762 entry, before `## 3.10.2`): append the
   four Security entries and the new `### Fixed` with four entries.
   → verify by `awk '/^## 3.10.3/,/^## 3.10.2/' CHANGELOG.md | grep -oE '#18(18|19|23|24|27|28|29|31)' | sort -u | wc -l` = 8.
   Traps: keep the blank line before `## 3.10.2`; issue numbers in titles
   are the closed issues (#1766, #1761, #1746/#1747, #1812, #1757, #1704/#1705/#1714, #1737, #1820), PR numbers in the body.
2. CHANGELOG.md:123 (end of the #1696 entry, before `### Changed` of 3.10.2):
   append the #1810 Security entry.
   → verify by `awk '/^## 3.10.2/,/^## 3.10.1/' CHANGELOG.md | grep -c '#1810'` = 1.
   Traps: none.

Coder: the lead lane assigned agy, but this is one file and two prose
inserts, so the lane agent writes it (managed rule: under three steps and
files, no handoff).

## Tests

`git diff --stat origin/main` lists only CHANGELOG.md and the three artifacts;
pre-commit passes.

## Rollback

`git revert` the step commit.
