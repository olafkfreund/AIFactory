---
status: approved
issue: 1960
intent: intent/2026-10-26-1960-originator-intake.md
---

# Spec: record the originator of a task at intake

## Design

Tasks are spec directories, not database rows, so the two values go into the spec's `requirements.json` next to `provenance` and `created_at`. No migration.

**One helper**, `apps/web-server/server/services/originator.py`:

- `principal_attribution(principal) -> {"originator": str | None, "agent_identity": str | None}`
  - `originator`: the principal's `id`, else its `email`. Null when the principal is not a dict, is a service (`is_service`: legacy `API_TOKEN`, ownerless `acw_` key), or is the auth-disabled / legacy placeholder user `id == "default"`.
  - `agent_identity`: `agent:aifactory/<api_key_id>` when the principal came from an `acw_` key (same format as `ApiKey.agent_identity`, #1887), else null. Kept independent of `originator`: an ownerless service key has an agent identity and no originator.
- `issue_originator(provider, login) -> str | None`: `"<provider>:<login>"`, null when either is unknown.

**Writers** (each writes both keys, null included, so "unknown" is recorded rather than absent):

- `routes/execution.py` `create_and_run_task` (REST/CLI and MCP): from `_access`, the principal `require_project_access` resolves.
- `mcp_stdio/router.py` `proxy_create_and_run_task`: passes `_access=acw_principal(key)`. Today the proxy omits `_access`, so the handler gets the unresolved `Depends` default; passing the key's principal also gives the existing audit row a real actor.
- `routes/tasks.py` `create_task` (REST/CLI): from `request.state.user`.
- `routes/projects.py` `create_project_task` (`POST /api/projects/{id}/tasks`, the web UI/CLI create path): from `_access`.
- `routes/github.py` `import_github_issues`: `gh issue view --json ...,author` reads the author from GitHub itself, so it stores `github:<author.login>`.
- `routes/from_issue.py`: only the provider-fetch path is trusted. `_fetch_issue_via_provider` returns `originator = issue_originator(provider.provider_type, IssueData.author)`, labelled with the provider object that actually did the fetch; an unknown provider type gives null. The payload path stores `originator = null`: a caller-supplied payload can name anyone. `_write_spec` takes `originator` explicitly and writes `agent_identity` from the request principal.

**Reader**: `task_service.load_spec_metadata` copies both from `requirements.json`; `task_models.Task` gains `originator` and `agent_identity` (both `str | None`); `task_to_dict` returns `originator` and `agentIdentity`. The MCP `task_get` proxy calls the same `get_task`, so it gets both for free. Specs written before this change read as null.

## Alternatives rejected

- **Database column + migration**: the task read model is the spec directory; a column would need a sync path nobody reads.
- **`originator` as a request field**: lets any caller name any human; the intent forbids it.
- **`task_metadata.json` instead of `requirements.json`**: that file holds execution settings and is rewritten by several writers; `requirements.json` is the intake record and already holds `provenance`.
- **Prefix internal users (`human:aifactory/<id>`)**: the parent intent's final format belongs to the trailer slice; the brief asks for the raw user id or email now, and adding a prefix later is a pure formatting change.
- **`/from-plan` (trusted plan) intake**: a PFactory service handoff with no human principal; it reads as null. Carrying PFactory's actor is PFactory's half (#1961).

- **Trust the payload's `author.login` / `user.login`** (first revision): the payload is caller-supplied, so a forged author would be stored as the human who asked. Rejected in review (2026-10-26); the payload path stores null.
- **Label from the declared `repo_ref`, defaulting to `github`**: a default is a guess. The label comes from the provider that fetched the issue, null if unknown.

## Risks

- `_access` now passed by the MCP proxy changes the audit row actor for `mcp.task.create_and_run` from an unresolved object to the key's principal. Intended.
- GitLab/Azure `IssueData.author` may be a display name rather than a login. It is still the provider's own author field, prefixed with that provider.
- OpenAPI schema for `Task` gains two nullable fields; regenerate if the repo checks it.

## Verification

New `tests/test_originator_intake.py`:

- MCP proxy create-and-run with an `acw_` key owned by user `u1` records `originator == "u1"` and `agent_identity == "agent:aifactory/<key_id>"`.
- From-issue with a provider-fetched issue whose author is `octocat` records `originator == "github:octocat"`; a GitLab provider gives `gitlab:octocat`; a provider without a type gives null.
- From-issue with a payload naming an author (`author.login`, `user.login`, string, or a forged `originator`) records `originator is None`.
- `POST /api/projects/{id}/tasks` records the principal; `import_github_issues` records `github:<login>`, null without an author.
- Legacy token (service principal), auth-disabled principal, and an issue without author record `originator is None`.

Out of scope, reported separately: `POST /api/tasks/from-issue` has no project-access dependency (router, route or include).
- `task_to_dict` of a spec carrying both returns `originator` and `agentIdentity`; an old spec returns null for both.

Plus the existing `test_mcp_stdio_create_and_run.py`, `test_from_issue_route.py`, `test_create_and_run_parallel.py` stay green; ruff and the mypy ratchet pass on touched files.
