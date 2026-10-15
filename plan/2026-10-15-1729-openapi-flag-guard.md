---
status: draft
issue: 1729
spec: spec/2026-10-15-1729-openapi-flag-guard.md
---

# Plan: CI fails when feature flags change the generated OpenAPI spec

Branch `ci/1729-openapi-flag-guard`, base `origin/dev` 7bcc9fe5. Line numbers
checked against that base. Independent of #1731 (PR #1785): that PR edits
`techdocs.yml:197` and the generator docstring (`:11-12`); this one appends
after `:200` and edits generator `:36-37`, so the hunks do not overlap.

Env: `V=<main clone>/apps/web-server/.venv/bin` (has both requirement sets),
`B=<main clone>/apps/backend/.venv/bin` (ruff).

## Approved decisions (from the spec)

- **D1** New last step of job `refresh-and-validate` in
  `.github/workflows/techdocs.yml`: rerun the generator with the five pinned
  flags exported as `true`, then `git diff --exit-code` on `openapi.yaml`;
  on a diff, `::error::` blaming the pin in `scripts/generate-openapi-spec.py`.
- **D2** Comments: replace the generator's `ponytail` note (`:36-37`) with a
  pointer to the CI step ("add a new gate to both lists"); one comment line
  in the techdocs block `:160-162`.
- **D3** No new deps, no `.env` variant, no allowlist of gated paths, no pytest.

## Steps

1. `.github/workflows/techdocs.yml`
   - After `:200` (end of file, inside the same job's `steps:` at 6-space
     indent), append:
     ```yaml

           # 5. The generator pins SAML/SCIM/mcp-remote/rmux off (#1632). Prove the
           #    pin holds: with every flag exported on, the output must still equal
           #    the committed spec that step 4 just matched to the flags-off run
           #    (#1729). A new env gate goes in both lists.
           - name: Check the OpenAPI spec ignores feature flags
             env:
               APP_DISABLE_AUTH: "true"
               SAML_ENABLED: "true"
               SCIM_ENABLED: "true"
               AIFACTORY_MCP_REMOTE_ENABLED: "true"
               AIFACTORY_RMUX_ENABLED: "true"
               APP_RMUX_ENABLED: "true"
             run: |
               /tmp/ws/bin/python scripts/generate-openapi-spec.py
               if ! git diff --exit-code -- apps/web-server/openapi.yaml; then
                 echo "::error::Feature flags changed the generated OpenAPI spec. Pin the new gate off in scripts/generate-openapi-spec.py and add it here."
                 exit 1
               fi
     ```
   - `:160-162` comment: append "Step 5 reruns it with them all on and fails
     if the spec changes (#1729)."
   → verify: `$V/python -c "import yaml;yaml.safe_load(open('.github/workflows/techdocs.yml'))"`
   and the step's `run:` locally (with `$V/python` in place of `/tmp/ws/bin/python`
   and the env exported) exits 0 with `git status` clean.
   Traps: 2-space YAML nesting matching the other steps; quote `"true"`;
   no `continue-on-error`; do not touch the drift check text (#1731 edits it).
2. `scripts/generate-openapi-spec.py:36-37`: replace the two `ponytail` comment
   lines with `# A new env gate must be added here and to techdocs.yml step 5,`
   / `# which reruns this with every flag on and fails on any diff (#1729).`
   → verify: `$B/ruff check` and `$B/ruff format --check` on the file.

Step 1 goes to codex (multi-line YAML); step 2 is a two-line comment, done directly.

## Tests

- Pass case: step 1's local run exits 0, `openapi.yaml` unchanged.
- Fail case (proves the guard): in a scratch copy of the generator with
  `"AIFACTORY_RMUX_ENABLED",` removed from the pin, the same run exits 1 and
  the diff adds rmux paths. Restore with `git checkout -- scripts/generate-openapi-spec.py apps/web-server/openapi.yaml`.
- TechDocs workflow green on the PR, step "Check the OpenAPI spec ignores feature flags" listed.

## Rollback

Revert the PR; the workflow loses the step. No runtime effect.
