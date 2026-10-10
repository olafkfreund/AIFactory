---
status: draft
issue: 1627
author: olafkfreund
---

# Intent: builds never run the target repo's own lint checks

Code paths are at commit 6ae5b646 (origin/dev, 2026-10-10).

## Problem

Not fixed on dev. No commit, PR or artifact references #1627. Outside the
tests, `apps/backend` never mentions ktlint, detekt or spotless.

A build reports success without running the lint or format jobs that the
target repo's CI runs. The PR is then the first place the problem shows up.

Reported case: in the MyFriends demo, task 022 produced 42 commits, 48 files
and 259 passing tests, and printed "All subtasks completed!". Its PR then
failed the repo's `kotlin-core (ktlint)` check (pinterest/ktlint 1.3.1) with
240 violations. All of them were formatting only.

Why:

- **Planning never sees the repo's lint.** `analysis/ci_discovery.py`
  (`CIDiscovery`, :96) parses `.github/workflows/*.yml` and keeps only test
  commands (`_extract_test_commands`, :356). It drops the lint steps. No
  production code calls `discover_ci` or `get_ci_test_commands`. The
  planner prompts pass on `test_command` only (`prompts/planner.md:174`).
- **The trailing gates include no lint except npm's.** `detect_gates`
  (`agents/gate_runner.py:101`) has one lint gate, `npm run lint` (:135).
  Languages handled by descriptors get only their `unit` lane (:246).
  `core/languages/kotlin.yaml` has no lint lane, and the loader rejects
  lane keys it does not know (`core/language_descriptors.py:117-120`).
- **Nothing says lint was skipped.** The completion messages are based
  only on subtask counts: `agents/coder.py:957` and :1077,
  `core/progress.py:179`, and `agents/tools_pkg/tools/progress.py:141`.
  The QA refusal (`tools_pkg/tools/qa.py:352-369`) and the CLI warning
  (`cli/build_commands.py:615-624`) react to a gate that failed. They
  cannot see a gate that never ran.

Reproduced at 6ae5b646 with a scratch Kotlin repo whose CI has a ktlint job.
`detect_gates` returns only `kotlin-unit` (`gradle test`), and
`discover_ci` returns empty `test_commands`.

This happens on every build whose target repo has a CI lint job that is not
an npm `lint` script.

## Proposed outcome

- A build whose target repo has a CI lint or format job does not report
  "complete" or "ready" while that check would fail.
- If a lint check is not run, the completion summary says so (for example,
  "tests pass, lint not run").
- For the reported case, the build either catches the ktlint violations
  before the PR opens or names ktlint as not run.

## Affected users and systems

- Operators and reviewers, who get a PR that was called finished and then
  fails the repo's required check.
- The autonomous PARR chain (build, auto-PR, TFactory verify, merge). A red
  required check stops the hands-free merge.
- Target repos in every language except npm projects with a `lint` script.
  That includes Kotlin, Swift and Go. Python repos get mypy and pytest but
  no ruff or flake8 gate.
- Code: `apps/backend/analysis/ci_discovery.py`, `agents/gate_runner.py`,
  `agents/coder.py`, `core/languages/*.yaml`, `core/language_descriptors.py`,
  `core/progress.py`, and the planner prompts.

## Constraints

- Commands taken from a target repo are untrusted. They must run only
  inside the existing gate sandbox (`gate_runner.py:361-396`, `network=none`
  by default, `GATE_TIMEOUT_SECONDS`), never on the host.
- The sandbox has no network. CI-pinned linters fetched at run time (Gradle
  plugins, a downloaded ktlint) fail there. A nix-provided linter can be a
  different version from the one CI pins.
- Kubernetes: a lint gate uses the same runner as the existing gates and
  adds no PVC or volume. Completion status must come from on-disk state,
  because another replica reads it.
- Today the trailing-gate path never raises (`coder.py:1403`), and the
  autonomy matrix and PR endgame depend on that. Changing it changes their
  contract. The generated autonomy matrix may then need regenerating.
- Consumers may parse the completion strings. Before rewording them, grep
  the web server and the CFactory and TFactory handoffs.
- Overlap: #1673 (open PR) changes the completion path at `coder.py:1092`.
  #1620 changes the nix store and kube runner in `gate_runner.py`.

## Open questions

1. Scope: all three fixes from the issue (read CI lint during planning,
   run it as a trailing gate, report "lint not run"), or only the
   reporting fix now, with the other two as follow-ups?
2. When the target repo's lint fails, should the build's completion check
   fail, or should the build only warn and label the PR?
3. Where should lint commands come from: the repo's own CI workflows,
   filtered by an allowlist, or a curated `lint` lane in each language
   descriptor?
4. Linter version and network: allow sandbox egress for lint gates, accept
   the nix-provided version, or bake the tools into the image?
5. Sequencing: wait for #1673 to merge and for #1620 to reach spec, or go
   ahead and rebase later?
6. Should the Swift "tests no lane can execute" problem (TFactory#1340)
   stay out of scope?
