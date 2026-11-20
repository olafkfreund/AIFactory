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
import contextlib
import json
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from core.child_env import child_env
from core.git_credentials import authed_push_url

__all__ = [
    "apply_correction",
    "check_fix_cycle_assertions",
    "record_handback",
    "write_fix_request",
]

Fixer = Callable[[Path], Awaitable[dict]]

_FIX_REQUEST_NAME = "QA_FIX_REQUEST.md"
# Audit record of an inbound handback: correlation key, manifest hash, and the
# baseline assertion snapshot the assertion-pinning guard diffs against (#467).
_HANDBACK_RECORD = "handback_received.json"
_GUARD_REPORT = "handback_assertion_guard.json"
_log = logging.getLogger(__name__)


async def publish_correction(
    spec_dir: Path, project_dir: Path, branch: str, paths: list[str] | None = None
) -> bool:
    """Commit a fixed task branch, push it, then report its new head."""
    from pfactory.tfactory_client import send_fix_completed
    from trusted_plan import _safe_branch

    if not _safe_branch(branch):
        return False

    def git(
        *args: str, env: dict[str, str] | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=project_dir,
            env=env if env is not None else child_env(),
            capture_output=True,
            text=True,
            timeout=120,
            check=check,
        )

    try:
        if git("symbolic-ref", "--short", "HEAD").stdout.strip() != branch:
            return False
        git("add", "-A", "--", *(paths if paths is not None else ["."]))
        diff = git("diff", "--cached", "--quiet", check=False).returncode
        if diff == 0:
            return False
        if diff != 1:
            return False
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _log.warning("correction commit preparation failed")
        return False
    try:
        git(
            "-c",
            "user.name=AIFactory",
            "-c",
            "user.email=aifactory@localhost",
            "commit",
            "-m",
            "fix: apply TFactory correction",
        )
        url = git("remote", "get-url", "origin").stdout.strip()
        with authed_push_url(url) as (push_url, env):
            git("push", push_url, f"HEAD:refs/heads/{branch}", env=env)
        head = git("rev-parse", "HEAD").stdout.strip()
        result = await send_fix_completed(spec_dir, spec_dir.name, head_sha=head)
        return bool(result.get("sent"))
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _log.warning("correction push or callback failed")
        return False


def write_fix_request(spec_dir: Path | str, fix_request_md: str) -> Path:
    """Write the fix-request markdown into the spec dir; return its path."""
    target = Path(spec_dir) / _FIX_REQUEST_NAME
    target.write_text(fix_request_md)
    return target


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _project_dir_for(spec_dir: Path) -> Path:
    """The project root for a ``.aifactory/specs/<id>/`` spec dir."""
    if spec_dir.parent.name != "specs" or spec_dir.parent.parent.name != ".aifactory":
        return spec_dir
    return spec_dir.parent.parent.parent


def record_handback(
    spec_dir: Path,
    *,
    source: str | None,
    correlation_key: str | None,
    manifest_hash: str | None,
    failing_test_count: int,
    triage_present: bool,
    tfactory_task_id: str | None = None,
    pre_fix_sha: str | None = None,
) -> dict:
    """Persist the handback audit record + assertion baseline against the spec.

    Records the TFactory manifest hash for audit (#467) and snapshots AIFactory's
    own test assertions *now* — before the fixer edits anything — so the
    end-of-cycle guard can prove the fixer didn't weaken them. Best-effort.
    """
    baseline: dict[str, int] = {}
    try:
        from .assertion_guard import snapshot_test_assertions

        baseline = snapshot_test_assertions(_project_dir_for(spec_dir))
    except Exception:  # noqa: BLE001 — snapshot is best-effort
        _log.debug("assertion baseline snapshot failed", exc_info=True)

    record = {
        "received_at": _now_iso(),
        "source": source,
        "correlation_key": correlation_key,
        "manifest_hash": manifest_hash,
        "failing_test_count": failing_test_count,
        "triage_present": triage_present,
        "tfactory_task_id": tfactory_task_id,
        "pre_fix_sha": pre_fix_sha,
        "assertion_baseline": baseline,
    }
    try:
        (spec_dir / _HANDBACK_RECORD).write_text(json.dumps(record, indent=2))
    except OSError:
        _log.debug("could not write handback record", exc_info=True)
    # Observability AC: surface manifest hash keyed by correlation_key.
    _log.info(
        "handback received correlation_key=%s manifest_hash=%s source=%s failing=%d",
        correlation_key,
        manifest_hash,
        source,
        failing_test_count,
    )
    return record


def check_fix_cycle_assertions(spec_dir: Path | str) -> dict:
    """After a fix cycle, flag any AIFactory test that lost/dropped assertions.

    Diffs the current test-assertion snapshot against the baseline recorded at
    handback receipt. Writes a guard report and logs violations keyed by the
    handback's correlation_key. Returns the report dict (``ok`` + violations).
    A weakened/removed assertion is *flagged*, not silently accepted (#467).
    Best-effort; never raises.
    """
    spec = Path(spec_dir)
    try:
        from .assertion_guard import guard_assertion_manifest, snapshot_test_assertions

        record = json.loads((spec / _HANDBACK_RECORD).read_text())
        baseline = record.get("assertion_baseline") or {}
        after = snapshot_test_assertions(_project_dir_for(spec))
        report = guard_assertion_manifest(baseline, after)
        out = {
            "ok": report.ok,
            "correlation_key": record.get("correlation_key"),
            "manifest_hash": record.get("manifest_hash"),
            "violations": [v.to_dict() for v in report.violations],
            "checked_at": _now_iso(),
        }
        (spec / _GUARD_REPORT).write_text(json.dumps(out, indent=2))
        if not report.ok:
            _log.warning(
                "assertion-guard FLAGGED correlation_key=%s: %d test file(s) "
                "weakened/removed during fix cycle: %s",
                out["correlation_key"],
                len(report.violations),
                [v.path for v in report.violations],
            )
        return out
    except FileNotFoundError:
        return {"ok": True, "violations": [], "skipped": "no handback baseline"}
    except Exception:  # noqa: BLE001 — guard is best-effort
        _log.debug("assertion-guard check failed", exc_info=True)
        return {"ok": True, "violations": [], "skipped": "error"}


async def _run_fixer_bg(spec_dir: Path) -> bool:
    """Run a real QA-fixer session to completion (detached background task).

    Mirrors ``qa/loop.py``'s human-feedback fixer path (model + provider
    resolution). SDK imports are lazy so this module stays import-light.
    Best-effort: failures are logged, never raised. Returns True only when the
    fixer session reported ``"fixed"`` (the Job entrypoint exits on it, #2116).
    """
    fixed = False
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
        handback = False
        with contextlib.suppress(OSError, ValueError):
            handback = bool(
                json.loads((spec_dir / _HANDBACK_RECORD).read_text()).get(
                    "tfactory_task_id"
                )
            )
        job_fixer = bool(os.environ.get("AIFACTORY_CORRECTION_FETCH_ID"))
        if handback and not job_fixer:
            project_dir = project_dir / ".aifactory/worktrees/tasks" / spec_dir.name
            if not project_dir.is_dir():
                return False
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
            status, _ = await run_qa_fixer_session(
                client, spec_dir, 0, False, project_dir=project_dir
            )
        fixed = status == "fixed"
        if fixed and handback and not job_fixer:
            from pfactory.tfactory_client import _build_branch

            fixed = await publish_correction(
                spec_dir, project_dir, _build_branch(spec_dir.name, spec_dir)
            )
    except Exception:  # noqa: BLE001 — background best-effort
        _log.exception("background QA fixer failed for %s", spec_dir)
    finally:
        # Assertion-pinning guard (#467): the cycle is over — verify the fixer
        # didn't weaken AIFactory's own test assertions to force a pass. Runs
        # even on fixer failure so a crashed-after-editing cycle is still
        # checked. Best-effort; only flags when a baseline was recorded.
        await asyncio.to_thread(check_fix_cycle_assertions, spec_dir)
    return fixed


_BACKGROUND_FIXERS: set[asyncio.Task[bool]] = set()


async def _default_fixer(spec_dir: Path) -> dict:
    """Schedule a real QA-fixer run in the background; return immediately.

    Running it inline would block the HTTP request for the whole fix; the
    background task surfaces progress through the usual task status.
    """
    # A STRONG reference, held until the fixer finishes (#1484). The event loop
    # keeps only a weak one, so a bare ``create_task(...)`` here could be
    # garbage-collected mid-run -- with no exception and no log -- after this
    # function has already answered ``scheduled: True``.
    task = asyncio.create_task(_run_fixer_bg(spec_dir))
    _BACKGROUND_FIXERS.add(task)
    task.add_done_callback(_BACKGROUND_FIXERS.discard)
    return {"status": "qa_fixing", "scheduled": True}


async def apply_correction(
    spec_dir: Path | str,
    fix_request_md: str,
    *,
    confirm: bool,
    fixer_fn: Fixer | None = None,
    triage: dict | None = None,
    manifest_hash: str | None = None,
    correlation_key: str | None = None,
    tfactory_task_id: str | None = None,
) -> dict:
    """Write a fix-request onto a spec and (on confirm) run the QA Fixer.

    Args:
        spec_dir: the resolved ``.aifactory/specs/<spec_id>/`` dir. The caller
            (the REST route) validates it exists before calling.
        fix_request_md: the ``QA_FIX_REQUEST.md`` body from TFactory.
        confirm: ``False`` previews; ``True`` writes + triggers the fixer.
        fixer_fn: injectable async fixer (tests pass a fake). Defaults to the
            real background runner.
        triage: optional *structured* triage report (TFactory#283). When present
            it is schema-validated (#467) and the QA Fixer runs only if it is
            valid; when absent the legacy markdown-only path is accepted
            unchanged (non-breaking — TFactory still POSTs markdown today).
        manifest_hash: optional assertion-manifest hash from TFactory (#283),
            recorded against the work item for audit. Falls back to a hash
            embedded in ``triage`` when not passed explicitly.
        correlation_key: RFC-0001 key for keyed logging/observability.

    Returns:
        A JSON-able dict describing what happened. On a malformed ``triage`` the
        QA Fixer does NOT run; the dict carries ``rejected: True`` + errors.
    """
    spec = Path(spec_dir)
    target = spec / _FIX_REQUEST_NAME

    # ── Typed-contract gate (#467) ────────────────────────────────────────────
    source: str | None = None
    failing_count = 0
    if triage is not None:
        from .handback_contract import validate_triage_report

        validation = validate_triage_report(triage)
        source = validation.source
        failing_count = validation.failing_test_count
        manifest_hash = manifest_hash or validation.manifest_hash
        correlation_key = correlation_key or validation.correlation_key
        if not validation.ok:
            _log.warning(
                "handback REJECTED correlation_key=%s: malformed triage report: %s",
                correlation_key,
                "; ".join(validation.errors),
            )
            return {
                "success": False,
                "rejected": True,
                "started": False,
                "correlation_key": correlation_key,
                "contract_version": validation.contract_version,
                "validation_errors": validation.errors,
                "message": (
                    "Triage report failed schema validation — QA Fixer not run. "
                    "Fix the report and resend."
                ),
            }

    if not confirm:
        return {
            "success": True,
            "confirm": False,
            "started": False,
            "would_write": str(target),
            "correlation_key": correlation_key,
            "manifest_hash": manifest_hash,
            "message": (
                "Preview only. Re-call with confirm=true to write "
                "QA_FIX_REQUEST.md and run the QA Fixer."
            ),
        }

    from pfactory.tfactory_client import _locate_build_commit

    located = _locate_build_commit(spec, spec.name) if tfactory_task_id else None
    pre_fix_sha = located[1] if located else None
    receipt = spec / _HANDBACK_RECORD
    if tfactory_task_id and manifest_hash and receipt.exists():
        try:
            previous = json.loads(receipt.read_text())
        except (OSError, ValueError):
            previous = {}
        if (
            previous.get("tfactory_task_id") == tfactory_task_id
            and previous.get("manifest_hash") == manifest_hash
            and previous.get("pre_fix_sha") == pre_fix_sha
        ):
            return {
                "success": True,
                "confirm": True,
                "started": False,
                "duplicate": True,
            }

    # Record the handback (manifest hash for audit + assertion baseline) before
    # anything writes to the tree, so the end-of-cycle guard has a clean before.
    await asyncio.to_thread(
        record_handback,
        spec,
        source=source,
        correlation_key=correlation_key,
        manifest_hash=manifest_hash,
        failing_test_count=failing_count,
        triage_present=triage is not None,
        tfactory_task_id=tfactory_task_id,
        pre_fix_sha=pre_fix_sha,
    )

    await asyncio.to_thread(write_fix_request, spec, fix_request_md)
    fixer_result = await (fixer_fn or _default_fixer)(spec)
    return {
        "success": True,
        "confirm": True,
        "started": True,
        "wrote": str(target),
        "correlation_key": correlation_key,
        "manifest_hash": manifest_hash,
        **fixer_result,
    }
