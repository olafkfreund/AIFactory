---
title: Trusted-plan Key Rotation
sidebar_position: 3
---

# Trusted-plan Key Rotation

The trusted-plan fast path lets an upstream authority (PFactory authoring,
CFactory governance) hand AIFactory a signed `implementation_plan.json` and skip
re-running the spec pipeline. Trust rests on an HMAC-SHA256 signature over the
canonical plan, keyed by a shared secret in
`AIFACTORY_TRUSTED_PLAN_KEY_<AUTHORITY>`. See `apps/backend/trusted_plan.py`.

## The gap this closes (#323, #310)

Before rotation support, each authority had exactly one key and no key id:

- A leaked authority key was valid **forever** — there was no way to revoke it.
- Rotating meant editing the single env var in place, which **instantly
  invalidated every plan signed with the old value** (downtime), and offered no
  overlap window.
- Nothing recorded *which* key signed a given plan.

## The mechanism: key id (`kid`) + keyring

A signed approval envelope may now carry an optional `kid`. Multiple
verification keys can be active for one authority at the same time:

| Env var | Keyring entry | Matches |
|---|---|---|
| `AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY` | `cfactory` | envelopes with **no** `kid` (legacy) |
| `AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY__2026Q3` | `cfactory/2026q3` | envelope `kid: 2026Q3` (case-insensitive) |
| `AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS` | — | comma list of `authority/kid` or bare `kid` to reject |

The signer stamps the matching `kid` into the envelope, and the id is **bound
into the signed bytes** — an attacker cannot relabel a captured envelope to point
at a different key without breaking the signature.

At verify time:

- An envelope with a `kid` verifies against `authority/kid`, unless that kid is
  retired (rejected first, even if the key material is still present).
- An envelope with no `kid` verifies against the legacy authority-only entry,
  exactly as before — the pre-rotation handshake is byte-identical and keeps
  working.

## Rotation runbook (zero downtime)

1. **Introduce** the new key alongside the old:
   `AIFACTORY_TRUSTED_PLAN_KEY_CFACTORY__2026Q4=<new secret>`. Both keys now
   verify.
2. **Cut over** signers to emit `kid: 2026Q4` (sign with the new key). Plans
   signed with either key still verify during the overlap.
3. **Drain**: wait until no in-flight plan was signed with the old key.
4. **Retire** the old key: add its id to
   `AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS` (e.g. `cfactory/2026q3`), then remove
   its env var. Any lingering envelope signed with it is now rejected.

## Emergency revocation (leaked key)

Add the compromised kid to `AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS` and roll out.
Verification rejects it immediately — no need to wait for the key material to be
scrubbed from every environment. If the leaked key was the legacy (no-kid)
entry, unset `AIFACTORY_TRUSTED_PLAN_KEY_<AUTHORITY>` and issue a keyed
replacement.

## Backward compatibility

`kid` is optional throughout. `sign_plan(...)` without `kid` produces the legacy
envelope and legacy signed bytes; `verify_plan_signature` accepts it against the
legacy keyring entry. Existing keys, env vars, and stored plans keep verifying
with no change.
