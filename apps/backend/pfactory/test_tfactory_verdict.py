"""#1695: tfactory_verdict reads the verdict TFactory itself recorded."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
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
    spec_dir: Path = Path("spec"),
) -> tuple[str | None, list[tuple[str, dict[str, str]]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    def getter(url: str, headers: dict[str, str]) -> tuple[int, object]:
        calls.append((url, headers))
        return routes[url]

    return tc.tfactory_verdict(spec_dir, spec_id, head=head, getter=getter), calls


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


def _vote_run(
    doc: dict[str, Any], task: tuple[int, object] | None = None
) -> tuple[str | None, dict[str, Any]]:
    vote: dict[str, Any] = {}
    routes = {TASK: task or _task(), TASK + "/verdicts.json": (200, doc)}
    out = tc.tfactory_verdict(
        Path("spec"), "048-feat", head="abc", getter=lambda u, _h: routes[u], vote=vote
    )
    return out, vote


def test_vote_summary_is_filled_on_a_trusted_result() -> None:
    summary = {"splits": {"3-0": 2}}
    doc = {"verdicts": [{"verdict": "accept"}], "verdict_vote": summary}
    assert _vote_run(doc) == ("pass", summary)


def test_vote_stays_empty_without_a_trusted_result_or_summary() -> None:
    doc = {"verdicts": [{"verdict": "accept"}], "verdict_vote": {"splits": {}}}
    assert _vote_run(doc, _task(status="running")) == (None, {})
    assert _vote_run({"verdicts": [{"verdict": "accept"}]}) == ("pass", {})


@pytest.mark.parametrize(
    ("success", "body", "expected"),
    [
        (True, {"workspace_project_id": "2b2e"}, "2b2e"),
        (True, ValueError("not json"), None),
        (False, {"workspace_project_id": "2b2e"}, None),
    ],
)
def test_poster_project_id_only_on_json_success(
    monkeypatch: pytest.MonkeyPatch, success: bool, body: object, expected: str | None
) -> None:
    class Response:
        status_code = 200 if success else 409
        is_success = success
        text = "x" * 2001

        def json(self) -> object:
            if isinstance(body, Exception):
                raise body
            return body

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def post(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    result = asyncio.run(tc._httpx_poster("http://tf", {}, {}))
    assert result["body"] == "x" * 2000
    assert result.get("workspace_project_id") == expected


@pytest.mark.parametrize(
    ("ok", "value", "included"),
    [
        (True, "2b2e", True),
        (True, 2, False),
        (True, "bad/id", False),
        (True, "x" * 129, False),
        (True, None, False),
        (False, "2b2e", False),
    ],
)
def test_send_handoff_records_valid_project_only(
    ok: bool, value: object, included: bool
) -> None:
    async def poster(
        _url: str, _payload: dict[str, object], _headers: dict[str, str]
    ) -> dict[str, object]:
        result: dict[str, object] = {"ok": ok, "status": 200 if ok else 409}
        if value is not None:
            result["workspace_project_id"] = value
        return result

    result = asyncio.run(
        tc.send_handoff({}, config={"base_url": "http://tf"}, poster=poster)
    )
    assert result.get("tfactory_project_id") == ("2b2e" if included else None)


def test_handoff_marker_is_atomically_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tc, "wants_auto_handoff", lambda _spec_dir: True)
    monkeypatch.setattr(tc, "build_commit_count", lambda _spec_dir, _spec_id: 1)
    monkeypatch.setattr(tc, "build_ingest_payload", lambda *_args, **_kwargs: {})

    async def handoff(_payload: dict[str, object]) -> dict[str, object]:
        return {"sent": True, "tfactory_project_id": "2b2e"}

    monkeypatch.setattr(tc, "send_handoff", handoff)
    mkstemp = tc.tempfile.mkstemp
    replace = tc.os.replace
    operations: list[tuple[str, Path, Path | None]] = []

    def tracked_mkstemp(**kwargs: object) -> tuple[int, str]:
        fd, path = mkstemp(**kwargs)
        operations.append(("mkstemp", Path(path), None))
        return fd, path

    def tracked_replace(src: str, dst: Path) -> None:
        operations.append(("replace", Path(src), dst))
        replace(src, dst)

    monkeypatch.setattr(tc.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(tc.os, "replace", tracked_replace)
    result = asyncio.run(
        tc.maybe_auto_handoff_tfactory(tmp_path, "048-feat", contract=None)
    )
    marker = tmp_path / "tfactory_handoff.json"
    assert json.loads(marker.read_text()) == result
    assert operations == [
        ("mkstemp", operations[0][1], None),
        ("replace", operations[0][1], marker),
    ]
    assert operations[0][1].parent == tmp_path
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("verdict,expected", [("flag", "pass"), ("reject", "fail")])
def test_recorded_project_scopes_both_requests(
    tmp_path: Path, verdict: str, expected: str
) -> None:
    (tmp_path / "tfactory_handoff.json").write_text(
        json.dumps({"sent": True, "tfactory_project_id": "2b2e"})
    )
    scoped = TASK + "?project_id=2b2e"
    verdicts = TASK + "/verdicts.json?project_id=2b2e"
    out, calls = _run(
        {scoped: _task(project="2b2e"), verdicts: _verdicts("accept", verdict)},
        spec_dir=tmp_path,
    )
    assert out == expected
    assert [url for url, _ in calls] == [scoped, verdicts]


def test_recorded_project_mismatch_stops_after_task(tmp_path: Path) -> None:
    (tmp_path / "tfactory_handoff.json").write_text(
        json.dumps({"sent": True, "tfactory_project_id": "2b2e"})
    )
    scoped = TASK + "?project_id=2b2e"
    out, calls = _run({scoped: _task(project="demo")}, spec_dir=tmp_path)
    assert out is None
    assert [url for url, _ in calls] == [scoped]


@pytest.mark.parametrize("status", [404, 409])
def test_scoped_failure_does_not_retry_bare(tmp_path: Path, status: int) -> None:
    (tmp_path / "tfactory_handoff.json").write_text(
        json.dumps({"sent": True, "tfactory_project_id": "2b2e"})
    )
    scoped = TASK + "?project_id=2b2e"
    out, calls = _run(
        {
            scoped: (status, None),
            TASK: _task(),
            TASK + "/verdicts.json": _verdicts("accept"),
        },
        spec_dir=tmp_path,
    )
    assert out is None
    assert [url for url, _ in calls] == [scoped]


@pytest.mark.parametrize(
    "marker",
    [
        {"sent": False, "tfactory_project_id": "2b2e"},
        {"sent": True, "tfactory_project_id": "bad/id"},
        {"sent": True, "tfactory_project_id": 123},
        "malformed",
    ],
)
def test_invalid_marker_uses_bare_urls(tmp_path: Path, marker: object) -> None:
    (tmp_path / "tfactory_handoff.json").write_text(
        marker if isinstance(marker, str) else json.dumps(marker)
    )
    out, calls = _run(
        {TASK: _task(), TASK + "/verdicts.json": _verdicts("accept")},
        spec_dir=tmp_path,
    )
    assert out == "pass"
    assert [url for url, _ in calls] == [TASK, TASK + "/verdicts.json"]


def test_unreadable_marker_uses_bare_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "tfactory_handoff.json"
    marker.write_text("{}")
    read_text = Path.read_text

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == marker:
            raise OSError("unreadable")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    out, calls = _run(
        {TASK: _task(), TASK + "/verdicts.json": _verdicts("accept")},
        spec_dir=tmp_path,
    )
    assert out == "pass"
    assert [url for url, _ in calls] == [TASK, TASK + "/verdicts.json"]


def test_no_marker_or_env_uses_derived_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TFACTORY_PROJECT_ID")
    spec_dir = tmp_path / "workspaces" / "derived" / ".aifactory" / "specs" / "048-feat"
    spec_dir.mkdir(parents=True)
    out, calls = _run(
        {TASK: _task(project="derived"), TASK + "/verdicts.json": _verdicts("accept")},
        spec_dir=spec_dir,
    )
    assert out == "pass"
    assert [url for url, _ in calls] == [TASK, TASK + "/verdicts.json"]
