# factory_common (vendored from the Factory hub)

This package is a **byte-identical vendored copy** of the canonical
`shared/factory-common/factory_common/` layer in the
[Factory hub](https://github.com/olafkfreund/Factory) — the single source of
truth for the fleet's deduped, stdlib-only utility primitives (epic Factory#154,
issue Factory#161):

- `factory_common.secrets` — the canonical secret-pattern table + `redact()` /
  `scan()` / `contains_secret()`.
- `factory_common.http` — the Cloudflare-friendly typed `urllib` JSON client.
- `factory_common.logsafe` — the CWE-117 log-value sanitizer (`sanitize_log()`).

## Why a second copy

`apps/backend/factory_common/` holds the same files at the same `.hub-sha`. The
web server runs with `apps/web-server` on `sys.path` (see
`apps/web-server/tests/conftest.py` and the image's `WORKDIR`) and does not put
`apps/backend` there at import time, so `server.*` cannot import that copy. A
symlink was rejected: CodeQL's extractor does not reliably follow symlinked
directories, and `logsafe`'s `.replace()` chain has to be *extracted* for the
`py/log-injection` barrier to be recognised. TFactory carries the same pair of
copies for the same reason (TFactory#1052).

Both copies come from the same hub commit and must be re-vendored together.

## Do not edit here

These files are owned by the hub. To change the behaviour, land the change in
`shared/factory-common/` in the Factory hub first (CODEOWNERS-reviewed), then
re-vendor **both** copies here and bump both `.hub-sha` files to the new hub
commit. The `factory_common drift` CI job
(`.github/workflows/cq-factory-common-drift.yml`) fails the build if a copy
diverges from the hub at the pinned SHA, and **also fails when the hub cannot be
reached at all** — a private repo, an expired token, a rename or a
garbage-collected pin — because a skipped diff is not a passed diff (hub
standards rule 4.7).

## Pinned hub commit

See `.hub-sha`.

## Consumers in this repo

- `server/**` wraps untrusted values in `factory_common.logsafe.sanitize_log`
  before they reach a log record (CWE-117).
