"""Apply an external correction to an existing spec (TFactory epic #182, #317).

This is AIFactory's inbound receiver for the TFactory→AIFactory hand-back. When
TFactory's tests find problems in a feature, it POSTs a correction here; this
module writes ``QA_FIX_REQUEST.md`` onto the *original* spec and runs the
existing QA Fixer (``qa.fixer.run_qa_fixer_session``) — the agent already built
to read that file.

Confirm-first, mirroring the MCP write tools:

  - ``confirm=False`` → preview. Nothing is written, nothing runs.
  - ``confirm=True``  → write ``QA_FIX_REQUEST.md`` + trigger the QA Fixer.

The fixer is injected via ``fixer_fn`` so unit tests need no SDK. The default
(``_default_fixer``) schedules the real fixer as a background task and returns
immediately — running it inline would block the HTTP request for the whole fix.
The default path requires a live SDK/provider and is verified end-to-end (both
apps running), not in unit tests.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

__all__ = ["apply_correction", "write_fix_request"]

Fixer = Callable[[Path], Awaitable[dict]]

_FIX_REQUEST_NAME = "QA_FIX_REQUEST.md"
_log = logging.getLogger(__name__)


def write_fix_request(spec_dir: Path | str, fix_request_md: str) -> Path:
    """Write the fix-request markdown into the spec dir; return its path."""
    target = Path(spec_dir) / _FIX_REQUEST_NAME
    target.write_text(fix_request_md)
    return target


async def _run_fixer_bg(spec_dir: Path) -> None:
    """Run a real QA-fixer session to completion (detached background task).

    Mirrors ``qa/loop.py``'s human-feedback fixer path (model + provider
    resolution). SDK imports are lazy so this module stays import-light.
    Best-effort: failures are logged, never raised.
    """
    try:
        from core.client import create_client
        from phase_config import (
            get_phase_model,
            get_phase_thinking_budget,
            get_provider_extra_kwargs,
            infer_provider_from_model,
        )
        from providers.factory import get_provider

        from .fixer import run_qa_fixer_session

        project_dir = spec_dir.parent.parent.parent
        qa_model = get_phase_model(spec_dir, "qa_fixer", None)
        budget = get_phase_thinking_budget(spec_dir, "qa_fixer")
        provider = infer_provider_from_model(qa_model)

        if provider == "claude":
            client = create_client(
                project_dir,
                spec_dir,
                qa_model,
                agent_type="qa_fixer",
                max_thinking_tokens=budget,
            )
        else:
            client = get_provider(
                provider,
                phase="qa_fixer",
                model=qa_model,
                working_dir=project_dir,
                **get_provider_extra_kwargs(provider, qa_model),
            )
        async with client:
            await run_qa_fixer_session(client, spec_dir, 0, False)
    except Exception:  # noqa: BLE001 — background best-effort
        _log.exception("background QA fixer failed for %s", spec_dir)


async def _default_fixer(spec_dir: Path) -> dict:
    """Schedule a real QA-fixer run in the background; return immediately.

    Running it inline would block the HTTP request for the whole fix; the
    background task surfaces progress through the usual task status.
    """
    asyncio.create_task(_run_fixer_bg(spec_dir))
    return {"status": "qa_fixing", "scheduled": True}


async def apply_correction(
    spec_dir: Path | str,
    fix_request_md: str,
    *,
    confirm: bool,
    fixer_fn: Fixer | None = None,
) -> dict:
    """Write a fix-request onto a spec and (on confirm) run the QA Fixer.

    Args:
        spec_dir: the resolved ``.aifactory/specs/<spec_id>/`` dir. The caller
            (the REST route) validates it exists before calling.
        fix_request_md: the ``QA_FIX_REQUEST.md`` body from TFactory.
        confirm: ``False`` previews; ``True`` writes + triggers the fixer.
        fixer_fn: injectable async fixer (tests pass a fake). Defaults to the
            real background runner.

    Returns:
        A JSON-able dict describing what happened.
    """
    spec = Path(spec_dir)
    target = spec / _FIX_REQUEST_NAME

    if not confirm:
        return {
            "success": True,
            "confirm": False,
            "started": False,
            "would_write": str(target),
            "message": (
                "Preview only. Re-call with confirm=true to write "
                "QA_FIX_REQUEST.md and run the QA Fixer."
            ),
        }

    await asyncio.to_thread(write_fix_request, spec, fix_request_md)
    fixer_result = await (fixer_fn or _default_fixer)(spec)
    return {
        "success": True,
        "confirm": True,
        "started": True,
        "wrote": str(target),
        **fixer_result,
    }
