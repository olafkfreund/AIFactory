---
status: draft
issue: 1727
author: olafkfreund
---

# Intent: bump-version.js also writes package-lock.json, and CI runs its tests

Covers #1727 and closes #1631. #1631 (openapi.yaml `info.version`) is already
fixed on `dev` by 4470e4c7 (#1728), which left the issue open; this task only
closes it.

## Problem

- `scripts/bump-version.js` writes `package.json`, `apps/frontend-web/package.json`,
  `apps/backend/__init__.py` and `apps/web-server/openapi.yaml`, but not
  `package-lock.json`. The lockfile carries the app version three times
  (`package-lock.json:3`, `:9`, `:25`: root, `packages[""]`, and the
  `apps/frontend-web` workspace entry), and these are fixed by hand on every
  release. The workspace entry has drifted before (3.8.0 against 3.9.0).
- No CI job runs `npm run test:scripts` (`package.json:20`, the only reference),
  so `scripts/bump-version.test.mjs` and `scripts/argv-safety.test.mjs` run only
  locally and can rot unnoticed.

## Proposed outcome

- After `node scripts/bump-version.js <x>`, the three lockfile version sites
  equal the new version, and the lockfile is in the same bump commit.
- Every CI run on a PR or push to dev/main runs `npm run test:scripts`.

## Affected users and systems

Release cutters (local script), `.github/workflows/ci.yml`. No runtime code.

## Constraints

- No new dependency. Do not run `npm install` from the script (it would rewrite
  resolutions, not just versions).
- Fail before any write if the lockfile cannot be bumped, like the openapi step.
- Keep the lockfile byte-identical apart from the three version strings.

## Open questions

- The optional `release.yml` spec-vs-package.json check from #1727: proposed
  out of scope (the #1631 spec rejected it as D10; `refresh-and-validate`
  already catches drift).
