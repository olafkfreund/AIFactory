"""#851: the honest-verification gate — a test/verification subtask cannot be
marked ``completed`` unless a real test command actually ran this build.

The Dishonest Coder wrote ``[x] Run all tests`` for a repo with no toolchain to
run them. These tests pin the gate that makes that claim falsifiable.
"""

import json
from pathlib import Path

import pytest
from agents.test_evidence import (
    exercises_shipped_app,
    http_paths,
    is_test_command,
    is_verification_subtask,
    looks_failed,
    read_test_evidence,
    record_test_run,
)
from agents.tools_pkg.tools.subtask import apply_subtask_status_update

_PLAN = {
    "phases": [
        {
            "id": "p1",
            "name": "Implement",
            "subtasks": [
                {
                    "id": "1.1",
                    "title": "Create strutil module",
                    "status": "in_progress",
                },
                {"id": "4.1", "title": "Run all tests", "status": "in_progress"},
            ],
        }
    ]
}


def _spec(tmp_path: Path) -> Path:
    (tmp_path / "implementation_plan.json").write_text(json.dumps(_PLAN))
    return tmp_path


def _complete(spec_dir: Path, subtask_id: str):
    return apply_subtask_status_update(spec_dir, subtask_id, "completed")


def _status(spec_dir: Path, subtask_id: str) -> str:
    plan = json.loads((spec_dir / "implementation_plan.json").read_text())
    for phase in plan["phases"]:
        for st in phase["subtasks"]:
            if st["id"] == subtask_id:
                return st["status"]
    raise AssertionError("subtask not found")


# -- unit: command / subtask / failure classification --------------------------


def test_is_test_command_runs_vs_mentions():
    assert is_test_command("pytest -q")
    assert is_test_command("cd api && go test ./...")
    assert is_test_command("pip install pytest && pytest")  # install THEN run
    assert not is_test_command("pip install pytest")
    assert not is_test_command("cat tests/test_foo.py")
    assert not is_test_command("")


def test_is_verification_subtask():
    assert is_verification_subtask({"title": "Run all tests"})
    assert is_verification_subtask({"description": "verify the implementation"})
    assert not is_verification_subtask({"title": "Create the strutil module"})


def test_looks_failed_only_on_clear_markers():
    assert looks_failed("=== 2 failed, 1 passed ===")
    assert not looks_failed("=== 5 passed in 0.1s ===")
    assert not looks_failed("")  # ambiguous/empty is NOT a failure → never false-block


# -- the gate ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_subtask_refused_without_evidence(tmp_path):
    """The #851 bug, pinned: no test ran → completing 'Run all tests' is refused
    and the plan status is left unchanged."""
    spec = _spec(tmp_path)
    res = await _complete(spec, "4.1")
    assert "Refused" in res["content"][0]["text"]
    assert _status(spec, "4.1") == "in_progress", (
        "status must NOT be persisted on refusal"
    )


@pytest.mark.asyncio
async def test_test_subtask_allowed_with_passing_evidence(tmp_path):
    spec = _spec(tmp_path)
    record_test_run(spec, "pytest -q", "=== 5 passed in 0.1s ===")
    res = await _complete(spec, "4.1")
    assert "Successfully updated" in res["content"][0]["text"]
    assert _status(spec, "4.1") == "completed"


@pytest.mark.asyncio
async def test_test_subtask_refused_when_last_run_failed(tmp_path):
    spec = _spec(tmp_path)
    record_test_run(spec, "pytest -q", "=== 1 failed, 4 passed ===")
    res = await _complete(spec, "4.1")
    assert "Refused" in res["content"][0]["text"]
    assert "last recorded test run failed" in res["content"][0]["text"]
    assert _status(spec, "4.1") == "in_progress"


@pytest.mark.asyncio
async def test_non_test_subtask_always_allowed(tmp_path):
    """A normal implementation subtask is never gated — no evidence needed."""
    spec = _spec(tmp_path)
    res = await _complete(spec, "1.1")
    assert "Successfully updated" in res["content"][0]["text"]
    assert _status(spec, "1.1") == "completed"


@pytest.mark.asyncio
async def test_gate_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_TEST_EVIDENCE_GATE", "off")
    spec = _spec(tmp_path)
    res = await _complete(spec, "4.1")  # no evidence, but gate off
    assert "Successfully updated" in res["content"][0]["text"]
    assert _status(spec, "4.1") == "completed"


def test_read_evidence_last_failed_reflects_only_latest(tmp_path):
    """A coder that fixed a failure and re-ran green is not held to the earlier
    failure — last_failed tracks only the most recent run."""
    record_test_run(tmp_path, "pytest", "1 failed")
    record_test_run(tmp_path, "pytest", "5 passed")
    ev = read_test_evidence(tmp_path)
    assert ev["ran"] and ev["runs"] == 2 and not ev["last_failed"]


# -- #1111: "a test ran" is not "a test ran against the deliverable" -----------
#
# Wave worker C2 wrote a correct router, never registered it in app.main, and
# tested it through a FastAPI() instance built inside its own test file. 45/45
# genuinely green, #851 satisfied, POST /api/quote a 404 on the shipped service.
# These tests pin BOTH directions: the hollow shape must be refused, and every
# legitimate shape must still complete.

# Verbatim shape of C2's tests/test_vat_quote.py (issue #1111).
_C2_HOLLOW = """\
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.vat_quote import router

# Build a minimal test application that includes only the VAT quote router.
_app = FastAPI()
_app.include_router(router)

client = TestClient(_app)


def test_quote():
    assert client.post("/api/quote", json={"net": 100}).status_code == 200
"""

# The repo's own established convention, which C2 deliberately deviated from.
_HONEST = """\
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_quote():
    assert client.post("/api/quote", json={"net": 100}).status_code == 200
"""

_HTTP_PLAN = {
    "phases": [
        {
            "id": "p1",
            "name": "Implement",
            "subtasks": [
                {
                    "id": "c2",
                    "description": "Implement POST /api/quote VAT quote endpoint",
                    "status": "in_progress",
                },
                {
                    "id": "pure",
                    "description": "Add a half-up rounding helper to vat_math",
                    "status": "in_progress",
                },
            ],
        }
    ]
}


def _http_project(tmp_path: Path) -> tuple[Path, Path]:
    """A project laid out the way a build sees it: repo root, spec dir under
    ``.aifactory/specs/``, and a src tree whose main.py never registers the
    router — the defect the hollow test is blind to."""
    spec = tmp_path / ".aifactory" / "specs" / "101-vat-quote"
    spec.mkdir(parents=True)
    (spec / "implementation_plan.json").write_text(json.dumps(_HTTP_PLAN))
    src = tmp_path / "src" / "app"
    src.mkdir(parents=True)
    (src / "main.py").write_text("from fastapi import FastAPI\n\napp = FastAPI()\n")
    (tmp_path / "tests").mkdir()
    return tmp_path, spec


async def _complete_in(project: Path, spec: Path, subtask_id: str):
    return await apply_subtask_status_update(spec, subtask_id, "completed", "", project)


@pytest.mark.asyncio
async def test_c2_hollow_app_is_refused(tmp_path):
    """The #1111 bug, pinned: a real, really-passing test asserting against an
    app built inside the test file is NOT evidence for the shipped endpoint."""
    project, spec = _http_project(tmp_path)
    (project / "tests" / "test_vat_quote.py").write_text(_C2_HOLLOW)
    record_test_run(spec, "pytest -q", "=== 45 passed in 1.2s ===")  # #851 satisfied

    res = await _complete_in(project, spec, "c2")
    text = res["content"][0]["text"]
    assert "Refused" in text
    assert "never touch the shipped application" in text
    assert "tests/test_vat_quote.py" in text
    assert _status(spec, "c2") == "in_progress", (
        "status must NOT be persisted on refusal"
    )


@pytest.mark.asyncio
async def test_test_against_shipped_app_is_accepted(tmp_path):
    """The same subtask, tested the way the repo's own tests do it, completes."""
    project, spec = _http_project(tmp_path)
    (project / "tests" / "test_vat_quote.py").write_text(_HONEST)
    record_test_run(spec, "pytest -q", "=== 45 passed in 1.2s ===")

    res = await _complete_in(project, spec, "c2")
    assert "Successfully updated" in res["content"][0]["text"]
    assert _status(spec, "c2") == "completed"


@pytest.mark.asyncio
async def test_one_honest_file_frees_the_hollow_ones(tmp_path):
    """A repo may hold throwaway-app tests as long as something drives the real
    app — otherwise the gate fires on correct work and gets switched off."""
    project, spec = _http_project(tmp_path)
    (project / "tests" / "test_vat_quote.py").write_text(_C2_HOLLOW)
    (project / "tests" / "test_api.py").write_text(_HONEST)

    res = await _complete_in(project, spec, "c2")
    assert "Successfully updated" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_pure_function_subtask_never_gated(tmp_path):
    """A unit test of a pure function has no app at all. It must complete even
    with a hollow HTTP test sitting in the same repo."""
    project, spec = _http_project(tmp_path)
    (project / "tests" / "test_vat_quote.py").write_text(_C2_HOLLOW)
    (project / "tests" / "test_vat_math.py").write_text(
        "from app.vat_math import round_half_up\n\n"
        "def test_round():\n    assert round_half_up(2.5) == 3\n"
    )

    res = await _complete_in(project, spec, "pure")
    assert "Successfully updated" in res["content"][0]["text"]
    assert _status(spec, "pure") == "completed"


@pytest.mark.asyncio
async def test_conftest_fixture_pattern_is_accepted(tmp_path):
    """The ordinary pytest shape — entrypoint imported in conftest, path
    asserted in the test — is honest and must not be flagged."""
    project, spec = _http_project(tmp_path)
    (project / "tests" / "conftest.py").write_text(
        "import pytest\nfrom fastapi.testclient import TestClient\n"
        "from app.main import app\n\n"
        "@pytest.fixture\ndef client():\n    return TestClient(app)\n"
    )
    (project / "tests" / "test_vat_quote.py").write_text(
        "def test_quote(client):\n"
        '    assert client.post("/api/quote").status_code == 200\n'
    )

    res = await _complete_in(project, spec, "c2")
    assert "Successfully updated" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_live_server_test_is_accepted(tmp_path):
    """A test driving a running server over the wire exercises the shipped app
    by definition, whatever it imports."""
    project, spec = _http_project(tmp_path)
    (project / "tests" / "test_smoke.py").write_text(
        "import httpx\n\ndef test_quote():\n"
        '    r = httpx.post("http://localhost:8000/api/quote")\n'
        "    assert r.status_code == 200\n"
    )

    res = await _complete_in(project, spec, "c2")
    assert "Successfully updated" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_non_python_repo_is_not_judged(tmp_path):
    """A Go service naming the same path has no Python test to read. The gate
    stays inert rather than refusing work it cannot assess."""
    project, spec = _http_project(tmp_path)
    (project / "quote_test.go").write_text(
        'package main\n\nfunc TestQuote(t *testing.T) { get("/api/quote") }\n'
    )

    res = await _complete_in(project, spec, "c2")
    assert "Successfully updated" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_deliverable_gate_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_TEST_EVIDENCE_GATE", "off")
    project, spec = _http_project(tmp_path)
    (project / "tests" / "test_vat_quote.py").write_text(_C2_HOLLOW)

    res = await _complete_in(project, spec, "c2")
    assert "Successfully updated" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_project_root_derived_when_caller_omits_it(tmp_path):
    """In-process callers pass project_dir; anything that does not still gets the
    check, via the <root>/.aifactory/specs/<spec> layout."""
    project, spec = _http_project(tmp_path)
    (project / "tests" / "test_vat_quote.py").write_text(_C2_HOLLOW)

    res = await apply_subtask_status_update(spec, "c2", "completed")
    assert "Refused" in res["content"][0]["text"]


# -- mutation guard -----------------------------------------------------------


def test_deliverable_check_discriminates_both_directions():
    """The gate itself must be falsifiable. Both assertions ride the same
    predicate, so weakening it to always-pass fails the first and weakening it to
    always-fail fails the second — a check that passes regardless goes red here."""
    assert not exercises_shipped_app(_C2_HOLLOW), (
        "a private FastAPI() built in the test file is not the shipped app"
    )
    assert exercises_shipped_app(_HONEST), (
        "importing the real entrypoint and asserting through it IS evidence"
    )
    # Importing the entrypoint while still building a rival app is not proof:
    # the assertions may be about either one.
    assert not exercises_shipped_app(_HONEST + "\n_other = FastAPI()\n")


def test_http_path_extraction_covers_the_planner_shapes():
    assert http_paths({"description": "Implement POST /api/quote"}) == ["/api/quote"]
    assert http_paths(
        {"verification": {"url": "http://localhost:5000/api/analytics/events"}}
    ) == ["/api/analytics/events"]
    assert http_paths({"acceptance_criteria": ["/api/quote returns 200"]}) == [
        "/api/quote"
    ]
    # No path named -> the check never engages at all.
    assert http_paths({"description": "Add a half-up rounding helper"}) == []
    assert http_paths({"files_to_create": ["src/app/vat_math.py"]}) == []
