# python

> Source: curated best practices | 2026

---

# Python - Idiomatic, typed, stdlib-first application code

This skill equips the coder to write modern Python 3.12+ that is fully type-annotated, uses the standard library before reaching for dependencies, and ships with tests. It assumes a `pyproject.toml`-managed project, `ruff` for lint+format, `mypy` (or `pyright`) for static typing, and `pytest` for tests. It enforces small pure functions, explicit error handling, dataclasses over dicts for structured data, and pathlib over os.path.

## When to Activate

Use when the task involves Python:
- Writing or modifying `.py` modules, packages, or scripts
- Building CLIs, web backends (FastAPI/Flask/Django), data pipelines, or automation
- Anything referencing `pyproject.toml`, `requirements.txt`, `pytest`, `ruff`, `mypy`, `venv`

## Idioms and Best Practices

**Project layout** (src layout, avoids import shadowing):
```
myproj/
  pyproject.toml
  src/myproj/__init__.py
  src/myproj/core.py
  tests/test_core.py
```
Set `[tool.pytest.ini_options] pythonpath = ["src"]` or install editable (`pip install -e .`).

**Type everything.** Use built-in generics and the `|` union (3.10+):
```python
def totals(rows: list[dict[str, int]]) -> dict[str, int] | None:
    ...
```
Prefer `collections.abc` for parameters (`Iterable`, `Mapping`, `Sequence`) and concrete types for returns.

**Structured data = dataclasses**, not bare dicts/tuples:
```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float
```
`frozen=True` for value objects, `slots=True` to cut memory and catch typos.

**Error handling: narrow and explicit.** Never bare `except:`. Raise specific exceptions; define a small exception hierarchy per package:
```python
class ConfigError(Exception): ...

def load(path: Path) -> Config:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ConfigError(f"missing config: {path}") from e
```
Use `raise ... from e` to preserve the cause. Prefer `contextlib.suppress(FileNotFoundError)` over try/except/pass.

**Paths and files:** `pathlib.Path`, not `os.path`. Always pass `encoding="utf-8"`. Use context managers for anything with a `close`.

**Comprehensions and generators** over manual loops for transforms; generators for large/streaming data:
```python
names = [u.name for u in users if u.active]
total = sum(line.amount for line in stream)   # no intermediate list
```

**Standard library first:** `functools.lru_cache`/`cache` for memoization, `itertools` for combinatorics, `collections.Counter`/`defaultdict`, `datetime` with `timezone.utc` (never naive datetimes at boundaries), `json`, `sqlite3`, `concurrent.futures` for parallelism, `argparse` for CLIs.

**Testing with pytest:**
```python
import pytest
from myproj.core import totals

def test_totals_sums_by_key():
    assert totals([{"a": 1}, {"a": 2}]) == {"a": 3}

def test_totals_empty_returns_none():
    assert totals([]) is None

@pytest.mark.parametrize("bad", [None, "x", 3])
def test_totals_rejects_non_list(bad):
    with pytest.raises(TypeError):
        totals(bad)
```
Use fixtures for setup, `tmp_path` for filesystem tests, `monkeypatch` for env/attr patching. Keep tests fast and isolated; no network.

**Concurrency:** use `asyncio` for I/O-bound concurrency (async web clients, DBs); `ThreadPoolExecutor` for blocking I/O you cannot make async; `ProcessPoolExecutor` for CPU-bound work (the GIL still matters in 3.12). Do not spawn raw threads without a pool.

**Formatting/lint:** `ruff format` and `ruff check --fix`. Configure in `pyproject.toml`:
```toml
[tool.ruff]
line-length = 100
target-version = "py312"
[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
```
Run `mypy --strict src/` and keep it green.

**Logging:** `logging.getLogger(__name__)`, never `print` in library code. Configure handlers only at the app entry point.

## Anti-patterns

- Mutable default arguments (`def f(x=[])`) - use `None` and assign inside.
- Bare `except:` or `except Exception: pass` that swallows errors silently.
- `from module import *` - it pollutes the namespace and breaks tooling.
- Using dicts/tuples as ad-hoc records where a dataclass belongs.
- Naive datetimes crossing boundaries - always attach `timezone.utc`.
- Reaching for `requests`/`pandas` when `urllib`/`csv`/`sqlite3` covers it.
- String-concatenating paths instead of `Path(...) / "sub"`.
- `os.system`/`shell=True` with interpolated input - use `subprocess.run([...])` with a list.
