"""Git metadata baseline snapshot and verification (#1746, #1747)."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from core.child_env import child_env
from security.git_validators import _is_dangerous_git_config

logger = logging.getLogger(__name__)

_REV_PARSE_COMMON_DIR = (
    "git",
    "rev-parse",
    "--path-format=absolute",
    "--git-common-dir",
)
_CONFIG_LIST = (
    "git",
    "config",
    "--list",
    "--show-scope",
    "-z",
    "--includes",
)
_TRACKED_SCOPES = frozenset({"local", "worktree"})
_TRACKED_PREFIXES = ("merge.", "filter.", "diff.")


class GitMetadataChangedError(RuntimeError):
    """A parallel child's metadata check failed; the build must end (#1746)."""


def _run_git(
    cwd: str | os.PathLike[str],
    argv: list[str],
    run: Callable[[list[str]], tuple[int, str]] | None,
) -> str:
    try:
        if run is not None:
            rc, out = run(argv)
        else:
            proc = subprocess.run(  # noqa: S603 - fixed argv
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                errors="surrogateescape",
                env=child_env(),
                timeout=60,
                check=False,
            )
            rc, out = proc.returncode, proc.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git failed: {exc}") from exc
    if rc != 0 or not isinstance(out, str):
        raise RuntimeError(f"git exited {rc}")
    return out


def _sha256_str(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogateescape")).hexdigest()


def _is_tracked_config_key(key: str) -> bool:
    k = key.lower()
    return (
        _is_dangerous_git_config(k)
        or k.startswith(_TRACKED_PREFIXES)
        or k == "core.attributesfile"
    )


def _parse_config(raw: str) -> dict[str, list[str]]:
    if not raw:
        return {}
    parts = raw.split("\0")
    if parts[-1] == "":
        parts.pop()
    if len(parts) % 2 != 0:
        raise RuntimeError("malformed git config --list -z output")
    grouped: dict[str, list[str]] = {}
    for i in range(0, len(parts), 2):
        scope, entry = parts[i], parts[i + 1]
        if scope not in _TRACKED_SCOPES:
            continue
        key, _, value = entry.partition("\n")
        if key and _is_tracked_config_key(key):
            grouped.setdefault(key, []).append(_sha256_str(value))
    return {k: sorted(v) for k, v in sorted(grouped.items())}


def snapshot(
    cwd: str | os.PathLike[str],
    run: Callable[[list[str]], tuple[int, str]] | None = None,
) -> dict[str, object]:
    """Capture a hash-only snapshot of repo-local git config and info/attributes."""
    raw_common = _run_git(cwd, list(_REV_PARSE_COMMON_DIR), run).strip()
    commondir = str((Path(cwd) / raw_common).resolve())
    cfg_out = _run_git(cwd, list(_CONFIG_LIST), run)
    config = _parse_config(cfg_out)
    attr_path = Path(commondir) / "info" / "attributes"
    try:
        attr_bytes = attr_path.read_bytes()
    except FileNotFoundError:
        attributes: str | None = None
    except OSError as exc:
        raise RuntimeError(f"cannot read {attr_path}: {exc}") from exc
    else:
        attributes = hashlib.sha256(attr_bytes).hexdigest()
    return {
        "repo": commondir,
        "config": config,
        "attributes": attributes,
    }


def _baseline_dir() -> Path:
    env_dir = os.environ.get("AIFACTORY_GIT_BASELINE_DIR")
    base = Path(env_dir) if env_dir else Path.home() / ".aifactory" / "git-baselines"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot create baseline dir {base}: {exc}") from exc
    return base


def _baseline_path(commondir: str) -> Path:
    return _baseline_dir() / f"{_sha256_str(commondir)[:32]}.json"


def _write_snapshot(path: Path, snap: dict[str, object]) -> None:
    payload = json.dumps(snap, sort_keys=True)
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=".baseline-", suffix=".tmp"
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            Path(tmp_name).replace(path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink()
            raise
    except OSError as exc:
        raise RuntimeError(f"cannot write baseline {path}: {exc}") from exc


def record_baseline(
    cwd: str | os.PathLike[str],
    *,
    overwrite: bool = False,
    run: Callable[[list[str]], tuple[int, str]] | None = None,
) -> None:
    """Record the git metadata baseline for ``cwd`` unless one already exists."""
    snap = snapshot(cwd, run=run)
    path = _baseline_path(str(snap["repo"]))
    if not overwrite and path.exists():
        return
    _write_snapshot(path, snap)


def _load_baseline(path: Path) -> tuple[dict[str, object], str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"unreadable git metadata baseline {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"corrupt git metadata baseline {path}")
    cfg = data.get("config")
    attrs = data.get("attributes")
    if (
        not isinstance(cfg, dict)
        or "attributes" not in data
        or (attrs is not None and not isinstance(attrs, str))
    ):
        raise RuntimeError(f"corrupt git metadata baseline {path}")
    return cfg, attrs


def check_git_metadata(
    cwd: str | os.PathLike[str],
    run: Callable[[list[str]], tuple[int, str]] | None = None,
) -> None:
    """Compare repo git metadata against its baseline; raise RuntimeError on drift."""
    current = snapshot(cwd, run=run)
    repo = str(current["repo"])
    path = _baseline_path(repo)
    if not path.exists():
        _write_snapshot(path, current)
        # Not fail closed: direct/CLI-only mode relies on it (#1832, user decision).
        msg = (
            f"git metadata baseline taken late for {repo}; "
            "changes made before now are not detected"
        )
        logger.warning("%s", msg)
        print(f"WARNING: {msg}", flush=True)  # noqa: T201 - visible in the build log
        return
    base_cfg, base_attrs = _load_baseline(path)
    cur_cfg: dict[str, object] = current["config"]  # type: ignore[assignment]
    changed = sorted(
        k for k in set(base_cfg) | set(cur_cfg) if base_cfg.get(k) != cur_cfg.get(k)
    )
    if base_attrs != current["attributes"]:
        changed.append("info/attributes")
    if changed:
        raise RuntimeError(
            f"git metadata changed since the baseline: {', '.join(changed)}; "
            "check .git/config and .git/info/attributes, then re-baseline: "
            f"python -m core.git_metadata --rebaseline {repo}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m core.git_metadata")
    parser.add_argument("--rebaseline", required=True, metavar="PATH")
    args = parser.parse_args()
    record_baseline(args.rebaseline, overwrite=True)
    print(snapshot(args.rebaseline)["repo"])  # noqa: T201 - CLI output
