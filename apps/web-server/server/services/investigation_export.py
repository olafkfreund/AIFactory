"""Export a pinned Git tree to the shared, read-only investigation source mount."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from server.services.build_backend import _ENV_REPO_PVC
from server.utils.subprocess_env import child_env, project_github_env

MAX_BYTES = 2 * 1024**3
_EXCLUDED = (
    ".env*",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_*",
    ".ssh/",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "credentials*",
    "secrets.*",
    ".aws/",
    ".kube/",
    "kubeconfig*",
    ".docker/config.json",
    "*.tfstate*",
    "*.tfvars",
    ".terraform/",
)


class ExportError(RuntimeError):
    """The source cannot safely be exported."""


def _excluded(path: PurePosixPath, patterns: tuple[str, ...]) -> bool:
    parts = path.parts
    if ".git" in parts:
        return True
    for pattern in patterns:
        if pattern.endswith("/"):
            if any(
                fnmatch.fnmatchcase("/".join(parts[i:j]), pattern[:-1])
                for i in range(len(parts))
                for j in range(i + 1, len(parts) + 1)
            ):
                return True
        elif "/" in pattern:
            if any(
                fnmatch.fnmatchcase("/".join(parts[i:]), pattern)
                for i in range(len(parts))
            ):
                return True
        elif any(fnmatch.fnmatchcase(part, pattern) for part in parts):
            return True
    return False


def export(  # noqa: PLR0912, PLR0915 - Keep archive safety checks in one guarded cleanup scope.
    repo_path: str | Path,
    sha: str,
    dest: str | Path,
    extra_patterns: str | list[str] | tuple[str, ...] = (),
) -> dict[str, int]:
    """Fetch ``sha`` and export its safe files; return their total byte count."""
    repo_path, dest = Path(repo_path), Path(dest)
    root = Path(os.environ.get("AIFACTORY_DATA_ROOT", "/home/nonroot/.aifactory"))
    if not os.environ.get(_ENV_REPO_PVC) or not os.path.ismount(root):
        raise ExportError("shared storage unavailable")
    if not dest.resolve().is_relative_to((root / "investigations").resolve()):
        raise ExportError("destination is outside investigation storage")
    if dest.exists() or dest.is_symlink():
        raise ExportError("destination already exists")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise ExportError("invalid sha")
    extra = (
        extra_patterns.splitlines()
        if isinstance(extra_patterns, str)
        else extra_patterns
    )
    extra = tuple(p.strip() for p in extra if p.strip())
    if any(p.startswith("!") for p in extra):
        raise ExportError("secret path negation is forbidden")
    patterns = (*_EXCLUDED, *extra)

    # The credential context is the existing project-repo helper; Git never sees a token in argv.
    git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo_path)]
    with project_github_env(
        repo_path, child_env(extra={"GIT_TERMINAL_PROMPT": "0"})
    ) as env:
        try:
            fetched = subprocess.run(  # noqa: S603 - Fixed git argv with validated SHA.
                [*git, "fetch", "--no-tags", "origin", sha],
                env=env,
                capture_output=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExportError("fetch failed") from exc
    if fetched.returncode:
        raise ExportError("unknown sha or fetch failed")

    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".investigation-export-", dir=dest.parent))
    process: subprocess.Popen[bytes] | None = None
    size = 0
    symlinks: list[tuple[Path, str]] = []
    try:
        process = subprocess.Popen(  # noqa: S603 - Fixed git argv with validated SHA.
            [*git, "archive", "--format=tar", sha],
            env=child_env(extra={"GIT_TERMINAL_PROMPT": "0"}),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise ExportError("archive stdout unavailable")
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if not path.parts or path.is_absolute() or ".." in path.parts:
                    raise ExportError("unsafe archive path")
                if _excluded(path, patterns):
                    continue
                target = temporary.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    size += member.size
                    if size > MAX_BYTES:
                        raise ExportError("export exceeds 2 GiB")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ExportError("unreadable archive member")
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(member.mode & 0o755)
                elif member.issym():
                    symlinks.append((target, member.linkname))
                else:
                    raise ExportError("unsupported archive member")
        process.stdout.close()
        if process.wait(timeout=300):
            raise ExportError("archive failed")
        for target, link in symlinks:
            if not link or Path(link).is_absolute():
                continue
            resolved = (target.parent / link).resolve()
            if not resolved.is_relative_to(temporary.resolve()):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
        while True:
            unsafe = [
                target
                for target, _ in symlinks
                if target.is_symlink()
                and not Path(os.path.realpath(target)).is_relative_to(
                    temporary.resolve()
                )
            ]
            if not unsafe:
                break
            for target in unsafe:
                target.unlink()
        (temporary / ".aifactory-export-ok").touch(exist_ok=False)
        temporary.rename(dest)
        return {"bytes": size}
    except (OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        raise ExportError("export failed") from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if temporary.exists():
            shutil.rmtree(temporary)
