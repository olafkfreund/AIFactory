---
status: approved
issue: 1646
intent: intent/2026-10-15-1646-handoff-credential-preflight.md
---

# Spec: probe gemini credentials, and preflight the TFactory handoff

## Design

Two changes, both reusing the existing preflight module and its modes
(`off` / `warn` default / `enforce`, `core/auth_preflight.py:57-65`).

### 1. A google probe in `core/auth_preflight.py`

- `probe_google(env)`: credential is `GEMINI_API_KEY`, else `GOOGLE_API_KEY`
  (the order the Gemini CLI uses; both kept by `providers/antigravity.py:65-69`).
  No key → `skipped` (the CLI may be on OAuth, which this cannot check).
- One generation-free request: `GET
  https://generativelanguage.googleapis.com/v1beta/models?pageSize=1` with the
  key in the `x-goog-api-key` header, never in the URL.
- Classification:
  - 200 → `ok`
  - 401 / 403 → `auth_failed`
  - 400 whose body contains `API_KEY_INVALID` → `auth_failed`. Google answers
    an invalid key with 400 + reason `API_KEY_INVALID`, not 401; this is the
    exact error in the issue, so it is a definite rejection, not a guess.
  - anything else, or a transport error → `inconclusive`
  The detail names the env var, never the value.
- A new `_http_get(url, headers) -> (status, body)` returns the status and the
  first 2000 chars of the body; `_http_status` becomes a wrapper returning the
  status, so `probe_anthropic` and its tests are unchanged.
- `providers_for_models` maps names starting with `gemini` to `google`;
  `_PROBES` gains `"google": probe_google`. The build path
  (`cli/build_commands.py:325`) then probes gemini with no change there.

### 2. Preflight inside `send_handoff` (`pfactory/tfactory_client.py:741`)

`send_handoff` is the one function all three handoff paths call
(`maybe_auto_handoff_tfactory` `:927`, `routes/execution.py:382` and `:945`), so
one change covers all of them.

- After the `not_configured` return and before the POST, read the models from
  `payload["contract"]["execution"]["phase_models"]` (the field that carries the
  verify-lane models, `:678-684`). No models → nothing probed.
- Skip when `preflight_mode() == "off"`. Otherwise run `run_auth_preflight` in
  `asyncio.to_thread` (the probe is blocking urllib), and log each non-`ok`
  result with `logger.warning` (provider + detail only).
- In `enforce`, the first `auth_failed` result returns
  `{"sent": False, "reason": "auth_preflight_failed", "provider": ..., "detail": ...}`
  without posting. `inconclusive` never blocks. In `warn`, the handoff is sent
  as today.
- Any exception from the preflight is caught and logged; `send_handoff` keeps
  its "never raises" contract and proceeds with the POST.

Callers already treat `sent: False` as a failed handoff and persist the result
to `tfactory_handoff.json`, so the named reason is visible with no caller change.

## Alternatives rejected

- **Make the cost router credential-aware** (issue option 1): right long-term,
  but it lives in PFactory and needs the target environment's credential state.
  Out of scope.
- **Preflight in `build_handoff_payload` / `build_ingest_payload`**: these build
  data; two of three paths would need separate calls. `send_handoff` is the
  single choke point.
- **Swap the verify planner to the `qa` model** (AIFactory#1645): trades this
  bug for a silent downgrade on governed tasks; parked by the issue author.
- **Block on any non-200**: would turn a Google outage into a refused handoff.

## Risks

- The probe runs in AIFactory's environment, not TFactory's. If TFactory holds
  a different key, the result can be wrong in either direction. Default `warn`
  makes a false result a log line only. Documented, not solved.
- Up to 10 s (existing `_TIMEOUT_S`) added per probed provider to a handoff.
  Runs off the event loop.
- Tests that send a handoff payload carrying `phase_models` while a real key
  is in the environment would make a network call. Existing handoff tests carry
  no `phase_models`; new tests monkeypatch the probe.

## Verification

- `tests/test_auth_preflight.py`: google classification (200, 401, 400 with
  `API_KEY_INVALID`, plain 400, 503, transport error, no key), key absent from
  the detail and the URL, `providers_for_models(["gemini"]) == ["google"]`.
- `tests/test_pfactory_tfactory_client.py`: with a monkeypatched preflight,
  `enforce` + `auth_failed` returns `auth_preflight_failed` and the poster is
  not called; `warn` + `auth_failed` posts; `enforce` + `inconclusive` posts.
- The `enforce` handoff test fails on unfixed dev (the poster is called).
- `ruff check`, `ruff format --check` on touched files.
