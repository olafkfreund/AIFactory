---
status: draft
issue: 2022
intent: intent/2026-11-16-2022-session-file-collide.md
---

# Spec: session file collisions and gate edge cases (#2022, #2035)

## Design

1. `memory/sessions.py save_session_insights`: create the file with mode
   `"x"` (exclusive); on `FileExistsError` bump `session_num` and retry. Nothing
   is ever overwritten, even by concurrent writers, so the gate's "seen in a
   prior file" invariant holds. The `prior_file == session_file` skip goes
   away. The final number is what is sent to Graphiti and stored in
   `session_number`. The prior-file scan runs after the number is chosen.
2. `memory/novelty.py repeat_key`: key a dict by its persisted form:
   `json.dumps(json.loads(json.dumps(item, default=str)), sort_keys=True)`.
   The inner round trip stringifies keys exactly as storage does (so
   save -> reload -> repeat matches) and removes mixed-type keys before sorting.
   `ValueError` (circular dict) or `TypeError` returns `None` (not gated).
3. `agents/tools_pkg/tools/memory.py record_gotcha`: before dedupe and write,
   prefix one backslash to every body line matching `^\\*## [YYYY-MM-DD HH:MM]`.
   The map is injective (existing backslashes shift one place), so distinct
   gotchas stay distinct and the header `split` is unambiguous.
4. Same three edits in PFactory and TFactory (files are the same up to
   formatting).

## Alternatives rejected

- Per-writer prefixes: changes `load_all_insights` globbing and file layout.
- Merge on overwrite: more code, still racy.
- Counter in the caller: three call sites and a resumed run still collide.
- Gate on the Graphiti path: Graphiti is not configured in the pod and the
  path has no prior-state to compare against without a Graphiti query; deferred.

## Risks

A retried save of the same session now writes a second file instead of
replacing the first; repeats are dropped by the gate, so only an empty-ish
file results. Old gotchas.md files with embedded headers keep the old split.

## Verification

New tests in `tests/test_memory_novelty.py` (collision keeps both files and
both items; mixed-key dict does not raise; embedded-header gotcha stored once
and a later identical inner text is not rejected). Also: two writers racing for one number keep both files; a mixed-key dict
saved, reloaded and repeated is dropped; a body with a literal backslash
header is distinct from the unescaped one. Run the memory tests and ruff.
