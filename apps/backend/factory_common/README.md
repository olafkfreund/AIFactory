# factory_common (vendored from the Factory hub)

This package is a **byte-identical vendored copy** of the canonical
`shared/factory-common/factory_common/` layer in the
[Factory hub](https://github.com/olafkfreund/Factory) — the single source of
truth for the fleet's deduped, stdlib-only utility primitives (epic Factory#154,
issue Factory#161):

- `factory_common.secrets` — the canonical secret-pattern table + `redact()` /
  `scan()` / `contains_secret()`.
- `factory_common.http` — the Cloudflare-friendly typed `urllib` JSON client.
- `factory_common.logsafe` — `sanitize_log()`, the CWE-117 / `py/log-injection`
  fix: escapes CR/LF and control characters before a value reaches a log message.
- `factory_common.url_safety` — `assert_safe_outbound_url()`, the SSRF guard for
  URLs a service fetches on a caller's behalf, in a strict and a permissive
  posture, plus a redirect-following fetch that re-validates every hop.

## There are TWO vendored copies

The other is `apps/web-server/factory_common/`, pinned to the same `.hub-sha`.
It is not a duplicate by accident: `apps/web-server` is the `sys.path` root for
`server.*` and `apps/backend` is not on the path at import time, so the server
tree cannot import this copy. **Re-vendor both together.**

A symlink was rejected deliberately. CodeQL's extractor does not reliably follow
symlinked directories, and `logsafe`'s `.replace()` chain has to be *extracted*
for the `py/log-injection` barrier to be recognised at all — the barrier is the
entire mechanism, so an unextracted copy is a silently disarmed one.

The same applies by name to `url_safety`: the SSRF barrier in
`.github/codeql/custom-queries/SsrfBarriers.qll` registers
`assert_safe_outbound_url` **by name**. Moving the module is safe; renaming the
function un-registers the barrier silently and reopens every alert it clears.

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
fails the build if this copy diverges from the hub at the pinned SHA, and
**also fails when the hub cannot be reached at all** — a private repo, an
expired token, a rename or a garbage-collected pin — because a skipped diff is
not a passed diff (hub standards rule 4.7).

## Pinned hub commit

See `.hub-sha`.

## Consumers in this repo

- `apps/backend/agents/base.py::sanitize_error_message` redacts log/error
  strings through `factory_common.secrets.redact` (the superset pattern table)
  before applying the AIFactory-specific `sk-` / `key-` / `token=` / `secret=`
  redactions and truncation.
