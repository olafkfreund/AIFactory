---
status: draft
issue: 1619
intent: intent/2026-09-29-1619-cockpit-truthful-builds.md
---

# Spec: the cockpit tells the truth about a running build

## What the measurements settled

| Question | Measured answer |
| --- | --- |
| Does a source of truth for live kubejobs already exist? | **Yes.** `job_state_store.get_active_kubejobs()` returns the running k8s-Job rows, and `reap_kubejob_builds` already asks the Kubernetes API whether each Job still exists (#1606). Nothing new needs to watch Kubernetes. |
| Can `is_running` read it without a round-trip per call? | **Yes.** `kubejob_reconcile_loop` (`agent_kubejob.py:704`) ticks every **15s** and already calls `get_active_kubejobs()`. A set maintained there is at most 15s stale — far fresher than the 30s cadence of the card's own poll. |
| Is a job row's key the task id? | **Yes.** `row["job_id"]` is `project_id:spec_id` — `_redrive_kubejob_review` partitions it on `":"`. No mapping layer is needed. |
| Why does the coder's spec read fail? | **Not the reason #1617 first gave.** The per-subtask worktree is created at `<task-worktree>/.aifactory/worktrees/tasks/<spec>__w__<subtask>`, i.e. the template is applied relative to the task worktree, which is already `/work/.aifactory/worktrees/tasks/<spec>`. The agent's cwd is two levels below the repo root, and the sandbox root is that doubled path. |
| Are the timestamps naive? | **Yes.** `agent_service.py:1200` writes `datetime.now().isoformat()`; `formatRelativeTime` (`lib/utils.ts:45`) parses with `new Date(...)`, which reads an offset-less string as local time. Pod UTC + operator BST = every age an hour old. |

## Design

### 1. `is_running` consults the kubejob set as well as the dict

`AgentService` gains `self._active_kubejob_task_ids: set[str]`, replaced wholesale
at the end of each `reconcile_kubejob_builds()` tick from the rows it has just
polled, minus any that went terminal during that tick.

```python
def is_running(self, task_id: str) -> bool:
    return task_id in self.running_tasks or task_id in self._active_kubejob_task_ids
```

Replaced wholesale, not mutated incrementally: a set that is only added to drifts
into claiming dead builds are alive, which the intent names as the one failure
worse than the current one. The set starts empty, so the worst case after a
web-server restart is the present behaviour until the first tick (≤15s).

### 2. `recover` refuses while a Job for that task is live

`POST /{task_id}/recover` gains `force: bool = False`. Without it, the route
answers **409** when `is_running(task_id)` is true, naming the live Job. With it,
the current behaviour is unchanged. This is deliberately expressed in terms of
the *fixed* `is_running`, so the refusal and the card's verdict cannot disagree.

### 3. Timestamps are written timezone-aware

`datetime.now()` becomes `datetime.now(UTC)` at `agent_service.py:1200` and at
every sibling writer of a task/subtask timestamp found by the same sweep. Reads
stay tolerant: a stored value without an offset is still parsed, treated as UTC,
so rows written before this change do not jump an hour in the other direction.

### 4. The coder's spec path

The measurement above rules out the fix #1617 first proposed. Two candidates
remain, and **the choice depends on one unmeasured fact**: whether the sandbox
refuses an absolute read outside its declared root.

- **If the nesting is accidental** — compose the subtask worktree from the repo
  root, so cwd becomes `/work/.aifactory/worktrees/tasks/<spec>__w__<subtask>`
  and repo-root-relative paths resolve. Root-cause fix, but it moves a path that
  other machinery (packing, the `plan-commands` allowlist, log collection) may
  already depend on.
- **If the nesting is deliberate** — copy `spec.md` and `implementation_plan.json`
  into the subtask worktree at dispatch, so they are inside the sandbox root by
  construction and no path crosses a boundary.

**This spec proposes the copy.** It is correct under either answer, it does not
move a path other machinery may depend on, and it removes the sandbox question
entirely rather than betting on it. The nesting itself is then a separate
question, filed as such rather than fixed blind.

Whichever lands, the read failing must stop being silent: when the coding phase
cannot read its spec or plan, it logs a warning naming both paths, so a degraded
run is distinguishable from a healthy one in the log.

## Alternatives rejected

- **Query `job_state_store` inside `is_running`.** Simpler to read, but puts a
  database round-trip behind a per-card 30-second poll, and makes a sync method
  async — a change that reaches every caller.
- **Ask the Kubernetes API in `is_running`.** Freshest answer, worst placement:
  an API call on a UI poll, and a new failure mode where the cockpit's verdict
  depends on the control plane's Kubernetes credentials.
- **Fix it in the frontend** (e.g. suppress "Stuck" for kubejob tasks). Hides the
  symptom on one client and leaves the endpoint lying to every other caller,
  including `recover`.
- **Delete the Job in `recover`.** Makes the button "correct" rather than
  refusing, but a button offered by a false verdict should not be given a bigger
  hammer; and killing a healthy build is exactly the outcome to prevent.
- **Widening the sandbox root to reach the specs directory.** Enlarges the
  security boundary to solve a path-composition problem.

## Risks

- **A stale set claims a dead build is alive.** Bounded by wholesale replacement
  each tick and by the reaper that already marks vanished Jobs terminal. Worst
  case is ≤15s of "still running" after a Job dies — the direction the intent
  says matters least, and strictly better than 100% wrong today.
- **`recover` refusing is a behaviour change on an existing endpoint.** Anything
  automating recovery against a live build starts getting 409s. That is the
  point, but it is a contract change and belongs in the changelog.
- **The task looks inert even once it stops saying "Stuck"**, because progress
  still cannot flow (#1618). Accepted and stated in the intent; the card will
  read "running, no detail" rather than "stuck, press Recover".
- **Copying the spec into the subtask worktree duplicates it**, so a run could
  read a stale copy if the spec changed mid-run. Specs are written at dispatch
  and not edited during a run, so the window is empty in practice — but it is a
  new copy where there was one file.
- **The timezone change is fleet-shaped.** Other services write task timestamps;
  this change covers AIFactory's writers only, and a sweep of the others is
  follow-up rather than scope creep.

## Verification

Deterministic:

1. `is_running` returns True for a task id present only in the kubejob set, True
   for one only in `running_tasks`, and False for neither. **Mutation:** drop the
   set from the expression and the first case must fail.
2. A reconcile tick whose rows no longer contain a task id removes it from the
   set — the anti-drift property, tested by two ticks with different rows.
3. `recover` answers 409 while `is_running` is true, and proceeds with
   `force: true`. **Mutation:** make `is_running` return False and the 409 test
   must fail.
4. A timestamp written by the changed path parses as timezone-aware UTC, and a
   naive stored value still parses (as UTC) rather than raising.
5. The coding phase logs a warning naming both paths when the spec read fails.

Live, in-cluster — the standard this defect demands, because unit-level reasoning
about `running_tasks` is exactly what looked correct while the cockpit lied:

6. With a real kubejob build running, `GET /api/tasks/<id>/running` returns
   `is_running: true`, and the cockpit card shows no "Stuck" badge. Today the
   same call returns false against a Job that has been `Running` for 49 minutes.
7. `POST /recover` on that live task answers 409; the Job is still `Running`
   afterwards.
8. When the Job ends, the set drops the task within one tick and `/running`
   returns false again.
9. A coding phase in that build reads `spec.md` and `implementation_plan.json`
   successfully — measured as zero "File does not exist" failures for those two
   paths, against 26 failures in one window today.

Gates: ruff, ruff format over the CI path list, `ratchet_lint.py --base
origin/dev` with its `--package` flags, the frontend's typecheck and vitest if
any frontend file changes, and the full backend suite.
