---
status: draft
issue: 2258
intent: intent/2026-11-26-2258-tf-writeback-head.md
---

# Spec: accept TFactory's own test write-back commit as the verified head

## Design

### 1. `tfactory_verdict` (`apps/backend/pfactory/tfactory_client.py`, around line 1279)

A new keyword-only parameter `head_parent: Callable[[str], str | None] | None = None`. It returns
the single parent SHA of a commit, or `None`.

In the task check, `task.get("source_sha") == head` is replaced by
`_verified_at(task, status_json, head, head_parent)`. That check runs after `status_json` is known
to be a dict. The new helper:

```python
def _verified_at(task, status_json, head, head_parent) -> bool:
    source = task.get("source_sha")
    if not head or not isinstance(source, str) or not source:
        return False
    if source == head:
        return True
    writer = status_json.get("git_writer")
    return (
        head_parent is not None
        and isinstance(writer, dict)
        and writer.get("ok") is True
        and writer.get("dry_run") is not True
        and writer.get("commit_sha") == head
        and head_parent(head) == source
    )
```

- `git_writer` is TFactory's record from its own API, the same trust source as `source_sha`. Its
  fields are `ok`, `dry_run` and `commit_sha`.
- The parent check proves that the head is TFactory's commit made directly on the verified
  build. A build commit pushed between verify and write-back makes the parent differ, and the
  result is `None`.
- Without `head_parent`, the behaviour is today's: exact SHA only.
- If `head_parent` raises, the existing `except` returns `None`.

### 2. The caller (`_build_merge_gate` in `apps/web-server/server/services/pr_endgame.py`, around line 2157)

It passes `head_parent=lambda sha: _commit_parent(owner, name, sha, runner)`. `_commit_parent` is
a new helper next to `_pr_head`:

- it runs `gh api /repos/{owner}/{repo}/commits/{sha} --jq '[.parents[].sha]|join(" ")'`;
- it returns the parent only when there is exactly one;
- otherwise, or on any error, it returns `None` and never raises.

A merge commit as head therefore never qualifies.

## Alternatives rejected

- **Trust `git_writer.commit_sha` without a parent check.** This is unsafe: `git_writer` fetches the
  branch tip before it commits, so a racing build push would merge unverified code.
- **TFactory records the parent.** That needs a TFactory change and release, and AIFactory would
  still be trusting a reported parent rather than checking the actual commit.
- **Re-verify after the write-back.** That loops: each verify writes another commit.
- **Turn off the TFactory write-back.** The accepted tests would then never reach the PR.

## Risks

- **An extra GitHub API call per gate poll**, made only when the SHAs differ and the `git_writer`
  record matches.
- **A tampered `git_writer` record would need TFactory's API to lie.** That is the same trust that
  `source_sha` already relies on.
- **The write-back commit's content is not re-checked.** It holds TFactory's tests, which TFactory
  ran itself, and host CI still runs on the head (the `ci-green` gate). Accepted.

## Verification

Unit tests in `apps/backend/pfactory/test_tfactory_verdict.py`:

| Case | Expected |
|---|---|
| head == `git_writer.commit_sha`, `ok`, parent == `source_sha` | `pass` / `fail` as the verdicts say |
| same, but the parent differs | `None` |
| same, but `head_parent` is not given | `None` |
| `ok: false`, or `dry_run: true`, or `commit_sha` differs | `None` |
| `head_parent` raises, or returns `None` | `None` |
| exact `source_sha == head` | unchanged; `head_parent` is not called |

Tests for `_commit_parent`: one parent gives that SHA; zero or two parents, a failed `runner`, or a
raise give `None`.

The existing pfactory and endgame suites pass, and CI is green.

Live: release 3.21.18, rerun E2E P1. S4 merges and S5 runs.
