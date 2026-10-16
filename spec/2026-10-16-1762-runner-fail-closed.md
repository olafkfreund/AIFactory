---
status: draft
issue: 1762
intent: intent/2026-10-16-1762-runner-fail-closed.md
---

# Spec: Runners fail closed when they cannot hide their environ

## Design

1. Add `require_non_dumpable()` to `apps/backend/core/process_hardening.py`. It
   calls `make_non_dumpable()` and raises
   `SystemExit("prctl(PR_SET_DUMPABLE, 0) failed")` when it returns False. That
   is the server's check moved into the shared module, unchanged.
2. The three runner sites (`core/client.py:619`, `core/simple_client.py:78`,
   `providers/factory.py:322`) replace their `if not make_non_dumpable():
   logger.warning(...)` block with `process_hardening.require_non_dumpable()`.
3. The server's `_make_non_dumpable()` (`server/main.py:794-797`) calls the same
   helper, so the two cannot drift. It keeps its `sys.path` set-up and name, so
   `test_server_non_dumpable.py` keeps working.
4. Non-Linux: `make_non_dumpable()` already returns True off Linux, the only
   escape the server has. Nothing new is added.
5. `child_env()` (`core/child_env.py:57`) compares `k.upper()` against
   `_STRIP_VARS`, which is all upper case. Linux env names are case-sensitive,
   so `anthropic_api_key_file` is a distinct variable that was let through.

## Alternatives rejected

- Raise `RuntimeError` in the runners: differs from the server; the brief asks
  for the same behaviour.
- Copy the `raise SystemExit` into each of the three sites: four copies of one
  check instead of one helper.
- An env opt-out for dev machines: the server has none; non-Linux is already
  handled.

## Risks

- The server calls `create_simple_client` and `get_provider` in-process (e.g.
  `server/routes/github.py:170`, `server/services/insights_service.py:374`). A
  `SystemExit` there would end the worker, but the server already made itself
  non-dumpable at startup and exits if that fails, so a repeat prctl on the same
  process does not fail in practice.
- A runner on a Linux host whose seccomp profile blocks prctl now stops instead
  of running with a readable environ. That is the intended trade.

## Verification

- New test in `tests/test_runner_hardening.py`: with `make_non_dumpable`
  patched to return False, `create_client`, `create_simple_client` and
  `get_provider` each raise `SystemExit`. Fails on dev (they log and go on).
- New test in `tests/test_child_process_env.py`: a lowercase
  `anthropic_api_key_file` is absent from `child_env()` and
  `child_env(runner=True)`. Fails on dev.
- Existing `tests/test_runner_hardening.py` and
  `apps/web-server/tests/test_server_non_dumpable.py` still pass.
- `ruff check`, `ruff format --check` on the touched files.
