---
status: approved
issue: 1674
author: olafkfreund
---

# Intent: six host credentials still reach the coding agent's environment

## Problem

The agent CLI inherits the whole host environment. The only filter is
`get_agent_env_blanks()` (`apps/backend/core/auth.py:167`). It blanks a name
only when `is_denied_env_key()` (`auth.py:162`) matches it, either in
`_AGENT_ENV_DENY_EXACT` (`auth.py:118`) or in `_AGENT_ENV_DENY_PATTERN`
(`auth.py:156`, `SECRET|PASSWORD|PRIVATE_KEY|CREDENTIAL|(?:^|_)KMS|PASSPHRASE|TRUSTED_PLAN_KEY`).
The blanks are merged at `core/client.py:614` and `core/simple_client.py:89`.

The #1668 hotfix closed the plan key and `AIFACTORY_TOKEN`. Six names that the
issue found in the production pod still match neither list, and a regex check
on this checkout confirms it:

| Name in the pod | Read by | Reaches the agent |
|---|---|---|
| `APP_CFACTORY_READ_KEY` (pydantic `CFACTORY_READ_KEY`, `env_prefix="APP_"`) | web server only (`routes/search.py:38`, `:64`) | in-pod |
| `CONTEXT7_KEY` | nothing; the context7 MCP reads `CONTEXT7_API_KEY` and is given no explicit `env` (`client.py:855-859`) | in-pod |
| `RAPIDAPI_KEY` | nothing | in-pod |
| `LANGCHAIN_API_KEY` | nothing | in-pod |
| `OPENAI_COMPATIBLE_API_KEY` | backend process (`phase_config.py:995`, `:1049`), `provider_health.py:20` | in-pod and kubejob |
| `S3_ACCESS_KEY` | `artifact_store.py:145`, `access_review_evidence_cron.py:107` | in-pod and kubejob |

A prompt-injected `env` or `printenv` in the agent's Bash tool can read them.
That exposes the cockpit search key, three third-party API keys, billable
model access through the OpenAI-compatible provider, and the S3 access key ID.
The S3 secret half is already blanked, so the access key ID has the lowest
impact of the six.

#1668 noted that kubejob build Jobs get an explicit env list without its two
variables. That holds for those two, but not for two of the six here:
`build_backend._PASSTHROUGH_BUILD_ENV` forwards `OPENAI_COMPATIBLE_API_KEY`
(`build_backend.py:295`) and `S3_ACCESS_KEY` (`:318`) into the Job, and run.py
there uses the same scrub. The gate Job is not affected, because
`gate_runner._STORE_ENV_VARS` goes only to the `unpack-workspace` initContainer
(`core/kube_sandbox.py:189-197`), where no agent runs.

Since #1691 the same `is_denied_env_key` also drives `core/child_env.py`, which
every server-spawned child goes through, including the run.py runner
(`runner=True`, `subprocess_env.py:73`). A name added to the deny list is
therefore removed from the runner as well, unless it is in `RUNNER_KEEP`
(`subprocess_env.py:32`). `OPENAI_COMPATIBLE_API_KEY` is in `RUNNER_KEEP`.
Neither S3 name is, and `S3_SECRET_KEY` is already denied, so the local runner
has had no S3 secret since #1691.

## Proposed outcome

- None of the six names, with a value set, is visible to an agent started
  through `create_client` or `create_simple_client`, in the pod or in a kubejob
  build Job.
- The processes that legitimately read them keep them. The web server keeps
  its CFactory key, provider health check and S3 cron. The runner keeps
  `OPENAI_COMPATIBLE_API_KEY`, so OpenAI-compatible phases still work.
- `tests/test_agent_env_scrub.py` covers all six names, and a test proves that
  the runner env still carries `OPENAI_COMPATIBLE_API_KEY`.

## Affected users and systems

- `apps/backend/core/auth.py` (deny list) and its two callers,
  `core/client.py` and `core/simple_client.py`.
- `apps/backend/core/child_env.py` and `apps/web-server/server/utils/subprocess_env.py`
  (`RUNNER_KEEP`), because they share the deny list.
- Kubejob build Jobs (`build_backend.py` passthrough), by way of run.py's scrub.
- Operators who run agent builds in the aifactory pod or as kubejob builds, and
  operators whose MCP servers read credentials from the agent's environment.

## Constraints

- Only add to what is scrubbed. Do not loosen any existing deny entry, the
  `AIFACTORY_ALLOW_API_KEY` opt-in for `ANTHROPIC_API_KEY`, `_STRIP_VARS`, or
  `_AGENT_ENV_KEEP` (OAuth token, `ANTHROPIC_BASE_URL`, `SDK_ENV_VARS`). Keep
  names always win over a deny.
- Do not break operator MCP credentials that the agent CLI passes on to MCP
  servers, which #1680 deliberately left alone.
- The in-pod path and the kubejob path must behave the same.
- The web-server process's own environment is not changed.
- Out of scope: Codex and Antigravity agents (#1692), `GITHUB_TOKEN` (#1688),
  and narrowing `build_backend.dispatch` (#1669, #1670, #1677). This change must
  not claim to fix #1692.
- No new state. The scrub stays per process and is read from `os.environ` on
  each call.

## Open questions

1. **Exact names or a pattern?** Exact names cannot break anything but miss
   future keys. A pattern such as `_API_KEY$|_ACCESS_KEY$|_READ_KEY$|^CONTEXT7_`
   catches new keys but also removes them from the runner and from operator MCP
   servers.
2. **S3 in the runner.** Deny `S3_ACCESS_KEY` everywhere, so the runner has
   neither S3 name, as it already lacks the secret. Or add both S3 names to
   `RUNNER_KEEP` and blank them only for the agent, which fixes the #1691
   regression in this task. The second option needs an agent-only scrub.
3. **Context7.** Only scrub the unused `CONTEXT7_KEY`? Or also pass the key as
   `CONTEXT7_API_KEY` in `mcp_servers["context7"]` so Context7 runs
   authenticated?
4. **Allowlist (#363, closed).** Keep the move from denylist to allowlist out of
   scope and file a new issue for it, or reopen #363?
5. **Kubejob passthrough.** Rely on run.py's scrub inside the Job, or file a
   separate issue to stop forwarding `OPENAI_COMPATIBLE_API_KEY` and the S3
   names into build Jobs?
