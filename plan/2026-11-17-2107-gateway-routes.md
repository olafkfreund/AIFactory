---
status: approved
issue: 2107
also: 2108
spec: spec/2026-11-17-2107-gateway-routes.md
coders:
  1: codex   # contained stdlib edit with small tricky logic (body rewrite, header, error paths)
  3: codex   # small contained edit of build_job_env plus two tests
  # steps 2 and 4: lead (precedence logic across modules; docs)
reviewers:
  - codex    # independent read-only review of each draft and of the final diff
---

# Plan: gateway resolves class routes; AIFactory consumes `phase_routes`

Approved decisions (from spec): the gateway maps a class-named `model` to the model
in a flat JSON routes file (`GATEWAY_ROUTES_PATH`, re-read per request) and answers
`X-Model-Route: <class>`; AIFactory carries `phase_routes` into task metadata
(`phaseRoutes`) and, only when `AIFACTORY_PHASE_ROUTES=on` and
`AIFACTORY_MODEL_GATEWAY_URL` is set and `LITELLM_GATEWAY_URL` is unset, uses
`openai-compatible:<class>` as the phase model. Precedence: fallback >
`pinnedModel` > phase route (difficulty floor low `cheap`, medium `balanced`, hard
`frontier`, raise only) > `phaseModels` > the rest unchanged. Scalar
`execution.route` is not consumed. Everything is inert with no env set.

Run from the worktree root. Python: `apps/backend/.venv` or repo `.venv`; check how
neighbouring tests run (`pytest tests/model_gateway`, `pytest apps/backend/test_routing_policy.py`).

## Steps

1. `apps/model-gateway/gateway.py` and `tests/model_gateway/test_gateway.py` (codex).
   `Config` gains `routes_path` from `GATEWAY_ROUTES_PATH` (default ""); update
   `load_config` and its docstring. In `do_POST`, after the length check and body
   read, add `_resolve_route(config.routes_path, body) -> (body, cls|None)`: with no
   path, or an unreadable or invalid routes file, or a body that is not a JSON
   object, or `model` not one of `local|cheap|balanced|frontier` AND a key of the routes map
   with a non-empty string value (a concrete model name is never rewritten, even if
   a routes file lists it), return the original bytes and None; else return `json.dumps` of the body
   with `model` replaced, and the class. `_reply` gains optional `extra` headers; the
   success and the upstream-`HTTPError` replies add `X-Model-Route` when a class
   was resolved (not 502 gateway errors). Tests per spec Verification (resolved +
   header, error status carries header, concrete model byte-identical and no header,
   class without entry, a concrete model that is also a routes-file key (unchanged), missing/invalid file, non-JSON/non-object body, no path,
   file edited between requests).
   → `pytest tests/model_gateway -q` and `ruff check apps/model-gateway tests/model_gateway`.
   Traps: stdlib only; AIFactory ruff and `standards/ruff.toml` disagree on "as"
   import aliases: use plain names; never log the routed model or any token.
2. `apps/backend/trusted_plan.py:773` map `"phase_routes": "phaseRoutes"` (no
   `isAutoProfile`); `apps/backend/phase_config.py`: `TaskMetadataConfig.phaseRoutes`,
   public `phase_route_model(phase, metadata) -> str | None` (None when flag off,
   gateway URL unset, `LITELLM_GATEWAY_URL` set, fallback set, `pinnedModel` set,
   `phaseRoutes` not a dict, or the value not one of the four classes; else
   `openai-compatible:<class>` after the floor), called in `_resolve_phase_model`
   and `get_phase_model_betas` right after the `pinnedModel` block (betas via
   `get_model_betas`); in `get_provider_extra_kwargs` for `openai-compatible`, a
   stripped class name with the feature on returns the gateway URL/token before the
   saved-endpoint lookups; `apps/web-server/server/services/agent_spec_creation.py:150`:
   extract a module-level `_spec_phase_model(metadata)` (route model via a lazy import
   of `phase_route_model`, else the existing auto-profile `phaseModels.spec`), used
   where `spec_phase_model` is set today; test it in
   `tests/test_agent_spec_creation_mixin.py` (route wins; flag off, pin and fallback
   give the old result; the source still passes it as `--model`). New `apps/backend/test_phase_routes.py` per spec Verification (reuse
   the helpers of `test_routing_policy.py`, including its autouse env scrub; also
   clear the new env vars).
   → `pytest apps/backend/test_phase_routes.py apps/backend/test_routing_policy.py -q`
   and `pytest tests/test_agent_spec_creation_mixin.py -q`, `ruff check` on touched files.
   Traps: `phaseModels` behaviour must stay byte-identical when the route is None;
   backend modules are imported lazily from the web-server (see
   `build_backend.py:662`); mypy ratchet: use `importlib` only where the neighbours do.
3. `apps/web-server/server/services/build_backend.py` (codex): forward
   `AIFACTORY_PHASE_ROUTES` to the Job only when `LITELLM_GATEWAY_URL` is unset in the
   control plane (so the Job cannot re-enable what the control plane disabled; not added
   to the plain passthrough tuple), and in `build_job_env`'s
   `if gateway_url:` block also `env["AIFACTORY_MODEL_GATEWAY_URL"] = gateway_url`.
   Add tests to `tests/test_build_backend_kubejob.py`: URL set -> Job env has
   `AIFACTORY_MODEL_GATEWAY_URL`; unset -> absent; `AIFACTORY_PHASE_ROUTES` forwarded
   when set and absent when not or when `LITELLM_GATEWAY_URL` is set (clear both in
   the autouse fixture), and `phase_route_model` under the env `build_job_env`
   returns is None when both gateway URLs were set.
   → `pytest tests/test_build_backend_kubejob.py -q`; ruff on both files.
   Traps: do not forward the token under a new name; direct keys stay swapped.
4. Docs (lead): rows for `GATEWAY_ROUTES_PATH` (format, hot reload, header) and
   `AIFACTORY_PHASE_ROUTES` (prerequisites: `AIFACTORY_MODEL_GATEWAY_URL`,
   `AIFACTORY_RUNTIMES=openai-compatible`, gateway routes file, `LITELLM_GATEWAY_URL`
   unset; registry `deny` mode note) in `docs/docs/environment-reference.md`; commented
   lines in `apps/backend/.env.example`; a CHANGELOG entry. Gitops follow-up
   (mount the routes ConfigMap) named in the PR, not built.
   → `python -m mkdocs build -f mkdocs.yml -d /tmp/techdocs-site` (as techdocs.yml:180), if mkdocs is installed.

## Tests

`pytest tests/model_gateway tests/test_build_backend_kubejob.py tests/test_agent_spec_creation_mixin.py -q` and
`pytest -o asyncio_mode=auto apps/backend/test_phase_routes.py apps/backend/test_routing_policy.py -q` pass (as CI runs suites outside `tests/`, ci.yml:206); repo lint per CI
(`ruff check`, `ruff format --check`, mypy ratchet as CI runs it); the generated
autonomy matrix is not touched, regenerate (never hand-merge) if a rebase conflicts.

## Rollback

All new behaviour is behind unset env vars; revert the PR. No data, schema or manifest changes.
