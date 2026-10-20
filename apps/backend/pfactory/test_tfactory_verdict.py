"""#1695: tfactory_verdict reads the verdict TFactory itself recorded."""

from __future__ import annotations

from pathlib import Path

import pfactory.tfactory_client as tc
import pytest

TASK = "http://tf/api/tfactory/tasks/048-feat"


def _task(
    status: str = "triaged", project: str = "demo", sha: str | None = "abc"
) -> tuple[int, object]:
    payload: dict[str, object] = {
        "project_id": project,
        "status_json": {"status": status},
    }
    if sha is not None:
        payload["source_sha"] = sha
    return 200, payload


def _verdicts(*kinds: str) -> tuple[int, object]:
    return 200, {"verdicts": [{"verdict": k} for k in kinds]}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TFACTORY_PROJECT_ID", "demo")
    monkeypatch.setattr(
        tc, "tfactory_config", lambda: {"base_url": "http://tf", "token": "tok"}
    )


def _run(
    routes: dict[str, tuple[int, object]],
    spec_id: str = "048-feat",
    head: str = "abc",
) -> tuple[str | None, list[tuple[str, dict[str, str]]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    def getter(url: str, headers: dict[str, str]) -> tuple[int, object]:
        calls.append((url, headers))
        return routes[url]

    return tc.tfactory_verdict(Path("spec"), spec_id, head=head, getter=getter), calls


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
    ("routes", "head"),
    [
        (
            {
                TASK: _task(project="other"),
                TASK + "/verdicts.json": _verdicts("accept"),
            },
            "abc",
        ),
        ({TASK: _task("running"), TASK + "/verdicts.json": _verdicts("accept")}, "abc"),
        (
            {
                TASK: _task("triaged_empty"),
                TASK + "/verdicts.json": _verdicts("accept"),
            },
            "abc",
        ),
        ({TASK: (404, None), TASK + "/verdicts.json": _verdicts("accept")}, "abc"),
        ({TASK: _task(), TASK + "/verdicts.json": (404, None)}, "abc"),
        ({TASK: _task(), TASK + "/verdicts.json": _verdicts()}, "abc"),
        ({TASK: _task(), TASK + "/verdicts.json": (200, [1])}, "abc"),
        ({TASK: (200, [1]), TASK + "/verdicts.json": _verdicts("accept")}, "abc"),
        (
            {TASK: _task(sha="old"), TASK + "/verdicts.json": _verdicts("accept")},
            "abc",
        ),
        (
            {TASK: _task(sha=None), TASK + "/verdicts.json": _verdicts("accept")},
            "abc",
        ),
        (
            {TASK: _task(sha=""), TASK + "/verdicts.json": _verdicts("accept")},
            "",
        ),
        (
            {TASK: _task(sha="old"), TASK + "/verdicts.json": _verdicts("reject")},
            "abc",
        ),
    ],
)
def test_no_trusted_result_is_none(
    routes: dict[str, tuple[int, object]], head: str
) -> None:
    assert _run(routes, head=head)[0] is None


def test_getter_raising_is_none() -> None:
    def boom(_url: str, _headers: dict[str, str]) -> tuple[int, object]:
        raise OSError("down")

    assert (
        tc.tfactory_verdict(Path("spec"), "048-feat", head="abc", getter=boom) is None
    )


def test_unconfigured_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "tfactory_config", lambda: {"base_url": "", "token": ""})
    assert _run({})[0] is None


def test_spec_id_is_url_quoted() -> None:
    base = "http://tf/api/tfactory/tasks/a%2Fb"
    out, _ = _run({base: _task(), base + "/verdicts.json": _verdicts("accept")}, "a/b")
    assert out == "pass"


@pytest.mark.parametrize(
    "body",
    [
        {"verdicts": [{"verdict": "accept"}, {}]},
        {"verdicts": [{"verdict": "accept"}, {"verdict": "maybe"}]},
        {"verdicts": [{"verdict": "accept"}, "accept"]},
    ],
)
def test_missing_or_unknown_verdict_is_none(body: dict[str, object]) -> None:
    assert _run({TASK: _task(), TASK + "/verdicts.json": (200, body)})[0] is None


def test_config_error_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> dict[str, str]:
        raise RuntimeError("env")

    monkeypatch.setattr(tc, "tfactory_config", boom)
    assert (
        tc.tfactory_verdict(
            Path("spec"), "048-feat", head="abc", getter=lambda _u, _h: (200, {})
        )
        is None
    )
