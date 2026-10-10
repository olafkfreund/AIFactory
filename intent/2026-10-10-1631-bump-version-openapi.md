---
status: draft
issue: 1631
author: olafkfreund
---

# Intent: bump-version.js must also bump the OpenAPI spec version

## Problem

The issue is still open. `scripts/bump-version.js` is byte-identical on `dev`
(HEAD 1c2df772) and `main` (3.9.0), and no commit or PR has fixed it.

`scripts/bump-version.js` changes the version in three files: the root
`package.json`, `apps/frontend-web/package.json` and `apps/backend/__init__.py`.
It never touches `info.version` in `apps/web-server/openapi.yaml`.

The generator builds that value from `__version__` in `apps/backend/__init__.py`
(`apps/web-server/server/main.py:320`, `:424`). The TechDocs `refresh-and-validate` job
regenerates the spec and fails on any diff. So every bump PR fails with
"Generated TechDocs artifacts are stale" until someone edits the spec by
hand. Every release since at least 3.6.78 carries that hand edit, on `dev`
up to 3.6.84 (`c4fcaec3`) and on `main` up to 3.9.0 (`bff1da52`, "openapi
and lockfile versions"). The script's output says nothing about the spec.

The same releases also hand-patch `package-lock.json`, which carries the
version at its root and in the `apps/frontend-web` workspace entry. The
script does not write it either. On `main` the workspace entry has already
drifted (3.8.0 against 3.9.0). No gate is known to fail on it.

The fix the workflow suggests, running `generate-openapi-spec.py` locally,
can rewrite more than the version line. The generator's output depends on
local feature flags (#1632, still open).

Correction: the issue says the script updated README.md. It does not. And
contrary to the script's header comment and its "Next steps" output, no
workflow writes README either: `release.yml` never touches it and README
carries no version string.

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
- Keep the release flow the same: one local commit, no tag, no push.
- `bump-version.js` stays importable (`require.main === module` guard) and
  `scripts/bump-version.test.mjs` stays the place its tests live.
- Overlap: open PR #1615 (catch `dev` up with `main`, release 3.6.85) edits
  `openapi.yaml`'s version line, so whichever merges second rebases. #1632
  (open issue) concerns the generator, not the bumper.

## Open questions

1. If the spec is missing or has no `info.version` line, should the bump fail
   hard, or only warn the way the `__init__.py` case does? A warning means the
   PR goes red on the same gate.
2. Should `package-lock.json` (root and workspace entry) be in scope too, since
   releases hand-patch it as well, or stay a separate issue because no gate
   fails on it?
3. Should the script also check, at the end, that every version-carrying file
   agrees? Or keep this to the missing writer(s)?
4. Should this fix also remove the stale claims that a workflow updates
   README (header comment and "Next steps" output), or leave them alone?
5. Should `release.yml` also check that `openapi.yaml` matches `package.json`,
   so a hand-made bump fails early there instead of in TechDocs?
