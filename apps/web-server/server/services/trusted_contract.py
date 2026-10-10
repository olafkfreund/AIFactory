"""Pure decision: which contract may the merge gate act on? (#1667)

Synchronous, no database I/O. The caller looks the record up (see
``trusted_contract_store.lookup``) and passes it in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from server.services.trusted_contract_store import LOOKUP_FAILED, TrustedRecord, lookup

_BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core.contract_trust import ENV, contract_digest, has_trusted_trace  # noqa: E402

_ISOLATED_STAMPS = ("kubejob", "sandbox-pidns")


def host_isolated() -> bool:
    """True when THIS web server's environment isolates the agent (D4-i)."""
    from server.services.build_backend import selected_backend  # noqa: PLC0415
    from server.services.job_state_store import store_enabled  # noqa: PLC0415

    # Without DATABASE_URL the kubejob backend falls back to in-pod builds.
    # D4 narrowed (#1667 review): a PID-namespaced sandbox is not enough while
    # run.py inside it carries the server's env (#1680); the agent could read
    # DATABASE_URL and rewrite its own record. Revisit when #1680 lands.
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


async def spawn_env(spec_dir: Path) -> dict[str, str]:
    """The verdict handed to run.py (#1673): a digest if verified, ``hold``, or nothing."""
    state, contract = resolve_contract(spec_dir, await lookup(spec_dir))
    if state == "verified":
        return {ENV: contract_digest(contract)}
    return {ENV: "hold"} if state == "hold" else {}


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
