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
   - In `_pr_title_body`, inside the existing `try`, collect the artifact lines from `prov` (the
     dict already read at ~1919). Do it only when `prov` is a dict and
     `prov.get("source_artifacts")` is a list.
   - After the `try`/`except` and before the `Fixes #` append (~1947), add the block when the
     list is non-empty.
   - Keep the function under the repo's complexity limits. If ruff flags PLR0912/C901, move the
     collection into a small `_artifact_lines(prov) -> list[str]` helper rather than adding a
     noqa.

   **Tests,** in `apps/web-server/tests/test_pr_endgame.py`, next to `test_pr_body_no_issue_no_fixes`
   (~871). Add the cases from the spec table:
   - all three valid items plus `issue_number`: the block is in order and comes before `Fixes #N`;
   - out-of-order items: the block is still in intent, spec, plan order;
   - invalid paths, one test each or parametrized: a newline, `../`, a backtick, the wrong stage
     prefix, `.txt`;
   - `source_artifacts` missing, not a list, or with no valid items: no `**Artifacts**`, and the
     body equals the body with the key removed;
   - no `description` with valid items: the block is still present.

   **Verify by:**

   ```bash
   cd apps/web-server && ../web-server/.venv/bin/pytest -q -o asyncio_mode=auto tests/test_pr_endgame.py tests/test_pr_verify_evidence.py
   ```

   Then the ratchets on committed files: `python scripts/cq_ratchet.py --base origin/dev` and
   `--tool mypy`.

   **Traps:**
   - The web-server tests need the web-server venv (in the main clone,
     `/mnt/code/Source-home/GitHub/AIFactory/apps/web-server/.venv`) and `-o asyncio_mode=auto`.
   - `cq_ratchet` only sees committed files: the lead commits before running it.
   - Do not touch `merger.py`, which uses only the title.
   - Do not change the default body text or the `_EVIDENCE_*` stripping.

## Tests

- **Step 1's pytest command:** all pass. There are no new failures against origin/dev. The known
  base failures listed in the repo are unrelated.
- **ruff and mypy ratchets:** clean.
- **CI on the PR:** green.

## Rollback

Revert the squash commit. The change only adds text to PR bodies, and no stored state changes.
