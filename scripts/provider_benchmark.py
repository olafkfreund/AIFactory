#!/usr/bin/env python3
"""
Provider benchmark harness — compare build providers head-to-head.

Runs the SAME from-scratch task under different provider configurations and
records speed / cost / accuracy / finish-quality from each build's
``build_report.json``, so you can compare:

  * claude_only        — Claude Code + Claude models everywhere
  * antigravity_only   — Antigravity CLI (Gemini) everywhere
  * claude_antigravity — Claude plans/QAs, Antigravity codes
  * copilot_auto       — GitHub Copilot codes wherever it can, Claude plans/QAs

Design: the SPEC + PLAN phases run on Claude as a stable baseline; the
**coding (and QA)** phase is what each config varies — that's the heavy phase and
the fair thing to compare. The harness seeds ``task_metadata.phaseModels`` after
spec creation, then runs the build.

Provider availability is checked first: a config whose provider CLI isn't
installed/authed is SKIPPED with a clear message rather than failing mid-build.

Usage:
    python scripts/provider_benchmark.py --list
    python scripts/provider_benchmark.py --config claude_only
    python scripts/provider_benchmark.py --all            # runs every AVAILABLE config
    python scripts/provider_benchmark.py --all --rounds 3 # repeat for stability

One build runs at a time. Results append to scripts/benchmark_results.jsonl and a
comparison table is printed at the end.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "apps" / "backend"
PY = str((REPO / "apps" / "web-server" / ".venv" / "bin" / "python"))
RESULTS = REPO / "scripts" / "benchmark_results.jsonl"

# The fixed task every config builds (kept identical for a fair comparison).
TASK = (
    "Build a production-shaped FastAPI API gateway FROM SCRATCH with many "
    "independent modules, each in its own file: config (pydantic-settings), "
    "Pydantic v2 models, an API-key auth middleware, a request-logging "
    "middleware, a /health router, a /version router, an upstream proxy router, "
    "and main.py wiring it together. Use uv + pytest + ruff + mypy; every module "
    "has its own tests. Keep modules INDEPENDENT so they can be built in parallel."
)

# coding/qa are the phases under test; planning stays Claude as a baseline.
CONFIGS: dict[str, dict] = {
    "claude_only": {
        "label": "Claude only (Claude Code)",
        "phase_models": {"planning": "opus", "coding": "sonnet", "qa": "sonnet", "qa_fixer": "sonnet"},
    },
    "antigravity_only": {
        "label": "Antigravity only (Gemini CLI)",
        "phase_models": {"planning": "antigravity", "coding": "antigravity", "qa": "antigravity", "qa_fixer": "antigravity"},
    },
    "claude_antigravity": {
        "label": "Claude (plan/QA) + Antigravity (code)",
        "phase_models": {"planning": "opus", "coding": "antigravity", "qa": "sonnet", "qa_fixer": "sonnet"},
    },
    "copilot_auto": {
        "label": "Copilot (code) + Claude (plan/QA)",
        "phase_models": {"planning": "sonnet", "coding": "copilot:claude-sonnet-4.5", "qa": "sonnet", "qa_fixer": "sonnet"},
    },
}

WORKERS = 4
BUILD_TIMEOUT_S = 60 * 60  # 1h safety cap per build

# A registered project to build in (so tasks show up in the portal). Each config
# creates a fresh spec here; its build runs in an isolated worktree forked from
# the clean base branch, so configs stay comparable. Override with --project.
DEFAULT_PROJECT = "/mnt/data/Source-home/GitHub/aif-bench-gateway"


# ── provider availability ──────────────────────────────────────────────────

def _infer_provider(model: str) -> str:
    sys.path.insert(0, str(BACKEND))
    from phase_config import infer_provider_from_model
    return infer_provider_from_model(model)


def provider_available(provider: str) -> tuple[bool, str]:
    """Return (ok, reason). Only checks what we can verify locally."""
    if provider == "claude":
        return True, "ok"
    if provider == "antigravity":
        # Use the provider's own resolver so we detect the bundled
        # ~/.gemini/antigravity-cli/bin install, not just $PATH.
        try:
            sys.path.insert(0, str(BACKEND))
            from providers.antigravity_agentic import get_antigravity_binary
            resolved = get_antigravity_binary()
            found = (os.path.isabs(resolved) and Path(resolved).exists()) or bool(shutil.which(resolved))
        except Exception:
            found = bool(shutil.which("antigravity") or shutil.which("gemini"))
        if found:
            return True, "ok (antigravity CLI resolved; needs GEMINI auth)"
        return False, "antigravity/gemini CLI not installed (install + auth the Antigravity CLI)"
    if provider == "copilot":
        if shutil.which("copilot"):
            return True, "ok (ensure `copilot` is authenticated)"
        return False, "copilot CLI not installed"
    if provider == "codex":
        if shutil.which("codex"):
            return True, "ok"
        return False, "codex CLI not installed"
    return True, "assumed ok"


def config_availability(name: str) -> tuple[bool, list[str]]:
    cfg = CONFIGS[name]
    reasons: list[str] = []
    ok = True
    for phase, model in cfg["phase_models"].items():
        prov = _infer_provider(model)
        avail, why = provider_available(prov)
        if not avail:
            ok = False
            reasons.append(f"{phase}={model} ({prov}): {why}")
    return ok, reasons


# ── build run ──────────────────────────────────────────────────────────────

def _fresh_project(tag: str) -> Path:
    base = Path("/tmp") / f"aif-bench-{tag}-{int(time.time())}"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "bench@aifactory.local"], cwd=base)
    subprocess.run(["git", "config", "user.name", "AIFactory Bench"], cwd=base)
    (base / "README.md").write_text(f"# {tag} benchmark\n")
    subprocess.run(["git", "add", "."], cwd=base)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=base)
    return base


def _latest_spec_dir(project: Path) -> Path | None:
    specs = project / ".aifactory" / "specs"
    if not specs.exists():
        return None
    # Pick the real spec dir: it must carry requirements.json, and skip any
    # orphan "*-pending" stub. Choose the most recently modified match.
    cands = [
        d for d in specs.iterdir()
        if d.is_dir() and not d.name.endswith("-pending")
        and (d / "requirements.json").exists()
    ]
    if not cands:  # fall back to anything with requirements.json
        cands = [d for d in specs.iterdir()
                 if d.is_dir() and (d / "requirements.json").exists()]
    if not cands:  # last resort: any dir
        cands = [d for d in specs.iterdir() if d.is_dir()]
    if not cands:
        return None
    return max(cands, key=lambda d: d.stat().st_mtime)


def _seed_phase_models(spec_dir: Path, phase_models: dict) -> None:
    meta_file = spec_dir / "task_metadata.json"
    meta = {}
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
        except json.JSONDecodeError:
            meta = {}
    meta["isAutoProfile"] = True
    meta["phaseModels"] = phase_models
    meta["parallel"] = True
    meta["workers"] = WORKERS
    meta_file.write_text(json.dumps(meta, indent=2))


def run_config(name: str, project_dir: str | None = None) -> dict:
    cfg = CONFIGS[name]
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    # Use a registered project (visible in the portal) when given; else a fresh
    # throwaway /tmp repo.
    project = Path(project_dir) if project_dir else _fresh_project(name)
    started = time.time()
    result: dict = {
        "config": name, "label": cfg["label"], "project": str(project),
        "started_at": datetime.now().isoformat(),
    }

    # 1) spec ONLY (Claude baseline). --no-build is critical: without it
    # spec_runner os.execv's straight into run.py and builds inline with default
    # models — before we can seed phaseModels below. --auto-approve skips the
    # interactive review gate.
    spec = subprocess.run(
        [PY, str(BACKEND / "runners" / "spec_runner.py"),
         "--task", TASK, "--project-dir", str(project),
         "--complexity", "standard", "--no-ai-assessment",
         "--no-build", "--auto-approve"],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S,
    )
    spec_dir = _latest_spec_dir(project)
    if spec_dir is None:
        result.update(ok=False, error="spec creation produced no spec dir",
                      spec_stderr=spec.stderr[-2000:])
        return result
    result["spec_id"] = spec_dir.name

    # 2) seed the provider config for the build phase
    _seed_phase_models(spec_dir, cfg["phase_models"])

    # 3) build
    subprocess.run(
        [PY, str(BACKEND / "run.py"),
         "--spec", spec_dir.name, "--project-dir", str(project),
         "--auto-continue", "--force", "--parallel", "--workers", str(WORKERS)],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S,
    )

    # 4) collect metrics
    result["wall_s"] = round(time.time() - started, 1)
    plan_f = spec_dir / "implementation_plan.json"
    if plan_f.exists():
        plan = json.loads(plan_f.read_text())
        subs = [s for ph in plan.get("phases", []) for s in ph.get("subtasks", [])]
        result["subtasks_total"] = len(subs)
        result["subtasks_done"] = sum(1 for s in subs if s.get("status") == "completed")
        result["status"] = plan.get("status")
        result["review_reason"] = plan.get("reviewReason")
    br = spec_dir / "build_report.json"
    if br.exists():
        b = json.loads(br.read_text())
        for k in ("total_waves", "observed_max_concurrency", "parallel_wall_s",
                  "speedup_vs_serial", "cost_usd", "total_tokens", "qa_rounds"):
            result[k] = b.get(k)
    result["qa_report"] = (spec_dir / "qa_report.md").exists()
    result["ok"] = result.get("subtasks_total", 0) > 0 and \
        result.get("subtasks_done", 0) == result.get("subtasks_total", -1)
    return result


# ── cli ────────────────────────────────────────────────────────────────────

def _print_table(rows: list[dict]) -> None:
    cols = ["config", "ok", "wall_s", "subtasks_done", "subtasks_total",
            "cost_usd", "qa_rounds", "speedup_vs_serial", "status"]
    print("\n" + " | ".join(c.ljust(12) for c in cols))
    print("-" * (15 * len(cols)))
    for r in rows:
        print(" | ".join(str(r.get(c, "-")).ljust(12) for c in cols))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", choices=list(CONFIGS))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--project", default=DEFAULT_PROJECT,
                    help="Registered project dir to build in (shows in portal). "
                         "Pass '' for a throwaway /tmp repo per run.")
    args = ap.parse_args()

    if args.list or (not args.config and not args.all):
        print("Provider benchmark configs:\n")
        for name, cfg in CONFIGS.items():
            ok, reasons = config_availability(name)
            mark = "RUNNABLE " if ok else "BLOCKED  "
            print(f"  [{mark}] {name:20s} — {cfg['label']}")
            for r in reasons:
                print(f"               ↳ {r}")
        print("\nRun:  --config <name>   or   --all  (runs every RUNNABLE config)")
        return 0

    targets = [args.config] if args.config else list(CONFIGS)
    rows: list[dict] = []
    for _ in range(max(1, args.rounds)):
        for name in targets:
            ok, reasons = config_availability(name)
            if not ok:
                print(f"\n=== SKIP {name} (blocked) ===")
                for r in reasons:
                    print(f"  ↳ {r}")
                rows.append({"config": name, "ok": "SKIP", "status": "blocked"})
                continue
            print(f"\n=== RUN {name}: {CONFIGS[name]['label']} ===")
            res = run_config(name, args.project or None)
            RESULTS.parent.mkdir(parents=True, exist_ok=True)
            with RESULTS.open("a") as f:
                f.write(json.dumps(res) + "\n")
            rows.append(res)
            print(json.dumps({k: res.get(k) for k in
                  ("ok", "wall_s", "subtasks_done", "subtasks_total", "cost_usd",
                   "qa_rounds", "speedup_vs_serial", "status")}, indent=2))

    _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
