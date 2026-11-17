---
status: approved
issue: 2107
also: 2108
intent: intent/2026-11-17-2107-gateway-routes.md
---

# Spec: gateway resolves class routes; AIFactory consumes `phase_routes`

## Design

### A. Gateway (`apps/model-gateway/gateway.py`, #2107)

- New env `GATEWAY_ROUTES_PATH` (optional): a JSON file `{"<class>": "<model>"}`
  for the catalog classes `local|cheap|balanced|frontier` (a ConfigMap mount).
  `Config` gains `routes_path`. Unset = inert, today's behaviour.
- On each `POST /v1/chat/completions`, after auth and the length check, read the
  routes file (no cache: ConfigMap updates take effect without restart, the same
  choice as `routing_policy._catalog_picks`). Parse the request body as JSON; if
  it is an object whose `model` is a string that is a key of the routes map with a
  non-empty string value, replace `model` with that value and re-encode. Any
  other case (no file, unreadable or invalid file, body not JSON or not an object,
  `model` not a class, class with no routed model) forwards the original bytes
  unchanged. A bad routes file is treated as empty: it can never fail a request.
- When a class was resolved, every response for that request (success or the
  upstream's error status) carries `X-Model-Route: <class>`. It is not sent
  otherwise: a concrete model reports no route, and PFactory falls back to the
  planning route. `_reply` gains an optional extra-headers argument.
- Only a class name is rewritten; the body is otherwise byte-for-byte what the
  client sent (re-encoded only when rewritten). The routed model name is never
  logged (the gateway logs nothing).

### B. Consumer (#2108)

1. `trusted_plan._EXECUTION_TO_METADATA`: `"phase_routes": "phaseRoutes"`. The map
   is carried as is, like `phase_models`; it does NOT set `isAutoProfile`.
   Scalar `execution.route` is not consumed (out of scope).
2. `phase_config.py`:
   - Env `AIFACTORY_PHASE_ROUTES` (`on` enables, anything else is off; default
     off) AND `AIFACTORY_MODEL_GATEWAY_URL` set AND `LITELLM_GATEWAY_URL` unset (the provider's
     `resolve_base_url` prefers LiteLLM unconditionally, `providers/_gateway.py:65`,
     so with both set the class would go to LiteLLM; the feature stays off then:
     fail closed to today's behaviour). The flag is the operator's
     assertion that the gateway has a routes file covering the classes; AIFactory
     cannot ask. `TaskMetadataConfig` gains `phaseRoutes`.
   - `_phase_route_model(phase, metadata)` returns `openai-compatible:<class>` or
     `None`. It returns `None` when a control-plane fallback is set or
     `metadata["pinnedModel"]` is set, so every caller honours those two above the
     route, and unless the feature is on, `metadata["phaseRoutes"]`
     is a dict, and its value for `phase` is one of the four classes. The class is
     raised to the RFC-0011 difficulty floor (`difficultyTier`, same mapping as
     `routing_policy._RFC0011_FLOOR`: low `cheap`, medium `balanced`, hard `frontier`; never lowered; `local` is the lowest rank).
   - `_resolve_phase_model` and `get_phase_model_betas` call it right after
     `pinnedModel` and before `phaseModels`. So: control-plane fallback >
     `pinnedModel` > phase route > `phaseModels` > CLI > contract routing >
     policy routing > metadata model > default. When it returns `None` the code
     is exactly today's. Betas for a route model are `[]` (not in
     `MODEL_BETAS_MAP`), mirrored via `get_model_betas`.
   - `get_provider_extra_kwargs`: for `openai-compatible` and a stripped model
     that is a class name while the feature is on, return
     `{"model": <class>, "base_url": AIFACTORY_MODEL_GATEWAY_URL, "api_key":
     AIFACTORY_MODEL_GATEWAY_TOKEN or OPENAI_COMPATIBLE_API_KEY or None}` BEFORE the
     saved-endpoint lookups, so a class name never reaches a saved endpoint or
     `api.openai.com`. The `model` sent to the gateway is the bare class, which
     the gateway resolves.
   - `agent_spec_creation.py:150` reads `phaseModels.spec` itself and passes
     `--model` to the spec runner (which only does `resolve_model_id`). It lazily
     imports `phase_route_model` (public name; same lazy-import precedent as
     `build_backend.py:662`) and, when it returns a model for `spec`, passes that
     as `--model` instead, so the spec runner and the build agree.
3. `build_backend.build_job_env`: add `AIFACTORY_PHASE_ROUTES` to
   `_PASSTHROUGH_BUILD_ENV`, and when the gateway URL is set also set
   `AIFACTORY_MODEL_GATEWAY_URL` in the Job env (the token already reaches the Job
   as `OPENAI_COMPATIBLE_API_KEY`). With no gateway URL the Job env is unchanged.
4. Prerequisite, documented not changed: the `openai-compatible` runtime must be
   allowed (`AIFACTORY_RUNTIMES=openai-compatible`, `core/runtime_gating.py`);
   the allowlist stays enforced, so an operator who forgets it gets the existing
   clear refusal, not a silent fallback.
5. Docs: `GATEWAY_ROUTES_PATH` and `AIFACTORY_PHASE_ROUTES` in
   `docs/docs/environment-reference.md` and `apps/backend/.env.example`;
   CHANGELOG line. The routes file format is documented there.

## Alternatives rejected

- Resolve classes in AIFactory (`routing_policy` catalog path): exists for the
  `routing.requested` tiers; the epic wants the gateway to own the mapping so one
  ConfigMap serves every service, and `phase_routes` carries classes, not tiers.
- Generating the routes file in the gateway from `model-catalog.json`: the
  catalog's models are Claude ids, the gateway's upstream is an OpenAI-compatible
  server; which model serves a class there is the operator's call. A flat map
  keeps that explicit; generating the ConfigMap is a gitops step.
- Gateway cache with mtime: not needed at this call rate; re-read is a small file.
- Auto-detect "gateway resolves classes" by probing it: a network call in model
  resolution; the explicit flag is simpler and fails visibly.
- Header on every response: a concrete model has no class to report; sending a
  guessed class would misreport the signature.
- Making the route outrank `pinnedModel`: a contract pin is the strongest signal.

## Risks

- Flag on with a gateway that lacks a route for a class: the class name goes
  upstream unchanged and the upstream rejects it (a visible failure, no silent
  wrong model). The control-plane fallback still recovers a failed non-Claude build.
- A route preferred over `phaseModels`: a model that PFactory picked is replaced by
  the gateway's model for that class. This is the point of the flag; default off.
- `enforce_model_registry` sees `openai-compatible:<class>`; under `deny` mode an
  unregistered id is blocked. Default `warn` only logs. Noted in the docs.
- `GATEWAY_ROUTES_PATH` file read per request adds one small read; a huge file is
  the operator's mistake. `shortcut: no cache; add an mtime cache if profiling shows it.`

## Verification

- `tests/model_gateway/test_gateway.py`: class resolved (upstream sees the routed
  model, response has `X-Model-Route`, error status also carries it); concrete
  model forwarded byte-identical, no header; class with no entry, missing file,
  invalid file, non-JSON body, non-object body: unchanged, no header; no
  `GATEWAY_ROUTES_PATH`: today's behaviour; file edited between requests takes
  effect.
- `apps/backend/test_phase_routes.py`: feature off (no flag, no gateway URL, or `LITELLM_GATEWAY_URL` set):
  `phaseModels` result is unchanged for every phase; on: route wins over
  `phaseModels`, `pinnedModel` and the fallback still outrank it; phase absent or
  invalid class: `phaseModels` wins; difficulty floor raises, never lowers; betas
  mirror; `get_provider_extra_kwargs` returns the gateway URL/token for a class and
  is unchanged for a real model name; the spec-creation entry returns the route model as `--model` (pin and fallback outrank); `execution_profile_to_metadata` maps
  `phase_routes` and omits `isAutoProfile`.
- `tests/test_build_backend_kubejob.py`: Job env gets `AIFACTORY_MODEL_GATEWAY_URL`
  only when the gateway URL is set, and forwards `AIFACTORY_PHASE_ROUTES`.
- Repo lint and the touched suites; CI green.
