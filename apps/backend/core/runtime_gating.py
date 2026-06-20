"""RFC-0014 §6 gated-runtime registry (operator allowlist + contract opt-in).

The provider factory knows how to *build* every runtime AIFactory supports
(``providers/factory.py``). RFC-0014 adds a second, orthogonal question on top of
"can we build it?": **is the operator willing to spend on it for this run?**

The default runtime is ``claude``. Every other runtime — ``codex``,
``antigravity`` (agent swarms), ``ollama``, ``ollama-cloud``, and the two
"speed-up" runtimes ``claude-subagents`` (parallel sub-agent fan-out) and
``dynamic-workflow`` (scripted multi-agent orchestration) — is *selectable* but
**off** unless BOTH of:

  1. the operator enables it in an allowlist (env ``AIFACTORY_RUNTIMES`` — a
     comma-separated runtime list — additive to the always-on ``claude``), AND
  2. the Task Contract opts in via ``execution.runtime``.

``claude-subagents`` and ``dynamic-workflow`` multiply spend (they fan out to N
agents), so they are MANUAL-ENABLE ONLY: they are never implied by a broad
allowlist token and must be named explicitly in ``AIFACTORY_RUNTIMES``.

This module is a pure decision core (env in, decision out): no I/O beyond reading
the passed-in env mapping, no provider construction. ``providers/factory.py`` and
``core/provider_failover.py`` consult it; the executor passes the contract's
``execution.runtime`` through ``runtime_from_execution``.

Run directly for the self-tests: ``python3 core/runtime_gating.py``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# The runtime that is ALWAYS available without any allowlist entry. Selecting it
# never requires operator opt-in (RFC-0014 §6: "All non-claude runtimes are
# disabled unless ...").
DEFAULT_RUNTIME = "claude"

# Every runtime RFC-0014 §6 names as selectable. ``claude`` is included so the
# set is the full universe of valid ``execution.runtime`` values; it is always
# enabled regardless of the allowlist.
KNOWN_RUNTIMES: frozenset[str] = frozenset(
    {
        "claude",
        "codex",
        "antigravity",
        "ollama",
        "ollama-cloud",
        "claude-subagents",
        "dynamic-workflow",
    }
)

# The "speed-up" runtimes (RFC-0014 §6): parallel sub-agent fan-out / scripted
# multi-agent orchestration. They multiply spend, so a broad allowlist token
# (e.g. ``all``) does NOT enable them — they must be named explicitly.
MANUAL_ENABLE_ONLY: frozenset[str] = frozenset({"claude-subagents", "dynamic-workflow"})

# Operator allowlist env var. Comma-separated runtime names. The literal token
# ``all`` enables every non-manual-only runtime (a convenience for operators who
# accept the cost envelope) but still NOT the manual-only speed-up runtimes.
ALLOWLIST_ENV = "AIFACTORY_RUNTIMES"
_ALLOW_ALL_TOKEN = "all"  # noqa: S105  # an allowlist sentinel token, not a secret


def _env_source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def normalize_runtime(runtime: str | None) -> str:
    """Lower-case/strip a runtime name; empty/None resolves to the default."""
    name = (runtime or "").strip().lower()
    return name or DEFAULT_RUNTIME


def operator_allowlist(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Resolve the operator-enabled runtime set from ``AIFACTORY_RUNTIMES``.

    Always includes ``claude``. The token ``all`` expands to every known runtime
    EXCEPT the manual-only speed-up runtimes (those must be named explicitly).
    Unknown tokens are ignored (a typo can never *enable* an unintended runtime).
    """
    raw = (_env_source(env).get(ALLOWLIST_ENV) or "").strip()
    enabled: set[str] = {DEFAULT_RUNTIME}
    if not raw:
        return frozenset(enabled)
    tokens = {t.strip().lower() for t in raw.split(",") if t.strip()}
    if _ALLOW_ALL_TOKEN in tokens:
        enabled |= KNOWN_RUNTIMES - MANUAL_ENABLE_ONLY
    enabled |= {t for t in tokens if t in KNOWN_RUNTIMES}
    return frozenset(enabled)


def is_runtime_enabled(
    runtime: str | None, env: Mapping[str, str] | None = None
) -> bool:
    """True when ``runtime`` is operator-enabled for this process.

    ``claude`` is always enabled. Every other runtime requires an explicit
    ``AIFACTORY_RUNTIMES`` entry (and the speed-up runtimes require being named
    explicitly — ``all`` does not cover them).
    """
    name = normalize_runtime(runtime)
    return name in operator_allowlist(env)


def runtime_from_execution(execution: Mapping[str, object] | None) -> str:
    """Read the contract opt-in ``execution.runtime``; default when absent.

    Pure/serializable: accepts the parsed ``execution`` block (or None) and
    returns a normalized runtime name. Non-string values resolve to the default.
    """
    if not isinstance(execution, Mapping):
        return DEFAULT_RUNTIME
    raw = execution.get("runtime")
    return normalize_runtime(raw if isinstance(raw, str) else None)


class RuntimeNotEnabledError(RuntimeError):
    """Raised when a contract opts into a runtime the operator has not enabled.

    Carries the offending runtime and the operator-enabled set so the caller can
    surface a precise, actionable reason (never a silent fallback to ``claude``,
    which would change the run's cost/behaviour without the operator knowing).
    """

    def __init__(self, runtime: str, enabled: frozenset[str]) -> None:
        self.runtime = runtime
        self.enabled = enabled
        manual_hint = (
            " (a speed-up runtime — it must be named explicitly in "
            f"{ALLOWLIST_ENV}, it is not covered by 'all')"
            if runtime in MANUAL_ENABLE_ONLY
            else ""
        )
        super().__init__(
            f"runtime {runtime!r} is not operator-enabled{manual_hint}. "
            f"Enabled runtimes: {sorted(enabled)}. "
            f"Set {ALLOWLIST_ENV} to opt in."
        )


def resolve_runtime(
    execution: Mapping[str, object] | None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve and gate the runtime for a build.

    Returns the contract's opted-in runtime when the operator has enabled it.
    Raises ``RuntimeNotEnabledError`` when the contract names a runtime the
    operator has NOT enabled — never silently degrades to ``claude``, so a
    cost/behaviour change is always explicit (RFC-0014 §6 + §7 "never silently
    degrade a governed task").
    """
    runtime = runtime_from_execution(execution)
    if runtime == DEFAULT_RUNTIME:
        return DEFAULT_RUNTIME
    enabled = operator_allowlist(env)
    if runtime not in enabled:
        raise RuntimeNotEnabledError(runtime, enabled)
    return runtime


def selectable_runtimes(env: Mapping[str, str] | None = None) -> dict[str, bool]:
    """Map every known runtime to whether it is operator-enabled.

    Useful for an operator-facing "what can this process run?" report (the CLI
    ``aifactory runtimes`` view / CFactory billing-mode panel).
    """
    enabled = operator_allowlist(env)
    return {name: (name in enabled) for name in sorted(KNOWN_RUNTIMES)}


# --------------------------------------------------------------------------- #
# Self-tests (run: python3 core/runtime_gating.py)
# --------------------------------------------------------------------------- #
def _check(ok: bool, detail: str) -> None:
    # Raising checker (not assert) so the module stays ruff-clean (S101) for the
    # new-file ratchet and stays meaningful under -O.
    if not ok:
        raise AssertionError(detail)


def _test_default_always_on() -> None:
    _check(is_runtime_enabled("claude", {}), "claude must be enabled with empty env")
    _check(
        is_runtime_enabled(None, {}), "default (None) must resolve to enabled claude"
    )
    _check(not is_runtime_enabled("codex", {}), "codex must be off without allowlist")


def _test_allowlist() -> None:
    env = {ALLOWLIST_ENV: "codex, ollama"}
    _check(is_runtime_enabled("codex", env), "codex must be on when allowlisted")
    _check(is_runtime_enabled("ollama", env), "ollama must be on when allowlisted")
    _check(not is_runtime_enabled("antigravity", env), "antigravity off (not listed)")
    # Unknown tokens are ignored and cannot enable anything.
    _check(
        operator_allowlist({ALLOWLIST_ENV: "nope, codex"})
        == frozenset({"claude", "codex"}),
        "unknown token ignored",
    )


def _test_all_token_excludes_manual() -> None:
    env = {ALLOWLIST_ENV: "all"}
    _check(is_runtime_enabled("codex", env), "'all' enables codex")
    _check(is_runtime_enabled("antigravity", env), "'all' enables antigravity")
    _check(
        not is_runtime_enabled("claude-subagents", env),
        "'all' must NOT enable subagents",
    )
    _check(
        not is_runtime_enabled("dynamic-workflow", env),
        "'all' must NOT enable dyn-workflow",
    )


def _test_manual_only_explicit() -> None:
    env = {ALLOWLIST_ENV: "claude-subagents"}
    _check(
        is_runtime_enabled("claude-subagents", env), "explicit subagents must enable it"
    )
    _check(
        not is_runtime_enabled("dynamic-workflow", env), "dynamic-workflow still off"
    )


def _test_resolve_and_raise() -> None:
    _check(resolve_runtime({"runtime": "claude"}, {}) == "claude", "claude resolves")
    _check(resolve_runtime(None, {}) == "claude", "no execution -> claude")
    _check(
        resolve_runtime({"runtime": "codex"}, {ALLOWLIST_ENV: "codex"}) == "codex",
        "opted-in + allowlisted codex resolves",
    )
    raised = False
    try:
        resolve_runtime({"runtime": "codex"}, {})
    except RuntimeNotEnabledError as exc:
        raised = True
        _check(exc.runtime == "codex", "error carries runtime")
    _check(raised, "ungated runtime must raise, never silently fall back to claude")


def _test_selectable_report() -> None:
    report = selectable_runtimes({ALLOWLIST_ENV: "codex"})
    _check(report["claude"] is True, "claude always selectable")
    _check(report["codex"] is True, "codex selectable when allowlisted")
    _check(report["ollama"] is False, "ollama not selectable")
    _check(set(report) == set(KNOWN_RUNTIMES), "report covers every known runtime")


def _test() -> None:
    _test_default_always_on()
    _test_allowlist()
    _test_all_token_excludes_manual()
    _test_manual_only_explicit()
    _test_resolve_and_raise()
    _test_selectable_report()
    print("runtime_gating self-tests: 6 groups passed")  # noqa: T201  # CLI self-test sink


if __name__ == "__main__":
    _test()
