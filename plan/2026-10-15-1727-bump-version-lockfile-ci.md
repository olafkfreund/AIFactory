---
status: approved
issue: 1727
spec: spec/2026-10-15-1727-bump-version-lockfile-ci.md
---

# Plan: bump-version.js also writes package-lock.json, and CI runs its tests

Branch `fix/1727-bump-version-lockfile-ci`, base `origin/dev` c89cffd8. Line
numbers checked against that base. The PR closes #1727 and #1631 (#1631 was
fixed by 4470e4c7 / #1728 but left open).

## Approved decisions (from the spec)

- **D1** Pure helper `setLockVersions(text, version)` in `scripts/bump-version.js`:
  `JSON.parse(text)`; return `null` unless `lock.packages` has both `""` and
  `"apps/frontend-web"` entries; set `lock.version`,
  `lock.packages[""].version`, `lock.packages["apps/frontend-web"].version`;
  return `JSON.stringify(lock, null, 2) + "\n"` (byte-exact round-trip for
  npm's format, verified on the base lockfile). Export it.
- **D2** Fail hard (`error()`) before any write if the lockfile is missing or
  the helper returns `null`.
- **D3** Write after the openapi write; add `'package-lock.json'` to the
  `git add --` argv so it lands in the one bump commit.
- **D4** New blocking CI job `scripts-test` in `ci.yml`: checkout, setup-node
  from `.nvmrc`, `npm run test:scripts`. No `npm ci`.
- **D5** Three unit tests with inline fixtures.
- **D6** No `release.yml` check. No `npm install` from the script. No new deps.

## Steps

1. `scripts/bump-version.js`
   - After `setOpenapiVersion` (ends `:217`), add `setLockVersions` per D1 with
     a one-line comment citing #1727.
   - In `main()` after the openapi pre-compute (`:259-264`), add:
     `lockPath = path.join(__dirname, '..', 'package-lock.json')`, read with
     `readIfPresent`, `error()` if `null` ("package-lock.json not found at …"),
     compute `lockNext = setLockVersions(lockRaw, newVersion)`, `error()` if
     `null` ("No root/apps/frontend-web entries in …").
   - After the openapi write (`:276-278`): `info('Updating package-lock.json...')`,
     `fs.writeFileSync(lockPath, lockNext)`, `success('Updated package-lock.json')`.
   - `:310`: append `'package-lock.json'` to the `git add --` argv.
   - `:349`: add `setLockVersions` to `module.exports`.
   → verify: `node -e "require('./scripts/bump-version.js')"` exits 0 and
   `npm run test:scripts` stays 16/16.
   Traps: keep `execFileSync` argv arrays (#1283); nothing from argv into a
   regex; do not change the `require.main === module` guard.

2. `scripts/bump-version.test.mjs`: import `setLockVersions`; add tests:
   (a) a fixture with root `version`, `packages[""]`, `packages["apps/frontend-web"]`
   and `packages["node_modules/x"]` at `1.0.0`: the first three become `9.9.9`,
   `node_modules/x` stays `1.0.0`; (b) returns `null` when
   `packages["apps/frontend-web"]` is missing; (c) output ends with `"}\n"` and
   uses 2-space indent (equals `JSON.stringify(obj, null, 2) + "\n"`).
   → verify: `npm run test:scripts` 19/19; the new tests fail against the
   base `bump-version.js` (helper is undefined).
   Traps: inline fixtures only, never the real lockfile.

3. `.github/workflows/ci.yml`: after the `frontend-test` job (ends `:835`),
   add job `scripts-test`, name `scripts (node --test)`, `runs-on: ubuntu-latest`,
   `timeout-minutes: 10`, steps: checkout
   (`actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`),
   setup-node (`actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0`,
   `node-version-file: .nvmrc`), `run: npm run test:scripts`. One comment
   line: why (#1727, was local-only).
   → verify: `python -c "import yaml;yaml.safe_load(open('.github/workflows/ci.yml'))"`;
   CI shows the job green on the PR.
   Traps: use the same pinned SHAs as the other jobs; no `cache:` (no npm ci).

## Tests

- `npm run test:scripts` → 19 pass, 0 fail.
- Scratch dry run: in a throwaway clone of the branch, `node scripts/bump-version.js 9.9.9`
  (with a CHANGELOG-less warning acceptable); `git show HEAD -- package-lock.json`
  shows exactly the three version lines changed.

## Rollback

Revert the PR's code commits; the lockfile goes back to hand-editing and the CI
job disappears. No data or runtime effect.
