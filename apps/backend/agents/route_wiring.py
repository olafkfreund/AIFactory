"""Post-merge route wiring: does the SHIPPED app serve what the plan promised? (#1123)

#1111 closed the per-subtask hole — a coder can no longer complete a subtask
that names an HTTP path when the only tests naming it assert against a
``FastAPI()`` built inside the test file. It is evaluated against the worktree
the subtask is working in, which in a parallel wave is *not* the merged tree, so
two things stay invisible to it:

1. **Cross-worker breakage.** Worker A's honest test passes in A's tree. Worker
   B merges later and changes the entrypoint in a way that breaks A's route.
   Nobody re-runs A's test against the merge.
2. **Wiring that only exists post-merge.** When the planner splits "write the
   module" and "wire it up" across workers, the writing worker legitimately
   cannot import a registration that has not landed yet.

Measured on the merged ``aifactory/101-vat-quote-endpoint-with-half-u`` tree:
the suite passes **91/91 both with and without** ``app.include_router(
vat_quote_router)``. Every test naming ``/api/quote`` builds its own throwaway
app, so deleting the one line that makes the endpoint reachable changes nothing.
A feature shipped with zero coverage of the endpoint anyone can actually call.

This module is the wave-level counterpart: **one** check, run once against the
tree that ships, that imports the real entrypoint and asserts every path the
plan promised resolves on it. Static analysis was rightly rejected for #1111's
per-subtask gate (the app may not be importable mid-subtask); by the trailing
gates the merge is done and importing it is exactly what a user would do.

It rides the existing trailing-gate machinery (:mod:`agents.gate_runner`) as one
more :class:`~agents.gate_runner.Gate`, so it inherits the host / factory-sandbox
/ k8s-Job / Nix-devshell runners — the check runs in the same environment the
project's own ``pytest`` gate runs in, which is the difference between a gate
that works in production and one that is permanently inert.

Kept off correct work, because a gate that fires on correct work gets switched
off within a week and then there is no gate at all:

* No HTTP path in the plan -> no gate emitted. CLI tools, libraries and schema
  work are never judged by it.
* No test in the tree names an application entrypoint -> no gate emitted. We do
  not guess an import; a non-Python or unconventional service is simply not this
  check's business.
* Cannot import, app exposes no routes -> exit ``SKIP_EXIT_CODE``, reported
  *skipped*, never *passed*. A check that did not run must read differently from
  one that ran clean, or a dead lane is quieter than a failing one.
* Only a path that resolves nowhere on the real app fails, and a failure lands
  in ``GATE_FAILURES.md`` for the QA/fix loop exactly as the ``mypy``/``pytest``
  gates and the #601/#611f guards do — loud, and fixable in the same build.

Shares the single escape hatch ``AIFACTORY_TEST_EVIDENCE_GATE=off`` with #851
and #1111; no second flag.
"""

from __future__ import annotations

import json
from pathlib import Path

from .gate_runner import Gate
from .test_evidence import discover_app_entrypoint, gate_enabled, http_paths

__all__ = ["SKIP_EXIT_CODE", "route_wiring_gate"]

# The probe's "I could not determine anything" exit. Distinct from 0 (verified
# clean) and 1 (a promised path is unreachable) so run_gates can report it as
# skipped rather than passed.
SKIP_EXIT_CODE = 77

GATE_NAME = "route-wiring"

# Runs in the PROJECT's interpreter, not ours — stdlib only, no imports from
# this repo, and it must never raise past its own handlers.
_PROBE = r'''
import os, sys

# The two layouts that exist: flat (``app/main.py``) and src (``src/app/main.py``).
# pytest gets src on the path from the project's conftest; a bare interpreter
# does not, so put both there and let the import decide.
_cwd = os.getcwd()
sys.path[:0] = [_cwd, os.path.join(_cwd, "src")]

target, wanted = sys.argv[1], sys.argv[2:]
module, _, name = target.partition(":")


def _skip(reason):
    print("route-wiring: NOT CHECKED - " + reason)
    raise SystemExit(__SKIP_EXIT_CODE__)


try:
    obj = getattr(__import__(module, fromlist=[name]), name)
except Exception as exc:
    _skip("cannot import %s (%s: %s)" % (target, type(exc).__name__, exc))

# ``create_app``/``get_app`` factories: call once to get the application.
if callable(obj) and not hasattr(obj, "routes") and not hasattr(obj, "url_map"):
    try:
        obj = obj()
    except Exception as exc:
        _skip("%s() raised %s: %s" % (target, type(exc).__name__, exc))

def _flatten(node, prefix=""):
    """Every path under ``node``, descending into mounts and included routers.

    FastAPI 0.141 stopped flattening ``include_router`` into ``app.routes`` and
    keeps an opaque ``_IncludedRouter`` instead, so reading ``app.routes`` alone
    silently loses every mounted route — which would fail exactly the builds this
    gate exists to pass. Follow whatever sub-router the object exposes.
    """
    found = []
    for route in getattr(node, "routes", None) or []:
        path = getattr(route, "path", None) or getattr(route, "path_format", "") or ""
        sub = route
        if getattr(sub, "routes", None) is None:
            sub = getattr(route, "router", None) or getattr(
                route, "original_router", None
            )
        if sub is not None and getattr(sub, "routes", None) is not None:
            found += _flatten(sub, prefix + path + (getattr(route, "prefix", "") or ""))
        elif path:
            found.append(prefix + path)
    return found


routes = []
if callable(getattr(obj, "openapi", None)):
    # FastAPI's own answer to "what do I serve", stable across versions.
    try:
        routes += list((obj.openapi() or {}).get("paths") or {})
    except Exception:
        pass
try:
    # Union, not fallback: catches Starlette-only apps and routes excluded from
    # the schema. Over-collecting here can only make the gate more permissive.
    routes += _flatten(obj)
except Exception:
    pass
if not routes and hasattr(obj, "url_map"):  # Flask
    routes = [str(r.rule) for r in obj.url_map.iter_rules()]
routes = sorted({r for r in routes if r})
if not routes:
    _skip("%s exposes no inspectable routes" % target)


def _segments(path):
    return [s for s in path.strip("/").split("/") if s]


def _served(want):
    w = _segments(want)
    for route in routes:
        r = _segments(route)
        if len(r) == len(w) and all(
            b.startswith("{") or b.startswith("<") or a.lower() == b.lower()
            for a, b in zip(w, r)
        ):
            return True
    return False


missing = [p for p in wanted if not _served(p)]
if missing:
    print("The merged tree's %s does not serve: %s" % (target, ", ".join(missing)))
    print("It serves: %s" % ", ".join(routes))
    print(
        "The plan promises these paths but the shipped application does not "
        "expose them, so nothing a user can call reaches them - a suite that "
        "tests them through an app built inside a test file would still be "
        "green (#1123). Register the route(s) on %s and re-run." % target
    )
    raise SystemExit(1)

print("route-wiring: %s serves all %d promised path(s)" % (target, len(wanted)))
'''.replace("__SKIP_EXIT_CODE__", str(SKIP_EXIT_CODE))


def route_wiring_gate(plan_path: Path | str, project_dir: Path | str) -> Gate | None:
    """The #1123 gate for this build, or ``None`` when there is nothing to check.

    ``None`` (no gate at all) is the answer whenever the question does not apply:
    the gate is disabled, the plan is unreadable, the plan promises no HTTP path,
    or the tree's own tests never name an application entrypoint. Conditions that
    only become visible once the probe runs report *skipped* instead — see the
    module docstring for why the two are not the same thing.
    """
    if not gate_enabled():
        return None
    try:
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    # The whole plan as one blob: http_paths already serialises whatever it is
    # given, and the union over every subtask is exactly what the wave promised.
    paths = http_paths(plan, local_urls_only=True)
    if not paths:
        return None

    target = discover_app_entrypoint(project_dir)
    if target is None:
        return None

    return Gate(
        GATE_NAME,
        ["python3", "-c", _PROBE, target, *paths],
        skip_code=SKIP_EXIT_CODE,
    )
