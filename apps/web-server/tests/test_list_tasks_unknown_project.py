"""GET /api/tasks?project_id=<unregistered> must 404, not 500.

The ``status`` query parameter of ``list_tasks`` shadowed starlette's
``status`` module, so the 404 branch raised AttributeError instead.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from server.routes import tasks as tasks_mod


def test_unknown_project_id_is_404():
    with (
        patch.object(tasks_mod, "load_projects", return_value={"p1": {"path": "/x"}}),
        pytest.raises(HTTPException) as exc,
    ):
        asyncio.run(
            tasks_mod.list_tasks(project_id="bogus", status=None, request=None, db=None)
        )
    assert exc.value.status_code == 404
