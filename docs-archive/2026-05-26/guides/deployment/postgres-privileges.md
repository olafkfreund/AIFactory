# Postgres privileges for AIFactory

> Audience: bank/fintech platform / DBA teams provisioning the Postgres
> role AIFactory's web-server connects with.
>
> Goal: the minimum privilege set that lets the app run + Alembic
> migrate, **without superuser, without `CREATE EXTENSION`, without
> ownership of the database itself**.

## Why this matters

Banks separate database administration (`postgres` superuser, owned by
the DBA team) from application access (the role AIFactory uses to read,
write, and migrate its own tables). AIFactory's migration must run
under the **app role** — not as DBA — because in a typical K8s deployment
the migration is a Helm `Job` running with the same secret as the
runtime pods.

AIFactory is deliberately designed so this is possible: **UUIDs are
generated in Python (`uuid.uuid4()`), not by Postgres**, so no
`pgcrypto` or `uuid-ossp` extension is required. The migration touches
only tables and indexes — no `CREATE EXTENSION`, no `CREATE TYPE`, no
`CREATE FUNCTION` in untrusted languages.

## Required privileges

Provision the app role with **exactly** these privileges. Substitute
`aifactory_app` for the role name your platform standard uses.

```sql
-- 1. The role itself. LOGIN only, no SUPERUSER, no CREATEDB, no CREATEROLE.
CREATE ROLE aifactory_app LOGIN PASSWORD '<generated>';

-- 2. The database. The DBA team owns it; the app role only connects.
--    (Replace `aifactory` with your chosen DB name.)
CREATE DATABASE aifactory;

-- 3. Connect privilege on the database.
GRANT CONNECT ON DATABASE aifactory TO aifactory_app;

-- 4. Schema privileges. The app role must be able to read and write
--    the schema where its tables live. CREATE is required so Alembic
--    can `CREATE TABLE` / `CREATE INDEX` during migrations.
\c aifactory
GRANT USAGE, CREATE ON SCHEMA public TO aifactory_app;

-- 5. Default privileges so any tables/sequences the app role creates
--    in the future remain owned by the role and usable by it.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aifactory_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO aifactory_app;
```

That's all. The app role does NOT need:

| ❌ Privilege | Why not |
|---|---|
| `SUPERUSER` | Never — superuser bypasses all checks |
| `CREATEDB` | The DB is created by the DBA team out-of-band |
| `CREATEROLE` | The app doesn't manage Postgres roles |
| `CREATE EXTENSION` | AIFactory uses no Postgres extensions |
| Ownership of database | Connect + schema grants are sufficient |
| `pg_read_server_files` / `pg_write_server_files` | No file I/O from SQL |
| Replication role attribute | App is a client, not a replication target |

## Verifying privileges

After provisioning, sanity-check from any psql client:

```sql
-- Confirm role attributes (should all be `f`, except `rolcanlogin`)
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin
FROM pg_roles WHERE rolname = 'aifactory_app';

-- Confirm schema privileges
SELECT has_schema_privilege('aifactory_app', 'public', 'USAGE');   -- t
SELECT has_schema_privilege('aifactory_app', 'public', 'CREATE');  -- t
```

## Running Alembic as the app role

This is what AIFactory's own boot path does when `APP_MIGRATIONS_AUTO_APPLY=true`,
or what the Helm Job does when `APP_MIGRATIONS_AUTO_APPLY=false`. Either
way, the connection is the app role's, not the DBA's:

```sh
DATABASE_URL="postgresql+asyncpg://aifactory_app:<password>@db.internal:5432/aifactory" \
  alembic upgrade head
```

Expected output ends with:

```
INFO  [alembic.runtime.migration] Running upgrade  -> <revision>, baseline_initial_schema
```

If you see `permission denied for schema public`, the `GRANT USAGE, CREATE
ON SCHEMA public` step was missed. If you see `must be owner of database`,
something is trying to `ALTER DATABASE` which AIFactory's migrations
never do — file a bug.

## Rotating the password

The role's password is read from the `DATABASE_URL` env var, sourced from
your secret manager via the `database-url` `ExternalSecret` in the Helm
chart (see Epic #26 P2 — encrypted secrets at rest). Rotating it is:

```sql
ALTER ROLE aifactory_app PASSWORD '<new>';
```

Then update the secret-manager entry; the next pod restart picks it up.
No application-side coordination required because there's no in-flight
DB-credential cache beyond the connection pool.

## Auditor cheat-sheet

For a SOC2 / ISO 27001 evidence packet on least-privilege database access:

- **Role privilege snapshot**: the `pg_roles` query above, captured at
  install time
- **Schema grant snapshot**: the `has_schema_privilege` query above
- **Migration log**: the `alembic upgrade` output from the runbook, showing
  the role under which migrations ran (NOT `postgres` / superuser)
- **Confirmation no extensions are used**: `SELECT extname FROM pg_extension;`
  inside the `aifactory` database should show only the built-in
  `plpgsql` (which ships with every Postgres database)

Cross-reference Epic #26 issue #34 (SOC2 evidence pack) when this role
enters production.
