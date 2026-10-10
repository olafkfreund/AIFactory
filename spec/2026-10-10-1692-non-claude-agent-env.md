---
status: draft
issue: 1692
intent: intent/2026-10-10-1692-non-claude-agent-env.md
---

# Spec: non-Claude agent CLIs must not inherit the runner's credentials

Paths are relative to `apps/backend/` unless they start with `tests/`,
`scripts/`, `docs/` or `.github/`. Line numbers are at `13a5de36`.

## Design

Every non-Claude CLI spawn site gets an `env=` built by the existing
`core.child_env.child_env(keep=..., extra=...)` (`core/child_env.py:27-58`).
No new helper is added, and `child_env.py` and `core/auth.py` are not edited.

How `child_env` behaves with `runner=False`, which is what the design relies on:

- It drops every deny-listed name (`is_denied_env_key`, `core/auth.py`), both
  `ANTHROPIC_API_KEY*` names (`_STRIP_VARS`, `child_env.py:16`) and every
  `*_KEY`/`*_TOKEN` name (`child_env.py:24,46`).
- It then puts back the `keep` names that are set (`:48`). **`keep` overrides
  every drop rule**, so a deny-listed name can be put back. That is what lets
  Vertex work, and it is also why OpenCode needs an explicit Anthropic guard
  (see below).
- It applies `extra` and appends the git hooks-off pin (`:51-57`).
- It reads `os.environ` on every call, so building it inside the spawn method
  reflects the env at spawn time.

Names without a `_KEY`/`_TOKEN` suffix pass through unchanged: `HOME`,
`PATH`, `XDG_*`, `CODEX_HOME`, `*_BASE_URL`, the non-key `OPENAI_COMPATIBLE_*`
names, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`.

### The six sites

| Site | Today | New |
|---|---|---|
| `providers/codex.py:186` | no `env=` | `env=child_env(keep=("OPENAI_API_KEY", "CODEX_API_KEY"))` |
| `providers/codex_agentic.py:240` | no `env=` | same as `codex.py`. Built once per `__aenter__`, which is right because the MCP child lives for the whole session. |
| `providers/antigravity.py:272` | no `env=` | `env=child_env(keep=_GEMINI_KEEP)`, with **no** `extra` (see the Q1 note) |
| `providers/antigravity_agentic.py:197` | `{**os.environ, "GEMINI_CLI_TRUST_WORKSPACE": "true"}` | `env = child_env(keep=_GEMINI_KEEP, extra={"GEMINI_CLI_TRUST_WORKSPACE": "true"})`. Keep the local name `env` and the `env=env` line at `:204`, because `tests/test_gemini_trust_workspace.py:22-29` greps the source for both. |
| `providers/copilot_agentic.py:195` | `{**os.environ, "COPILOT_ALLOW_ALL": "true"}` | `env = child_env(keep=("COPILOT_GITHUB_TOKEN",), extra={"COPILOT_ALLOW_ALL": "true"})` |
| `providers/opencode_agentic.py:326` | `env = os.environ.copy()` | `env = child_env(keep=_opencode_keep(self._model))`. The `setdefault` of `OPENCODE_DISABLE_AUTOUPDATE` at `:327` and the XDG pre-warm below it stay unchanged. |

`_GEMINI_KEEP = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")`
is defined once in `providers/antigravity.py`. `antigravity_agentic.py`
already imports from that module (`:50`), so it imports the tuple from there.
`GOOGLE_APPLICATION_CREDENTIALS` must be in the tuple: the deny list removes
it (`core/auth.py:144`), and kubejob mounts Vertex ADC through it
(`core/job_dispatch.py:127-140, :576`).

The new module-level function in `providers/opencode_agentic.py`:

```python
def _opencode_keep(model: str) -> tuple[str, ...]:
    # ponytail: env name derived by convention (<PROVIDER>_API_KEY); a provider
    # with an unusual name (amazon-bedrock's AWS_*, google-vertex ADC, azure)
    # falls back to `opencode auth login`. Follow-up issue if one shows up.
    provider, sep, _ = model.partition("/")
    if not sep or not provider:
        return ()
    name = f"{provider.upper().replace('-', '_')}_API_KEY"
    if name.startswith("ANTHROPIC"):  # keep would restore it (child_env.py:48)
        return ()
    if provider == "google":
        return (name, "GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_API_KEY")
    return (name,)
```

The Anthropic guard is required. Without it, `anthropic/claude-sonnet-4-5`
(the example model in the error at `opencode_agentic.py:285`) would put
`ANTHROPIC_API_KEY` back. If the model is empty or has no slash, no key is
kept, and `_build_command` (`:254`) still raises its existing clear error
(`:279-287`).

`OPENCODE_DISABLE_AUTOUPDATE` stays a `setdefault` and does not move into
`extra=`, because `extra` would override a value the operator set.

**Imports:** add `from core.child_env import child_env` to all six files.
Remove `import os` from `antigravity_agentic.py:41` and `copilot_agentic.py:40`.
In each file, the replaced line is the only use of `os`, so leaving the import
in fails ruff F401. `opencode_agentic.py` keeps `os`, because `:228` still
uses it. Importing `core.child_env` creates no import cycle: it pulls in
`core.auth`, which imports only stdlib and `factory_common.logsafe`.

### Proposed answers to the intent's open questions

These are proposed defaults for the user to confirm.

1. **Scope: all six sites, in one PR.** All six leak through the same gap,
   so fixing two would leave four identical one-line holes, two of them on
   auto-approve CLIs (Copilot `--allow-all-tools`, OpenCode `run`). Each site
   gets one `child_env(keep=...)` call, with any existing flag passed as
   `extra=`.
   *Narrowed from the proposed default:* the text-only `antigravity.py` does
   **not** get `GEMINI_CLI_TRUST_WORKSPACE`. It never set the flag before.
   Adding it would make Gemini trust `.gemini/` settings in a worktree the
   agent can write to, including settings that declare MCP servers. That
   widens what the process can do, which is unrelated to #1692.
2. **Copilot: keep only `COPILOT_GITHUB_TOKEN`.** Drop `GH_TOKEN` and
   `GITHUB_TOKEN`. Those are the runner's push/PR token (`GITHUB_KEEP`,
   `child_env.py:18`) and must not reach an `--allow-all-tools` agent.
   Copilot authenticates with its on-disk OAuth login (`~/.copilot`, which
   survives because `HOME`/`XDG_*` are kept; the in-cluster `copilot-apps.json`
   is unaffected), or with a `COPILOT_GITHUB_TOKEN` the operator sets on
   purpose. This does not conflict with #1688, which moves git/gh to a
   per-call `github_env` and touches no agent spawn, or with #1671, because no
   GitHub identity name is added to any keep list.
3. **OpenCode: keep only the key for the configured model's provider.** Use
   `_opencode_keep(self._model)` as above. Never keep `ANTHROPIC_API_KEY`;
   anthropic models use `opencode auth login`. Keeping every provider key
   would expose the other vendors' billing keys to an agent that runs shell
   commands without approval.
4. **Git hooks: accept the `GIT_CONFIG_*` hooks-off pin** from `child_env`.
   Do not build a variant without it. The pin is appended after any existing
   `GIT_CONFIG_*` entry (`child_env.py:51-57`), so nothing the operator set is
   lost. #1689 and #1690 build on the same pin. The PR must say that
   repository hooks do not run inside commits the agent makes.
5. **Tool loop: file a separate follow-up issue** for the `tools/executor.py`
   scrub used by the OpenAI-compatible and Ollama agents. It stays out of this
   PR, and the PR does not claim coverage for it. It is in-process code, not a
   CLI spawn, and #1674's plan also leaves it out of #1692.

## Alternatives rejected

- **Fix only the two sites the issue names.** That leaves four identical
  holes, two of them on auto-approve CLIs.
- **A per-provider helper, table or `agent_cli_env()` in `core/`.** That is a
  second allow/deny list in the `core/` files that #1671, #1689 and #1690 are
  editing. Six inline `keep=` tuples are smaller and easier to review.
- **`child_env(runner=True)`, then popping names.** `runner=True` keeps
  `CLAUDE_CODE_OAUTH_TOKEN`, `CONTEXT7_API_KEY` and
  `OPENAI_COMPATIBLE_API_KEY`, which is the leak itself. Popping names
  afterwards is a deny list, so any credential added later would leak by
  default.
- **Copilot keeps `GH_TOKEN`/`GITHUB_TOKEN`.** That puts the push/PR token
  inside an `--allow-all-tools` agent.
- **OpenCode keeps every provider key.** See Q3.
- **A fixed allow-list of OpenCode providers.** It is more code, and it goes
  stale as models.dev adds providers. The naming convention plus the
  `ponytail:` comment covers it.
- **`OPENCODE_DISABLE_AUTOUPDATE` in `extra=`.** That turns `setdefault` into
  an override that ignores the operator's value.
- **A `child_env` variant without the hooks-off pin.** It would conflict with
  #1689/#1690 and weaken the default.
- **Trust flag on text-only Antigravity.** See the Q1 note.
- **Folding in the `tools/executor.py` scrub.** See Q5.
- **Adding the six files to `_BACKEND_FILES` in
  `tests/test_no_unscrubbed_spawn.py:59`.** The per-site tests already guard
  these sites. Add it if a seventh CLI provider appears.

## Risks

All of these go in the `fix(security)` PR body. The PR's answer to "do keys
change on a deployed path" is this: no keys are added. On both the
`subprocess` and `kubejob` backends, credentials are removed from the CLI
children, and the runner's own env is unchanged. kubejob needs no Job-spec or
Helm change, because the scrub runs inside the runner pod on the env the Job
injected (`build_backend.py`).

1. **Copilot authenticated through `GH_TOKEN`/`GITHUB_TOKEN` loses auth.**
   Release note: set `COPILOT_GITHUB_TOKEN`, or use the on-disk login.
2. **OpenCode with `anthropic/*` that relied on `ANTHROPIC_API_KEY`** now
   needs `opencode auth login`.
3. **OpenCode providers whose auth name is not `<P>_API_KEY`**
   (amazon-bedrock's deny-listed `AWS_*`, google-vertex ADC, azure) fall back
   to on-disk auth. This is recorded in the `ponytail:` comment, and a
   follow-up issue is opened only if someone hits it.
4. **Codex custom providers** whose `env_key` is not
   `OPENAI_API_KEY`/`CODEX_API_KEY` (for example `AZURE_OPENAI_API_KEY`) lose
   the key. `OPENAI_BASE_URL` still passes. Release note: use `codex login`
   or `OPENAI_API_KEY`.
5. **The OpenCode model prefix chooses which `*_API_KEY` survives.** The model
   is operator config: an explicit model is checked by `_MODEL_NAME_RE`
   (`opencode_agentic.py:94`, applied at `:217`), while the
   `OPENCODE_DEFAULT_MODEL` fallback (`:228`) is not. Either way the derived
   name always ends in `_API_KEY`. It can therefore never be
   `GH_TOKEN`, `GITHUB_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` or
   `COPILOT_GITHUB_TOKEN`, and the Anthropic guard covers `ANTHROPIC_*`. A
   nonsense prefix such as `linear/` would keep `LINEAR_API_KEY`. That is
   accepted, because the operator owns the model setting.
6. **Git hooks no longer run inside commits the agent makes**
   (`core.hooksPath=/dev/null`). The PR states this.
7. **Residual exposure the PR must not claim to fix.** The PR says
   "env-borne credentials", not "all credentials".
   - `HOME` is kept on purpose, so on-disk credentials such as
     `~/.config/gh/hosts.yml` and `~/.git-credentials` stay readable.
   - `SSH_AUTH_SOCK` passes through, because it has no `_KEY`/`_TOKEN` suffix
     and is not deny-listed.
   - The `tools/executor.py` tool loop is untouched (Q5).

   File follow-up issues for these.

## Verification

Add one new file, `tests/test_agent_cli_env.py`, fully typed for
`mypy --strict`. `tests/test_child_process_env.py` is not edited, because the
#1671 branch and the #1673 worktree both have pending edits to it.

- **Setup:** use `monkeypatch.setenv` to set fake values for the foreign
  names: `GH_TOKEN`, `GITHUB_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`,
  `ANTHROPIC_API_KEY`, `CONTEXT7_API_KEY` and `OPENROUTER_API_KEY`. Also set
  each site's own key, `HOME` and `PATH`.
- **Capture:** patch `providers.<mod>.asyncio.create_subprocess_exec` with a
  typed fake that records `kwargs["env"]` and then raises a private
  `_Spawned` exception. Patch `shutil.which`, and `get_antigravity_binary` for
  the two Antigravity providers, to return a dummy path. Drive each site
  through the method that spawns: `CodexAgenticProvider.__aenter__`, and the
  query/run path for the other five. Suppress the error that `_Spawned`
  causes at each site.
- **One parametrised case per site (6 cases).** Each case asserts:
  - the foreign names are absent (`OPENROUTER_API_KEY` counts as foreign
    except for OpenCode built with `openrouter/x`);
  - the site's own key is present;
  - its flag is present: `GEMINI_CLI_TRUST_WORKSPACE` (agentic Antigravity
    only), `COPILOT_ALLOW_ALL`, `OPENCODE_DISABLE_AUTOUPDATE`;
  - `HOME` and `PATH` are present;
  - the hooks-off pin is present: some `GIT_CONFIG_KEY_n == "core.hooksPath"`.
- **Edge cases:**
  - `_opencode_keep`: `anthropic/x` gives `()`; `google/x` gives the three
    names; `x-y/m` gives `("X_Y_API_KEY",)`; `""` and a slash-less model give
    `()`.
  - Antigravity keeps `GOOGLE_APPLICATION_CREDENTIALS` and
    `GOOGLE_CLOUD_PROJECT`.
  - Codex keeps `OPENAI_BASE_URL`.
  - Text-only `antigravity.py` does not get `GEMINI_CLI_TRUST_WORKSPACE`.
  - A variable set after the provider is constructed shows up at spawn, which
    proves the env is built per call.
- **Assertion style:** assert on key membership only, for example
  `assert "GH_TOKEN" not in env`. Never put env values in assertion messages
  or logs (CodeQL clear-text rules).
- **Existing tests stay green:** `tests/test_gemini_trust_workspace.py`, the
  `_build_subprocess_env` cases in `tests/test_opencode_provider.py:316-430`
  (they use only `XDG_*`/`HOME`, which still pass through),
  `tests/test_copilot_provider.py`, `tests/test_codex_stderr_drain.py`,
  `tests/test_outbound_scrub_all_providers.py`, `tests/test_qa_providers.py`
  and `tests/test_no_unscrubbed_spawn.py`.

CI gates:

- **Default ruff:** the two `os` import removals clear F401.
- **Strict ruff + `mypy --strict` ratchet** (`.github/workflows/cq-ratchet.yml`,
  `scripts/cq_ratchet.py`): six touched providers plus the new test file.
  `_opencode_keep` is annotated, and the new test starts from zero violations,
  so it must have none.
- **CodeQL:** no new logging. The existing debug lines log only
  `cmd`/`cwd`/model.
- **Autonomy matrix** (`.github/workflows/autonomy-matrix.yml`): `providers`
  is a model-client root (`scripts/gen_autonomy_matrix.py:462`), so the new
  `providers -> core.child_env -> core.auth` import edges may change the
  section C output. Run `python scripts/gen_autonomy_matrix.py --check`
  locally. If it reports drift, regenerate and commit
  `docs/docs/compliance/autonomy-matrix.md` and
  `docs/static/compliance/autonomy-matrix.json` in the same PR.

Follow-up issues to file: the `tools/executor.py` scrub (Q5), and the
residual exposure through `HOME` on-disk credentials and `SSH_AUTH_SOCK`
(Risk 7).
