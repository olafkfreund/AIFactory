"""The two direct ``claude --print`` spawns must scrub argv (AIFactory#1860).

A real executable stands in for the Claude CLI and records its argv, as
``test_insights_outbound_scrub.py`` does. Every value is synthetic.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

WEB_SERVER = Path(__file__).resolve().parents[1]
BACKEND = WEB_SERVER.parent / "backend"
for _root in (WEB_SERVER, BACKEND):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from server.services.clarification_service import (  # noqa: E402
    generate_clarification_questions,
)
from server.services.insights_service import (  # noqa: E402
    InsightsMessage,
    InsightsService,
    InsightsSession,
)

SSN = "123-45-6789"
EMAIL = "jane.doe@example.com"
PHONE = "555-123-4567"
PAN = "4111111111111111"
PII_VALUES = (SSN, EMAIL, PHONE, PAN)
PII_MARKERS = (
    "[REDACTED_SSN]",
    "[REDACTED_EMAIL]",
    "[REDACTED_PHONE]",
    "[REDACTED_CC]",
)
CONTEXT = ("services/billing/charge.py", "retry_policy=exponential", "exit code 2")
PROMPT = (
    f"Checkout failed for customer SSN {SSN}. Contact {EMAIL} or {PHONE}. "
    f"Card {PAN}. See services/billing/charge.py, retry_policy=exponential, "
    "exit code 2."
)

_CLI = """#!/usr/bin/env python3
import json, sys
json.dump(sys.argv[1:], open(sys.argv[0] + ".argv.json", "w"))
print(json.dumps({"title": "t", "description": "d", "skip": False,
                  "skipReason": "", "questions": []}))
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("LITELLM_AUDIT_SCRUB_OUTBOUND", raising=False)
    monkeypatch.setattr(
        "server.services.insights_providers.claude_provider."
        "ClaudeProvider._resolve_claude_token",
        lambda _self: (None, None, None),
    )


@pytest.fixture(autouse=True)
def project_class(monkeypatch):
    cls = {"value": "internal"}

    async def _classification(_path):
        return cls["value"]

    monkeypatch.setattr(
        "server.services.project_settings_store.data_classification", _classification
    )
    return cls


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    cli = tmp_path / "claude"
    cli.write_text(_CLI)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    argv_file = tmp_path / "claude.argv.json"

    def read() -> str | None:
        return (
            " ".join(json.loads(argv_file.read_text())) if argv_file.exists() else None
        )

    return read


async def _clarification(tmp_path: Path) -> Any:
    return await generate_clarification_questions(PROMPT, "see description", tmp_path)


async def _insights(tmp_path: Path) -> Any:
    service = InsightsService()
    session = InsightsSession(
        id="s",
        project_id="p",
        title="t",
        messages=[
            InsightsMessage(id="m", role="user", content=PROMPT, timestamp="now")
        ],
    )
    service.get_current_session = lambda *_a, **_k: session
    return await service.generate_task_from_chat(tmp_path, "p")


DRIVERS = pytest.mark.parametrize("driver", [_clarification, _insights])


@DRIVERS
async def test_regulated_is_scrubbed_even_with_kill_switch(
    driver, fake_cli, project_class, tmp_path, monkeypatch
):
    project_class["value"] = "regulated"
    monkeypatch.setenv("LITELLM_AUDIT_SCRUB_OUTBOUND", "false")
    await driver(tmp_path)
    argv = fake_cli()
    assert argv is not None
    assert not any(v in argv for v in PII_VALUES)
    assert all(m in argv for m in PII_MARKERS)
    assert all(c in argv for c in CONTEXT)


@DRIVERS
async def test_internal_with_kill_switch_is_raw(
    driver, fake_cli, tmp_path, monkeypatch
):
    monkeypatch.setenv("LITELLM_AUDIT_SCRUB_OUTBOUND", "false")
    await driver(tmp_path)
    argv = fake_cli()
    assert all(v in argv for v in PII_VALUES)
    assert not any(m in argv for m in PII_MARKERS)


@DRIVERS
async def test_internal_default_is_scrubbed(driver, fake_cli, tmp_path):
    await driver(tmp_path)
    argv = fake_cli()
    assert not any(v in argv for v in PII_VALUES)
    assert all(m in argv for m in PII_MARKERS)


def _boom(monkeypatch: pytest.MonkeyPatch) -> None:
    def raiser(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("scrub unavailable")

    for mod in ("clarification_service", "insights_service"):
        monkeypatch.setattr(f"server.services.{mod}.scrub_insights_outbound", raiser)


async def test_clarification_fails_closed(fake_cli, tmp_path, monkeypatch):
    _boom(monkeypatch)
    result = await _clarification(tmp_path)
    assert result["skip"] is True
    assert fake_cli() is None


async def test_insights_scrub_failure_propagates(fake_cli, tmp_path, monkeypatch):
    _boom(monkeypatch)
    with pytest.raises(RuntimeError):
        await _insights(tmp_path)
    assert fake_cli() is None
