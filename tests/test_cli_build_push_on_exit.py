"""#1633 C1 + Decision 5: the build Job reports its spend on EVERY exit.

``cli/main.py`` used to push ``token_usage.json`` only after ``handle_build_command``
returned, so a build that ended through ``sys.exit`` (QA failure, held migration,
pre-flight pause...) spent tokens the control plane never heard about. The push now
sits in a ``finally``; the branch/memory/gate-marker/task_logs pushes stay
success-only so a failed branch is never published.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from cli import build_commands  # noqa: E402
from core import workspace_fetch as wf  # noqa: E402

# `cli/__init__.py` re-exports the function `main`, which shadows the submodule:
# `import cli.main as m` / `from cli import main` bind the FUNCTION.
cli_main = importlib.import_module("cli.main")

_PUSHES = (
    "maybe_push_workspace_branch",
    "maybe_push_plan",
    "maybe_push_memory",
    "maybe_push_usage",
    "maybe_push_gate_marker",
    "maybe_push_task_logs",
)


def _drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan_only: bool,
    build,
) -> tuple[dict[str, list[tuple]], Path]:
    """Run ``cli.main.main`` with ``build`` standing in for the build."""
    calls: dict[str, list[tuple]] = {n: [] for n in _PUSHES}
    for name in _PUSHES:
        monkeypatch.setattr(
            wf, name, lambda *a, _n=name, **_k: calls[_n].append(a) or True
        )
    spec_dir = tmp_path / "001-x"
    spec_dir.mkdir()
    monkeypatch.setattr(wf, "maybe_unpack_workspace", lambda *_a, **_k: False)
    monkeypatch.setattr(cli_main, "setup_environment", lambda: tmp_path)
    monkeypatch.setattr(cli_main, "find_spec", lambda *_a, **_k: spec_dir)
    monkeypatch.setattr(cli_main, "handle_build_command", build)
    monkeypatch.delenv("TRACEPARENT", raising=False)
    monkeypatch.delenv("WORKSPACE_URI", raising=False)
    argv = [
        "run.py",
        "--spec",
        "001-x",
        "--project-dir",
        str(tmp_path),
        "--auto-continue",
    ]
    if plan_only:
        argv.append("--stop-after-planning")
    monkeypatch.setattr(sys, "argv", argv)
    return calls, spec_dir


def _exit_with(code: int):
    def build(**_kw):
        raise SystemExit(code)

    return build


@pytest.mark.parametrize("plan_only", [False, True], ids=["build", "plan_only"])
@pytest.mark.parametrize("code", [1, 0])
def test_exit_pushes_usage_and_keeps_code(tmp_path, monkeypatch, code, plan_only):
    calls, spec_dir = _drive(
        tmp_path, monkeypatch, plan_only=plan_only, build=_exit_with(code)
    )
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == code
    assert calls["maybe_push_usage"] == [(spec_dir, "001-x")]
    assert len(calls["maybe_push_plan"]) == (0 if plan_only else 1)
    for name in (
        "maybe_push_workspace_branch",
        "maybe_push_memory",
        "maybe_push_gate_marker",
        "maybe_push_task_logs",
    ):
        assert calls[name] == [], name


@pytest.mark.parametrize("plan_only", [False, True], ids=["build", "plan_only"])
def test_success_pushes_each_once(tmp_path, monkeypatch, plan_only):
    calls, _ = _drive(
        tmp_path, monkeypatch, plan_only=plan_only, build=lambda **_k: None
    )
    cli_main.main()
    counts = {n: len(c) for n, c in calls.items()}
    if plan_only:
        assert counts == {n: int(n == "maybe_push_usage") for n in _PUSHES}
    else:
        assert counts == dict.fromkeys(_PUSHES, 1)


def test_crash_still_pushes_usage(tmp_path, monkeypatch):
    def build(**_kw):
        raise RuntimeError("boom")

    calls, _ = _drive(tmp_path, monkeypatch, plan_only=False, build=build)
    with pytest.raises(RuntimeError, match="boom"):
        cli_main.main()
    assert len(calls["maybe_push_usage"]) == 1
    assert calls["maybe_push_workspace_branch"] == []


# ── Decision 5: the Job writes the pre-flight approval pause ──


def _preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    uri: bool,
    auto: bool,
    plan: dict | None,
) -> tuple[int | str | None, Path]:
    spec_dir = tmp_path / "001-spec"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("# Feature\n")
    plan_file = spec_dir / "implementation_plan.json"
    if plan is not None:
        plan_file.write_text(json.dumps(plan))
    monkeypatch.setenv("AIFACTORY_AUTH_PREFLIGHT", "off")
    if uri:
        monkeypatch.setenv("WORKSPACE_URI", "s3://bucket/key")
    else:
        monkeypatch.delenv("WORKSPACE_URI", raising=False)
    review_state = MagicMock()
    review_state.is_approval_valid.return_value = False
    review_state.approved = False
    review_state_cls = MagicMock()
    review_state_cls.load.return_value = review_state
    patches = [
        patch("cli.utils.validate_environment", MagicMock(return_value=True)),
        patch("cli.utils.print_banner", MagicMock()),
        patch.object(build_commands, "ReviewState", review_state_cls),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(SystemExit) as exc:
            build_commands.handle_build_command(
                project_dir=tmp_path,
                spec_dir=spec_dir,
                model="claude-sonnet-4-5",
                max_iterations=1,
                verbose=False,
                force_isolated=False,
                force_direct=True,
                auto_continue=auto,
                skip_qa=True,
                force_bypass_approval=False,
            )
    return exc.value.code, plan_file


def test_preflight_pause_plan_uri_missing_plan(tmp_path, monkeypatch):
    code, plan_file = _preflight(tmp_path, monkeypatch, uri=True, auto=True, plan=None)
    assert code == 0
    assert json.loads(plan_file.read_text()) == {
        "phases": [],
        "status": "human_review",
        "reviewReason": "plan_review",
    }


def test_preflight_pause_plan_uri_existing_plan(tmp_path, monkeypatch):
    phases = [{"name": "b", "subtasks": [{"id": "1", "status": "pending"}]}]
    code, plan_file = _preflight(
        tmp_path,
        monkeypatch,
        uri=True,
        auto=True,
        plan={"phases": phases, "status": "in_progress"},
    )
    assert code == 0
    plan = json.loads(plan_file.read_text())
    assert plan["phases"] == phases
    assert (plan["status"], plan["reviewReason"]) == ("human_review", "plan_review")


def test_preflight_pause_plan_no_uri(tmp_path, monkeypatch):
    code, plan_file = _preflight(tmp_path, monkeypatch, uri=False, auto=True, plan=None)
    assert code == 0
    assert not plan_file.exists()


def test_preflight_pause_plan_cli_mode(tmp_path, monkeypatch):
    # WORKSPACE_URI set, so only the auto_continue guard keeps the plan unwritten.
    code, plan_file = _preflight(tmp_path, monkeypatch, uri=True, auto=False, plan=None)
    assert code == 1
    assert not plan_file.exists()
