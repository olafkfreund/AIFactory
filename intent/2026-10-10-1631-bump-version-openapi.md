---
status: draft
issue: 1631
author: olafkfreund
---

# Intent: bump-version.js must also bump the OpenAPI spec version

## Problem

The issue is still open on `dev` (HEAD 1c2df772). No commit or PR has fixed it.

`scripts/bump-version.js` changes the version in three files: the root
`package.json`, `apps/frontend-web/package.json` and `apps/backend/__init__.py`.
It never touches `info.version` in `apps/web-server/openapi.yaml`.

CI builds that value from `__version__` in `apps/backend/__init__.py`
(`server/main.py:320`, `:424`). The TechDocs `refresh-and-validate` job
regenerates the spec and fails on any diff. So every bump PR fails with
"Generated TechDocs artifacts are stale" until someone edits the spec by
hand. That is what happened in 3.6.82, 3.6.83 and 3.6.84 (`c4fcaec3`).
The script's output says nothing about the spec.

The fix the workflow suggests, running `generate-openapi-spec.py` locally,
can rewrite more than the version line. The generator's output depends on
local feature flags (#1632, still open).

Correction: the issue says the script updated README.md. It does not. The
release workflow writes README after publish.

## Proposed outcome

- After `bump-version.js X.Y.Z`, the spec's `info.version` is `X.Y.Z` and the
  file is part of the script's single bump commit.
- A bump PR made with the script passes `refresh-and-validate` with no
  hand edits.
- No other byte of `openapi.yaml` changes.

## Affected users and systems

- Release authors who run `scripts/bump-version.js`.
- `scripts/bump-version.js` and its test `scripts/bump-version.test.mjs`.
- `apps/web-server/openapi.yaml` (one line per bump).
- `.github/workflows/techdocs.yml` `refresh-and-validate`: the gate that now
  goes green. The workflow itself is unchanged.

## Constraints

- Keep the #1273 / #1283 hardening: reads go through `readIfPresent`, no
  exists-then-read, no RegExp built from argv, `execFileSync` with argv
  arrays only. `newVersion` stays validated as `^\d+\.\d+\.\d+$` before any
  write.
- Do not run the generator from the bumper (#1632, #906). Edit the text only.
- Change only `info.version`, in the exact form the generator emits
  (`  version: X.Y.Z`, unquoted, under `info:`). No YAML re-serialisation and
  no new dependency.
- Keep the release flow the same: one local commit, no tag, README still
  written by the release workflow. Add the spec to the step-7 `git add` list.
- `bump-version.js` stays importable (`require.main === module` guard). New
  helpers are exported and tested in the existing test file on a tmp file.
- No overlap with other open work. Only #1632 shares the file, and it does not
  share this code path.

## Open questions

1. If the spec is missing or has no `info.version` line, should the bump fail
   hard, or only warn the way the `__init__.py` case does? A warning means the
   PR goes red on the same gate.
2. Should the script also check, at the end, that all four version-carrying
   files agree? Or keep this to the one missing writer?
3. Should this fix correct the issue text and the script's "Next steps" output
   about README, or leave them alone?
4. Should `release.yml` also check that `openapi.yaml` matches `package.json`,
   so a hand-made bump fails early there instead of in TechDocs?
