"""D1 independent-review gate for every PR entry point (#2233)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Literal

_BACKEND_PATH = Path(__file__).parent.parent.parent.parent / "backend"
if str(_BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(_BACKEND_PATH))

from agents.model_identity import read_identities  # noqa: E402
from phase_config import resolve_model_id  # noqa: E402

GateReason = Literal["qa_missing", "qa_stale", "qa_not_independent", "head_unavailable"]
GATE_REASONS: frozenset[str] = frozenset(
    {
        "qa_missing",
        "qa_stale",
        "qa_not_independent",
        "head_unavailable",
    }
)


def _canonical_model(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    raw = entry.get("served_model") or entry.get("model")
    if not isinstance(raw, str) or not raw.strip():
        return None
    resolved = resolve_model_id(raw.strip())
    return resolved if isinstance(resolved, str) and resolved.strip() else None


def check(source_spec_dir: Path, head_sha: str | None) -> GateReason | None:  # noqa: PLR0911, PLR0912 - explicit refusal reasons
    """Return None when QA approved *head_sha* independently, else the refusal reason."""
    if not isinstance(head_sha, str) or not head_sha.strip():
        return "head_unavailable"
    spec_dir = Path(source_spec_dir)
    try:
        evidence = json.loads((spec_dir / "qa_evidence.json").read_text("utf-8"))
    except (OSError, ValueError):
        return "qa_missing"
    if not isinstance(evidence, dict) or evidence.get("status") != "approved":
        return "qa_missing"

    reviewed_head = evidence.get("reviewed_head")
    report_sha256 = evidence.get("report_sha256")
    if (
        not isinstance(reviewed_head, str)
        or not reviewed_head.strip()
        or reviewed_head.strip() != head_sha.strip()
        or not isinstance(report_sha256, str)
        or not report_sha256.strip()
    ):
        return "qa_stale"
    try:
        report_bytes = (spec_dir / "qa_evidence_report.md").read_bytes()
    except OSError:
        return "qa_stale"
    if hashlib.sha256(report_bytes).hexdigest() != report_sha256.strip():
        return "qa_stale"

    reviewer_model = _canonical_model(evidence.get("reviewer"))
    if reviewer_model is None:
        return "qa_not_independent"
    try:
        identities = read_identities(spec_dir)
    except (OSError, ValueError):
        return "qa_not_independent"
    if not identities:
        return "qa_not_independent"
    coding_models: set[str] = set()
    for item in identities:
        if isinstance(item, dict) and item.get("phase") == "planning":
            continue
        model = _canonical_model(item)
        if model is None:
            return "qa_not_independent"
        coding_models.add(model)
    if not coding_models:
        return "qa_not_independent"
    if reviewer_model in coding_models:
        return "qa_not_independent"
    return None
