"""A review gate that cannot read its input must hold the task, not release it.

AIFactory#1384, the sibling of TFactory#1139. Four separate handlers read
``requireReviewBeforeCoding`` from ``task_metadata.json`` inside a
``try``/``except`` and, on failure, left the permissive value standing:

    execution.py            require_review stayed False  -> plan never marked human_review
    task_phase.py (x2)      returned False               -> caller told "no review requested"
    agent_spec_creation.py  should_auto_approve stayed True -> spec AUTO-APPROVED

The last is the sharpest: ``should_auto_approve`` DEFAULTS to True and the only
thing that clears it is the flag being read, so an unreadable file did not merely
skip review -- it approved.

``except`` there does not mean "no review requested". It means "I could not find
out whether review was requested", and only one of those is safe to treat as the
permissive answer.

Asserted structurally, deliberately. These sit inside long request handlers that
spawn subprocesses; a behavioural test would need enough mocking to become its own
liability, and carving seams into them during a security fix is the wrong moment
to refactor. This reads the source and pins the property directly.

Scope note: the file list is derived, not hand-written. Anything that reads the
flag inside a ``try`` is examined, so a NEW handler added later is covered
without editing this test.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_FLAG_KEY = "requireReviewBeforeCoding"

# The permissive value each site must NOT leave standing when the read fails.
_PERMISSIVE = {"require_review": False, "should_auto_approve": True}


def _closes_safely(handler: ast.ExceptHandler, block: ast.Try) -> bool:
    """Does this handler refuse to leave the permissive answer standing?

    Inspects real AST nodes. An earlier version substring-matched the handler's
    ``ast.dump``, so the flag name and a ``False`` constant merely had to appear
    SOMEWHERE in the handler -- a ``logger.warning`` mentioning the flag beside
    any unrelated ``False`` satisfied it. Mutation-testing caught that: flipping
    ``should_auto_approve = False`` to ``True`` left the test green. A detector
    that cannot see the defect it was written for is worse than none, because it
    reports safety.

    Two shapes, both real here:

    * the decision lives in a VARIABLE -- the handler must ASSIGN the safe value;
    * the site is a helper whose RETURN is the decision -- the handler must
      RETURN the safe value.
    """
    for node in ast.walk(handler):
        # shape 1: an assignment to a known flag, with the safe constant.
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                permissive = _PERMISSIVE.get(target.id)
                if permissive is not None and node.value.value is (not permissive):
                    return True
        # shape 2: a helper returning the decision. Every helper here treats
        # False as the permissive answer, so True is the safe one.
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant):
            if node.value.value is True:
                return True
    return False


def _guarded_handlers() -> list[tuple[Path, int, bool]]:
    """(file, handler line, closes_safely) for every try that reads the flag."""
    out: list[tuple[Path, int, bool]] = []
    for path in sorted(_ROOT.glob("apps/**/*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _FLAG_KEY not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if _FLAG_KEY not in body:
                continue
            for handler in node.handlers:
                out.append((path, handler.lineno, _closes_safely(handler, node)))
    return out


def test_no_review_gate_leaves_the_permissive_value_standing() -> None:
    handlers = _guarded_handlers()

    # Guard the measurement: if the shape changes so nothing is found, this
    # would pass having examined nothing (Factory#832).
    assert handlers, (
        f"no try/except reads {_FLAG_KEY} anywhere under apps/ -- either the "
        "gates moved or this test has stopped examining anything"
    )

    open_failing = [
        f"{p.relative_to(_ROOT)}:{ln}" for p, ln, safe in handlers if not safe
    ]
    assert not open_failing, (
        f"handler(s) {open_failing} swallow a metadata read failure and leave "
        "the permissive value standing. A task that asked for human review "
        "would proceed without it, and in agent_spec_creation.py the spec is "
        "auto-approved outright (AIFactory#1384)."
    )


def test_the_permissive_defaults_are_still_what_makes_this_matter() -> None:
    """If these defaults flipped, the handlers above would be redundant.

    Pinned so that a future change to either default fails here and tells the
    reader to re-check the handlers, rather than silently turning them into
    no-ops.
    """
    exec_src = (_ROOT / "apps/web-server/server/routes/execution.py").read_text(
        encoding="utf-8"
    )
    spec_src = (
        _ROOT / "apps/web-server/server/services/agent_spec_creation.py"
    ).read_text(encoding="utf-8")
    assert "require_review = False" in exec_src, (
        "require_review no longer defaults to False -- re-check the fail-closed "
        "handler is still meaningful"
    )
    assert "should_auto_approve = True" in spec_src, (
        "should_auto_approve no longer defaults to True -- re-check the "
        "fail-closed handler is still meaningful"
    )
