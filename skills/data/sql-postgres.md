# sql-postgres

> Source: curated best practices | 2026

---

# PostgreSQL - Production schema design, indexing, and safe queries

PostgreSQL 16 is the default relational store for transactional workloads: strong data types, constraints, transactional DDL, mature indexing, and rich JSON support. This skill covers schema design that encodes invariants in the database, index selection driven by real query shapes, zero-downtime migrations, parameterized queries that eliminate SQL injection, and connection pooling for high-concurrency services.

## When to Activate

Use when the task involves PostgreSQL:
- Designing tables, constraints, or relationships
- Writing or reviewing SQL queries (SELECT/INSERT/UPDATE joins, CTEs, window functions)
- Adding indexes or diagnosing slow queries with EXPLAIN
- Writing migrations (adding columns, backfills, altering constraints)
- Connection pooling, transactions, or isolation-level questions

## Patterns and Best Practices

### Schema design — encode invariants in the database

```sql
CREATE TABLE users (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email       citext NOT NULL UNIQUE,          -- case-insensitive, unique
    status      text   NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','suspended','deleted')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE orders (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    total_cents integer NOT NULL CHECK (total_cents >= 0),  -- money as integer cents
    placed_at   timestamptz NOT NULL DEFAULT now()
);
```

Rules: use `timestamptz` never `timestamp`; store money as integer cents (never float); use `bigint` identity keys; put `CHECK` constraints and `NOT NULL` on everything you can; name foreign keys with explicit `ON DELETE` behavior.

### Indexing — match the query, not the column

```sql
-- Composite index: column order = equality columns first, then range/sort
CREATE INDEX idx_orders_user_placed ON orders (user_id, placed_at DESC);
-- Serves: WHERE user_id = $1 ORDER BY placed_at DESC LIMIT 20

-- Partial index: only the rows you actually query
CREATE INDEX idx_users_active ON users (email) WHERE status = 'active';

-- Covering index (index-only scans) via INCLUDE
CREATE INDEX idx_orders_lookup ON orders (user_id) INCLUDE (total_cents, placed_at);

-- Create without locking writes in production
CREATE INDEX CONCURRENTLY idx_orders_placed ON orders (placed_at);
```

Verify with `EXPLAIN (ANALYZE, BUFFERS)`. A `Seq Scan` on a large table in a hot path means a missing or unusable index (e.g. a function applied to the column, or leading wildcard `LIKE '%x'`).

### Parameterized queries — never interpolate

```python
# psycopg 3 — driver binds params server-side; injection impossible
cur.execute(
    "SELECT id, email FROM users WHERE status = %s AND created_at > %s",
    ("active", cutoff),
)

# WRONG — string formatting is an injection hole, never do this:
# cur.execute(f"SELECT * FROM users WHERE email = '{email}'")
```

Identifiers (table/column names) can't be parameterized — allowlist them against a fixed set, never pass user input through.

### Transactions and isolation

```python
with pool.connection() as conn:          # autocommit off inside the block
    with conn.transaction():             # BEGIN … COMMIT / ROLLBACK on error
        conn.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s", (amt, src))
        conn.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s", (amt, dst))
```

Use `SERIALIZABLE` for invariants across rows and retry on `40001` serialization failures. Prefer optimistic concurrency (a `version` column checked in the `WHERE`) over long-held locks. Never hold a transaction open across a network/user round trip.

### Zero-downtime migrations

```sql
-- Adding a NOT NULL column safely (3 steps, not one):
ALTER TABLE users ADD COLUMN plan text;                 -- 1. nullable, instant
UPDATE users SET plan = 'free' WHERE plan IS NULL;      -- 2. backfill in batches
ALTER TABLE users ALTER COLUMN plan SET NOT NULL;       -- 3. validate

-- Validate a new constraint without a long table lock:
ALTER TABLE orders ADD CONSTRAINT chk_total CHECK (total_cents >= 0) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT chk_total;       -- scans without blocking writes
```

Backfill large tables in bounded batches (`WHERE id BETWEEN … LIMIT 10000`) to avoid long transactions and table bloat.

### Connection pooling

Postgres backends are expensive; open a bounded pool, not a connection per request. Size the app pool small (`max_size ≈ cores * 2`) and put PgBouncer (transaction mode) in front when many app instances multiply connections toward `max_connections`.

```python
from psycopg_pool import ConnectionPool
pool = ConnectionPool(conninfo, min_size=2, max_size=10, timeout=5)
```

## Anti-patterns

- String-interpolating user input into SQL — always parameterize.
- `SELECT *` in application code — name columns so schema changes don't break callers.
- Storing money as `float`/`real` — rounding errors; use integer cents or `numeric`.
- `timestamp` without time zone — use `timestamptz`.
- One index per column hoping the planner combines them — build composite indexes matching real WHERE/ORDER BY shapes.
- `CREATE INDEX` (non-concurrent) on a live large table — locks writes; use `CONCURRENTLY`.
- Adding a `NOT NULL DEFAULT` + constraint in one blocking statement on a huge table.
- Holding a transaction open across user input or external HTTP calls.
- `ORDER BY id OFFSET 100000` for pagination — use keyset pagination (`WHERE id > $last`).
