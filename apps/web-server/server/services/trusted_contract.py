"""Pure decision: which contract may the merge gate act on? (#1667)

Synchronous, no database I/O. The caller looks the record up (see
``trusted_contract_store.lookup``) and passes it in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_ISOLATED_STAMPS = ("kubejob", "sandbox-pidns")


def host_isolated() -> bool:
    """True when THIS web server's environment isolates the agent (D4-i)."""
    from . import sandbox
    from .build_backend import selected_backend

    if selected_backend() == "kubejob":
        return True
    return bool(
        sandbox.is_enabled()
        and sandbox._mode() in ("fs", "strict")
        and sandbox._pidns_enabled()
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def has_trusted_trace(spec_dir: Path) -> bool:
    """True when the spec dir shows the task came through the trusted-plan path."""
    spec_dir = Path(spec_dir)
    for rel in ("context/task_contract.json", "implementation_plan.json"):
        data = _read_json(spec_dir / rel)
        if isinstance(data, dict) and "approval" in data:
            return True
    req = _read_json(spec_dir / "requirements.json")
    prov = req.get("provenance") if isinstance(req, dict) else None
    return isinstance(prov, dict) and prov.get("trusted_plan") is True


def resolve_contract(spec_dir: Path, trusted: Any) -> tuple[str, dict[str, Any]]:
    """Return ``(state, contract)``; state is ``verified``, ``hold`` or ``legacy``."""
    try:
        from pfactory.tfactory_client import (  # type: ignore[import-not-found,unused-ignore] # noqa: PLC0415
            load_task_contract,
        )
        from trusted_plan import (  # type: ignore[import-not-found,unused-ignore] # noqa: PLC0415
            _canonical,
            verify_plan_signature,
        )
    except ImportError:
        return "hold", {}

    from .trusted_contract_store import LOOKUP_FAILED, TrustedRecord

    spec_dir = Path(spec_dir)
    try:
        if trusted is LOOKUP_FAILED:
            return "hold", {}
        if isinstance(trusted, TrustedRecord):
            ok, _reason = verify_plan_signature(trusted.contract)
            if not ok or trusted.build_isolation not in _ISOLATED_STAMPS:
                return "hold", {}
            on_disk = json.loads(
                (spec_dir / "context" / "task_contract.json").read_text()
            )
            if _canonical(on_disk) != _canonical(trusted.contract):
                return "hold", {}
            return "verified", trusted.contract
        if has_trusted_trace(spec_dir):
            return "hold", {}
    except Exception:  # noqa: BLE001 - any doubt holds; only legacy keeps old behaviour
        return "hold", {}
    return "legacy", load_task_contract(spec_dir)
