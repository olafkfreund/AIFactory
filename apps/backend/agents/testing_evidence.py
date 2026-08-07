"""Testing-subtask evidence — a testing subtask cannot be prose either (#1176).

The ``cicd`` sibling of this gate is :mod:`agents.pipeline_evidence` (#1113).
The ``testing`` child has the identical shape and reproduced in the same two
runs of ``olafkfreund/aifactory-demo``: PFactory's synthesizer writes the child
body as "implement the testing strategy specified in
``docs/plans/<plan-id>-testing-strategy.md``", the delta pass mines that path as
the only file-like token in the record, and the coder is handed exactly one file
to create — a markdown file. It creates it::

    spec 101: TEST service=testing
              files_to_create=['docs/plans/030-...-testing-strategy.md']
    3aa3767 aifactory: TEST - No description
      docs/plans/030-...-testing-strategy.md | 278 ++++++++++

Its acceptance criteria ("Unit, integration, and e2e lanes are scaffolded and
runnable") are as unsatisfiable by a document as the CI/CD ones were.

Why this gate is needed NOW, and not covered by #851: until #1176 a ``testing``
subtask was declared ``is_handoff`` and excluded from the coder's queue by the
accounting layer — while both engines dispatched it anyway. #1176 removes that
fiction, so the subtask is honestly the coder's, and honestly the coder's means
honestly gated. #851 does not cover it: it asks "did a test command run this
build", and the serial engine reads the whole build's evidence ledger, so any
earlier subtask's green ``pytest`` satisfies it. Measured on ``origin/dev``::

    build-wide evidence seen by the serial gate:
        {'ran': True, 'last_failed': False, 'runs': 1, 'last_command': 'pytest -q'}
    refusal for the prose-only testing subtask: None

That build-wide scoping is its own defect (#1187) and is not fixed here; this
gate is keyed on the subtask's declared deliverables instead, so it holds
regardless of how evidence is scoped.

PFactory#461 is the planner half — the testing child should name real test files
the way PFactory#460 taught the CI/CD child to name the real pipeline. This is
the half that does not depend on the planner getting it right, and it goes quiet
once #461 lands: a subtask naming real test files that exist passes with no edit.

Narrow on purpose, because a gate that fires on correct work gets switched off:

* Only ``service == "testing"`` — the contract field PFactory sets. Deliberately
  NO text matching: nearly every subtask's text mentions tests, so a text rule
  would fire on ordinary implementation work. The ``cicd`` gate can afford text
  patterns because "ci/cd" is rare in a feature subtask; "test" is not.
* Only the subtask's OWN declared files are judged. A testing subtask that
  declares no files is inert — there is nothing to be dishonest about.
* Marking the subtask ``failed`` is never blocked (RFC-0006).

Shares the ``AIFACTORY_TEST_EVIDENCE_GATE=off`` escape hatch, checked once for
all gates in :func:`agents.completion_gate.completion_refusal`.

Checked by ``tests/test_testing_evidence_gate.py`` on the serial engine and
``tests/test_wave_completion_gate.py`` on the wave engine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["declared_files", "is_testing_subtask", "testing_deliverable_gap"]

# The contract field. No text fallback — see the module docstring.
_TESTING_SERVICES = frozenset({"testing", "test", "qa"})

# A deliverable that is documentation rather than a runnable test.
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_DOC_DIRS = ("docs/", "doc/", "documentation/")


def is_testing_subtask(subtask: dict[str, Any]) -> bool:
    """True when this subtask is the plan's testing work — the gated kind."""
    return str(subtask.get("service") or "").strip().lower() in _TESTING_SERVICES


def declared_files(subtask: dict[str, Any]) -> list[str]:
    """Every file path this subtask promises to create or modify."""
    out: list[str] = []
    for key in ("files_to_create", "files_to_modify"):
        value = subtask.get(key)
        if isinstance(value, list):
            out += [str(p).strip() for p in value if str(p).strip()]
    return out


def _is_doc(path: str) -> bool:
    low = path.replace("\\", "/").lower()
    return low.endswith(_DOC_SUFFIXES) or low.startswith(_DOC_DIRS)


def testing_deliverable_gap(
    subtask: dict[str, Any], project_dir: Path | str
) -> str | None:
    """Why this testing subtask may not be completed, or ``None`` to allow it.

    Two refusals, both decided from the subtask record plus the tree:

    1. every file it declares is documentation — the record itself cannot yield
       a runnable test, which is the PFactory#461 defect stated at completion
       time rather than discovered by QA a cycle later;
    2. it declares real test files and not one of them exists — it promised
       tests and the tree does not have them.

    Any declared non-doc file that exists passes. A subtask declaring no files
    is inert.
    """
    if not is_testing_subtask(subtask):
        return None

    declared = declared_files(subtask)
    if not declared:
        return None

    subtask_id = str(subtask.get("id", "")) or "?"
    code = [p for p in declared if not _is_doc(p)]

    if not code:
        docs = ", ".join(sorted(declared))
        return (
            f"Refused: subtask '{subtask_id}' is the plan's testing work, but every "
            f"file it declares is documentation ({docs}). A design document cannot "
            "satisfy criteria about test lanes being scaffolded and RUNNABLE — this "
            "is the same defect the CI/CD subtask had, where a 278-line strategy "
            "document shipped and QA rejected sign-off. Write the actual tests into "
            "the repo's test directory (follow the layout the repo already uses) and "
            "run them, then mark this completed. Keep the strategy document if it is "
            "useful; it is not the deliverable. If this repo genuinely has no place "
            "to put tests, mark this subtask 'failed' with that reason — do NOT "
            "report it completed on prose (RFC-0006)."
        )

    root = Path(project_dir)
    if not any((root / p).exists() for p in code):
        missing = ", ".join(sorted(code))
        return (
            f"Refused: subtask '{subtask_id}' promised test file(s) that do not "
            f"exist: {missing}. Create them (or the equivalent tests in the repo's "
            "actual test layout) and run the suite before completing this subtask. "
            "If the tests belong somewhere else, put them there and say so in the "
            "completion note; if they cannot be written, mark this subtask 'failed' "
            "with the reason rather than reporting it completed."
        )

    return None
