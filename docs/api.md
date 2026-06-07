# API Reference

AIFactory's control plane is the **web-server** — a FastAPI application that
exposes a REST + WebSocket API (projects, tasks, execution, files, auth /
OIDC / SCIM, MCP, audit).

## Interactive Swagger

The full, always-current OpenAPI 3.1 specification is published to the
Backstage catalog as the **`aifactory-web-api`** API entity. Open the
component's **APIs** tab (or the API entity directly) to browse every endpoint
with request/response schemas in Swagger UI.

- **Spec source:** [`apps/web-server/openapi.yaml`](https://github.com/olafkfreund/AIFactory/blob/dev/apps/web-server/openapi.yaml)
- **Live docs (running portal):** `http://<host>:3101/docs` (Swagger) ·
  `http://<host>:3101/redoc` (ReDoc) — enabled when `APP_DEBUG=true`.
- **Raw spec (running portal):** `http://<host>:3101/openapi.json`

## How it stays current

The committed spec is regenerated from the live FastAPI app by
[`scripts/generate-openapi-spec.py`](https://github.com/olafkfreund/AIFactory/blob/dev/scripts/generate-openapi-spec.py),
and CI (`.github/workflows/techdocs.yml`) re-runs it on every change so the
published API docs never drift from the code.

## Authentication

All `/api/*` routes require a bearer token (`Authorization: Bearer <token>`):

- a **JWT** access token (login / OIDC / SAML), or
- the legacy **`API_TOKEN`** for machine-to-machine / local use.

Object-level authorization (per-org access on projects, tasks, and files) is
enforced for human JWT users; the service token bypasses for trusted M2M
traffic. See the security epic for details.
