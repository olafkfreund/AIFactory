---
status: draft
issue: 1627
intent: intent/2026-10-10-1627-coder-repo-lint-gate.md
---

# Spec: record CI linters that no gate ran as `skipped`

## Design

Only the reporting fix ships now. When the trailing gates have run, each
linter that the target repo's CI runs and no executed gate covered gets a
`GateResult(name=<tool>, passed=False, skipped=True)`. For the reported
case, the record reads `kotlin-unit: passed, ktlint: skipped`. That text
reaches stdout (`agents/coder.py:1387`, `:1394`), the `.trailing_gates_done`
evidence (`:1396`, `gate_runner.py` `write_trailing_gate_marker`) and, from
there, the PR body. Nothing is executed and no verdict changes.

All paths are under `apps/backend/`. Two production files are touched, with
about 25 lines.

### 1. `analysis/ci_discovery.py`: one constant and one helper after `discover_ci` (:429-440)

```python
# Only these constants ever reach gate evidence; CI text never does (#1627).
CI_LINTERS: tuple[str, ...] = (
    "detekt", "eslint", "flake8", "golangci-lint", "ktlint", "ruff", "spotless", "swiftlint",
)
_MAX_WORKFLOW_BYTES = 256 * 1024
_YAML_ANCHOR = re.compile(r"(?:^|[\s:\[{,-])&[\w-]+", re.MULTILINE)


def ci_linters(project_dir: Path) -> list[str]:
    """Allowlisted linters the repo's CI mentions. Never raises; [] when unsure."""
    try:
        wf_dir = project_dir / ".github" / "workflows"
        for f in [*wf_dir.glob("*.yml"), *wf_dir.glob("*.yaml")]:
            # ponytail: refuse hostile files rather than parse them on the host;
            # a legit anchored or >256 KiB workflow just goes undetected (today's behaviour).
            if f.is_symlink() or not f.is_file() or f.stat().st_size > _MAX_WORKFLOW_BYTES:
                return []
            if _YAML_ANCHOR.search(f.read_text(encoding="utf-8", errors="replace")):
                return []
        config = discover_ci(project_dir)
        steps = "\n".join(s for wf in (config.workflows if config else []) for s in wf.steps).lower()
        return [n for n in CI_LINTERS if re.search(rf"(?<![a-z0-9]){re.escape(n)}", steps)]
    except Exception:  # noqa: BLE001 - detection is advisory, never costs the gate record
        return []
```

- It reuses `CIWorkflow.steps`, which already holds each step's name,
  `run:` line and `uses:` (:200-225). No new parsing. CircleCI and Jenkins
  also fill `workflows`, so they are covered too; only the GitHub path has
  the pre-guard.
- **Why the pre-guard.** `discover_ci` has no caller today. This change makes
  it run on the host against agent-writable files, outside the sandbox.
  `_parse_github_actions` (:153-165) follows symlinks (`ci.yml -> /dev/zero`)
  and has no size cap. `yaml.safe_load` (:347) accepts aliases, and
  `str(step).lower()` (:220) expands a small alias bomb exponentially. The
  guard refuses all three before `discover_ci` sees them.
- **Why the `try`.** The whole trailing-gate body sits in one `try` whose
  `except` only logs at debug level (`coder.py:1239`, `:1397-1398`). If
  detection raised, `write_trailing_gate_marker` (:1396) would be skipped, and
  QA (`tools_pkg/tools/qa.py:340-352`) and the CLI would see no gate evidence.
- **Left-bounded match.** `ktlint`, `spotlessCheck`, `ruff check` and
  `uses: ScaCap/action-ktlint` match; `gruff` does not.
- Output order follows the sorted constant and has no duplicates, so the
  same tree always gives the same marker.

### 2. `agents/coder.py`: `_run_trailing_gates_if_build_complete`, between :1351 and :1352

- Add `GateResult` to the existing local `from .gate_runner import (...)`
  (:1240-1248).
- Add a single-name local import:
  `from analysis.ci_discovery import ci_linters  # noqa: PLC0415`
  (the same pattern as `qa.py:261`).

```python
results = await run_gates(gate_dir, gates)
# #1627: a lint the repo's CI runs but no executed gate covered is recorded, not silent.
ran = {r.name for r in results if not r.skipped}
results += [
    GateResult(name=tool, passed=False, skipped=True)
    for tool in ci_linters(gate_dir)
    if {"eslint": "lint"}.get(tool, tool) not in ran  # npm `lint` gate (gate_runner.py:135) covers eslint
]
summary = summarize_gates(results)
```

**Unchanged behaviour:**

- `failing_gates` excludes skips (`gate_runner.py:704-706`), so a lint skip
  never creates `GATE_FAILURES.md` and never raises. When another gate did
  fail, the `Summary:` line of `GATE_FAILURES.md` also lists
  `ktlint: skipped`. That is informational only, because the fix loop acts on
  the `## <gate>` sections, which list failures only.
- `evidence_shows_an_executed_gate` stays True whenever a real gate ran
  (`gate_runner.py:933`). All outcomes stay in `_KNOWN_GATE_OUTCOMES`
  (:900), so QA approval (`qa.py:341-369`) and the CLI warning
  (`cli/build_commands.py:610-624`) behave as before.
- If every real gate was skipped, the CLI's "every gate was skipped" message
  now also names the linter, which is accurate.

**Not touched:** `gate_runner.py`, `cli/build_commands.py`, `deploy_scaffold`,
`core/languages/*.yaml`, `core/language_descriptors.py`, and the completion
banners. So there is no overlap with #1673 or #1620.

### Proposed answers to the intent's open questions

These are proposed defaults for the approver.

1. **Scope.** Only the reporting fix now. Open one follow-up issue for
   "read CI lint during planning" and "run lint as a trailing gate".
   *Why:* `discover_ci` already extracts every step and only lacks a caller.
   The summary already reaches stdout, the marker and the PR body
   (web-server `merger.py:205-213`), which meets outcome bullets 2 and 3. It
   uses the existing word `skipped`, not a new one like `not run`, because
   `gate_outcomes` splits on `,` and `:` (`gate_runner.py:903-906`) and any
   other word fails closed (:900, :931-932). That would make QA (`qa.py:272`)
   refuse every Kotlin build.
2. **Fail or warn.** Warn only. A lint that was not run is `skipped`. It is
   not written to the failure sections of `GATE_FAILURES.md` and it does not
   raise. Whether a lint that ran and failed should block is a question for
   the follow-up.
   *Why:* the trailing-gate path never raises (`coder.py:1397`), and the
   autonomy matrix and PR endgame depend on that.
3. **Source of lint commands.** Detection only, with a fixed allowlist
   matched against the discovered CI steps. Only allowlist constants are
   written into the evidence. No CI text is copied and nothing is executed.
   Execution belongs to the follow-up, through a curated descriptor `lint`
   lane, never commands copied from CI.
   *Why:* repo commands are untrusted, and CI text in the evidence could
   corrupt the `,`/`:` parse. A lane needs `KNOWN_LANES` and
   `_ALLOWED_LANE_KEYS` (`core/language_descriptors.py:46`, `:112`) plus the
   hub-governed YAML (`core/languages/kotlin.yaml:3-6`), which is cross-repo
   work.
4. **Version and network.** Defer. Nothing here runs a linter, so the
   sandbox stays `network=none` (`gate_runner.py:361`). For the follow-up,
   default to the nix-provided linter with `network=none` and record its
   version in the gate output. Do not open egress.
5. **Sequencing.** Go ahead now and rebase later. #1673 edits
   `build_commands.py` and `deploy_scaffold` (called at `coder.py:1086-1092`),
   and #1620 edits `gate_runner.py:355-396`. Neither touches this block or
   `ci_discovery.py`.
6. **Swift lanes (TFactory#1340).** Keep them out of scope. That problem is
   about test lanes, it is already tracked, and it lives in hub-governed
   `swift.yaml`.
7. **Banners** (`coder.py:1077`, `core/progress.py:179`,
   `tools_pkg/tools/progress.py:141`). Leave them unchanged. They have
   consumers (`cli/build_commands.py:134`, web-server `task_service.py:825`),
   and the "Trailing gates passed/failed: <summary>" line plus the PR
   evidence already carry `ktlint: skipped`.

## Alternatives rejected

- **A new outcome word such as `not run`.** It is outside
  `_KNOWN_GATE_OUTCOMES`, fails closed and blocks every Kotlin build at QA.
- **Echoing the CI step or job name** (e.g. `kotlin-core (ktlint)`). That is
  untrusted text, and a `,` or `:` in it corrupts `gate_outcomes`. It is also
  a CodeQL `py/log-injection` source.
- **Detection inside `detect_gates` or `gate_runner.py`.** It collides with
  #1620, and it mixes "a gate to run" with "a gate we know we did not run".
- **Two summary strings** (gates only for `GATE_FAILURES.md`, gates plus
  lint for the marker). It keeps `ktlint: skipped` off the `Summary:` line,
  but the fix loop works from the failure sections, so it adds diff for
  no change in behaviour.
- **Annotating the `if not gates:` branch (:1340-1345).** QA and the CLI key
  on `startswith("no gates detected")`, so that marker text must not change,
  and a status-only line there adds code for a case nobody reported. This is
  an accepted ceiling, covered by the follow-up.
- **Scanning raw workflow text without a YAML parse.** It avoids the alias
  bomb, but comments would produce false positives, and the pre-guard gives
  the same safety while keeping Q3's "discovered CI steps".
- **Rewording the banners.** Per Q7.

## Risks

- **False positive.** A step that mentions an allowlisted name without
  running it (for example `echo "skip ruff"`), or a `kotlin-unit` that runs
  ktlint through Gradle, records `ruff: skipped` or `ktlint: skipped`. That
  is one extra informational token, warn-only, and it changes no verdict.
  The follow-up lint lane fixes it.
- **False negative.** Linters outside the allowlist, CI systems
  `discover()` does not parse (GitLab), anchored or oversized GitHub
  workflows, and a missing PyYAML (`_parse_yaml` returns None, :344-354)
  all stay silent. That is the same as today, so there is no regression.
  The anchor regex can also match a rare shell `&word`, which skips
  detection; that is the safe direction.
- **Stale cache.** None. `discover_ci` builds a fresh `CIDiscovery` on each
  call (:439), and there is no module cache in the long-lived web-server
  process.
- **Ratchet.** Both files are in the per-file ruff and `mypy --strict`
  ratchet (`*/apps/backend/*.py`, `scripts/cq_ratchet.py:171`). The new code
  is fully typed and adds no violations.
- **Hosts.** This runs on whichever host or build Job runs the trailing
  gates. It adds no PVC, volume, network or subprocess.

## Verification

Tests go in root `tests/`, which is outside the ratchet. YAML-dependent
cases use `pytest.mark.skipif(not HAS_YAML)`.

1. `tests/test_ci_discovery.py`, for `ci_linters`:
   - a ktlint job, `./gradlew spotlessCheck` and `uses: ScaCap/action-ktlint`
     give `["ktlint", "spotless"]`
   - a `gruff` step gives `[]`
   - a symlinked workflow gives `[]`
   - a workflow over 256 KiB gives `[]`
   - an alias-bomb workflow returns `[]` in under a second
   - malformed YAML and a missing `.github` both give `[]`
   - no name in `CI_LINTERS` contains `,` or `:`
2. `tests/test_coder_trailing_gates.py`, using the existing `_setup` and
   fake `run_gates` pattern (:41-60):
   - **Go plus `golangci-lint run` in CI, `go-test` passed.** The marker
     evidence ends with `go-test: passed, golangci-lint: skipped`, there is
     no `GATE_FAILURES.md`, `evidence_shows_an_executed_gate` is True, and
     every `gate_outcomes` value is in {passed, failed, skipped}.
   - **Injection guard.** With the step `run: ktlint --reporter=plain,a:b`,
     the evidence has `ktlint: skipped` and nothing from the step text.
   - **npm `lint` passed plus `eslint` in CI.** There is no `eslint` entry.
   - **`ci_linters` monkeypatched to raise** (in `agents.coder`'s import
     target). The marker is still written.
3. **Mutation check.** Delete the `results += [...]` statement, and the
   golangci-lint test must fail. Restore it by re-editing (no git stash,
   checkout or restore).
4. Unchanged suites pass:
   `pytest tests/test_coder_trailing_gates.py tests/test_ci_discovery.py tests/test_qa_approval_needs_gate_evidence.py tests/test_gate_evidence_tree_binding.py tests/test_gates_on_already_complete.py`
5. CI gates:
   - `ruff format --check` and `ruff check`
   - `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`,
     then the same command with `--tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini`
   - `python scripts/gen_autonomy_matrix.py --check`, which cites neither
     file; re-run after any rebase
   - CodeQL: only constant labels are logged, and nothing is spawned, so
     `tests/test_no_unscrubbed_spawn.py` is unaffected
   - `tests/test_security.py`'s GitCommitValidator fails while files are
     staged. That is environmental, not a regression.
6. Housekeeping:
   - add a CHANGELOG entry under `[Unreleased]`, keeping both sides on
     conflict
   - the commit scope has no `#`
   - open the follow-up issue for the lint lane and planning-time CI lint
     (Q1, Q3, Q4)

The plan will have three file-editing steps (helper, coder.py block,
tests), which meets the `coder` handoff threshold.
