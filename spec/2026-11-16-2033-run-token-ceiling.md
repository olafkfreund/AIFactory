---
status: draft
issue: 2033
intent: intent/2026-11-16-2033-run-token-ceiling.md
---

# Spec: run token ceiling and default-on guardrail

## Design

- `agents/act_loop_hooks.py`: `guardrail_enabled()` is true unless the env is
  `0/false/no/off`. New `token_ceiling()` (`AIFACTORY_RUN_TOKEN_CEILING`,
  default 10,000,000, `0` disables), `charge_tokens(spec_dir, usage)` and
  `start_run(spec_dir)`.
- Tokens are counted where usage is already counted: `agents/coder.py` reads
  `error_info["usage"]` after each session. It sums input + output + cache
  creation tokens (cache reads excluded, they dominate and are cheap). Passing
  the ceiling writes the existing `guardrail_halt.json`
  (`halt_reason: token_ceiling: ...`). The build loop and QA loop already stop
  on that file and `completion.py` already puts it in the completion event.
- `start_run` clears the counter and any stale halt file when the build loop
  starts, so a default-on guardrail cannot halt a rerun on an old halt.
- `core/client.py`: `max_turns` reads `AIFACTORY_MAX_TURNS`, default 1000.

## Alternatives rejected

- Wire `budget_enforcement.py`: it is a USD pre-run estimate over a catalog,
  not an in-run counter; a different mechanism.
- Lower `max_turns` by default: changes behaviour for legitimate long runs;
  the ceiling is the real bound.
- A new halt channel: the halt file already reaches loops and the event.

## Risks

Default-on guardrail may halt a legitimate run that repeats a failing call;
thresholds are env-tunable and `off` restores old behaviour. The ceiling only
counts build sessions (QA sessions do not report usage here).

## Verification

Unit tests in `tests/test_act_loop_wiring.py`: default-on, `off`, repeated
failing Edit is halted, ceiling halts, `0` disables, `start_run` resets.
