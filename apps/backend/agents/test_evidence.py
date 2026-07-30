"""Test-execution evidence — the honest-verification gate (#851).

The Dishonest Coder, live: on the first autonomous run the coder wrote
``[x] Run all tests`` and ``## Final Status: COMPLETE / Ready for merge`` into a
committed artifact for a Go repo that had **no toolchain to run ``go test`` at
all**. The tests never ran; the claim was luck, not verification. RFC-0006's
rule is "never claim something works when it was never verified" — and the
artifact a human reads violated it.

This module makes the claim falsifiable. A PostToolUse hook
(``agents.act_loop_hooks.test_evidence_posttool_hook``) records every real test
command the coder runs via Bash — tamper-evident because it captures the actual
tool execution, not the model's self-report — into
``<spec_dir>/.aifactory/test_evidence.jsonl``. ``update_subtask_status`` then
refuses to mark a test/verification subtask ``completed`` unless a test command
actually ran (and did not clearly fail). No evidence -> the coder must run the
tests, or honestly mark the subtask ``failed`` with a reason. It can no longer
silently self-report a green checkbox.

Coarse by design: the gate asks "did tests run in this build", not "for this
exact subtask" — which is what catches #851 (zero runs on a toolchain-less repo)
without threading per-subtask timing. Default ON; escape hatch
``AIFACTORY_TEST_EVIDENCE_GATE=off`` for the rare repo where the gate misfires.

#1111 — "a test ran" is not "a test ran against the deliverable"
--------------------------------------------------------------
The #851 gate proves a test *executed*. It cannot prove the test exercised the
thing that was *built*. Observed live on ``101-vat-quote-endpoint``: wave worker
C2 wrote a correct ``APIRouter``, never registered it in ``app.main``, and
tested it through a ``FastAPI()`` instance it constructed inside its own test
file. 45/45 genuinely green, gate satisfied, ``POST /api/quote`` a 404 on the
shipped service. The tests passed against an application that existed only
inside the test file.

:func:`deliverable_evidence_gap` closes that. When a subtask names an HTTP path
(its description, acceptance criteria, or ``verification.url``), completing it
requires at least one Python test file that mentions the path AND reaches the
*shipped* app — by importing an application entrypoint (``app.main`` and
friends) without building a throwaway app of its own, or by calling a live
server over HTTP. C2's file does neither, so it is refused.

Deliberately narrow, because a gate that fires on correct work gets switched off
within a week and then there is no gate at all:

* No HTTP path in the subtask -> not checked. Pure-function unit tests, CLI
  work, schema changes and everything else complete exactly as before.
* No Python test file mentions the path -> not checked. A Go/Rust/TS repo is
  never judged by a Python heuristic, and "there is no test at all" belongs to
  the acceptance-criteria gate and TFactory, not here.
* One honest file is enough. A repo may hold as many throwaway-app tests as it
  likes as long as something also drives the real app.
* ``conftest.py`` counts, so the ordinary ``client`` fixture pattern (entrypoint
  imported in conftest, path asserted in the test) passes untouched.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

_EVIDENCE_REL = ".aifactory/test_evidence.jsonl"

# Test-RUNNER invocations, across the languages the fleet builds. A command is
# evidence of a real run only when one of these appears at the START of a
# sub-command (the invoked program), NOT merely as an argument — so
# ``pytest -q`` and ``cd api && go test ./...`` count, while ``pip install
# pytest`` and ``cat test_foo.py`` do not (see is_test_command).
_TEST_RUNNER_PATTERNS: tuple[str, ...] = (
    "pytest",
    "py.test",
    "unittest",  # via `python -m unittest`, after the wrapper strip below
    "nosetests",
    "go test",
    "gotestsum",
    "npm test",
    "npm run test",
    "yarn test",
    "pnpm test",
    "jest",
    "vitest",
    "mocha",
    "cargo test",
    "cargo nextest",
    "mvn test",
    "mvn verify",
    "gradle test",
    "gradlew test",
    "rspec",
    "rails test",
    "phpunit",
    "dotnet test",
    "ctest",
    "bats",
    "tox",
    "rebar3 eunit",
    "mix test",
)

# Leading wrappers stripped from a sub-command before matching the runner at its
# start: dir changes, sudo/env, VAR=val assignments, and language run-wrappers
# (``python -m``, ``npx``, ``poetry run`` …). Applied repeatedly so
# ``cd api && poetry run python -m pytest`` reduces to ``pytest``.
_LEADING_WRAPPERS = re.compile(
    r"^\s*(cd\s+\S+|sudo|env|time|nice|"
    r"[A-Za-z_][A-Za-z0-9_]*=\S+|"
    r"npx|poetry\s+run|pipenv\s+run|uv\s+run|pdm\s+run|hatch\s+run|"
    r"python3?\s+-m|py\s+-m)\s+",
    re.IGNORECASE,
)

# Clear failure markers in test output. Used only to BLOCK an obviously-failed
# run from being reported complete — ambiguous output is treated as a pass so the
# gate never false-blocks a real green run on a heuristic.
_FAIL_MARKERS = re.compile(
    r"(\bFAILED\b|\bFAIL\b|\d+\s+failed|--- FAIL|"
    r"AssertionError|Traceback \(most recent call last\)|"
    r"tests? failed|exit code [1-9]|exit status [1-9]|npm ERR!|"
    r"error: test failed|FAILURES!|BUILD FAILURE)",
    re.IGNORECASE,
)

# Title/description keywords that make a subtask a verification subtask — the
# only kind this gate governs. Anything else completes unchanged.
_VERIFY_KEYWORDS = re.compile(
    r"\b(run (all |the )?tests?|test suite|unit tests?|integration tests?|"
    r"pytest|go test|npm test|verify (the )?(tests?|build|implementation)|"
    r"validate (the )?(tests?|implementation)|ensure tests? pass|"
    r"testing)\b",
    re.IGNORECASE,
)


def gate_enabled() -> bool:
    """The evidence gate is ON by default (it is the #851 fix). Disable per-run
    with ``AIFACTORY_TEST_EVIDENCE_GATE`` in {off,0,false,no}."""
    return (
        os.environ.get("AIFACTORY_TEST_EVIDENCE_GATE") or ""
    ).strip().lower() not in {
        "off",
        "0",
        "false",
        "no",
    }


def is_test_command(command: str) -> bool:
    """True when ``command`` actually RUNS a test suite (not installs/reads one).

    A test runner counts only when it is the invoked program at the start of a
    sub-command (after stripping ``cd``/env/``python -m``-style wrappers), so
    ``pip install pytest`` and ``cat test_foo.py`` are correctly excluded while
    ``cd api && python -m pytest -q`` is included.
    """
    if not command:
        return False
    # Split on shell sequencing/pipe operators into candidate sub-commands.
    for part in re.split(r"&&|\|\||;|\||\bthen\b|\bdo\b", command):
        sub = part.strip()
        prev = None
        while sub and sub != prev:  # peel stacked leading wrappers
            prev = sub
            sub = _LEADING_WRAPPERS.sub("", sub, count=1).strip()
            sub = re.sub(r"^\./", "", sub)  # ./gradlew, ./mvnw
        low = sub.lower()
        if any(
            low == pat or low.startswith(pat + " ") for pat in _TEST_RUNNER_PATTERNS
        ):
            return True
    return False


def looks_failed(output: Any) -> bool:
    """True only when the test output carries a CLEAR failure marker. Ambiguous
    or empty output → False (the gate blocks on 'did not run', not on a fuzzy
    pass/fail read, so a real green run is never false-blocked)."""
    text = output if isinstance(output, str) else json.dumps(output, default=str)
    return bool(_FAIL_MARKERS.search(text or ""))


def is_verification_subtask(subtask: dict[str, Any]) -> bool:
    """True when the subtask is about running/verifying tests — the gated kind."""
    blob = " ".join(str(subtask.get(k, "")) for k in ("title", "description", "name"))
    return bool(_VERIFY_KEYWORDS.search(blob))


def _evidence_path(spec_dir: Path | str) -> Path:
    return Path(spec_dir) / _EVIDENCE_REL


def record_test_run(spec_dir: Path | str, command: str, output: Any) -> None:
    """Append one recorded test-command execution. Best-effort; never raises."""
    entry = {
        "ts": round(time.time(), 3),
        "command": command[:500],
        "failed": looks_failed(output),
    }
    try:
        path = _evidence_path(spec_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def read_test_evidence(spec_dir: Path | str) -> dict[str, Any]:
    """Summarise the recorded runs for the gate.

    Returns ``{"ran": bool, "last_failed": bool, "runs": int, "last_command":
    str|None}``. ``last_failed`` reflects only the most recent run, so a coder
    that fixed a failure and re-ran green is not held to the earlier failure.
    """
    path = _evidence_path(spec_dir)
    runs: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line:
                runs.append(json.loads(line))
    except (OSError, ValueError):
        pass
    last = runs[-1] if runs else None
    return {
        "ran": bool(runs),
        "last_failed": bool(last and last.get("failed")),
        "runs": len(runs),
        "last_command": last.get("command") if last else None,
    }


# ── #1111 deliverable-coverage: did the test exercise the SHIPPED artefact? ───

# HTTP paths named by a subtask. Three shapes, because the planner emits all
# three: ``POST /api/quote`` in a description, ``"url":
# "http://localhost:5000/api/analytics/events"`` in a verification block, and a
# bare ``/api/...`` in an acceptance criterion. Over-extraction is harmless —
# a path nothing mentions simply leaves the check inert (see
# deliverable_evidence_gap), so these lean permissive.
_METHOD_PATH = re.compile(
    r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b\s+(/[\w\-./{}]*)",
    re.IGNORECASE,
)
_URL_PATH = re.compile(r"https?://[^\s\"'`\\]*?(/[\w\-./{}]*)")
_BARE_PATH = re.compile(r"(?<![\w./])(/(?:api|v\d)/[\w\-./{}]*)", re.IGNORECASE)

# Modules a service is actually SERVED from. A test importing one of these is
# talking to the shipped application; a test importing only the feature module
# may be talking to an app that exists nowhere but the test file.
_ENTRYPOINT_MODULES = frozenset(
    {"main", "app", "application", "server", "api", "wsgi", "asgi"}
)
# ...and the app object it must pull out of them, so ``from app.main import
# settings`` is not mistaken for wiring up the real application.
_APP_NAMES = frozenset(
    {"app", "application", "api", "server", "create_app", "get_app", "make_app"}
)

_IMPORT_FROM = re.compile(r"^[ \t]*from[ \t]+([\w.]+)[ \t]+import[ \t]+([^\n#]+)", re.M)
_IMPORT_PLAIN = re.compile(r"^[ \t]*import[ \t]+([\w.]+)", re.M)

# In-test construction of a throwaway web application — the C2 shape exactly.
# Its presence means an assertion in this file may be about an app that is not
# the one the service serves, so the file is not accepted as proof on its own.
_LOCAL_APP = re.compile(
    r"\b(?:FastAPI|Flask|Starlette|Quart|Sanic|Bottle|Falcon|Chalice)\s*\("
)

# A test driving a real running server over the wire is exercising the shipped
# app by definition, whatever it imports.
_LIVE_SERVER = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)[:/]")

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".aifactory",
        ".worktrees",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
        "dist",
        "build",
    }
)

_MAX_TEST_FILES = 500
_MAX_TEST_BYTES = 400_000


def http_paths(subtask: dict[str, Any]) -> list[str]:
    """HTTP paths this subtask promises to deliver, from anywhere in its record.

    Serialising the whole subtask is deliberate: description, acceptance
    criteria and the planner's ``verification`` block all carry the path, and
    which one is populated varies by plan shape.
    """
    blob = json.dumps(subtask, default=str)
    found: list[str] = []
    for pattern in (_METHOD_PATH, _URL_PATH, _BARE_PATH):
        for raw in pattern.findall(blob):
            path = raw.rstrip("/.,;:)\"'").strip()
            if len(path) > 1 and path not in found:
                found.append(path)
    return found


def imports_app_entrypoint(text: str) -> bool:
    """True when ``text`` imports the application object from an entrypoint
    module (``from app.main import app``, ``import server``, …)."""
    for module, names in _IMPORT_FROM.findall(text):
        if module.rsplit(".", 1)[-1] in _ENTRYPOINT_MODULES and any(
            part.strip().split(" as ")[0].strip() in _APP_NAMES
            for part in names.replace("(", "").replace(")", "").split(",")
        ):
            return True
    return any(
        module.rsplit(".", 1)[-1] in _ENTRYPOINT_MODULES
        for module in _IMPORT_PLAIN.findall(text)
    )


def exercises_shipped_app(text: str) -> bool:
    """True when this test source reaches the app the service actually serves.

    Two ways: it imports the entrypoint and does not build a rival app of its
    own, or it calls a live server over HTTP. Importing the entrypoint *and*
    constructing a local app is not accepted — that is exactly the ambiguity
    #1111 is about, and the fix is one line in the test.
    """
    if _LIVE_SERVER.search(text):
        return True
    return imports_app_entrypoint(text) and not _LOCAL_APP.search(text)


def _python_test_files(project_dir: Path | str) -> list[Path]:
    """Python test modules under ``project_dir``, bounded so the gate stays cheap.

    ``os.walk`` rather than ``rglob`` so ``node_modules``/``.venv`` are pruned
    before they are descended — this runs on every subtask completion.
    """
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.startswith("test_") or name.endswith("_test.py"):
                if not name.endswith(".py"):
                    continue
                out.append(Path(dirpath) / name)
                if len(out) >= _MAX_TEST_FILES:
                    return out
    return out


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_TEST_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _with_conftests(path: Path, project_dir: Path) -> str:
    """A test file's source plus every ``conftest.py`` above it, so the standard
    fixture pattern (entrypoint imported in conftest) reads as honest."""
    chunks = [_read(path)]
    for parent in path.parents:
        chunks.append(_read(parent / "conftest.py"))
        if parent in (project_dir, parent.parent):
            break
    return "\n".join(chunks)


def deliverable_evidence_gap(
    subtask: dict[str, Any], project_dir: Path | str
) -> str | None:
    """Refusal reason when an HTTP subtask's tests never touch the shipped app.

    Returns ``None`` (complete freely) unless ALL of: the subtask names an HTTP
    path, at least one Python test file mentions that path, and not one of those
    files reaches the real application. See the module docstring for why each
    condition is required to keep the gate off correct work.
    """
    paths = http_paths(subtask)
    if not paths:
        return None  # not an HTTP deliverable — nothing to be hollow about

    root = Path(project_dir)
    hollow: list[str] = []
    for path in _python_test_files(root):
        text = _read(path)
        if not text or not any(p in text for p in paths):
            continue
        if exercises_shipped_app(_with_conftests(path, root)):
            return None  # real evidence exists; one honest file is enough
        try:
            hollow.append(str(path.relative_to(root)))
        except ValueError:  # pragma: no cover - path outside root
            hollow.append(path.name)

    if not hollow:
        # Nothing in Python mentions the path: a non-Python service, or no test
        # at all. Neither is this gate's question — the acceptance-criteria gate
        # and TFactory own "is there a test".
        # ponytail: Python-only on purpose. Extend per language only when a
        # second language actually produces this failure.
        return None

    return (
        f"Refused: this subtask delivers {', '.join(paths[:3])}, but the only tests "
        f"that mention it ({', '.join(sorted(hollow)[:3])}) never touch the shipped "
        "application. They import the feature module and assert against an app "
        "constructed inside the test file, so they would still pass if the route "
        "were never registered — which is how a green suite shipped a 404 (#1111). "
        "Import the real application entrypoint (e.g. `from app.main import app`) "
        "and assert through it, register the route there if it is not registered "
        "yet, re-run the tests, then complete this subtask. If the route genuinely "
        "cannot be reached, mark this subtask 'failed' with the reason rather than "
        "reporting unverified work complete (RFC-0006)."
    )


if __name__ == "__main__":  # pragma: no cover - runnable self-check
    import tempfile

    # is_test_command: real runs vs installs/reads
    assert is_test_command("pytest -q")
    assert is_test_command("cd api && go test ./...")
    assert is_test_command("python -m pytest tests/")
    assert not is_test_command("pip install pytest")
    assert not is_test_command("cat tests/test_foo.py")
    assert not is_test_command("echo running tests")
    assert not is_test_command("")

    # verification-subtask detection
    assert is_verification_subtask({"title": "Run all tests"})
    assert is_verification_subtask(
        {"description": "verify the implementation with pytest"}
    )
    assert not is_verification_subtask({"title": "Create strutil module"})

    # failure marker
    assert looks_failed("=== 2 failed, 1 passed ===")
    assert looks_failed("--- FAIL: TestX")
    assert not looks_failed("=== 5 passed in 0.1s ===")
    assert not looks_failed("ok  \tstrutil\t0.002s")

    # record + read roundtrip
    with tempfile.TemporaryDirectory() as d:
        assert read_test_evidence(d) == {
            "ran": False,
            "last_failed": False,
            "runs": 0,
            "last_command": None,
        }
        record_test_run(d, "pytest -q", "=== 5 passed ===")
        ev = read_test_evidence(d)
        assert ev["ran"] and not ev["last_failed"] and ev["runs"] == 1
        record_test_run(d, "pytest -q", "=== 1 failed ===")
        ev = read_test_evidence(d)
        assert ev["ran"] and ev["last_failed"] and ev["runs"] == 2

    # #1111 deliverable coverage — both directions, on the real C2 shape
    assert http_paths({"description": "Add POST /api/quote"}) == ["/api/quote"]
    assert http_paths({"verification": {"url": "http://localhost:5000/api/x"}}) == [
        "/api/x"
    ]
    assert http_paths({"description": "Create strutil.slugify"}) == []

    _HOLLOW = (
        "from fastapi import FastAPI\n"
        "from fastapi.testclient import TestClient\n"
        "from app.vat_quote import router\n"
        "_app = FastAPI()\n_app.include_router(router)\n"
        "client = TestClient(_app)\n"
        "def test_q():\n    assert client.post('/api/quote').status_code == 200\n"
    )
    _HONEST = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "client = TestClient(app)\n"
        "def test_q():\n    assert client.post('/api/quote').status_code == 200\n"
    )
    assert not exercises_shipped_app(_HOLLOW)
    assert exercises_shipped_app(_HONEST)

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "tests").mkdir()
        st = {"id": "1.1", "description": "Add POST /api/quote endpoint"}
        (root / "tests" / "test_vat_quote.py").write_text(_HOLLOW)
        assert deliverable_evidence_gap(st, root)  # C2's shape is refused
        (root / "tests" / "test_api.py").write_text(_HONEST)
        assert deliverable_evidence_gap(st, root) is None  # one honest file frees it
        # a subtask with no HTTP path is never judged
        assert deliverable_evidence_gap({"description": "Add slugify()"}, root) is None

    print("test_evidence self-check passed")  # noqa: T201
