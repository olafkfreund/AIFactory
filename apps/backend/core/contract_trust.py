"""Act on a task contract only if the web server verified it (#1673).

The server passes its verdict to ``run.py`` as ``AIFACTORY_TRUSTED_CONTRACT``:
the sha256 of the verified contract, ``hold``, or ``legacy``; unset means no
server. The pod gets a digest, never the signing key.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import Any

ENV = "AIFACTORY_TRUSTED_CONTRACT"
LEGACY = "legacy"
HOLD_KEY = "contractHold"


# Logs show only these constants: CodeQL's name heuristic treats anything read
# next to the verdict env var as a secret (py/clear-text-logging-sensitive-data).
_REASON_LABELS = {
    r: r
    for r in (
        "no contract",
        "digest mismatch",
        "held by server",
        "server saw no signed contract",
        "server verified",
        "trusted trace without a server verdict",
        "no server verdict",
    )
}


def reason_label(why: str) -> str:
    """A constant label for a ``trusted_contract`` reason; ``error`` otherwise."""
    return _REASON_LABELS.get(why, "error")


def contract_digest(obj: Any) -> str:
    """Hex sha256 of the canonical contract; server and pod share this."""
    from trusted_plan import _canonical  # noqa: PLC0415

    return hashlib.sha256(_canonical(obj).encode()).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def record_hold(spec_dir: Path, note: str | None) -> None:
    """Put a hold note on the task's plan for the task status (#1709); None clears it.

    Never creates a plan, never raises: the note is display-only.
    """
    plan_file = Path(spec_dir) / "implementation_plan.json"
    plan = _read_json(plan_file)
    if not isinstance(plan, dict) or plan.get(HOLD_KEY) == note:
        return
    if note is None:
        plan.pop(HOLD_KEY)
    else:
        plan[HOLD_KEY] = note
    with contextlib.suppress(OSError):
        plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")


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


def trusted_contract(spec_dir: Path) -> tuple[str, dict[str, Any] | None, str]:  # noqa: PLR0911 - one return per truth-table row
    """Return ``(state, contract, reason)``; state is verified, hold or legacy.

    Reads the file once and hashes that same object. On hold the contract is
    returned only so a caller can see what it claims; never act on it.
    """
    try:
        from core.migration_mapper import load_contract  # noqa: PLC0415

        contract = load_contract(spec_dir)
        present = ENV in os.environ
        value = os.environ.get(ENV)
        if contract is None:
            if not present or value in ("hold", LEGACY):
                return "legacy", None, "no contract"
            return "hold", None, "digest mismatch"
        if value == "hold":
            return "hold", contract, "held by server"
        if value == LEGACY:
            return "hold", contract, "server saw no signed contract"
        if present:
            if value == contract_digest(contract):
                return "verified", contract, "server verified"
            return "hold", contract, "digest mismatch"
        if has_trusted_trace(spec_dir):
            return "hold", contract, "trusted trace without a server verdict"
        return "legacy", contract, "no server verdict"
    except Exception as exc:  # noqa: BLE001 - any doubt holds
        return "hold", None, f"error: {type(exc).__name__}"
