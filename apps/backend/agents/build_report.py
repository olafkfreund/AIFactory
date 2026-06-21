"""
Build Report — Profiling & Benchmark Artifact (Phase 1 of the self-healing
pipeline design; tracking #397)
==========================================================================

Every build emits ``build_report.json`` — a single greppable artifact that makes
a run measurable and comparable: wave structure + observed concurrency (from the
parallel wave events, #393), wall-clock and **speedup vs a serial baseline**,
cost and tokens by category (from token attribution, #262), and slots the later
self-healing phases fill in (QA rounds, checkpoints/rollbacks, self-heal events,
security findings, risk tier).

It reads the per-spec artifact files directly (``parallel_report.json``,
``token_usage.json``) rather than importing the producers, so this module is
self-contained and works whether the build ran serial (no parallel report) or in
waves. Pure aggregation — it never changes build behavior.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPORT_FILENAME = "build_report.json"
# Written by agents/wave_log.py (#393). Read directly to keep this module
# decoupled from the wave executor.
PARALLEL_REPORT_FILENAME = "parallel_report.json"
# Written by agents/token_attribution.py (#262).
USAGE_FILENAME = "token_usage.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def compute_speedup(
    parallel_wall_s: float, serial_baseline_s: float | None
) -> float | None:
    """Speedup = serial baseline / parallel wall-clock (None if not computable)."""
    if not serial_baseline_s or parallel_wall_s <= 0:
        return None
    return round(serial_baseline_s / parallel_wall_s, 2)


def _wave_summary(parallel_report: dict[str, Any] | None) -> dict[str, Any]:
    """Distil the parallel report into the build-report's wave section."""
    if not parallel_report:
        return {
            "parallel": False,
            "workers_max": 1,
            "total_waves": 0,
            "observed_max_concurrency": 1,
            "parallel_wall_s": 0.0,
            "waves": [],
        }
    waves = parallel_report.get("waves", []) or []
    wall = round(sum(float(w.get("duration_s", 0.0) or 0.0) for w in waves), 3)
    return {
        "parallel": bool(parallel_report.get("parallel", True)),
        "workers_max": parallel_report.get("workers_max", 1),
        "total_waves": parallel_report.get("total_waves", len(waves)),
        "observed_max_concurrency": parallel_report.get("observed_max_concurrency", 1),
        "parallel_wall_s": wall,
        "waves": [
            {
                "wave": w.get("wave"),
                "subtask_ids": w.get("subtask_ids", []),
                "concurrency": w.get("concurrency"),
                "duration_s": w.get("duration_s"),
            }
            for w in waves
        ],
    }


def _cost_summary(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Distil token_usage.json into the build-report's cost section."""
    if not usage:
        return {"cost_usd": 0.0, "total_tokens": 0, "tokens_by_category": {}}
    categories = usage.get("categories", {}) or {}
    return {
        "cost_usd": round(float(usage.get("totalCostUsd", 0.0) or 0.0), 6),
        "total_tokens": int(usage.get("totalTokens", 0) or 0),
        "tokens_by_category": {
            name: int(cat.get("tokens", 0) or 0)
            for name, cat in categories.items()
            if isinstance(cat, dict)
        },
    }


def build_report(
    spec_dir: Path,
    *,
    serial_baseline_s: float | None = None,
    extra: dict[str, Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Assemble the build report dict from the spec's artifact files.

    Args:
        spec_dir: Spec directory the build ran against.
        serial_baseline_s: Wall-clock of a comparable serial build, to compute
            speedup (e.g. the 2026-06-05 #376 baseline).
        extra: Fields the later self-healing phases contribute (qa_rounds,
            checkpoints, rollbacks, self_heal_events, security_findings,
            risk_tier). Merged over the defaults.
        now: ISO timestamp source (injectable for tests).
    """
    spec_dir = Path(spec_dir)
    waves = _wave_summary(_read_json(spec_dir / PARALLEL_REPORT_FILENAME))
    cost = _cost_summary(_read_json(spec_dir / USAGE_FILENAME))
    stamp = now() if callable(now) else datetime.now().isoformat()

    report: dict[str, Any] = {
        "spec": spec_dir.name,
        "parallel": waves["parallel"],
        "workers_max": waves["workers_max"],
        "total_waves": waves["total_waves"],
        "observed_max_concurrency": waves["observed_max_concurrency"],
        "parallel_wall_s": waves["parallel_wall_s"],
        "serial_baseline_s": serial_baseline_s,
        "speedup_vs_serial": compute_speedup(
            waves["parallel_wall_s"], serial_baseline_s
        ),
        "waves": waves["waves"],
        "cost_usd": cost["cost_usd"],
        "total_tokens": cost["total_tokens"],
        "tokens_by_category": cost["tokens_by_category"],
        # Slots populated by later self-healing phases (#397). Defaulted so the
        # report schema is stable from Phase 1.
        "qa_rounds": 0,
        "checkpoints": [],
        "rollbacks": [],
        "self_heal_events": [],
        "security_findings": [],
        "risk_tier": None,
        "updated_at": stamp,
    }
    if extra:
        report.update(extra)
    return report


def write_build_report(
    spec_dir: Path,
    *,
    serial_baseline_s: float | None = None,
    extra: dict[str, Any] | None = None,
    source_spec_dir: Path | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Build and persist build_report.json (mirrored to source spec for the UI)."""
    report = build_report(
        spec_dir, serial_baseline_s=serial_baseline_s, extra=extra, now=now
    )
    payload = json.dumps(report, indent=2)
    targets = [Path(spec_dir) / REPORT_FILENAME]
    if source_spec_dir and Path(source_spec_dir) != Path(spec_dir):
        targets.append(Path(source_spec_dir) / REPORT_FILENAME)
    for target in targets:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
        except OSError as exc:  # observability must never break a build
            logger.debug("build_report write failed (%s): %s", target, exc)
    return report


def load_build_report(spec_dir: Path) -> dict[str, Any] | None:
    """Read a persisted build_report.json (None if absent/invalid)."""
    return _read_json(Path(spec_dir) / REPORT_FILENAME)
