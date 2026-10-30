---
status: draft
issue: 1972
intent: intent/2026-10-30-1972-project-scoped-synthetic-key.md
---

# Spec: project-scoped synthetic correlation keys

## Design

**Key format.** The synthetic suffix is the service's own task id
`<project_id>:<spec_id>`:

- AIFactory: `af-<project_id>:<spec_id>`, i.e. `af-<task_id>`. `project_id` is
  the project's UUID (`routes/projects.py` `uuid4()`), the same value every
  web-server path already uses to build `task_id`.
- TFactory: `tf-<project_id>:<spec_id>`, where `project_id` is the TFactory
  workspace directory (`workspaces/<project_id>/specs/<spec_id>`, see
  `task_control._spec_dir`), i.e. `spec_dir.parent.parent.name`.

**AIFactory.**

- `services/completion.py` `correlation_key(task_id, issue_number, repo)`
  takes the task id instead of the spec id. The event builders, the
  from-issue response and `task_service.task_to_dict` pass the task id they
  already hold.
- Build process: `agents/parallel_integration.py` emits worker and progress
  events from inside run.py, which knows only the worktree directory, not the
  project UUID. Its `task_id` was already wrong (`<worktree dir>:<spec_id>`).
  The web server now puts `AIFACTORY_TASK_ID=<task_id>` into the build's env
  on both spawn paths (local `agent_service` subprocess env next to the
  marker nonce, kubejob `build_backend` `extra_env`), and
  `parallel_integration` reads it, falling back to the old derivation. This
  also fixes the worker events' `task_id`.
- TFactory handoff mirror (`pfactory/tfactory_client.py`):
  `_event_correlation_key(req, task_id)` returns `af-<task_id>` in the
  repo-unknown case. `build_ingest_payload` and `maybe_auto_handoff_tfactory`
  take a keyword `task_id: str | None = None`; both web-server callers
  (`routes/execution.py`, `services/completion_orchestration.py`) pass it.
  With no `task_id` the mirror sets no key (TFactory then uses its own
  `tf-` key): a split card, never a collision.

**TFactory.** `agents/triager.py` `_correlation_key` and
`agents/handback/send.py` `_correlation_key_for` return
`tf-<spec_dir.parent.parent.name>:<spec_id>` in the synthetic case.
Contract, handoff and `<repo>#<n>` precedence is unchanged.

**CFactory.** `card_intake._upstream_key`: for an AIFactory body without
`correlation_key`, return `af-<task_id>` (from-issue answers
`task_id = <project>:<spec>`). Fall back to `af-<spec_id>` only when the body
has no `task_id`.

**PFactory.** No change: `pf-<session_id>` is globally unique.

**Length.** UUID 36 + `:` + spec id. Web-server spec ids are
`NNN-<slug[:30]>` (`task_service.get_next_spec_id`), so AIFactory keys are
about 75 characters. TFactory project ids are workspace names. No truncation is
added; a key over 128 would need a hashing rule, which no current id reaches.

**Existing rows.** Prod check (read-only, aggregate) on 2026-10-30: 0 `af-`
work items and 0 `af-` cards; 13 `tf-` work items, newest 2026-10-23, none in
flight. No migration. Old `tf-` rows stay as history.

## Alternatives rejected

- **Hash of the spec dir path**: the build Job sees `/work/...`, the control
  plane sees the PVC path, so the two would disagree.
- **Stamp a random id into the spec dir at creation**: many creation paths
  (from-issue, project tasks, MCP, trusted plan, ideation); a missed one keeps
  the collision silently, and old specs need a fallback anyway.
- **`project_dir.name` in the build process**: it is the worktree name, not
  the project UUID, so worker events would split from the terminal event.
- **Keep `af-<spec_id>` and dedupe in CFactory**: CFactory cannot tell two
  projects apart from the key alone.

## Risks

- A task in flight across the deploy re-keys to a new card (same as #1960).
  None in flight per the prod check.
- A build started by an old web server has no `AIFACTORY_TASK_ID`; its worker
  events fall back to the worktree-based task id and split from the terminal
  event until the web server is upgraded. Deploy the web server and build
  image together (one image).
- Old CFactory with new AIFactory: from-issue always returns
  `correlation_key`, so the `_upstream_key` fallback is not reached.

## Verification

- AIFactory: `correlation_key("p1:003-x", None) != correlation_key("p2:003-x", None)`;
  the handoff mirror equals the event key for a given task id; the
  parallel-integration worker event uses `AIFACTORY_TASK_ID`; spawn env carries
  it. `test_completion_emitter.py` and the tfactory_client tests pass.
- TFactory: two spec dirs `workspaces/p1/specs/003-x` and
  `workspaces/p2/specs/003-x` give distinct keys in both functions.
- CFactory: `_upstream_key({"task_id": "p:003-x", "spec_id": "003-x"}, "aifactory") == "af-p:003-x"`,
  and it equals AIFactory's `correlation_key("p:003-x", None)` string.
- Each repo's CI is green.
