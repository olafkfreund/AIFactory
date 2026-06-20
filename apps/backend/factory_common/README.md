# factory_common (vendored from the Factory hub)

This package is a **byte-identical vendored copy** of the canonical
`shared/factory-common/factory_common/` layer in the
[Factory hub](https://github.com/olafkfreund/Factory) — the single source of
truth for the fleet's deduped, stdlib-only utility primitives (epic Factory#154,
issue Factory#161):

- `factory_common.secrets` — the canonical secret-pattern table + `redact()` /
  `scan()` / `contains_secret()`.
- `factory_common.http` — the Cloudflare-friendly typed `urllib` JSON client.

## Why vendored (not pip-installed)

The fleet vendors shared layers byte-for-byte behind a drift gate rather than
publishing a package, exactly as it already does for `standards/` (see
`standards/.hub-sha` and the `config-drift` job in `.github/workflows/cq-ratchet.yml`).
This keeps the coder pod / CI dependency-free (the layer is stdlib-only and
importable anywhere) while a CI gate guarantees the copy cannot silently drift
from the hub.

## Do not edit here

These files are owned by the hub. To change the behaviour, land the change in
`shared/factory-common/` in the Factory hub first (CODEOWNERS-reviewed), then
re-vendor here and bump `.hub-sha` to the new hub commit. The
`factory_common drift` CI job (`.github/workflows/cq-factory-common-drift.yml`)
fails the build if this copy diverges from the hub at the pinned SHA.

## Pinned hub commit

See `.hub-sha`.

## Consumers in this repo

- `apps/backend/agents/base.py::sanitize_error_message` redacts log/error
  strings through `factory_common.secrets.redact` (the superset pattern table)
  before applying the AIFactory-specific `sk-` / `key-` / `token=` / `secret=`
  redactions and truncation.
