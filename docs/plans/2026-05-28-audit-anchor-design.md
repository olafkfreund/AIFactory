# Design — ISO 27001 evidence + signed audit-chain anchor (Epic #35 #43)

> **STATUS: shipped 2026-05-28** — closed by #43. Implementation PRs: #181, #182, #183, #184, #185, #186. See [CHANGELOG.md](../../CHANGELOG.md) for the v1.1 release notes.

> Locked from super-brainstorm 2026-05-28. Implementation in 2 PRs
> after sign-off.

## Why we're doing this

The v1.0 audit chain (Epic #26 P5) hashes every row to its
predecessor. That **detects insertion / deletion / mutation** by an
attacker without DB access. It **does NOT detect** an attacker who
can re-compute the entire chain from any point forward — which any
DB admin can do (`audit_chain.py` explicitly calls this out at lines
11-15 as a documented v1.1 gap).

#43 closes that gap by signing the chain head daily with an
HMAC-SHA256 key wrapped by the existing KMS backend. External
verifiers receive both the rows AND the daily anchors, and can prove
untamperedness by re-computing the chain + verifying the HMAC.

It also formalises the ISO 27001 Annex A control mapping doc that
operators reference in their own ISMS, plus an access-review export
endpoint covering SOC2 CC6.2 / ISO 27001 A.9.2.5.

## Out of scope (explicit)

- **External anchor publication** (S3 WORM, RFC 3161 TSA, Sigstore /
  Rekor) — the v1.1 scope is "audit-chain anchor inline in export".
  We document the residual trust assumption: DB admin == compliance-
  trusted role. External-pub options remain a v1.2 expansion that
  reuses this PR's anchor format.
- **Asymmetric signatures.** HMAC suffices because the v1.1 verifier
  is the operator (who has the unwrapped key). Public verifiers come
  with external pub above.
- **Per-event signing.** Cadence is daily; per-event signing adds
  per-write overhead with no operational benefit at our scale.
- **GDPR-aware anchor recomputation.** When a GDPR erasure rewrites
  details_json + user_id, the chain reverifies because the canonical
  encoding is stable (P5.5 work). The anchor still verifies post-
  erasure. Documented in the concept doc.

## Locked decisions

### 1. Signing mechanism — HMAC-SHA256 with KMS-wrapped key

The web-server holds a 32-byte symmetric signing key wrapped by the
existing `crypto/kms/` backend (Fernet / AWS KMS / Vault / Azure /
GCP). The unwrapped key lives in memory for the pod's lifetime;
anchor signing is in-process.

```python
def sign_anchor(chain_head: str, signing_key: bytes) -> str:
    """HMAC-SHA256(key, chain_head) hex-encoded."""
    assert len(signing_key) == 32, "signing key must be exactly 32 raw bytes"
    return hmac.new(signing_key, chain_head.encode(), hashlib.sha256).hexdigest()
```

**KMS backend contract for this PR (reviewer finding #4):** every
backend's `decrypt(blob)` MUST return **exactly 32 raw bytes** when
fed a blob originally produced by `encrypt(32_raw_bytes)`. This is
implicit in the existing backend tests, but PR-1 adds an explicit
round-trip test per backend (`tests/audit/test_signing_key_kms_roundtrip.py`)
to nail this down — Fernet wraps differently from AWS KMS, and a
subtle envelope mismatch would silently produce wrong HMACs only on
the cloud backends.

**Key rotation — new `audit_signing_keys` table (reviewer finding #1):**

A single KMS rotation MUST NOT silently invalidate older anchors. We
store the wrapped key blob per version so historical anchors stay
verifiable:

```sql
CREATE TABLE audit_signing_keys (
    version       INTEGER PRIMARY KEY,           -- monotone; matches audit_anchors.key_version
    wrapped_key   BYTEA NOT NULL,                -- KMS-wrapped 32-byte raw key
    created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    retired_at    TIMESTAMP                      -- NULL = currently signing; non-NULL = verify-only
);
```

Rotation runbook:
1. Generate a new 32-byte raw key.
2. Wrap it with the current KMS root.
3. INSERT a new row; the previous row's `retired_at` is set to now.
4. Cron job picks up the highest-version non-retired row on next tick.

Verifier loads the wrapped key matching the anchor's `key_version`,
unwraps it via the current KMS root (the KMS rotation runbook from
P2 already handles re-wrapping the table contents).

**Operational safety:** the in-memory `signing_key` bytes are NEVER
logged. `audit_anchor.py` defines a `_SigningKey` newtype with
`__repr__` that returns `"<SigningKey v=N>"` (no key material).
Tests verify no logger call serialises the key by inspecting log
output for the raw bytes pattern.

**Why not asymmetric?** Sign API would add 3-5 days per-backend
(AWS KMS Sign, GCP KMS asymmetricSign, etc.) and no public verifier
exists at this stage. Move to asymmetric in v1.2 when external pub
lands.

### 2. Anchor cadence — Daily cron at 00:00 UTC

A background job in `apps/web-server/server/jobs/audit_anchor.py`
runs daily at 00:00 UTC.

**Timezone discipline (reviewer recommendation #3):** the job uses
`datetime.now(timezone.utc)` exclusively; the SQL queries it issues
include explicit `AT TIME ZONE 'UTC'` for any date-bucket arithmetic.
This guards against pods deployed with non-UTC `TZ` env or Postgres
configured with a non-UTC `TimeZone` GUC.

**Day-boundary semantics:** an anchor for day `D` covers all rows
with `created_at < D+1 (UTC midnight)`. The chain head signed is
`prev_hash` of the last `audit_logs` row whose `created_at < D+1`.
Rows arriving after `D+1` are covered by day `D+1`'s anchor.

**Idempotency (reviewer recommendation #2):** unique constraint on
`(DATE(signed_at AT TIME ZONE 'UTC'))` on `audit_anchors`. Two
concurrent triggers (cron + manual /admin endpoint) racing for the
same day's anchor — one wins, the other 409s with "anchor already
exists for YYYY-MM-DD".

**First anchor + zero-row days (reviewer finding #5):**
- **First anchor (no prior anchor, no audit rows):** emit an anchor
  with `chain_head_hash = GENESIS` ("the system was quiescent since
  genesis"). Verifier accepts.
- **First anchor (no prior anchor, audit rows exist):** emit with
  the latest row's `prev_hash`. Standard.
- **Zero-row day:** emit anyway, signing the same `chain_head_hash`
  as the previous anchor. Verifier sees consecutive anchors with
  identical `chain_head_hash` as "quiescent day" evidence. Each row
  still has a unique `signed_at` so the unique-on-date constraint
  isn't violated.

**Backfill correctness (reviewer recommendation #1):** the startup
backfill computes each missed day's anchor against `prev_hash` of
the last row with `created_at < day_end(D)`, NOT against the current
chain head. Without this, backfill anchors silently sign post-day
data.

**Why daily not hourly?** Daily matches the audit-log retention
granularity + operator habits. Tampering window of ≤24h acceptable
per the threat model.

### 3. External publication — Local-only inline in export

Anchors live in `audit_anchors` on the same DB. The export endpoint
(reusing `/api/admin/audit/export` from P5) interleaves anchor rows
into the NDJSON stream so external verifiers receive them with the
data they sign.

**Deterministic placement rule (reviewer finding #3 + recommendation #4):**

The NDJSON stream is ordered by `created_at ASC`. Anchor rows are
inserted at the point where the next row's `created_at >= anchor.signed_at`
(i.e. anchors appear AFTER the last row they cover). Verifiers process
the stream in order:
- maintain a running `prev_hash` (start = GENESIS)
- for each row: re-compute `prev_hash` from the row's content; assert
  it matches the row's `prev_hash` column
- for each anchor: assert the running `prev_hash` equals
  `anchor.chain_head_hash`; verify HMAC; reset window
- rows AFTER the last anchor form a "pending window" that's expected
  and accepted (will be closed by the next day's anchor)

The "pending window" rule is documented explicitly so off-hours
exports don't appear corrupt.

**CSV export (existing P5 surface):** CSV cannot interleave anchor
records meaningfully — different schema. PR-1 ships the NDJSON
interleave; CSV exports add anchors as a SIDECAR file
(`audit_anchors_<window>.csv`) downloaded alongside. The route
documents this and operators choosing CSV opt out of chain
verifiability (CSV is for spreadsheet review, not auditing).

**Filtered exports (reviewer finding #3):** the
`?max_classification=internal` filter described in §5 below **does
NOT interleave anchors**. Filtered exports cannot be chain-verified
(removing `confidential` rows leaves gaps in `prev_hash` continuity
the anchor cannot reconcile). The route returns a 400 if the caller
combines `?max_classification` with `?include_anchors=true` AND a
clear `X-AIFactory-Verifiable: false` header on filtered NDJSON. The
concept doc says explicitly: "filtered exports are for spreadsheet
review; auditor-bound exports MUST be unfiltered."

**Trust scope:** an attacker with DB write access AND the unwrapped
HMAC key can rewrite anchors. We document this explicitly: the
HMAC key path (mounted via existing KMS plumbing) must be at least
as protected as the DB itself. External pub (v1.2) closes the gap
properly by writing anchors to a target the DB admin can't rewrite.

### 4. `audit_anchors` schema

```sql
CREATE TABLE audit_anchors (
    id              VARCHAR(36) PRIMARY KEY,
    chain_head_hash VARCHAR(64) NOT NULL,    -- hex SHA-256 of last row
    signature       VARCHAR(64) NOT NULL,    -- hex HMAC-SHA256
    signed_at       TIMESTAMP NOT NULL,
    key_version     INTEGER NOT NULL,        -- which wrapped key signed
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_audit_anchors_signed_at ON audit_anchors(signed_at);
```

Append-only at the application layer (no PATCH / DELETE routes).

### 5. Data classification — three tiers

New column on `audit_logs`:
```python
classification: Mapped[str] = mapped_column(
    String(16), nullable=False, server_default="internal",
)
```

With CHECK constraint (Postgres) / application-side validator (SQLite):
`classification IN ('public', 'internal', 'confidential')`.

Default is `'internal'`. Action classifiers in
`apps/web-server/server/services/audit_service.py` set the right
value per action kind:
- `'public'` — actions whose visibility is OK in any export (e.g.
  `health.check`)
- `'internal'` — most operator actions (default)
- `'confidential'` — KMS access, key rotation, audit-chain
  rewrites, GDPR erasure events

**Chain-protection decision (reviewer finding #2):** `classification`
**IS included in `_canonical()`**. An attacker flipping
`confidential→public` to leak rows past the `?max_classification`
filter would otherwise be undetectable. The cost: existing pre-#43
rows have no classification (NULL would be canonical-encoded as the
empty string, matching what the new default produces). The migration
back-fills all existing rows with `'internal'` BEFORE adding the
NOT NULL constraint, then `_canonical()` reads `row.get("classification") or ""`
in a way that maintains hash compatibility for the migrated rows.

**Migration verification:** PR-1 adds a test that:
1. Builds an audit chain with the old `_canonical()` encoding (no
   classification field).
2. Runs the migration with the backfill.
3. Re-verifies the chain with the new `_canonical()`. MUST PASS.

If the verification breaks, the migration logic is wrong — we have
to either pre-compute new hashes per row OR cement that
classification is OUTSIDE the chain. We commit to inside-the-chain
because outside leaves the leak vector open.

**GDPR erasure interaction:** the P5.5 erasure path rewrites
`details_json` + `user_id`. Classification is left untouched (it's
not PII). The chain re-verifies post-erasure because the canonical
encoding is identical pre/post for the classification field.

**Filtered exports** (`?max_classification=internal`) are
non-verifiable per §3 above. The filter is for routine review work,
not auditor-bound exports.

### 6. Access-review export — `GET /api/admin/access-review`

```
GET /api/admin/access-review?org=<org_id>
Authorization: Bearer <admin-token>

Response (NDJSON):
{"user_id": "...", "email": "...", "role": "owner", "active": true, "last_login_at": "2026-05-27T..."}
{"user_id": "...", "email": "...", "role": "member", "active": true, "last_login_at": "2026-05-21T..."}
...
```

Includes every CURRENT `OrgMember` with their User row's email,
role, `last_login_at`, `is_active`.

**Honesty about historical state (reviewer recommendation #6 +
finding):** the original design proposed `?as_of=YYYY-MM-DD`. Audit
of the schema shows `OrgMember` has no `deactivated_at` history
column, so we cannot show membership-at-date — we can only show
current state. Two options weighed:

1. **Drop `?as_of`** — endpoint returns current state only.
   Doc explicit. (Locked.)
2. Add `org_member_history` audit table — scope creep into a
   separate epic.

Auditors evaluating CC6.2 / A.9.2.5 typically run access reviews
quarterly against current state + the audit log's
`org.member.add/remove` events. The audit log provides the "what
changed since last review" delta. Documented explicitly in the
concept doc.

**`last_login_at` source (reviewer open question + audit):** the User
model has no `last_login_at` column. PR-1 adds it via migration:

```python
# users.last_login_at — DateTime, nullable, updated by auth.py on
# every successful login (OIDC, SAML, password). Used by the
# access-review export. NULL for users who have never logged in.
last_login_at: Mapped[datetime | None] = mapped_column(
    DateTime, nullable=True,
)
```

The auth path (`server/auth.py` + OIDC callback + SAML ACS) issues
a single UPDATE per successful auth. Negligible cost; indexed if the
access-review query is slow at large org sizes (deferred to v1.2
profiling).

Reuses the existing admin auth dependency (no new auth surface).
SOC2 CC6.2 + ISO 27001 A.9.2.5 evidence.

### 7. ISO 27001 Annex A control map — `guides/compliance/iso27001-evidence.md`

Document scopes ~30-40 controls AIFactory directly evidences:

- **A.5 (Information security policies)** — operator-owned; doc points
  to README + SECURITY.md
- **A.8 (Asset management)** — operator-owned + AIFactory's KMS
  data-key inventory
- **A.9 (Access control)** — A.9.2.1 user registration via OIDC
  (Epic #26 P3) + SAML (Epic #41 — **partially implemented as of
  this writing**; SAML/SCIM routes pending in #41 PR-1b2/1b3);
  A.9.2.5 access reviews (new endpoint above); A.9.4.1 RBAC via
  OrgMember. Any control entry citing #41 is explicitly marked
  "partially implemented / planned" until #41 closes.
- **A.10 (Cryptography)** — A.10.1.1 encryption at rest (P2);
  A.10.1.2 key management (KMS + this PR's rotation)
- **A.12 (Operations security)** — A.12.1.4 dev/prod separation;
  A.12.4 logging (audit chain + this PR's anchor); A.12.6.1
  vulnerability management (CI security-scan job)
- **A.13 (Communications security)** — A.13.1.1 network controls
  (NetPol in #36); A.13.2.1 transfer policies
- **A.14 (System acquisition / dev / maintenance)** — A.14.2.1 secure
  development (CI gates); A.14.2.8 testing (acceptance test matrix)
- **A.16 (Incident management)** — operator-owned; doc points to
  incident response runbooks
- **A.17 (BCP)** — operator-owned + AIFactory's backup story
- **A.18 (Compliance)** — A.18.1.3 audit logs (this PR closes
  v1.0 gap); A.18.1.4 PII protection (GDPR erasure)

Each control entry: **Control text** (verbatim or close), **AIFactory
contribution** (what code/feature evidences this), **Operator
responsibility** (what the operator must add). Living doc; non-author
review before publishing.

### 8. CI — in-process tests in P5 acceptance job

`tests/audit/test_anchor_*.py` covers:
- HMAC sign + verify round-trip with a fixed test key
- Daily-cron job back-fills missing anchors on startup
- Export NDJSON interleaves anchor records correctly
- Verifier (reusable utility) re-computes chain + verifies anchor
- GDPR-erased rows still verify post-erasure (regression for P5.5
  interaction)
- Access-review export endpoint shape + filtering

Marker: `@pytest.mark.audit`. Existing **audit (P5 acceptance)** CI
job picks them up.

## Implementation plan — 2 PRs

### PR-1 — Anchor core + cron + classification + access review

- Alembic migration: `audit_anchors` table + `audit_logs.classification`
  column.
- `apps/web-server/server/services/audit_anchor.py` — HMAC signer,
  key loading (KMS unwrap), `sign_chain_head()`, `verify_anchor()`.
- `apps/web-server/server/jobs/audit_anchor_cron.py` — daily-cron
  job + startup backfill.
- `apps/web-server/server/services/audit_export.py` — interleave
  anchor records into the NDJSON stream.
- `apps/web-server/server/services/audit_service.py` — classification
  on every event.
- `apps/web-server/server/routes/admin.py` (or new module) —
  `GET /api/admin/access-review` endpoint.
- Tests for each.

### PR-2 — Helm + ISO 27001 doc + concept doc

- `charts/aifactory/values.yaml` — `audit.anchor:` block
  (`enabled`, `keySecretName`, `cron.schedule`) + validators.
- `charts/aifactory/templates/cronjob.yaml` — Kubernetes CronJob
  resource for the daily anchor job (alternative: lifespan-managed
  asyncio task; pick at PR-2 time based on operator preference).
- `tests/helm/test_audit_anchor_toggle.py`.
- `guides/compliance/iso27001-evidence.md` — the Annex A doc.
- `docs/docs/concepts/audit-anchor.md` — user-facing concept page.
- `docs/sidebars.ts` entry.
- Update `CHANGELOG.md` v3.0 limitation #1 strike-through.

## Failure-safe contract

Same as #42 + #41: every anchor / classification code path wrapped in
`try/except`. A broken anchor job logs WARNING + retries next tick;
audit log writes always proceed even if classification can't be
resolved (default to `'internal'`).

## Threat model summary

| Threat | v1.0 (P5) | v1.1 (#43) |
|--------|-----------|------------|
| Replay-attacker rewrites N rows | Detected via chain | Detected via chain + cross-reference to anchor |
| Read-replica replayed forward | Detected via chain | Detected via chain + anchor mismatch |
| DB admin re-signs entire chain | **Undetected** | Detected unless admin also has HMAC key |
| DB admin re-signs chain AND has HMAC key | Undetected | Undetected (v1.2 external pub closes) |
| Offline export tampered between download and audit | N/A — verifier re-computes locally | Detected — anchor verifies the chain head |

## Decision audit summary

8 of 8 brainstorm decisions taken on recommended options. Reviewer
audit pass added 5 critical findings + 6 recommendations, all baked
in above:

| Finding | Resolution |
|---------|------------|
| `audit_signing_keys` table missing | Added in §1 — versioned wrapped-key store |
| `classification` chain protection ambiguous | Locked: IN `_canonical()`; migration backfills before NOT NULL |
| Filtered export non-verifiable | Documented + filter mutually exclusive with `include_anchors` |
| KMS `decrypt()` return-type contract | Explicit 32-byte assertion + per-backend round-trip test |
| First anchor / zero-row day behaviour | Specified in §2 |
| Backfill uses end-of-day chain head | Specified in §2 |
| Concurrent cron + manual trigger | Unique constraint on `DATE(signed_at AT TIME ZONE 'UTC')` |
| Timezone discipline | UTC explicit in code + SQL |
| NDJSON anchor placement | Deterministic rule + pending-window doc |
| `User.last_login_at` doesn't exist | Migration in PR-1 adds it |
| Access-review `?as_of` misleading | Dropped; current-state only with audit-log delta narrative |
| ISO 27001 doc cites partially-shipped #41 | Marked "partially implemented / planned" |
| `signing_key` log safety | `_SigningKey` newtype + `__repr__` + log-bytes test |

No deviations from brainstorm intent — refinements tighten the
design without changing scope.
