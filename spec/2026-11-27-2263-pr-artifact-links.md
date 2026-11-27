---
status: draft
issue: 2263
intent: intent/2026-11-27-2263-pr-artifact-links.md
---

# Spec: list `provenance.source_artifacts` in the auto-PR body

## Design

All the code is in `apps/web-server/server/services/pr_endgame.py`, in `_pr_title_body` (~line 1905).

**Source.** `requirements.json` → `provenance.source_artifacts`. `trusted_plan.py` (~1199) copies it
from the signed PFactory contract. Each item is a dict with `stage`, `path`, `blob_sha`,
`approve_commit_sha`, `approve_commit_author` and `attested_by`. Only `stage` and `path` are read.

**Validation.** A module constant:

```python
_ARTIFACT_PATH = re.compile(r"(intent|spec|plan)/[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.md")
```

For each stage in the order `intent`, `spec`, `plan`, take the first item where:
- `item` is a dict;
- `item["stage"] == stage`;
- `item["path"]` is a `str` that `_ARTIFACT_PATH.fullmatch`es;
- the path starts with `f"{stage}/"`.

Anything else is skipped silently. The record is client-attested, so these values are untrusted
text, and the regex leaves no room for markup, newlines or a `Fixes #` line.

**Output.** When at least one stage qualifies, the body gains this block after the description and
attribution lines, and before `Fixes #N`:

```text

**Artifacts**
- intent: `intent/<slug>.md`
- spec: `spec/<slug>.md`
- plan: `plan/<slug>.md`
```

A stage with no valid item is left out. With no `source_artifacts`, or none valid, the body is
byte-identical to today's.

The paths are relative to the PR's own repo, so a reviewer finds them in "Files changed" or the
tree. They are not URLs, because `_pr_title_body` doesn't know the provider (GitHub, GitLab or
Azure).

## Alternatives rejected

- **Permalinks built from `provenance.repo` and `approve_commit_sha`:** these would be
  GitHub-specific, and the body builder has no provider. It's a possible follow-up if the plain
  paths prove too little.
- **Searching the worktree's `intent/`, `spec/` and `plan/` for the issue slug when there is no
  provenance:** tasks without `source_artifacts`, from plain issue intake, have no approved
  triple to find. Guessing by slug could link the wrong task's files.
- **Relative Markdown links (`[intent](intent/x.md)`):** GitHub resolves these against the PR URL,
  not the repo tree, so they break.

## Risks

- **`requirements.json` sits in the spec directory, which the build agent can write.** A forged
  list can change only these three regex-bounded lines of the PR body. It cannot add markup,
  closing keywords or an evidence block.
- **No change for other tasks:** without the key, the code path is the same as today.

## Verification

New tests in `apps/web-server/tests/test_pr_endgame.py`:

| Case | Expected |
|---|---|
| all three valid items, plus `issue_number` | the body has the block with the three paths in stage order, and `Fixes #N` comes after it |
| items out of order (plan, intent, spec) | the block is still in intent, spec, plan order |
| a path with a newline, `../`, a backtick, the wrong stage prefix, or a non-`.md` file | that stage is left out |
| `source_artifacts` missing, not a list, or with no valid items | the body equals today's (no `**Artifacts**`) |
| no `description` (default body) with valid items | the block is still added |

Then:

- The existing `test_pr_endgame.py` and `test_pr_verify_evidence.py` tests pass.
- The ruff and mypy ratchets are clean.
- CI is green.
- Live: after the AIFactory release, the P1 harness S4 shows `triple_linked: true`.
