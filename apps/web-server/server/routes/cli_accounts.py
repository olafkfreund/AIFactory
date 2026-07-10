"""
CLI Account management routes for Codex CLI (OpenAI) and Antigravity CLI
(Google — the post-Gemini-CLI successor).

Provides detection, credential import, API key storage, terminal-based
login polling, and CLI install/update for third-party CLI tools.

Back-compat: the canonical CLI id is ``antigravity``, but the legacy
``gemini`` id is still accepted on every endpoint (normalised via
``_canonical_cli``) and the ``/cli-accounts/detect`` response still includes a
``gemini`` key mirroring ``antigravity`` so older frontends keep working.
"""

import asyncio
import base64
import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical CLI ids.  "antigravity" is the Google CLI (formerly "gemini").
SUPPORTED_CLIS = {"codex", "antigravity"}

# Legacy id -> canonical id.  Old clients / stored values that say "gemini"
# are transparently routed to the "antigravity" config.
_CLI_ALIASES = {"gemini": "antigravity"}

# Antigravity is the npm package @google/gemini-cli installed into
# ~/.gemini/antigravity-cli (matches scripts/install-backend.js). The bundled
# binary is ~/.gemini/antigravity-cli/bin/antigravity (symlink -> gemini).
ANTIGRAVITY_INSTALL_DIR = Path.home() / ".gemini" / "antigravity-cli"

CREDENTIALS_DIR = Path.home() / ".aifactory"

CLI_CONFIG = {
    "codex": {
        "binary": "codex",
        "version_cmd": "codex --version",
        "credentials_file": Path.home() / ".codex" / "auth.json",
        "config_file": Path.home() / ".codex" / "config.toml",
        "stored_credentials": CREDENTIALS_DIR / "codex-credentials.json",
        "npm_package": "@openai/codex",
    },
    "antigravity": {
        "binary": "antigravity",
        "version_cmd": "antigravity --version",
        # Credentials/config still live under ~/.gemini (the Antigravity CLI
        # keeps the legacy gemini-cli config directory).
        "credentials_file": Path.home() / ".gemini" / "settings.json",
        "oauth_credentials_file": Path.home() / ".gemini" / "oauth_creds.json",
        # Stored-credential filename kept as gemini-credentials.json for
        # back-compat with existing installs.
        "stored_credentials": CREDENTIALS_DIR / "gemini-credentials.json",
        "npm_package": "@google/gemini-cli",
        "install_dir": ANTIGRAVITY_INSTALL_DIR,
    },
}


def _canonical_cli(cli: str) -> str:
    """Normalise a CLI id, mapping the legacy ``gemini`` alias to ``antigravity``."""
    return _CLI_ALIASES.get(cli, cli)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CLIAccountStatus(BaseModel):
    cli: str
    installed: bool
    version: str | None = None
    authenticated: bool
    authMethod: str | None = None
    email: str | None = None
    credentialsPath: str | None = None
    tokenExpiresAt: str | None = None
    latestVersion: str | None = None


class APIKeyRequest(BaseModel):
    api_key: str = Field(min_length=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_antigravity_binary() -> str:
    """Dynamically resolve the antigravity / gemini binary path."""
    if shutil.which("antigravity"):
        return "antigravity"
    custom_path = ANTIGRAVITY_INSTALL_DIR / "bin" / "antigravity"
    if custom_path.exists():
        return str(custom_path)
    if shutil.which("gemini"):
        return "gemini"
    # Fallback to antigravity since we preinstall it by default
    return "antigravity"


# Back-compat alias for the old helper name (imported by insights provider).
get_gemini_binary = get_antigravity_binary


def _validate_cli(cli: str) -> str:
    """Validate + normalise a CLI id. Returns the canonical id.

    Accepts the legacy ``gemini`` alias (maps to ``antigravity``).
    """
    canonical = _canonical_cli(cli)
    if canonical not in SUPPORTED_CLIS:
        supported = ", ".join(sorted(SUPPORTED_CLIS))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported CLI: {cli}. Must be one of: {supported} (alias 'gemini' accepted)",
        )
    return canonical


def _detect_cli_version(cli: str) -> str | None:
    """Detect if a CLI is installed and return its version string.

    Uses shutil.which for fast PATH lookup. For Node.js CLIs with slow
    startup (e.g. Gemini ~4s), reads version from package.json instead.
    Falls back to bash -l -c only when the binary isn't on the non-login PATH.
    """
    cli = _canonical_cli(cli)
    cfg = CLI_CONFIG[cli]
    if cli == "antigravity":
        binary = get_antigravity_binary()
    else:
        binary = cfg["binary"]

    # Fast path: check if binary is on PATH without spawning a shell
    bin_path = shutil.which(binary) if not binary.startswith("/") else binary
    if not bin_path or (binary.startswith("/") and not Path(bin_path).exists()):
        bin_path = None
        # Fallback: try login shell in case PATH is set in .bashrc/.profile
        # (only useful for bare binary names, not absolute paths).
        if not binary.startswith("/"):
            try:
                result = subprocess.run(
                    ["bash", "-l", "-c", f"which {shlex.quote(binary)}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    bin_path = result.stdout.strip()
            except Exception:
                pass

    if not bin_path:
        # Final fallback: probe well-known install locations the user's PATH
        # may not include. Antigravity CLI (the post-Gemini-sunset successor)
        # ships `gemini` (and an `antigravity` alias) under
        # ~/.gemini/antigravity-cli/bin/ by default; that directory is rarely
        # on PATH but the binary IS installed.
        candidates = []
        if (
            cli == "antigravity"
            or binary in ("gemini", "antigravity")
            or (
                binary.startswith("/")
                and (binary.endswith("gemini") or binary.endswith("antigravity"))
            )
        ):
            candidates += [
                ANTIGRAVITY_INSTALL_DIR / "bin" / "antigravity",
                ANTIGRAVITY_INSTALL_DIR / "bin" / "gemini",
            ]
        for candidate in candidates:
            if candidate.is_file() or candidate.is_symlink():
                bin_path = str(candidate)
                break

    if not bin_path:
        return None

    # For npm-installed CLIs, try reading version from package.json
    # (avoids slow Node.js startup, e.g. gemini --version takes ~4s)
    pkg_version = _read_npm_package_version(bin_path)
    if pkg_version:
        return pkg_version

    # Run version command directly (no login shell overhead)
    try:
        cmd = f"{binary} --version" if cli == "antigravity" else cfg["version_cmd"]
        result = subprocess.run(
            cmd.split(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            raw = result.stdout.strip()
            parts = raw.split()
            for part in reversed(parts):
                if part and part[0].isdigit():
                    return part
            return raw
    except Exception:
        pass
    return None


def _read_npm_package_version(bin_path: str) -> str | None:
    """Try to read version from the npm package.json for a globally-installed CLI."""
    try:
        real_path = Path(bin_path).resolve()
        # npm global layout: .../lib/node_modules/<pkg>/dist/index.js
        # bin symlink points to the dist entry point
        # Walk up to find package.json
        for parent in real_path.parents:
            pkg_json = parent / "package.json"
            if pkg_json.exists():
                data = json.loads(pkg_json.read_text())
                version = data.get("version")
                if version:
                    return version
                break
    except Exception:
        pass
    return None


def _read_json_file(path: Path) -> dict | None:
    """Safely read and parse a JSON file."""
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read {path}: {e}")
    return None


def _extract_email_from_jwt(token: str) -> str | None:
    """Extract email from a JWT id_token payload without verification."""
    try:
        payload_b64 = token.split(".")[1]
        # Add padding for base64
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("email")
    except Exception:
        return None


def _get_codex_email() -> str | None:
    """Extract email from Codex auth.json id_token."""
    auth_data = _read_json_file(CLI_CONFIG["codex"]["credentials_file"])
    if auth_data:
        id_token = auth_data.get("tokens", {}).get("id_token", "")
        if id_token:
            return _extract_email_from_jwt(id_token)
    return None


def _get_gemini_email() -> str | None:
    """Extract email from Antigravity oauth_creds.json id_token."""
    oauth_data = _read_json_file(CLI_CONFIG["antigravity"]["oauth_credentials_file"])
    if oauth_data:
        id_token = oauth_data.get("id_token", "")
        if id_token:
            return _extract_email_from_jwt(id_token)
    return None


def _check_latest_version(cli: str) -> str | None:
    """Check npm registry for the latest version of a CLI package."""
    package = CLI_CONFIG[cli]["npm_package"]
    npm_bin = shutil.which("npm")
    if not npm_bin:
        return None
    try:
        result = subprocess.run(
            [npm_bin, "show", package, "version"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        logger.debug(f"Failed to check latest version for {package}: {e}")
    return None


def _detect_codex_credentials() -> tuple[bool, str | None, str | None]:
    """Check for Codex credentials. Returns (authenticated, authMethod, tokenExpiresAt).

    Codex auth.json structure:
    {
      "OPENAI_API_KEY": null,
      "tokens": {
        "id_token": "...",
        "access_token": "...",
        "refresh_token": "...",
        "account_id": "..."
      },
      "last_refresh": "2026-01-16T22:55:10Z"
    }
    """
    cfg = CLI_CONFIG["codex"]

    # Check stored credentials first
    stored = _read_json_file(cfg["stored_credentials"])
    if stored:
        source = stored.get("source", "")
        if source == "api_key" and stored.get("api_key"):
            return True, "api_key", None
        if stored.get("access_token"):
            return True, "oauth", stored.get("expires_at")

    # Check CLI's native auth.json — tokens are nested under "tokens" key
    auth_data = _read_json_file(cfg["credentials_file"])
    if auth_data:
        tokens = auth_data.get("tokens", {})
        if tokens.get("access_token") or tokens.get("refresh_token"):
            return True, "oauth", auth_data.get("expires_at")

    # Check config.toml for API key
    config_path = cfg["config_file"]
    try:
        if config_path.exists():
            content = config_path.read_text()
            if "api_key" in content or "OPENAI_API_KEY" in content:
                return True, "api_key", None
    except OSError:
        pass

    # Check environment variable
    if os.environ.get("OPENAI_API_KEY"):
        return True, "api_key", None

    return False, None, None


def _detect_gemini_credentials() -> tuple[bool, str | None, str | None]:
    """Check for Gemini credentials. Returns (authenticated, authMethod, tokenExpiresAt).

    Gemini settings.json structure:
    { "security": { "auth": { "selectedType": "oauth-personal" } } }

    OAuth credentials are stored separately in ~/.gemini/oauth_creds.json.
    """
    cfg = CLI_CONFIG["antigravity"]

    # Check stored credentials first
    stored = _read_json_file(cfg["stored_credentials"])
    if stored:
        source = stored.get("source", "")
        if source == "api_key" and stored.get("api_key"):
            return True, "api_key", None
        auth_method = stored.get("authMethod")
        if auth_method in ("google_login", "oauth"):
            return True, "google_login", None

    # Check CLI's native settings.json — auth type is nested
    settings = _read_json_file(cfg["credentials_file"])
    if settings:
        # Nested path: security.auth.selectedType
        selected_type = (
            settings.get("security", {}).get("auth", {}).get("selectedType", "")
        )
        if selected_type in ("oauth-personal", "LOGIN_WITH_GOOGLE"):
            return True, "google_login", None
        if selected_type == "API_KEY" or settings.get("apiKey"):
            return True, "api_key", None

    # Check for separate oauth_creds.json file
    oauth_creds_path = cfg["oauth_credentials_file"]
    if oauth_creds_path.exists():
        oauth_data = _read_json_file(oauth_creds_path)
        if oauth_data:
            return True, "google_login", None

    # Check environment variable
    if os.environ.get("GEMINI_API_KEY"):
        return True, "api_key", None

    return False, None, None


def _get_cli_status(cli: str) -> CLIAccountStatus:
    """Get full status for a CLI."""
    cli = _canonical_cli(cli)
    version = _detect_cli_version(cli)
    installed = version is not None

    authenticated = False
    auth_method = None
    token_expires_at = None
    credentials_path = None
    latest_version = None
    email = None

    if installed:
        if cli == "codex":
            authenticated, auth_method, token_expires_at = _detect_codex_credentials()
            email = _get_codex_email()
        else:
            authenticated, auth_method, token_expires_at = _detect_gemini_credentials()
            email = _get_gemini_email()

        stored_path = CLI_CONFIG[cli]["stored_credentials"]
        if stored_path.exists():
            credentials_path = str(stored_path)

        # Check for latest version (non-blocking, best-effort)
        latest_version = _check_latest_version(cli)

    return CLIAccountStatus(
        cli=cli,
        installed=installed,
        version=version,
        authenticated=authenticated,
        authMethod=auth_method,
        email=email,
        credentialsPath=credentials_path,
        tokenExpiresAt=token_expires_at,
        latestVersion=latest_version,
    )


def _save_credentials(cli: str, data: dict) -> None:
    """Save credentials to ~/.aifactory/{cli}-credentials.json with 0o600."""
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    path = CLI_CONFIG[cli]["stored_credentials"]
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


def _poll_codex_token(mtime_before: float) -> None:
    """Poll ~/.codex/auth.json for new credentials after user runs `codex login`."""
    credentials_path = CLI_CONFIG["codex"]["credentials_file"]
    for _ in range(90):  # ~3 minutes
        try:
            if credentials_path.exists():
                current_mtime = credentials_path.stat().st_mtime
                if current_mtime > mtime_before:
                    auth_data = _read_json_file(credentials_path)
                    if auth_data:
                        tokens = auth_data.get("tokens", {})
                        if tokens.get("access_token") or tokens.get("refresh_token"):
                            _save_credentials(
                                "codex",
                                {
                                    "source": "cli_login",
                                    "access_token": tokens.get("access_token"),
                                    "refresh_token": tokens.get("refresh_token"),
                                    "expires_at": auth_data.get("expires_at"),
                                    "imported_at": time.strftime(
                                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                    ),
                                },
                            )
                            logger.info("[Codex] Credentials detected and saved")
                            _broadcast_cli_auth_event("codex", True)
                            return
        except Exception as e:
            logger.warning(f"[Codex] Polling error: {e}")
        time.sleep(2)
    logger.warning("[Codex] Credentials not detected within timeout")
    _broadcast_cli_auth_event("codex", False)


def _poll_gemini_token(mtime_before: float) -> None:
    """Poll ~/.gemini/settings.json and oauth_creds.json for new credentials."""
    settings_path = CLI_CONFIG["antigravity"]["credentials_file"]
    oauth_path = CLI_CONFIG["antigravity"]["oauth_credentials_file"]

    for _ in range(90):  # ~3 minutes
        try:
            # Check settings.json
            settings_changed = False
            if settings_path.exists():
                current_mtime = settings_path.stat().st_mtime
                if current_mtime > mtime_before:
                    settings_changed = True

            # Also check oauth_creds.json
            oauth_changed = False
            if oauth_path.exists():
                oauth_mtime = oauth_path.stat().st_mtime
                if oauth_mtime > mtime_before:
                    oauth_changed = True

            if settings_changed or oauth_changed:
                settings = (
                    _read_json_file(settings_path) if settings_path.exists() else {}
                )
                selected_type = ""
                if settings:
                    selected_type = (
                        settings.get("security", {})
                        .get("auth", {})
                        .get("selectedType", "")
                    )

                if (
                    selected_type in ("oauth-personal", "LOGIN_WITH_GOOGLE")
                    or oauth_changed
                ):
                    _save_credentials(
                        "antigravity",
                        {
                            "source": "cli_login",
                            "selectedType": selected_type or "oauth-personal",
                            "authMethod": "google_login",
                            "imported_at": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            ),
                        },
                    )
                    logger.info("[Gemini] Credentials detected and saved")
                    _broadcast_cli_auth_event("antigravity", True)
                    return
                elif selected_type == "API_KEY" or (
                    settings and settings.get("apiKey")
                ):
                    _save_credentials(
                        "antigravity",
                        {
                            "source": "cli_login",
                            "selectedType": selected_type,
                            "authMethod": "api_key",
                            "imported_at": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            ),
                        },
                    )
                    logger.info("[Gemini] API key credentials detected and saved")
                    _broadcast_cli_auth_event("antigravity", True)
                    return
        except Exception as e:
            logger.warning(f"[Gemini] Polling error: {e}")
        time.sleep(2)
    logger.warning("[Gemini] Credentials not detected within timeout")
    _broadcast_cli_auth_event("antigravity", False)


def _broadcast_cli_auth_event(cli: str, success: bool) -> None:
    """Broadcast a cli-account-auth event via WebSocket."""
    try:
        from ..websockets.events import broadcast_event

        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            broadcast_event("cli-account-auth", {"cli": cli, "success": success})
        )
        loop.close()
    except Exception as e:
        logger.warning(f"Failed to broadcast cli-account-auth event: {e}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/cli-accounts/detect")
async def detect_cli_accounts():
    """Detect Codex and Antigravity CLIs and their credential status.

    The response is keyed by the canonical ``antigravity`` id, and also
    duplicated under the legacy ``gemini`` key so older frontends keep working.
    """
    loop = asyncio.get_event_loop()
    codex_future = loop.run_in_executor(None, _get_cli_status, "codex")
    antigravity_future = loop.run_in_executor(None, _get_cli_status, "antigravity")
    codex_status, antigravity_status = await asyncio.gather(
        codex_future, antigravity_future
    )
    antigravity_dump = antigravity_status.model_dump()
    return {
        "codex": codex_status.model_dump(),
        "antigravity": antigravity_dump,
        # Back-compat: legacy clients read cliAccounts.gemini.
        "gemini": antigravity_dump,
    }


@router.get("/cli-accounts/{cli}/status")
async def get_cli_status(cli: str):
    """Get detailed status for a specific CLI."""
    cli = _validate_cli(cli)
    return _get_cli_status(cli).model_dump()


@router.post("/cli-accounts/{cli}/import")
async def import_cli_credentials(cli: str):
    """Import existing credentials from the CLI's default location."""
    cli = _validate_cli(cli)
    cfg = CLI_CONFIG[cli]

    if cli == "codex":
        auth_data = _read_json_file(cfg["credentials_file"])
        if auth_data:
            tokens = auth_data.get("tokens", {})
            if tokens.get("access_token") or tokens.get("refresh_token"):
                _save_credentials(
                    cli,
                    {
                        "source": "import",
                        "access_token": tokens.get("access_token"),
                        "refresh_token": tokens.get("refresh_token"),
                        "expires_at": auth_data.get("expires_at"),
                        "imported_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                    },
                )
                return {
                    "success": True,
                    "message": "Codex credentials imported successfully",
                }
        return {
            "success": False,
            "error": "No Codex credentials found at ~/.codex/auth.json",
        }

    else:  # antigravity
        # Check settings.json for auth type
        settings = _read_json_file(cfg["credentials_file"])
        selected_type = ""
        if settings:
            selected_type = (
                settings.get("security", {}).get("auth", {}).get("selectedType", "")
            )

        # Check oauth_creds.json
        oauth_creds = _read_json_file(cfg["oauth_credentials_file"])

        if selected_type in ("oauth-personal", "LOGIN_WITH_GOOGLE") or oauth_creds:
            _save_credentials(
                cli,
                {
                    "source": "import",
                    "selectedType": selected_type or "oauth-personal",
                    "authMethod": "google_login",
                    "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            return {
                "success": True,
                "message": "Antigravity credentials imported successfully",
            }
        if settings and (selected_type == "API_KEY" or settings.get("apiKey")):
            _save_credentials(
                cli,
                {
                    "source": "import",
                    "selectedType": selected_type,
                    "authMethod": "api_key",
                    "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            return {
                "success": True,
                "message": "Antigravity credentials imported successfully",
            }
        return {
            "success": False,
            "error": "No Antigravity credentials found at ~/.gemini/settings.json",
        }


@router.post("/cli-accounts/{cli}/api-key")
async def set_cli_api_key(cli: str, body: APIKeyRequest):
    """Save a manual API key for a CLI."""
    cli = _validate_cli(cli)
    _save_credentials(
        cli,
        {
            "source": "api_key",
            "api_key": body.api_key,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    return {"success": True, "message": f"API key saved for {cli}"}


@router.post("/cli-accounts/{cli}/start-login")
async def start_cli_login(cli: str):
    """Start terminal-based login polling.

    Records the credential file mtime, then starts a background thread
    that watches for file changes (same pattern as Claude OAuth polling).
    The frontend should instruct the user to run the login command in
    a terminal.
    """
    cli = _validate_cli(cli)
    cfg = CLI_CONFIG[cli]
    credentials_path = cfg["credentials_file"]

    mtime_before = credentials_path.stat().st_mtime if credentials_path.exists() else 0

    if cli == "codex":
        threading.Thread(
            target=_poll_codex_token, args=(mtime_before,), daemon=True
        ).start()
        return {
            "success": True,
            "data": {
                "message": "Polling started. Run 'codex login' in your terminal to authenticate.",
                "command": "codex login",
            },
        }
    else:  # antigravity
        threading.Thread(
            target=_poll_gemini_token, args=(mtime_before,), daemon=True
        ).start()
        binary = get_antigravity_binary()
        return {
            "success": True,
            "data": {
                "message": f"Polling started. Run '{binary}' in your terminal and select Login with Google.",
                "command": binary,
            },
        }


@router.post("/cli-accounts/{cli}/start-login-terminal")
async def start_cli_login_terminal(cli: str):
    """Start an interactive terminal session that runs the CLI login command.

    Creates a PTY session, sends the auth command, and starts credential
    polling in the background. Returns the terminal ID so the frontend
    can connect via WebSocket to show the interactive session.
    """
    cli = _validate_cli(cli)
    cfg = CLI_CONFIG[cli]

    # Verify CLI is installed
    version = _detect_cli_version(cli)
    if not version:
        raise HTTPException(
            status_code=400,
            detail=f"{cli} CLI is not installed. Please install it first.",
        )

    # Determine the auth command
    if cli == "codex":
        auth_command = "codex auth login"
    else:
        auth_command = f"{get_antigravity_binary()} auth login"

    # Create a PTY terminal session
    from ..pty.manager import get_pty_manager

    manager = get_pty_manager()
    terminal_id = manager.create_session(
        cwd=str(Path.home()),
        shell=None,
        env=None,
    )

    # Send the auth command to the terminal after a brief delay
    # to let the shell initialize
    def _send_auth_command():
        time.sleep(0.5)
        manager.write(terminal_id, auth_command + "\n")

    threading.Thread(target=_send_auth_command, daemon=True).start()

    # Start credential file polling in background
    credentials_path = cfg["credentials_file"]
    mtime_before = credentials_path.stat().st_mtime if credentials_path.exists() else 0

    if cli == "codex":
        threading.Thread(
            target=_poll_codex_token, args=(mtime_before,), daemon=True
        ).start()
    else:
        threading.Thread(
            target=_poll_gemini_token, args=(mtime_before,), daemon=True
        ).start()

    return {
        "success": True,
        "data": {
            "terminalId": terminal_id,
            "command": auth_command,
            "message": f"Terminal created. Running '{auth_command}'...",
        },
    }


def _run_login_shell(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a command inside a login shell.

    Takes an argument list (not a raw string) to prevent shell injection.
    """
    safe_cmd = " ".join(shlex.quote(a) for a in args)
    return subprocess.run(
        ["bash", "-l", "-c", safe_cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _create_antigravity_symlink() -> None:
    """Create the ``antigravity`` -> ``gemini`` symlink in the install bin dir.

    Mirrors the symlink step in ``scripts/install-backend.js`` so installing
    the Antigravity CLI via the portal matches installing it via the script:
    the bundled binary is named ``gemini`` and we expose it as ``antigravity``.
    """
    try:
        bin_dir = ANTIGRAVITY_INSTALL_DIR / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        symlink_path = bin_dir / "antigravity"
        target_path = bin_dir / "gemini"
        if target_path.exists():
            if symlink_path.exists() or symlink_path.is_symlink():
                symlink_path.unlink()
            symlink_path.symlink_to("gemini")
            logger.info("Successfully created antigravity -> gemini symlink.")
    except Exception as se:
        logger.error(f"Failed to create antigravity symlink: {se}")


def _npm_install_cli(cli: str, package: str) -> subprocess.CompletedProcess:
    """Run ``npm install -g`` for a CLI, scoped to its install dir if any.

    For Antigravity, installs ``@google/gemini-cli`` into
    ``~/.gemini/antigravity-cli`` (matching ``scripts/install-backend.js``) and
    creates the ``antigravity`` symlink afterwards.  For other CLIs, installs
    globally.  Re-running performs an update to the latest published version.
    """
    install_dir = CLI_CONFIG[cli].get("install_dir")
    if install_dir is not None:
        Path(install_dir).parent.mkdir(parents=True, exist_ok=True)
        result = _run_login_shell(
            ["npm", "install", "-g", "--prefix", str(install_dir), package],
            timeout=120,
        )
        if result.returncode == 0:
            _create_antigravity_symlink()
        return result
    return _run_login_shell(["npm", "install", "-g", package], timeout=120)


@router.post("/cli-accounts/{cli}/install")
def install_or_update_cli(cli: str):
    """Install or update a CLI tool via npm.

    Pattern based on git.py install_claude_code():
    1. Check if Node.js/npm is available
    2. npm install -g <package> (works for both install and update)
    3. Verify with <cli> --version
    4. Return {success, version, wasUpdate}

    For Antigravity, the install mirrors ``scripts/install-backend.js``:
    ``@google/gemini-cli`` is installed into ``~/.gemini/antigravity-cli`` and
    the ``antigravity`` symlink is (re)created.  Re-running this endpoint
    updates an existing install to the latest published version.
    """
    cli = _validate_cli(cli)
    cfg = CLI_CONFIG[cli]
    package = cfg["npm_package"]

    # Check existing version (to determine install vs update)
    old_version = _detect_cli_version(cli)
    was_update = old_version is not None

    # Step 1: Check Node.js availability
    try:
        # Two commands — use hardcoded shell string (no user input)
        node_check = subprocess.run(
            ["bash", "-l", "-c", "node --version && npm --version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if node_check.returncode != 0:
            return {
                "success": False,
                "error": "Node.js/npm not found. Please install Node.js first.",
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to check Node.js: {e}",
        }

    # Step 2: Install/update via npm (scoped to the install dir for Antigravity)
    try:
        logger.info(f"[{cli}] Running npm install -g {package}...")
        install_result = _npm_install_cli(cli, package)

        if install_result.returncode != 0:
            error_msg = install_result.stderr.strip() or install_result.stdout.strip()
            return {
                "success": False,
                "error": f"npm install failed: {error_msg}",
            }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Installation timed out after 120 seconds.",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Installation failed: {e}",
        }

    # Step 3: Verify installation
    new_version = _detect_cli_version(cli)
    if not new_version:
        return {
            "success": False,
            "error": f"{cli} not found after installation. You may need to restart your shell.",
        }

    action = "updated" if was_update else "installed"
    logger.info(f"[{cli}] Successfully {action}: {new_version}")

    return {
        "success": True,
        "version": new_version,
        "wasUpdate": was_update,
        "message": f"{cli.capitalize()} CLI {action} successfully ({new_version})",
    }


@router.delete("/cli-accounts/{cli}")
async def remove_cli_account(cli: str):
    """Remove stored credentials for a CLI."""
    cli = _validate_cli(cli)
    path = CLI_CONFIG[cli]["stored_credentials"]
    if path.exists():
        path.unlink()
        return {"success": True, "message": f"Credentials removed for {cli}"}
    return {"success": True, "message": f"No stored credentials found for {cli}"}
