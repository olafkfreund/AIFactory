---
status: approved
issue: AIFactory#1860
spec: spec/2026-10-24-1860-claude-print-scrub.md
---

# Plan: scrub outbound PII on the two direct `claude --print` spawns

AIFactory branch `fix/1860-claude-print-scrub`, base `dev`, one PR into `dev`.
Factory branch `docs/1860-claude-print-scrub-exception`, base `main`, one PR
into `main`, opened only after the AIFactory PR is merged. Line numbers are
from `origin/dev` at 2e46886b (the #1862 merge) and Factory `origin/main`.
Four steps edit files, so under the org rules the steps go to the `coder`
agent and the review to a fresh Opus agent.

## Decisions (carried from the approved spec)

- Reuse `scrub_insights_outbound(message, None, owner=..., classification=...)`
  from `apps/web-server/server/services/insights_providers/base.py:75`. It
  already fails closed on a missing `core.outbound_scrub` (raises
  `RuntimeError`) and honours `scrub_outbound_enabled(classification)`. Take
  element `[0]` of the returned tuple.
- Classification: `await project_settings_store.data_classification(project_path)`
  (`project_settings_store.py:382`, returns `regulated` if unreadable), the
  #1862 pattern. Call it as a module attribute.
- `internal` follows `scrub_outbound_enabled`: scrubbed by default, raw only
  under `LITELLM_AUDIT_SCRUB_OUTBOUND=false`. `regulated` is always scrubbed.
- Clarification fails closed to its existing `safe_default` (skip=True);
  task-from-chat lets the error propagate to the route, which already returns
  `{"success": False}`.
- Only argv is scrubbed. Stored chat sessions are untouched.
- No new flag, helper, or pattern. The Factory docs drop the "known exception"
  sentence and the two matching clauses in R-004 and A.8.11.

## Steps

1. `apps/web-server/server/services/clarification_service.py:166-171`
   (`generate_clarification_questions`). Between line 166
   (`prompt = CLARIFICATION_PROMPT.format(...)`) and line 171
   (`cmd = [claude_bin, "--print", "--model", "haiku", prompt]`), add:

   ```python
   # AIFactory#1860: argv is the wire; scrub it like every other LLM path.
   from . import project_settings_store
   from .insights_providers.base import scrub_insights_outbound

   try:
       prompt = scrub_insights_outbound(
           prompt,
           None,
           owner="ClarificationService",
           classification=await project_settings_store.data_classification(
               project_path
           ),
       )[0]
   except Exception:  # noqa: BLE001 - fail closed: send nothing, never block the user
       logger.error("[ClarificationService] Outbound scrub failed; not sending", exc_info=True)
       return safe_default
   ```

   → verify by `ruff check apps/web-server/server/services/clarification_service.py`
   and the step 3 tests.
   Traps: put it before `cmd` is built (the prompt is the last argv element).
   Never log `prompt` or `cmd` (CodeQL clear-text logging, see
   `test_insights_outbound_scrub.py:205-209`). Keep the imports lazy, inside
   the function, like line 158. `from . import project_settings_store`, not
   `from .project_settings_store import data_classification`, or the test
   monkeypatch is bypassed.

2. `apps/web-server/server/services/insights_service.py:480-510`
   (`InsightsService.generate_task_from_chat`). After the
   `summarization_prompt = (...)` block ends (line 490) and before line 510
   (`cmd = [claude_bin, "--print", "--model", model_value, summarization_prompt]`),
   add:

   ```python
   # AIFactory#1860: argv is the wire; scrub it like every other LLM path.
   from . import project_settings_store
   from .insights_providers.base import scrub_insights_outbound

   summarization_prompt = scrub_insights_outbound(
       summarization_prompt,
       None,
       owner="InsightsService.generate_task_from_chat",
       classification=await project_settings_store.data_classification(
           project_path
       ),
   )[0]
   ```

   No try/except here: the error must not fall through to sending raw text,
   and `routes/changelog.py:1092-1104` already catches it.
   → verify by `ruff check apps/web-server/server/services/insights_service.py`
   and the step 3 tests.
   Traps: do not move it inside the later `try:` at line ~539, whose
   `except Exception` returns an empty task and would hide a scrub failure
   as "no task". Do not scrub `session.messages` (they are the stored UI copy).
   Check `project_settings_store` is not already imported at module level in
   this file before adding the lazy import (it is not at 2e46886b).

3. New `apps/web-server/tests/test_claude_print_outbound_scrub.py`. Copy the
   header and `sys.path` block from `test_insights_outbound_scrub.py:14-35`
   and its synthetic `SSN/EMAIL/PHONE/PAN`, `PII_VALUES`, `PII_MARKERS`,
   `CONTEXT`, `PROMPT` constants (lines 47-76). Fixtures:
   - autouse: `monkeypatch.delenv("LITELLM_AUDIT_SCRUB_OUTBOUND", raising=False)`.
   - autouse `project_class`: monkeypatch
     `server.services.project_settings_store.data_classification` with an
     async fn returning a mutable value, default `"internal"`
     (as `test_insights_outbound_scrub.py:120-134`).
   - autouse: monkeypatch
     `server.services.insights_providers.claude_provider.ClaudeProvider._resolve_claude_token`
     to `lambda _self: (None, None, None)`.
   - `fake_cli(tmp_path, monkeypatch)`: write `tmp_path/"claude"`
     (`#!/usr/bin/env python3`, dumps `sys.argv[1:]` to `tmp_path/"argv.json"`,
     prints `{"title": "t", "description": "d", "skip": false, "skipReason": "", "questions": []}`),
     chmod +x, `monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")`,
     return a reader for the joined argv (`None` if the file was never written).
   - Drivers: clarification calls
     `generate_clarification_questions(PROMPT, "see description", tmp_path)`;
     insights builds `InsightsService()`, sets
     `service.get_current_session = lambda *_a, **_k: InsightsSession(id="s", project_id="p", title="t", messages=[InsightsMessage(id="m", role="user", content=PROMPT, timestamp="now")])`,
     then `await service.generate_task_from_chat(tmp_path, "p")`.

   Tests, parametrized over the two drivers:
   - `regulated` + kill-switch `false` → no PII value in argv, every marker
     present, every `CONTEXT` string present.
   - `internal` + kill-switch `false` → every PII value present, no marker
     (today's behaviour; also the mutation control).
   - `internal`, kill-switch unset → scrubbed (deployment default).
   - clarification only: monkeypatch
     `server.services.insights_providers.base.scrub_insights_outbound` to raise
     `RuntimeError` → result `skip is True` and argv file never written.
   - insights only: same monkeypatch → `pytest.raises(RuntimeError)` and argv
     file never written.

   → verify by
   `apps/backend/.venv/bin/pytest apps/web-server/tests/test_claude_print_outbound_scrub.py apps/web-server/tests/test_insights_outbound_scrub.py -q -o asyncio_mode=auto`
   (run from the worktree root; the venv is in the main clone at
   `/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv` if the worktree
   has none). Then mutation: comment out the step 1 block, the clarification
   regulated test must fail; restore; same for step 2.
   Traps: deviation (ratchet gate PLC0415/PLR0915): the services import
   `scrub_insights_outbound` and `project_settings_store` at module level
   (insights via a small `_scrub_chat_prompt` helper), so the raise-case
   monkeypatch targets `server.services.<service>.scrub_insights_outbound`. Synthetic values only; no real provider is contacted. The shebang
   needs `python3` on the child's `PATH`, so prepend, never replace, `PATH`.

4. Ship AIFactory: `ruff check` + `ruff format --check` on the three files,
   commit `fix(security): scrub outbound PII on direct claude --print spawns (#1860)`
   with hooks on (they can take 10+ minutes; use a long timeout, never
   `--no-verify`), push, open a PR into `dev` linking intent/spec/plan and
   saying which steps the coder did. Wait for CI "Pytest co-located
   (apps/web-server)" green, review, merge.
   → verify by `gh pr checks` green and the PR merged.
   Traps: never force-push or `--admin`.

5. Factory, worktree on `docs/1860-claude-print-scrub-exception` from `origin/main`,
   only after step 4 is merged:
   - `docs/compliance/policies/data-classification-and-handling-policy.md:42-44`:
     delete the sentence "Known exception: the two direct `claude --print`
     spawns in the web-server are not scrubbed at any tier (AIFactory#1860)."
     and rejoin the wrapped line so "... except to `regulated`. Where data"
     reads on.
   - `docs/compliance/risk-register.md:38` (R-004): remove
     " or via two unscrubbed direct CLI spawns (AIFactory#1860)" from the risk
     text and "close the CLI-spawn gap (AIFactory#1860); " from the treatment.
     Leave scores and status alone.
   - `docs/compliance/statement-of-applicability.md:153` (A.8.11): remove
     "; partial: two direct CLI spawns unscrubbed (R-004)". Status stays
     "Implemented".
   Commit `docs(compliance): drop the claude --print scrub exception (AIFactory#1860)`,
   PR into `main` linking the merged AIFactory PR and this plan.
   → verify by `grep -rn "1860\|direct CLI spawn\|Known exception" docs/compliance`
   returning nothing, and the docs CI green.
   Traps: no other edits to these files; markdown lint runs in hooks.

## Tests

- `apps/backend/.venv/bin/pytest apps/web-server/tests/test_claude_print_outbound_scrub.py apps/web-server/tests/test_insights_outbound_scrub.py -q -o asyncio_mode=auto`
  → all pass.
- Mutation (step 3) → each removed scrub turns its regulated test red.
- CI on the AIFactory PR → green.

## Rollback

Revert the AIFactory merge commit (restores raw argv on both spawns), then
revert the Factory docs commit so the policy names the exception again. The
two reverts go together; the docs must not claim a fix that is reverted.
