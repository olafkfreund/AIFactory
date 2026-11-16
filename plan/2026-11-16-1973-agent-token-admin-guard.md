---
status: draft
issue: 1973
spec: spec/2026-11-16-1973-agent-token-admin-guard.md
coders:
  # all steps by the lead: one small module change plus a manifest and docs edit
  1: lead
  2: lead
  3: lead
reviewers:
  - codex   # independent read of the diff against this plan
---

# Plan: refuse an admin-scoped GitHub token for agent children

Carries the approved spec (`spec/2026-11-16-1973-agent-token-admin-guard.md`).

## Approved decisions

- `apps/web-server/server/utils/subprocess_env.py`: `ADMIN_SCOPES` = {delete_repo, admin:org, admin:org_hook, admin:repo_hook, admin:public_key, admin:gpg_key, admin:ssh_signing_key, admin:enterprise, site_admin}; `admin_scopes(token)` returns `None` for `ghs_`/`github_pat_`/`ghu_` tokens, otherwise one `GET {GITHUB_API_URL or https://api.github.com}/user` (5 s) reading `X-OAuth-Scopes`, cached per sha256(token) for 3600 s; transport error, non-200 or an absent header = `UNKNOWN`.
- `AIFACTORY_AGENT_TOKEN_ADMIN_POLICY` = `warn` (default) or `refuse`; anything else = `refuse`. A finding or `UNKNOWN` under `refuse`: log ERROR once per token hash (scope names only, never the token) and `runner_github_env` returns `{}`. A finding under `warn`: log WARNING once, token passes. Applies to the agent-token branch and the fallback branch; not to the multi-installation branch.
- `dotenv_key_blocked(key)` is also true for GitHub token names when the policy is `refuse`.
- Out of scope, unchanged: tenant-saved project `GITHUB_TOKEN`, the control plane's own git/gh calls, and the runners' own `load_dotenv()` of an operator-owned `apps/backend/.env` inside the image (`cli/utils.py:48`, `runners/*`): the cutover checklist asks the operator to confirm no GitHub token is in that file.
- `scripts/github-app-manifest.json` permissions: `contents: write, pull_requests: write, issues: write, checks: read, metadata: read`; `--workflows` still adds `workflows: write`.
- Docs: `docs/docs/concepts/github-app.md` permission list, policy variable, cutover order (all three App keys together; remove every PAT source: `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, MCP `tokenEnv` alias, `gh` hosts.yml; leave `AIFACTORY_AGENT_GITHUB_TOKEN` unset so children use the App token).
- gitops side (factory-gitops PR, executed from the Factory plan step 5): three optional `secretKeyRef` App env entries from `factory-secrets`, `AIFACTORY_AGENT_TOKEN_ADMIN_POLICY: "warn"`, `docs/aifactory-github-app-cutover.md`; `GITHUB_TOKEN` line stays until cutover.
- Issue handling: #1973, #1700, #1701 get a "Decision (owner delegation 2026-11-16)" comment and stay open on owner-only steps.

## Steps

1. `apps/web-server/server/utils/subprocess_env.py`: add the guard (`ADMIN_SCOPES`, `admin_scopes`, policy reader, `_guard(token) -> bool`), call it in `runner_github_env` on the agent-token and fallback branches, extend `dotenv_key_blocked`, export nothing new except what tests need. → verify: `pytest tests/test_github_env.py tests/test_child_process_env.py tests/test_agent_env_scrub.py apps/web-server/tests/test_github_app.py` still pass.
   Traps: ruff and `standards/ruff.toml` disagree on sorting `as` aliases inside parenthesised imports (use plain names); never put a token in a log line or exception; `subprocess_env` is imported before `github_app` (keep the lazy imports).
2. New `tests/test_agent_token_guard.py` (stub `_fetch_scopes`, no network). Cases: (a) classic token with `delete_repo` + `warn`: every forwarded name (`GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, the MCP alias) still carries it and one WARNING is logged without the token; (b) same + `refuse`: `{}` and one ERROR; (c) `UNKNOWN` + `refuse` -> `{}`, `UNKNOWN` + `warn` -> passes; (d) `ghs_`, `github_pat_`, `ghu_` tokens: fetch not called, pass; (e) unknown policy string behaves as `refuse`; (f) repeated calls fetch once (cache); (g) the agent-token branch is guarded too; (h) `dotenv_key_blocked("GITHUB_TOKEN")` true under `refuse`, false under `warn` without an agent token; (i) multi-installation branch untouched. → verify: `pytest tests/test_agent_token_guard.py` fails on `origin/dev` code and passes after step 1.
3. `scripts/github-app-manifest.json`, `tests/test_github_app_setup.py::test_manifest_permissions`, `docs/docs/concepts/github-app.md`. → verify: `pytest tests/test_github_app_setup.py`; `ruff check . && ruff format --check .`; `npm --prefix docs run build` if `docs/node_modules` exists, else report that the docs build was not run; `pre-commit run --all-files`.
   Traps: do not hand-merge `docs/docs/compliance/autonomy-matrix.md` (generated; regenerate on conflict).

## Tests

`pytest tests/test_agent_token_guard.py tests/test_github_env.py tests/test_child_process_env.py tests/test_agent_env_scrub.py tests/test_github_app_setup.py apps/web-server/tests/test_github_app.py`; ruff check/format; pre-commit; docs build as in step 3.

## Rollback

Revert the commit. With the default `warn` policy the behaviour for the live pod differs only by one log line.
