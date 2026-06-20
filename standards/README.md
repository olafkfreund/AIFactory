# Vendored Factory coding-standards baseline

This directory is a **pinned, vendored copy** of the fleet's shared lint baseline.
The single source of truth lives in the hub repo at `Factory/standards/`
(see `Factory/standards/coding-standards.md`).

| File | What it is |
|---|---|
| `ruff.toml` | Shared Python lint baseline (the full strict select set). |
| `mypy.ini` | Shared `mypy --strict` baseline. |
| `.editorconfig` | Editor defaults (a copy also lives at the repo root). |
| `.hub-sha` | The Factory hub commit this copy was vendored from. |

## How AIFactory consumes it

- The **ratchet CI job** (`.github/workflows/cq-ratchet.yml`) runs
  `ruff --config standards/ruff.toml` and `mypy --config-file standards/mypy.ini`
  on **only the Python files changed in the PR** (standards section 4.6). New and
  touched code is therefore held to the full strict bar without turning the whole
  legacy tree red.
- The repo-wide `ruff check` in `ci.yml` continues to use the root `ruff.toml`,
  which carries documented legacy carve-outs for hotspots not yet cleaned. Those
  carve-outs are a **ratchet backlog**, not a permanent loosening.

## Tighten-only

Per the standard, a service config may add rules or lower numeric caps; it may not
remove a selected rule category, raise a complexity cap, or disable a gate. The
vendored copies here must stay byte-identical to the hub at the pinned `.hub-sha`
(a future drift gate — Factory#154 — diffs them); local tightening belongs in the
root `ruff.toml`, never in these files.

To resync after a hub change, re-copy the files and update `.hub-sha`.
