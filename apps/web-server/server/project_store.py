"""The project registry's DB store (#2157, Factory#3857 phase 1).

``project_registry.load_projects()/save_projects()`` keep their dict shape, but
the rows now live in the ``projects`` table, the only store. ``projects.json``
is read exactly once, by :func:`import_projects_json`, and written only by
:func:`export_projects_json` (the rollback path).

The engine is async-only and the ~40 callers are sync (some inside a running
loop), so this module owns a daemon thread with its own event loop and its own
small engine; sync calls block on a future, async callers use the ``a*`` twins.
Always the same loop, so asyncpg connections are never shared across loops.

Row mapping: ``slug``, ``repo_identity``, ``team_id``, ``org_id`` are columns;
everything else in a legacy entry (settings, tokens, queue, unknown keys) is
kept whole as JSON in ``projects.settings_json`` so import/export is lossless.

``save_projects`` is upsert-only and writes only entries that changed since
:func:`load_projects` returned them, so a handler saving a stale copy of a
project it did not touch cannot overwrite a concurrent update. Removal is the
explicit :func:`archive_project`.

``python -m server.project_store export`` and ``import [--overwrite]`` are the
rollback / roll-forward commands for a stopped service.
"""

# ruff: noqa: T201 - the rollback CLI prints its result
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import subprocess
import threading
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.config import get_settings
from server.database import engine as _db_engine
from server.database.models import Base, Organization, Project, Team, User
from server.db_pool import pool_kwargs
from server.paths import atomic_write_secret_json
from server.project_identity import (
    derive_slug,
    normalize_repo_identity,
    unique_slug,
    validate_slug,
)
from server.tenancy import DEFAULT_ORG_ID
from server.utils.subprocess_env import child_env

logger = logging.getLogger(__name__)

T = TypeVar("T")

_IDENTITY_KEYS = ("slug", "team_id", "repo_identity", "org_id")
_CALL_TIMEOUT = 10  # seconds a sync caller waits for the registry thread
_IMPORTED_SUFFIX = ".imported"
_ATTEMPTS = 3  # commit retries when a concurrent create takes a derived slug


class SlugConflictError(ValueError):
    """The requested slug is taken in the org, or is not a valid slug."""

    def __init__(self, message: str, *, invalid: bool = False) -> None:
        super().__init__(message)
        self.invalid = invalid


# --------------------------------------------------------------------------
# Configuration (tests point the store at a temp DB / data dir via configure()
# or the AIFACTORY_REGISTRY_URL / _DATA_DIR / _CREATE_SCHEMA environment)
# --------------------------------------------------------------------------

_cfg: dict[str, Any] = {"url": None, "create_schema": False, "data_dir": None}


def configure(
    url: str | None = None,
    *,
    create_schema: bool = False,
    data_dir: str | Path | None = None,
) -> None:
    """Point the store at ``url`` / ``data_dir`` (default: the app's own)."""
    reset()
    _cfg.update(url=url, create_schema=create_schema, data_dir=data_dir)


def _url() -> str:
    return (
        _cfg["url"]
        or os.environ.get("AIFACTORY_REGISTRY_URL")
        or _db_engine.DATABASE_URL
    )


def _create_schema() -> bool:
    # Tests only: production schema comes from alembic.
    return bool(
        _cfg["create_schema"] or os.environ.get("AIFACTORY_REGISTRY_CREATE_SCHEMA")
    )


def _projects_file() -> Path:
    base = (
        _cfg["data_dir"]
        or os.environ.get("AIFACTORY_REGISTRY_DATA_DIR")
        or get_settings().PROJECTS_DATA_DIR
    )
    return Path(base) / "projects.json"


# --------------------------------------------------------------------------
# The registry thread
# --------------------------------------------------------------------------

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_engine: Any = None
_sessions: async_sessionmaker[AsyncSession] | None = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop  # noqa: PLW0603
    with _lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=loop.run_forever, name="project-registry", daemon=True
            ).start()
            _loop = loop
        return _loop


def _submit(coro: Awaitable[T]) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, _ensure_loop())  # type: ignore[arg-type]


def _run(coro: Awaitable[T], timeout: float = _CALL_TIMEOUT) -> T:
    """Run ``coro`` on the registry loop and wait (sync callers).

    On timeout the work is cancelled (its transaction rolls back), so a caller
    that was told "failed" never has the write land afterwards.
    """
    fut = _submit(coro)
    try:
        result: T = fut.result(timeout=timeout)
        return result
    except TimeoutError:
        fut.cancel()
        raise


async def _arun(coro: Awaitable[T]) -> T:
    """Same, without blocking the caller's event loop."""
    return await asyncio.wrap_future(_submit(coro))


async def _get_sessions() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessions
    if _sessions is not None:
        return _sessions
    url = _url()
    kwargs = {} if url.startswith("sqlite") else pool_kwargs(url, "DBREG", 2, 2)
    eng = create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        **kwargs,
    )
    if eng.dialect.name == "sqlite":
        event.listen(eng.sync_engine, "connect", _db_engine._enable_wal_mode)
    if _create_schema():
        tables = [t.__table__ for t in (Organization, User, Team, Project)]
        async with eng.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(c, tables=tables)  # type: ignore[arg-type]
            )
    _engine, _sessions = eng, async_sessionmaker(eng, expire_on_commit=False)
    return _sessions


async def _dispose() -> None:
    global _engine, _sessions
    eng, _engine, _sessions = _engine, None, None
    if eng is not None:
        await eng.dispose()


def reset() -> None:
    """Drop the engine (tests, reconfiguration); the next call rebuilds it."""
    if _loop is not None and _engine is not None:
        with contextlib.suppress(Exception):
            _run(_dispose())


# --------------------------------------------------------------------------
# Row <-> dict
# --------------------------------------------------------------------------


def _dump(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True, default=str)


def _to_dict(row: Project) -> dict[str, Any]:
    entry: dict[str, Any] = json.loads(row.settings_json) if row.settings_json else {}
    entry.setdefault("name", row.name)
    entry.setdefault("path", row.path)
    entry.update(
        org_id=row.org_id,
        slug=row.slug,
        team_id=row.team_id,
        repo_identity=row.repo_identity,
    )
    return entry


class _Projects(dict[str, dict[str, Any]]):
    """``load_projects()`` result: remembers what it loaded so saves can diff."""

    _snap: dict[str, str]


def _wrap(rows: list[Project]) -> _Projects:
    out = _Projects()
    out._snap = {}
    for row in rows:
        out[row.id] = _to_dict(row)
        out._snap[row.id] = _dump(out[row.id])
    return out


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


async def _ensure_org(s: AsyncSession, org_id: str) -> None:
    """Mirror ``seed_tenant_defaults`` so a project never fails the org FK."""
    if await s.get(Organization, org_id) is not None:
        return
    if await s.get(User, _db_engine.DEFAULT_USER_ID) is None:
        s.add(
            User(
                id=_db_engine.DEFAULT_USER_ID,
                email="default@localhost",
                name="Default User",
                password_hash="disabled",  # noqa: S106 - not a credential, login disabled
                role="user",
                is_active=False,
            )
        )
        await s.flush()
    slug = org_id
    if (
        await s.execute(select(Organization.id).where(Organization.slug == slug))
    ).first():
        slug = f"{org_id}-{uuid.uuid4().hex[:6]}"
    s.add(
        Organization(
            id=org_id,
            name=org_id,
            slug=slug,
            owner_id=_db_engine.DEFAULT_USER_ID,
            plan="free",
        )
    )
    await s.flush()


async def _default_team(s: AsyncSession, org_id: str) -> str:
    team_id = (
        await s.execute(
            select(Team.id).where(Team.org_id == org_id, Team.is_default.is_(True))
        )
    ).scalar()
    if team_id:
        return team_id
    team = Team(org_id=org_id, name="Default", slug="default", is_default=True)
    s.add(team)
    await s.flush()
    return team.id


def _git_remote_identity(path: str) -> str | None:
    if not path or not Path(path).is_dir():
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", path, "remote", "get-url", "origin"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            env=child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return normalize_repo_identity(out.stdout.strip()) if out.returncode == 0 else None


async def _upsert(
    s: AsyncSession, pid: str, entry: dict[str, Any], *, reactivate: bool = False
) -> None:
    """Insert or update one project; writes identity values back into ``entry``."""
    row = await s.get(Project, pid)
    org_id = entry.get("org_id") or (row.org_id if row else None) or DEFAULT_ORG_ID
    await _ensure_org(s, org_id)

    wanted = entry.get("slug")
    if wanted is not None and (row is None or wanted != row.slug):
        if not validate_slug(wanted):
            raise SlugConflictError(f"Invalid slug {wanted!r}", invalid=True)
        taken = await s.execute(
            select(Project.id).where(
                Project.org_id == org_id, Project.slug == wanted, Project.id != pid
            )
        )
        if taken.first():
            raise SlugConflictError(
                f"Slug {wanted!r} is already used in this organization"
            )
        slug = wanted
    elif row is not None and row.slug:
        slug = row.slug
    else:
        used = set(
            (await s.execute(select(Project.slug).where(Project.org_id == org_id)))
            .scalars()
            .all()
        )
        base = derive_slug(entry.get("name") or Path(entry.get("path") or "").name)
        slug = unique_slug(base, {u for u in used if u})

    path = str(entry.get("path") or "")
    if "repo_identity" in entry:
        repo = entry["repo_identity"]
    elif row is not None:
        repo = row.repo_identity
    else:
        repo = normalize_repo_identity(entry.get("clonedFrom")) or (
            await asyncio.to_thread(_git_remote_identity, path)
        )
    team_id = entry.get("team_id") or (row.team_id if row else None)
    # an exported id may name a team that no longer exists (schema rollback)
    if not team_id or await s.get(Team, team_id) is None:
        team_id = await _default_team(s, org_id)

    entry.update(org_id=org_id, slug=slug, repo_identity=repo, team_id=team_id)
    blob = _dump({k: v for k, v in entry.items() if k not in _IDENTITY_KEYS})
    name = str(entry.get("name") or Path(path).name or pid)
    if row is None:
        row = Project(id=pid, path=path[:1024], name=name[:255])
        s.add(row)
    row.org_id, row.name, row.path = org_id, name[:255], path[:1024]
    row.slug, row.repo_identity, row.team_id = slug, repo, team_id
    row.settings_json = blob
    if reactivate:
        row.archived_at = None
    await s.flush()


async def _save(projects: dict[str, dict[str, Any]]) -> None:
    await _import_pending()
    sessions = await _get_sessions()
    snap = getattr(projects, "_snap", None)
    # identity values are written back into the entries; a retry must derive them
    # again (the rolled-back team row, a taken slug), not treat them as requested
    original = {
        pid: {k: e[k] for k in _IDENTITY_KEYS if k in e}
        for pid, e in projects.items()
        if isinstance(e, dict)
    }
    for attempt in range(_ATTEMPTS):
        try:
            async with sessions() as s, s.begin():
                for pid, entry in projects.items():
                    if not isinstance(entry, dict):
                        continue
                    if snap is not None:
                        if snap.get(pid) == _dump(entry):
                            continue
                    else:
                        row = await s.get(Project, pid)
                        if row is not None and _dump(_to_dict(row)) == _dump(entry):
                            continue
                    await _upsert(s, pid, entry)
            return
        except IntegrityError:
            # a concurrent create took the derived slug: re-derive and retry
            if attempt == _ATTEMPTS - 1:
                raise
            for pid, keys in original.items():
                entry = projects[pid]
                for k in _IDENTITY_KEYS:
                    entry.pop(k, None)
                entry.update(keys)


async def _load(include_archived: bool = False) -> list[Project]:
    await _import_pending()
    sessions = await _get_sessions()
    async with sessions() as s:
        query = select(Project)
        if not include_archived:
            query = query.where(Project.archived_at.is_(None))
        return list((await s.execute(query)).scalars().all())


# --------------------------------------------------------------------------
# Import / export (projects.json is a one-off input and a rollback output)
# --------------------------------------------------------------------------


async def _import_file(file: Path, *, overwrite: bool = False) -> int:
    try:
        data = json.loads(file.read_text())
    except (OSError, ValueError):
        logger.warning("projects.json unreadable, not imported", exc_info=True)
        return 0
    if not isinstance(data, dict):
        logger.warning("projects.json is not an object, not imported")
        return 0
    sessions = await _get_sessions()
    count = 0
    async with sessions() as s, s.begin():
        for pid, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if await s.get(Project, pid) is not None and not overwrite:
                continue
            await _upsert(s, pid, dict(entry), reactivate=overwrite)
            count += 1
        if overwrite:
            stale = (
                (await s.execute(select(Project).where(Project.archived_at.is_(None))))
                .scalars()
                .all()
            )
            for row in stale:
                if row.id not in data:
                    row.archived_at = _now()
    with contextlib.suppress(OSError):
        file.replace(file.with_name(file.name + _IMPORTED_SUFFIX))
    if count:
        logger.info("Imported %d project(s) from %s into the registry DB", count, file)
    return count


async def _import_pending() -> None:
    file = _projects_file()
    if file.exists():
        await _import_file(file)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def import_projects_json(
    path: str | Path | None = None, overwrite: bool = False
) -> int:
    """Import ``projects.json``: new ids only, unless ``overwrite`` (roll forward)."""
    # bulk command: every new project may probe its git remote
    return _run(
        _import_file(Path(path) if path else _projects_file(), overwrite=overwrite),
        timeout=600,
    )


async def aimport_pending() -> None:
    """Startup hook: import a leftover ``projects.json`` before serving."""
    await _arun(_import_pending())


def export_projects_json(path: str | Path | None = None) -> int:
    """Write the active projects to ``projects.json`` (rollback to the old release)."""
    target = Path(path) if path else _projects_file()
    projects = {pid: dict(p) for pid, p in load_projects().items()}
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_secret_json(target, projects)
    return len(projects)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def load_projects() -> dict[str, dict[str, Any]]:
    """Active projects as ``{id: entry}`` (the legacy ``projects.json`` shape)."""
    return _wrap(_run(_load()))


async def aload_projects() -> dict[str, dict[str, Any]]:
    return _wrap(await _arun(_load()))


def save_projects(projects: dict[str, dict[str, Any]]) -> None:
    """Upsert the entries changed since they were loaded. Never removes."""
    _run(_save(projects))


async def asave_projects(projects: dict[str, dict[str, Any]]) -> None:
    await _arun(_save(projects))


async def _archive(project_id: str) -> bool:
    sessions = await _get_sessions()
    async with sessions() as s, s.begin():
        row = await s.get(Project, project_id)
        if row is None or row.archived_at is not None:
            return False
        row.archived_at = _now()
        return True


def archive_project(project_id: str) -> bool:
    """Hide a project from every list and lookup. Row, id and slug stay."""
    return _run(_archive(project_id))


async def aarchive_project(project_id: str) -> bool:
    return await _arun(_archive(project_id))


def get_project(
    project_id: str, include_archived: bool = False
) -> dict[str, Any] | None:
    for row in _run(_load(include_archived=include_archived)):
        if row.id == project_id:
            return _to_dict(row)
    return None


def find_by_slug(
    slug: str, org_ids: set[str] | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """Active projects with ``slug`` (in ``org_ids`` when given; ``None`` = all orgs)."""
    return [
        (pid, p)
        for pid, p in load_projects().items()
        if p.get("slug") == slug and (org_ids is None or p.get("org_id") in org_ids)
    ]


def _main() -> None:
    parser = argparse.ArgumentParser(prog="python -m server.project_store")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export", help="write the registry to projects.json (rollback)")
    imp = sub.add_parser("import", help="import projects.json")
    imp.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.cmd == "export":
        print(f"exported {export_projects_json()} project(s) to {_projects_file()}")
    else:
        file = _projects_file()
        if not file.exists():
            # never guess a snapshot: a stale backup would archive newer projects
            raise SystemExit(f"{file} not found; nothing imported")
        print(f"imported {import_projects_json(overwrite=args.overwrite)} project(s)")


if __name__ == "__main__":
    _main()
