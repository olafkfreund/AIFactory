"""Allowlist for per-project settings keys (#1745, #1795)."""

import re

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
        "AIFACTORY_DATA_CLASSIFICATION",
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
