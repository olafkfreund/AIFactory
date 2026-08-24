"""`spec_id` cannot escape the specs directory (#1410).

`safe_spec_component` existed and was applied in the ROUTE handlers, which
sanitise `spec_id` by reassignment before joining. The service layer built the
same paths itself and guarded only 2 of its 31 joins across 13 modules.
`agent_kubejob` was the clearest -- it split a job id on ":" and joined the tail
onto a path with nothing in between.

`specpath.py`'s own docstring already claimed services "keep their own barrier
for the paths they build directly". They did not, and CodeQL's sanitizer-aware
rule said so (`py/path-injection-sanitized`, alert 2422).

The fix moves the join itself behind the barrier rather than adding a guard at
each call site, because a per-site guard leaves the next site unguarded. These
tests pin BOTH halves: the constructor rejects traversal, and no service module
still builds the path by hand.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.specpath import spec_dir_for

_SERVICES = Path(__file__).parent.parent / "apps" / "web-server" / "server" / "services"
_RAW_JOIN = re.compile(r'/\s*"specs"\s*/\s*\w+')


@pytest.mark.parametrize(
    "evil",
    [
        "../../etc",
        "..",
        ".",
        "a/b",
        "/absolute",
        "spec/../../../root",
        "",
        "..%2f..%2fetc",
    ],
)
def test_traversal_components_are_rejected(evil: str, tmp_path: Path) -> None:
    """Raising, not sanitising: a rewritten id hides a caller bug or an attack."""
    with pytest.raises(ValueError):
        spec_dir_for(tmp_path, evil)


def test_a_normal_spec_id_builds_the_expected_path(tmp_path: Path) -> None:
    assert spec_dir_for(tmp_path, "001-demo") == (
        tmp_path / ".aifactory" / "specs" / "001-demo"
    )


def test_the_result_stays_inside_the_specs_root(tmp_path: Path) -> None:
    """The property that matters, stated directly rather than implied."""
    root = (tmp_path / ".aifactory" / "specs").resolve()
    got = spec_dir_for(tmp_path, "042-thing").resolve()

    assert got.is_relative_to(root)


def test_no_service_module_builds_the_path_by_hand() -> None:
    """The wiring half.

    A correct constructor nobody calls fixes nothing, and this defect was
    precisely "the barrier exists but this layer does not use it". Asserting the
    raw join is gone is what stops it coming back one call site at a time.
    """
    offenders = sorted(
        f.name for f in _SERVICES.glob("*.py") if _RAW_JOIN.search(f.read_text())
    )

    assert not offenders, (
        "these service modules join a spec id onto a path directly instead of "
        f"using spec_dir_for(): {offenders}"
    )


def test_there_are_service_modules_to_check() -> None:
    """Guard the guard -- an empty glob would make the check above vacuous."""
    assert len(list(_SERVICES.glob("*.py"))) > 10
