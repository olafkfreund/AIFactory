---
status: draft
issue: 2263
spec: spec/2026-11-27-2263-pr-artifact-links.md
coders:
  1: codex  # small, security-relevant string handling; the owner routes coding to Codex
reviewers:
  - codex  # independent read of the validation regex and body ordering
  - agy    # second model on the same diff (agent-team)
---

# Plan: list `provenance.source_artifacts` in the auto-PR body

## Approved decisions

**Where:** only `_pr_title_body` in `apps/web-server/server/services/pr_endgame.py` (~1905–1951),
plus tests.

**Source:** `requirements.json` → `provenance.source_artifacts`. This is a list of dicts. Read only
`stage` and `path`.

**Validation:** add a module constant next to the other module-level regexes. `re` is already
imported on line 25.

```python
_ARTIFACT_PATH = re.compile(r"(intent|spec|plan)/[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.md")
```

For each stage in `("intent", "spec", "plan")`, take the first item where all of these hold:
- the item is a `dict`;
- `item.get("stage") == stage`;
- `item.get("path")` is a `str`;
- `_ARTIFACT_PATH.fullmatch(path)` matches;
- `path.startswith(f"{stage}/")`.

Skip anything else silently.

**Output:** when at least one stage qualifies, append this block after the description and
attribution lines, and before `Fixes #N`:

```text
\n\n**Artifacts**\n- intent: `intent/<slug>.md`\n- spec: `spec/<slug>.md`\n- plan: `plan/<slug>.md`
```

Leave out a stage that has no valid item. When nothing qualifies, the body is byte-identical to
today's. Use plain paths, not URLs or relative Markdown links.

## Steps

1. **`apps/web-server/server/services/pr_endgame.py`:**
   - Add `_ARTIFACT_PATH` at module level.
   - Add a typed helper `_artifact_lines(prov: object) -> list[str]`. It returns the
     `- <stage>: \`<path>\`` lines by the rule above, and `[]` when `prov` is not a dict or
     `source_artifacts` is not a list. Its own `isinstance` checks mean no `TypeError` can escape.
     Use the helper from the start: an inline loop trips C901 (Codex measured 12 against a limit
     of 10).
   - In `_pr_title_body`, inside the existing `try`, right after the `if who:` append (~1940):

     ```python
     arts = _artifact_lines(prov)
     if arts:
         body = body + "\n\n**Artifacts**\n" + "\n".join(arts)
     ```

     Being inside the `try` means a failed read keeps today's fallback, with no variable to
     initialise. It also places the block after the attribution lines and before `Fixes #N`.

   **Tests,** in `apps/web-server/tests/test_pr_endgame.py`, next to `test_pr_body_no_issue_no_fixes`
   (~871):
   - **All three valid items plus `issue_number`:** the block lists the three paths in order, and
     `Fixes #N` comes after it.
   - **Ordering with attribution:** `AIFACTORY_AGENT_COMMIT_TRAILERS=1`, `agent_identity` and
     `originator` set. The order is description, attribution, `**Artifacts**`, `Fixes #N`.
   - **Out-of-order items:** the block is still in intent, spec, plan order.
   - **Mixed list:**
     - a non-dict item (`None`, a bare string);
     - a dict with no `path` or with a non-string `path`;
     - an invalid candidate before the first valid one for a stage, which still picks the valid
       one;
     - a second valid item for a stage, where only the first is emitted;
     - one stage with no valid item, which is left out and leaves no blank line.
   - **Invalid paths,** parametrized: an embedded newline, a trailing `\n`, `../`, a backtick,
     the wrong stage prefix, `.txt`.
   - **No-op cases** (`source_artifacts` missing, not a list, or with no valid items): assert the
     exact legacy body string, not only a comparison with a second call.
   - **No `description`** with valid items: the block follows the default body.

   **Verify by,** from the worktree root:

   ```bash
   W=/mnt/code/Source-home/GitHub/AIFactory/apps/web-server/.venv/bin
   (cd apps/web-server && $W/pytest -q -o asyncio_mode=auto tests/test_pr_endgame.py tests/test_pr_verify_evidence.py)
   ```

   After the lead commits, run the ratchets from the repo root:
   `python scripts/cq_ratchet.py --base origin/dev`, then the same with `--tool mypy`.

   **Traps:**
   - The worktree has no venv of its own, so use the main clone's web-server venv as above, with
     `-o asyncio_mode=auto`.
   - `cq_ratchet` only sees committed files.
   - Do not touch `merger.py`, which uses only the title.
   - Do not change the default body text or the `_EVIDENCE_*` stripping.

## Tests

- **Step 1's pytest command:** all pass. There are no new failures against origin/dev. The known
  base failures listed in the repo are unrelated.
- **ruff and mypy ratchets:** clean.
- **CI on the PR:** green.
- **Live, after the AIFactory release:** a P1 run's S4 shows `triple_linked: true`. That run
  opens a new auto-PR. Adopted PRs return before `_pr_title_body`, and an already-open PR's body
  is not rewritten, so neither is covered (Codex review).

## Rollback

Revert the squash commit. The change only adds text to PR bodies, and no stored state changes.
