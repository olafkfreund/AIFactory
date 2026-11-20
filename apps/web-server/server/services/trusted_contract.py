"""Pure decision: which contract may the merge gate act on? (#1667)

Synchronous, no database I/O. The caller looks the record up (see
``trusted_contract_store.lookup``) and passes it in.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from server.services.trusted_contract_store import LOOKUP_FAILED, TrustedRecord, lookup

_BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core.contract_trust import (  # noqa: E402
    ENV,
    LEGACY,
    contract_digest,
    has_trusted_trace,
)

_ISOLATED_STAMPS = ("kubejob", "sandbox-pidns")


def host_isolated() -> bool:
    """True when THIS web server's environment isolates the agent (D4-i)."""
    from server.services.build_backend import selected_backend  # noqa: PLC0415
    from server.services.job_state_store import store_enabled  # noqa: PLC0415

    # Decided policy (#1760, #1724): kubejob with the durable store is the
    # only isolated host. Without DATABASE_URL kubejob falls back to in-pod
    # builds. Elsewhere, PID-namespaced sandbox included, the agent shares the
    # uid and can read the per-call GitHub token in hosts.yml via /proc/<child>.
    # A separate-uid design would need its own spec.
    return selected_backend() == "kubejob" and store_enabled()


def _record_verified(spec_dir: Path, record: TrustedRecord) -> bool:
    """The record's signature holds, its build was isolated, and the spec copy matches."""
    from trusted_plan import (  # type: ignore[import-not-found,unused-ignore] # noqa: PLC0415
        _canonical,
        verify_plan_signature,
    )

    ok, _reason = verify_plan_signature(record.contract)
    if not ok or record.build_isolation not in _ISOLATED_STAMPS:
        return False
    on_disk = json.loads((spec_dir / "context" / "task_contract.json").read_text())
    return bool(_canonical(on_disk) == _canonical(record.contract))


def handoff_contract(spec_dir: Path, trusted: Any) -> dict[str, Any] | None:
    """The contract to hand TFactory (D3): verified record, ``{}`` if held, else ``None``."""
    state, contract = resolve_contract(spec_dir, trusted)
    if state == "verified":
        return contract
    return {} if state == "hold" else None


def adopted_handoff(spec_dir: Path, trusted: Any) -> dict[str, Any] | None:
    """Return the verified adopted identity, holding if either disk copy differs."""
    state, contract = resolve_contract(spec_dir, trusted)
    if state == "hold":
        raise ValueError("trusted contract unavailable")
    provenance = contract.get("provenance") if state == "verified" else None
    if not isinstance(provenance, dict) or not provenance.get("source_branch"):
        if isinstance(provenance, dict) and provenance.get("pr_number"):
            raise ValueError("incomplete adopted handoff in trusted contract")
        try:
            metadata = json.loads((spec_dir / "task_metadata.json").read_text())
        except (OSError, ValueError):
            metadata = {}
        if isinstance(metadata, dict) and (
            metadata.get("source_branch") or metadata.get("pr_number")
        ):
            raise ValueError("adopted handoff has no verified contract")
        return None
    from trusted_plan import _safe_branch  # noqa: PLC0415

    if (
        not _safe_branch(provenance["source_branch"])
        or re.fullmatch(r"[0-9a-fA-F]{40}", provenance.get("approved_head_sha") or "")
        is None
        or not isinstance(provenance.get("repo"), str)
        or not provenance["repo"]
        or type(provenance.get("pr_number")) is not int
        or provenance["pr_number"] <= 0
    ):
        raise ValueError("incomplete adopted handoff in trusted contract")
    fields = {
        "source_branch": "source_branch",
        "approved_head_sha": "approved_head_sha",
        "pr_number": "pr_number",
        "repo": "github_repo",
        "base_ref": "base_branch",
    }
    try:
        metadata = json.loads((spec_dir / "task_metadata.json").read_text())
        requirements = json.loads((spec_dir / "requirements.json").read_text())
        request_provenance = requirements["provenance"]
        if not isinstance(metadata, dict) or not isinstance(request_provenance, dict):
            raise ValueError
        if any(
            metadata.get(target) != provenance.get(source)
            or request_provenance.get(source) != provenance.get(source)
            for source, target in fields.items()
            if source in provenance
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("adopted handoff differs from trusted contract") from exc
    return provenance


async def spawn_env(spec_dir: Path) -> dict[str, str]:
    """The verdict handed to run.py (#1673): a digest if verified, ``hold``, or ``legacy``."""
    state, contract = resolve_contract(spec_dir, await lookup(spec_dir))
    if state == "verified":
        return {ENV: contract_digest(contract)}
    return {ENV: "hold"} if state == "hold" else {ENV: LEGACY}


def resolve_contract(spec_dir: Path, trusted: Any) -> tuple[str, dict[str, Any]]:
    """Return ``(state, contract)``; state is ``verified``, ``hold`` or ``legacy``."""
    try:
        from pfactory.tfactory_client import (  # type: ignore[import-not-found,unused-ignore] # noqa: PLC0415
            load_task_contract,
        )
    except ImportError:
        return "hold", {}

    spec_dir = Path(spec_dir)
    try:
        if isinstance(trusted, TrustedRecord):
            # D4: on a host that does not isolate the agent, the record itself
            # may be forged, so no consumer (merge, path floor, TFactory
            # handoff) may treat it as verified.
            if host_isolated() and _record_verified(spec_dir, trusted):
                return "verified", trusted.contract
            return "hold", {}
        # LOOKUP_FAILED, or no record but a trusted trace (D5 + (c): no stamp).
        if trusted is LOOKUP_FAILED or has_trusted_trace(spec_dir):
            return "hold", {}
    except Exception:  # noqa: BLE001 - any doubt holds; only legacy keeps old behaviour
        return "hold", {}
    return "legacy", load_task_contract(spec_dir)
