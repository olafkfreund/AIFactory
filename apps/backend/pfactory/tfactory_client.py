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
    "wants_auto_handoff",
    "maybe_auto_handoff_tfactory",
]

# (url, json_payload, headers) -> {"status": int, "ok": bool, "body": str}
Poster = Callable[[str, dict, dict], Awaitable[dict]]

# TFactory's self-contained intake: create a test task from a raw spec, no
# shared workspace/branch required (POST {project_id, spec_id, spec_text}).
_DEFAULT_PATH = "/api/specs/ingest"


def tfactory_config(env: dict | None = None) -> dict:
    """Read TFactory transport config from the environment."""
    env = env if env is not None else os.environ
    return {
        "base_url": (env.get("TFACTORY_BASE_URL") or "").rstrip("/"),
        # TFactory's auth middleware requires a bearer token on /api/*. Prefer a
        # dedicated TFACTORY_TOKEN; fall back to the shared APP_API_TOKEN that
        # every factory pod carries (factory-secrets) — without it the handoff
        # 401s (#517).
        "token": env.get("TFACTORY_TOKEN") or env.get("APP_API_TOKEN") or "",
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


def _aifactory_project_name(spec_dir: Path) -> str:
    """Derive the project name from the workspace layout
    (.../workspaces/<project>/.aifactory/specs/<spec_id>)."""
    parts = Path(spec_dir).resolve().parts
    if "workspaces" in parts:
        i = parts.index("workspaces")
        if i + 1 < len(parts):
            return parts[i + 1]
    try:
        return Path(spec_dir).parents[2].name
    except IndexError:
        return ""


def build_ingest_payload(spec_dir: Path, spec_id: str) -> dict:
    """Build the payload for TFactory's self-contained spec intake
    (``POST /api/specs/ingest``): ``{project_id, spec_id, spec_text}``.

    ``project_id`` is the TFactory project to ingest into — resolved by name
    from the AIFactory workspace (TFactory carries a matching project),
    overridable via ``TFACTORY_PROJECT_ID``. ``spec_text`` is the finished
    spec.md (falls back to the requirements description). #517.
    """
    spec_dir = Path(spec_dir)
    project_id = (
        os.environ.get("TFACTORY_PROJECT_ID") or _aifactory_project_name(spec_dir)
    )
    spec_text = ""
    spec_md = spec_dir / "spec.md"
    if spec_md.exists():
        spec_text = spec_md.read_text()
    if not spec_text.strip():
        try:
            req = json.loads((spec_dir / "requirements.json").read_text())
            spec_text = req.get("description") or req.get("title") or ""
        except (OSError, ValueError):
            pass
    return {
        "project_id": project_id,
        "spec_id": spec_id,
        "spec_text": spec_text,
        "format": "markdown",
    }


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


def wants_auto_handoff(spec_dir: Path) -> bool:
    """True if the task opted into auto-handover to TFactory on completion.

    Read from ``task_metadata.json`` (``auto_handover_tfactory``). Best-effort:
    a missing/unreadable file means "no".
    """
    try:
        meta = json.loads((Path(spec_dir) / "task_metadata.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(meta, dict)
        and (meta.get("auto_handover_tfactory") or meta.get("autoHandoverTFactory"))
    )


async def maybe_auto_handoff_tfactory(spec_dir: Path, spec_id: str) -> dict:
    """On a task's terminal SUCCESS, hand the finished build to TFactory for
    testing — but only when the task opted in (``auto_handover_tfactory`` in
    task_metadata) AND TFactory is configured (``TFACTORY_BASE_URL``). The
    handoff carries the spec's requirements + PFactory/Task-Contract meta + the
    mutation ledger as evidence. Best-effort: never raises, never blocks task
    completion (#496).
    """
    spec_dir = Path(spec_dir)
    if not wants_auto_handoff(spec_dir):
        return {"sent": False, "reason": "not_requested"}
    try:
        payload = build_ingest_payload(spec_dir, spec_id)
        result = await send_handoff(payload)
    except Exception as exc:  # noqa: BLE001 — must never break task completion
        return {"sent": False, "reason": "error", "error": str(exc)[:300]}
    # Record a local marker so the UI / operator can see the handoff outcome.
    try:
        (spec_dir / "tfactory_handoff.json").write_text(json.dumps(result, indent=2))
    except OSError:
        pass
    return result
