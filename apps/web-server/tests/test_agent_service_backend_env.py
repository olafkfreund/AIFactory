"""Backend .env settings cannot reintroduce denied child credentials."""

from pathlib import Path

import pytest
from server.services.agent_service import _load_backend_env


def test_load_backend_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".env"
    path.write_text("JWT_SECRET=x\nGRAPHITI_ENABLED=true\nEXISTING=new\n")
    env = {"EXISTING": "original"}

    _load_backend_env(path, env)

    assert env == {"GRAPHITI_ENABLED": "true", "EXISTING": "original"}
