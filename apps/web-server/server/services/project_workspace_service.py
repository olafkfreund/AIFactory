"""Portal-managed project workspaces (#82 PR-A).

When AIFactory runs on a developer laptop, the user's git repo lives on
the same filesystem as the portal and the existing ``POST /api/projects
{path}`` route just registers that directory. That model breaks for
every other deployment shape:

- **Single-user VPS** — repo is on the laptop, portal on the VPS, no
  shared filesystem.
- **Kubernetes** — portal pod has no view into the user's machine.
- **Shared/SaaS** — the path concept doesn't even map.

This service backs the alternative path: the portal accepts a Git URL
and clones it into a local workspace directory. The workspace root is
configurable via ``PROJECT_WORKSPACE_ROOT`` (defaults to
``~/.aifactory/workspaces/`` on laptop installs, expected to be a
mounted PVC in K8s installs). The returned path is what the rest of
AIFactory (Auto-Fix, agent_service, etc.) uses as the project's
on-disk root — they don't need to know whether the project was added
via path or URL.

Auth in PR-A is whatever the host's git config already provides —
i.e. public HTTPS URLs and SSH keys configured in ``~/.ssh/``. Stored
git credentials (Deploy Keys, GitHub App install IDs, PATs) land in
PR-C.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

from factory_common.logsafe import sanitize_log

from server.error_ref import InputRejectedError
from server.specpath import safe_spec_component

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_ROOT = Path.home() / ".aifactory" / "workspaces"

# Default git operation timeout — long enough for a fresh clone of a
# medium-sized repo over a slow link, short enough that a hung remote
# doesn't lock up the portal forever.
DEFAULT_GIT_TIMEOUT_SECONDS = 600


def workspace_root() -> Path:
    """Resolve the directory under which all portal-managed clones live.

    Looks at ``PROJECT_WORKSPACE_ROOT`` env first (the K8s/SaaS path),
    falls back to ``~/.aifactory/workspaces/`` (laptop path).
    """
    env = os.environ.get("PROJECT_WORKSPACE_ROOT")
    if env:
        return Path(env).expanduser()
    return DEFAULT_WORKSPACE_ROOT


def slug_from_git_url(git_url: str) -> str:
    """Turn a git URL into a filesystem-safe directory slug.

    ``git@github.com:olaf/AIFactory.git`` → ``olaf-AIFactory``
    ``https://github.com/olaf/AIFactory.git`` → ``olaf-AIFactory``
    ``https://gitlab.com/group/sub/repo`` → ``group-sub-repo``

    The slug is used as the directory name under ``workspace_root()``, so it
    is a path COMPONENT built from request input and goes through the same
    barrier every other such component in this server uses. It rejects rather
    than rewrites (#1313): a gitUrl whose path is ``..`` is either an attack or
    a typo, and quietly cloning it into a directory the caller never named
    registers a project under a name they did not ask for.

    Raises:
        InputRejectedError: the URL path yields no usable directory name.
    """
    # SCP-style ("git@host:owner/repo.git") — split on the colon, drop the host.
    if git_url.startswith("git@") and ":" in git_url:
        _, path = git_url.split(":", 1)
    else:
        parsed = urlparse(git_url)
        path = parsed.path.lstrip("/")
    # Strip .git suffix + lowercase + replace path separators with hyphens.
    if path.endswith(".git"):
        path = path[:-4]
    # Replace any non-alnum/hyphen char with hyphen; collapse repeats.
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", path).strip("-") or "workspace"
    # `.` is inside the character class above, so `..` survives the
    # substitution intact and `workspace_root() / slug` climbs OUT of the
    # workspace root. `safe_spec_component` is the repo's existing barrier for
    # exactly this ("is this string safe to join onto a trusted root") and
    # already treats "." and ".." as reserved — reused rather than re-derived.
    try:
        return safe_spec_component(slug, "gitUrl")
    except ValueError:
        # `from None`: the ValueError quotes the rejected value, and the
        # message below is the one the caller sees.
        raise InputRejectedError(
            "Invalid gitUrl: its path is not a usable workspace directory name"
        ) from None


def _inject_credential(git_url: str, username: str) -> str:
    """Rewrite an HTTPS git URL to carry the *username only* (#82 PR-C).

    ``https://github.com/owner/repo.git`` →
    ``https://oauth2@github.com/owner/repo.git``

    The token is deliberately NOT embedded (AIFactory#1362, converging on
    TFactory's fork; PFactory#602). A URL handed to ``git`` becomes an argv
    element, and argv is world-readable via ``/proc/<pid>/cmdline`` for the
    lifetime of the child. AIFactory#1356 stopped that argv reaching the log
    files; it could not take the credential out of argv itself. This does:
    the password is supplied out-of-band via ``GIT_ASKPASS`` (see
    :func:`_git_askpass_env`), which git asks for because this URL carries a
    username and no password.

    SSH URLs (``git@host:...``) are returned unchanged — they auth via
    keys, not URLs; stored Deploy Keys are a separate path (out of
    scope for V1 of PR-C).
    """
    if not git_url.startswith("https://"):
        return git_url
    rest = git_url[len("https://") :]
    return f"https://{username}@{rest}"


# Tiny POSIX askpass helper. git invokes it as ``<script> "<prompt>"`` and
# reads the answer from stdout. We branch on the prompt: git asks for the
# username first ("Username for '...'"), then the password. Both values come
# from the environment (``GIT_USER`` / ``GIT_PASS``) — never argv — so the
# token never appears in any process command line.
_GIT_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  Username*) printf '%s' "$GIT_USER" ;;
  *)         printf '%s' "$GIT_PASS" ;;
esac
"""


@contextlib.contextmanager
def _git_askpass_env(username: str, token: str) -> Iterator[dict[str, str]]:
    """Yield env vars that feed a git credential via ``GIT_ASKPASS``.

    Writes the askpass helper to a ``0700`` temp file and points
    ``GIT_ASKPASS`` at it. The token travels in ``GIT_PASS`` (read by the
    script), so it never lands in argv or in git's persisted config.
    ``/proc/<pid>/environ`` is owner-only; ``/proc/<pid>/cmdline`` is
    world-readable -- that asymmetry is the whole point of the move. The
    script is removed when the context exits.
    """
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly below
        mode="w", prefix="git-askpass-", suffix=".sh", delete=False
    )
    try:
        handle.write(_GIT_ASKPASS_SCRIPT)
        handle.close()
        Path(handle.name).chmod(stat.S_IRWXU)  # 0700 — owner-only rwx
        yield {
            "GIT_ASKPASS": handle.name,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_USER": username,
            "GIT_PASS": token,
        }
    finally:
        with contextlib.suppress(OSError):
            Path(handle.name).unlink()


async def _restore_sanitized_origin_best_effort(
    *, git_url: str, workspace: Path, timeout_seconds: float
) -> None:
    """Best-effort strip of the injected credential USERNAME, for a failure
    path where another exception is already in flight. Logs but never raises
    -- the caller needs to see the ORIGINAL failure, not a cleanup error.

    Since #1362 the token is never in this URL (it rides in ``GIT_PASS``), so
    what is left behind on a failure is a username, not a secret.
    """
    try:
        await _run_git(
            ["remote", "set-url", "origin", git_url],
            cwd=workspace,
            timeout=timeout_seconds,
        )
    except GitOperationError as exc:
        logger.error(
            "[workspace] failed to restore sanitized origin URL for "
            "%s after a failed pull; the username-bearing URL may still "
            "be persisted in .git/config: %s",
            sanitize_log(workspace),
            sanitize_log(exc),
        )


async def _strip_credential_or_raise(
    *, git_url: str, workspace: Path, timeout_seconds: float, after: str
) -> None:
    """Strip the injected credential USERNAME from origin after a SUCCESSFUL
    git op.

    Kept fail-closed rather than softened when #1362 took the TOKEN out of the
    URL: what lingers now is a username, not a secret, but a workspace whose
    origin still points at the credentialed form is not the workspace the
    caller asked for, and this path only fires when ``git remote set-url``
    itself fails -- which is a real problem worth surfacing either way.
    Self-heals on retry -- see the callers' docs for why.
    """
    try:
        await _run_git(
            ["remote", "set-url", "origin", git_url],
            cwd=workspace,
            timeout=timeout_seconds,
        )
    except GitOperationError as exc:
        logger.error(
            "[workspace] credential left in %s/.git/config: failed to "
            "strip credential from origin after %s: %s",
            sanitize_log(workspace),
            sanitize_log(after),
            sanitize_log(exc),
        )
        raise GitOperationError(
            f"{after} {workspace} but could not strip the credential from "
            "origin afterwards -- refusing to hand back a workspace with "
            "a credential persisted in .git/config"
        ) from exc


async def clone_or_update(
    git_url: str,
    branch: str | None = None,
    slug: str | None = None,
    *,
    root: Path | None = None,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
    credential: tuple[str, str] | None = None,
) -> Path:
    """Clone the repo into the workspace root, or fast-forward an existing clone.

    Args:
        git_url: HTTPS or SSH URL to the repository.
        branch: Optional branch to checkout after clone. ``None`` uses
            the remote's HEAD.
        slug: Optional override for the workspace directory name.
            Defaults to ``slug_from_git_url(git_url)``.
        root: Optional override for the workspace root. Defaults to
            ``workspace_root()`` (PROJECT_WORKSPACE_ROOT env or
            ``~/.aifactory/workspaces/``).
        timeout_seconds: Per-operation timeout.
        credential: Optional ``(username, token)`` tuple. When provided
            and ``git_url`` is HTTPS, the credential is injected into
            the URL for the network operation only — never persisted to
            git's config (the workspace dir gets a sanitized origin
            via ``git remote set-url`` after the fetch). Use this with
            credentials from the ``git_credentials`` table (#82 PR-C).

    Returns:
        Absolute path to the local clone.

    Raises:
        GitOperationError: On any non-zero ``git`` exit code or timeout.
    """
    workspace = (root or workspace_root()) / (slug or slug_from_git_url(git_url))
    workspace.parent.mkdir(parents=True, exist_ok=True)

    # Build the URL that actually gets passed to ``git`` for network ops, plus
    # the credential env. The token is NEVER embedded in the URL/argv
    # (AIFactory#1362): the URL carries only the username and the password is
    # fed via GIT_ASKPASS, so it can't be read from ``/proc/<pid>/cmdline``.
    # Note: ``credential`` is the secret material — never log it.
    fetch_url = git_url
    askpass_ctx: contextlib.AbstractContextManager[dict[str, str]]
    if credential is not None:
        username, token = credential
        fetch_url = _inject_credential(git_url, username)
        askpass_ctx = _git_askpass_env(username, token)
    else:
        askpass_ctx = contextlib.nullcontext({})

    with askpass_ctx as cred_env:
        if (workspace / ".git").is_dir():
            # Existing clone — fetch + reset/fast-forward.
            # For credentialed pulls, point origin at the username-only URL
            # FOR THIS OPERATION ONLY, then restore the bare origin so not
            # even the username ends up in ``.git/config``. The token is not
            # in this URL at all -- it reaches git via ``cred_env`` below.
            if credential is not None:
                await _run_git(
                    ["remote", "set-url", "origin", fetch_url],
                    cwd=workspace,
                    timeout=timeout_seconds,
                )
            try:
                await _run_git(
                    ["fetch", "--prune", "origin"],
                    cwd=workspace,
                    timeout=timeout_seconds,
                    extra_env=cred_env,
                )
                if branch:
                    await _run_git(
                        ["checkout", branch],
                        cwd=workspace,
                        timeout=timeout_seconds,
                    )
                try:
                    await _run_git(
                        ["pull", "--ff-only"],
                        cwd=workspace,
                        timeout=timeout_seconds,
                        extra_env=cred_env,
                    )
                except GitOperationError:
                    # The managed mirror diverged or carries local/untracked
                    # changes (e.g. a consumer wrote build artifacts into it),
                    # so a fast-forward pull aborts. This clone is a disposable
                    # mirror of origin — hard-reset to the fetched remote tip and
                    # drop untracked cruft rather than failing the whole operation
                    # with a 400. (Honours the documented "reset/fast-forward".)
                    target = f"origin/{branch}" if branch else "FETCH_HEAD"
                    await _run_git(
                        ["reset", "--hard", target],
                        cwd=workspace,
                        timeout=timeout_seconds,
                    )
                    await _run_git(
                        ["clean", "-fd"],
                        cwd=workspace,
                        timeout=timeout_seconds,
                    )
                    logger.warning(
                        "[workspace] ff-only pull failed for %s; hard-reset to %s "
                        "and cleaned untracked files",
                        sanitize_log(workspace),
                        sanitize_log(target),
                    )
            except Exception:
                # The fetch/checkout/pull sequence itself failed. Best-effort
                # restore the sanitized origin so the credential doesn't linger
                # any longer than it has to, but don't mask the real failure
                # behind a cleanup error -- there's already an exception in
                # flight and the caller needs to see *that* one.
                if credential is not None:
                    await _restore_sanitized_origin_best_effort(
                        git_url=git_url,
                        workspace=workspace,
                        timeout_seconds=timeout_seconds,
                    )
                raise
            else:
                if credential is not None:
                    # SELF-HEALING, not just loud: this is the pull path, and
                    # the top of this branch (`if credential is not None`)
                    # unconditionally re-injects the credentialed `fetch_url`
                    # into origin on every call regardless of what's there now.
                    # So the NEXT call for this workspace re-attempts this same
                    # strip -- the credential doesn't sit behind a green result
                    # forever, it keeps failing loud until a strip finally
                    # succeeds. A fresh clone that fails here also self-heals:
                    # the workspace now has a `.git` dir, so the next call
                    # takes THIS pull path rather than re-cloning.
                    await _strip_credential_or_raise(
                        git_url=git_url,
                        workspace=workspace,
                        timeout_seconds=timeout_seconds,
                        after="pulled",
                    )
            logger.info("[workspace] pulled latest into %s", sanitize_log(workspace))
            return workspace

        # Fresh clone. The `--` separates options from positional args so a
        # `fetch_url`/branch beginning with '-' can't be parsed as a git flag
        # (#323 C5 defense-in-depth; the route validates these too).
        cmd = ["clone"]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend(["--", fetch_url, str(workspace)])
        await _run_git(
            cmd, cwd=workspace.parent, timeout=timeout_seconds, extra_env=cred_env
        )
        if credential is not None:
            # Strip the credential from origin so it isn't persisted in the
            # workspace's ``.git/config``. The clone already succeeded, so a
            # failure here must fail closed -- logging and returning "success"
            # would hand back a workspace with a live credential sitting on
            # disk indefinitely. Self-heals too: the workspace now has a
            # ``.git`` dir, so a retry takes the pull path above, which
            # re-attempts this same strip.
            await _strip_credential_or_raise(
                git_url=git_url,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                after="cloned",
            )
        logger.info(
            "[workspace] cloned %s → %s", sanitize_log(git_url), sanitize_log(workspace)
        )
        return workspace


class GitOperationError(RuntimeError):
    """Raised when a git operation fails or times out."""


#: Every git subcommand this module invokes. ``args[0]`` is a hard-coded
#: literal at every ``_run_git`` call site, but the value that reaches a log
#: line and an exception message must be provably one of these rather than
#: "element 0 of a list that also carries the credentialed fetch URL" -- which
#: is all the code can otherwise say about it.
_GIT_SUBCOMMANDS = ("clone", "fetch", "checkout", "pull", "remote")


def _safe_subcommand(args: list[str]) -> str:
    """Return ``args[0]`` as one of :data:`_GIT_SUBCOMMANDS`, else ``"unknown"``.

    Returns the matching *constant*, not the caller's string, so nothing
    derived from ``args`` -- which carries a PAT-bearing URL on a credentialed
    call (see :func:`_inject_credential`) -- can reach a log sink or an
    exception message through it.
    """
    head = args[0] if args else ""
    for known in _GIT_SUBCOMMANDS:
        if head == known:
            return known
    return "unknown"


async def _run_git(
    args: list[str],
    *,
    cwd: Path,
    timeout: float,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Run ``git <args>`` with a timeout. Returns stdout on success.

    ``extra_env`` is merged on top of the base environment. It is how the
    ``GIT_ASKPASS`` credential vars reach a network operation (AIFactory#1362,
    converging on TFactory's fork) without the token ever being on the command
    line -- and so out of ``/proc/<pid>/cmdline``, which #1356 could not
    address because it only stopped the argv reaching the LOG.
    """
    cmd = ["git", *args]
    # The argv is NEVER logged or interpolated into an error. `_inject_credential`
    # embeds the PAT in the fetch URL, which becomes an argv element, so
    # `" ".join(args)` wrote `https://oauth2:<PAT>@host/...` straight to a DEBUG
    # line -- and this fleet forwards application logs off-host. Driving the real
    # pipeline put the token on three lines across `server.log` and `errors.log`
    # (the DEBUG line here, plus both copies `error_reference` makes of the
    # GitOperationError message below). The subcommand and cwd identify the
    # operation without it.
    subcommand = _safe_subcommand(args)
    logger.debug("[workspace] running: git %s (cwd=%s)", subcommand, sanitize_log(cwd))
    # Restrict git transports to https/ssh/git (#323 C5): blocks the `ext::`
    # transport helper (arbitrary command execution) even if a malicious URL
    # slips past the route validator.
    env = {**os.environ, "GIT_ALLOW_PROTOCOL": "https:ssh:git"}
    if extra_env:
        env.update(extra_env)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as e:
        raise GitOperationError(f"git executable not found on PATH: {e}") from e

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        # ponytail: the process may have already exited between the timeout
        # firing and kill() running -- nothing to do either way
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise GitOperationError(f"git {subcommand} timed out after {timeout}s") from e

    if proc.returncode != 0:
        # stderr is kept: `client_error` hands the caller a reference id, not
        # this text. It no longer rests on git redacting URL userinfo from the
        # errors it composes either -- since #1362 the credential is not in the
        # URL or the argv at all, so there is nothing for git, or for a hostile
        # remote's verbatim `remote:` lines, to echo back.
        raise GitOperationError(
            f"git {subcommand} failed (exit {proc.returncode}): "
            f"{stderr.decode('utf-8', 'replace').strip() or 'no stderr'}"
        )
    return stdout.decode("utf-8", "replace")
