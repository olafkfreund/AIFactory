"""Tests for the Antigravity CLI account routes (formerly Gemini).

Covers:
- canonical id + legacy ``gemini`` alias normalisation
- the ``antigravity`` CLI_CONFIG entry (npm package, install dir, binary)
- install/update command construction (npm install into the antigravity
  install dir + symlink) — mocked subprocess, no real network
- version detection for the bundled install layout

All subprocess / filesystem interactions are mocked.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the server package is importable when tests run from repo root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from fastapi import HTTPException  # noqa: E402
from server.routes import cli_accounts as ca  # noqa: E402

# ---------------------------------------------------------------------------
# Config / alias normalisation
# ---------------------------------------------------------------------------


def test_canonical_cli_maps_gemini_to_antigravity():
    assert ca._canonical_cli("gemini") == "antigravity"
    assert ca._canonical_cli("antigravity") == "antigravity"
    assert ca._canonical_cli("codex") == "codex"


def test_validate_cli_accepts_gemini_alias():
    assert ca._validate_cli("gemini") == "antigravity"
    assert ca._validate_cli("antigravity") == "antigravity"
    assert ca._validate_cli("codex") == "codex"


def test_validate_cli_rejects_unknown():
    with pytest.raises(HTTPException) as exc:
        ca._validate_cli("bogus")
    assert exc.value.status_code == 400


def test_supported_clis_canonical():
    assert ca.SUPPORTED_CLIS == {"codex", "antigravity"}
    assert "gemini" not in ca.SUPPORTED_CLIS


def test_antigravity_config_uses_google_npm_package_and_install_dir():
    cfg = ca.CLI_CONFIG["antigravity"]
    assert cfg["npm_package"] == "@google/gemini-cli"
    assert cfg["binary"] == "antigravity"
    assert cfg["install_dir"] == ca.ANTIGRAVITY_INSTALL_DIR
    # Install dir mirrors scripts/install-backend.js.
    assert str(cfg["install_dir"]).endswith(".gemini/antigravity-cli")
    # Stored credential filename kept for back-compat.
    assert str(cfg["stored_credentials"]).endswith("gemini-credentials.json")


def test_get_antigravity_binary_alias_exists():
    # Legacy helper name still importable (insights provider uses it).
    assert ca.get_gemini_binary is ca.get_antigravity_binary


# ---------------------------------------------------------------------------
# Install / update command construction
# ---------------------------------------------------------------------------


def test_npm_install_antigravity_uses_prefixed_install_dir_and_symlink():
    """Antigravity install scopes npm to the install dir and creates symlink."""
    captured = {}

    def fake_run(args, timeout=60):
        captured["args"] = args
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with (
        patch.object(ca, "_run_login_shell", side_effect=fake_run),
        patch.object(ca, "_create_antigravity_symlink") as mock_symlink,
        patch("pathlib.Path.mkdir"),
    ):
        result = ca._npm_install_cli("antigravity", "@google/gemini-cli")

    assert result.returncode == 0
    args = captured["args"]
    # npm install -g --prefix <install_dir> @google/gemini-cli
    assert args[:4] == ["npm", "install", "-g", "--prefix"]
    assert args[4] == str(ca.ANTIGRAVITY_INSTALL_DIR)
    assert args[5] == "@google/gemini-cli"
    # Symlink (antigravity -> gemini) created after a successful install.
    mock_symlink.assert_called_once()


def test_npm_install_antigravity_skips_symlink_on_failure():
    def fake_run(args, timeout=60):
        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = "boom"
        return result

    with (
        patch.object(ca, "_run_login_shell", side_effect=fake_run),
        patch.object(ca, "_create_antigravity_symlink") as mock_symlink,
        patch("pathlib.Path.mkdir"),
    ):
        result = ca._npm_install_cli("antigravity", "@google/gemini-cli")

    assert result.returncode == 1
    mock_symlink.assert_not_called()


def test_npm_install_codex_uses_plain_global_install():
    """Non-Antigravity CLIs install globally without a prefix or symlink."""
    captured = {}

    def fake_run(args, timeout=60):
        captured["args"] = args
        result = MagicMock()
        result.returncode = 0
        return result

    with patch.object(ca, "_run_login_shell", side_effect=fake_run):
        ca._npm_install_cli("codex", "@openai/codex")

    assert captured["args"] == ["npm", "install", "-g", "@openai/codex"]


def test_install_endpoint_reports_update_when_already_installed():
    """install_or_update_cli reports wasUpdate=True + 'updated' on re-install."""
    node_ok = MagicMock()
    node_ok.returncode = 0

    install_ok = MagicMock()
    install_ok.returncode = 0
    install_ok.stdout = ""
    install_ok.stderr = ""

    # First version detect = already installed; second = post-update version.
    versions = iter(["1.0.0", "1.2.0"])

    with (
        patch.object(ca, "_detect_cli_version", side_effect=lambda c: next(versions)),
        patch("subprocess.run", return_value=node_ok),
        patch.object(ca, "_npm_install_cli", return_value=install_ok),
    ):
        # Pass the legacy "gemini" id to exercise normalisation through the endpoint.
        result = ca.install_or_update_cli("gemini")

    assert result["success"] is True
    assert result["wasUpdate"] is True
    assert result["version"] == "1.2.0"
    assert "updated" in result["message"].lower()


def test_install_endpoint_reports_fresh_install():
    node_ok = MagicMock()
    node_ok.returncode = 0
    install_ok = MagicMock()
    install_ok.returncode = 0
    install_ok.stdout = ""
    install_ok.stderr = ""

    # First detect = None (not installed); second = new version.
    versions = iter([None, "1.2.0"])

    with (
        patch.object(ca, "_detect_cli_version", side_effect=lambda c: next(versions)),
        patch("subprocess.run", return_value=node_ok),
        patch.object(ca, "_npm_install_cli", return_value=install_ok),
    ):
        result = ca.install_or_update_cli("antigravity")

    assert result["success"] is True
    assert result["wasUpdate"] is False
    assert "installed" in result["message"].lower()


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


def test_detect_cli_version_reads_npm_package_json_for_antigravity():
    """Version detection prefers the bundled package.json (avoids slow startup)."""
    with (
        patch.object(ca, "get_antigravity_binary", return_value="antigravity"),
        patch(
            "shutil.which",
            return_value="/home/u/.gemini/antigravity-cli/bin/antigravity",
        ),
        patch.object(ca, "_read_npm_package_version", return_value="3.4.5"),
    ):
        version = ca._detect_cli_version("antigravity")
    assert version == "3.4.5"


def test_detect_cli_version_normalises_gemini_alias():
    """Passing the legacy 'gemini' id resolves the antigravity config."""
    with (
        patch.object(ca, "get_antigravity_binary", return_value="antigravity"),
        patch("shutil.which", return_value="/usr/bin/antigravity"),
        patch.object(ca, "_read_npm_package_version", return_value="9.9.9"),
    ):
        version = ca._detect_cli_version("gemini")
    assert version == "9.9.9"
