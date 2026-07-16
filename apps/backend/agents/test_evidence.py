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

    print("test_evidence self-check passed")  # noqa: T201
