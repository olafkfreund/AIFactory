"""Exercise the question checkpoint against a local bare Git origin."""

import os
import subprocess
from pathlib import Path

import pytest
from agents.coder import _checkpoint_question_branch

SPEC = "001-checkpoint"
BRANCH = f"aifactory/{SPEC}"
QID = "q-0123456789ab"


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
        check=check,
    )


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    repo = tmp_path / "project"
    repo.mkdir()
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    _git(repo, "init", "-b", BRANCH)
    _git(repo, "config", "user.name", "Checkpoint Test")
    _git(repo, "config", "user.email", "checkpoint@example.invalid")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "specs").mkdir()
    (repo / "specs" / "tracked.py").write_text("original = True\n")
    (repo / "questions").mkdir()
    (repo / "questions" / "obsolete.py").touch()
    (repo / ".gitignore").write_text("ignored.py\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo, origin


@pytest.mark.parametrize("state_mode", ["untracked", "ignored", "tracked"])
def test_checkpoint_preserves_project_changes_and_pushes(
    repos: tuple[Path, Path], state_mode: str
) -> None:
    repo, origin = repos
    expected = {
        "specs/tracked.py",
        "questions/obsolete.py",
        "src/token_parser.py",
        ".github/workflows/ci.yml",
        "specs/notes.md",
        "questions/faq.md",
        "aifactory/specs/legacy.py",
    }
    state = f".aifactory/specs/{SPEC}"
    if state_mode == "ignored":
        (repo / ".gitignore").write_text(
            "ignored.py\n.aifactory/\n.aifactory-security.json\n"
        )
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-m", "ignore factory state")
    if state_mode == "tracked":
        for name in (f"{state}/.question_run_id", ".aifactory-allowlist"):
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("original factory state\n")
            _git(repo, "add", "--", name)
        _git(repo, "commit", "-m", "track factory state")
    for name in (expected - {"questions/obsolete.py"}) | {
        f"{state}/questions/{QID}.json",
        f"{state}/questions/.budget.json",
        f"{state}/.question_run_id",
        f"{state}/.question_attempt",
        ".aifactory-allowlist",
        "ignored.py",
        "nested/container.P12",
    }:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("checkpoint contents\n")
    (repo / "questions" / "obsolete.py").unlink()

    _checkpoint_question_branch(repo, SPEC, QID)

    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == (
        f"wip: paused for question {QID}"
    )
    changed = _git(repo, "diff", "--name-only", "HEAD^", "HEAD").stdout.splitlines()
    assert set(changed) == expected
    assert (
        _git(origin, "rev-parse", f"refs/heads/{BRANCH}").stdout
        == _git(repo, "rev-parse", "HEAD").stdout
    )


@pytest.mark.parametrize(
    "secret_name",
    [
        ".env",
        ".env.local",
        "id_rsa",
        ".npmrc",
        "config/credentials.json",
        "kube/config",
        "tls/server.KEY",
        "keystore.jks",
        "terraform.tfstate",
        "terraform.tfstate.backup",
        "prod.tfvars",
        ".netrc",
        "deploy/app-secret.yaml",
        ".git-credentials",
        "my.kubeconfig",
        "id_ed25519.pub",
        ".aws/credentials",
        "prod.env",
        ".envrc",
        ".docker/config.json",
        ".pgpass",
    ],
)
def test_checkpoint_skips_untracked_secret_names(
    repos: tuple[Path, Path], secret_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    repo, _ = repos
    source = {"src/token_parser.py", ".github/workflows/ci.yml"}
    body = "harmless placeholder\n"
    for name in source | {secret_name}:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    _checkpoint_question_branch(repo, SPEC, QID)

    changed = _git(repo, "diff", "--name-only", "HEAD^", "HEAD").stdout.splitlines()
    assert set(changed) == source
    assert (repo / secret_name).read_text() == body
    warnings = [
        record.message for record in caplog.records if record.name == "agents.coder"
    ]
    assert warnings == [
        f"Question checkpoint excluded 1 untracked files: {[secret_name]!r}"
    ]


def test_checkpoint_stages_tracked_secret_name_and_literal_paths(
    repos: tuple[Path, Path],
) -> None:
    repo, _ = repos
    (repo / ".env").write_text("original placeholder\n")
    _git(repo, "add", ".env")
    _git(repo, "commit", "-m", "track dotenv")
    expected = {".env", "-source.py", "space name.py", "line\nbreak.py", "[glob]*?.py"}
    for name in expected:
        (repo / name).write_text("checkpoint contents\n")

    _checkpoint_question_branch(repo, SPEC, QID)

    changed = _git(repo, "diff", "--name-only", "-z", "HEAD^", "HEAD").stdout
    assert set(changed.rstrip("\0").split("\0")) == expected


def test_checkpoint_skips_nested_repository(
    repos: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    repo, origin = repos
    nested = repo / "vendor" / "dep"
    nested.mkdir(parents=True)
    _git(nested, "init")
    (nested / "dep.py").write_text("nested = True\n")
    _git(nested, "add", "dep.py")
    _git(
        nested,
        "-c",
        "user.name=Checkpoint Test",
        "-c",
        "user.email=checkpoint@example.invalid",
        "commit",
        "-m",
        "nested source",
    )
    (repo / "source.py").write_text("source = True\n")

    _checkpoint_question_branch(repo, SPEC, QID)

    ref = f"refs/heads/{BRANCH}"
    assert (
        _git(origin, "rev-parse", ref).stdout == _git(repo, "rev-parse", "HEAD").stdout
    )
    assert _git(origin, "show", f"{ref}:source.py").stdout == "source = True\n"
    assert _git(origin, "ls-tree", ref, "--", "vendor").stdout == ""
    assert (nested / ".git").is_dir()
    assert (nested / "dep.py").is_file()
    assert (
        "Question checkpoint excluded 1 untracked files: ['vendor/dep/']"
        in caplog.messages
    )


def test_checkpoint_stages_untracked_symlink(repos: tuple[Path, Path]) -> None:
    repo, _ = repos
    target = "specs/tracked.py"
    (repo / "link.py").symlink_to(target)

    _checkpoint_question_branch(repo, SPEC, QID)

    assert _git(repo, "ls-tree", "HEAD", "--", "link.py").stdout.startswith(
        "120000 blob "
    )
    assert _git(repo, "show", "HEAD:link.py").stdout == target


def test_secret_prevents_checkpoint_and_push(repos: tuple[Path, Path]) -> None:
    repo, origin = repos
    before = _git(repo, "rev-parse", "HEAD").stdout
    (repo / "credentials.py").write_text("token = '" + "ghp_" + "A" * 36 + "'\n")
    _git(repo, "add", "credentials.py")

    with pytest.raises(RuntimeError, match="containing secrets"):
        _checkpoint_question_branch(repo, SPEC, QID)

    assert _git(repo, "rev-parse", "HEAD").stdout == before
    assert (
        _git(
            origin,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{BRANCH}",
            check=False,
        ).returncode
        == 1
    )
