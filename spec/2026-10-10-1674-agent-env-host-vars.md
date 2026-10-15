---
status: approved
issue: 1674
intent: intent/2026-10-10-1674-agent-env-host-vars.md
---

# Spec: six host credentials still reach the coding agent's environment

Checked against branch HEAD `445e7dc7`.

## Design

Add six exact names to the existing deny list. No other code changes.

### 1. Deny list: `apps/backend/core/auth.py:118-152`

Add these names to `_AGENT_ENV_DENY_EXACT`, as one group with a comment that cites #1674:

```python
    # Host keys that matched neither list (#1674). Their readers keep them:
    # web server (own env), runner via RUNNER_KEEP, kubejob run.py via Job spec.
    "APP_CFACTORY_READ_KEY",
    "CONTEXT7_KEY",
    "RAPIDAPI_KEY",
    "LANGCHAIN_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "S3_ACCESS_KEY",
```

The following stay as they are: `_AGENT_ENV_DENY_PATTERN` (`:156`),
`is_denied_env_key` (`:162`), `_AGENT_ENV_KEEP`, `RUNNER_KEEP`
(`apps/web-server/server/utils/subprocess_env.py:32-40`), `core/child_env.py`,
the context7 MCP entry (`core/client.py:855-859`) and
`build_backend._PASSTHROUGH_BUILD_ENV`
(`apps/web-server/server/services/build_backend.py:295`, `:318`).
`get_agent_env_blanks` (`auth.py:167`) and its two merge sites
(`client.py:614`, `simple_client.py:89`) pick up the new names with no edit.

### 2. Effect on each process

| Process | Effect |
|---|---|
| Agent in the pod, through `create_client` / `create_simple_client` | All six are blanked. |
| Agent inside a kubejob build | All six are blanked by the same `get_agent_env_blanks`, which run.py reaches through `create_client` / `create_simple_client`. The pod and the Job behave the same. |
| Web-server process | No change. It never filters its own env, so `routes/search.py:38/:64`, `provider_health.py:20` and `access_review_evidence_cron.py:107` keep working. |
| In-pod runner (`make_subprocess_env`, `runner=True`) | Keeps `OPENAI_COMPATIBLE_API_KEY`, because `RUNNER_KEEP` is restored after the deny filter (`child_env.py:49`). Loses `S3_ACCESS_KEY` and the four unused keys. |
| Other server-started children (`runner=False`) | No change. `_CREDENTIAL_NAME` (`child_env.py:24`) already drops all six names because they end in `_KEY`. |
| kubejob run.py | Keeps the S3 key and the OpenAI-compatible key. Its env comes from the Job spec, not from `child_env`. |
| Catalog MCP servers with an explicit `env` | No change. `_externalize_secret_env` merges into `sdk_env` at `client.py:914`, after the blanks at `:614`, so a declared value still wins. |

### 3. Tests

- **`tests/test_agent_env_scrub.py`:** add the six names to `SECRETS`
  (line 25 onward) with the value `"x"`. That brings them under
  `test_all_secrets_are_blanked` (`:73`) and `test_simple_client_env_is_scrubbed`
  (`:101`). No new test function is needed.
- **`tests/test_child_process_env.py`:**
  - In `test_credential_names_dropped_for_tools_kept_for_runner` (`:184-208`),
    the assertion on line 207, `runner["CONTEXT7_KEY"] == "c"`, will fail once
    this change lands. Replace it with:
    - `runner["OLLAMA_API_KEY"] == "c"`, which still shows that a
      credential-shaped name that is not denied survives in the runner (#1680);
    - `"CONTEXT7_KEY" not in runner` and `"S3_ACCESS_KEY" not in runner`.

    Keep the half of the test that covers tools.
  - In the `secret_env` fixture (`:36-46`), set `OPENAI_COMPATIBLE_API_KEY=oc`.
    In `test_make_subprocess_env_keep_and_drop` (`:87`), assert
    `env["OPENAI_COMPATIBLE_API_KEY"] == "oc"`. This proves that the runner
    exception holds.

### 4. Follow-up issue (outside the diff)

File one new issue that links #363 and #1674. Its evidence:

- the six names this denylist missed;
- `OLLAMA_API_KEY` is forwarded into build Jobs and is not denied;
- denial is case-sensitive, while pydantic `env_prefix="APP_"` (`config.py:248`)
  is case-insensitive by default, so a lowercase `app_cfactory_read_key` would
  still be read and would not be blanked;
- `tools/executor.py:313` runs Bash for the OpenAI-compatible and Ollama
  agentic providers with the unscrubbed runner env, which #1692 does not cover;
- the allowlist question.

### Proposed answers to the intent's open questions

These are proposed defaults for the approver to confirm or change at this gate.
None of them has been approved yet.

1. **Exact names or a pattern? Proposed: exact names.** Add the six names to
   `_AGENT_ENV_DENY_EXACT` and leave `_AGENT_ENV_DENY_PATTERN` as it is. Since
   #1691, `is_denied_env_key` also filters the runner (`child_env.py:43-48`). A
   pattern such as `_API_KEY$` would therefore strip operator MCP credentials
   with arbitrary names from the runner, which #1680 kept on purpose
   (`child_env.py:20-23`). The existing list already uses exact names. The wider
   problem goes to the follow-up issue (Q4).
2. **S3 in the runner? Proposed: deny `S3_ACCESS_KEY` everywhere and leave
   `RUNNER_KEEP` unchanged.** The intent says the `RUNNER_KEEP` option would
   need an agent-only scrub. It would not: adding a name to `RUNNER_KEEP`
   already gives "the runner keeps it, the agent gets a blank", because
   `get_agent_env_blanks` reads the runner's own `os.environ`
   (`auth.py:167-183`). That is how `OPENAI_COMPATIBLE_API_KEY` works today.
   It is still not needed here. S3 is read only on the packed-workspace path
   (`core/workspace_fetch.py:84`, `:196`), and in the kubejob run.py gets S3
   from the Job spec. The runner has had no `S3_SECRET_KEY` since #1691, and
   nothing has failed.
3. **Context7? Proposed: scrub only.** Do not add an `env` block to
   `mcp_servers["context7"]`. Nothing reads `CONTEXT7_KEY`, and context7 starts
   without an `env` (`client.py:855-859`), so it runs unauthenticated today.
   Wiring the key in is a feature that hands the key to the MCP. It needs its
   own issue if someone wants it.
4. **Allowlist (#363)? Proposed: keep it out of scope and file the new
   follow-up issue in section 4** rather than reopening #363. An allowlist
   changes behaviour for every operator MCP credential, so it is a design
   change. A new issue can carry this round's evidence.
5. **Kubejob passthrough? Proposed: rely on run.py's scrub and file no new
   issue.** run.py in the Job needs `OPENAI_COMPATIBLE_API_KEY`
   (`phase_config.py:995`, `:1049`) and S3 (`workspace_fetch.py:196`, `:278`).
   Narrowing dispatch belongs to #1669, #1670 and #1677.
6. **Is the `OPENAI_COMPATIBLE_API_KEY` runner exception safe? Proposed: yes,
   with no code change.** `RUNNER_KEEP` lists it (`subprocess_env.py:35`), and
   `child_env` restores keep names after the deny filter (`child_env.py:49`).
   The new assertion in `test_make_subprocess_env_keep_and_drop` pins this.

## Alternatives rejected

1. **Suffix pattern (`_API_KEY$|_ACCESS_KEY$|...`).** It strips operator MCP
   credentials with arbitrary names from the runner, which #1680 kept on
   purpose. Custom MCP servers have no `env` block and depend on the inherited
   environment.
2. **Adding the S3 names to `RUNNER_KEEP`.** The in-pod runner does not use S3,
   so this would hand it a credential it never reads.
3. **A new agent-only scrub helper.** `RUNNER_KEEP` plus `get_agent_env_blanks`
   already provides this.
4. **Wiring `CONTEXT7_API_KEY` into the context7 MCP.** That is a feature (Q3).
5. **Stopping the forwarding of these keys into build Jobs.** It would break
   OpenAI-compatible phases and the packed unpack in kubejob builds, and it is
   out of scope (#1669, #1670, #1677).
6. **Reopening #363.** See Q4.
7. **Case-folding in `is_denied_env_key`.** It closes a real gap: a lowercase
   `app_cfactory_read_key`. But it changes the meaning of every existing entry
   and of both callers, which goes beyond the exact-names decision. It goes to
   the follow-up issue.
8. **Extra guard tests: a lock on blank-then-MCP ordering, and a check on
   kubejob passthrough credentials.** This change touches neither the ordering
   at `client.py:614`/`:914` nor the passthrough list, so these tests would
   protect behaviour that is outside this diff. The passthrough guard would
   also need an `OLLAMA_API_KEY` exception that is still undecided. Both go to
   the follow-up issue.

## Risks

- **An operator MCP server that reads `RAPIDAPI_KEY`, `LANGCHAIN_API_KEY` or
  `CONTEXT7_KEY` from the inherited agent env loses it.** This affects the
  aifactory pod and kubejob builds. The workaround is to declare the name in the
  catalog server's `env`, which takes precedence over the blank
  (`client.py:914`). State this in the PR description.
- **In-pod gate packing.** `gate_runner._store_env` (`gate_runner.py:433`)
  reads S3 names from the runner env. In the pod the runner has had no
  `S3_SECRET_KEY` since #1691, so removing the access key ID breaks nothing that
  works today. The PR must not claim this path is fixed. In a kubejob the Job
  spec still supplies both names.
- **Exact names miss future keys and lowercase variants.** This is accepted, and
  both go to the follow-up issue.
- **Codex and Antigravity agents (#1692) are not covered.** The PR must not
  claim they are.
- **The in-house tool loop is not covered either, and #1692 does not name it.**
  `openai_compatible_agentic.py` and `ollama_agentic.py` run the model's Bash
  calls through `tools/executor.py:313` (`create_subprocess_shell` with no
  `env`), so those phases see the runner's full environment, including
  `OPENAI_COMPATIBLE_API_KEY` and every `RUNNER_KEEP` name. This change neither
  fixes nor worsens that. The PR must not claim OpenAI-compatible or Ollama
  phases are scrubbed, and the follow-up issue (section 4) must record it.

## Verification

1. Run
   `pytest tests/test_agent_env_scrub.py tests/test_child_process_env.py tests/test_build_backend_kubejob.py tests/test_mcp_secret_env_599.py tests/test_no_unscrubbed_spawn.py -q`.
   Everything must pass. Reverting the `auth.py` hunk must make the new
   assertions fail. `test_build_backend_kubejob.py:345-373` must still show that
   both forwarded names reach the Job.
2. From `apps/backend`, run this with all six names set to `x`:
   `python -c "from core.auth import get_agent_env_blanks; print(sorted(get_agent_env_blanks()))"`.
   The output must include all six.
3. After deploying, run a throwaway agent task whose Bash step runs
   `env | grep -E 'CFACTORY_READ|CONTEXT7|RAPIDAPI|LANGCHAIN|OPENAI_COMPATIBLE|S3_ACCESS'`,
   once in the pod and once as a kubejob build. Expect empty values or no
   output.
4. Confirm that an OpenAI-compatible phase still completes in both places, and
   that the web server's `/search` still returns results.
