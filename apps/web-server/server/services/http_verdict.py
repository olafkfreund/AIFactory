"""Make a handler's HTTP status agree with the verdict in its own body.

Factory#460: the worktree routes answer with a ``{"success": bool, "error": str}``
envelope and returned refusals -- GitHub declining a merge, a missing worktree,
an unparseable task id -- inside an HTTP **200**. A 200 that means "failed" is a
broken contract: every client then has to know, out of band, that this
particular service lies on the status line. The cockpit did not know, so it
reported a refused merge as "Done." and wrote ``ok=true`` into its audit trail.

Rather than edit each of the ~25 failure returns in those handlers -- which
fixes the ones that exist today and none of the ones added tomorrow -- the
decorator sits at the seam and translates. The JSON body is unchanged, so every
consumer that already reads ``success`` keeps working byte for byte; only the
status line stops lying.

One status for every refusal: 409 Conflict. These are all "the current state of
the repository does not permit this" -- no worktree, no such project, a PR
GitHub will not merge -- and the body still carries the precise reason. A
per-site 400/404/409 mapping would be guesswork over error strings, and no
client needs the distinction: they need "this did not happen".
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

REFUSED_STATUS = 409

P = ParamSpec("P")
R = TypeVar("R")


def _translate(result: R) -> R | JSONResponse:
    """A body that says ``success: False`` becomes a 409 carrying that body."""
    if isinstance(result, dict) and result.get("success") is False:
        return JSONResponse(
            status_code=REFUSED_STATUS, content=jsonable_encoder(result)
        )
    return result


def honest_status(handler: Callable[P, Any]) -> Callable[P, Any]:
    """Answer ``409`` when the handler's own body says ``success: False``.

    ``functools.wraps`` keeps ``__wrapped__`` intact, so FastAPI's
    ``inspect.signature`` still sees the real parameters and every ``Depends``
    (notably ``require_task_access``) resolves exactly as before.

    In-process callers -- ``mcp_stdio``'s proxies call these handlers directly
    rather than over HTTP -- receive the ``JSONResponse``, which FastAPI returns
    verbatim, so the honest status propagates through that surface too.

    **A sync handler stays sync** (AIFactory#1126). FastAPI runs a ``def``
    endpoint in a threadpool and an ``async def`` one on the event loop, so
    wrapping a sync handler in an async wrapper would silently move it onto the
    loop. The two sync handlers in the #1126 sweep are
    ``cli_accounts.install_or_update_cli`` and ``github.install_github_cli`` --
    both shell out to a package-manager install that runs for tens of seconds,
    which is precisely the work that must not block the loop. Branching on
    ``iscoroutinefunction`` keeps each handler on the execution model FastAPI
    chose for it.
    """
    if inspect.iscoroutinefunction(handler):

        @wraps(handler)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            return _translate(await handler(*args, **kwargs))

        return async_wrapper

    @wraps(handler)
    def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
        return _translate(handler(*args, **kwargs))

    return sync_wrapper
