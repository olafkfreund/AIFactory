"""
Authentication helpers for AIFactory.

Provides centralized authentication token resolution with fallback support
for multiple environment variables, AIFactory profiles, Claude Code CLI
credentials, and SDK environment variable passthrough for custom API endpoints.
"""

import importlib
import json
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Any

# Priority order for auth token resolution.
# NOTE: By DEFAULT we do NOT fall back to ANTHROPIC_API_KEY — AIFactory targets
# Claude subscription (Max) OAuth, and a stray API key would silently bill the
# user's API credits when OAuth is misconfigured/expired. Operators who bill via
# a direct API key (no subscription) opt in with AIFACTORY_ALLOW_API_KEY=1 — see
# api_key_auth_enabled(); when enabled, ANTHROPIC_API_KEY becomes a valid auth
# source and is no longer scrubbed from agents.
AUTH_TOKEN_ENV_VARS = [
    "CLAUDE_CODE_OAUTH_TOKEN",  # OAuth token from Claude Code CLI
    "ANTHROPIC_AUTH_TOKEN",  # CCR/proxy token (for enterprise setups)
]


def api_key_auth_enabled() -> bool:
    """True when the operator opted into direct ANTHROPIC_API_KEY auth.

    Default **off**: AIFactory is OAuth-only (Claude subscription), and any
    ANTHROPIC_API_KEY in the environment is scrubbed from agents so it can never
    silently bill the Anthropic API. Operators whose intended billing model IS a
    direct API key (no Max subscription) set ``AIFACTORY_ALLOW_API_KEY=1`` to
    permit it — then the key is accepted as an auth token and passed through to
    agents instead of being blanked.
    """
    return os.environ.get("AIFACTORY_ALLOW_API_KEY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def headless_prefer_api_key() -> bool:
    """True when headless builds should PREFER the non-expiring ANTHROPIC_API_KEY
    over the interactive OAuth subscription token (#611 / RFC-0008 §3.2b).

    Only meaningful together with :func:`api_key_auth_enabled` — the key must be
    permitted before it can be preferred. Default **off**: OAuth/subscription
    stays the default so the anti-silent-billing guard holds unless an operator
    opts in (e.g. for headless / benchmark runs where the OAuth token expires).
    """
    return os.environ.get("AIFACTORY_HEADLESS_PREFER_API_KEY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# Environment variables to pass through to SDK subprocess
# NOTE: ANTHROPIC_API_KEY is excluded here by default (prevents silent API
# billing); it is added dynamically by get_sdk_env_vars() only when
# AIFACTORY_ALLOW_API_KEY is set (opt-in).
SDK_ENV_VARS = [
    # API endpoint configuration
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    # Model overrides (from API Profile custom model mappings)
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    # SDK behavior configuration
    "NO_PROXY",
    "DISABLE_TELEMETRY",
    "DISABLE_COST_WARNINGS",
    "API_TIMEOUT_MS",
]


# --- Agent env credential scrubbing (#363 / H1, slice 1) -------------------
#
# The Claude Agent SDK spawns the agent CLI with the FULL parent environment
# merged in (``process_env = {**os.environ, ..., **options.env}`` in the SDK's
# subprocess transport). So without scrubbing, the agent's Bash subprocess
# inherits every host secret the web-server holds — the AIFactory admin
# ``API_TOKEN``, the ``JWT_SECRET``, ``DATABASE_URL``, cloud + Vault creds — all
# reachable via ``env`` / ``printenv`` and exfiltratable by a prompt-injected
# command. An isolated feature-build agent never legitimately needs these.
#
# Since ``options.env`` wins over the inherited environment, we neutralize the
# secrets by setting them to empty there. The agent's OWN auth vars
# (``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_AUTH_TOKEN`` / SDK passthrough) are
# preserved so it keeps working. (Full OS isolation — caps/RO-FS/egress — is the
# remainder of #363.)

# Vars the agent CLI legitimately needs — never blanked.
_AGENT_ENV_KEEP: set[str] = {
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    *SDK_ENV_VARS,
}

# Specific host/infra secrets to neutralize in the agent environment.
_AGENT_ENV_DENY_EXACT: set[str] = {
    # AIFactory's own control-plane secrets — an agent with these could call the
    # web-server as admin or forge tokens.
    "API_TOKEN",
    "APP_API_TOKEN",
    "JWT_SECRET",
    "APP_JWT_SECRET",
    "DATABASE_URL",
    "APP_DATABASE_URL",
    # Provider API keys (the agent authenticates via OAuth, not these).
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "OPENROUTER_API_KEY",
    "VOYAGE_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    # Cloud + secrets-manager credentials.
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AZURE_CLIENT_SECRET",
    "VAULT_TOKEN",
    # Integrations.
    "LINEAR_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
}

# Generic secret-bearing names (any host var matching is neutralized).
_AGENT_ENV_DENY_PATTERN = re.compile(
    r"(SECRET|PASSWORD|PRIVATE_KEY|CREDENTIAL|_KMS|PASSPHRASE)", re.I
)


def get_agent_env_blanks() -> dict[str, str]:
    """Return ``{var: ""}`` for host secrets that must not reach the agent.

    Blanking (not deleting) because the SDK merges ``options.env`` over the
    inherited environment — an explicit empty value overrides the inherited
    secret. Never blanks the agent's own auth vars (:data:`_AGENT_ENV_KEEP`).
    """
    allow_api_key = api_key_auth_enabled()
    blanks: dict[str, str] = {}
    for key in os.environ:
        if key in _AGENT_ENV_KEEP:
            continue
        # When the operator opted into API-key auth, the agent legitimately needs
        # ANTHROPIC_API_KEY — don't blank it. Default (off) still scrubs it.
        if key == "ANTHROPIC_API_KEY" and allow_api_key:
            continue
        if key in _AGENT_ENV_DENY_EXACT or _AGENT_ENV_DENY_PATTERN.search(key):
            blanks[key] = ""
    return blanks


def get_token_from_keychain() -> str | None:
    """
    Get authentication token from system credential store.

    Reads Claude Code credentials from:
    - macOS: Keychain
    - Windows: Credential Manager
    - Linux: Not yet supported (use env var)

    Returns:
        Token string if found, None otherwise
    """
    system = platform.system()

    if system == "Darwin":
        return _get_token_from_macos_keychain()
    elif system == "Windows":
        return _get_token_from_windows_credential_files()
    else:
        # Linux: secret-service not yet implemented
        return None


def _get_token_from_macos_keychain() -> str | None:
    """Get token from macOS Keychain."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode != 0:
            return None

        credentials_json = result.stdout.strip()
        if not credentials_json:
            return None

        data = json.loads(credentials_json)
        token = data.get("claudeAiOauth", {}).get("accessToken")

        if not token:
            return None

        # Validate token format (Claude OAuth tokens start with sk-ant-oat01-)
        if not token.startswith("sk-ant-oat01-"):
            return None

        return token

    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, Exception):
        return None


def _get_token_from_windows_credential_files() -> str | None:
    """Get token from Windows credential files.

    Claude Code on Windows stores credentials in ~/.claude/.credentials.json
    """
    try:
        # Claude Code stores credentials in ~/.claude/.credentials.json
        cred_paths = [
            os.path.expandvars(r"%USERPROFILE%\.claude\.credentials.json"),
            os.path.expandvars(r"%USERPROFILE%\.claude\credentials.json"),
            os.path.expandvars(r"%LOCALAPPDATA%\Claude\credentials.json"),
            os.path.expandvars(r"%APPDATA%\Claude\credentials.json"),
        ]

        for cred_path in cred_paths:
            if os.path.exists(cred_path):
                with open(cred_path, encoding="utf-8") as f:
                    data = json.load(f)
                    token = data.get("claudeAiOauth", {}).get("accessToken")
                    if token and token.startswith("sk-ant-oat01-"):
                        return token

        return None

    except (json.JSONDecodeError, KeyError, FileNotFoundError, Exception):
        return None


def unseal_profiles(data: dict[str, Any]) -> dict[str, Any]:
    """Decrypt the sealed OAuth tokens in a claude-profiles.json payload (#1276).

    ``server.crypto`` lives in the web-server package, which apps/backend runs
    alongside in the deployed image. A store written before #1276 holds
    plaintext and passes through untouched.

    Resolved by name rather than imported: a plain import of ``server.*`` from
    apps/backend is un-gateable here -- at module scope mypy --strict reports
    import-not-found, and inside the function ruff reports PLC0415.

    If the web-server package is genuinely absent the payload is returned as
    read; a sealed value is then an ``enc.v1:``-prefixed string that no Claude
    endpoint accepts, so auth fails visibly instead of silently succeeding with
    the wrong identity.
    """
    try:
        module = importlib.import_module("server.crypto.secret_field")
    except ImportError:  # pragma: no cover - web-server package not importable
        return data
    unsealed: dict[str, Any] = module.unseal_profiles(data)
    return unsealed


def _get_token_from_aifactory_profiles() -> str | None:
    """Read token from AIFactory profiles storage (~/.aifactory/claude-profiles.json)."""
    path = Path.home() / ".aifactory" / "claude-profiles.json"
    if not path.exists():
        return None
    try:
        data = unseal_profiles(json.loads(path.read_text()))
        active_id = data.get("activeProfileId")
        for profile in data.get("profiles", []):
            if profile.get("id") == active_id and profile.get("oauthToken"):
                return profile["oauthToken"]
        # If no active profile, return first token found
        for profile in data.get("profiles", []):
            if profile.get("oauthToken"):
                return profile["oauthToken"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _get_token_from_claude_credentials() -> str | None:
    """Read token from official Claude Code CLI credentials (~/.claude/.credentials.json)."""
    path = Path.home() / ".claude" / ".credentials.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        token = data.get("claudeAiOauth", {}).get("accessToken")
        if token and token.startswith("sk-ant-oat01-"):
            return token
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def get_auth_token() -> str | None:
    """
    Get authentication token from environment variables or credential stores.

    Checks multiple sources in priority order:
    1. CLAUDE_CODE_OAUTH_TOKEN (env var)
    2. ANTHROPIC_AUTH_TOKEN (CCR/proxy env var for enterprise setups)
    3. AIFactory profiles (~/.aifactory/claude-profiles.json)
    4. Claude Code CLI credentials (~/.claude/.credentials.json)
    5. System credential store (macOS Keychain, Windows Credential Manager)

    NOTE: ANTHROPIC_API_KEY is intentionally NOT supported to prevent
    silent billing to user's API credits when OAuth is misconfigured.

    Returns:
        Token string if found, None otherwise
    """
    # Opt-in (#611): headless/benchmark runs may prefer the non-expiring
    # ANTHROPIC_API_KEY over the interactive OAuth token — but only when API-key
    # auth is already permitted. OAuth remains the default everywhere else.
    if headless_prefer_api_key() and api_key_auth_enabled():
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            return api_key

    # First check environment variables
    for var in AUTH_TOKEN_ENV_VARS:
        token = os.environ.get(var)
        if token:
            return token

    # Check AIFactory profiles
    token = _get_token_from_aifactory_profiles()
    if token:
        return token

    # Check Claude Code CLI credentials
    token = _get_token_from_claude_credentials()
    if token:
        return token

    # System credential store
    token = get_token_from_keychain()
    if token:
        return token

    # Opt-in only: a direct API key counts as auth when AIFACTORY_ALLOW_API_KEY
    # is set (operators billing via API, no subscription). Off by default so a
    # stray key never silently substitutes for a missing OAuth token.
    if api_key_auth_enabled():
        return os.environ.get("ANTHROPIC_API_KEY") or None

    return None


def get_auth_token_source() -> str | None:
    """Get the name of the source that provided the auth token."""
    # Mirror get_auth_token()'s opt-in headless API-key preference (#611).
    if (
        headless_prefer_api_key()
        and api_key_auth_enabled()
        and os.environ.get("ANTHROPIC_API_KEY")
    ):
        return "ANTHROPIC_API_KEY (headless preference)"

    # Check environment variables first
    for var in AUTH_TOKEN_ENV_VARS:
        if os.environ.get(var):
            return var

    # Check AIFactory profiles
    if _get_token_from_aifactory_profiles():
        return "AIFactory Profiles"

    # Check Claude Code CLI credentials
    if _get_token_from_claude_credentials():
        return "Claude Code CLI Credentials"

    # Check if token came from system credential store
    if get_token_from_keychain():
        system = platform.system()
        if system == "Darwin":
            return "macOS Keychain"
        elif system == "Windows":
            return "Windows Credential Files"
        else:
            return "System Credential Store"

    # Opt-in direct API key (AIFACTORY_ALLOW_API_KEY).
    if api_key_auth_enabled() and os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY (opt-in)"

    return None


def require_auth_token() -> str:
    """
    Get authentication token or raise ValueError.

    Raises:
        ValueError: If no auth token is found in any supported source
    """
    token = get_auth_token()
    if not token:
        error_msg = (
            "No OAuth token found.\n\n"
            "AIFactory uses Claude Code OAuth authentication by default.\n"
            "Direct API keys (ANTHROPIC_API_KEY) are opt-in: set "
            "AIFACTORY_ALLOW_API_KEY=1 to bill via a direct key instead.\n\n"
        )
        # Provide platform-specific guidance
        system = platform.system()
        if system == "Darwin":
            error_msg += (
                "To authenticate:\n"
                "  1. Run: claude setup-token\n"
                "  2. The token will be saved to macOS Keychain automatically\n\n"
                "Or set CLAUDE_CODE_OAUTH_TOKEN in your .env file."
            )
        elif system == "Windows":
            error_msg += (
                "To authenticate:\n"
                "  1. Run: claude setup-token\n"
                "  2. The token should be saved to Windows Credential Manager\n\n"
                "If auto-detection fails, set CLAUDE_CODE_OAUTH_TOKEN in your .env file.\n"
                "Check: %LOCALAPPDATA%\\Claude\\credentials.json"
            )
        else:
            error_msg += (
                "To authenticate:\n"
                "  1. Run: claude setup-token\n"
                "  2. Set CLAUDE_CODE_OAUTH_TOKEN in your .env file"
            )
        raise ValueError(error_msg)
    return token


def get_sdk_env_vars() -> dict[str, str]:
    """
    Get environment variables to pass to SDK.

    Collects relevant env vars (ANTHROPIC_BASE_URL, etc.) that should
    be passed through to the claude-agent-sdk subprocess.

    Returns:
        Dict of env var name -> value for non-empty vars
    """
    env = {}
    for var in SDK_ENV_VARS:
        value = os.environ.get(var)
        if value:
            env[var] = value
    # Opt-in: pass the direct API key through to the SDK subprocess so the agent
    # CLI can authenticate with it. Off by default (OAuth-only); see
    # api_key_auth_enabled().
    if api_key_auth_enabled():
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
    return env


def ensure_claude_code_oauth_token() -> None:
    """
    Ensure CLAUDE_CODE_OAUTH_TOKEN is set (for SDK compatibility).

    If not set but other auth tokens are available, copies the value
    to CLAUDE_CODE_OAUTH_TOKEN so the underlying SDK can use it.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return

    token = get_auth_token()
    if token:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
