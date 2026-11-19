"""Extract counts only from recognizable test runner output."""

import re

_PYTEST = re.compile(
    r"^=+\s+(?:\d+\s+(?:passed|failed|skipped|errors?|xfailed|xpassed|warnings?|deselected)(?:,\s*|\s+))+"
    r"in\s+\d+(?:\.\d+)?s\s+=+$"
)
_PYTEST_COUNT = re.compile(r"(\d+)\s+(passed|failed|skipped|error|errors)")
_JEST = re.compile(r"^Tests:\s+(.+)$")
_VITEST = re.compile(r"^\s*Tests\s+(.+?)\s+\(\d+\)(?:\s|$)")
_JS_COUNT = re.compile(r"(\d+)\s+(passed|failed|skipped|todo)")
_GO = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+Test\S+")
_CARGO = re.compile(
    r"^test result: (?:ok|FAILED)\.\s+(\d+) passed; (\d+) failed; (\d+) ignored;"
)
_SUREFIRE = re.compile(
    r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)"
)


def parse(log_text: str, truncated: bool) -> dict[str, int] | dict[str, bool]:  # noqa: PLR0912 - Runner formats need distinct checks.
    """Return passed/failed/skipped counts, or mark unrecognized output."""
    lines = log_text.splitlines()
    for line in reversed(lines):
        match = _SUREFIRE.match(line.strip())
        if match:
            total, failures, errors, skipped = map(int, match.groups())
            if total >= failures + errors + skipped:
                return {
                    "passed": total - failures - errors - skipped,
                    "failed": failures + errors,
                    "skipped": skipped,
                }
        match = _CARGO.match(line.strip())
        if match:
            passed, failed, skipped = map(int, match.groups())
            return {"passed": passed, "failed": failed, "skipped": skipped}
        if _PYTEST.fullmatch(line.strip()):
            counts = dict.fromkeys(("passed", "failed", "skipped"), 0)
            for value, kind in _PYTEST_COUNT.findall(line):
                counts["failed" if kind in ("error", "errors") else kind] += int(value)
            if any(counts.values()):
                return counts
        match = _JEST.match(line.strip()) or _VITEST.match(line)
        if match:
            counts = dict.fromkeys(("passed", "failed", "skipped"), 0)
            for value, kind in _JS_COUNT.findall(match.group(1)):
                counts["skipped" if kind == "todo" else kind] += int(value)
            if any(counts.values()):
                return counts

    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for line in lines:
        match = _GO.match(line)
        if match:
            counts[
                {"PASS": "passed", "FAIL": "failed", "SKIP": "skipped"}[match.group(1)]
            ] += 1
    if any(counts.values()) and not truncated:
        return counts
    return {"parsed": False}
