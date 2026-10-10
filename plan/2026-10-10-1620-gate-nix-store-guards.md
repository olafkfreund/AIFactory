---
status: draft
issue: 1620
spec: spec/2026-10-10-1620-gate-nix-store-guards.md
---

# Plan: gate Nix store guards and comments match reality

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1620`, branch
`fix/1620-gate-nix-store-guards`, spec approved at `378ff4b6`. All paths are
repo-relative. Every cited line was checked at `378ff4b6` and matches the
spec's `7fc66064` references; no later commit touched these regions.

## Approved decisions (self-contained summary)

1. **Scope.** #1620 covers intent items 1-3 only: the false comments, guard
   parity between the two gate paths, and the `nix_env.py` promise. It closes
   when they merge. Item 4 (the unseeded build `/nix/store` mount) goes to
   follow-up (a). Item 5 (Swift diagnosis) goes to ops follow-up (b).
2. **Guard (Q1).** Delete the gate-side #1541 packed-path exception. Both gate
   paths (packed and co-mount) pass the `nix_in_image()`-guarded value. The
   build path stays as it is: `build_backend.py:663-664` drops the PVC on its
   packed path only.
3. **No passthrough here.** Do not add `AIFACTORY_PACKED_NIX_IN_IMAGE` or
   `AIFACTORY_NIX_STORE_PVC` to `_PASSTHROUGH_BUILD_ENV`
   (`apps/web-server/server/services/build_backend.py:259`). Forwarding both
   together is follow-up (a), after #1674 and #1692. Forwarding only one
   brings back the #253 node pin, so that option is rejected.
4. **Single guarded read.** Remove the name `configured_store_pvc` entirely, so
   no later call site can pick up the unguarded value. Changing only line 538
   was rejected for that reason.
5. **Build store mount (Q2).** No code change here. Follow-up (a) removes the
   unseeded mount (`job_dispatch.py:554-562`), the self-check at
   `job_dispatch.py:912` and `tests/test_build_backend_kubejob.py:128,178,1412-1500`,
   and updates the `AIFACTORY_NIX_STORE_PVC` row in
   `docs/docs/environment-reference.md`. Reviving the PVC is not proposed.
6. **Follow-ups (Q3).** File (a) and (b) BEFORE the PR, because their numbers
   (called `#A` and `#B` below) go into the rewritten comments.
7. **Ordering (Q4).** Neither wait nor rebase. This change does not touch
   `build_backend.py` or `_PASSTHROUGH_BUILD_ENV`, so it merges independently
   of #1674, #1673, #1692 and #1677. The follow-ups wait for them.
8. **Swift probe (Q5).** The user or an operator with kubectl runs it in (b)
   after merge, by raising `ttlSecondsAfterFinished` or with
   `kubectl get jobs -w`, and captures the logs. Until then SPM fetching over
   git stays labelled a hypothesis.
9. **Comment text.** State only measured facts: the `-nix` image bakes swift,
   kotlin, gradle and openjdk-21, and inside the build Job neither variable is
   set. The Swift deadline cause is unknown and points to `#B`. Nothing may
   contradict `nix_env.py:38-41`.
10. **Logging.** The log lines at `gate_runner.py:504-507` are not changed;
    after the fix they are accurate for both paths. No log calls are added or
    changed (CodeQL).
11. **Out of scope (rejected).** Moving the guard into `KubeJobSandbox` /
    `kube_sandbox.py`; editing `qa.py:155` (accurate history); touching
    `build_backend.py`, `job_dispatch.py` or `test_build_backend_kubejob.py`.
12. **Tests.** Rename and rewrite the packed test to assert `None`; add a
    flag-off packed test asserting `"aifactory-nix-store"` (it catches a
    hard-coded `None`). Leave `:94`, `:109` and
    `test_co_mount_path_still_drops_the_warm_store` unchanged.
13. **Runtime effect.** None today: the build Job forwards neither variable,
    so the gate path behaves the same before and after.

**Ownership.** Steps 1-3 edit files (4 files total), so the `coder` agent
(Sonnet) takes them, step 1 at start and each later step via `SendMessage`.
The session model (Opus) does step 0 and step 4 (external actions).

**Deviation from the spec's order.** Filing the follow-ups moves to step 0:
step 2's comments need their numbers, and filing at the end would mean a second
edit pass or placeholders that could ship.

## Steps

0. **(session model) GitHub: file follow-ups (a) and (b)** → verify by
   `gh issue view <A>` and `gh issue view <B>`.
   - (a) Forward `AIFACTORY_PACKED_NIX_IN_IMAGE` and `AIFACTORY_NIX_STORE_PVC`
     together into the build Job (`_PASSTHROUGH_BUILD_ENV`,
     `build_backend.py:259`). Remove the unseeded build `/nix/store` mount
     (`job_dispatch.py:554-562`), the self-check at `job_dispatch.py:912` and
     `tests/test_build_backend_kubejob.py:128,178,1412-1500`. Update the
     `AIFACTORY_NIX_STORE_PVC` row in `docs/docs/environment-reference.md`.
     Blocked by #1674 and #1692. Forwarding only one variable brings back the
     #253 node pin.
   - (b) Ops: diagnose why the Swift gate hits its deadline. Run the probe with
     kubectl (raise `ttlSecondsAfterFinished` or `kubectl get jobs -w`) and
     capture the logs. SPM fetching over git is a hypothesis.
   - Record the numbers as A and B; steps 2-4 substitute them.
   Traps: the github MCP server failed to connect in this session, so use
   `gh issue create`. Both issues must exist before step 2 starts.

1. **(coder) `tests/test_gate_runner_nix.py`: tests, red first** → verify by
   `$PY -m pytest tests/test_gate_runner_nix.py -q`: 1 failed (the renamed
   test, which still gets `"aifactory-nix-store"`), 20 passed.
   - `:324-326`, docstring of `test_gate_timeout_is_configurable`: becomes
     "The budget must be raisable without a release. Parsed from a mapping, so
     no module reload is needed — reloading swaps module identity and breaks
     sibling tests' monkeypatching." Assertions unchanged.
   - `:341`: rename `test_packed_path_uses_the_warm_store_even_with_nix_in_image`
     to `test_packed_path_drops_the_warm_store_when_nix_in_image`.
   - `:342-349` docstring: the `-nix` image bakes swift, kotlin, gradle and
     openjdk-21; the packed path honours `nix_in_image()` like the co-mount
     path and the build packed path (`build_backend.py:663-664`).
   - `:360` comment becomes
     `# Nix IS in the image, so the packed path drops the warm store too.`
   - `:375` becomes `assert seen["nix_store_pvc"] is None, seen`.
   - `:376-377`: drop the "genuinely the only PVC" comment; keep the assertion
     as `assert "repo_pvc" not in seen  # proves the packed branch ran, not co-mount`.
   - After `:377` insert
     `test_packed_path_keeps_the_warm_store_when_flag_off(monkeypatch, tmp_path)`,
     docstring "Default OFF stays warm on the packed path too." Same setup as
     the renamed test (flake.nix in `tmp_path`, `AIFACTORY_DATA_ROOT`,
     `S3_ENDPOINT`, `AIFACTORY_BUILD_IMAGE=aifactory:sha-x-nix`,
     `AIFACTORY_NIX_STORE_PVC=aifactory-nix-store`, patched
     `_packed_workspace_for`, local `FakeSandbox`), except
     `monkeypatch.delenv("AIFACTORY_PACKED_NIX_IN_IMAGE", raising=False)`.
     Assert `seen["nix_store_pvc"] == "aifactory-nix-store", seen` and
     `"repo_pvc" not in seen`. It is green from the start; it exists to catch
     the hard-coded-`None` mutation.
   - Do not touch `:94-107`, `:109-115` or
     `test_co_mount_path_still_drops_the_warm_store`.
   Traps: root `tests/` is outside the ratchet, so no mypy-strict typing, but
   default `ruff format` / `ruff check` apply. Keep the local `FakeSandbox`
   pattern; do not switch to `_capture_sandbox`. Do not commit while red;
   steps 1 and 2 land in one commit.

2. **(coder) `apps/backend/agents/gate_runner.py` and
   `apps/backend/core/nix_env.py`: code and comments** → verify by
   `$PY -m pytest tests/test_gate_runner_nix.py -q` → `21 passed`, then the
   mutation checks and linters in ## Tests.
   - `gate_runner.py:40-45`, comment above `_timeout_from_env` (`:46`): replace.
     New text: the budget is 600s and can be raised without a release; the
     `-nix` image bakes swift, kotlin, gradle and openjdk-21; why Swift hits
     the deadline is unknown (SPM fetching over git is a hypothesis), see #B.
     Remove "cold-fetches", "bakes no" and "drags in GTK".
   - `gate_runner.py:483-490`: keep (RFC-0016 #197 and #253 rationale).
   - `gate_runner.py:491-501`: replace the #1541 RWX / packed-exception
     paragraph and the "bakes no language closures" paragraph with one short
     paragraph saying: (1) both gate paths drop the PVC when `nix_in_image()`
     is true; the build path drops it on its packed path only
     (`build_backend.py:663-664`); (2) inside the build Job neither variable is
     forwarded (`_PASSTHROUGH_BUILD_ENV`, `build_backend.py:259`), so the guard
     is inert there today; (3) the two must be forwarded together (#A), because
     forwarding only the PVC brings back the #253 node pin.
   - `gate_runner.py:502-503` become:
     ```python
     nix_store_pvc = None if nix_in_image() else (
         os.environ.get("AIFACTORY_NIX_STORE_PVC", "") or None
     )
     ```
   - `gate_runner.py:504-507` (log block): no change.
   - `gate_runner.py:532-535`: delete the "Warming matters most here" comment
     inside the packed `try:`.
   - `gate_runner.py:538`: `nix_store_pvc=configured_store_pvc,` becomes
     `nix_store_pvc=nix_store_pvc,`. `:565` already reads that way.
   - `nix_env.py:44-46`, `nix_in_image()` docstring: keep "Read by both Job
     paths (build: build_backend; gate/verify: gate_runner)". Add: the gate
     runs inside the build Job, which inherits neither
     `AIFACTORY_PACKED_NIX_IN_IMAGE` nor `AIFACTORY_NIX_STORE_PVC`, so on the
     gate path the flag is off and no store PVC is set until #A forwards both
     together. `:38-42` stay.
   Traps:
   - `:502-503` and `:538` change in the same edit; removing the name while
     `:538` still uses it raises `NameError`.
   - The guarded read exceeds 88 chars on one line; keep the parenthesised
     wrap and run `ruff format`.
   - No log call added or changed (CodeQL clear-text-logging and
     log-injection). `:505` still logs `nix_store_pvc`, a non-sensitive name.
   - No subprocess spawns added (`test_no_unscrubbed_spawn` unaffected).
   - Do not touch `build_backend.py`, `job_dispatch.py`, `kube_sandbox.py`,
     `qa.py` or `test_build_backend_kubejob.py`.
   - New comments must not contradict `nix_env.py:38-41`.
   - The coder cannot run `git stash`, `checkout` or `restore`: mutations are
     applied and reverted by editing.

3. **(coder) `CHANGELOG.md:101-108`: one bullet** under `[Unreleased]` →
   `### Fixed`, after the #1669 entry and before `## 3.6.84` (`:109`) → verify
   by `git diff CHANGELOG.md` showing one added bullet in that section.
   Text: "The packed gate path now honours `AIFACTORY_PACKED_NIX_IN_IMAGE` like
   the build path. Runtime behaviour does not change, because the build Job
   inherits neither variable (#1620, follow-ups #A, #B)."
   Traps: a conflict with other in-flight PRs is likely; keep both entries.

4. **(session model) Commit, review, PR** → verify by `git log -1` and
   `gh pr view`.
   - Commit (steps 1-3 together):
     `fix(gate): honour nix_in_image on the packed gate path (#1620)`, ending
     with the attribution trailer lines. The scope must not contain `#`.
   - Put the venv on PATH so the pre-commit hook runs
     (`PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH`).
   - Review with a fresh `opus` agent given only this plan path and `git diff`.
   - PR description links `intent/`, `spec/` and `plan/`
     `2026-10-10-1620-gate-nix-store-guards.md`, plus #A and #B, says the coder
     did steps 1-3, and uses "Closes #1620" (valid only because #A and #B exist).
   Traps: a `GitCommitValidator` failure in `tests/test_security.py` while
   files are staged is environmental. No ordering dependency on #1674, #1673,
   #1692 or #1677.

## Tests

Run from the worktree with `PY=/mnt/code/Source-home/GitHub/AIFactory/.venv/bin/python`
(the system python has no pytest). Baseline at `378ff4b6`: 20 passed.

1. `$PY -m pytest tests/test_gate_runner_nix.py -q` → `21 passed`.
2. `$PY -m pytest tests/test_gate_runner_nix.py -q -k "packed_path or warm_store"`
   → `5 passed` (2 packed tests, `:94`, `:109`, co-mount test).
3. Mutation checks, each applied by editing, file re-run, then reverted by
   editing:

   | # | Edit in `gate_runner.py` | Must fail |
   |---|---|---|
   | M1 | packed call: `nix_store_pvc=os.environ.get("AIFACTORY_NIX_STORE_PVC") or None` | only the renamed packed test (1 failed, 20 passed) |
   | M2 | packed call: `nix_store_pvc=None` | only `test_packed_path_keeps_the_warm_store_when_flag_off` |
   | M3 | guard inverted: `(os.environ.get(...) or None) if nix_in_image() else None` | 5 failed: `:94`, `:109`, both packed tests, co-mount test |
   | M4 | all reverted | none: `21 passed` |

   The `nix_env.py` docstring, comments and CHANGELOG are prose; check 4
   covers the stale text.
4. `grep -rnE 'bakes no|cold-fetches|configured_store_pvc|drags in GTK' apps/backend tests/test_gate_runner_nix.py`
   → no output (exit 1).
5. `ruff format --check apps/backend apps/web-server scripts tests` and
   `ruff check apps/backend apps/web-server scripts tests` → exit 0.
6. After `git add`, strict ratchet with no new findings:
   - `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
   - `python scripts/cq_ratchet.py --staged --tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
7. `python scripts/gen_autonomy_matrix.py --check` → passes (the matrix cites
   neither `gate_runner.py` nor `nix_env.py`). If a rebase shifts cited lines,
   regenerate with `python scripts/gen_autonomy_matrix.py`; never hand-merge.

Skipped: autonomy-matrix regen (not cited); web-server tests (backend-only
change).

## Rollback

- **Before merge:** re-edit the four files back to `378ff4b6` (the coder
  cannot use checkout); the test count falls back to 20.
- **After merge:** `git revert <merge-sha>` in a PR titled
  `revert(gate): ... (#1620)`, then re-run ## Tests.
- **Runtime:** no risk today, because the build Job forwards neither variable.
  Once follow-up (a) forwards both, the fast lever is unsetting
  `AIFACTORY_PACKED_NIX_IN_IMAGE` in gitops, which restores the warm store on
  both gate paths without a release.
