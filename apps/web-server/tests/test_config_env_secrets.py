"""#1931: APP_REQUIRE_ENV_SECRETS refuses to generate secret files."""

import pytest
from server.config import Settings


def test_switch_raises_when_secret_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_REQUIRE_ENV_SECRETS", "true")
    monkeypatch.setenv("APP_API_TOKEN", "t")
    monkeypatch.delenv("APP_JWT_SECRET", raising=False)
    monkeypatch.setenv("APP_PROJECTS_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="APP_JWT_SECRET"):
        Settings(_env_file=None)


def test_switch_passes_with_env_and_defaults_off(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_PROJECTS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_API_TOKEN", "t")
    monkeypatch.setenv("APP_JWT_SECRET", "j")
    monkeypatch.setenv("APP_REQUIRE_ENV_SECRETS", "true")
    assert Settings(_env_file=None).JWT_SECRET  # taken from env, not generated
    monkeypatch.delenv("APP_REQUIRE_ENV_SECRETS")
    assert Settings(_env_file=None).REQUIRE_ENV_SECRETS is False
