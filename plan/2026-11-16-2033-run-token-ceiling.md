---
status: draft
issue: 2033
spec: spec/2026-11-16-2033-run-token-ceiling.md
coders:
  1: lead   # small, tricky logic in one file; no handoff worth the cache cost
  2: lead
reviewers:
  - codex   # read-only final review of the diff
  - agy
---

# Plan: run token ceiling and default-on guardrail

Decisions from the spec: guardrail on unless `off|0|false|no`; per-run ceiling
`AIFACTORY_RUN_TOKEN_CEILING` default 10M (input+output+cache creation, 0
off) enforced in `agents/coder.py` where session usage is read, halting via
`guardrail_halt.json`; `start_run` clears stale halt and counter;
`AIFACTORY_MAX_TURNS` default 1000.

## Steps

1. `apps/backend/agents/act_loop_hooks.py`, `agents/coder.py` (before loop and
   after `usage_payload`), `core/client.py:1117`: implement as above
   → `pytest tests/test_act_loop_wiring.py`.
   Traps: ruff sorting of imports; never raise into the agent.
2. `docs/docs/environment-reference.md:221`, `apps/backend/.env.example`:
   new defaults; tests in `tests/test_act_loop_wiring.py`.

## Tests

`PYTHONPATH=apps/backend pytest tests/test_act_loop_wiring.py tests/test_guardrails.py`; `ruff check`, `ruff format --check`.

## Rollback

Revert the commit; or set `AIFACTORY_ACT_GUARDRAIL=off` and
`AIFACTORY_RUN_TOKEN_CEILING=0`.
