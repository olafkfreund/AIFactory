"""Tests for the GitHub App operator setup script."""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "github_app_setup.py"
_spec = importlib.util.spec_from_file_location("github_app_setup", _SCRIPT)
assert _spec and _spec.loader
setup = importlib.util.module_from_spec(_spec)
sys.modules["github_app_setup"] = setup
_spec.loader.exec_module(setup)


def test_manifest_permissions() -> None:
    manifest = setup.load_manifest(False, None)
    assert manifest["default_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "metadata": "read",
    }
    assert "administration" not in manifest["default_permissions"]
    assert setup.load_manifest(True, None)["default_permissions"] == {
        **manifest["default_permissions"],
        "workflows": "write",
    }


def test_form_html() -> None:
    manifest = setup.load_manifest(False, None)
    escaped = json.dumps(manifest).replace('"', "&quot;")
    for org, url in (
        (None, "https://github.com/settings/apps/new"),
        ("my-org", "https://github.com/organizations/my-org/settings/apps/new"),
    ):
        page = setup.form_html(manifest, org)
        assert f'<form method="post" action="{url}">' in page
        assert f'name="manifest" value="{escaped}"' in page
    with pytest.raises(ValueError):
        setup.form_html(manifest, "a/b")


def test_secret_yaml() -> None:
    pem = "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----\n"
    secret = yaml.safe_load(setup.secret_yaml(pem))
    assert secret["metadata"]["name"] == "aifactory-github-app"
    assert secret["stringData"]["private-key"] == pem


def test_write_private(tmp_path: Path) -> None:
    output = tmp_path / "secret.yaml"
    setup.write_private(output, "secret")
    assert output.read_text() == "secret"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        setup.write_private(output, "replacement")
    assert output.read_text() == "secret"


def test_copilot_listed() -> None:
    listed = {"requested_reviewers": [{"login": "Copilot"}]}
    assert setup.copilot_listed(201, listed)
    assert not setup.copilot_listed(201, {"requested_reviewers": []})
    assert not setup.copilot_listed(422, listed)


def test_invalid_conversion_code_does_not_call_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid code reached the network")

    monkeypatch.setattr(setup.urllib.request, "urlopen", fail_network)
    output = tmp_path / "secret.yaml"
    assert setup.main(["convert", "bad code!", "--out", str(output)]) != 0
    assert not output.exists()
