---
status: draft
issue: 1631
intent: intent/2026-10-10-1631-bump-version-openapi.md
---

# Spec: bump-version.js must also bump the OpenAPI spec version

## Design

The fix edits one line of text. `scripts/bump-version.js` rewrites `info.version` in
`apps/web-server/openapi.yaml` (line 5, `  version: 3.6.84`) and stages the file in its
single bump commit. The change touches two files, `scripts/bump-version.js` and
`scripts/bump-version.test.mjs`. It adds no new file and no new dependency.

1. **Pure helper**, placed next to `changelogHasVersion` (around `:180-210`):

   ```js
   // Rewrite info.version only, in the form the generator emits (main.py:424).
   // Constant regex: nothing from argv goes into it (#1283). Scoped to info: so
   // a nested `version:` elsewhere in the spec is never touched.
   function setOpenapiVersion(text, version) {
     const re = /^(info:\n(?:  .*\n)*?)  version: .*$/m;
     if (!re.test(text)) return null;
     return text.replace(re, (_m, head) => `${head}  version: ${version}`);
   }
   ```

   - The replacement is a function, so a `$&` or `$1` in `version` is never
     expanded. This holds even if `parseVersion` is ever loosened.
   - `.` matches neither `\r` nor `\n`, and `$` under `/m` stops before a line
     ending. So the match cannot run past the version value. The lazy `*?`
     makes the first `  version:` under the top-level `info:` win. The match
     is linear in the size of the `info:` block, so it has no ReDoS shape.

2. **Compute before any write.** In `main()`, between step 4 (`:245`) and
   step 5 (`:247`), add:

   ```js
   const openapiPath = path.join(__dirname, '..', 'apps', 'web-server', 'openapi.yaml');
   const openapiRaw = readIfPresent(openapiPath);
   if (openapiRaw === null) error(`OpenAPI spec not found at ${openapiPath}`);
   const openapiNext = setOpenapiVersion(openapiRaw, newVersion);
   if (openapiNext === null) error(`No info.version line in ${openapiPath}`);
   ```

   `error()` (`:48`) exits. Nothing has been written yet (the first write is at
   `:150`), so the tree stays clean. In step 5, after `updateBackendInit`, add
   `fs.writeFileSync(openapiPath, openapiNext)` and
   `success('Updated apps/web-server/openapi.yaml')`.

3. **`:291`**: add `'apps/web-server/openapi.yaml'` to the `git add --` argv.

4. **Remove the stale README claims (Q4)**: delete the header line at `:27`,
   the comment at `:257-259` and the output line at `:315`. Line `:314` then
   ends the list, so it gets the trailing `\n` that `:315` had. That is a
   one-character edit, not a pure deletion.

5. **`:331`**: export the helper with
   `module.exports = { readIfPresent, changelogHasVersion, setOpenapiVersion };`.

6. **Tests** in `scripts/bump-version.test.mjs`, following the pattern at
   `:19-65`:
   - (a) The real header from `openapi.yaml:1-6`, including the em dash at
     `:4`. Only line 5 differs, checked by splitting both texts into lines and
     comparing them.
   - (b) A synthetic fixture with an 8-space `version:` under `components:`
     and a description that contains `version: 1`. Both are left unchanged.
   - (c) Text with no `info.version` returns `null`.
   - (d) `setOpenapiVersion(header, '$&')` writes a literal `  version: $&`.
     This pins the replacer-function choice.

7. **Follow-up issue** (not code). It covers three things:
   - the `package-lock.json` version sites at `:3`, `:9` and `:25`;
   - an optional `release.yml:64-71` check that the spec matches
     `package.json`;
   - running `npm run test:scripts` in CI, which no workflow does today.

Expected diff: about 15 lines added and 5 removed in the script, and about
35 lines added in the test.

### Proposed answers to the intent's open questions

These are proposed defaults for the approver to confirm or change.

| # | Question | Proposed answer | Why |
| - | -------- | --------------- | --- |
| — | Is it fixed on `dev` already? | No. Implement it, and do not close the issue. | `bump-version.js:291` stages only the two `package.json` files and `__init__.py`. `openapi.yaml:5` is still hand-set to 3.6.84. |
| 1 | Spec missing, or no `info.version`: fail or warn? | Fail hard with `error()`, before the first write (Design step 2). | A warning still sends the PR red at `techdocs.yml:178-182`. Failing after the `package.json` write would leave a half-bumped tree, which is what the warn path at `:167-171` does. |
| 2 | `package-lock.json` in scope? | No. Put it in the follow-up issue. | No gate reads it: `techdocs.yml:178` diffs only `docs/dependencies.md` and `openapi.yaml`, and `release.yml:67` reads only `package.json`. Adding it means three more version sites. |
| 3 | End-of-run check that every file agrees? | No. | `refresh-and-validate` (job `techdocs.yml:133`, steps `:160-182`) regenerates the spec from `__init__.py` and fails on any drift. |
| 4 | Remove the stale README claims? | Yes, at `:27`, `:257-259` and `:315` (Design step 4). | They are false, because no workflow writes README, and they sit in the file this change already edits. |
| 5 | Add a `release.yml` check that `openapi.yaml` matches `package.json`? | No. Mention it in the follow-up only. | A hand-made bump already fails on the PR at `techdocs.yml:182`, before it reaches `main`. |
| — | Overlap with in-flight work | Only PR #1615, which edits the same line 5. Whichever merges second rebases one line. | No other open worktree touches `bump-version`, `openapi.yaml` or `techdocs.yml`. |

## Alternatives rejected

- **Run `scripts/generate-openapi-spec.py` from the bumper.** It needs a
  Python venv with the backend and web-server requirements. Its output also
  depends on local feature flags (#1632, #906), so it can rewrite more than
  one line. The intent's constraints rule it out.
- **Parse and dump with `js-yaml`.** It adds a new dependency. Re-serialising
  14k+ lines would also change formatting and escaping, and that would itself
  fail `techdocs.yml:178`.
- **Hard-code "line 5".** If the generator's key order changes, the script
  would silently edit the wrong line.
- **Unscoped `/^  version: .*$/m`.** It works today, because every other
  `version:` key sits at 8 spaces (`:10547`, `:10807`, `:10857`, `:12616`,
  `:14564`, `:14582`).
  But it breaks silently if a top-level block ever gains a 2-space
  `version:`. Scoping to `info:` costs one group.
- **Build the regex from `newVersion`.** That brings back the
  js/regex-injection that #1283 removed.
- **A replacement string `'$1  version: ' + version`.** It is safe only
  because of the `parseVersion` check, while the replacer function costs
  nothing.
- **Warn and skip, like `updateBackendInit` (`:167-171`).** The PR still
  goes red on TechDocs, and the run leaves a half-bumped tree.
- **Write the spec in step 5 without computing it first.** A missing spec
  would then fail after `package.json` was already written.
- **A test against the real committed `openapi.yaml`.** The TechDocs gate
  already catches a change in how the generator emits the line. That test
  would only report the same drift earlier, in a test that CI does not run.

## Risks

- **CRLF checkout.** `info:\n` does not match, so the helper returns `null`
  and `error()` stops the bump before any write. The failure is loud, with no
  corruption. The repo stores the spec with LF line endings only.
- **The generator starts quoting the version.** The script would then write
  the unquoted form, and `refresh-and-validate` would catch the drift on the
  PR, so the failure is loud. Today `main.py:424` and `yaml.safe_dump` emit
  it unquoted.
- **Existing failure paths are unchanged.** A crash between writes, or a
  failed `git commit` at `:292`, still leaves a dirty tree. Q3 keeps this out
  of scope.
- **The tests do not run in CI.** No workflow runs `npm run test:scripts`
  (`package.json:20`), so they run locally only. This goes in the follow-up
  issue.
- **Default installs.** The change is confined to a release-time developer
  script and its test. No installed package, runtime module, Nix output or
  workflow changes, so install and runtime behaviour are identical.
- **PR #1615** edits the same version line, which means a one-line rebase.
- **Correction to the earlier notes.** `openapi.yaml:1988` is description
  text ("update project version files"), not a `version:` key. That is why
  test (b) uses a synthetic fixture.

## Verification

- Run `node --test scripts/bump-version.test.mjs` (or `npm run test:scripts`).
  The 6 existing tests and the 4 new ones should pass.
- Run `node scripts/bump-version.js 9.99.0` on a scratch branch with a clean
  tree. Use an untagged version: `v3.6.85` is already tagged, so
  `validate-release.js` (`:21-24`) would stop a `3.6.85` run before any write.
  Then check:
  - `git show --stat HEAD` lists 4 files: both `package.json` files,
    `apps/backend/__init__.py` and `apps/web-server/openapi.yaml`;
  - `git show HEAD -- apps/web-server/openapi.yaml` shows exactly
    `-  version: 3.6.84` / `+  version: 9.99.0`.
- Test the failure path: temporarily rename the spec and run the script.
  It should exit non-zero, and `git status --porcelain` should be empty.
- In CI, the next bump PR made with the script passes `refresh-and-validate`
  (job `techdocs.yml:133`, steps `:160-182`) with no hand edits. This PR does
  not trigger that workflow: it touches neither `openapi.yaml` nor any path in
  the `techdocs.yml:25-36` filter. A bump PR does, since it edits both
  `apps/frontend-web/package.json` and `openapi.yaml`.

### CI gates

- **ruff, and strict ruff + `mypy --strict` ratchet:** these do not run,
  because no `.py` file changes.
- **CodeQL (JS):**
  - The regex is constant, so there is no js/regex-injection.
  - The read goes through `readIfPresent`, so there is no
    js/file-system-race.
  - git calls stay in `execFileSync` argv arrays.
  - The logs print only a path and the validated version, so there is no
    clear-text-logging alert.
- **Autonomy matrix `--check`** (`autonomy-matrix.yml`): no workflow or
  protection file changes, so the matrix output is unchanged.
- **TechDocs `refresh-and-validate`:** this change exists to make bump PRs
  pass this gate.
