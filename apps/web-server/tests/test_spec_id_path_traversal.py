"""spec_id must be validated before it is joined onto a path (#1056).

Two kinds of test here, deliberately:

* behavioural, on ``safe_spec_component`` itself, and
* structural, asserting that every route which splits ``task_id`` barriers the
  result. The structural one is the point: the original defect was not a bad
  validator, it was a good validator that eleven call sites never called. A
  purely behavioural suite would have passed against the vulnerable code.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.specpath import contained_path, safe_spec_component  # noqa: E402

_ROUTES = _WEB_SERVER / "server" / "routes"

# Every route module that splits a request-supplied task_id into a spec_id.
_SPLIT_RE = re.compile(
    r"^\s*(?:project_id|_), spec_id = task_id\.split\(\":\", 1\)", re.M
)


_HOSTILE = (
    "../../etc",
    "..",
    ".",
    "../",
    "a/../../b",
    "/etc/passwd",
    "a/b",
    "spec\x00null",
    "",
)

_LEGITIMATE = ("001-add-auth", "spec_42", "a.b-c_1", "X" * 255)


def test_traversal_components_are_rejected() -> None:
    # A plain loop rather than @pytest.mark.parametrize: with
    # --ignore-missing-imports (the test relax set) pytest resolves to Any, so
    # the decorator untypes the function and mypy --strict reports
    # untyped-decorator. Widening the fleet-wide relax set to accommodate one
    # test file would be the tail wagging the dog.
    for hostile in _HOSTILE:
        with pytest.raises(ValueError):
            safe_spec_component(hostile)


def test_legitimate_spec_ids_are_accepted() -> None:
    for legit in _LEGITIMATE:
        assert safe_spec_component(legit) == legit


def test_path_join_silently_collapses_traversal() -> None:
    """The reason validation must happen BEFORE the join, not after.

    This is what makes the barrier necessary rather than merely tidy: the join
    itself raises nothing and looks correct at the call site.
    """
    joined = Path("/srv/specs") / "../../etc"
    assert Path("/etc") == Path(joined).resolve()


def test_every_task_id_split_is_followed_by_a_barrier() -> None:
    """Regression lock for the actual defect (#1056).

    A new route that parses task_id and forgets safe_spec_component
    reintroduces the traversal. Fails loudly here instead of silently
    shipping.
    """
    offenders: list[str] = []
    for module in sorted(_ROUTES.glob("*.py")):
        text = module.read_text()
        for match in _SPLIT_RE.finditer(text):
            # The barrier must appear within a few lines of the split, before
            # any path expression can consume the value.
            window = text[match.end() : match.end() + 600]
            if "safe_spec_component" not in window:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{module.name}:{line}")
    assert not offenders, (
        "task_id split without a safe_spec_component barrier at: "
        + ", ".join(offenders)
    )


def test_bare_task_id_branches_are_also_barriered() -> None:
    """The ``else: spec_id = task_id`` branches carry request data too.

    projects.py accepts a bare id as well as project_id:spec_id. That branch
    skips the split, so the split-based check above cannot see it — and it is
    not one bit safer.
    """
    text = (_ROUTES / "projects.py").read_text()
    bare = text.count("            spec_id = task_id\n")
    assert bare > 0, "shape changed; update this test rather than deleting it"
    assert text.count("spec_id = safe_spec_component(spec_id)") >= bare


def test_every_spec_path_join_in_a_route_is_barriered() -> None:
    """The split-based check above is not enough, and that cost us two routes.

    ``worktree_merge.resolve_git_merge_conflicts`` used ``task_id`` WHOLE
    instead of splitting it, so the split regex never saw it and it joined an
    unvalidated request value onto the specs path -- then read and WROTE files
    under it. ``get_worktree_diff`` had the barrier, but inside the ``if ":" in
    task_id`` branch only, so the bare-id branch walked straight past it.

    This test keys off the join rather than the parse: any route that puts a
    bare ``task_id``/``spec_id`` into a path must have called the barrier
    earlier in the same function, whichever way it got there.
    """
    join_re = re.compile(
        r'/ "specs" / (?:task_id|spec_id)\b|/ "tasks" / (?:task_id|spec_id)\b'
    )
    def_re = re.compile(r"^(?:async )?def ", re.M)
    offenders: list[str] = []
    for module in sorted(_ROUTES.glob("*.py")):
        text = module.read_text()
        starts = [m.start() for m in def_re.finditer(text)]
        for match in join_re.finditer(text):
            # Everything from the top of the enclosing function to the join.
            enclosing = max((s for s in starts if s < match.start()), default=0)
            if "safe_spec_component" not in text[enclosing : match.start()]:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{module.name}:{line}")
    assert not offenders, (
        "spec path join with no safe_spec_component earlier in the same "
        "function at: " + ", ".join(offenders)
    )


# --------------------------------------------------------------------------
# contained_path: the whole-path half of the same story
# --------------------------------------------------------------------------


def test_contained_path_accepts_paths_inside_a_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    assert contained_path(root / "src", [root], "project") == (root / "src").resolve()
    # The root itself is inside the root.
    assert contained_path(root, [root], "project") == root.resolve()


def test_contained_path_rejects_traversal_out_of_the_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    for hostile in ("../../etc/passwd", "..", "/etc/passwd", "src/../../.."):
        with pytest.raises(ValueError):
            contained_path(root / hostile, [root], "project")


def test_contained_path_rejects_a_symlink_that_escapes(tmp_path: Path) -> None:
    """Resolve-then-check, not check-then-resolve.

    A helper that tested the unresolved string would pass this: the literal
    path is inside the root. Only the resolved form shows the escape, which is
    why the resolve lives inside the helper.
    """
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    with pytest.raises(ValueError):
        contained_path(root / "escape", [root], "project")


def test_contained_path_does_not_treat_a_sibling_prefix_as_containment(
    tmp_path: Path,
) -> None:
    """``/srv/proj-evil`` is not inside ``/srv/proj``.

    A ``str.startswith`` implementation gets this wrong. The parents-based test
    does not, and this test is what stops someone "simplifying" it back.
    """
    root = tmp_path / "proj"
    root.mkdir()
    sibling = tmp_path / "proj-evil"
    sibling.mkdir()
    with pytest.raises(ValueError):
        contained_path(sibling, [root], "project")


def test_contained_path_is_not_satisfied_by_mere_existence(tmp_path: Path) -> None:
    """The barrier registered in CodeQL must constrain WHICH path is reached.

    ``/etc/passwd`` exists and is readable; an existence check would clear it.
    If this ever passes, the barrier in PathInjectionSanitized.ql has become a
    lie and every alert it clears is unproven.
    """
    root = tmp_path / "project"
    root.mkdir()
    assert Path("/etc/passwd").exists()
    with pytest.raises(ValueError):
        contained_path("/etc/passwd", [root], "project")
