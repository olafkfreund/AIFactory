"""Per-project settings store backed by the web-server database (#1795).

Settings live outside the agent-writable project clone. On the first read for
a project with no database row, an untracked ``<project>/.aifactory/.env`` is
imported once (keeping only keys ``is_store_key`` accepts) and renamed to
``.env.migrated-<UTC YYYY-MM-DD>``. After that, the database row always wins.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory_common.logsafe import sanitize_log
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from server.crypto import secret_field
from server.database.models import ProjectSettings
from server.services.project_env import is_project_env_key
from server.utils.subprocess_env import child_env

logger = logging.getLogger(__name__)

ENV = "AIFACTORY_PROJECT_MCP_CONFIG"

_MCP_FIXED_KEYS: frozenset[str] = frozenset(
    {
        "CONTEXT7_ENABLED",
        "PLAYWRIGHT_MCP_ENABLED",
        "PUPPETEER_MCP_ENABLED",
        "CUSTOM_MCP_SERVERS",
    }
)
_AGENT_MCP_KEY = re.compile(r"AGENT_MCP_[A-Za-z0-9_]+_(?:ADD|REMOVE)")

# In-process snapshot for synchronous readers (pr_endgame).
_SNAPSHOT: dict[str, dict[str, str]] = {}
_WARNED_REAPPEARED: set[str] = set()
_SESSION_FACTORY: Any = None


def project_key(path: str | Path) -> str:
    """Return the sha256 hex of ``os.path.abspath(path)``."""
    return hashlib.sha256(os.path.abspath(path).encode()).hexdigest()  # noqa: PTH100


def is_store_key(k: str) -> bool:
    """Return True iff ``k`` is accepted in the project settings store."""
    return k == "CUSTOM_MCP_SERVERS" or is_project_env_key(k)


def _is_mcp_key(k: str) -> bool:
    return k in _MCP_FIXED_KEYS or bool(_AGENT_MCP_KEY.fullmatch(k))


class ProjectSettingsUnavailableError(RuntimeError):
    """The settings store could not be read, so a launch must not start (#1795)."""


def _unavailable(
    what: str, project_path: str | Path
) -> ProjectSettingsUnavailableError:
    logger.error(
        "project settings unavailable for %s: %s",
        sanitize_log(str(project_path)),
        sanitize_log(what),
    )
    return ProjectSettingsUnavailableError("project settings unavailable: " + what)


def _parse_env_file(path: Path) -> tuple[dict[str, str], list[str]]:
    """Parse ``path`` using #1790's line rules, keeping only keys ``is_store_key`` accepts.

    Returns ``(accepted_dict, skipped_key_names)``. Raises
    ``ProjectSettingsUnavailableError`` when the file can't be read, so nothing
    is stored or renamed and the next read tries again.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise _unavailable(
            f"cannot read legacy env file ({type(e).__name__})", path
        ) from e

    accepted: dict[str, str] = {}
    skipped: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not is_store_key(key):
            if key:
                skipped.add(key)
            continue
        accepted[key] = value

    return accepted, sorted(skipped)


_GIT_NOT_A_REPO = 128


def _is_tracked(project_path: str | Path) -> bool:
    """Return True iff git tracks ``.aifactory/.env`` inside ``project_path``.

    Fails closed: only exit 128 (not a git repo) counts as untracked. Any other
    error raises, so the import waits for a later read. The clone is
    agent-writable, so its fsmonitor and hooks are switched off for this call.
    """
    try:
        res = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [  # noqa: S607
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(project_path),
                "ls-files",
                "--",
                ".aifactory/.env",
            ],
            env=child_env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise _unavailable(
            f"git check failed ({type(e).__name__})", project_path
        ) from e
    if res.returncode == _GIT_NOT_A_REPO:
        return False
    if res.returncode != 0:
        raise _unavailable(f"git check exited {res.returncode}", project_path)
    return bool(res.stdout.strip())


def _rename_legacy(env_file: Path) -> Path | None:
    """Rename ``env_file`` to ``.env.migrated-<UTC YYYY-MM-DD>[-N]`` without overwriting."""
    if not env_file.exists():
        return None
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    base_name = f".env.migrated-{date_str}"
    target = env_file.with_name(base_name)
    suffix = 1
    while target.exists():
        target = env_file.with_name(f"{base_name}-{suffix}")
        suffix += 1
    try:
        env_file.rename(target)
        return target
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning(
            "Failed to rename legacy project env file %s: %s",
            sanitize_log(str(env_file)),
            sanitize_log(str(e)),
        )
        return None


def _decode_settings(raw: str, project_path: str | Path) -> dict[str, str]:
    """Unseal and JSON-decode a stored settings column.

    A row that can't be decrypted or parsed raises: treating it as empty would
    launch tasks without their tokens and let the next save overwrite a
    recoverable row (wrong or rotated key, KMS outage).
    """
    unsealed = secret_field.unseal(raw)
    if not unsealed:
        raise _unavailable("stored settings cannot be decrypted", project_path)
    try:
        parsed = json.loads(unsealed)
    except json.JSONDecodeError as e:
        raise _unavailable("stored settings are not valid JSON", project_path) from e
    if not isinstance(parsed, dict):
        raise _unavailable("stored settings are not a JSON object", project_path)
    return {
        str(k): str(v)
        for k, v in parsed.items()
        if isinstance(k, str) and isinstance(v, str) and is_store_key(k)
    }


def _resolve_session_factory(session_factory: Any = None) -> Any:
    if session_factory is not None:
        return session_factory
    if _SESSION_FACTORY is not None:
        return _SESSION_FACTORY
    from server.database.engine import async_session_factory  # noqa: PLC0415

    return async_session_factory


def _warn_reappeared(env_file: Path) -> None:
    try:
        accepted, skipped = _parse_env_file(env_file)
        names = ", ".join(sorted(set(accepted) | set(skipped))) or "<none>"
    except ProjectSettingsUnavailableError:
        names = "<unreadable>"
    logger.warning(
        "Ignoring %s because project settings are already in the store (keys: %s)",
        sanitize_log(str(env_file)),
        sanitize_log(names),
    )


async def _reseal_if_unchanged(session: Any, key: str, raw: str, resealed: str) -> None:
    # Conditional: a save committed since our read must not be overwritten.
    await session.execute(
        update(ProjectSettings)
        .where(ProjectSettings.project_key == key, ProjectSettings.settings == raw)
        .values(settings=resealed)
    )
    await session.commit()


class ProjectSettingsStore:
    """Async store for per-project settings with lazy one-time `.env` import."""

    def __init__(self, session_factory: Any = None) -> None:
        self._session_factory = _resolve_session_factory(session_factory)

    async def get_settings(self, project_path: str | Path) -> dict[str, str]:
        p_path = Path(project_path)
        key = project_key(p_path)
        env_file = p_path / ".aifactory" / ".env"

        async with self._session_factory() as session:
            row = await session.get(ProjectSettings, key)
            if row is not None:
                if env_file.exists() and key not in _WARNED_REAPPEARED:
                    _WARNED_REAPPEARED.add(key)
                    _warn_reappeared(env_file)
                raw = row.settings
                decoded = _decode_settings(raw, p_path)
                if not secret_field.is_sealed(raw):
                    # Written before a KMS key existed: seal it now if we can.
                    resealed = secret_field.seal(raw)
                    if resealed and secret_field.is_sealed(resealed):
                        await _reseal_if_unchanged(session, key, raw, resealed)
                _SNAPSHOT[key] = dict(decoded)
                return decoded

            imported: dict[str, str] = {}
            should_rename = False
            if env_file.exists():
                if await asyncio.to_thread(_is_tracked, p_path):
                    _WARNED_REAPPEARED.add(key)
                    logger.warning(
                        "Refusing to import git-tracked %s; storing empty project settings",
                        sanitize_log(str(env_file)),
                    )
                else:
                    imported, skipped = _parse_env_file(env_file)
                    if skipped:
                        logger.warning(
                            "Skipped disallowed keys in %s: %s",
                            sanitize_log(str(env_file)),
                            sanitize_log(", ".join(skipped)),
                        )
                    should_rename = True

            sealed = secret_field.seal(json.dumps(imported)) or "{}"
            session.add(ProjectSettings(project_key=key, settings=sealed))
            try:
                await session.commit()
            except IntegrityError:
                # Another replica imported first: its row wins, it renames.
                await session.rollback()
                existing = await session.get(ProjectSettings, key)
                if existing is None:
                    raise _unavailable(
                        "concurrent import left no row", p_path
                    ) from None
                decoded = _decode_settings(existing.settings, p_path)
                _SNAPSHOT[key] = dict(decoded)
                return decoded

        if should_rename:
            _rename_legacy(env_file)

        _SNAPSHOT[key] = dict(imported)
        return dict(imported)

    async def put_settings(
        self, project_path: str | Path, settings: dict[str, str]
    ) -> None:
        invalid = [k for k in settings if not is_store_key(k)]
        if invalid:
            raise ValueError(
                f"Disallowed project settings key(s): {', '.join(sorted(invalid))}"
            )
        clean = {str(k): str(v) for k, v in settings.items()}
        sealed = secret_field.seal(json.dumps(clean)) or "{}"
        key = project_key(project_path)

        async with self._session_factory() as session:
            row = await session.get(ProjectSettings, key)
            if row is None:
                session.add(ProjectSettings(project_key=key, settings=sealed))
            else:
                row.settings = sealed
            await session.commit()

        _SNAPSHOT[key] = dict(clean)

    async def refresh_all(self) -> None:
        try:
            from server.project_registry import load_projects  # noqa: PLC0415

            projects = load_projects()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to load project registry during settings refresh: %s",
                sanitize_log(str(e)),
            )
            return

        for pid, meta in projects.items():
            if not isinstance(meta, dict) or not meta.get("path"):
                continue
            try:
                await self.get_settings(Path(meta["path"]))
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Failed to refresh project settings for %s: %s",
                    sanitize_log(str(pid)),
                    sanitize_log(str(e)),
                )

    async def load_project_env(
        self, project_path: str | Path, env: dict[str, str]
    ) -> None:
        settings = await self.get_settings(project_path)
        for k, v in settings.items():
            if is_project_env_key(k) and k not in env:
                env[k] = v

    async def mcp_spawn_env(self, project_path: str | Path) -> dict[str, str]:
        settings = await self.get_settings(project_path)
        mcp = {k: v for k, v in settings.items() if _is_mcp_key(k)}
        return {ENV: json.dumps(mcp)}


def is_loaded(project_path: str | Path) -> bool:
    """True once this process has read or written the project's settings."""
    return project_key(project_path) in _SNAPSHOT


def cached_setting(project_path: str | Path, key: str) -> str | None:
    """Return ``key`` for ``project_path`` from the in-process snapshot, or ``None``."""
    proj = _SNAPSHOT.get(project_key(project_path))
    if proj is None:
        return None
    return proj.get(key)


async def get_settings(
    project_path: str | Path, session_factory: Any = None
) -> dict[str, str]:
    """Read (or lazily import) the stored settings for ``project_path``."""
    return await ProjectSettingsStore(session_factory).get_settings(project_path)


async def put_settings(
    project_path: str | Path,
    settings: dict[str, str],
    session_factory: Any = None,
) -> None:
    """Replace the stored settings for ``project_path``."""
    await ProjectSettingsStore(session_factory).put_settings(project_path, settings)


async def refresh_all(session_factory: Any = None) -> None:
    """Refresh the in-process snapshot for every project in the registry."""
    await ProjectSettingsStore(session_factory).refresh_all()


async def _settings_for_spawn(
    project_path: str | Path, session_factory: Any = None
) -> dict[str, str]:
    # Starting with no settings would silently drop tokens, branch and flags;
    # never fall back to the file either.
    try:
        return await get_settings(project_path, session_factory)
    except ProjectSettingsUnavailableError:
        raise
    except Exception as e:
        logger.error(
            "project settings unavailable for %s: %s",
            sanitize_log(str(project_path)),
            sanitize_log(type(e).__name__),
        )
        raise ProjectSettingsUnavailableError(
            "project settings unavailable: database error"
        ) from e


async def load_project_env(
    project_path: str | Path,
    env: dict[str, str],
    session_factory: Any = None,
) -> None:
    """Copy stored keys passing ``is_project_env_key`` into ``env`` without overriding."""
    settings = await _settings_for_spawn(project_path, session_factory)
    for k, v in settings.items():
        if is_project_env_key(k) and k not in env:
            env[k] = v


async def mcp_spawn_env(
    project_path: str | Path, session_factory: Any = None
) -> dict[str, str]:
    """Return ``{ENV: json.dumps(mcp_keys)}`` for ``project_path``."""
    settings = await _settings_for_spawn(project_path, session_factory)
    mcp = {k: v for k, v in settings.items() if _is_mcp_key(k)}
    return {ENV: json.dumps(mcp)}
