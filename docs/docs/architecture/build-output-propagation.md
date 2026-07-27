---
title: Build output propagation
sidebar_label: Build output propagation
---

# Build output propagation — what survives a build, and why

**Read this before writing anything to disk during a build.** Four separate bugs
have been caused by the same wrong assumption, and three consecutive attempted
fixes for the fourth were wrong for the same reason. This page exists so the
fifth does not happen.

## The rule

> **A build Job's filesystem is write-once-and-discard. Code escapes via
> `git push`. Everything else escapes only if something explicitly pushes it.**

If you write a file during a build and expect the control plane, the cockpit, or
a later build to read it, that file needs a push-back. There is no automatic
propagation, and a green build proves nothing about whether your file survived.

## Why: two execution paths, and only one has a durable `/work`

`apps/backend/core/job_dispatch.py` chooses the Job's `/work` volume:

```python
work_co_mount = bool(spec.data_pvc and spec.worktree_subpath)
if work_co_mount:
    # data PVC, co-mounted by subPath  ->  /work is DURABLE
elif spec.workspace_uri:
    # RFC-0017 #190 packed path        ->  /work is an emptyDir, EPHEMERAL
```

| | co-mount path | packed path |
|---|---|---|
| selected when | `data_pvc` **and** `worktree_subpath` set | `WORKSPACE_URI` set |
| `/work` is | the data PVC | an **emptyDir** |
| workspace arrives by | already there | unpacked from object storage at start |
| files written during the build | survive | **destroyed with the pod** |

The fleet currently runs the **packed path**. You can confirm which path a Job
took in one command — do this rather than reasoning about it:

```bash
kubectl get job <job> -n factory \
  -o jsonpath='{range .spec.template.spec.volumes[?(@.name=="work")]}{.persistentVolumeClaim.claimName}{" emptyDir="}{.emptyDir}{"\n"}{end}'
```

`emptyDir={}` means nothing you write to disk will survive.

:::warning
`build_backend.py`'s module docstring describes `/work` as a co-mounted PVC
subPath. That describes the co-mount path only. It is **not** what the fleet runs
today, and taking it at face value is what produced three wrong fixes for #1030.
:::

## The push-back pattern

Every artefact that has to cross the boundary is pushed by the Job and fetched by
the control plane, both keyed off `spec_id` alone so no URI needs threading.

| Artefact | Producer (`cli/main.py`) | Consumer (`services/completion.py`) | Added after |
|---|---|---|---|
| built branch | `maybe_push_workspace_branch` | `git` / PR endgame | #190 |
| `token_usage.json` | `maybe_push_usage` | `maybe_fetch_usage` | #190 |
| `task_logs.json` | `maybe_push_task_logs` | `maybe_fetch_task_logs` | Factory#218 |
| `implementation_plan.json` | `maybe_push_plan` | `maybe_fetch_plan` | #852 |
| `memory/` | `maybe_push_memory` | `maybe_fetch_memory` | #1038 |

Every row was added **after** the same class of bug was hit again. The pattern is
in `apps/backend/core/workspace_fetch.py`; copy the nearest neighbour.

Notes that matter:

- **Directories travel as `tar.gz`.** `memory/` is the only tree so far; extract
  with `filter="data"` so a crafted archive cannot escape the destination.
- **Fetches must MERGE, never replace.** `memory/` accumulates across sessions
  and — once pooled — across specs. A replacing fetch discards exactly what the
  chain exists to keep.
- **Both halves are best-effort and never raise.** A build that produced working
  code must not fail because an artefact could not be filed.

## How to verify — and how not to

The three failed fixes for #1030 each had passing unit tests, a passing mutation
check, and a plausible reading of the code. None of that detects a file written
to the wrong filesystem.

**What actually works:**

1. Run a real build.
2. `find` the expected path on the PVC and count files.
3. Compare against a pre-recorded baseline.

```bash
kubectl exec -n factory deploy/aifactory -c aifactory -- \
  sh -c 'find /home/nonroot/.aifactory/workspaces/<project>/.aifactory/memory -type f | wc -l'
```

**Two traps that produced false passes here:**

- **A directory existing is not data.** `mkdir(exist_ok=True)` in a helper
  creates the directory during an unrelated call. Count files, never `test -d`.
- **A populated directory is not proof your code populated it.** Files observed
  after a build had been written by an earlier run. Record the count *before*,
  and attribute the delta.

## Checklist for a new build artefact

- [ ] Does the control plane, cockpit or a later build need to read it? If yes it
      needs a push-back — writing it is not enough
- [ ] Producer in `cli/main.py`, keyed by `spec_id`; consumer in
      `completion.py` alongside the others
- [ ] Directory? tar it, and extract with `filter="data"`
- [ ] Fetch merges rather than replaces
- [ ] Both halves best-effort, never raising
- [ ] Verified on a **real build** against a **pre-recorded baseline**, not by a
      passing test suite
