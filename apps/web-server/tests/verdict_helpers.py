"""Read a handler's verdict off the result, whatever shape it arrives in.

`honest_status` (AIFactory#1126, Factory#460) makes a handler that refuses
answer 409 instead of 200. Tests that call a handler **directly** -- as several
in this suite do, to avoid standing up a TestClient -- therefore receive a
``JSONResponse`` for a refusal and a plain ``dict`` for a success.

`verdict()` returns ``(status, body)`` for both, so a test can assert the thing
that actually matters: **the status line**. A test that asserted only the body
would pass just as happily against the unfixed code -- which is the exact trap
#1126 is about -- so prefer ``assert status == REFUSED_STATUS`` over
``assert body["success"] is False`` alone.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.responses import JSONResponse

OK_STATUS = 200


def verdict(result: object) -> tuple[int, dict[str, Any]]:
    """``(status_code, body)`` for a handler result called in-process."""
    if isinstance(result, JSONResponse):
        body: dict[str, Any] = json.loads(bytes(result.body))
        return result.status_code, body
    assert isinstance(result, dict), f"unexpected handler result: {type(result)!r}"
    return OK_STATUS, result


def body(result: object) -> dict[str, Any]:
    """Just the body, for assertions about the payload rather than the status."""
    return verdict(result)[1]
