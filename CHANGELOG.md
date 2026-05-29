## [Unreleased]

### Documentation

- Themed the documentation site to match the skill_pool terminal aesthetic (phosphor green on black, JetBrains Mono, CRT scanlines).

### Added — GCP MCP catalog entry (#168, Epic #100)

- **GCP catalog entry (`transport="http"`)** — closes the last gap in the default MCP server catalog (Epic #100). Google Cloud AI Companion MCP went GA in March 2026 and uses a remote-first HTTP transport rather than a local subprocess. The entry is auto-enabled when a project has GCP markers (`gcp/`, `app.yaml`, `cloudbuild.yaml`) and `GOOGLE_APPLICATION_CREDENTIALS` is set. See `apps/backend/agents/tools_pkg/mcp_catalog.py`.
- **HTTP transport in `MCPCatalogEntry`** — the dataclass now supports `transport="http"` with `http_endpoint`. `build_server_config()` routes to `_build_http_config()` for HTTP entries, producing `{"type": "http", "url": "..."}` (the shape the Claude Agent SDK's HTTP MCP client expects). V1/V1.5 stdio entries are byte-for-byte unchanged.
- **`GCP_MCP_ENDPOINT` env override** — the default GA endpoint (`https://cloudaicompanion.googleapis.com/v1/extensions/default/mcp`) is overrideable at call time via `GCP_MCP_ENDPOINT`. Useful for VPC Service Controls perimeters, staging projects, and future GCP MCP servers (BigQuery, Cloud Run, etc.).
- **Helm slot `mcpCredentials.gcp`** — new sub-block under `mcpCredentials` with `secretName` (GCP-dedicated Secret, overrides the shared `secretName` for the SA JSON mount) and `endpointOverride` (wires `GCP_MCP_ENDPOINT` into the pod). The existing `mcpCredentials.providers.gcp` file mount + env var is unchanged; the new block is additive.
- **Docs** — GCP section added to `docs/docs/concepts/mcp-credentials.md` covering Workload Identity (preferred on GKE), service-account JSON setup, endpoint override, and troubleshooting.
- **Tests** — `tests/test_mcp_catalog_gcp.py` (25 unit tests for catalog shape, marker detection, endpoint override, HTTP config shape, backward-compat) and `tests/helm/test_mcp_credentials_gcp.py` (8 Helm acceptance tests for secret mount, secretName override, and endpointOverride rendering).

### Added — v1.2 per-tenant audit-chain anchor (#208)

- **Per-tenant HMAC-SHA256 signing keys** — the tenant reconciler issues one 32-byte KMS-wrapped key per isolated org (Option A: one chain + one key per tenant, ISO 27001 A.12.4.2/A.12.4.3 compliant). Keys are stored in `audit_signing_keys` with `org_id` set and mirrored to the tenant's Vault path for auditor handover.
- **Per-tenant chain genesis** — each isolated tenant's first `audit_logs` row uses `prev_hash='GENESIS-T-<org-uuid>'`, separating the per-tenant chain from the shared chain. `tenant_audit_state` table tracks the cutover boundary, current chain head, and lifecycle ('active'|'sealed').
- **Per-tenant daily anchor** — the cron iterates over isolated orgs (in id order, batch-committed) and emits one `audit_anchors` row per org per day, signed with the org's key. One tenant's KMS failure does NOT block others (failure-safe per-tenant loop).
- **Per-tenant verifier** — `verify_tenant_anchored_export(ndjson, signing_keys, org_id)` validates a per-tenant export's chain. Asserts cross-tenant replay is rejected (design finding #6). `compute_tenant_hash` and `verify_tenant_chain` in `audit_chain.py` provide the domain-separated chain helpers.
- **Helm opt-in** — `audit.anchor.perTenant: false` (default). `perTenantOptions.keyCacheSize`, `batchSize`, `retentionDays`, `metrics.perOrgLabels` operator-tunable. Validator rejects `perTenant=true` without `tenant.isolationEnabled=true`.
- **Export endpoint change (v1.2 behavior change)**: `?org_id=...&include_anchors=true` now accepted for isolated tenants when `perTenant=true` (was 400 in v1.1 — the per-tenant chain CAN verify a per-tenant export). Shared-chain deployments unchanged.
- **ISO 27001 evidence** — A.12.4.2, A.12.4.3, A.18.1.3, A.18.2.2 entries updated with per-tenant chain contribution (v1.2+; v1.1 entries preserved). Closes the v1.1 gap where "protection of log information" was evidenced at deployment granularity only.
- **Schema migration** (`c3d7e8f1a2b4`): `audit_anchors.org_id` (nullable FK, ON DELETE SET NULL), `audit_signing_keys.org_id` (nullable FK, ON DELETE CASCADE), `tenant_audit_state` table, composite index `audit_logs(org_id, created_at)`.
- Refs: #208 / Epic #204. Design doc: `docs/plans/2026-05-29-per-tenant-audit-anchor-design.md`.

---

## [1.1.0] - 2026-05-28

**Enterprise v1.1: Multi-tenant isolation, observability, audit hardening, and legacy-IdP federation**

AIFactory ships 7 major features for regulated deployments (banks, healthcare, fintech). Epic #35 closed all 9 child issues. All features opt-in via Helm values.

### Added — Epic #35 (Enterprise v1.1)

#### Identity & Access (SAML 2.0 + SCIM 2.0) — #41
- **PR #177 (PR-1a)**: SAML security foundation — OneLogin SDK wrapper (`strict=True`, `wantAssertionsSigned=True`, RSA-SHA256), HMAC-signed RelayState (CSRF defence), SCIM Pydantic schemas (RFC 7643), min-viable filter parser (eq-only on `userName`/`externalId`/`active`), Bearer-token middleware with constant-time compare. 48 unit tests.
- **PR #178 (PR-1b1)**: `external_identities` table (kind + subject + FK CASCADE) for multi-IdP linkage; auto-backfill of `users.oidc_sub` as `kind='oidc:legacy'`. 4 Postgres tests.
- **PR #195 (PR-1b2)**: SAML SP routes (/login, /acs, /metadata) with HMAC-bound RelayState, per-assertion-TTL replay cache, XSW defence. Cross-IdP collision guard (same-email-different-IdP → 409).
- **PR #198 (PR-1b3)**: SCIM 2.0 CRUD on Users + Groups with array-append PATCH (Azure AD compat), soft-delete + 404-on-GET.
- **PR #199 (PR-2)**: Helm `saml:` + `scim:` blocks with required-when-enabled validators. E2E tests. Concept doc: [saml-scim](docs/docs/concepts/saml-scim.md).

#### Tenant Isolation Mode — #36
- **PR #192 (PR-1)**: Per-tenant K8s Namespace + ServiceAccount + NetworkPolicy + IAM (IRSA) + Vault path schema skeleton.
- **PR #200 (PR-2)**: Full K8s/IAM/Vault writes. Redis SETNX leader election with Lua check-and-delete release. Agent spawner routing to per-tenant pods.
- **PR #201 (PR-2 continued)**: Tenant dry-run reconciler. OPA Gatekeeper sample policies (namespace-prefix + RoleBinding-tenant-scope). Daily teardown CronJob with 24-hour dry-run window.
- **PR #206 (PR-3)**: Helm pre-install CNI probe (Calico/Cilium hard-fail for FQDN policy support). Helm `tenant.isolationEnabled` block. Concept doc: [tenant-isolation](docs/docs/concepts/tenant-isolation.md).

#### LiteLLM Gateway — #38
- **PR #193 (PR-1)**: LiteLLM gateway env redirect for HTTP providers (OpenAI-compatible routing).
- **PR #194 (PR-2a)**: `organizations.allowed_models` JSON column for per-org LLM allowlist.
- **PR #202 (PR-3)**: LiteLLM sub-chart deployment. Per-org budget + rate-limit + PII-redacted audit hooks. Grafana dashboards (7 panels). Concept doc: [litellm-gateway](docs/docs/concepts/litellm-gateway.md).
- **PR #203 (PR-2b)**: Audit hook + PII redactor (mask CC#, API keys). Per-org virtual-key lifecycle.
- **Scope caveat:** Claude calls (via Agent SDK) bypass gateway in v1.1 — SDK speaks Anthropic-format `/v1/messages`, wire-incompatible with LiteLLM's OpenAI endpoint. Closes v3.0 limitation #6; v1.2 adds Claude enforcement wrapper.

#### Cloud LLM Routing (Bedrock + Vertex) — #39
- **PR #197**: Bedrock + Vertex AI routing via LiteLLM gateway. Concept doc: [cloud-llm-routing](docs/docs/concepts/cloud-llm-routing.md).

#### OpenTelemetry Distributed Tracing — #42
- **PR #175**: In-process tracer + auto-instrumentation (FastAPI, SQLAlchemy, asyncpg, httpx, Redis). Per-phase manual `task:phase:*` spans. W3C `traceparent` injected into Redis envelopes + subprocess env. CorrelationIdMiddleware sources `request_id` from active `trace_id`. 14 unit tests.
- **PR #176**: Helm `otel:` block (samplingRatio, headersSecretName for vendor auth). Agent-subprocess `tracing_bootstrap.py`. 23 tests (12 helm + 8 bootstrap + 3 e2e). Concept doc: [observability-tracing](docs/docs/concepts/observability-tracing.md).
- Closes v3.0 limitation #4.

#### ISO 27001 Evidence + Signed Audit-Chain Anchor — #43
- **PR #180**: Design doc with 8 brainstorm decisions + reviewer findings baked in.
- **PR #181**: Schema migration adding `audit_anchors`, `audit_signing_keys`, `audit_logs.classification`, `users.last_login_at`. UTC-day unique index. 5 Postgres tests.
- **PR #182**: `audit_anchor.py` signer/verifier. `_SigningKey` newtype, leak-safe `__repr__`, HMAC sign/verify, KMS-wrapped key versioning. 15 unit tests.
- **PR #183**: `audit_anchor_cron.py` — daily 00:00 UTC tick, startup backfill, first-anchor semantics, idempotency. Classification-window hashing closes "flip confidential→public to leak" attack. 8 unit tests.
- **PR #184**: `audit_export.py` interleaves anchors into NDJSON. `verify_anchored_export()` offline verifier for auditors. Fixed chain-head semantic bug. 7 unit tests.
- **PR #185**: `GET /api/admin/access-review` endpoint (SOC2 CC6.2 + ISO A.9.2.5 quarterly reviews). 6 unit tests.
- **PR #186**: Helm `audit.anchor:` block (Kubernetes CronJob or in-process scheduler). ISO 27001 Annex A evidence map at `guides/compliance/iso27001-evidence.md` (30 controls directly evidenced). Concept doc: [audit-anchor](docs/docs/concepts/audit-anchor.md). 10 helm tests.
- Closes v3.0 limitation #1.

#### gVisor RuntimeClass — #37
- **PR #169**: Agent pods opt-in to `runtimeClassName: gvisor` for kernel-level isolation.

#### Multi-Replica Support (S3 + Redis) — #40, #154
- **PR #171**: Cross-replica event bus via Redis pub/sub.
- **PR #172**: Helm Redis pub/sub chart wiring + multi-replica docs.
- **PR #173**: WorkspaceStore module + fsspec-based S3 snapshots (AWS / MinIO / GCS / Azure).
- **PR #174**: Helm storage block + agent snapshot hook + MinIO CI.
- Closes v3.0 limitation #5.

### Migration & Upgrade

**All v1.1 features are off by default.** Enable in your Helm values:
```yaml
saml:
  enabled: true
scim:
  enabled: true
tenant:
  isolationEnabled: true
litellm:
  enabled: true
otel:
  enabled: true
audit:
  anchor:
    enabled: true
workspaces:
  storage:
    enabled: true
redis:
  enabled: true
```

**Backwards-compatible:** No schema-breaking migrations. Operators upgrade by bumping chart version + applying `helm upgrade`. The `alembic upgrade head` migration runs automatically on web-pod start (auto-apply default).

### Out of scope (v1.2 tracking issue #204)
- Claude-on-LiteLLM enforcement wrapper (scope caveat above)
- Per-tenant audit-chain anchor
- SAML Single Logout
- PII bundle: Luhn-checked CC pattern + scrubBeforeSend mode

### Contributors

Shipped across 32 merged PRs (#169–#199, #200–#206) in a single coordinated session. Epic #35 closed all 9 child issues.

---

## Unreleased

### ⚖️ Licensing

- **Relicensed from AGPL-3.0 → dual MIT OR GPL-3.0.** AIFactory is now
  available under the recipient's choice of either license. See
  `LICENSE`, `LICENSE-MIT`, and `LICENSE-GPL`. SPDX identifier:
  `MIT OR GPL-3.0-only`. The `dataseek.team` enterprise-licensing
  contact line (which referenced a non-existent email) was removed.

### 🏷️ Branding

- **Rebrand `dataseeek` → `olafkfreund`.** The `dataseeek` GitHub org
  doesn't exist; every reference in non-archive files was rewritten to
  point at the actual repo location (`olafkfreund/AIFactory`) and the
  actual GitHub Pages URL (`olafkfreund.github.io/AIFactory`). Affects
  README badges, docusaurus config, package.json URLs, demo repo path,
  cosign verify identity in image-mirroring drills, and ghcr.io image
  paths in the Helm chart docs.

### 📚 Documentation

- **Full docs rewrite + GitHub Pages site.** The `guides/` directory was
  archived to `docs-archive/2026-05-26/guides/` (git history preserved).
  A fresh Docusaurus site at `docs/` is published to
  <https://olafkfreund.github.io/AIFactory/> via a new
  `.github/workflows/docs.yml` workflow. Includes 18 reorganized pages:
  Getting Started, Demo, Concepts (3), Architecture (3 with Mermaid
  diagrams), Wiki (FAQ/Troubleshooting/Glossary), Showcase, Compliance
  (SOC2/GDPR), Contributing, Roadmap. The legacy `guides/` content is
  unchanged in archive form and still searchable via `git log --follow`.

- **README.md slimmed from 557 to 115 lines.** Hero + tagline + 60-second
  quickstart + demo callout + screenshot grid + prominent docs links.
  Everything operational moved to the docs site.

### 🏛️ Enterprise v1.1 (Epic #35)

- **#42 — OpenTelemetry distributed tracing** ✅ (closed)
  - PR #175: in-process tracer + auto-instrumentation (FastAPI, SQLAlchemy,
    asyncpg, httpx, Redis), per-phase manual `task:phase:*` spans for the
    agent lifecycle, W3C `traceparent` injected into Redis envelopes for
    cross-replica trace continuity, `TRACEPARENT` env injected into
    subprocess for cross-process continuity, `CorrelationIdMiddleware`
    sources `request_id` from active `trace_id` (client header still wins
    for back-compat), full failure-safe contract (broken collector never
    crashes the app), 14 unit tests with `InMemorySpanExporter`.
  - PR #176: Helm `otel:` block (typed config, `samplingRatio ∈ [0.0, 1.0]`,
    `headersSecretName` for vendor-auth), two operator-misconfig validators
    that fail `helm template` loud, agent-subprocess `tracing_bootstrap.py`
    that re-attaches the parent context from `TRACEPARENT`, concept doc
    at [`/concepts/observability-tracing`](https://olafkfreund.github.io/AIFactory/concepts/observability-tracing),
    23 new tests (12 helm + 8 bootstrap + 3 e2e).
  - Closes v3.0 limitation #4 ("No built-in OpenTelemetry distributed tracing").
- **#43 — ISO 27001 evidence + signed audit-chain anchor** ✅ (closed)
  - PR #180: design doc with 8 brainstorm decisions + 5 critical
    reviewer findings baked in.
  - PR #181: schema migration adding `audit_anchors`, `audit_signing_keys`,
    `audit_logs.classification`, `users.last_login_at`. Postgres
    UTC-day unique index makes daily anchoring idempotent. 5 Postgres
    acceptance tests.
  - PR #182: `audit_anchor.py` signer/verifier service. `_SigningKey`
    newtype with leak-safe `__repr__`, HMAC sign/verify, versioned
    KMS-wrapped key storage so root-key rotation doesn't invalidate
    prior anchors. KMS-decrypt contract enforced (raises on wrong
    type or wrong length so cloud backends can't silently produce
    wrong HMACs). 15 unit tests including log-safety verification.
  - PR #183: `audit_anchor_cron.py` — daily 00:00 UTC tick, startup
    backfill of missed days, zero-row-day handling, first-anchor
    semantics, idempotency. Classification-window hashing closes the
    "flip confidential→public to leak past export filter" attack
    surface — design decision #5 honest revision (the original
    `_canonical()` extension was mathematically incompatible with
    pre-#43 chain re-verification, so classification protection moved
    to the anchor layer). 8 unit tests.
  - PR #184: `audit_export.py` interleaves anchors deterministically
    into NDJSON. `verify_anchored_export()` is the offline verifier
    helper auditors use. Caught + fixed a real semantic bug in #183
    where the cron stored raw `prev_hash` instead of the outgoing
    chain head. 7 unit tests.
  - PR #185: `GET /api/admin/access-review` endpoint for SOC2 CC6.2 +
    ISO 27001 A.9.2.5 quarterly access reviews. `users.last_login_at`
    stamped on every successful OIDC login. 6 unit tests.
  - PR #186: Helm `audit.anchor:` block with Kubernetes CronJob OR
    in-process scheduler. ISO 27001 Annex A evidence map at
    `guides/compliance/iso27001-evidence.md` (~30 controls directly
    evidenced; remainder marked as operator responsibility). Concept
    doc at [/concepts/audit-anchor](https://olafkfreund.github.io/AIFactory/concepts/audit-anchor).
    10 helm acceptance tests covering toggle, scheduler enum,
    cron-knob flow-through, security-context match with web pod.
  - Closes v3.0 limitation #1 ("Audit chain has no signed external
    anchor").
- **#41 — SAML 2.0 + SCIM 2.0 for legacy-IdP banks** ✅ (closed)
  - PR #177 (PR-1a): Security-foundation modules. SAML replay cache with
    per-assertion TTL (not blanket-LRU — the reviewer-flagged trap), OneLogin
    SDK wrapper with `strict=True` + `wantAssertionsSigned=True` + RSA-SHA256
    hard-coded, HMAC-signed RelayState (CSRF defence with constant-time
    verify), SCIM Pydantic schemas per RFC 7643, minimum-viable filter
    parser (eq-only on `userName`/`externalId`/`active`), Bearer-token
    middleware with 503-loud-on-misconfig + constant-time compare. 48 unit
    tests, all the security-critical surface where bugs become security holes.
  - PR #178 (PR-1b1): `external_identities` table (kind + subject + FK
    CASCADE) for multi-IdP linkage; backfills existing `users.oidc_sub`
    rows as `kind='oidc:legacy'`. 4 Postgres-marked tests.
  - PR #195 (PR-1b2): SAML SP routes `/login` + `/acs` + `/metadata` with
    HMAC RelayState verification, per-assertion-TTL replay defence, and
    the cross-IdP collision guard (decision #4 — auto-link only when no
    other-kind identity exists; otherwise 409).
  - PR #198 (PR-1b3): full SCIM CRUD on `/api/scim/v2/Users` + `/Groups`
    per RFC 7644: array-append PATCH semantics (the Azure-AD-relies-on-it
    bug), soft-delete + 404-on-GET (the Azure-AD-resync-loop trap), `If-Match`
    accepted as advisory only.
  - PR #196 (PR-1b4): merged identity-provider discovery for the login
    page (`GET /api/auth/identity-providers`); SAML + OIDC routers
    auto-mount on enable. SAML session extended into `auth.py`'s
    `current_user_dependency` (cookie shape matches OIDC).
  - PR-2 (this PR): Helm `saml:` + `scim:` blocks with 4 schema validators
    (operator misconfigs fail at `helm install`, not first pod start), env
    + Secret-mount wiring, SP cert rotation via projected-volume optional
    source. Concept doc at [/concepts/saml-scim](https://olafkfreund.github.io/AIFactory/concepts/saml-scim)
    with Okta / Azure AD / Keycloak IdP preset recipes + SP cert rotation
    runbook + decision-#11 local-logout note. 30 helm-toggle tests
    covering all 4 validators + env wiring + coexistence with v1.1 toggles.
    New `saml-scim (P8 acceptance)` CI job.
  - Closes v3.0 limitation #2 ("SSO is OIDC-only").
- **#37 — gVisor RuntimeClass** ✅ (closed previously this cycle): agent
  pods can opt in to `runtimeClassName: gvisor` for kernel-level isolation.
- **#40 — S3 workspace storage + Redis pub/sub** ✅ (closed previously this
  cycle): workspaces snapshot to fsspec-backed S3 (AWS / MinIO / GCS /
  Azure); Redis pub/sub fans out WebSocket events across replicas.
  Closes v3.0 limitation #5 ("Single-replica only").
- **#154 — Scoped MCP API keys** ✅ (closed previously this cycle):
  per-developer `acw_` keys with scope-gated mutating routes.

### ✨ Added

- **`scripts/demo.sh`** — end-to-end demo runner (Bash + jq + gh).
  Seeds `olafkfreund/aifactory-demo` with 3 issues, registers the repo
  with your portal, imports the issues as backlog tasks, prompts you
  to drive Claude Code from the terminal, then kicks off an autonomous
  build. Flags: `--yolo`, `--no-reset`, `--portal=URL`.

- **`scripts/capture-screenshots.ts`** — Playwright headless Chromium
  driver that captures 14 named PNGs of the marquee portal views to
  `docs/static/img/screenshots/`. Reproducible — anyone can refresh
  the gallery with `npm -w apps/frontend-web run capture-screenshots`.

- **`Justfile`** — canonical command index. `just --list` shows
  `install`, `backend`, `frontend`, `docs-dev`, `demo`, `screenshots`,
  `test-backend`, `test-frontend`, `test-postgres`, `test-all`.

- **Root `package.json` scripts**: `docs:install`, `docs:dev`,
  `docs:build`, `demo`, `screenshots`.

---

## 3.0.2 - 2026-05-26

Patch release fixing two leftover wiring + branding bugs from v3.0.0.

### 🛠️ Fixed

- **P6 observability never wired into `main.py`**. The
  `server/observability/` package shipped in v3.0.0 (Epic #26 P6)
  but `main.create_app()` never called `install_metrics(app)`,
  `configure_structlog()`, or `app.add_middleware(CorrelationIdMiddleware)`.
  As a result the production portal exposed neither `/metrics` nor
  structured JSON logs nor correlation IDs — despite all P6 unit
  tests passing (they built their own minimal FastAPI app and called
  the functions directly, bypassing main.py). v3.0.2 wires the three
  calls in the correct order:
  - `configure_structlog()` at the top of `create_app()` so
    boot-time logs are already JSON.
  - `CorrelationIdMiddleware` added LAST so it's the outermost
    layer (sets X-Request-ID before TokenAuth runs; 401 responses
    still carry the ID — auditors rely on this).
  - `install_metrics(app)` after all routers are mounted so the
    Prometheus instrumentator can derive cardinality-capped
    `handler` labels from the route table.

  Regression test added at `tests/obs/test_p6_main_wiring.py`:
  imports `main.create_app()` and asserts `/metrics` returns 200 +
  CorrelationIdMiddleware echoes back `X-Request-ID` + the FastAPI
  app title is AIFactory + `app.version` matches the package version.
  Gates every PR forward.

- **Leftover Magestic branding in `main.py`**. The v3.0.0 rebrand
  missed three string constants:
  - `title="Magestic AI Web API"` → `"AIFactory Web API"`
  - `description="Web API for Magestic AI autonomous coding framework"`
    → `"Web API for AIFactory — self-hosted AI task management +
    agent orchestration"`
  - Root-route message `"Magestic AI Web Server"` →
    `"AIFactory Web Server"`

  Plus the hardcoded `version="1.0.0"` on the FastAPI app + on
  `/api/health` was a drift hazard. v3.0.2 reads the canonical
  version from `apps/backend/__init__.py` at startup (the file
  `bump-version.js` already updates on every release), via a tiny
  `_read_app_version()` helper. No more silent version-skew.

### Upgrade notes

- Backwards-compatible patch: `helm upgrade aifactory --version 3.0.2`
  picks up both fixes with no schema or config changes.
- Operators who deployed v3.0.1 had a non-functional `/metrics`
  endpoint. After upgrading, configure your Prometheus scrape job
  against the now-live endpoint (see `docs-archive/2026-05-26/guides/operations/observability.md`).

## 3.0.1 - 2026-05-26

Patch release with two operator-visible fixes.

### 🛠️ Fixed

- **SQLite migration crash on fresh install**. The P2.3
  `encrypt_credentials` migration (`c6e3b2d4a8f0`) used a direct
  `op.alter_column(nullable=False)` to re-apply the NOT NULL
  constraint on `email_accounts.access_token` after the encrypted-
  column swap. SQLite doesn't support `ALTER TABLE ... ALTER
  COLUMN ... SET NOT NULL` — backends booting against a fresh
  SQLite (`autoApply=true` in the Helm chart's POC path; default
  local-dev path) crashed during `alembic upgrade head`. Wrapped
  the step in `op.batch_alter_table`, mirroring P3.3's
  `d8f1a3c5e7b9` migration. Postgres deployments are unaffected
  (their behavior was correct via the same native ALTER).
  Regression test added at `tests/secrets/test_p2_sqlite_migration.py`
  that runs `alembic upgrade head` against a temp SQLite file —
  gates every PR going forward.

- **AIFactory logo not displaying in the sidebar/loading screen/
  onboarding**. The new logo + favicon assets were stashed before
  P1 work began and never restored to the main release. Bundle
  contains the updated `logo.png` (547 KB, full-res AIFactory
  brand), `favicon.ico` (15 KB), `apple-touch-icon.png` (43 KB),
  and 16/32 px favicon variants. The sidebar `<img src="/logo.png">`
  reference is unchanged — the new files just slot in.

### Upgrade notes

- **Operators on v3.0.0**: this is a backwards-compatible patch.
  `helm upgrade` to v3.0.1 picks up both fixes.
- **Operators who already migrated** (the SQLite migration crash
  blocked them from getting that far on v3.0.0): no special
  handling needed — fresh install + `helm install aifactory --version 3.0.1`
  works end-to-end.

## 3.0.0 - 2026-05-26

The AIFactory **enterprise GA** release (Epic #26). Self-hosted Helm
chart with PSS-restricted defaults, encrypted-at-rest secrets backed
by 5 KMS backends, OIDC SSO, tamper-evident audit chain, GDPR
right-to-erasure, structured-JSON observability + Prometheus
metrics, and a full SOC 2 / GDPR / STRIDE evidence pack with three
ship-readiness drill scripts.

### ⚠ Breaking changes

- **Forward-only schema migration** `c6e3b2d4a8f0_encrypt_credentials`:
  `email_accounts.access_token`, `email_accounts.refresh_token`, and
  `llm_endpoints.api_key` columns convert from plaintext `Text` to
  encrypted `LargeBinary`. The migration is **forward-only** — there
  is no downgrade path. Operators MUST take a `pg_dump` backup before
  upgrading from any v2.x install.
- **Required Postgres backend for production**: SQLite remains
  supported for dev/POC, but `kms_data_keys` + the audit chain
  expect Postgres semantics for indexed lookups.
- **Container runs as non-root uid 65532** with read-only root
  filesystem and dropped capabilities. Operators with custom
  init-containers writing to `/` must mount tmpfs/emptyDir.

### ✨ Added — Epic #26 phases

- **P0 — Container hygiene**: Chainguard distroless base
  (digest-pinned), Trivy CVE scan, Syft SBOM, cosign keyless
  signing via GitHub OIDC, multi-arch (amd64+arm64) manifest
  inspection.
- **P1 — Postgres backend**: `asyncpg` driver, Alembic migrations,
  optional `APP_MIGRATIONS_AUTO_APPLY=false` for Helm Job mode,
  bank-grade privilege model (no SUPERUSER, no CREATE EXTENSION).
- **P2 — Encrypted secrets at rest**: `EncryptedString`
  `TypeDecorator` over `LargeBinary`, per-org `kms_data_keys` with
  LRU cache, 5 KMS backends (`fernet` for dev, `aws_kms`,
  `vault_transit`, `azure_kv`, `gcp_kms`), root-key rotation CLI
  (`python -m server.crypto rotate-root`), forward-only column
  migration with KMS-aware backfill.
- **P3 — OIDC SSO**: `authlib`-based Authorization Code + PKCE +
  state + nonce, JIT user/`OrganizationMember` provisioning with
  claim-mapped roles (`APP_OIDC_GROUP_TO_ROLE`), 15-minute access
  TTL + 8-hour refresh, IdP-validated refresh path with userinfo
  caching, logout redirect to IdP `end_session_endpoint`. Presets
  for Keycloak, Okta, Azure AD.
- **P4 — Helm chart**: `charts/aifactory/` with PSS-restricted
  security contexts, default-deny NetworkPolicy + 443 egress
  allowlist, ExternalSecret templates for 4 backends, optional
  bundled Postgres `StatefulSet` for POC mode, `customCABundle`
  for TLS-intercepting corporate proxies, schema-validated
  `values.yaml`.
- **P5 — Audit hardening**: SHA-256 hash chain on every audit-log
  write, NDJSON + CSV streaming export at `/api/audit/export`,
  air-gappable external verifier (`python -m server.audit
  verify-chain`), GDPR Art. 17 erasure that re-hashes the chain so
  `verify-chain` continues to pass, daily retention job (default
  13 months = SOC 2 12 + buffer).
- **P6 — Observability**: `structlog` JSON-to-stdout with
  ISO-8601 timestamps + `request_id` binding, correlation-ID
  middleware (`X-Request-ID`) with `httpx` propagation, Prometheus
  `/metrics` with cardinality-capped `handler` labels (route
  templates, not raw paths), optional `METRICS_SCRAPE_TOKEN`
  bearer gate, Helm `ServiceMonitor` template, pre-built Grafana
  dashboard JSON (7 panels).
- **P7 — Evidence + ship-readiness drills**: SOC 2 evidence pack
  (CC1-CC9 + A1 + C1), GDPR DPIA + data-flow diagram, STRIDE
  threat model, 4-cloud-path deployment runbook (EKS+RDS / AKS+
  Azure Postgres / GKE+Cloud SQL / vanilla K8s+Vault), v0.x → v3.0
  upgrade guide, three executable drill scripts
  (`backup-restore.sh`, `upgrade-in-place.sh`, `image-mirroring.sh`)
  with `--dry-run` modes.

### 📚 Documentation

New operator runbooks under `guides/`:
- `guides/operations/audit-trail.md`
- `guides/operations/encrypted-secrets-dr.md`
- `guides/operations/image-mirroring.md`
- `guides/operations/kms-rotation-runbook.md`
- `guides/operations/observability.md`
- `guides/operations/oidc-setup.md`
- `guides/deployment/helm-install.md`
- `guides/deployment/runbook.md`
- `guides/deployment/upgrade.md`
- `guides/compliance/soc2-evidence.md`
- `guides/compliance/dpia-data-flow.md`
- `guides/security/threat-model.md`
- `guides/observability/grafana-aifactory.json`

### 🧪 CI

11 acceptance jobs gate every PR (≈2000 tests total):
`backend (ruff + pytest)`, `docker (P0)`, `postgres (P1) × {15, 16}`,
`secrets (P2)`, `oidc (P3)`, `helm (P4)`, `audit (P5)`, `obs (P6)`,
`evidence (P7)`, `frontend (typecheck)`.

### ⚠ Documented v3.0 limitations (v3.1 follow-ups)

Tracked in `guides/compliance/soc2-evidence.md § Documented
limitations`. Each maps to a v3.1 Epic #35 issue:

1. ~~Audit chain has no signed external anchor.~~ ✅ **Closed by
   Epic #35 #43** — daily HMAC-signed anchor + KMS-wrapped key
   rotation + offline verifier helper. See
   [audit-anchor concept doc](https://olafkfreund.github.io/AIFactory/concepts/audit-anchor)
   and `guides/compliance/iso27001-evidence.md` for ISO 27001 Annex A mapping.
2. Revocation latency bounded by 15-minute access-token TTL (back-
   channel logout deferred).
3. FIPS 140-2/3 modules not validated.
4. ~~No built-in OpenTelemetry distributed tracing.~~ ✅ **Closed by
   Epic #35 #42** — see `## Unreleased § Enterprise v1.1`.
5. ~~Single-replica only (multi-replica via Redis pub/sub deferred).~~
   ✅ **Closed by Epic #35 #40** — multi-replica works with
   `redis.enabled=true` + `workspaces.storage.enabled=true`.
6. ~~LLM-call audit deferred to v3.1 LiteLLM gateway.~~ ✅ **Closed by
   Epic #35 #38** — operator opts in via `litellm.enabled=true`; the
   sub-chart deploys a per-tenant budget / rate-limit / allowlist
   gateway with PII-redacted `audit_logs` rows + Grafana dashboards.
   **Scope caveat:** Claude calls (via the Claude Agent SDK) bypass
   the gateway in v1.1 — the SDK speaks Anthropic-format `/v1/messages`
   which is wire-incompatible with LiteLLM's OpenAI endpoint.
   Claude retains its existing chain-audit coverage (`claude.session.*`
   events signed by the daily anchor from #43); v1.2 closes the
   enforcement gap via an in-process SDK wrapper. See the
   [LiteLLM gateway concept doc](https://olafkfreund.github.io/AIFactory/concepts/litellm-gateway)
   for the full operator walkthrough.

### ✨ Added
- **GitHub PR Review Integration**: End-to-end support for PR reviews including listing, fetching, posting reviews, checking new commits, and viewing logs via dedicated API endpoints.
- **PR Review WebSocket Events**: Real-time progress, completion, and error events via WebSocket for live feedback during PR reviews.
- **PR Action Endpoints**: Support for posting reviews, commenting, merging, assigning, and canceling PRs through backend API.
- **AI-Powered Conflict Resolution**: Enhanced "Fix Conflicts with AI" functionality with real git merge and AI resolution of conflict markers.
- **Task from Chat Feature**: Button in Insights chat to convert conversation into a structured task (title + PRD description) with editable preview.
- **Open in Browser**: New "Open in Browser" button in EditorPage that serves files with correct MIME types and asset URL rewriting.
- **QA Fixer Phase**: Added separate `qa_fixer` phase in phase configuration, allowing independent model and thinking settings.
- **Phase-Scaled Progress**: Monotonically increasing progress percentages across phases (planning 0–20%, coding 20–80%, QA 80–95%, complete 95–100%).
- **Terminal Persistence**: TerminalGrid now remains mounted across view switches to prevent stuck terminals and lost PTY connections.
- **Model & Token Metrics**: Display assistant model name on chat messages and show tokens/sec metrics after each response across all providers.
- **Dark Theme & UI Improvements**: Enhanced folder navigation, keyboard support (Enter/Backspace), HTML preview, progress labels, and overall dark theme consistency.

### 🛠️ Fixed
- **GitHub PR Connection Detection**: Fixed incorrect endpoint call (`window.API.github.checkGitHubConnection` → `window.API.checkGitHubConnection`).
- **AI Merge Conflict Resolution**: Fixed syntax error in `github.py` caused by AI-generated extra closing brace.
- **requireReviewBeforeCoding Sync**: Ensured field is written to `task_metadata.json` when editing tasks.
- **Email Notifications**: Fixed silent failure under legacy token auth by populating default user context.
- **Build Progress & Subtask Status**: Added fallback in `post_session_processing` to detect new commits and force-update status.
- **File Serving 404s**: Resolved `404` errors for `/api/files/serve` by properly staging the endpoint and enabling public access with path-traversal protection.
- **Model Config Loss**: Fixed `UpdateModelConfigRequest` to preserve all fields (provider, profileId, model, thinkingLevel, temperature).
- **Issue-to-Task Creation**: Fixed backend `TaskMetadata` model to include `githubIssueNumber`, `affectedFiles`, and `acceptanceCriteria`.
- **Sidebar Layout**: Restored proper layout and spacing in sidebar components.

### 🔧 Changed
- **Project Renaming**: Renamed from "Claude Code Manager Web" to **AIFactory** across UI, navigation, and documentation.
- **MCP Template Filtering**: Removed redundant and duplicate quick templates (filesystem, fetch, github, gitlab) that conflict with native tools.
- **Hardcoded Model Values**: Replaced inline model/thinking defaults with shared constants to ensure user-configured settings take effect.
- **Git Ignore Safety**: Added `.aifactory-security.json` and `.aifactory-status` to `.gitignore` during project init and unstage during merges.
- **CLI Detection Optimization**: Improved speed using `shutil.which` and `npm package.json` parsing instead of slow Node.js startup (~4s → <50ms).

### 📦 Updated
- **README.md**: Updated project documentation with fixed GitHub URL, removed non-existent files, and added Docker deployment guide.
- **Phase Progress Logic**: Refactored progress logic to prevent backward jumps between phases using defined phase ranges.