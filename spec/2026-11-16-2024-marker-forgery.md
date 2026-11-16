---
status: approved
issue: 2024
intent: intent/2026-11-16-2024-marker-forgery.md
---

# Spec: Job log markers cannot set terminal state

## Design

All three paths (build Job, spec Job, in-pod) read markers through one
function, `AgentEmitMixin._handle_output_line` in
`apps/web-server/server/services/agent_emit.py`. Its `phase_map` currently
accepts `complete`, `completed` and `failed`. Change: a phase marker may set
only `spec_creation`, `planning`, `coding`, `qa_review`, `qa_fixing`. A marker
naming `complete`, `completed` or `failed` is treated as telemetry: the line is
still logged, no phase is tracked, no progress or status event is emitted, and
one warning is logged. Terminal state is produced by exit-code/Job
reconciliation (`agent_service.py` exit handler, `process_monitor.py`, the
kubejob Job-outcome path). Residual, accepted: `process_monitor.py`
(~:437-488) reads the tracked phase at exit with an unapproved review to pick
COMPLETED (100%) versus PLAN_REVIEW (20%), so a forged non-terminal phase can
still shift that choice. Both are human-review states and neither is
pass/fail: a progress label, not a verdict.

## Alternatives rejected

- Strip the nonce from the runner's own env after reading it: the build emits
  markers from many modules and re-reads `os.environ` per call
  (`core/phase_event.py`); it would need a process-wide cache and a redesign,
  and `/proc/<pid>/environ` keeps the launch env anyway.
- Per-marker HMAC over a key the agent cannot read: same runner-side key
  exposure, larger change.

## Risks

Suppressing a legitimate QA `failed` marker relies on the exit path (exit
code, review state, subtask evidence), which is unchanged and already decides
the verdict. A legitimate final `complete` marker no longer pushes the card to 100% by
itself; exit reconciliation does that moments later (process_monitor emits
COMPLETED at exit). Forged non-terminal phases remain possible (a cosmetic
phase label only).

## Verification

Tests in `tests/test_marker_nonce.py`: a valid-nonce `complete` and `failed`
marker leaves the phase unchanged and emits no progress, through both the
in-process handler and the kubejob stream; valid `coding` still works.
