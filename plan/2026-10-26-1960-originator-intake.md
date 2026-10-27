---
status: approved
issue: 1960
spec: spec/2026-10-26-1960-originator-intake.md
coders:
  1: codex   # wave rule: lanes implement through codex-implement, fall back to self
  2: codex
  3: codex
---

# Plan: record the originator of a task at intake

Approved decisions (from the spec):

- Store `originator` and `agent_identity` as top-level keys of the spec's `requirements.json` at intake. Write both on every covered path, `null` when unknown. No DB column, no migration.
- `originator` = principal `id`, else `email`; null for a non-dict principal, `is_service` principals, and `id == "default"` (auth-disabled and legacy-token placeholder). From-issue: `"<provider>:<login>"` only on the provider-fetch path, labelled with the fetching provider's `provider_type`; null when the author or provider type is unknown. The payload path always stores null (revision 2026-10-26, review of #1897).
- Also covered (revision): `POST /api/projects/{id}/tasks` from `_access`, and `import_github_issues` as `github:<author.login>` from `gh issue view`.
- `agent_identity` = `agent:aifactory/<api_key_id>` when the principal has `api_key_id` (same format as `ApiKey.agent_identity`, #1887), else null. Independent of `originator`.
- No request field sets either value. No git trailers. No change to auth, scopes or access checks.
- Exposed on task GET as `originator` and `agentIdentity`.
- `/from-plan` is out of scope (reads as null).

## Steps

1. New `apps/web-server/server/services/originator.py`: `principal_attribution(principal) -> dict[str, str | None]` and `issue_originator(provider, login) -> str | None`, as above. Wire writers:
   - `routes/execution.py` `create_and_run_task` (~line 1228, where `requirements` is built before it is written): `requirements.update(principal_attribution(_access if isinstance(_access, dict) else None))`.
   - `mcp_stdio/router.py:268` `proxy_create_and_run_task`: pass `_access=acw_principal(key)` (import from `.auth`).
   - `routes/tasks.py` `create_task` (~line 287, the `requirements` dict): `principal_attribution(getattr(request.state, "user", None) if request is not None else None)`.
   → verify by `pytest tests/test_mcp_stdio_create_and_run.py tests/test_create_and_run_parallel.py -q`
   Traps: `test_mcp_stdio_create_and_run.py`'s fake `create_and_run_task` has a fixed signature; give it `**_kw`. Keep `PLR0912`/complexity budgets (put logic in the helper, not the handler). `from __future__ import annotations` modules cannot use `Annotated` closure deps.
2. `routes/from_issue.py`: `_fetch_issue_via_provider` (line ~148) adds `"originator": issue_originator(provider.provider_type.value, issue.author)` (null when the type is unknown); `_normalize_issue` is unchanged; `_write_spec` (line 157) takes `principal=None` and `originator=None`, writes `originator` as given and `agent_identity` from `principal_attribution(principal)`; `create_from_issue` sets `originator` only from the fetched dict (payload path: null) and passes `getattr(raw_request.state, "user", None)` when `raw_request` is set.
   → verify by `pytest tests/test_from_issue_route.py -q`
   Traps: `_normalize_issue(payload, issue_number)` is called with and without the number (#1418); keep that. Do not change the dedup path.
3. Reader: `routes/task_service.py` `load_spec_metadata` (defaults dict ~line 513 and the requirements block ~line 538) copies `originator` and `agent_identity`; `spec_to_task` (~line 1043) passes them; `routes/task_models.py` `Task` (after `repo`, line 154) adds both as `str | None = Field(None, ...)`; `task_to_dict` (~line 1316) adds `"originator"` and `"agentIdentity"`. Add `tests/test_originator_intake.py` with the cases below. Regenerate the OpenAPI spec if the repo has a check for it.
   → verify by `pytest tests/test_originator_intake.py -q`

4. (Revision) `routes/projects.py` `create_project_task` (~line 1288, `requirements` dict): `**principal_attribution(_access if dict else None)`. `routes/github.py` `import_github_issues` `_import_one` (~line 1840, `requirements` dict): `"originator": issue_originator("github", (issue_data.get("author") or {}).get("login"))`. Tests for both in `tests/test_originator_intake.py`; forged-payload test in `tests/test_from_issue_route.py`.
   → verify by `pytest tests/test_originator_intake.py tests/test_from_issue_route.py tests/test_pfactory_import.py tests/test_solo_mode.py -q`

## Tests

`tests/test_originator_intake.py`:
- MCP proxy create-and-run, `acw_` key with `user_id="u1"`, `key_id="k1"` → `requirements.json` has `originator == "u1"`, `agent_identity == "agent:aifactory/k1"`.
- From-issue, provider (type GitHub) returns an issue authored by `octocat` → `originator == "github:octocat"`; GitLab → `gitlab:octocat`; no type → null.
- From-issue payload naming an author, or a forged `originator` → `originator is None`.
- `create_project_task` records the principal; `import_github_issues` records `github:<login>`.
- Unknown stays null: legacy-token principal, `DISABLE_AUTH` principal, issue with no author → `originator is None`.
- `get_task`/`task_to_dict` on a spec with both → returns `originator` and `agentIdentity`; on a spec without them → both null.

Then `ruff check` and `ruff format --check` on touched files, `python scripts/ratchet_lint.py` if present, and the existing tests named in steps 1-2.

## Rollback

Revert the PR. Specs written meanwhile keep two extra JSON keys that nothing else reads.
