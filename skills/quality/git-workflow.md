# git-workflow

> Source: curated best practices | 2026

---

# Git Workflow - small atomic commits, clear history, nothing dangerous merged

Version control history is documentation the future reads to understand why the code is the way it is. A clean history — small, focused commits with honest messages, each one a working state — lets you bisect a bug to a single change, revert one mistake without unpicking ten, and review a PR in one sitting. A messy history — giant "misc fixes" commits, secrets committed and "removed" later, a 4,000-line PR nobody can actually review — hides bugs and burns reviewer goodwill. The habits are cheap; the payoff is every time someone (including you) asks "why did this change".

## When to Activate

Use when committing, branching, or preparing a pull request:
- staging changes and writing a commit message
- deciding how to split work into commits or PRs
- opening or reviewing a pull request
- about to commit files that might contain secrets or generated junk
- cleaning up a branch before merge

## Principles and Practices

**One logical change per commit.** A commit should be a single coherent step that leaves the code working: one bug fix, one refactor, one feature slice. Do not mix a refactor with a behavior change — when a bug appears, you cannot tell which part caused it, and you cannot revert one without the other. Keep formatting-only changes in their own commit so they do not drown a real diff.

**Conventional commit messages.** A structured, greppable format that also drives changelogs and versioning:

```
<type>(<scope>): <imperative summary, ~50 chars>

<body: what changed and WHY, wrapped ~72 cols. The diff shows how.>
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `build`, `ci`. Write the summary in the imperative — "add retry to fetch", not "added" or "adds". The body explains the reasoning a reviewer or future maintainer needs; the code shows the mechanics. `fix: stuff` and `wip` are not messages.

**Atomic and working.** Every commit should build and pass tests on its own — that is what makes `git bisect` able to pin a regression to one commit, and what makes any commit safe to revert. Do not commit a half-applied change that only works together with the next one; squash those into one.

**Small, focused PRs.** A reviewable PR is small — a few hundred lines, one concern. Reviewers give real attention to 200 lines and rubber-stamp 2,000. Split large work into a stack of small PRs (refactor first, feature second) that each merge independently. A small PR ships faster, gets better review, and is easier to revert. If a PR needs a table of contents, it is too big.

**PR hygiene.** Title says what and why in one line. Description covers: what changed, why, how it was tested, and anything the reviewer should look at hardest. Link the issue. Keep the branch current with the base (rebase or merge per team convention) so CI tests what will actually land. Respond to every review comment. Do not merge red CI.

**Never commit secrets or junk.** No API keys, passwords, tokens, `.env` files, or credentials — ever. "Removing" a secret in a later commit does NOT remove it; it lives in history forever and must be rotated as compromised. Prevent it up front: a comprehensive `.gitignore`, a pre-commit secret scanner, and a review habit of reading your own diff before pushing. Do not commit build artifacts, `node_modules`, large binaries, editor files, or generated code that belongs in the pipeline.

```bash
git diff --staged      # read exactly what you are about to commit, every time
```

**Branch off main, keep branches short-lived.** Create a branch per task off the default branch; keep it alive days, not weeks — long-lived branches drift and produce painful merges. Name it for the work (`fix/login-timeout`, `feat/csv-export`). Delete it after merge.

**Rewrite only unpushed/local history.** Interactive rebase, squash, and amend are great for cleaning up a branch *before* it is shared — collapse "wip" commits into meaningful ones. Never rewrite history others have pulled (shared branches, main); a force-push there breaks everyone's checkout. `--force-with-lease` over `--force` even on your own branch, so you do not clobber a teammate's push.

**Commit often locally, curate before pushing.** Frequent local commits are a safety net; squash and reorder them into a clean story before the PR. The published history should read as if you did the work cleanly the first time.

## Anti-patterns

- Giant commits mixing feature + refactor + formatting — un-bisectable, un-revertable.
- Messages like `fix`, `wip`, `stuff`, `update`, or ones that restate the diff.
- Committing a broken intermediate state that only works with the next commit.
- 2,000-line PRs that get rubber-stamped because nobody can review them.
- Committing secrets/`.env`/keys and "removing" them later — history keeps them; rotate.
- Committing `node_modules`, build output, large binaries, or editor cruft (fix `.gitignore`).
- Force-pushing shared branches or main, clobbering others' work.
- Long-lived feature branches that drift for weeks and merge in pain.
- Merging with red CI, or pushing without reading your own staged diff.
