"""Guard: the image configures a git credential helper for github.com pushes.

`gh` is authenticated in the pod from GITHUB_TOKEN, but git cannot see gh's
credential on its own. Without a helper, every `git push` to the https origin
has no username to send and dies non-interactively with "could not read
Username for 'https://github.com': No such device or address" — which surfaces
as an HTTP 409 on /worktree/create-pr *after* a fully successful build, so the
work is done and the branch never leaves the pod.

This asserts the Dockerfile rather than a call site on purpose: AIFactory pushes
from several places (pr_endgame, routes/pr, completion_orchestration) and the
whole point of configuring it globally is that a call site added later inherits
the working behaviour instead of the broken one.

The pod's filesystem is read-only at runtime, so this cannot be repaired live —
it has to be baked at build time or it is not there at all.
"""

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def _dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_dockerfile_configures_github_credential_helper() -> None:
    text = _dockerfile_text()
    assert 'credential."https://github.com".helper' in text, (
        "Dockerfile must configure a credential helper for https://github.com; "
        "without it `git push` fails non-interactively and create-pr 409s."
    )
    assert "!gh auth git-credential" in text, (
        "The helper must delegate to `gh auth git-credential` so the token is "
        "read from gh's own environment and never stored in git config or argv."
    )


def test_credential_helper_is_configured_globally_not_per_repo() -> None:
    """`--global`, so every push site inherits it — including future ones."""
    text = _dockerfile_text()
    line = next(
        (
            ln
            for ln in text.splitlines()
            if 'credential."https://github.com".helper' in ln
        ),
        None,
    )
    assert line is not None, "credential helper line not found in Dockerfile"
    assert "--global" in line, (
        f"credential helper must be --global, got: {line.strip()}"
    )


def test_no_token_is_baked_into_git_config() -> None:
    """The helper must shell out to gh, never embed a literal token."""
    text = _dockerfile_text()
    for line in text.splitlines():
        if 'credential."https://github.com".helper' not in line:
            continue
        for marker in (
            "ghp_",
            "github_pat_",
            "gho_",
            "$GITHUB_TOKEN",
            "${GITHUB_TOKEN}",
        ):
            assert marker not in line, (
                f"credential helper line must not carry a token ({marker}); "
                "it is world-readable in the image layer"
            )
