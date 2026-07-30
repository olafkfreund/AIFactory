# Vendored Factory coding-standards baseline

This directory is a **pinned, vendored copy** of the fleet's shared lint baseline.
The single source of truth lives in the hub repo at `Factory/standards/`
(see `Factory/standards/coding-standards.md`).

| File | What it is | Drift-compared |
|---|---|---|
| `coding-standards.md` | The normative fleet standard. Read this before changing code here. | byte-exact |
| `ruff.toml` | Shared Python lint baseline (the full strict select set). | body only |
| `mypy.ini` | Shared `mypy --strict` baseline. | body only |
| `.editorconfig` | Editor defaults (a copy also lives at the repo root). | body only |
| `.hub-sha` | The Factory hub commit these copies were vendored from. | it *is* the pin |

`coding-standards.md` is compared byte-exact and carries no provenance header:
the body-only comparator strips lines starting with `#`, which in Markdown is
every heading, so a stripped compare would let section titles drift unnoticed.

None of these files is editable here. To change a rule, change it in the hub;
to adopt a hub change, re-vendor all four files and bump `.hub-sha` in the same
commit.

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
vendored copies here must stay byte-identical to the hub at the pinned `.hub-sha`.
The `config-drift` job in `.github/workflows/cq-ratchet.yml` diffs them on every
PR and **fails when the hub cannot be reached**, because a skipped diff is not a
passed diff (hub standards rule 4.7). Local tightening belongs in the root
`ruff.toml`, never in these files.

To resync after a hub change:

```sh
HUB=<hub commit sha>
for f in coding-standards.md ruff.toml mypy.ini .editorconfig; do
  curl -fsSL "https://raw.githubusercontent.com/olafkfreund/Factory/$HUB/standards/$f" -o "standards/$f"
done
printf '%s\n' "$HUB" > standards/.hub-sha
```
