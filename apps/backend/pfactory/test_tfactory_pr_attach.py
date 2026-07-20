"""#964: send_pr_attach POSTs the opened PR to TFactory so the verify verdict
posts back (the verifying handoff was sent before the PR existed)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pfactory.tfactory_client as tc
import pytest


@pytest.mark.asyncio
async def test_pr_attach_posts_to_the_spec_pr_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TFACTORY_PROJECT_ID", "demo")
    monkeypatch.setattr(
        tc, "tfactory_config", lambda: {"base_url": "http://tf", "token": "tok"}
    )
    seen: dict[str, Any] = {}

    async def fake_poster(
        url: str, payload: dict[str, Any], headers: dict[str, Any]
    ) -> dict[str, Any]:
        seen.update(url=url, payload=payload, headers=headers)
        return {"ok": True, "status": 200}

    out = await tc.send_pr_attach(
        Path("spec"),
        "048-feat",
        383,
        "olafkfreund/aifactory-demo",
        poster=fake_poster,
    )
    assert out["sent"] is True
    assert seen["url"] == "http://tf/api/specs/demo/048-feat/pr"
    assert seen["payload"] == {
        "pr_number": 383,
        "repo_slug": "olafkfreund/aifactory-demo",
    }
    assert seen["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_pr_attach_not_configured_without_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tc, "tfactory_config", lambda: {"base_url": "", "token": ""})
    out = await tc.send_pr_attach(Path("spec"), "048-feat", 1, None)
    assert out == {"sent": False, "reason": "not_configured"}
