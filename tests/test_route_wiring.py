"""Post-merge route-wiring gate (#1123).

The defect, measured on the merged ``aifactory/101-vat-quote-endpoint-with-half-u``
tree: the suite passes 91/91 both with and without
``app.include_router(vat_quote_router)``. These tests reproduce that shape in
miniature — a router written in one file, registered (or not) in the entrypoint,
and tested only through a throwaway app — and assert the gate is what finally
goes red when the registration is deleted.

The probe runs in a real interpreter against a real FastAPI app; only the
*choice* of interpreter is injected, so the gate's own logic is never faked.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from agents.gate_runner import Gate, run_gates
from agents.route_wiring import SKIP_EXIT_CODE, route_wiring_gate

pytest.importorskip("fastapi")

# A plan that promises POST /api/quote three ways (the shapes the planner really
# emits) plus a documentation link that is not a promise to serve anything.
PLAN = {
    "phases": [
        {
            "name": "quote",
            "subtasks": [
                {"id": "1.1", "description": "Add POST /api/quote"},
                {"id": "1.2", "description": "Register the router on the app"},
                {
                    "id": "1.3",
                    "description": "Verify it",
                    "verification": {"url": "http://localhost:8000/api/quote"},
                },
                {"id": "1.4", "description": "see https://docs.example.com/latest/"},
            ],
        }
    ]
}

_ROUTER = (
    "from fastapi import APIRouter\n"
    "router = APIRouter()\n"
    "@router.post('/api/quote')\n"
    "async def quote():\n    return {'total': 1}\n"
)
_MAIN_WIRED = (
    "from fastapi import FastAPI\n"
    "from app.vat_quote import router\n"
    "app = FastAPI()\n"
    "app.include_router(router)\n"
    "@app.get('/healthz')\n"
    "async def healthz():\n    return {'status': 'ok'}\n"
)
_MAIN_UNWIRED = _MAIN_WIRED.replace("app.include_router(router)\n", "")
# The C2 shape: names the path, never touches the shipped app.
_HOLLOW_TEST = (
    "from fastapi import FastAPI\n"
    "from fastapi.testclient import TestClient\n"
    "from app.vat_quote import router\n"
    "_app = FastAPI()\n_app.include_router(router)\n"
    "client = TestClient(_app)\n"
    "def test_quote():\n"
    "    assert client.post('/api/quote').status_code == 200\n"
)
# The one file that tells us where the shipped application lives.
_ROOT_TEST = (
    "from fastapi.testclient import TestClient\n"
    "from app.main import app\n"
    "client = TestClient(app)\n"
    "def test_healthz():\n    assert client.get('/healthz').status_code == 200\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(root: Path, main_src: str, *, root_test: str | None = _ROOT_TEST) -> Path:
    """A src-layout project, with a plan.json beside it. Returns the plan path."""
    _write(root / "src" / "app" / "__init__.py", "")
    _write(root / "src" / "app" / "vat_quote.py", _ROUTER)
    _write(root / "src" / "app" / "main.py", main_src)
    _write(root / "tests" / "test_vat_quote.py", _HOLLOW_TEST)
    if root_test is not None:
        _write(root / "tests" / "test_root.py", root_test)
    plan = root / "plan.json"
    plan.write_text(json.dumps(PLAN), encoding="utf-8")
    return plan


def _runner(command: list[str], cwd: Path) -> tuple[int | None, str]:
    """gate_runner's contract, pinned to the interpreter running these tests
    (the only one guaranteed to have fastapi installed)."""
    cmd = [sys.executable if c == "python3" else c for c in command]
    proc = subprocess.run(  # noqa: S603 - argv built by the gate under test
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _run(gate: Gate, cwd: Path):
    return asyncio.run(run_gates(cwd, [gate], runner=_runner))[0]


def test_passes_when_the_router_is_registered(tmp_path: Path) -> None:
    plan = _tree(tmp_path, _MAIN_WIRED)
    gate = route_wiring_gate(plan, tmp_path)
    assert gate is not None
    result = _run(gate, tmp_path)
    assert result.passed and not result.skipped, result.output_tail


def test_fails_when_the_registration_is_deleted(tmp_path: Path) -> None:
    """The mutation the 91-test suite could not see."""
    plan = _tree(tmp_path, _MAIN_UNWIRED)
    gate = route_wiring_gate(plan, tmp_path)
    assert gate is not None
    result = _run(gate, tmp_path)
    assert not result.passed and not result.skipped
    assert "/api/quote" in result.output_tail
    assert "app.main:app" in result.output_tail


def test_only_the_promised_paths_are_checked(tmp_path: Path) -> None:
    """A link to somebody's documentation is not a route this build must serve."""
    plan = _tree(tmp_path, _MAIN_WIRED)
    gate = route_wiring_gate(plan, tmp_path)
    assert gate is not None
    assert gate.command[4:] == ["/api/quote"]


def test_parameterised_routes_match(tmp_path: Path) -> None:
    main = _MAIN_WIRED.replace(
        "@app.get('/healthz')\nasync def healthz():\n    return {'status': 'ok'}\n",
        "@app.get('/api/items/{sku}')\nasync def item(sku: str):\n    return {}\n",
    )
    plan = tmp_path / "plan.json"
    _write(tmp_path / "src" / "app" / "__init__.py", "")
    _write(tmp_path / "src" / "app" / "vat_quote.py", _ROUTER)
    _write(tmp_path / "src" / "app" / "main.py", main)
    _write(tmp_path / "tests" / "test_root.py", _ROOT_TEST)
    plan.write_text(
        json.dumps({"subtasks": [{"description": "GET /api/items/abc123"}]}),
        encoding="utf-8",
    )
    gate = route_wiring_gate(plan, tmp_path)
    assert gate is not None
    assert _run(gate, tmp_path).passed


def _stub_tree(root: Path, main_src: str) -> Path:
    """A project whose entrypoint is a hand-rolled stub, so the route-collection
    logic is pinned independently of whichever FastAPI happens to be installed."""
    _write(root / "app" / "__init__.py", "")
    _write(root / "app" / "main.py", main_src)
    _write(root / "tests" / "test_root.py", "from app.main import app\n")
    plan = root / "plan.json"
    plan.write_text(
        json.dumps({"subtasks": [{"description": "Add POST /api/quote"}]}),
        encoding="utf-8",
    )
    return plan


# FastAPI 0.141 stopped flattening include_router into app.routes and keeps an
# opaque, path-less holder instead. Reading app.routes alone loses the mounted
# route entirely — a false failure on a correctly wired build.
_INCLUDED_ROUTER_SHAPE = """
class R:
    def __init__(self, path): self.path = path
class Router:
    def __init__(self, routes): self.routes = routes
class Included:
    path = None
    def __init__(self, router): self.original_router = router
class App:
    routes = [R("/healthz"), Included(Router([R("/api/quote")]))]
app = App()
"""

# A Starlette-less app that answers only through its OpenAPI schema.
_OPENAPI_ONLY_SHAPE = """
class App:
    routes = []
    def openapi(self): return {"paths": {"/api/quote": {}, "/healthz": {}}}
app = App()
"""


def test_finds_routes_behind_an_included_router(tmp_path: Path) -> None:
    plan = _stub_tree(tmp_path, _INCLUDED_ROUTER_SHAPE)
    gate = route_wiring_gate(plan, tmp_path)
    assert gate is not None
    result = _run(gate, tmp_path)
    assert result.passed and not result.skipped, result.output_tail


def test_finds_routes_from_the_openapi_schema(tmp_path: Path) -> None:
    plan = _stub_tree(tmp_path, _OPENAPI_ONLY_SHAPE)
    gate = route_wiring_gate(plan, tmp_path)
    assert gate is not None
    result = _run(gate, tmp_path)
    assert result.passed and not result.skipped, result.output_tail


def test_skipped_not_passed_when_the_app_cannot_be_imported(tmp_path: Path) -> None:
    """A check that never ran must not read like one that ran clean."""
    plan = _tree(tmp_path, _MAIN_WIRED)
    (tmp_path / "src" / "app" / "main.py").unlink()
    gate = route_wiring_gate(plan, tmp_path)
    assert gate is not None
    result = _run(gate, tmp_path)
    assert result.skipped and result.status == "skipped"
    assert "NOT CHECKED" in result.output_tail


def test_skip_code_is_not_confused_with_success(tmp_path: Path) -> None:
    """Gate.skip_code only maps its own code; 0 still means passed."""

    def exits(code: int):
        def run(_command: list[str], _cwd: Path) -> tuple[int, str]:
            return code, ""

        return run

    skip_gate = Gate("x", ["true"], skip_code=SKIP_EXIT_CODE)
    res = asyncio.run(run_gates(tmp_path, [skip_gate], runner=exits(SKIP_EXIT_CODE)))
    assert res[0].skipped
    res = asyncio.run(run_gates(tmp_path, [skip_gate], runner=exits(0)))
    assert res[0].passed and not res[0].skipped
    plain = Gate("x", ["true"])  # no skip_code: 77 is an ordinary failure
    res = asyncio.run(run_gates(tmp_path, [plain], runner=exits(SKIP_EXIT_CODE)))
    assert not res[0].passed and not res[0].skipped


def test_no_gate_when_the_plan_promises_no_http_path(tmp_path: Path) -> None:
    _tree(tmp_path, _MAIN_WIRED)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"subtasks": [{"description": "Add slugify()"}]}), encoding="utf-8"
    )
    assert route_wiring_gate(plan, tmp_path) is None


def test_no_gate_when_no_test_names_an_entrypoint(tmp_path: Path) -> None:
    """We never guess the import; a repo with only hollow tests is not judged."""
    plan = _tree(tmp_path, _MAIN_WIRED, root_test=None)
    assert route_wiring_gate(plan, tmp_path) is None


def test_no_gate_when_the_plan_is_unreadable(tmp_path: Path) -> None:
    _tree(tmp_path, _MAIN_WIRED)
    assert route_wiring_gate(tmp_path / "nope.json", tmp_path) is None


def test_escape_hatch_is_the_existing_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _tree(tmp_path, _MAIN_UNWIRED)
    assert route_wiring_gate(plan, tmp_path) is not None
    monkeypatch.setenv("AIFACTORY_TEST_EVIDENCE_GATE", "off")
    assert route_wiring_gate(plan, tmp_path) is None
