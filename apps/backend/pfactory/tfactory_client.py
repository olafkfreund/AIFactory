"""Outbound TFactory transport (epic #327, #337).

When a governed PFactory child routes to TFactory (``handoff:tfactory`` /
``type:testing``), AIFactory POSTs the spec + its ``pfactory:meta`` to
TFactory's HTTP API for test generation. Symmetric with the inbound correction
receiver (``qa/correction.py``, #317).

Configuration (environment):

* ``TFACTORY_BASE_URL``    — e.g. ``https://tfactory.internal`` (required to send)
* ``TFACTORY_TOKEN``       — bearer token (optional)
* ``TFACTORY_HANDOFF_PATH``— endpoint path, default ``/api/handoff``

Graceful by design: when ``TFACTORY_BASE_URL`` is unset, :func:`send_handoff`
returns ``{"sent": False, "reason": "not_configured"}`` and never raises — the
caller still records the local handoff marker. The HTTP poster is injectable so
tests need no network.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

__all__ = [
    "tfactory_config",
    "build_handoff_payload",
    "send_handoff",
    "load_tfactory_block",
]

# (url, json_payload, headers) -> {"status": int, "ok": bool, "body": str}
Poster = Callable[[str, dict, dict], Awaitable[dict]]

_DEFAULT_PATH = "/api/handoff"


def tfactory_config(env: dict | None = None) -> dict:
    """Read TFactory transport config from the environment."""
    env = env if env is not None else os.environ
    return {
        "base_url": (env.get("TFACTORY_BASE_URL") or "").rstrip("/"),
        "token": env.get("TFACTORY_TOKEN") or "",
        "path": env.get("TFACTORY_HANDOFF_PATH") or _DEFAULT_PATH,
    }


def load_tfactory_block(spec_dir: Path) -> dict:
    """Read the Task Contract v2 ``tfactory`` block from implementation_plan.json.

    PFactory computes this block (lanes/frameworks/endpoints/coverage/mutation/
    security/ac_to_code_map) and it is installed verbatim by trusted_plan ingest.
    Returns ``{}`` when absent (v1 plans) or unreadable — TFactory then falls
    back to its own inference.
    """
    plan_file = Path(spec_dir) / "implementation_plan.json"
    if not plan_file.exists():
        return {}
    try:
        plan = json.loads(plan_file.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    block = plan.get("tfactory")
    return block if isinstance(block, dict) else {}


def build_handoff_payload(
    spec_id: str,
    requirements: dict | None,
    classification: Any,
    metadata: dict | None,
    tfactory: dict | None = None,
    spec_dir: Path | None = None,
) -> dict:
    """Build the JSON payload AIFactory sends to TFactory for a handoff.

    When the spec carries a Task Contract v2 ``tfactory`` block (RFC-0002), it is
    included so TFactory plans tests from declared lanes/frameworks/endpoints/
    scope instead of inferring them. Omitted (empty) for v1 specs.

    When ``spec_dir`` is given and a mutation ledger was recorded this run (#476,
    ``AIFACTORY_MUTATION_LEDGER``), the ledger rides along as handover evidence so
    TFactory sees exactly what the coder changed. Additive/best-effort.
    """
    requirements = requirements or {}
    gh = requirements.get("githubIssue") if isinstance(requirements, dict) else None
    labels = gh.get("labels", []) if isinstance(gh, dict) else []
    payload = {
        "source": "aifactory",
        "taxonomy": "v1",
        "spec_id": spec_id,
        "title": requirements.get("title"),
        "description": requirements.get("description"),
        "labels": labels,
        "handoff": getattr(classification, "handoff", None),
        "types": list(getattr(classification, "types", ()) or ()),
        "priority": getattr(classification, "priority", None),
        "pfactory_meta": metadata or {},
        "tfactory": tfactory or {},
    }
    if spec_dir is not None:
        try:
            from agents.mutation_ledger import MutationLedger

            mutations = MutationLedger(spec_dir).read()
            if mutations:
                payload["mutations"] = mutations
        except Exception:  # noqa: BLE001 — evidence is best-effort
            pass
    return payload


async def _httpx_poster(url: str, payload: dict, headers: dict) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        return {
            "status": resp.status_code,
            "ok": resp.is_success,
            "body": resp.text[:2000],
        }


async def send_handoff(
    payload: dict,
    *,
    config: dict | None = None,
    poster: Poster | None = None,
) -> dict:
    """POST a handoff payload to TFactory. Never raises.

    Returns a JSON-able result: ``{"sent": bool, "reason": str|None, ...}``.
    ``reason`` is ``"not_configured"`` (no base URL), ``"http_error"`` (non-2xx),
    or ``"error"`` (transport exception).
    """
    config = config or tfactory_config()
    base_url = config.get("base_url")
    if not base_url:
        return {"sent": False, "reason": "not_configured"}

    url = base_url + (config.get("path") or _DEFAULT_PATH)
    headers = {"Content-Type": "application/json"}
    if config.get("token"):
        headers["Authorization"] = f"Bearer {config['token']}"

    poster = poster or _httpx_poster
    try:
        result = await poster(url, payload, headers)
    except Exception as exc:  # noqa: BLE001 — transport must never crash routing
        return {"sent": False, "reason": "error", "error": str(exc)[:300]}

    ok = bool(result.get("ok"))
    return {
        "sent": ok,
        "reason": None if ok else "http_error",
        "status": result.get("status"),
        "url": url,
    }
