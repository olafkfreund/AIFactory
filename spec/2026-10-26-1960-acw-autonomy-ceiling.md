---
status: approved
issue: 1960
intent: https://github.com/olafkfreund/Factory/blob/docs/1960-agent-git-identity/intent/2026-10-26-1960-agent-git-identity.md
---

# Spec: autonomy ceiling and agent identity on `acw_` keys

Slice 1 of olafkfreund/Factory#1960 (the epic plan's stage 2). The intent lives in the Factory repo because the epic does (link above).

## Design

1. **Ceiling column.** `ApiKey.autonomy_ceiling` (`apps/web-server/server/database/models.py`, ~line 378): `String(16)`, nullable, no default. It holds the most autonomous tier the key's agent may act at, in `merge_policy.py`'s own tier vocabulary (`low`/`medium`/`hard`). Alembic migration `<rev>_api_key_autonomy_ceiling.py` adds the column, `down_revision = "5e2d9c1a7b40"` (the current head); the downgrade drops it. Existing rows stay null.
2. **Agent identity.** A read-only `ApiKey.agent_identity` property returns `f"agent:aifactory/{self.id}"`. It is derived, not stored: it cannot drift from the key, and revoking the key retires the identity. `key_id` is already `str(ApiKey.id)` (`mcp_remote/auth.py:131`), so audit rows that already carry `key_id` map to it directly.
3. **Effective tier.** A pure function in `apps/backend/merge/merge_policy.py`, `cap_disposition(decision, ceiling) -> str`, returns the stricter of a `decide_merge` disposition and the ceiling's disposition (`low`→`auto-merge`, `medium`→`hold-async`, `hard`→`hold-blocking`, aliases accepted through `_TIER_ALIASES`).
   - `ceiling is None` or blank: returns `decision` unchanged, so behaviour is unchanged for every existing key.
   - Unrecognised ceiling or unrecognised decision: returns `hold-blocking`. It fails closed, like `decide_merge` does for an unknown tier (#1158).
   - It never loosens a decision.
4. **Setting it.** `CreateApiKeyRequest` gains an optional `autonomy_ceiling: Literal["low", "medium", "hard"] | None = None`, passed to the model. `ApiKeyResponse` gains `autonomy_ceiling: str | None = None` and `agent_identity: str | None = None`, filled in by the create and list routes. The fields are additive and absent fields stay null.
5. **Who may set it** (review of PR #1887, decided by the lead):
   - `POST /api/keys` returns 403 when the caller authenticated with an `acw_` key (`request.state.user` carries `api_key_id`). Keys cannot mint keys, so a key holder cannot issue itself a new key without its ceiling.
   - A non-null `autonomy_ceiling` needs the org `admin` role (or higher), checked with the same `_role_level` rule `require_org_role("admin")` uses. The rule is applied inline because `org_id` is in the request body and the check only applies when a ceiling is set.
   - No ceiling stays open to members, for backward compatibility.

Not in this slice: wiring `cap_disposition` into the live merge path. That needs the merge path to know which key started the run (slice 2, originator/key capture at intake). `mcp_stdio/auth.py` is not touched, because AIFactory #1885 is changing it.

## Alternatives rejected

- **Ceiling as a disposition string** (`auto-merge`…). Rejected because `merge_policy.py` and #1961's `autonomy_tier` already speak in `low/medium/hard`. A second vocabulary would need a mapping that could drift.
- **Ceiling in the comma-separated `scopes` column** (e.g. `tier:low`). Rejected: scopes are capabilities, and parsing a tier out of a scope string invites a missing entry being read as "no limit".
- **Storing `agent_identity` as a column.** Rejected: it is a pure function of `id`, so storing it only adds a way to disagree.
- **A default ceiling for new keys.** Rejected by decision: a missing tier is null, never a default (#1961 rule against fabricated governance values).
- **Editing `mcp_stdio/auth.py`** to expose the tier on the auth context. Deferred until #1885 lands.

## Risks

- The migration adds one nullable column: no lock beyond a brief `ALTER TABLE`, no backfill. SQLite (tests) and Postgres both support it.
- `cap_disposition` is unused at runtime in this slice, so it cannot change any merge. Its risk is in the later wiring, which will be tested then.
- **No ceiling still means no limit.** A member can create a key with no ceiling, which is the least restricted key. That is acceptable only while `cap_disposition` gates nothing. **Hard gate:** before slice 2 wires `cap_disposition` into the merge path, unceiled keys must either default to the strictest tier (`hard`) or be refused for agent work. Slice 2 cannot ship without one of the two.
- Ceilings are set by admins, and keys cannot mint keys, so a key holder cannot lift its own ceiling by minting a fresh key.

## Verification

- `tests/test_merge_policy.py`: `cap_disposition` across a matrix of decision × ceiling, `None` passthrough, alias acceptance, unknown ceiling or unknown decision → `hold-blocking`, and never looser than the decision.
- Route tests (SQLite + TestClient): an `acw_`-authenticated create gets 403; a member setting a ceiling gets 403; an admin setting a ceiling gets 201; a member with no ceiling gets 201; create stores the ceiling, and create and list both return `autonomy_ceiling` and `agent_identity`.
- `tests/test_api_key_autonomy_ceiling.py`: a key with a ceiling carries it and `agent_identity == "agent:aifactory/<id>"`; a key without one has `autonomy_ceiling is None`; the request model rejects an unknown ceiling and defaults to `None`; the migration chains onto the current head and leaves a single head.
- ruff, the mypy ratchet on touched files, and targeted pytest.
