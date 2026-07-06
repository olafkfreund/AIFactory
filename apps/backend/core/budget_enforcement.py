"""RFC-0014 §7 budget enforcement (operator-gated, pre-execution).

``execution.budget_usd`` / ``execution.routing.cost_ceiling_usd`` are
**observe-only** by default — today's behaviour (``trusted_plan`` carries
``budget_usd`` into task metadata and ``completion`` surfaces a post-hoc
``usage.budget`` warning when the rolled-up actual spend exceeds it). RFC-0014
adds ``execution.budget_mode ∈ {observe, enforce}``:

* ``observe`` (default) — exactly today: estimate is informational, the build
  always proceeds. No behaviour change.
* ``enforce`` (operator-gated) — BEFORE the build, the per-role phase models are
  priced from the hub model catalog (``cost_router_core.estimate_cost`` +
  ``model-catalog.json``) and compared to the ceiling. If the estimate exceeds
  the ceiling the router first tries to **downgrade within the capability floor**
  (RFC-0011 difficulty tier → floor class), and if even the floor-respecting
  selection is over budget it **fails fast with a named reason**. A ``governed``
  task is NEVER silently degraded (RFC-0014 §7): an over-budget governed plan
  fails fast rather than dropping below its frontier planning floor.

This is a pure decision core: catalog + parsed ``execution`` block in, a typed
``BudgetDecision`` out. It performs no provider construction and no file I/O
beyond ``load_catalog`` (delegated to the vendored ``cost_router_core``). The
executor consults it after ``apply_execution_profile`` and before dispatch.

Run directly for the self-tests: ``python3 core/budget_enforcement.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core import cost_router_core as crc

# The parsed Task Contract ``execution`` block. Loose by design (it carries
# free-form contract fields); typed here once so the strict bar is satisfied
# without sprinkling ``dict[str, Any]`` through every signature.
Execution = dict[str, Any]

# The default catalog ships alongside this module (vendored from the hub, #664).
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent / "model-catalog.json"

# Roles priced for a build. ``planning`` is the governance-sensitive role whose
# floor ``governed`` routing forces to frontier. Mirrors model-catalog roles.
_PRICED_ROLES: tuple[str, ...] = ("planning", "coding", "qa", "test_gen")

# Per-role nominal token estimate (in, out) used when the contract gives no
# measured estimate. Deliberately coarse: enforcement compares a same-basis
# estimate to a same-basis ceiling the planner produced, so the absolute number
# matters less than that planning is weighted heavier than mechanical roles.
_ROLE_TOKENS: dict[str, tuple[int, int]] = {
    "planning": (60_000, 40_000),
    "coding": (120_000, 120_000),
    "qa": (60_000, 30_000),
    "test_gen": (60_000, 60_000),
}

BudgetMode = Literal["observe", "enforce"]

# Resolution outcomes for a build's budget decision.
_OUTCOME_OBSERVE = "observe"
_OUTCOME_WITHIN_BUDGET = "within_budget"
_OUTCOME_DOWNGRADED = "downgraded"
_OUTCOME_FAIL_FAST = "fail_fast"


def parse_budget_mode(execution: Execution | None) -> BudgetMode:
    """Read ``execution.budget_mode``; default ``observe`` (today's behaviour).

    Any value other than the literal ``enforce`` resolves to ``observe`` so an
    unknown/typo'd mode can never silently start enforcing (or, worse, refusing)
    builds — enforcement must be opted into explicitly.
    """
    if not isinstance(execution, dict):
        return "observe"
    return "enforce" if execution.get("budget_mode") == "enforce" else "observe"


def _routing(execution: Execution | None) -> Execution:
    if not isinstance(execution, dict):
        return {}
    routing = execution.get("routing")
    return routing if isinstance(routing, dict) else {}


def cost_ceiling_usd(execution: Execution | None) -> float | None:
    """Resolve the cost ceiling: ``execution.routing.cost_ceiling_usd`` first,
    then the legacy flat ``execution.budget_usd``. None ⇒ no ceiling."""
    routing = _routing(execution)
    for source, key in ((routing, "cost_ceiling_usd"), (execution or {}, "budget_usd")):
        val = source.get(key) if isinstance(source, dict) else None
        if isinstance(val, (int, float)):
            return float(val)
    return None


def routing_class(execution: Execution | None) -> str:
    """Resolve the RFC-0014 routing class (``economy``/``standard``/``premium``/
    ``governed``); ``standard`` when unset."""
    cls = _routing(execution).get("class")
    return cls if isinstance(cls, str) and cls else "standard"


def difficulty_tier(execution: Execution | None) -> str:
    """Resolve the RFC-0011 difficulty tier driving the capability floor.

    Looks at ``execution.routing.difficulty`` then ``execution.difficulty`` then
    ``execution.complexity``; unknown/absent ⇒ ``medium`` (the catalog router
    treats unknown tiers as the balanced floor, never below).
    """
    routing = _routing(execution)
    for source, key in (
        (routing, "difficulty"),
        (execution or {}, "difficulty"),
        (execution or {}, "complexity"),
    ):
        val = source.get(key) if isinstance(source, dict) else None
        if isinstance(val, str) and val:
            return val
    return "medium"


def _phase_models(execution: Execution | None) -> dict[str, str]:
    if not isinstance(execution, dict):
        return {}
    raw = execution.get("phase_models")
    if not isinstance(raw, dict):
        return {}
    return {
        str(role): str(model) for role, model in raw.items() if isinstance(model, str)
    }


# Shorthand → catalog id, mirroring phase_config.MODEL_ID_MAP for the common pins
# (the catalog keys on full ids). Unknown shorthands pass through unchanged so an
# already-full id (or a local ``ollama:<model>`` form) still resolves.
_SHORTHAND_TO_CATALOG: dict[str, str] = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _catalog_id(model: str) -> str:
    name = model.strip()
    if name in _SHORTHAND_TO_CATALOG:
        return _SHORTHAND_TO_CATALOG[name]
    # Local/subscription families key on a ``provider:<model>`` placeholder.
    for prefix in ("ollama:", "ollama-cloud:"):
        if name.startswith(prefix):
            return f"{prefix}<model>"
    return name


def estimate_build_cost(
    execution: Execution | None,
    catalog: dict[str, crc.ModelEntry],
) -> tuple[float | None, dict[str, float | None]]:
    """Estimate the total + per-role USD cost of a build's phase models.

    Each role's model is priced at its nominal token weight. Roles whose model is
    subscription/local/unknown contribute no ``$`` (None per role); the total is
    the sum of the metered roles, or None when NO role yields a ``$`` estimate
    (mirrors CFactory billing-mode display — a fully subscription/local build has
    no dollar estimate, only tokens/time).
    """
    phase_models = _phase_models(execution)
    per_role: dict[str, float | None] = {}
    total: float | None = None
    for role in _PRICED_ROLES:
        model = phase_models.get(role)
        if model is None:
            continue
        in_tok, out_tok = _ROLE_TOKENS.get(role, (50_000, 50_000))
        cost = crc.estimate_cost(_catalog_id(model), in_tok, out_tok, catalog)
        per_role[role] = cost
        if cost is not None:
            total = cost if total is None else total + cost
    return total, per_role


@dataclass(frozen=True)
class BudgetDecision:
    """The outcome of a pre-execution budget check.

    ``proceed`` is the executor's go/no-go. ``outcome`` is a stable tag
    (``observe`` / ``within_budget`` / ``downgraded`` / ``fail_fast``).
    ``reason`` is a human-readable, NAMED explanation — always populated on a
    refusal so a blocked build is never mysterious. ``phase_models`` is the
    (possibly downgraded) per-role model map the executor should use.
    """

    proceed: bool
    outcome: str
    mode: BudgetMode
    reason: str
    estimate_usd: float | None
    ceiling_usd: float | None
    phase_models: dict[str, str] = field(default_factory=dict)
    per_role_usd: dict[str, float | None] = field(default_factory=dict)


def _downgrade_within_floor(
    execution: Execution,
    catalog: dict[str, crc.ModelEntry],
) -> dict[str, str] | None:
    """Try to re-pick each role's model as the cheapest catalog model at/above
    its capability floor. Returns a new phase_models map, or None if any role has
    no catalog candidate at its floor (caller then fails fast).

    ``planning``'s floor is raised to frontier for a ``governed`` task, so a
    governed plan can NEVER be downgraded below frontier (RFC-0014 §7).
    """
    tier = difficulty_tier(execution)
    cls = routing_class(execution)
    current = _phase_models(execution)
    out: dict[str, str] = {}
    for role in _PRICED_ROLES:
        if role not in current:
            continue
        floor = (
            crc.planning_floor_class(tier, cls)
            if role == "planning"
            else crc.capability_floor_class(tier)
        )
        in_tok, out_tok = _ROLE_TOKENS.get(role, (50_000, 50_000))
        pick = crc.cheapest_capable_model(role, floor, None, (in_tok, out_tok), catalog)
        if pick is None:
            return None
        out[role] = pick
    return out


def enforce_budget(
    execution: Execution | None,
    *,
    catalog: dict[str, crc.ModelEntry] | None = None,
    catalog_path: str | Path | None = None,
) -> BudgetDecision:
    """Decide go/no-go for a build under RFC-0014 §7 budget rules.

    ``observe`` (default) always proceeds with the contract's phase models — the
    estimate is informational only (no behaviour change vs today). ``enforce``
    prices the build and, if over the ceiling, downgrades within the capability
    floor or fails fast with a named reason; a ``governed`` task that cannot fit
    fails fast rather than dropping below its frontier planning floor.
    """
    execution = execution if isinstance(execution, dict) else {}
    mode = parse_budget_mode(execution)
    ceiling = cost_ceiling_usd(execution)
    if catalog is None:
        catalog = crc.load_catalog(catalog_path or DEFAULT_CATALOG_PATH)

    estimate, per_role = estimate_build_cost(execution, catalog)
    current_models = _phase_models(execution)

    if mode == "observe":
        return BudgetDecision(
            proceed=True,
            outcome=_OUTCOME_OBSERVE,
            mode=mode,
            reason="budget_mode=observe (informational); build proceeds",
            estimate_usd=estimate,
            ceiling_usd=ceiling,
            phase_models=current_models,
            per_role_usd=per_role,
        )

    # enforce mode. No ceiling, no metered estimate, or under ceiling -> proceed.
    if ceiling is None or estimate is None or estimate <= ceiling:
        return BudgetDecision(
            proceed=True,
            outcome=_OUTCOME_WITHIN_BUDGET,
            mode=mode,
            reason=(
                f"estimate {_fmt(estimate)} within ceiling {_fmt(ceiling)}"
                if ceiling is not None
                else "no cost ceiling set; build proceeds"
            ),
            estimate_usd=estimate,
            ceiling_usd=ceiling,
            phase_models=current_models,
            per_role_usd=per_role,
        )

    # Over budget. A governed task is never silently degraded — fail fast.
    if routing_class(execution) == "governed":
        return BudgetDecision(
            proceed=False,
            outcome=_OUTCOME_FAIL_FAST,
            mode=mode,
            reason=(
                f"governed task estimate {_fmt(estimate)} exceeds ceiling "
                f"{_fmt(ceiling)}; refusing to degrade a governed task below its "
                "capability floor (RFC-0014 §7)"
            ),
            estimate_usd=estimate,
            ceiling_usd=ceiling,
            phase_models=current_models,
            per_role_usd=per_role,
        )

    downgraded = _downgrade_within_floor(execution, catalog)
    if downgraded is None:
        return BudgetDecision(
            proceed=False,
            outcome=_OUTCOME_FAIL_FAST,
            mode=mode,
            reason=(
                f"estimate {_fmt(estimate)} exceeds ceiling {_fmt(ceiling)} and no "
                "catalog model meets the capability floor for every role"
            ),
            estimate_usd=estimate,
            ceiling_usd=ceiling,
            phase_models=current_models,
            per_role_usd=per_role,
        )

    new_estimate, new_per_role = estimate_build_cost(
        {"phase_models": downgraded}, catalog
    )
    if new_estimate is not None and new_estimate > ceiling:
        return BudgetDecision(
            proceed=False,
            outcome=_OUTCOME_FAIL_FAST,
            mode=mode,
            reason=(
                f"floor-respecting selection still {_fmt(new_estimate)} > ceiling "
                f"{_fmt(ceiling)}; failing fast rather than dropping below the "
                "capability floor"
            ),
            estimate_usd=new_estimate,
            ceiling_usd=ceiling,
            phase_models=downgraded,
            per_role_usd=new_per_role,
        )

    return BudgetDecision(
        proceed=True,
        outcome=_OUTCOME_DOWNGRADED,
        mode=mode,
        reason=(
            f"downgraded within capability floor: estimate {_fmt(estimate)} -> "
            f"{_fmt(new_estimate)} under ceiling {_fmt(ceiling)}"
        ),
        estimate_usd=new_estimate,
        ceiling_usd=ceiling,
        phase_models=downgraded,
        per_role_usd=new_per_role,
    )


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.2f}"


# --------------------------------------------------------------------------- #
# Self-tests (run: python3 core/budget_enforcement.py)
# --------------------------------------------------------------------------- #
def _check(ok: bool, detail: str) -> None:
    if not ok:
        raise AssertionError(detail)


def _fixture_catalog() -> dict[str, crc.ModelEntry]:
    catalog: dict[str, crc.ModelEntry] = crc.load_catalog(DEFAULT_CATALOG_PATH)
    return catalog


# Named ceilings for the self-tests (avoids inline magic-value comparisons).
_TINY_CEILING = 0.01
# Opus coding+qa at nominal weights ≈ $4.65 at $5/$25 MTok, so this must sit
# below that for the downgrade self-test to trip (was 5.0 under Opus-4.1 pricing).
_MID_CEILING = 3.0


def _test_parse_mode() -> None:
    _check(parse_budget_mode(None) == "observe", "None -> observe")
    _check(parse_budget_mode({}) == "observe", "empty -> observe")
    _check(parse_budget_mode({"budget_mode": "enforce"}) == "enforce", "enforce parsed")
    _check(
        parse_budget_mode({"budget_mode": "weird"}) == "observe", "unknown -> observe"
    )


def _test_observe_never_blocks(cat: dict[str, crc.ModelEntry]) -> None:
    execution = {
        "phase_models": {"planning": "opus", "coding": "opus", "qa": "opus"},
        "routing": {"cost_ceiling_usd": _TINY_CEILING},
        # no budget_mode -> observe
    }
    decision = enforce_budget(execution, catalog=cat)
    _check(decision.proceed, "observe must always proceed even far over ceiling")
    _check(decision.outcome == _OUTCOME_OBSERVE, "outcome must be observe")
    _check(
        decision.estimate_usd is not None and decision.estimate_usd > _TINY_CEILING,
        "observe still computes an estimate",
    )


def _test_enforce_within_budget(cat: dict[str, crc.ModelEntry]) -> None:
    execution = {
        "budget_mode": "enforce",
        "phase_models": {"coding": "haiku"},
        "routing": {"cost_ceiling_usd": 100.0, "class": "economy", "difficulty": "low"},
    }
    decision = enforce_budget(execution, catalog=cat)
    _check(decision.proceed, "under ceiling must proceed")
    _check(decision.outcome == _OUTCOME_WITHIN_BUDGET, "within_budget outcome")


def _test_enforce_downgrades(cat: dict[str, crc.ModelEntry]) -> None:
    # medium tier (balanced floor), standard class, opus everywhere, tiny ceiling
    # -> must downgrade coding/qa/test to the cheapest balanced-floor model.
    execution = {
        "budget_mode": "enforce",
        "phase_models": {"coding": "opus", "qa": "opus"},
        "routing": {
            "cost_ceiling_usd": _MID_CEILING,
            "class": "standard",
            "difficulty": "medium",
        },
    }
    decision = enforce_budget(execution, catalog=cat)
    _check(
        decision.outcome == _OUTCOME_DOWNGRADED,
        f"expected downgrade, got {decision.outcome}",
    )
    _check(decision.proceed, "downgraded build proceeds")
    _check(
        decision.phase_models["coding"] != "opus", "coding must be downgraded off opus"
    )
    # The floor-respecting re-pick lands on a flat-rate model (None $) or a
    # metered model under the ceiling; either way it must not exceed the ceiling.
    _check(
        decision.estimate_usd is None or decision.estimate_usd <= _MID_CEILING,
        "downgraded estimate must not exceed ceiling",
    )


def _test_governed_fails_fast(cat: dict[str, crc.ModelEntry]) -> None:
    execution = {
        "budget_mode": "enforce",
        "phase_models": {"planning": "opus", "coding": "opus"},
        "routing": {
            "cost_ceiling_usd": 0.01,
            "class": "governed",
            "difficulty": "hard",
        },
    }
    decision = enforce_budget(execution, catalog=cat)
    _check(not decision.proceed, "governed over budget must NOT proceed")
    _check(decision.outcome == _OUTCOME_FAIL_FAST, "fail_fast outcome for governed")
    _check("governed" in decision.reason, "reason names the governed refusal")
    # The contract models must be preserved (never silently degraded).
    _check(decision.phase_models["planning"] == "opus", "governed models untouched")


def _test_floor_too_expensive_fails_fast(cat: dict[str, crc.ModelEntry]) -> None:
    # hard tier forces a frontier floor; the cheapest frontier model is opus, so a
    # tiny ceiling cannot be met even after downgrade -> fail fast (not governed).
    execution = {
        "budget_mode": "enforce",
        "phase_models": {"coding": "opus"},
        "routing": {
            "cost_ceiling_usd": 0.01,
            "class": "standard",
            "difficulty": "hard",
        },
    }
    decision = enforce_budget(execution, catalog=cat)
    _check(not decision.proceed, "frontier floor over a tiny ceiling must fail fast")
    _check(decision.outcome == _OUTCOME_FAIL_FAST, "fail_fast outcome")
    _check("floor" in decision.reason, "reason mentions the floor")


def _test_subscription_local_no_block(cat: dict[str, crc.ModelEntry]) -> None:
    # A fully subscription/local build has no $ estimate -> enforce cannot block.
    execution = {
        "budget_mode": "enforce",
        "phase_models": {"coding": "ollama:qwen", "qa": "codex"},
        "routing": {"cost_ceiling_usd": 0.01, "difficulty": "low"},
    }
    decision = enforce_budget(execution, catalog=cat)
    _check(decision.proceed, "no $ estimate -> cannot block (billing-mode parity)")
    _check(decision.estimate_usd is None, "subscription/local estimate is None")


def _test() -> None:
    cat = _fixture_catalog()
    _test_parse_mode()
    _test_observe_never_blocks(cat)
    _test_enforce_within_budget(cat)
    _test_enforce_downgrades(cat)
    _test_governed_fails_fast(cat)
    _test_floor_too_expensive_fails_fast(cat)
    _test_subscription_local_no_block(cat)
    print("budget_enforcement self-tests: 7 groups passed")  # noqa: T201  # CLI self-test sink


if __name__ == "__main__":
    _test()
