"""#1695: tfactory_verdict reads the verdict TFactory itself recorded."""

from __future__ import annotations

from pathlib import Path

import pfactory.tfactory_client as tc
import pytest

TASK = "http://tf/api/tfactory/tasks/048-feat"


def _task(status: str = "triaged", project: str = "demo") -> tuple[int, object]:
    return 200, {"project_id": project, "status_json": {"status": status}}


def _verdicts(*kinds: str) -> tuple[int, object]:
    return 200, {"verdicts": [{"verdict": k} for k in kinds]}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TFACTORY_PROJECT_ID", "demo")
    monkeypatch.setattr(
        tc, "tfactory_config", lambda: {"base_url": "http://tf", "token": "tok"}
    )


def _run(
    routes: dict[str, tuple[int, object]], spec_id: str = "048-feat"
) -> tuple[str | None, list[tuple[str, dict[str, str]]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    def getter(url: str, headers: dict[str, str]) -> tuple[int, object]:
        calls.append((url, headers))
        return routes[url]

    return tc.tfactory_verdict(Path("spec"), spec_id, getter=getter), calls


def test_flag_does_not_block_and_request_shape() -> None:
    out, calls = _run(
        {TASK: _task(), TASK + "/verdicts.json": _verdicts("accept", "flag")}
    )
    assert out == "pass"
    assert [u for u, _ in calls] == [TASK, TASK + "/verdicts.json"]
    assert all(h == {"Authorization": "Bearer tok"} for _, h in calls)


def test_reject_fails() -> None:
    out, _ = _run(
        {TASK: _task(), TASK + "/verdicts.json": _verdicts("accept", "reject")}
    )
    assert out == "fail"


@pytest.mark.parametrize(
    "routes",
    [
        {TASK: _task(project="other"), TASK + "/verdicts.json": _verdicts("accept")},
        {TASK: _task("running"), TASK + "/verdicts.json": _verdicts("accept")},
        {TASK: _task("triaged_empty"), TASK + "/verdicts.json": _verdicts("accept")},
        {TASK: (404, None), TASK + "/verdicts.json": _verdicts("accept")},
        {TASK: _task(), TASK + "/verdicts.json": (404, None)},
        {TASK: _task(), TASK + "/verdicts.json": _verdicts()},
        {TASK: _task(), TASK + "/verdicts.json": (200, [1])},
        {TASK: (200, [1]), TASK + "/verdicts.json": _verdicts("accept")},
    ],
)
def test_no_trusted_result_is_none(routes: dict[str, tuple[int, object]]) -> None:
    assert _run(routes)[0] is None


def test_getter_raising_is_none() -> None:
    def boom(_url: str, _headers: dict[str, str]) -> tuple[int, object]:
        raise OSError("down")

    assert tc.tfactory_verdict(Path("spec"), "048-feat", getter=boom) is None


def test_unconfigured_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "tfactory_config", lambda: {"base_url": "", "token": ""})
    assert _run({})[0] is None


def test_spec_id_is_url_quoted() -> None:
    base = "http://tf/api/tfactory/tasks/a%2Fb"
    out, _ = _run({base: _task(), base + "/verdicts.json": _verdicts("accept")}, "a/b")
    assert out == "pass"
