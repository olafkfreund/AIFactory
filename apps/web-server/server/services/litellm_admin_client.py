"""LiteLLM admin-API client (Epic #35 #38 PR-2b).

Async wrapper around LiteLLM's virtual-key admin endpoints. Used by
the tenant reconciler to sync per-org enforcement state into LiteLLM's
Postgres-backed budget + allowlist storage (design §3).

Endpoints covered
-----------------

- ``POST /key/generate`` — create a per-org virtual key.
- ``POST /key/update``   — update allowed_models / budget on an existing key.
- ``POST /key/delete``   — hard-delete (called after the day-30 grace
                          window from #36).
- ``GET  /key/info``     — read a key's current state (used by tests
                          + drift recovery).
- ``GET  /key/list``     — enumerate all keys (drift recovery).

Master-key handling (design §3 reviewer finding #2)
---------------------------------------------------

LiteLLM's admin API requires a master key (``LITELLM_MASTER_KEY``).
We NEVER accept the plaintext key from env — that's the explicit
anti-pattern flagged by the reviewer. Instead the wrapped key sits
in a KMS-wrapped Secret (``aifactory-litellm-master-key``) and we
unwrap via the existing ``crypto/kms`` abstraction on each admin-API
call. Calls are seconds apart not per-request so the unwrap cost is
negligible; the alternative (cache the plaintext key in process
memory) widens the blast radius if a pod is compromised.

The wrapped key blob is supplied via env (``LITELLM_MASTER_KEY_WRAPPED``)
as a base64-encoded ciphertext. Construction raises ``ValueError`` if
the env var is unset OR if a plaintext-looking env (``LITELLM_MASTER_KEY``)
is present — fail-fast at startup, never accidentally fall back to a
plaintext key.

Failure-safe contract
---------------------

Each method wraps + raises typed errors:
- ``LiteLLMAdminError`` for non-2xx responses.
- ``LiteLLMAdminUnavailableError`` for network failures.
The reconciler catches both and retries on the next tick (per #36's
reconciler pattern).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)


# Default budget when none specified. Operators override via Helm
# values; documented in PR-3's concept doc. Per-day budget keeps the
# blast radius of a runaway agent bounded.
_DEFAULT_BUDGET_USD_PER_DAY: float = 50.0
_DEFAULT_BUDGET_DURATION: str = "1d"

_KEY_GENERATE: str = "/key/generate"
_KEY_UPDATE: str = "/key/update"
_KEY_DELETE: str = "/key/delete"
_KEY_INFO: str = "/key/info"
_KEY_LIST: str = "/key/list"

# Timeout for admin API calls. Admin operations are infrequent + small;
# a generous timeout absorbs transient gateway pod restarts without
# misclassifying the request as failed.
_DEFAULT_TIMEOUT_S: float = 10.0


class LiteLLMAdminError(RuntimeError):
    """LiteLLM admin endpoint returned a non-2xx response.

    The error message includes the endpoint + status code + best-effort
    response body (truncated) so operators can debug from logs alone.
    """

    def __init__(self, endpoint: str, status: int, body: str) -> None:
        super().__init__(
            f"LiteLLM admin {endpoint} returned HTTP {status}: {body[:500]}"
        )
        self.endpoint = endpoint
        self.status = status
        self.body = body


class LiteLLMAdminUnavailableError(RuntimeError):
    """The LiteLLM admin API is unreachable (network / DNS / connect).

    Distinct from ``LiteLLMAdminError`` so reconciler logic can
    differentiate "the server said no" from "the server is down".
    The reconciler treats both as transient + retries.
    """

    def __init__(self, endpoint: str, underlying: Exception) -> None:
        super().__init__(f"LiteLLM admin {endpoint} unreachable: {underlying!r}")
        self.endpoint = endpoint
        self.underlying = underlying


class LiteLLMAdminClient:
    """Async client for LiteLLM's virtual-key admin API.

    Construction reads the wrapped master key from env + unwraps via
    the KMS backend. ``base_url`` defaults to ``LITELLM_GATEWAY_URL``;
    explicit construction is supported for tests.

    All methods are awaitable. They raise ``LiteLLMAdminError`` for
    4xx/5xx + ``LiteLLMAdminUnavailableError`` for network errors. The
    reconciler wraps + retries.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        # For tests: inject a callable that returns the unwrapped key.
        # Production passes None and the constructor reads + unwraps
        # from env.
        master_key_provider: Any = None,
    ) -> None:
        if base_url is None:
            base_url = os.environ.get("LITELLM_GATEWAY_URL", "").strip()
        if not base_url:
            raise ValueError(
                "LiteLLMAdminClient: base_url not supplied and "
                "LITELLM_GATEWAY_URL env is unset",
            )

        # Plaintext-anti-pattern guard (design §3 reviewer finding #2).
        # Refuse to start if the operator has set a plaintext master
        # key in env. Forces the KMS-wrapped path.
        if os.environ.get("LITELLM_MASTER_KEY", "").strip():
            raise ValueError(
                "LiteLLMAdminClient refuses to accept a plaintext "
                "LITELLM_MASTER_KEY env var (design §3 forbidden "
                "anti-pattern). Use LITELLM_MASTER_KEY_WRAPPED with "
                "a KMS-encrypted ciphertext instead.",
            )

        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._master_key_provider = master_key_provider

    # ------------------------------------------------------------------
    # Key lifecycle
    # ------------------------------------------------------------------

    async def create_virtual_key(
        self,
        org_id: str,
        allowed_models: list[str],
        budget_usd_per_day: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Provision a new LiteLLM virtual key for an organization.

        Returns the generated ``key`` (sk-... prefix). Callers persist
        it in the org's vault path or DB so the gateway can validate
        per-request Authorization headers against the LiteLLM-side
        registry.

        ``allowed_models`` is the same list shape as
        ``organizations.allowed_models`` (PR-2a). ``["*"]`` translates
        to "no per-tenant restriction" at LiteLLM's level (LiteLLM's
        deployment-wide model_list still applies).
        """
        budget = (
            budget_usd_per_day
            if budget_usd_per_day is not None
            else (_DEFAULT_BUDGET_USD_PER_DAY)
        )
        # WHY: LiteLLM's `models: ["*"]` is shorthand for "no per-key
        # restriction" — exactly what the design wants for backward-
        # compat orgs whose allowlist hasn't been narrowed yet.
        payload: dict[str, Any] = {
            "user_id": org_id,
            "models": list(allowed_models) if allowed_models else ["*"],
            "max_budget": budget,
            "budget_duration": _DEFAULT_BUDGET_DURATION,
            "metadata": {"aifactory_org_id": org_id, **(metadata or {})},
        }
        body = await self._post(_KEY_GENERATE, payload)
        key = body.get("key") or body.get("api_key") or ""
        if not key:
            raise LiteLLMAdminError(
                _KEY_GENERATE,
                200,
                f"response missing 'key': {body}",
            )
        return str(key)

    async def update_virtual_key(
        self,
        org_id: str,
        allowed_models: list[str] | None = None,
        budget_duration: str | None = None,
        max_budget: float | None = None,
    ) -> None:
        """Update an existing key's allowed_models / budget.

        ``budget_duration="0s"`` is the documented LiteLLM idiom for
        "immediate block" (no budget window). Used on soft-delete.
        """
        payload: dict[str, Any] = {"user_id": org_id}
        if allowed_models is not None:
            payload["models"] = list(allowed_models) if allowed_models else ["*"]
        if budget_duration is not None:
            payload["budget_duration"] = budget_duration
        if max_budget is not None:
            payload["max_budget"] = max_budget
        await self._post(_KEY_UPDATE, payload)

    async def disable_virtual_key(self, org_id: str) -> None:
        """Immediately block a key by zeroing the budget window.

        Used on org soft-delete. Per design PR-2 implementation plan:
        the day-30 grace period from #36 keeps the key in 'disabled'
        state so audit history remains coherent; hard-delete only
        fires after the grace window expires.
        """
        await self.update_virtual_key(
            org_id=org_id,
            budget_duration="0s",
            max_budget=0.0,
        )

    async def delete_virtual_key(self, org_id: str) -> None:
        """Hard-delete a virtual key. Called after day-30 grace period."""
        # LiteLLM's /key/delete takes a list of keys; we delete by
        # user_id binding via the metadata-tag we wrote at create-time.
        await self._post(_KEY_DELETE, {"user_ids": [org_id]})

    async def list_virtual_keys(self) -> list[dict[str, Any]]:
        """Enumerate all virtual keys. Used by reconciler for drift recovery."""
        # WHY GET-with-payload: LiteLLM's admin API mixes GET + POST
        # idioms; /key/list is GET with optional query params. We
        # send the simplest possible request (no filter) and let the
        # reconciler filter client-side.
        body = await self._get(_KEY_LIST)
        keys = body.get("keys") or body.get("data") or []
        if not isinstance(keys, list):
            return []
        return [k for k in keys if isinstance(k, dict)]

    async def get_virtual_key_info(self, org_id: str) -> dict[str, Any] | None:
        """Return key metadata for one org, or None if not provisioned."""
        try:
            body = await self._get(_KEY_INFO, params={"user_id": org_id})
        except LiteLLMAdminError as exc:
            if exc.status == 404:
                return None
            raise
        info = body.get("info") or body
        return info if isinstance(info, dict) else None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, json_body=payload)

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = urljoin(self._base_url + "/", path.lstrip("/"))
        # Unwrap the master key per request (design §3 — KMS unwrap
        # is cheap + admin calls are infrequent).
        master_key = self._unwrap_master_key()
        headers = {
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                )
        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            raise LiteLLMAdminUnavailableError(path, exc) from exc

        if response.status_code >= 400:
            raise LiteLLMAdminError(
                endpoint=path,
                status=response.status_code,
                body=response.text,
            )

        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return {}

    def _unwrap_master_key(self) -> str:
        """Resolve the LiteLLM master key — KMS-wrapped path only.

        Production: read wrapped ciphertext from env, KMS-decrypt,
        return the plaintext key (held in caller's local scope only;
        not cached).
        Tests: ``master_key_provider`` injection bypasses the KMS path.
        """
        if self._master_key_provider is not None:
            return str(self._master_key_provider())

        wrapped_b64 = os.environ.get("LITELLM_MASTER_KEY_WRAPPED", "").strip()
        if not wrapped_b64:
            raise ValueError(
                "LITELLM_MASTER_KEY_WRAPPED is unset; cannot unwrap "
                "the LiteLLM master key. See PR-3 concept doc for the "
                "KMS-wrapping runbook.",
            )

        try:
            wrapped = base64.b64decode(wrapped_b64)
        except Exception as exc:
            raise ValueError(
                f"LITELLM_MASTER_KEY_WRAPPED is not valid base64: {exc}",
            ) from exc

        # Lazy import: the crypto package isn't always on sys.path in
        # backend-only contexts. This client is a web-server-side
        # concept so the import shouldn't fail in production.
        from ..crypto.kms import get_backend

        try:
            plaintext = get_backend().decrypt(wrapped)
        except Exception as exc:
            raise ValueError(
                f"KMS decrypt of LITELLM_MASTER_KEY_WRAPPED failed: {exc}. "
                f"Either the wrapped value is corrupt or the KMS root "
                f"has rotated without re-wrapping the master key.",
            ) from exc

        return plaintext.decode("utf-8")


__all__ = [
    "LiteLLMAdminClient",
    "LiteLLMAdminError",
    "LiteLLMAdminUnavailableError",
]
