---
status: approved
issue: 1588
spec: spec/2026-09-23-1588-techdocs-dependabot.md
---

# Plan: CI regenerates the dependency page on a bot PR, and only that page

## Decisions (carried from the approved intent and spec)

- A new `regenerate-for-bot` job in `techdocs.yml`, for `pull_request` events whose author is
  `dependabot[bot]`, with **job-level** `contents: write` (the workflow default stays read).
- It runs the **base branch's** copy of `scripts/generate-techdocs-deps.py`, so the code that
  runs with a writable token is trusted, then restores the script.
- It commits and pushes **`docs/dependencies.md` only**. `openapi.yaml` is never written back.
- `refresh-and-validate` keeps its strict check; human PRs are unaffected.
- Fallback if this proves awkward: drop versions from the table (spec alternative (b)).

## Steps

1. `.github/workflows/techdocs.yml`: add `regenerate-for-bot` above `refresh-and-validate`:
   - `if: github.event_name == 'pull_request' && github.event.pull_request.user.login == 'dependabot[bot]'`;
   - `permissions: {contents: write}`; `timeout-minutes: 10`;
   - checkout with `ref: ${{ github.event.pull_request.head.ref }}`,
     `fetch-depth: 0` (the base copy in the next step needs history);
   - `git fetch origin ${{ github.base_ref }}` then
     `git checkout origin/${{ github.base_ref }} -- scripts/generate-techdocs-deps.py`;
   - `python3 scripts/generate-techdocs-deps.py`;
   - `git checkout -- scripts/generate-techdocs-deps.py` (restore before staging anything);
   - commit + push **only** `docs/dependencies.md`, as `github-actions[bot]`, skipping cleanly
     when `git diff --quiet -- docs/dependencies.md`; a push failure is an error, never a
     silent skip.
   → verify: `actionlint`; step 4's dry run.
2. Same file: `refresh-and-validate` gains `needs: regenerate-for-bot` **and**
   `if: always()`. Without `always()` a skipped bot job (every human PR) would skip the gate
   too — a gate that disappears for humans is worse than the bug being fixed.
   → verify: `actionlint`; a human PR still runs the gate (step 4).
3. Header comment on the new job: why it exists (#1588: the gate is red by construction for a
   frontend bump because Dependabot cannot run the generator), why the base copy is checked
   out, and why `openapi.yaml` is excluded.
4. **Dry run before merge** (workflows cannot be tested locally):
   - `actionlint .github/workflows/techdocs.yml`;
   - run the generator locally against a simulated bump — edit a version in
     `apps/frontend-web/package.json`, run `python3 scripts/generate-techdocs-deps.py`, confirm
     the only changed file is `docs/dependencies.md`, then revert both;
   - push this branch and confirm `refresh-and-validate` still runs on it (a human PR:
     `regenerate-for-bot` skipped, gate green).
   → verify: the recorded outputs.
5. **Prove it on a real bot PR after merge** — this is the acceptance evidence and cannot be
   faked earlier: the next Dependabot PR touching `apps/frontend-web/package.json` must go
   green **without a human commit**. If none is open, `gh workflow run` cannot simulate it
   (the author condition), so the honest fallback is to wait for the next bump and check then.

## Tests

No unit tests for workflows. Evidence:

```bash
actionlint .github/workflows/techdocs.yml
python3 scripts/generate-techdocs-deps.py && git status --short   # only docs/dependencies.md
```
plus, on the first Dependabot frontend bump after merge:

```bash
gh pr view <n> --json commits --jq '.commits[]|.authors[0].login + ": " + .messageHeadline'
# expect a github-actions[bot] regeneration commit, and NO human one
gh pr checks <n>    # techdocs gate green
```

## Rollback

Revert the commit. The gate returns to failing on bot PRs, which is today's behaviour, and the
manual regeneration commit is the workaround again.
