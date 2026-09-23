---
status: draft
issue: 1588
intent: intent/2026-09-23-1588-techdocs-dependabot.md
---

# Spec: CI regenerates the dependency page on a bot PR, and only that page

## Design

Option **(a)** from the intent, narrowed to the one file a bot can make stale.

A new job in `techdocs.yml`, `regenerate-for-bot`, runs **before** the existing
`refresh-and-validate` gate and only when:

```
github.event_name == 'pull_request' && github.event.pull_request.user.login == 'dependabot[bot]'
```

It has job-level `permissions: contents: write` (the file-level default stays `contents: read`,
so nothing else gains write). It:

1. checks out the PR's head branch (a Dependabot branch in this repo, never a fork);
2. **replaces the generator with the base branch's copy** —
   `git checkout origin/${{ github.base_ref }} -- scripts/generate-techdocs-deps.py` — so the
   script that runs with a writable token is trusted code, not whatever the branch contains.
   This satisfies the intent's constraint directly rather than relying on "Dependabot only
   edits manifests";
3. runs `python3 scripts/generate-techdocs-deps.py` (stdlib only — no install, no app import);
4. restores the script (`git checkout -- scripts/...`) so it can never be committed;
5. commits **`docs/dependencies.md` alone** and pushes, or exits cleanly when there is no diff.

`refresh-and-validate` is unchanged: it still regenerates both files and fails on any drift.
For a human PR, nothing changes at all.

**`apps/web-server/openapi.yaml` is deliberately not written back.** It is the API contract;
regenerating it needs both requirement sets installed and an app import, and it must stay a
human decision (#1569 regenerated it by hand for exactly this reason). A Dependabot bump
cannot make it stale anyway.

## Alternatives rejected

- **(b) Drop versions from the table.** It ends the drift with no token and no write-back, and
  it stays the fallback if the write-back proves awkward. Rejected as the primary because the
  versions are most of the table's value: "what do we depend on, at what version" is the
  question TechDocs is answering.
- **(c) Generate at publish time, stop committing the file.** Also ends the drift, but the
  table stops being visible in review and in the repo, and the gate that proves the published
  docs match the code disappears with it.
- **Skip the gate for bot PRs.** The published table would then quietly drift from reality —
  the outcome the intent rules out.
- **Let the bot run the generator itself.** Dependabot runs no repo code; there is no such hook.

## Risks

- **A push to a Dependabot branch stops Dependabot rebasing it.** That is already true of the
  hand-pushed regeneration commits this replaces (#1536, #1531), so it is not new — but it
  means a superseded bump needs the PR recreated rather than rebased.
- **Loop safety:** the push triggers another run; the second run regenerates, finds no diff and
  exits without pushing. The loop terminates after one extra run.
- **A writable token on a bot-triggered event.** Precedent: `dependabot-digest-automerge.yml`
  already runs on `pull_request` with `contents: write` and has a successful run on a Dependabot
  PR. The base-copy step above is what keeps that safe here.
- **Fork PRs** would have a read-only token and no writable branch. Dependabot PRs are in-repo,
  and the job's condition covers only Dependabot, but the job must fail loudly (not silently
  skip) if it ever cannot push.

## Verification

- **The failing case, end to end:** re-open or re-run a frontend bump (#1531/#1536 are the real
  examples) with the job in place — the bot commit lands on the branch and the gate goes green
  with no human push.
- A **human** PR that forgets to regenerate still fails the gate (unchanged behaviour).
- A Dependabot PR touching **only lock files** produces no diff, so the job pushes nothing.
- The job **never** commits `scripts/generate-techdocs-deps.py` or `openapi.yaml` — asserted by
  inspecting the pushed commit's file list in the test run.
- `actionlint` clean.
