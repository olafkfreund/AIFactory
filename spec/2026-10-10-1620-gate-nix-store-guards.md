---
status: approved
issue: 1620
intent: intent/2026-10-10-1620-gate-nix-store-guards.md
---

# Spec: gate Nix store guards and comments do not match reality

## Design

This change covers intent items 1-3: the false comments, guard parity between
the two gate paths, and the `nix_env.py` promise. Items 4 and 5 go to follow-up
issues (see Q2, Q3 and Q5 below). The change does not add to the env surface,
change mounts or touch the build path. Lines are as of `7fc66064`.

### Proposed answers to the intent's open questions (defaults, not yet approved)

1. **Guard (items 2-3).** Delete the gate-side exception. The packed call
   passes the guarded value, so both gate paths apply `nix_in_image()`.
   The build path applies it on its packed path only (`build_backend.py:663-664`);
   the gate's co-mount path already applies it, and this keeps that. Neither variable is added to
   `_PASSTHROUGH_BUILD_ENV` here. Forwarding both together goes into a follow-up
   issue after #1674 and #1692. Why: the exception's only rationale is the
   "bakes no language closures" claim (`gate_runner.py:499-501`), which the
   issue measured as false. Neither variable is passed through today, so
   runtime behaviour does not change.
2. **Build store mount (item 4).** No code change here. File a follow-up to
   remove the unseeded `/nix/store` mount (`job_dispatch.py:554-562`), the
   self-check at `:912` and the tests at
   `tests/test_build_backend_kubejob.py:128,178,1412-1500`. Reviving the PVC is
   not proposed. The mount is opt-in (`build_backend.py:659`), so default
   installs never use it, and `#1677` touches the same test file.
3. **Scope.** #1620 covers items 1-3 and closes when they merge. Item 4 goes to
   one follow-up issue, item 5 to a separate ops issue. The intent's outcome
   allows "a tracked task to find one" for item 5.
4. **Ordering.** Neither wait nor rebase. Nothing here touches
   `build_backend.py` or `_PASSTHROUGH_BUILD_ENV`, so the change merges
   independently of #1674, #1673, #1692 and #1677. The follow-ups wait for them.
5. **Swift probe (item 5).** The user, or an operator with kubectl, runs it in
   the ops follow-up after this merges. They either raise
   `ttlSecondsAfterFinished` or watch with `kubectl get jobs -w` and capture logs
   from one Swift gate run. Until then the SPM-over-git cause stays labelled a
   hypothesis. Agents lack cluster access (intent Constraints).
6. **Replacement comment text.** State only what the issue measured: the `-nix`
   image bakes swift, kotlin, gradle and openjdk-21, and inside the build Job
   neither `AIFACTORY_PACKED_NIX_IN_IMAGE` nor `AIFACTORY_NIX_STORE_PVC` is set.
   Label the Swift deadline cause unknown and point to the ops follow-up.

### Changes (4 files)

1. **`apps/backend/agents/gate_runner.py`**
   - **:502-503:** replace the two lines with one guarded read, and remove the
     `configured_store_pvc` name:
     `nix_store_pvc = None if nix_in_image() else (os.environ.get("AIFACTORY_NIX_STORE_PVC", "") or None)`
     (wrapped if line length requires it). With no raw value in scope, a later
     call site cannot pick it up again, which is how the #1541 exception got in.
   - **:538:** `nix_store_pvc=configured_store_pvc` becomes
     `nix_store_pvc=nix_store_pvc`. The packed call (`:536-541`) and the
     co-mount call (`:561-566`) now pass the same value.
     This also corrects the `:504-507` log. Today, with the flag on and a PVC
     set, it logs "no warm Nix store PVC" while the packed Job mounts one. After
     the change the log matches both paths. The log lines themselves do not
     change.
   - **:40-45 (timeout comment):** the budget is 600s and configurable. The
     `-nix` image bakes swift, kotlin, gradle and openjdk-21. Why Swift hits the
     deadline is unknown (SPM fetching over git is a hypothesis); link the ops
     follow-up.
   - **:491-501:** keep the #197 and #253 rationale (:483-490). Replace the
     #1541 RWX, packed-path exception and "bakes no language closures"
     paragraphs with one short paragraph:
     - both gate paths drop the PVC when `nix_in_image()` is true (the build
       path drops it on its packed path only, `build_backend.py:663-664`);
     - inside the build Job neither variable is forwarded
       (`_PASSTHROUGH_BUILD_ENV`, `build_backend.py:259`), so the guard is
       inert there today;
     - the two must be forwarded together (link the follow-up), because
       forwarding only the PVC brings back the #253 node pin.
   - **:532-535:** delete the "Warming matters most here" comment.
2. **`apps/backend/core/nix_env.py:44-46`:** keep "read by both Job paths". Add
   that the gate runs inside the build Job, which inherits neither variable, so
   on the gate path the flag is off and no store PVC is set until the follow-up
   forwards both together. `:38-41` is already true and stays.
3. **`tests/test_gate_runner_nix.py`**
   - **:341-377:** rename to
     `test_packed_path_drops_the_warm_store_when_nix_in_image`. Rewrite the
     docstring to the measured facts. Assert `seen["nix_store_pvc"] is None`.
     Keep `assert "repo_pvc" not in seen`.
   - **New, after it:** `test_packed_path_keeps_the_warm_store_when_flag_off`:
     same setup with `AIFACTORY_PACKED_NIX_IN_IMAGE` deleted, assert
     `seen["nix_store_pvc"] == "aifactory-nix-store"`. The existing flag-off
     test at `:109` covers only the co-mount call, so without this one a
     hard-coded `None` on the packed call would pass every test.
   - **:324 docstring:** "Every gate cold-fetches its closure" is also false.
     Change it to "the budget must be raisable without a release" and keep the
     reload rationale.
   - Leave `:94`, `:109` and `test_co_mount_path_still_drops_the_warm_store`
     (`:380`) as they are.
4. **`CHANGELOG.md`:** add one line under `[Unreleased]` / `### Fixed`. It says
   the packed gate path now honours `AIFACTORY_PACKED_NIX_IN_IMAGE` like the
   build path. It also says runtime does not change, because the build Job
   inherits neither variable (#1620).

### Follow-ups to file before the PR

Their numbers go into the rewritten comments.

- **(a) Forward and mount:** forward both variables together through
  `_PASSTHROUGH_BUILD_ENV`, and remove the unseeded build `/nix/store` mount
  (`job_dispatch.py:554-562`, `:912`,
  `tests/test_build_backend_kubejob.py:128,178,1412-1500`). Update the
  `AIFACTORY_NIX_STORE_PVC` row in `docs/docs/environment-reference.md` there.
  This waits on #1674, #1673, #1692 and #1677.
- **(b) Ops:** run a live Swift gate probe (Q5).

## Alternatives rejected

- **Forward both variables through `_PASSTHROUGH_BUILD_ENV` now.** It edits the
  security surface under review in #1674 and changes runtime behaviour.
  Deferred to follow-up (a).
- **Forward only one variable.** This brings back the #253 node pin, which the
  intent's constraints rule out.
- **Remove the build `/nix/store` mount here.** It changes the asserted mount
  set at `job_dispatch.py:912` and test files that #1677 edits. Deferred to (a).
- **Keep the packed exception and fix only the comments.** The exception's
  only rationale is the false "no closures baked" claim. It would also leave
  one path ignoring a flag the other honours, which fails the intent's outcome.
- **Change only `:538` and keep `configured_store_pvc`.** The diff is smaller,
  but the unguarded value stays in scope for the next caller.
- **Move the guard into `KubeJobSandbox`.** It would apply to callers not
  reviewed here (verify, TFactory) and would pull `kube_sandbox.py` into scope.
- **Edit `qa.py:155`.** It records a historical #1541 measurement that is still
  accurate as history.

## Risks

- **Default installs:** unchanged. With the flag off and no PVC set (the
  defaults, `docs/docs/environment-reference.md:240,247`), both the guarded and
  raw values are `None` on every path.
- **Runtime:** none in the live cluster. Inside the build Job `nix_in_image()`
  is always False, so the guarded and raw values are the same. The behaviour
  changes only for a gate run outside the build Job with the flag on and a PVC
  set. There the packed path stops mounting the warm store. That is the same
  speed-for-schedulability trade the build path makes (`nix_env.py:38-43`), and
  RFC-0017/#253 is unaffected: the change can only remove a PVC, never add one.
- **New false claims:** the new comment text states only what the issue
  measured and calls the Swift cause unknown. Check the wording against
  `nix_env.py:38-41` so the two do not contradict each other.
- **Conflicts:** a CHANGELOG conflict with other PRs is likely; keep both
  entries. No overlap with the files #1674, #1673, #1692 or #1677 edit.

## Verification

- `pytest tests/test_gate_runner_nix.py -q`: all pass, including the renamed
  packed test, the new flag-off packed test, and `:94` and `:109`.
- Mutation checks, applied by editing and reverted by re-editing (the coder has
  no stash or checkout):
  1. Pass the raw env read on the packed call: the renamed test fails.
  2. Hard-code `nix_store_pvc=None` on the packed call: the new flag-off packed
     test fails.
  3. Restore both and confirm green.
- `grep -rn "bakes no\|cold-fetches\|configured_store_pvc" apps/backend tests/test_gate_runner_nix.py`
  returns nothing.
- Default ruff: `ruff format --check` and `ruff check` on `apps/backend` and
  `tests`.
- Strict ratchet. fnmatch's `*` crosses `/` (`scripts/cq_ratchet.py:171`), so
  `gate_runner.py` and `nix_env.py` are in scope. Run
  `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`,
  then the `--tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini`
  variant. Expect no new findings. `tests/` is outside ratchet scope.
- `python scripts/gen_autonomy_matrix.py --check` passes. Neither file is cited
  in the matrix.
- CodeQL: no log calls are added or changed.
- Not affected: no subprocess spawn or passthrough changes, so
  `test_no_unscrubbed_spawn` and `test_agent_env_scrub` are unaffected. No
  web-server tests are touched.
- Commit hygiene: the scope must not contain `#` (use `fix(gate): ... (#1620)`).
  The `tests/test_security.py` GitCommitValidator failure while files are
  staged is environmental.
- Close #1620 when this merges, with follow-ups (a) and (b) filed.
