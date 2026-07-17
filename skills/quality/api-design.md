# api-design

> Source: curated best practices | 2026

---

# API Design - predictable, consistent, and hard to misuse

A good API is boring: resources are named the way you would guess, status codes mean what the spec says, errors have a shape you can parse, and nothing surprises the caller. Consistency beats cleverness — once a client learns one endpoint, every other endpoint should behave the same way. Design for evolution from day one (versioning, additive changes) because the one thing guaranteed about a public API is that you will need to change it without breaking the callers who depend on it.

## When to Activate

Use when designing or reviewing any interface others call:
- adding or changing a REST/HTTP endpoint, GraphQL schema, or RPC method
- deciding status codes, request/response shapes, or error formats
- adding pagination, filtering, or bulk operations
- versioning an API or making a breaking change
- writing or updating an OpenAPI/schema spec

## Principles and Practices

**Resource naming (REST).** Nouns, plural, lowercase, hierarchical. The HTTP verb carries the action — do not put verbs in the path.

```
GET    /users              list users
POST   /users              create a user
GET    /users/42           fetch one
PATCH  /users/42           partial update
DELETE /users/42           delete
GET    /users/42/orders    that user's orders (sub-resource)
```

Avoid `/getUser`, `/createUserNow`, `/users/42/delete`. Keep casing and pluralization consistent across the whole API.

**Status codes that mean what they say.** Do not return `200 {"error": ...}`.
- `200` OK, `201` Created (with a `Location` header), `204` No Content (successful delete).
- `400` malformed request, `401` not authenticated, `403` authenticated but not allowed, `404` not found, `409` conflict (duplicate, version mismatch), `422` semantically invalid, `429` rate-limited.
- `500` you broke, `503` dependency down. Never return `500` for a user's bad input.

**Consistent error shape.** Every error, everywhere, same envelope — so clients write one parser.

```json
{ "error": { "code": "validation_error",
             "message": "email is required",
             "field": "email",
             "request_id": "req_abc123" } }
```

Machine-readable `code`, human-readable `message`, and a `request_id` the caller can quote in a support ticket. Never leak stack traces or SQL.

**Pagination — always, for any list that can grow.** An unbounded list is a latent outage. Prefer cursor-based pagination (stable under inserts, scales) over offset for large or fast-changing sets; offset is fine for small admin lists.

```
GET /users?limit=50&cursor=eyJpZCI6NDJ9
→ { "data": [...], "next_cursor": "eyJpZCI6OTJ9" }   # null when done
```

Cap `limit` server-side (a client asking for 1,000,000 gets 100). Return the page plus how to get the next one.

**Versioning from day one.** Put a version in the URL (`/v1/users`) or a header. Within a version, only make **additive, backward-compatible** changes: add optional fields, add endpoints. Removing a field, renaming, changing a type, or tightening validation is breaking — that needs `/v2`. Clients ignore unknown fields, so additions are safe.

**Idempotency for unsafe operations.** `GET`, `PUT`, `DELETE` are idempotent by HTTP semantics. `POST` is not — so for create/payment endpoints, accept an `Idempotency-Key` header and dedupe on it, so a client that retries after a timeout does not create two orders or charge twice.

**Validate every input, reject clearly.** Validate types, required fields, ranges, and formats at the boundary; return `400`/`422` with a body that names the offending field. Never trust the client. Set sane defaults for optional params and document them.

**Design the response for the client, not the database.** Do not expose internal column names, join tables, or auto-increment IDs that leak volume. Return what the consumer needs in a stable shape. Prefer opaque IDs. Use ISO-8601 UTC for timestamps everywhere.

**Document with a machine-readable spec.** Maintain an OpenAPI (REST) or SDL (GraphQL) spec as the source of truth — it generates clients, validates requests, and gives callers real docs. An endpoint without a spec entry does not exist. Keep the spec in the repo and in review.

**Filtering, sorting, sparse fields via query params.** `GET /users?status=active&sort=-created_at&fields=id,name`. Consistent conventions across resources; document the allowed values.

## Anti-patterns

- Verbs in paths (`/getUsers`, `/users/1/doDelete`) — let the HTTP method say the action.
- `200 OK` wrapping an error body; using `500` for bad user input.
- A different error shape per endpoint, forcing clients to special-case each.
- List endpoints with no pagination or no server-side cap on page size.
- Breaking changes (renamed/removed fields, tightened validation) inside an existing version.
- Non-idempotent create/payment endpoints with no idempotency key — retries double-charge.
- Leaking DB schema, internal IDs, or stack traces through the response.
- Inconsistent casing/pluralization/date formats across the API surface.
- No OpenAPI/schema spec, so the only documentation is the source code.
