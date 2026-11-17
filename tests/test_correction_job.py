"""The QA correction Job entrypoint and fixer result (#2116)."""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest
from core import workspace_fetch
from qa import correction, correction_job


def _fake_fixer_modules(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    async def session(*_a: object, **_k: object) -> tuple[str, str]:
        if isinstance(result, Exception):
            raise result
        return result, ""  # type: ignore[return-value]

    mods = {
        "core.client": types.SimpleNamespace(create_client=lambda *_a, **_k: Client()),
        "phase_config": types.SimpleNamespace(
            get_phase_model=lambda *_a: "sonnet",
            get_phase_thinking_budget=lambda *_a: 0,
            get_provider_extra_kwargs=lambda *_a: {},
            infer_provider_from_model=lambda _m: "claude",
        ),
        "providers.factory": types.SimpleNamespace(get_provider=lambda *_a, **_k: None),
        "qa.fixer": types.SimpleNamespace(run_qa_fixer_session=session),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.mark.parametrize(
    ("result", "expected"),
    [("fixed", True), ("error", False), (RuntimeError("boom"), False)],
)
async def test_run_fixer_bg_true_only_when_fixed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: object, expected: bool
) -> None:
    _fake_fixer_modules(monkeypatch, result)
    assert await correction._run_fixer_bg(tmp_path) is expected


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    return tmp_path


@pytest.mark.parametrize(
    ("fixed", "pushed", "code"), [(True, True, 0), (True, False, 1), (False, True, 1)]
)
def test_main_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixed: bool,
    pushed: bool,
    code: int,
) -> None:
    project = _repo(tmp_path)
    seen: list[str] = []

    async def fake_fixer(spec_dir: Path) -> bool:
        seen.append(spec_dir.name)
        return fixed

    monkeypatch.setattr(workspace_fetch, "maybe_unpack_workspace", lambda _p: True)
    monkeypatch.setattr(workspace_fetch, "maybe_push_correction", lambda _p, _b: pushed)
    monkeypatch.setattr(correction, "_run_fixer_bg", fake_fixer)
    argv = ["--project", str(project), "--spec-id", "001-x"]
    assert correction_job.main(argv) == code
    assert seen == ["001-x"]
