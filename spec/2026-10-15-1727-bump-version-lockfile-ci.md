---
status: approved
issue: 1727
intent: intent/2026-10-15-1727-bump-version-lockfile-ci.md
---

# Spec: bump-version.js also writes package-lock.json, and CI runs its tests

## Design

- **D1, pure helper `setLockVersions(text, version)`** in `scripts/bump-version.js`,
  next to `setOpenapiVersion` (`:213-217`). It parses the lockfile JSON, sets
  `lock.version`, `lock.packages[""].version` and
  `lock.packages["apps/frontend-web"].version`, and returns
  `JSON.stringify(lock, null, 2) + "\n"`. It returns `null` when either
  `packages` entry is missing. On the current `package-lock.json` this
  serialisation round-trips byte-exactly (checked with node), so only the
  three version strings change. Exported beside the other helpers (`:349`).
- **D2, fail before any write.** In `main()` step 4b (`:259-264`), read
  `package-lock.json` with `readIfPresent`; call `error()` if it is missing or
  the helper returns `null`. Same rule as the openapi step (#1631 D4).
- **D3, write in step 5** after the openapi write (`:276-278`), and add
  `'package-lock.json'` to the `git add --` argv (`:310`) so it lands in the
  same bump commit.
- **D4, CI job `scripts-test`** in `.github/workflows/ci.yml`, after
  `frontend-test` (`:813-835`): checkout, `actions/setup-node` with
  `node-version-file: .nvmrc` (same pinned SHAs as the other jobs), then
  `npm run test:scripts`. No `npm ci`: the tests use only node built-ins.
- **D5, three unit tests** in `scripts/bump-version.test.mjs` with inline
  fixtures: sets the three sites and leaves other packages alone; returns null
  without the workspace entry; output keeps 2-space indent and trailing newline.
- **D6:** no `release.yml` check (optional in #1727, rejected as D10 in #1631).

## Alternatives rejected

- `npm install --package-lock-only` from the script: needs the network and can
  rewrite resolutions beyond the version fields.
- Regex edit of the lockfile text: the version lines are not uniquely
  addressable by regex without fragile line/indent assumptions; JSON round-trip
  is exact for npm's format.
- Adding the step to an existing frontend job: couples script tests to
  `npm ci` and the frontend jobs' failure modes; a separate job is clearer.

## Risks

- A future npm lockfile format that does not round-trip through
  `JSON.stringify(..., 2)` would show extra diff; the bump commit's diff would
  make it visible. Low.
- The new CI job is blocking; the 16 existing script tests pass on `dev` today.

## Verification

- `npm run test:scripts`: new tests fail before the helper exists, pass after.
- Dry run on a scratch copy: `node scripts/bump-version.js 9.9.9` in a throwaway
  clone, then `git show --stat HEAD` lists `package-lock.json` and
  `git show HEAD -- package-lock.json` shows exactly three changed lines.
- CI: the new `scripts (node --test)` job is green on the PR.
