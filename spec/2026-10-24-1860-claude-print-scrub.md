---
status: approved
issue: AIFactory#1860
intent: intent/2026-10-24-1860-claude-print-scrub.md
---

# Spec: scrub outbound PII on the two direct `claude --print` spawns

## Design

Reuse the insights seam's helper. `scrub_insights_outbound(message, history,
*, owner, classification)` in
`apps/web-server/server/services/insights_providers/base.py:75` already does
exactly this job for one prompt: it imports `core.outbound_scrub` and raises
`RuntimeError` if the import fails (fail closed), returns the prompt unchanged
when `scrub_outbound_enabled(classification)` is false, and otherwise returns
`scrub_outbound_prompt(message, owner=owner)`. Both call sites pass
`history=None` and take element `[0]`.

The classification comes from
`await project_settings_store.data_classification(project_path)`
(`project_settings_store.py:382`), as #1862 does. It returns `regulated` when
the store cannot be read, so the floor never drops. Call it through the module
attribute (`project_settings_store.data_classification(...)`), not a
`from ... import data_classification`, so the existing test fixture that
monkeypatches the module attribute works.

1. `clarification_service.generate_clarification_questions`: after
   `prompt = CLARIFICATION_PROMPT.format(...)` and before `cmd = [...]`,
   replace `prompt` with the scrubbed prompt (`owner="ClarificationService"`).
   Wrap the classification read and scrub in `try/except Exception`: log
   (without the prompt) and `return safe_default`. That is fail closed (nothing
   is sent) and keeps the function's "never block the user" contract.
2. `InsightsService.generate_task_from_chat`: after `summarization_prompt` is
   built and before `cmd = [...]`, replace it with the scrubbed prompt
   (`owner="InsightsService.generate_task_from_chat"`). No local catch: an
   error propagates to `routes/changelog.py:1092-1104`, which already turns
   it into `{"success": False, ...}`. Nothing is sent. The stored session is
   not touched, so the UI keeps what the user typed.

Imports are lazy inside each function (both files already import
`insights_providers` and `subprocess_env` lazily), which avoids import cycles
and module-load cost.

Tests: a new file `apps/web-server/tests/test_claude_print_outbound_scrub.py`
in the style of `test_insights_outbound_scrub.py:201-253`. A fake `claude`
executable in `tmp_path`, put first on `PATH` (both sites resolve it with
`shutil.which("claude")`), writes its `sys.argv[1:]` to a JSON file and prints
a valid JSON reply. The data class is pinned by monkeypatching
`server.services.project_settings_store.data_classification`. For each of the
two services:

- `regulated` with `LITELLM_AUDIT_SCRUB_OUTBOUND=false`: argv has none of the
  four synthetic PII values and all four `[REDACTED_*]` markers, plus the
  non-PII context.
- `internal` with `LITELLM_AUDIT_SCRUB_OUTBOUND=false`: argv carries the raw
  values (today's behaviour, and the mutation control showing the test
  observes the scrub).
- `internal` with the kill-switch unset: scrubbed (the deployment default).
- clarification only: if the scrub raises, the CLI is never run and the
  result is the `skip=True` default.

Factory docs (separate PR into `main`, after the AIFactory PR merges):

- `docs/compliance/policies/data-classification-and-handling-policy.md:42-44`:
  delete "Known exception: the two direct `claude --print` spawns in the
  web-server are not scrubbed at any tier (AIFactory#1860)." and rejoin the
  line wrap.
- `docs/compliance/risk-register.md:38` (R-004): drop "or via two unscrubbed
  direct CLI spawns (AIFactory#1860)" from the risk text and "close the
  CLI-spawn gap (AIFactory#1860);" from the treatment. Scores and status stay.
- `docs/compliance/statement-of-applicability.md:153` (A.8.11): drop
  "; partial: two direct CLI spawns unscrubbed (R-004)". Status stays
  "Implemented".

## Alternatives rejected

- Call `scrub_outbound_enabled` / `scrub_outbound_prompt` from `core` directly
  at each site: duplicates the fail-closed import and `sys.path` handling that
  `base.py` already owns.
- Route both calls through `ClaudeProvider.send_message` so the
  `ProviderStrategy` seam scrubs them: changes argv shape (stream-json output,
  other flags), parsing and timeouts; far bigger than the bug.
- Set `AIFACTORY_DATA_CLASSIFICATION` in the child env (the changelog
  pattern): the child is the `claude` CLI, which does not scrub; the prompt is
  already in argv.
- A shared helper for "build a scrubbed `claude --print` command": two call
  sites, three lines each; not worth a new abstraction.

## Risks

- Over-redaction of a clarification or task prompt: limited to the four
  high-precision built-in patterns (#1010); same risk the insights chat
  already carries.
- A store read on every call adds one small DB/file read; both paths already
  spawn a CLI subprocess, so it is negligible.
- If `core` is not importable in a deployment, clarification silently returns
  "skip" and task-from-chat returns an error instead of sending raw text. That
  is the intended fail-closed behaviour; it is logged.

## Verification

- `apps/backend/.venv/bin/pytest apps/web-server/tests/test_claude_print_outbound_scrub.py apps/web-server/tests/test_insights_outbound_scrub.py -q -o asyncio_mode=auto` passes.
- Mutation check: with the scrub line removed from either service, its
  regulated test fails.
- `ruff check` and `ruff format --check` on the touched files are clean.
- CI "Pytest co-located (apps/web-server)" green on the PR.
- Factory PR: `grep -rn "1860" docs/compliance` returns nothing after the edit.
