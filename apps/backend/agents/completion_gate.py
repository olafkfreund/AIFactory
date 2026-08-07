"""The one door a subtask walks through to be called "completed" (#1177).

AIFactory has two coding engines. The serial coder marks a subtask complete by
calling the ``update_subtask_status`` tool
(:func:`agents.tools_pkg.tools.subtask.apply_subtask_status_update`); the
parallel wave runner marks it complete itself, in
``agents.parallel_integration``, after merging the child worktree back.

The honesty gates (#851 test-execution evidence, #1111 deliverable coverage,
#1113 pipeline evidence) were wired into the first path only, so a wave child
was completed on "the session returned success and the merge worked" — which is
not evidence that the work was done. #1111's own motivating failure (wave worker C2 shipping a route
it never registered) came through exactly that path.

This module is the gate itself, so both engines *call* it rather than each
carrying a copy that can drift (the ruff/mypy ratchet shipped five drifted
copies for exactly that reason, Factory#590).

**Adding a gate: add it here.** Not in ``apply_subtask_status_update``, or the
wave path silently skips it again.

Inputs, and why they are these:

* ``subtask`` — the plan record, so a gate can read what this subtask itself
  promised (its acceptance criteria, its HTTP paths).
* ``project_dir`` — the tree that ships. Serial passes the task worktree
  mid-build; the wave path passes the same tree *after* the child's merge, so
  the gate judges merged reality, not an isolated child tree.
* ``evidence`` — an already-read :func:`agents.test_evidence.read_test_evidence`
  summary rather than a spec dir, because a wave child records its test runs in
  its own worktree spec dir and that worktree is deleted by the merge. The
  caller reads the summary while it still exists.

All gates share the single ``AIFACTORY_TEST_EVIDENCE_GATE=off`` escape hatch;
no second flag. Marking a subtask ``failed`` is never blocked (RFC-0006) — that
is the caller's business, and neither caller gates a failure.

Checked by ``tests/test_wave_completion_gate.py``, which drives BOTH engines
through this function — including the mutation that removes the routing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .pipeline_evidence import pipeline_evidence_gap
from .test_evidence import (
    deliverable_evidence_gap,
    gate_enabled,
    is_verification_subtask,
)

__all__ = ["completion_refusal"]


def completion_refusal(
    subtask: dict[str, Any],
    project_dir: Path | str,
    evidence: dict[str, Any],
) -> str | None:
    """Why this subtask may NOT be reported completed, or ``None`` to allow it.

    The returned text is addressed to the coder: it says what is missing and
    what to do instead, including the honest ``failed`` option.
    """
    if not gate_enabled():
        return None

    subtask_id = str(subtask.get("id", "")) or "?"

    # #1113: the CI/CD subtask may not be satisfied with prose. Its acceptance
    # criteria are about stages that RUN on push and PR, so the only evidence is
    # the pipeline file — twice in a row the coder committed a design document
    # under docs/plans/ and left ci.yml untouched, because the plan named that
    # document as its only file to create. Inert unless the subtask is a CI/CD
    # subtask AND names stages its repo's pipeline does not have.
    #
    # FIRST of the three: gates run narrowest-predicate first so the refusal an
    # operator reads is the most specific one. Not cosmetic — a CI/CD subtask's
    # own text says "the test stage runs the full suite", which makes it a
    # verification subtask to #851 below, so the other order answers "ci.yml has
    # no stages" with "run pytest now": true, and no help.
    gap = pipeline_evidence_gap(subtask, project_dir)
    if gap:
        return gap

    # #1111: "a test ran" is not "a test ran against the deliverable". A subtask
    # that promises an HTTP path may not be completed when the only tests naming
    # that path assert against an app built inside the test file — genuinely
    # green, and blind to a route nobody registered. Inert unless the subtask
    # names a path and a Python test mentions it, so pure-function work is
    # untouched.
    gap = deliverable_evidence_gap(subtask, project_dir)
    if gap:
        return gap

    # #851: a test/verification subtask may not be reported "completed" unless a
    # real test command actually ran (captured tamper-evidently by the PostToolUse
    # hook). No run — or a run that clearly failed — is refused with actionable
    # guidance, so the coder can no longer self-report a green checkbox for tests
    # it never executed (RFC-0006).
    if is_verification_subtask(subtask):
        if not evidence.get("ran"):
            return (
                f"Refused: subtask '{subtask_id}' is a test/verification subtask, "
                "but no test command ran this build. Run the tests now (e.g. "
                "pytest / go test / npm test) — the build records the run "
                "automatically — then mark it completed. If this repo has NO "
                "runnable test environment, mark this subtask 'failed' with a note "
                "saying tests could not be executed. Do NOT report it completed "
                "unverified (RFC-0006: never claim verification that did not happen)."
            )
        if evidence.get("last_failed"):
            return (
                f"Refused: the last recorded test run failed (command: "
                f"{evidence.get('last_command')!r}). Fix the failures and re-run the "
                f"tests green before completing subtask '{subtask_id}', or mark it "
                "'failed' with the reason. Do NOT report it completed over failing "
                "tests."
            )

    return None
