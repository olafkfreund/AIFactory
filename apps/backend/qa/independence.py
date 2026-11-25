"""Choose a QA verdict writer outside every coding and fixer identity."""

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from core.runtime_gating import is_runtime_enabled
from model_registry import ModelNotApprovedError, check_model_registered
from phase_config import (
    _resolve_phase_model,
    get_provider_extra_kwargs,
    infer_provider_from_model,
    resolve_model_id,
)

log = logging.getLogger(__name__)


class IndependentReviewUnavailable(RuntimeError):
    """No configured, eligible, independent QA model exists."""


@dataclass(frozen=True)
class Pick:
    model: str
    provider: str
    override: str | None = None


def is_configured(model: str) -> bool:
    """Check runtime availability without reading credential values."""
    provider = infer_provider_from_model(model)
    if provider == "claude":
        return shutil.which("claude") is not None and (
            "CLAUDE_CODE_OAUTH_TOKEN" in os.environ
            or "ANTHROPIC_API_KEY" in os.environ
            or (Path.home() / ".claude/.credentials.json").exists()
        )
    if provider == "codex":
        return shutil.which("codex") is not None and (
            "CODEX_API_KEY" in os.environ
            or "OPENAI_API_KEY" in os.environ
            or (Path.home() / ".codex/auth.json").exists()
        )
    if provider == "antigravity":
        return shutil.which("antigravity") is not None and (
            "GEMINI_API_KEY" in os.environ
            or "GOOGLE_API_KEY" in os.environ
            or (Path.home() / ".gemini/oauth_creds.json").exists()
        )
    if provider == "openai-compatible":
        try:
            extra = get_provider_extra_kwargs(provider, model)
        except ValueError:
            return False
        if model.lower().startswith("studio:"):
            return bool(extra.get("api_key"))
        return bool(
            extra.get("api_key")
            or (extra.get("base_url") and extra["base_url"] != "https://api.openai.com")
            or os.environ.get("OPENAI_COMPATIBLE_BASE_URL")
        )
    if provider == "ollama":
        return bool(os.environ.get("OLLAMA_BASE_URL"))
    return False


def pick_reviewer(gates, identities: list[dict], *, configured=is_configured) -> Pick:
    """Select a distinct QA model, preferring a different configured provider."""
    try:
        qa_model = gates.model("qa")
    except ModelNotApprovedError:
        qa_model = _resolve_phase_model(
            Path(os.devnull), "qa", gates.cli_model, metadata=gates.metadata
        )
    identities = [i for i in identities if i.get("phase") != "planning"]
    if any(not i.get("model") for i in identities):
        raise IndependentReviewUnavailable(
            "A coding or fixer model identity is unknown"
        )
    coding_models = {
        resolve_model_id(i["served_model"] or i["model"])
        for i in identities
        if i.get("served_model") or i.get("model")
    }
    for phase in ("coding", "qa_fixer"):
        try:
            coding_models.add(resolve_model_id(gates.model(phase)))
        except ModelNotApprovedError:
            pass
    plan = gates.approved_plan if hasattr(gates, "approved_plan") else None
    if isinstance(plan, dict):
        phases = plan.get("phases")
        for phase in phases if isinstance(phases, list) else []:
            if isinstance(phase, dict):
                subtasks = phase.get("subtasks")
                for subtask in subtasks if isinstance(subtasks, list) else []:
                    if isinstance(subtask, dict) and isinstance(
                        subtask.get("model"), str
                    ):
                        coding_models.add(resolve_model_id(subtask["model"]))
    coding_providers = {i.get("provider") for i in identities}
    routes = gates.metadata.get("phaseRoutes") or {}
    qa_route = routes.get("qa")
    coding_routes = {routes.get(i.get("phase")) for i in identities}
    candidates = [qa_model]
    catalog = json.loads(
        (Path(__file__).parent.parent / "core/model-catalog.json").read_text()
    )["models"]
    candidates.extend(
        model
        for model, entry in catalog.items()
        if entry.get("class") == "frontier"
        and entry.get("provider") not in coding_providers
        and "qa" in entry.get("roles", [])
    )
    # Other-provider reviewers come from operator config: the registry passes
    # any non-Claude name, so never guess model ids here.
    candidates.extend(
        m.strip()
        for m in os.environ.get("AIFACTORY_QA_ALT_MODELS", "").split(",")
        if m.strip()
    )
    candidates.extend(("opus", "fable", "sonnet"))
    eligible = []
    for candidate in dict.fromkeys(candidates):
        provider = infer_provider_from_model(candidate)
        if (
            not is_runtime_enabled(provider)
            or not check_model_registered(candidate, "qa")[0]
            or not configured(candidate)
        ):
            continue
        if resolve_model_id(candidate) in coding_models:
            continue
        if (
            candidate == qa_model
            and qa_route
            and (
                qa_route in coding_routes
                or any(
                    i.get("provider") == provider and not i.get("served_model")
                    for i in identities
                )
            )
        ):
            continue
        eligible.append(Pick(candidate, provider))
    if not eligible:
        raise IndependentReviewUnavailable(
            "No configured, registry-eligible model is independent of coding and fixer sessions"
        )
    picked = next(
        (p for p in eligible if p.provider not in coding_providers), eligible[0]
    )
    if picked.model != qa_model:
        reason = (
            f"QA model {qa_model} replaced by {picked.model} for independent review"
        )
        log.warning("[QA independence] %s", reason)
        return Pick(picked.model, picked.provider, reason)
    return picked
