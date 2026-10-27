---
status: approved
issue: 1925
spec: spec/2026-10-27-1925-auto-fix-review-gate.md
---

# Plan: manual auto-fix start must not force-run an unreviewed plan

Approved decisions (from the spec):

- `start_auto_fix` passes `force=True` only when THIS call created the spec
  dir. A reused spec dir gets `force=False`; `should_pass_force` and run.py's
  pre-flight decide (no review required → `--force`; valid approval → build;
  none/stale → pause in human_review). No approval logic in the web-server.
- All routes in `routes/auto_fix.py` get `Depends(require_project_access(role))`:
  `viewer` for GET queue; `member` for GET/PUT config (holds botToken),
  check-new and `{N}/start`. Path placeholder `{projectId}` → `{project_id}`
  (the dependency reads `project_id`); URLs unchanged.
- `requireHumanApproval` (auto-fix config) → `_write_spec_dir` writes
  `requirements.json` `metadata.requireReviewBeforeCoding = true`, merged with
  delegation metadata. Default off → poll path unchanged.
- Do not edit `agent_worktree_sync.py`, `agents/coder.py`, `review/state.py`,
  `cli/build_commands.py` (other lanes).

Tests live in repo-root `tests/` (the spec said `apps/web-server/tests`; that
dir does not exist). Pattern for sys.path: `tests/test_auto_fix_pull_on_poll.py`.

## Steps

1. `apps/web-server/server/services/auto_fix_service.py` `start_auto_fix`
   (~l.457–565): set `created = spec_id is None` before the create branch; pass
   `force=created` to `start_task_execution`, replacing the comment with one
   line on why a reused spec is not forced (#1925). New
   `tests/test_auto_fix_review_gate.py`: patch `server.project_registry.load_projects`,
   `get_agent_service` (AsyncMock `start_task_execution`), `_provider_for`,
   `server.websockets.events.broadcast_event`; (a) existing
   `.aifactory/specs/001-gh7-x/` → called with `force=False`; (b) no spec dir →
   spec created, `force=True`. Also (c) `should_pass_force(spec_dir, False)` is
   False for a spec whose requirements.json has `requireReviewBeforeCoding: true`
   and True without it (documents what run.py receives).
   → verify: `pytest tests/test_auto_fix_review_gate.py -q` (fails on (a) without the fix)
   Traps: none.
2. `apps/web-server/server/routes/auto_fix.py`: import `Depends` and
   `require_project_access` (`from .project_authz import require_project_access`);
   rename `projectId` → `project_id` in all 5 paths and handlers; add
   `_access: dict = Depends(require_project_access("<role>"))` per the decisions.
   Update the module docstring paths. Test in the same file: mount
   `auto_fix.router` under `/api/projects` in a bare FastAPI app, monkeypatch
   `server.routes.project_authz._auth_disabled` → False and
   `server.routes.project_authz.authorize_project_for_user` with an async fake
   that raises `HTTPException(403)` (records minimum_role), override `get_db`;
   POST `/api/projects/p1/auto-fix/7/start` → 403, service not called, role
   recorded `member`. → verify: same pytest.
   Traps: `authorize_project_for_user` is looked up as a module global inside
   `ProjectAccessChecker._authorize`, so patch it on `server.routes.project_authz`.
3. `auto_fix_service.py`: `_write_spec_dir` gains kw-only
   `require_review: bool = False`; when True set
   `requirements.setdefault("metadata", {})["requireReviewBeforeCoding"] = True`
   (restructure the delegation block to use the same setdefault). Docstring arg line.
   `start_auto_fix` passes `require_review=bool((get_config(project_id) or {}).get("requireHumanApproval"))`.
   Test: `_write_spec_dir(..., require_review=True)` → metadata flag true;
   default → no `metadata` key; with both flags → both keys.
   → verify: `pytest tests/test_auto_fix_review_gate.py tests/test_delegation_v1_gaps.py tests/test_delegation_v1b.py tests/test_auto_fix_pull_on_poll.py -q`
   Traps: `test_write_spec_dir_omits_delegation_when_default_off` may assert no metadata — keep it passing.

   **Deviation (Opus review):** writing the flag to \`requirements.json\` alone
   does nothing for a fresh spec: \`force=True\` short-circuits
   \`should_pass_force\`, so \`sync_require_review_metadata\` never copies it to
   \`task_metadata.json\`, which is what the coder's gate reads. Fixed at the
   root in \`services/task_phase.py\` \`should_pass_force\`: it now always runs
   the sync and then ORs in \`force\` (\`not sync(...) or force\`), so every
   forced start keeps the flag. Same return value as before.
   Test: \`_write_spec_dir(require_review=True)\` then \`should_pass_force(d, True)\`
   leaves \`task_metadata.json\` \`requireReviewBeforeCoding: true\` (fails
   without the reorder). The other \`force=True\` callers were checked:
   \`delegation_runner\` is planner-only, and \`plan_approval\` runs after an
   approval. Neither loses anything the gate needs.

   **Deviation (lead's Opus review of the PR):**
   - PUT config needs \`admin\` (matches PATCH project settings), not \`member\`.
   - GET config returns \`botToken\` as \`********\`. PUT keeps the stored
     token when the field is the mask or omitted (payload default \`None\`);
     any other value replaces it, and \`""\` still clears it.
   - Dependencies use \`Annotated[dict, Depends(...)]\` aliases
     (\`Viewer\`/\`Member\`/\`Admin\`), as \`routes/from_issue.py\` does, because
     the strict ratchet counts B008 on \`= Depends(...)\` defaults.
   - Tests cover GET masking, PUT keeping the token for the mask or an
     omitted field, PUT replacing it, and PUT denied below admin.
   - Not done: a per-issue start lock (the lead marked it optional; the
     existing "already running" ValueError covers the same-task double start).

## Tests

`pytest tests/test_auto_fix_review_gate.py tests/test_delegation_v1_gaps.py tests/test_delegation_v1b.py tests/test_auto_fix_pull_on_poll.py -q` all pass;
`ruff check` + `ruff format --check` on touched files.

## Rollback

Revert the PR. No data or schema change.
