---
status: approved
issue: 1631
spec: spec/2026-10-10-1631-bump-version-openapi.md
---

# Plan: bump-version.js must also bump the OpenAPI spec version

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1631`, branch
`fix/1631-bump-version-openapi`, base HEAD `c637b727` (spec approved). Every
line number below was checked against that HEAD. `scripts/bump-version.test.mjs`
has 68 lines. `apps/web-server/openapi.yaml:5` reads `  version: 3.6.84`, and the
file uses LF line endings.

## Approved decisions (carried over from the spec)

- **D1, not fixed on dev.** `bump-version.js:291` stages only the two
  `package.json` files and `apps/backend/__init__.py`. `openapi.yaml:5` is
  hand-set. Implement the fix, and do not close #1631 from the PR.
- **D2, edit the text only.** Do not run `generate-openapi-spec.py` (#1632, #906),
  do not re-serialise with js-yaml, and add no new dependency or file. Code
  changes touch only `scripts/bump-version.js` and `scripts/bump-version.test.mjs`.
  `openapi.yaml` is not edited in this PR.
- **D3, pure helper `setOpenapiVersion(text, version)`.** It uses the constant
  regex `/^(info:\n(?:  .*\n)*?)  version: .*$/m`. The regex is scoped to the
  top-level `info:` block, and the first 2-space `version:` under it wins.
  Nothing from argv goes into the regex (#1283). The helper returns `null` when
  nothing matches. The replacement is the function
  `(_m, head) => \`${head}  version: ${version}\``, so `$&` and `$1` are never
  expanded. It writes the unquoted 2-space form that the generator emits.
- **D4 (Q1), fail hard before any write.** If `readIfPresent` returns `null`
  (spec missing) or the helper returns `null` (no `info.version`), call
  `error()`. Compute this between main() steps 4 and 5, before the first
  `writeFileSync` (`updatePackageJson`, `:150`). Warn-and-skip is rejected.
- **D5, write in step 5.** After `updateBackendInit`, call
  `fs.writeFileSync(openapiPath, openapiNext)`, then
  `success('Updated apps/web-server/openapi.yaml')`.
- **D6, same single commit.** Add `'apps/web-server/openapi.yaml'` to the
  `git add --` argv. Nothing else in the release flow changes: no tag, no push,
  and `execFileSync` is called only with argv arrays.
- **D7 (Q2):** `package-lock.json` is out of scope and goes to the follow-up issue.
- **D8 (Q3):** no end-of-run consistency check. `refresh-and-validate` already
  catches drift.
- **D9 (Q4), remove the stale README claims:** header `:27`, comment
  `:257-259`, and output line `:315`. Line `:314` takes over the trailing `\n`.
- **D10 (Q5):** no `release.yml` check. It is only mentioned in the follow-up.
- **D11:** export `setOpenapiVersion` next to `readIfPresent` and
  `changelogHasVersion`. Keep the `require.main === module` guard.
- **D12, four new unit tests**, all using inline fixtures and never the real
  committed `openapi.yaml`:
  - (a) the real header, where only line 5 changes
  - (b) a nested 8-space `version:` and a description that mentions `version: 1`, both left untouched
  - (c) no `info.version` returns `null`
  - (d) `'$&'` is written literally
- **D13, follow-up issue (not code):**
  - the `package-lock.json` version sites `:3`, `:9` and `:25`
  - an optional spec-vs-`package.json` check in `release.yml:64-71`
  - running `npm run test:scripts` in CI (no CI job runs it today; `package.json:20` is the only reference)
- **D14:** PR #1615 also edits `openapi.yaml:5`. Whichever PR merges second
  rebases that one line.

### Corrections the plan makes to the spec's Verification section

1. **The failure-path check.** The spec says to rename `openapi.yaml` and run
   the script, but that never reaches the new code. A rename leaves the tree
   dirty, so `checkGitStatus()` (`:114-119`) exits first. The rename therefore
   has to be committed in a scratch worktree before the script runs (E3 below).
2. **Hooks.** `core.hooksPath` is `/mnt/data/Source-home/GitHub/AIFactory/.husky`.
   The script's own `git commit` stages `apps/backend/__init__.py`, which runs
   the ruff/mypy ratchet in `.husky/pre-commit`. The hook also `git add`s
   `README.md`. Run the scripted end-to-end checks with hooks off:
   `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null`.
   `run()` passes `process.env` through to the child process.
3. **Line citation.** The spec's helper comment cites `main.py:424`, but that
   line is unrelated on this branch. The version source is `_read_app_version`
   at `apps/web-server/server/main.py:336-356`, used at `:442`. Cite those lines.

### Open point for the approver

D2 limits the code change to two files. The step design adds a `CHANGELOG.md`
entry under `[Unreleased]`, which is docs only, as a third file (Step 4). Drop
Step 4 if D2 is meant to exclude it.

## Steps

Ownership: the `coder` agent does Steps 1 and 2, because together they make
three file-editing steps with Step 4. The session model does Steps 3-5, because
they need `git worktree`, branch switching and the PR.

1. **`scripts/bump-version.test.mjs`:15, :1-6, after :68: write the failing tests first (red)**
   → verify by `node --test scripts/bump-version.test.mjs`. Expect the 6
   existing tests to pass and the 4 new ones to fail with
   `setOpenapiVersion is not a function`.

   - `:15` becomes
     `const { readIfPresent, changelogHasVersion, setOpenapiVersion } = require('./bump-version.js');`
   - `:3-6`: add the header line
     `//   - setOpenapiVersion (#1631): info:-scoped constant regex, replacer fn (no $-expansion)`
   - Append after `:68`:

     ```js
     // apps/web-server/openapi.yaml:1-6 verbatim, em dash included.
     const OPENAPI_HEADER =
       'openapi: 3.1.0\ninfo:\n  title: AIFactory Web API\n' +
       '  description: Web API for AIFactory — self-hosted AI task management + agent orchestration\n' +
       '  version: 3.6.84\npaths:\n';

     // Every line but `onlyIdx` is byte-identical; line `onlyIdx` equals `want`.
     const assertOnlyLineChanged = (before, after, onlyIdx, want) => {
       const a = before.split('\n');
       const b = after.split('\n');
       assert.equal(b.length, a.length);
       b.forEach((line, i) => assert.equal(line, i === onlyIdx ? want : a[i], `line ${i + 1}`));
     };

     test('setOpenapiVersion rewrites only info.version in the real header', () => {
       assertOnlyLineChanged(OPENAPI_HEADER, setOpenapiVersion(OPENAPI_HEADER, '9.99.0'), 4, '  version: 9.99.0');
     });

     test('setOpenapiVersion leaves a description mention and a nested version alone', () => {
       const spec =
         'openapi: 3.1.0\ninfo:\n  title: T\n' +
         '  description: pinned to version: 1 of the wire format\n' +
         '  version: 1.2.3\npaths: {}\ncomponents:\n  schemas:\n    Thing:\n      properties:\n' +
         '        version: 7.7.7\n';
       assertOnlyLineChanged(spec, setOpenapiVersion(spec, '9.99.0'), 4, '  version: 9.99.0');
     });

     test('setOpenapiVersion returns null when info has no version', () => {
       // The 2-space `version:` under components is load-bearing: an info-unscoped
       // regex would match it and return a string instead of null.
       const spec = 'openapi: 3.1.0\ninfo:\n  title: T\npaths: {}\ncomponents:\n  x:\n  version: 1\n';
       assert.equal(setOpenapiVersion(spec, '9.99.0'), null);
     });

     test('setOpenapiVersion writes $-patterns in the version literally', () => {
       assert.equal(setOpenapiVersion(OPENAPI_HEADER, '$&').split('\n')[4], '  version: $&');
     });
     ```

   Traps:
   - Write the em dash as the literal U+2014 character. Do not use `\u2014` or `--`.
   - Keep the file ESM and keep using `createRequire`.
   - Never read the real `openapi.yaml` (D12).
   - Do not stage anything while running `tests/test_security.py`: its
     GitCommitValidator check fails for environmental reasons when files are
     staged.

2. **`scripts/bump-version.js`: implement the change.** Edit from the bottom up
   so the line numbers stay valid.
   → verify by `node --test scripts/bump-version.test.mjs` (tests 10, pass 10)
   and `node --check scripts/bump-version.js`.

   - `:331` becomes
     `module.exports = { readIfPresent, changelogHasVersion, setOpenapiVersion };`
     (D11). Keep the guard at `:326-329`.
   - `:314-315`: delete `:315`. Change `:314` to
     ``log(`      - Create GitHub release with changelog from CHANGELOG.md\n`, colors.yellow);``
     (D9).
   - `:291` becomes
     `run('git', ['add', '--', 'apps/frontend-web/package.json', 'package.json', 'apps/backend/__init__.py', 'apps/web-server/openapi.yaml']);`
     (D6).
   - `:257-260`: delete the 3-line `// Note: README.md is NOT updated here ...`
     comment and the blank line after it (D9).
   - After `:255` (the closing `}` of the `updateBackendInit` if-block), insert (D5):

     ```js

       info('Updating apps/web-server/openapi.yaml...');
       fs.writeFileSync(openapiPath, openapiNext);
       success('Updated apps/web-server/openapi.yaml');
     ```

   - Between `:245` (`success('Release validation passed');`) and `:247`
     (`// 5. Update all version files`), insert (D4):

     ```js

       // 4b. Pre-compute the OpenAPI spec bump; fail before writing anything (#1631).
       const openapiPath = path.join(__dirname, '..', 'apps', 'web-server', 'openapi.yaml');
       const openapiRaw = readIfPresent(openapiPath);
       if (openapiRaw === null) error(`OpenAPI spec not found at ${openapiPath}`);
       const openapiNext = setOpenapiVersion(openapiRaw, newVersion);
       if (openapiNext === null) error(`No info.version line in ${openapiPath}`);
     ```

   - Between `:205` (the end of `changelogHasVersion`) and `:207`
     (`// Main function`), insert (D3):

     ```js
     // Rewrite info.version in the OpenAPI spec text (#1631). Constant regex scoped to
     // the top-level info: block; the first 2-space `version:` under it wins, so the
     // 8-space component `version:` keys are never touched. Nothing from argv enters
     // the pattern (#1283), and the replacer is a function so `$&`/`$1` in the
     // version stay literal. Emits the unquoted form the generator writes
     // (_read_app_version, apps/web-server/server/main.py:336-356). Returns null when
     // there is no info.version line.
     function setOpenapiVersion(text, version) {
       const re = /^(info:\n(?:  .*\n)*?)  version: .*$/m;
       if (!re.test(text)) return null;
       return text.replace(re, (_m, head) => `${head}  version: ${version}`);
     }

     ```

   - `:27`: delete ` *      - Updates README` (D9).
   - Also check that `node -e "require('./scripts/bump-version.js')"` exits 0
     with no output, which shows the guard holds. Then run
     `npm run test:scripts` and expect tests 16, pass 16.
   - Mutation checks: edit the helper, run the unit tests, then restore it by
     re-editing (no stash). Each mutant must fail exactly the listed test:

     | ID | Edit | Fails |
     |---|---|---|
     | M1 | replacer becomes the string `` `$1  version: ${version}` `` | (d) |
     | M2 | regex becomes `/^((?:.*\n)*?)  version: .*$/m` | (c) |
     | M3 | regex becomes `/^(info:\n(?:  .*\n)*?)  .*?version: .*$/m` (matches `version:` mid-line, e.g. inside the description) | (b) |
     | M4 | delete `if (!re.test(text)) return null;` | (c) |

   Traps:
   - `error()` (`:48-51`) calls `process.exit(1)`, so no `return` is needed
     after it.
   - Do not cite `main.py:424`.
   - The regex has no `/g` flag, so `.test()` keeps no `lastIndex` state. Keep it that way.
   - Assume LF line endings and add no CRLF handling (YAGNI).
   - Add no new dependency or file (D2).
   - Spawns must stay as argv arrays, with no shell (CodeQL js/command-injection).

3. **End-to-end wiring checks, done by the session model.** These run in a
   throwaway detached worktree with hooks off. They cover `main()` around
   `:245-260` and `:291`. Nothing is pushed.
   → verify with E1-E4 below.

   ```sh
   W=$(mktemp -d)/e2e
   export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null
   git -C /mnt/code/Source-home/GitHub/AIFactory-1631 worktree add --detach "$W" HEAD
   cd "$W"
   ```

   - **E1, success path:** `node scripts/bump-version.js 9.99.0` exits 0.
     - `git show --stat --format= HEAD` lists exactly 4 files:
       `apps/frontend-web/package.json`, `package.json`, `apps/backend/__init__.py`
       and `apps/web-server/openapi.yaml`.
     - `git show HEAD -- apps/web-server/openapi.yaml | grep '^[-+] '` prints
       exactly `-  version: 3.6.84` and `+  version: 9.99.0`.
     - `git status --porcelain` is empty.
   - **E2, reset:** `git reset -q --hard HEAD~1`.
   - **E3, spec missing:**
     - Run `git mv apps/web-server/openapi.yaml apps/web-server/openapi.yaml.off && git commit -qm scratch && H=$(git rev-parse HEAD)`.
     - The script exits 1 and its output contains `OpenAPI spec not found`.
     - `git status --porcelain` is empty, and `git rev-parse HEAD` still equals `$H`.
   - **E4, no info.version:**
     - Run `git reset -q --hard HEAD~1 && sed -i 's/^  version: /  vers: /' apps/web-server/openapi.yaml && git commit -qam scratch`.
     - The script exits 1, its output contains `No info.version line`, and porcelain is empty.
   - Optional wiring mutants. Each needs its own scratch commit, because
     `main()` refuses a dirty tree:
     - W1: drop the path from the `:291` argv. E1 then shows 3 files and
       ` M apps/web-server/openapi.yaml`.
     - W2: move the 4b block after `updatePackageJson`. E3 then leaves the
       `package.json` files modified.
   - Clean up with
     `git -C /mnt/code/Source-home/GitHub/AIFactory-1631 worktree remove --force "$W"; unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0`.

   Traps:
   - Commit Steps 1-2 (and Step 4 if kept) on the branch first: the worktree is
     created from `HEAD`, so uncommitted edits are not in it and E1 would show
     3 files.
   - Validation: E1 exits 0 only if `validate-release.js:20-24` accepts
     `v9.99.0`, which needs the tag to be absent. Check that first. If
     validation fails, E3 and E4 abort before reaching 4b and prove nothing.
   - Never run the script in the main worktree, and never push the scratch
     commits.

4. **`CHANGELOG.md`:1-3: add the changelog entry (subject to the open point above).**
   Under `## [Unreleased]`, in `### Fixed` (create that subsection if it is
   missing), add:
   `- bump-version.js now updates apps/web-server/openapi.yaml info.version in the same commit, and fails before writing if the spec or its info.version is missing (#1631).`
   → verify by `git diff CHANGELOG.md`, which should show a single added bullet.

   Traps:
   - Expect conflicts with open PRs (#1670, #1674, #1677, #1688-#1690). Keep
     both sides' entries.

5. **Follow-up issue draft and PR, done by the session model.**
   → verify that `gh pr view` shows base `dev` and the links.

   - Draft the follow-up issue body (D13). It should record D7, D8 and D10 as
     out of scope here.
   - Open a PR into `dev` titled
     `fix(scripts): bump-version updates openapi.yaml info.version`. The scope
     must not contain `#`.
   - The PR body:
     - links `intent/`, `spec/` and `plan/` for `2026-10-10-1631-bump-version-openapi`
     - states that the coder did Steps 1-2
     - notes the overlap with PR #1615 at `openapi.yaml:5` (D14)
     - does **not** close #1631 (D1)

   Traps:
   - Get the user's approval before pushing or commenting.

## Tests

| Command | Expected |
|---|---|
| Baseline `node --test scripts/*.test.mjs` | tests 12, pass 12 (6 bump-version, 6 argv-safety) |
| `node --test scripts/bump-version.test.mjs` after Step 1 | 6 pass, 4 fail (red) |
| `node --test scripts/bump-version.test.mjs` after Step 2 | tests 10, pass 10, fail 0 |
| `npm run test:scripts` | tests 16, pass 16, fail 0 |
| `node --check scripts/bump-version.js` | exit 0 |
| `node -e "require('./scripts/bump-version.js')"` | exit 0, no output |
| Mutants M1-M4 (`node --test --test-reporter=tap scripts/bump-version.test.mjs \| grep '^not ok'`) | each fails only its listed test |
| E1-E4 (Step 3) | as listed above |

No repo-wide gate runs automatically:
- ESLint does not cover `scripts/`.
- No CI job runs `test:scripts`.

These repo traps do not apply to this change:
- ruff, cq_ratchet and mypy, CodeQL py/*, py/log-injection: no `.py` file changes.
- `gen_autonomy_matrix`: no cited file moves.
- `child_env`: `run()` is unchanged and no spawn is added.
- `asyncio_mode`: no web-server tests change.

## Rollback

`git revert <merge-sha>`. Nothing runs at import time and there is no migration.
After a revert, bump PRs again need `apps/web-server/openapi.yaml:5` edited by
hand to pass TechDocs `refresh-and-validate`. A bad bump commit is local until
pushed: `git reset --hard HEAD~1` on the bump branch.

## Deviations

- Step 2: mutant M1 (`$1` string replacer) fails tests (a), (b) and (d), not
  only (d): `$1` expands to the info head, so the output is wrong for any
  version.
- Step 5: follow-up #1727 filed (package-lock.json, release.yml check, CI for test:scripts). E1-E4 run by the session model: all pass.
