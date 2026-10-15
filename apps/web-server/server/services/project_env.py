"""Shared loader for per-project `.aifactory/.env` files (#1745)."""

import logging
import re
from pathlib import Path

from factory_common.logsafe import sanitize_log

logger = logging.getLogger(__name__)

_AGENT_MCP_KEY = re.compile(r"AGENT_MCP_[A-Za-z0-9_]+_(?:ADD|REMOVE)")
_REFUSED_KEY = re.compile(
    r"LD_.*|DYLD_.*|NODE_OPTIONS|BASH_ENV|ENV|PYTHON.*|GIT_.*|PATH|HOME|SHELL|.*_PROXY",
    re.IGNORECASE,
)
_FACTORY_GIT_KEYS = frozenset(
    {
        "GIT_PROVIDER",
        "GIT_TOKEN",
        "GIT_BASE_URL",
        "GIT_ORG",
        "GIT_PROJECT",
        "GIT_REPO",
    }
)

PROJECT_ENV_KEYS = frozenset(
    {
        # routes/projects.py settings
        "AI_FACTORY_MODEL",
        "MEMORY_BACKEND",
        "GRAPHITI_MCP_URL",
        "MAIN_BRANCH",
        "GIT_PROVIDER",
        "GIT_TOKEN",
        "GIT_BASE_URL",
        "GIT_ORG",
        "GIT_PROJECT",
        "GIT_REPO",
        "GITHUB_TOKEN",
        "GITHUB_REPO",
        "AIFACTORY_PR_REVIEWER",
        "GRAPHITI_ENABLED",
        "USE_CLAUDE_MD",
        "REMOTE_CONTROL_BY_DEFAULT",
        "DELEGATE_BY_DEFAULT",
        "AIFACTORY_AUTO_PR",
        "AIFACTORY_AUTO_MERGE",
        # routes/context.py env
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "ENABLE_FANCY_UI",
        "GRAPHITI_EMBEDDER_PROVIDER",
        "OLLAMA_BASE_URL",
        "OLLAMA_EMBEDDING_MODEL",
        "OLLAMA_EMBEDDING_DIM",
        "OPENAI_API_KEY",
        "VOYAGE_API_KEY",
        "VOYAGE_EMBEDDING_MODEL",
        "GOOGLE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
        "GRAPHITI_DATABASE",
        "GRAPHITI_DB_PATH",
        # core/client.py MCP toggles
        "CONTEXT7_ENABLED",
        "PLAYWRIGHT_MCP_ENABLED",
        "PUPPETEER_MCP_ENABLED",
    }
)


def is_project_env_key(key: str) -> bool:
    """Return True iff ``key`` is an allowed project-level `.aifactory/.env` variable."""
    return bool(
        (key in PROJECT_ENV_KEYS or _AGENT_MCP_KEY.fullmatch(key))
        and (key in _FACTORY_GIT_KEYS or not _REFUSED_KEY.fullmatch(key))
    )


def load_project_env(project_path: Path, env: dict[str, str]) -> None:
    """Load allowed keys from ``<project_path>/.aifactory/.env`` into ``env`` without overriding."""
    env_file = project_path / ".aifactory" / ".env"
    if not env_file.exists():
        return
    try:
        text = env_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(
            "Failed to read project env file %s: %s",
            sanitize_log(str(env_file)),
            sanitize_log(str(e)),
        )
        return

    skipped: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not is_project_env_key(key):
            skipped.add(key)
            continue
        if key not in env:
            env[key] = value

    if skipped:
        skipped_names = ", ".join(sorted(skipped))
        logger.warning(
            "Skipped disallowed keys in %s: %s",
            sanitize_log(str(env_file)),
            sanitize_log(skipped_names),
        )
