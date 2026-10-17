---
status: draft
issue: 1757
spec: spec/2026-10-17-1757-agent-child-isolation.md
---

# Plan: agent child isolation (private HOME, env allowlist, quiesce before push)

Self-contained. You do not need to open the intent or the spec.
Issues: #1757, #1732, #1762 (b), #1724 (build Jobs only), #1760 (decision only).
All paths below are relative to the repo root; backend paths are under
`apps/backend/`. Tests live in the repo-root `tests/` dir (they put
`apps/backend` on `sys.path` themselves) unless a path says otherwise.

## Approved decisions (from the spec, Option A, user 2026-10-17)

1. **D1 Private HOME per agent CLI spawn.** A 0700 temp dir (`aif-home-*`)
   holding only that CLI's own login file(s), copied 0600 from the real
   home. A changed login is written back under a lock. If the dir cannot be
   built the spawn fails (raises); there is no fallback to the real HOME.
2. **D2 Env allowlist for agent children** instead of the deny list, so
   `SSH_AUTH_SOCK` is dropped. The deny list still applies on top. Escape
   hatch `AIFACTORY_AGENT_ENV_EXTRA` (comma-separated names). Non-agent
   callers of `child_env()` keep today's behaviour.
3. **D3 Operator MCP credentials** pass through an explicit `env_keys` list
   on a custom MCP server entry.
4. **D4 The backend `.env` load** in `agent_service` drops denied keys.
5. **D5 Quiesce in build Jobs:** before the packed-workspace push, SIGKILL
   every other process of the same uid outside run.py's ancestor chain;
   refuse the push if any survive. Only when `JOB_ID` and `FACTORY_SERVICE`
   are both set. Local runners keep the window (documented; #1724 stays open
   for it).
6. **D6 `host_isolated()` stays narrow** (#1760): comment and test docstring
   give the reason; no code change.
7. Losing `SSH_AUTH_SOCK`, `~/.ssh` and home-folder tool configs for agents
   is accepted.

## Decisions made while planning (within the spec)

- P1 Lock = `flock` on the real credential's **parent directory**, not the
  file: the atomic `os.replace` swaps the inode, so a lock on the file would
  not serialise two writers.
- P2 Write-back is compare-and-swap: only when the real file still holds the
  bytes we copied in (another run did not refresh it meanwhile), only for
  files that existed before the run, and only when the new content parses as
  JSON (every listed login file is JSON). Otherwise keep the real file and
  log a warning. Config files we copy (claude/gemini `settings.json`) are
  never written back: a same-uid agent can edit them and they can run
  hooks.
- P3 Cleanup is a `weakref.finalize` owned by the `AgentHome` object (so it
  also runs at interpreter exit). One-shot spawns keep `home` in a local;
  SDK clients tie it to the client with `weakref.finalize(client,
  home.close)`; the long-lived codex MCP server closes it in `__aexit__`.
- P4 Git identity: the private HOME gets a `.gitconfig` with only
  `user.name`, `user.email` and `safe.directory` read from the real global
  config (agents commit; packed Jobs mark `/work` safe in the global config).
- P5 claude: `settings.json` is copied (read-only) because fast mode writes
  `fastMode` there before the spawn (`core/fast_mode.py`). The private HOME
  is therefore created after the fast-mode block in `create_client`.
- P6 The web-server insights providers
  (`apps/web-server/server/services/insights_providers/*`) are not in the
  spec's caller list and are not changed here.

## Step 1. `core/agent_home.py`: the private HOME helper (new file)

**File:** `apps/backend/core/agent_home.py` (new), test
`tests/test_agent_home.py` (new).

**Change.** One class:

```python
_BASES = {  # env override -> default dir under the real HOME
    "CLAUDE_CONFIG_DIR": ".claude",
    "CODEX_HOME": ".codex",
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_CACHE_HOME": ".cache",
    "XDG_STATE_HOME": ".local/state",
}
# cli -> ((base env var or "" for HOME itself, relative path, write back), ...)
_LOGINS = {
    "claude": (("CLAUDE_CONFIG_DIR", ".credentials.json", True),
               ("CLAUDE_CONFIG_DIR", "settings.json", False)),
    "codex": (("CODEX_HOME", "auth.json", True),),
    "copilot": (("XDG_CONFIG_HOME", "github-copilot/apps.json", True),
                ("XDG_CONFIG_HOME", "github-copilot/hosts.json", True),
                ("", ".copilot/config.json", True)),
    "gemini": (("", ".gemini/oauth_creds.json", True),
               ("", ".gemini/google_accounts.json", True),
               ("", ".gemini/settings.json", False)),
    "opencode": (("XDG_DATA_HOME", "opencode/auth.json", True),),
    "bash": (),
}

class AgentHome:
    def __init__(self, cli: str) -> None   # raises on any failure (fail closed)
    env: dict[str, str]                    # HOME, TMPDIR, every _BASES var
    def close(self) -> None                # idempotent: write back, rmtree
    __enter__/__exit__                     # close on exit
```

- Real path of an entry: `Path(os.environ.get(base) or Path.home() /
  _BASES[base]) / rel` when `base`, else `Path.home() / rel`. Private path:
  `home / _BASES[base] / rel`, else `home / rel`.
- `__init__`: unknown `cli` -> `ValueError`. `home =
  tempfile.mkdtemp(prefix="aif-home-")` (0700). Create `home/tmp` (0700).
  For each entry whose real path `is_file()`: read bytes, create parents
  0700, write private copy with `os.open(..., O_WRONLY|O_CREAT|O_EXCL,
  0o600)`. Record `(private, real, original_bytes, writeback)`. Missing real
  file: skip (env-token auth). Write `.gitconfig` (P4). Any exception:
  `shutil.rmtree(home, ignore_errors=True)` then re-raise.
- `env` = `{"HOME": home, "TMPDIR": home/tmp, **{var: home/_BASES[var]}}`
  as strings.
- `.gitconfig` (P4): run `["git", "config", "--global", "--null",
  "--get-regexp", r"^(user\.(name|email)|safe\.directory)$"]` with
  `env=child_env()` (real HOME), `timeout=10`, `capture_output=True`,
  `check=False`. Parse `-z` output: records split on `\0`, each `key\nvalue`.
  Skip values containing `\n`. Write `[user]\n\tname = "<v>"` style lines
  (section = text before the last dot, name = after), escaping `\` and `"`.
  `FileNotFoundError` (no git) or non-zero rc: write no identity, do not
  fail.
- `close()` -> `self._finalizer()`; the finalizer is
  `weakref.finalize(self, _cleanup, home, entries)` with a module-level
  `_cleanup` that never references `self` and never raises (log warnings).
  `_cleanup` per entry with `writeback` and `original is not None` and the
  private file exists and its bytes differ and `json.loads` succeeds:
  `fd = os.open(real.parent, os.O_RDONLY)`, `fcntl.flock(fd, LOCK_EX)`;
  re-read real; if equal to `original`: `mkstemp(dir=real.parent,
  prefix=".aif-")`, `fchmod 0o600`, write, `os.replace` onto `real`; else
  log "changed meanwhile, kept". Always unlock/close fd. Then
  `shutil.rmtree(home, ignore_errors=True)`.

**Tests** (`tests/test_agent_home.py`), all with `monkeypatch.setenv("HOME",
tmp_path/"real")`, and `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `XDG_*` deleted:
1. codex home holds `.codex/auth.json` (0600, dir 0700) and not the
   claude/gemini files or a fake `real/.ssh/id_ed25519`; `env["CODEX_HOME"]`
   and `env["HOME"]` point inside the private dir.
2. Changed valid JSON login is written back; unchanged is not touched
   (mtime same); non-JSON change not written back; changed real file
   (simulated concurrent refresh) is kept.
3. `settings.json` changes are never written back.
4. `close()` removes the dir; second `close()` is a no-op; dropping the last
   reference (`del h; gc.collect()`) removes it too.
5. `mkdtemp` patched to raise `OSError` -> `AgentHome("codex")` raises; copy
   failure (real file `chmod 000`, skip if root) raises and leaves no
   `aif-home-*` dir.
6. `CODEX_HOME` override: the real path is read from it.
7. `.gitconfig`: with a real `~/.gitconfig` holding `user.name`,
   `credential.helper` and `safe.directory`, the private one has name and
   safe.directory, not the helper.

**Check:** `cd apps/backend && .venv/bin/python -m pytest ../../tests/test_agent_home.py -q`
(lane: use the main clone's `apps/backend/.venv/bin/python`), `ruff check`,
`ruff format --check` on both files.

**Traps.** Do not call `Path.home()` at import time. Never touch the real
user's files in tests (always fake `HOME`). The finalizer callback must not
hold `self`, or the object never dies. `flock` on a directory fd needs
`O_RDONLY`, not `O_WRONLY`.

## Step 2. Env allowlist in `core/auth.py` and `child_env(agent=True)`

**Files:** `apps/backend/core/auth.py:109-198`, `apps/backend/core/child_env.py:64-100`,
tests `tests/test_agent_env_scrub.py`, `tests/test_child_process_env.py`.

**Change.**
- `auth.py` after `_AGENT_ENV_DENY_PATTERN` (:167-170): add

  ```python
  _AGENT_ENV_ALLOW = re.compile(
      r"PATH|LANG|LANGUAGE|TZ|TERM|COLORTERM|TMPDIR|USER|LOGNAME|SHELL|CI"
      r"|PYTHONUNBUFFERED|PYTHONIOENCODING|TRACEPARENT"
      r"|SSL_CERT_FILE|SSL_CERT_DIR|NODE_EXTRA_CA_CERTS|REQUESTS_CA_BUNDLE"
      r"|GIT_(?:AUTHOR|COMMITTER)_(?:NAME|EMAIL)"
      r"|LC_\w+|CLAUDE_CODE_\w+|NIX_\w+|(?i:(?:HTTPS?|NO|ALL)_PROXY)"
  )
  def is_agent_env_allowed(name: str) -> bool:
      if name in _AGENT_ENV_KEEP: return True
      extra = {n.strip() for n in os.environ.get("AIFACTORY_AGENT_ENV_EXTRA", "").split(",")}
      return (bool(_AGENT_ENV_ALLOW.fullmatch(name)) or name in extra) and not is_denied_env_key(name)
  ```
  (`""` from the split must not match a real name; env names are never
  empty, so it is harmless.)
- `get_agent_env_blanks()` (:180-198): blank every `os.environ` key for
  which `is_agent_env_allowed` is False, keeping the existing
  `ANTHROPIC_API_KEY`-when-API-key-auth exception. Update the docstring and
  the #363 comment block (:93-107) to say allowlist (#1757).
- One info log per process per new name, names only:
  `_log_dropped(names)` keeps a module-level `set`, skips names that
  `is_denied_env_key` matches, and logs
  `"agent env allowlist dropped (names only, #1757): %s"`. Called from
  `get_agent_env_blanks()` and from `child_env(agent=True)`.
- `child_env.py`: add keyword `agent: bool = False`. Filter adds
  `and (not agent or is_agent_env_allowed(k))`. `keep` and `extra` are
  applied after, as today, so they still win. Docstring: one line for
  `agent`. Import `is_agent_env_allowed` (and `_log_dropped`) from
  `core.auth`.

**Tests.**
- `tests/test_agent_env_scrub.py`: `KEEP` loses `HOME`, `PWD`, `NODE_ENV`
  (now blanked: that is the point); add a test that `SSH_AUTH_SOCK`,
  `FOO_SECRETISH`, `HOME` are blanked, `PATH`, `LANG`, `LC_ALL`,
  `NIX_PATH`, `CLAUDE_CODE_OAUTH_TOKEN`, `https_proxy` are not; that
  `AIFACTORY_AGENT_ENV_EXTRA=MY_TOOL_DIR` keeps `MY_TOOL_DIR` but never a
  denied name (`AIFACTORY_AGENT_ENV_EXTRA=JWT_SECRET` still blanks it).
  `test_empty_when_no_secrets` keeps passing (only PATH and the token set).
- `tests/test_child_process_env.py`: with `agent=True`, `FOO_SECRETISH` and
  `SSH_AUTH_SOCK` absent, `PATH` present, a `keep` name present, `extra`
  applied, git pins still present; without `agent`, `FOO_SECRETISH` and
  `SSH_AUTH_SOCK` present (unchanged behaviour).

**Check:** `pytest tests/test_agent_env_scrub.py tests/test_child_process_env.py
tests/test_env_passthrough_credentials.py tests/test_agent_sdk_env_blanks.py -q`,
ruff check/format on the touched files.

**Traps.** Linux env names are case-sensitive: the allowlist is exact-case
except the proxy vars. Do not change the default `child_env()` path: ~150
server call sites depend on it. `is_denied_env_key` must still win over the
allowlist and the extra list, but not over `_AGENT_ENV_KEEP`.

## Step 3. Wire the private HOME into the Claude SDK paths

**Files and lines:**
- `apps/backend/core/client.py`: after the fast-mode block (ends :656), add
  `home = AgentHome("claude")` and `sdk_env.update(home.env)` with a
  one-line comment (#1732; after fast mode so `settings.json` carries
  `fastMode`). After `bare_client = ClaudeSDKClient(...)` (:1170):
  `weakref.finalize(bare_client, home.close)`.
- `apps/backend/core/simple_client.py:86-88` and `:107`: same pattern
  (no fast mode there; create the home right after
  `sdk_env.update(get_agent_env_blanks())`).
- `apps/backend/runners/insights_runner.py:186-198`: `home =
  AgentHome("claude")` before the client; `env={**get_agent_env_blanks(),
  **home.env}`. `home` is a local that lives through `async with client`.
- `apps/backend/runners/ai_analyzer/claude_client.py:121-138`: build the
  bare `ClaudeSDKClient` into a local, `weakref.finalize(bare, home.close)`,
  return `wrap_client_outbound_scrub(bare)`; env as above.
- `apps/backend/runners/github/services/followup_reviewer.py:681-705`:
  `home = AgentHome("claude")` before the `async for`; env as above.

**Tests.**
- `tests/test_agent_sdk_env_blanks.py`: the two runtime tests assert the
  env also has `HOME` under an `aif-home-` dir and `SSH_AUTH_SOCK == ""`
  (set it first). The followup AST test accepts `env=` being a dict
  display whose `**` items include a `get_agent_env_blanks()` call.
- `tests/test_agent_env_scrub.py::test_simple_client_env_is_scrubbed`: the
  capture returns `types.SimpleNamespace()` (must be weak-referenceable)
  and asserts `env["HOME"]` is a private `aif-home-` dir that exists while
  the client is alive.

**Check:** `pytest tests/test_agent_sdk_env_blanks.py tests/test_agent_env_scrub.py
tests/test_agent_home.py -q` plus `pytest tests -q -k "client and env"` for
neighbours; ruff on touched files.

**Traps.** `object()` cannot be weak-referenced: any test double for
`ClaudeSDKClient` must be a normal object. The home must be created before
`ClaudeAgentOptions` is built (`options_kwargs["env"]` is `sdk_env`,
`client.py:1076`, the same dict, so updating it earlier is enough). Do not
create the home before `require_auth_token()`.

## Step 4. Wire the private HOME and allowlist into the CLI providers and tool loop

**Files and lines** (each: `home = AgentHome("<cli>")` as a local just
before the spawn, then `env=child_env(keep=<same keep>, extra={**home.env,
<existing extra>}, agent=True)`):
- `apps/backend/providers/codex.py:193` (`"codex"`).
- `apps/backend/providers/copilot_agentic.py:195-197` (`"copilot"`; keep
  `COPILOT_ALLOW_ALL` in `extra`).
- `apps/backend/providers/antigravity.py:285` (`"gemini"`).
- `apps/backend/providers/antigravity_agentic.py:197-199` (`"gemini"`; keep
  `GEMINI_CLI_TRUST_WORKSPACE`).
- `apps/backend/providers/opencode_agentic.py:330-358`: give
  `_build_subprocess_env` a parameter `home_env: Mapping[str, str] | None =
  None` passed as `extra`, with `agent=True`; in `_run_opencode` (:437):
  `home = AgentHome("opencode")`, `env = self._build_subprocess_env(home.env)`.
  The pre-warm then copies the real `~/.cache` catalogue into the private
  `XDG_CACHE_HOME`, which is what it already does for a foreign cache.
- `apps/backend/providers/codex_agentic.py`: `self._home: AgentHome | None
  = None` in `__init__` (:163 area); in `__aenter__` before the spawn
  (:240) `self._home = AgentHome("codex")` and the env as above; in
  `__aexit__` (:295-316) after the process block:
  `if self._home: self._home.close(); self._home = None`.
- `apps/backend/tools/executor.py:317-323` (tool-loop Bash): `home =
  AgentHome("bash")`, `env=child_env(extra=home.env, agent=True)`;
  `:423-429` (grep): `env=child_env(agent=True)` only.

**Tests.** New `tests/test_agent_cli_isolation.py`: patch
`asyncio.create_subprocess_exec` in `providers.codex` to capture `env` and
raise after capture (or return a fake proc); with `SSH_AUTH_SOCK`,
`FOO_SECRETISH` and `OPENAI_API_KEY` set and a fake real HOME holding
`.codex/auth.json` and `.ssh/id_ed25519`: env has no `SSH_AUTH_SOCK` or
`FOO_SECRETISH`, has `OPENAI_API_KEY`, `HOME` is a private dir whose
`.codex/auth.json` exists and whose `.ssh` does not. Same for the executor
Bash path: run `printf '%s|%s' "$HOME" "${SSH_AUTH_SOCK:-none}"` through
`ToolExecutor` (real subprocess, the security hook allows `printf`) and
assert HOME is not the fake real HOME and the socket is `none`.

**Check:** `pytest tests/test_agent_cli_isolation.py -q` and every existing
provider test: `pytest tests -q -k "codex or copilot or antigravity or
opencode or executor"`; ruff on touched files.

**Traps.** Existing tests may monkeypatch `child_env` with a fake that has
no `agent` kwarg: update those fakes, do not drop the kwarg. Keep the
existing `keep=` tuples exactly. Do not wrap large existing `try` blocks in
a new `with`: a local `home` is enough (P3) and keeps the diff small.

## Step 5. MCP `env_keys` and the backend `.env` scrub

**Files and lines:**
- `apps/backend/core/client.py:362-371` `_validate_custom_mcp_server`:
  add `"env_keys"` to `allowed_fields`; if present it must be a list of
  strings each matching `[A-Za-z_][A-Za-z0-9_]*`, else return False.
- `apps/backend/core/client.py:946-950` (command servers): after building
  the entry, for each name in `custom.get("env_keys", [])`: skip with a
  warning when `is_denied_env_key(name)` (parity with today, where such a
  var was blanked), else copy `os.environ[name]` into `sdk_env` when set.
  Values go to the claude process env, never to the server config (argv),
  as `_externalize_secret_env` does (:380-394). Put this in a small helper
  `_custom_server_env(custom, sdk_env)` so it is testable.
- `apps/web-server/server/services/agent_service.py:1047-1060`: skip keys
  `is_denied_env_key(key)` matches. Extract the loop into a module-level
  `_load_backend_env(path: Path, env: dict[str, str]) -> None` (same
  behaviour plus the scrub) and call it; import `is_denied_env_key` from
  `core.auth` the way `server/utils/subprocess_env.py` reaches core.

**Tests.**
- `tests/test_mcp_env_keys.py` (new): validator accepts
  `env_keys=["MY_MCP_KEY"]`, rejects `env_keys="X"` and `["bad-name"]`;
  `_custom_server_env` copies `MY_MCP_KEY` into the sink, refuses
  `JWT_SECRET`, ignores unset names, and the server dict carries no value.
- `apps/web-server/tests/test_agent_service_backend_env.py` (new):
  `_load_backend_env` on a temp `.env` with `JWT_SECRET=x`,
  `GRAPHITI_ENABLED=true` and a key already in `env`: result has
  `GRAPHITI_ENABLED`, not `JWT_SECRET`, and the existing key unchanged.

**Check:** `pytest tests/test_mcp_env_keys.py -q`;
`cd apps/web-server && pytest tests/test_agent_service_backend_env.py -q`;
ruff on touched files.

**Traps.** `env_keys` is only meaningful for `type: command`; ignore it for
`http`. Do not put values in the server dict: the SDK serialises it into
argv (#599).

## Step 6. Quiesce same-uid processes before the Job push (#1724)

**Files and lines:** `apps/backend/core/process_hardening.py` (append),
`apps/backend/core/workspace_fetch.py:113-119` (after the `wt.is_dir()`
check), test `tests/test_quiesce_before_push.py` (new).

**Change.**
- `process_hardening.py`:
  - `_stray_pids() -> list[int]`: walk `/proc/[0-9]*`; keep pids whose
    `status` `Uid:` real uid equals `os.getuid()`, whose `State:` is not
    `Z`, and that are not in the ancestor chain of `os.getpid()` (walk
    `PPid:` up to 0). Unreadable or vanished entries are skipped.
  - `kill_stray_processes(rounds: int = 5) -> bool`: up to `rounds` times,
    `os.kill(pid, SIGKILL)` each stray (suppress `ProcessLookupError`,
    `PermissionError`), `time.sleep(0.2)`; return True as soon as
    `_stray_pids()` is empty, else False.
  - `in_build_job() -> bool`: `JOB_ID` and `FACTORY_SERVICE` both non-empty.
- `workspace_fetch.maybe_push_workspace_branch`: after the worktree check,
  `if process_hardening.in_build_job() and not
  process_hardening.kill_stray_processes(): log error "agent-started
  processes survived; refusing to push (#1724)" and return False`.
  Only this push mints the GitHub token after the agent ran
  (`core/worktree.py:385` fetches at worktree setup, before the agent).

**Tests** (never call the real kill against the test machine):
1. `_stray_pids()` against the real `/proc`: start `subprocess.Popen(["sleep",
   "30"], start_new_session=True)`; its pid is in the list, `os.getpid()`
   and `os.getppid()` are not; then kill it yourself and `wait()`.
2. `kill_stray_processes` with `_stray_pids` patched to `[pid]` then `[]`
   and `os.kill` patched to record: returns True and killed `pid` with
   `SIGKILL`. Patched to always `[pid]`: returns False (and `time.sleep`
   patched out).
3. `maybe_push_workspace_branch` with `WORKSPACE_URI`, `JOB_ID`,
   `FACTORY_SERVICE` set, a real worktree dir, `kill_stray_processes`
   patched to False and `subprocess.run` patched to fail the test if
   called: returns False. With `JOB_ID` unset: `kill_stray_processes` is
   not called.

**Check:** `pytest tests/test_quiesce_before_push.py -q` plus existing
`pytest tests -q -k workspace_fetch`; ruff on touched files.

**Traps.** Never run `kill_stray_processes()` unpatched in a test: it would
kill the developer's own processes. Read `Uid:` from `/proc/<pid>/status`,
not `stat().st_uid`: a non-dumpable process's `/proc` dir is root-owned,
so an agent process could hide by calling `prctl`. Skip zombies (`Z`), or
killed children run.py never reaped would make the check fail forever.

## Step 7. `host_isolated()` stays narrow (#1760), comment and docstring

**Files and lines:** `apps/web-server/server/services/trusted_contract.py:35-38`,
`apps/web-server/tests/test_trusted_contract_merge.py:379`.

**Change.** Replace "Revisit when #1680 lands." with the reason it stays
narrow (#1760, 2026-10-17): in a PID-namespaced sandbox run.py and the
agent share the uid and the namespace, the agent can wait for the per-call
GitHub token there, and the sandbox cannot yet run the backend from `/home`
or `/nix`; widen once that changes. Docstring of
`test_pid_namespaced_sandbox_host_is_not_isolated`: same reason, one line.

**Check:** `cd apps/web-server && pytest tests/test_trusted_contract_merge.py -q`.

**Traps.** Comment and docstring only; no behaviour change.

## Rollback

Each step is its own commit; revert in reverse order. Steps 3 and 4 depend
on 1 and 2; 5, 6 and 7 are independent. Reverting step 2 alone restores
the deny list (`agent=True` callers would fail on the unknown kwarg, so
revert 3 and 4 first).

## Out of scope

Separate uid for agents (Option C, #363 end state); the local-runner /proc
window for the per-call token (#1724 stays open for it); widening
`host_isolated()` (#1760); the web-server insights providers (P6).
