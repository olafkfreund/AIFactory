---
status: draft
issue: 1627
spec: spec/2026-10-10-1627-coder-repo-lint-gate.md
---

# Plan: record CI linters that no gate ran as `skipped`

Branch `fix/1627-coder-repo-lint-gate`, base `origin/dev`. Line numbers checked
at worktree HEAD 3f82c69c. That HEAD has no diff against the spec baseline
c59a0ea6 in `ci_discovery.py`, `coder.py`, `gate_runner.py`, `qa.py` or either
test file, so every line reference in the spec still holds.

## Approved decisions (self-contained)

- **Scope (Q1):** this ships only the reporting fix. Outcome 1 of the intent,
  blocking "complete" when repo lint would fail, is deferred. One follow-up
  issue covers reading CI lint during planning, running lint as a trailing
  gate, and the Q3, Q4 and Q7 follow-up items.
- **Fail or warn (Q2):** warn only. A linter that did not run becomes a
  `skipped` `GateResult`. It is never written to a GATE_FAILURES.md failure
  section and never raises. The trailing-gate path keeps its never-raise
  contract (coder.py:1407). The follow-up decides whether a lint that ran and
  failed should block.
- **Source (Q3):** detection only. A fixed allowlist is matched against the
  CI that `discover_ci` finds (`CIWorkflow.name` and `CIWorkflow.steps`). Only
  the allowlist constants reach the evidence. No CI text is copied and nothing
  is executed. Any future execution goes through a curated descriptor `lint`
  lane, never through commands copied from CI.
- **Version and network (Q4):** deferred. The sandbox stays `network=none`.
  The follow-up's default is a nix-provided linter with `network=none` that
  records its version in the gate output, with no egress.
- **Sequencing (Q5):** proceed now and rebase later. This does not overlap
  #1673 (`build_commands.py`, `deploy_scaffold`) or #1620
  (`gate_runner.py:355-396`).
- **Swift test lanes (Q6, TFactory#1340):** out of scope.
- **Completion banners (Q7):** coder.py:957 and :1077,
  `core/progress.py:179` and `tools_pkg/tools/progress.py:141` stay
  unchanged. Grepping their consumers belongs to the follow-up.
- **Outcome word:** the existing `skipped`, not "not run". `gate_outcomes`
  splits on `,` and `:`, and any value outside `_KNOWN_GATE_OUTCOMES`
  (gate_runner.py:900) fails closed, which would make QA refuse every Kotlin
  build.
- **Allowlist:** `CI_LINTERS: tuple[str, ...] = ("detekt", "eslint",
  "flake8", "golangci-lint", "ktlint", "ruff", "spotless", "swiftlint")`, kept
  sorted. No name may contain `,` or `:`.
- **Helper:** `ci_linters(project_dir: Path) -> list[str]` returns the
  allowlisted names found in any discovered workflow's name or steps.
  - The output is in `CI_LINTERS` order with no duplicates, so the marker is
    deterministic.
  - It never raises and returns `[]` when unsure. The whole body sits in
    `try/except Exception` returning `[]`, marked `# noqa: BLE001`.
- **Pre-guard:** before calling `discover_ci`, check every file in
  `.github/workflows/` matching `*.yml` or `*.yaml`. Return `[]` if any file:
  - is a symlink;
  - is not a regular file;
  - is larger than 256 KiB; or
  - contains a YAML anchor (`&name`).

  The guard covers GitHub only. CircleCI and Jenkins workflows still come
  through `discover_ci` without it.
- **Matching:** case-insensitive and left-bounded (no letter or digit
  immediately before the name), against the lowercased `CIWorkflow.name` and
  steps.
  - `ktlint`, `spotlessCheck`, `ruff check` and `uses: ScaCap/action-ktlint`
    all match. `gruff` does not.
  - A job keyed `ktlint` whose only step is `./gradlew check` is still found
    through its name.
- **No new YAML parsing:** reuse `discover_ci` and `CIWorkflow.steps` (name,
  run, `uses: ...`). Scanning the raw text was rejected.
- **coder.py hook:** after `run_gates` and before `summarize_gates`:
  - Collect `ran`, the names of the results that were not skipped.
  - For each tool from `ci_linters(gate_dir)` whose covering gate name is not
    in `ran`, append `GateResult(name=tool, passed=True, skipped=True)`.
  - The covering name is the tool itself, except for eslint, which is covered
    by the npm `lint` gate (gate_runner.py:135-136).
  - Use `passed=True`, as spec section 2 says, to match `run_gates`' record
    for a missing tool (gate_runner.py:673). The spec's Design intro says
    `passed=False`, but section 2 is the operative one. `GateResult.status`
    returns `skipped` and `failing_gates` leaves skipped results out either
    way.
- **Placement:** detection lives in coder.py, not in `detect_gates` or
  gate_runner.py. That avoids colliding with #1620 and keeps "a gate to run"
  separate from "a gate that did not run".
- **One summary string:** the same string feeds the GATE_FAILURES.md
  `Summary:` line and the marker. Keeping two strings was rejected.
  `ktlint: skipped` on the `Summary:` line is informational only.
- **`if not gates:` branch (coder.py:1340-1345):** not annotated. Its marker
  text, "no gates detected ...", must stay unchanged because QA and the CLI
  match on `startswith`. This is an accepted ceiling; the follow-up covers it.
- **Files left alone:** `gate_runner.py`, `cli/build_commands.py`,
  `deploy_scaffold`, `core/languages/*.yaml`, `core/language_descriptors.py`,
  the completion banners, and the `apps/backend/ci_discovery.py` shim. The
  change touches two production files, about 25 lines.
- **Quality bar:**
  - New code is fully typed. Both files are in the per-file ruff and
    `mypy --strict` ratchet and must add no violations.
  - No logging, so CodeQL `py/log-injection` cannot trigger.
  - No subprocess, network, PVC or volume.

### Deviation from spec Verification 2.4 (recorded here for approval)

The spec says to monkeypatch `analysis.ci_discovery.ci_linters` to raise and
then expect the marker to be written. As designed, that cannot pass.

- The new call at coder.py:1351-1352 has no guard of its own. Only the
  whole-body `try` (coder.py:1239, `except` at :1407) covers it.
- If `ci_linters` itself raises, control skips `write_trailing_gate_marker`
  (:1406), so no marker is written.
- The spec's "Why the try" paragraph puts the never-raise guarantee inside
  `ci_linters`.

This plan therefore monkeypatches `analysis.ci_discovery.discover_ci`, which
`ci_linters` looks up as a module global. That exercises `ci_linters`' own
`try`, and the marker is still written. No code change follows from this.

The alternative is to wrap the coder call in its own `try`. That changes the
Sites table and would need the spec approved again, so it is not taken.

## Steps

The plan has 3 steps that edit files and touches 5 files, so the `coder` agent
implements it once this plan is approved. Run every Python command with
`PY=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`, because the
worktree has no venv and the system Python lacks PyYAML.

1. **tests/test_ci_discovery.py: end of file (after the `requires_yaml` marker
   at :34), and tests/test_coder_trailing_gates.py: end of file (reuse
   `_setup`, ending around :37, and the fake `run_gates` pattern at :40-62):**
   add the red tests listed in **Tests** (T1-T11 and C1-C4).
   → Verify by
   `$PY/pytest -q -p no:cacheprovider tests/test_ci_discovery.py tests/test_coder_trailing_gates.py`.
   The new tests fail (an `AttributeError` on `acd.ci_linters`, or an
   assertion). The 33 existing tests still pass: 28 passed and 3 skipped in
   test_ci_discovery, 5 passed in test_coder_trailing_gates.

   Traps:
   - **Imports:** import the module with
     `import analysis.ci_discovery as acd` (in the coder tests,
     `import analysis.ci_discovery as ci_discovery`). Do not add exports to
     the `apps/backend/ci_discovery.py` shim, because that edits a sixth file.
   - **PyYAML:** without it, `_parse_yaml` returns `None`, so every case that
     expects `[]` passes even with its guard removed. Put `@requires_yaml` on
     T1-T10 and C1-C4. T9 and T11 may run without it. In the coder file,
     define a local `skipif(not HAS_YAML)` using `HAS_YAML` imported from
     `analysis.ci_discovery`.
   - **Async:** the coder tests are async and use `@pytest.mark.asyncio`, as
     the existing tests do.
   - **Where workflows go:** the coder tests write workflows into the
     **worktree**, because `gate_dir` is the worktree.
   - **Ruff:** the root `tests/` directory is outside the ratchet but is still
     covered by default `ruff format --check` and `ruff check`.
   - **Subprocesses:** do not spawn any in the tests, so
     `test_no_unscrubbed_spawn` does not trip.

2. **apps/backend/analysis/ci_discovery.py:441-442 (after `discover_ci` ends at
   :440, before `get_ci_test_commands` at :443):** add `CI_LINTERS`,
   `_MAX_WORKFLOW_BYTES = 256 * 1024`,
   `_YAML_ANCHOR = re.compile(r"&[A-Za-z0-9_-]+")` and `ci_linters`:

   ```python
   def ci_linters(project_dir: Path) -> list[str]:
       """Allowlisted linters CI runs. Detection only; never raises, [] when unsure (#1627)."""
       try:
           wf_dir = project_dir / ".github" / "workflows"
           for f in (*wf_dir.glob("*.yml"), *wf_dir.glob("*.yaml")):
               if f.is_symlink() or not f.is_file() or f.stat().st_size > _MAX_WORKFLOW_BYTES:
                   return []
               if _YAML_ANCHOR.search(f.read_text(encoding="utf-8", errors="replace")):
                   return []
           config = discover_ci(project_dir)
           if config is None:
               return []
           haystacks = [" ".join([w.name, *w.steps]).lower() for w in config.workflows]
           return [
               t
               for t in CI_LINTERS
               if any(re.search(rf"(?<![a-z0-9]){re.escape(t)}", h) for h in haystacks)
           ]
       except Exception:  # noqa: BLE001 - reporting aid, must never break a build
           return []
   ```

   **apps/backend/agents/coder.py:1240-1248:** add `GateResult` to the local
   `from .gate_runner import (...)`.

   **apps/backend/agents/coder.py, between :1351
   (`results = await run_gates(gate_dir, gates)`) and :1352
   (`summary = summarize_gates(results)`):**

   ```python
   from analysis.ci_discovery import ci_linters  # noqa: PLC0415 - deferred, mirrors gate imports

   # A CI linter no gate ran is recorded `skipped`, never silently absent (#1627).
   ran = {r.name for r in results if not r.skipped}
   results += [
       GateResult(name=tool, passed=True, skipped=True)
       for tool in ci_linters(gate_dir)
       if ("lint" if tool == "eslint" else tool) not in ran
   ]
   ```

   → Verify by
   `$PY/pytest -q -p no:cacheprovider tests/test_ci_discovery.py tests/test_coder_trailing_gates.py`
   (all green), then the mutation procedure in **Tests**.

   Traps:
   - **Confirmed at HEAD:** `CIWorkflow.name: str` (:63),
     `CIWorkflow.steps: list[str]` (:65), `CIConfig.workflows:
     list[CIWorkflow]` (:87), `discover_ci -> CIConfig | None` (:429) and
     `run_gates -> list[GateResult]` (gate_runner.py:642-647). So `+=` and
     the comprehension type-check without casts.
   - **Order of guards:** check size with `stat` before reading, so an
     oversized file is never read. `is_file()` follows symlinks, so the
     `is_symlink()` clause is the only thing that refuses a link.
   - **Boundaries:** `(?<![a-z0-9])` keeps `ruff` from matching inside
     `gruff`. Only the left side is bounded, so `spotless` matches
     `spotlesscheck`.
   - **Imports:** `re` and `Path` are already imported (:26-44), so no new
     import is needed. For `GateResult`, run `ruff check --fix` and let isort
     place it; do not hand-order it.
   - **No logging** in `ci_linters` or in the new coder lines, including the
     CI text and the exception, so CodeQL stays clean.
   - **Leave unchanged:** the `if not gates:` branch (:1340-1345), the marker
     write at :1406 and the `except` at :1407-1408.
   - **Mutations:** make them only with edits. Never use `git stash`,
     `checkout` or `restore`.
   - **Commit scope** must not contain `#`. Use for example
     `fix(gates): record CI linters no gate ran as skipped (#1627)`.
   - **Staged files:** the `tests/test_security.py` GitCommitValidator test
     fails while files are staged. That is environmental.

3. **CHANGELOG.md:1 (`## [Unreleased]`):** add the entry "Trailing gates now
   record CI linters that no gate ran (e.g. `ktlint: skipped`) instead of
   omitting them; reporting only, never blocks (#1627)." Then draft the
   follow-up issue, "Repo lint lane: read CI lint during planning and run lint
   as a trailing gate". It covers:
   - Q1 outcome 1: block "complete" when repo lint would fail.
   - Q3: run lint only through the descriptor `lint` lane.
   - Q4: a nix-provided linter with `network=none` that records its version.
   - Annotating the `if not gates:` branch.
   - Q7: grep the consumers of the completion banners.
   - Deciding whether a lint that ran and failed should block.

   → Verify by `git diff --stat origin/dev...` listing only
   `apps/backend/analysis/ci_discovery.py`, `apps/backend/agents/coder.py`,
   `tests/test_ci_discovery.py`, `tests/test_coder_trailing_gates.py`,
   `CHANGELOG.md` and the `intent/`, `spec/` and `plan/` files.

   Traps:
   - **CHANGELOG conflicts:** keep both entries.
   - **The PR:**
     - Link the intent, spec and plan, say which steps the coder did, and
       paste the step 2 verification output.
     - Rebase onto `dev` and rerun `gen_autonomy_matrix.py --check`. If it
       drifts, regenerate the matrix rather than hand-editing it.
   - **Do not open the follow-up issue or the PR without the user's go-ahead.**

## Tests

### tests/test_ci_discovery.py: append `class TestCiLinters` (11 tests)

Add a helper `_wf(root, text, name="ci.yml")` that writes
`root/.github/workflows/<name>`. Use neutral names (`name: CI`, job `build`)
unless a test is about the names.

| # | Test | Input | Assert | Mutation (edit, expect FAIL, edit back) |
|---|---|---|---|---|
| T1 | test_detects_step_and_uses | ktlint job + `run: ./gradlew spotlessCheck` + `uses: ScaCap/action-ktlint@v3` | `== ["ktlint", "spotless"]` | Return matches in discovery order without deduplicating, so ktlint appears twice. |
| T2 | test_detects_job_key | job keyed `ktlint`, only step `run: ./gradlew check` | `== ["ktlint"]` | Match the steps only, not `w.name`. |
| T3 | test_left_bounded | `run: gruff --check` | `== []` | Drop the `(?<![a-z0-9])` lookbehind. |
| T4 | test_symlinked_workflow_refused | `tmp/real.yml` holds a ktlint step; `.github/workflows/ci.yml` is a symlink to it | `== []` | Delete the `is_symlink()` clause. |
| T5 | test_oversize_workflow_refused | valid ktlint workflow + `"# " + "x" * (256 * 1024)` | `== []` | Raise the cap to 1 MiB. |
| T6 | test_anchor_refused | `- run: &cmd ktlint` / `- run: *cmd` | `== []` | Delete the anchor check. The result becomes `["ktlint"]`. |
| T7 | test_alias_bomb_fast | 9-level billion-laughs list | `== []`; `time.monotonic()` elapsed `< 1.0` | None: without the guard it hangs or runs out of memory. T6 covers the same guard. |
| T8 | test_malformed_yaml | `jobs: [unclosed` | `== []` | None (contract). |
| T9 | test_no_github_dir | empty tmp dir | `== []` | None (contract). |
| T10 | test_never_raises | valid ktlint workflow; `monkeypatch.setattr(acd, "discover_ci", boom)` | `== []`, no exception | Delete the body `try/except`. |
| T11 | test_names_marker_safe | none | `len == 8`; sorted; no `,` or `:` in any name | Add `"ktlint,a:b"` to `CI_LINTERS`. |

Not tested: the regular-file guard (a FIFO). Without the guard the read blocks
forever, so its mutation check would hang. Add the test once a fixture exists
that does not hang.

### tests/test_coder_trailing_gates.py: append 4 tests

Reuse `_setup`, the `ImplementationPlan.load` patch and the fake `run_gates`.
Read the evidence with
`(spec_dir / ".trailing_gates_done").read_text().splitlines()[1]`. Line 0 is
the sha, or `-` outside git.

| # | Test | Setup | Assert | Mutation |
|---|---|---|---|---|
| C1 | test_ci_linter_without_gate_recorded_skipped | Go worktree, `run: golangci-lint run`; fake returns go-test passed | `ev.endswith("go-test: passed, golangci-lint: skipped")`; no `spec_dir / "GATE_FAILURES.md"`; `gate_runner.evidence_shows_an_executed_gate(ev)`; `set(gate_runner.gate_outcomes(ev)) <= {"passed", "failed", "skipped"}` | **Spec mutation:** delete the `results += [...]` statement. C1 and C2 fail. |
| C2 | test_ci_text_never_reaches_evidence | Go worktree, `run: ktlint --reporter=plain,a:b` | `ev == "go-test: passed, ktlint: skipped"`; `"a:b"` and `"plain"` not in `ev`; `gate_outcomes(ev) == ["passed", "skipped"]` | Make `ci_linters` return the matched step text instead of the constant. |
| C3 | test_eslint_covered_by_npm_lint | `go_in_worktree=False`; worktree `package.json` with `{"scripts": {"lint": "eslint ."}}`; CI `run: npx eslint .`; fake returns `lint` passed | `ev == "lint: passed"`; `"eslint"` not in `ev` | Replace `("lint" if tool == "eslint" else tool)` with `tool`. |
| C4 | test_marker_written_when_detection_raises (deviation) | Go worktree + ktlint CI; `monkeypatch.setattr(ci_discovery, "discover_ci", boom)` | marker exists; `ev == "go-test: passed"` | Delete the `try/except` in `ci_linters`. The coder's outer `except` then skips the marker write. |

Not tested on purpose: the `if not r.skipped` filter in `ran`. A skipped npm
`lint` with eslint in CI also records `eslint: skipped`, which is the
behaviour the spec calls for.

### Mutation procedure

For each row that has a mutation:

1. Make the edit.
2. Run
   `$PY/pytest -q -p no:cacheprovider <file>::<Class>::<test>` (or
   `<file>::<test>`). Expect **1 failed**, or 2 failed (C1, C2) for the spec
   mutation.
3. Edit the code back.
4. Rerun the same command and expect it to pass.

Afterwards, `git diff --stat` must list only the two source files, the two
test files and `CHANGELOG.md`. Re-read the restored lines by eye.

### Commands and expected results

1. `$PY/pytest -q -p no:cacheprovider tests/test_ci_discovery.py -k TestCiLinters`
   gives **11 passed**.
2. `$PY/pytest -q -p no:cacheprovider tests/test_coder_trailing_gates.py` gives
   **9 passed**.
3. `$PY/pytest -q -p no:cacheprovider tests/test_coder_trailing_gates.py tests/test_ci_discovery.py tests/test_qa_approval_needs_gate_evidence.py tests/test_gate_evidence_tree_binding.py tests/test_gates_on_already_complete.py`
   gives **68 passed, 3 skipped**. The baseline is 53 passed, 3 skipped, plus
   15 new tests.
4. `$PY/pytest tests/ -m "not slow" -q` (what CI runs) shows no new failures.
   The GitCommitValidator test fails while files are staged; that is
   environmental.
5. `ruff format --check apps/backend apps/web-server scripts tests && ruff check apps/backend apps/web-server scripts tests`
   is clean.
6. After `git add`, run
   `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`,
   then the same command with
   `--tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini`.
   Neither reports a new violation.
7. `python scripts/gen_autonomy_matrix.py --check` passes. Rerun it after any
   rebase, and regenerate rather than hand-merge.

## Rollback

Revert the single squash commit with `git revert <sha>`, or use
`git revert -m 1 <merge>` for a merge commit. On a CHANGELOG conflict, drop
only the #1627 line.

No state needs migrating or cleaning up. Markers already written with
`<tool>: skipped` still use a known outcome in `_KNOWN_GATE_OUTCOMES`, so QA
and the CLI read them the same way after the revert.
