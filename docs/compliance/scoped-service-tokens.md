# Retiring the shared wildcard service token (Factory#312 / #310 gap #2)

Status: Draft — step 1 (additive recognition) implemented; later phases planned.
Owner: Compliance / Platform.
Scope: AIFactory `apps/web-server` auth; coordinated across PFactory / TFactory /
CFactory for the M2M edges.

## The problem

Every Factory sibling ships the same shared wildcard bearer token
`APP_API_TOKEN` (Kubernetes secret `factory-secrets`), injected into all four
planes. In `apps/web-server/server/auth.py` a request whose bearer matches
`settings.API_TOKEN` is authenticated as a service principal:

```
request.state.user = {"id": "default", ..., "is_service": True}
```

`routes/project_authz.py::is_service_principal()` then treats `is_service` as a
blanket bypass of every per-org / per-project authorization check. Because the
same token authenticates all machine-to-machine (M2M) traffic in the PARR chain
(PFactory -> AIFactory -> TFactory), a compromise of any single sibling yields
full fleet admin over the REST + WebSocket surface. There is no way to tell
which sibling a call came from, and no way to grant a caller less than
everything.

## The target model (already partly built)

A scoped `acw_` API-key path already exists (`routes/api_keys.py`,
`mcp_remote/auth.py`). An `acw_` key carries:

- an owning `org_id` (or none, for a service key),
- an optional owning `user_id` (none => service/M2M key),
- an explicit comma-separated `scopes` set (e.g. `mcp:read`, `mcp:write`).

`mcp_remote/auth.py::AuthenticatedKey` already parses those scopes and offers
`has_scope()`. The middleware's Strategy 3 already validates `acw_` keys for the
general REST surface. The wildcard's replacement is therefore a **scoped `acw_`
service key per sibling edge**, not a new credential type.

## Design principles

- Additive and reversible at every step. No big-bang cutover.
- The wildcard keeps working exactly as today until the final phase explicitly
  retires it, and every phase has a one-setting rollback.
- Behavioural change is gated behind config flags that default to current
  behaviour, so a deploy of the code changes nothing until an operator opts in.
- No gitops secret changes are bundled with code changes.

## Phased migration

### Phase 1 — Recognise scoped service tokens (this PR, additive)

Add `APP_SCOPED_SERVICE_TOKENS_ENABLED` (default `false`). When enabled, the
`acw_` branch of `TokenAuthMiddleware` surfaces the key's explicit scopes on the
principal:

```
request.state.user["scopes"]         = ["deploy:read", "tasks:write", ...]
request.state.user["scoped_service"] = True   # key has no owning user
```

`is_service` is unchanged, so nothing that reads the principal today behaves
differently — the scopes are merely made visible. The wildcard path (Strategy 2)
is untouched.

- Rollback: unset the flag (or it is simply never set). Code path with the flag
  off is byte-identical to pre-change.
- Not in this phase: any enforcement of those scopes; any wildcard change.

### Phase 2 — Mint and distribute per-sibling scoped service keys

For each M2M edge, mint a dedicated `acw_` service key whose scopes cover only
what that edge needs. Store each in the caller's own secret, distinct from the
shared wildcard. Callers keep sending the wildcard as well, so nothing breaks.

**The edges, enumerated from the callers rather than guessed (2026-08-10).** Each
row is what that caller actually requests; a scope set wider than this column is
a scope set nobody measured.

| Edge | Caller | Endpoints called | Shape |
|---|---|---|---|
| PFactory -> AIFactory | `plan_pipeline.py` via `plan/emit/contract_emit.py` | `POST /api/tasks/from-plan` | one route, write |
| AIFactory -> TFactory | `backend/pfactory/tfactory_client.py` | `POST /api/specs/ingest`, `GET /api/specs/{project}/{spec}/pr` | one write, one read |
| AIFactory intake -> PFactory | `web-server/services/intake_poller.py` | `POST /api/plan/sessions/from-issue`, `POST /api/tasks/from-issue` | two writes |
| CFactory cockpit -> all three | `backend/cfactory/adapters/*.py` | `GET` on `/api/capabilities`, `/api/tasks`, `/api/tasks/{id}`, `/api/plan/sessions`, `/api/plan/sessions/{id}`, `/api/tfactory/tasks`, `/api/tfactory/tasks/{spec}`, `/api/tfactory/tasks/{spec}/test-plan.json` | **read-only** |

Two things follow from the table:

- **Do the cockpit first.** It is the largest blast-radius reduction available —
  today it holds a credential granting blanket `is_service` across the fleet, and
  every path it calls is a GET. A read-only key there removes most of the risk in
  one credential, and it is the safest to reverse because nothing it does mutates.
- **The pipeline edges are narrow.** One or two write endpoints each; the
  PFactory -> AIFactory scope set is a single route. These do not need a scope
  taxonomy, they need four keys with obvious contents.

**`PFACTORY_AIFACTORY_API_TOKEN` already exists** and is read ahead of the
wildcard in `plan_pipeline.py`, so that edge needs a secret value and no code
change at all.

**A second wildcard is in scope (Factory#312).** `CFACTORY_MCP_SECRET` is
CFactory's own legacy full-scope bearer, live in production, with its scoped
replacement (`CFACTORY_API_KEYS`) already running beside it at lower precedence —
the same shape as the problem this document describes. It carries write reach:
the MCP surface creates cards and dispatches work into the factory. Retiring
`APP_API_TOKEN` alone leaves half the blast radius in place.

- Rollback: revoke the new key (`is_active = false`); callers still hold the
  wildcard.

### Phase 3 — Cut each caller over to its scoped key

Edge by edge, switch each sibling to send its scoped `acw_` key instead of the
wildcard, one edge per change, verified in isolation. The wildcard remains
accepted throughout, so any edge can be rolled back independently by pointing it
back at `APP_API_TOKEN`.

### Phase 4 — Enforce scopes (replace the blanket bypass)

Add `APP_SCOPED_SERVICE_TOKENS_ENFORCE` (default `false`). When enabled,
`is_service_principal()` / `check_project_access()` stop treating a
`scoped_service` principal as an unconditional bypass and instead require the
specific scope for the operation (read vs write, deploy, etc.). A wildcard
principal (no `scopes`) still bypasses, so this phase changes behaviour only for
callers that have already moved to scoped keys in Phase 3.

- Rollback: unset the enforce flag; scoped principals revert to bypass.

### Phase 5 — Retire the wildcard

Once every edge is on a scoped key and enforcement has been proven in
production, stop accepting `API_TOKEN`: gate Strategy 2 behind an
`APP_LEGACY_WILDCARD_TOKEN_ENABLED` flag defaulted to `false`, then remove the
secret from gitops. The one-time deprecation warning already emitted on every
wildcard authentication (`_warn_legacy_api_token_once`) is the signal that the
edge inventory is complete: when it stops firing in production, no caller still
depends on the wildcard.

- Rollback: re-enable the flag; the wildcard secret can be re-injected.

## Test coverage for Phase 1

`apps/web-server/tests/test_scoped_service_tokens.py` proves:

- a scoped `acw_` service token authenticates and the principal carries only its
  own scopes;
- the wildcard path is unchanged whether or not the flag is set;
- with the flag off (default), the `acw_` principal carries no `scopes` /
  `scoped_service` keys — the new path is off by default.
