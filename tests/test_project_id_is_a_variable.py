"""`AIFACTORY_PROJECT_ID` is a repository VARIABLE, never a secret (#1389/#1390).

`${{ secrets.X }}` on a name that is stored as a variable does not error --
it resolves to the empty string. So the workflow ran, posted
`project_id: ""`, got a 404, and (with `curl -sf`) printed nothing and exited
22. The value was set the whole time; it was being read from the wrong
namespace.

That failure is invisible in review: both spellings look correct, and the only
symptom is downstream. This test pins the namespace so the mistake cannot
return silently in a new workflow.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = sorted(
    (Path(__file__).parent.parent / ".github" / "workflows").glob("*.yml")
)

_SECRETS_REF = re.compile(r"\$\{\{\s*secrets\.AIFACTORY_PROJECT_ID\s*\}\}")
_VARS_REF = re.compile(r"\$\{\{\s*vars\.AIFACTORY_PROJECT_ID\s*\}\}")


def test_there_are_workflows_to_check() -> None:
    """Guard the guard: an empty glob would make every test below vacuous."""
    assert WORKFLOWS, (
        "no workflow files found -- this suite would pass by examining nothing"
    )


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_project_id_is_never_read_from_secrets(wf: Path) -> None:
    text = wf.read_text(encoding="utf-8")
    assert not _SECRETS_REF.search(text), (
        f"{wf.name} reads AIFACTORY_PROJECT_ID from `secrets.`, which resolves to "
        "an empty string for a repository variable (#1389). Use `vars.`."
    )


def test_at_least_one_workflow_actually_uses_it() -> None:
    """Otherwise the assertion above passes because nothing references it at all."""
    users = [
        w.name for w in WORKFLOWS if _VARS_REF.search(w.read_text(encoding="utf-8"))
    ]
    assert users, (
        "no workflow reads AIFACTORY_PROJECT_ID from `vars.` -- the check above proves nothing"
    )
