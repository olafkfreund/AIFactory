---
status: approved
issue: 1632
intent: intent/2026-10-10-1632-openapi-feature-flags.md
---

# Spec: the OpenAPI spec must not depend on the generator's environment

## Design

The generator fixes the flag set itself: every env-gated router is off. The
published spec then describes the default deployment, whatever `.env`, shell or
working directory it runs from. Runtime code is not touched.

### 1. `scripts/generate-openapi-spec.py`

- Add `import os` next to `import sys` (line 14).
- Inside `main()`, before `from server.main import app` (line 26), add:

  ```python
      # Pin every env-gated router OFF so the spec is the default deployment's
      # API, independent of .env, the shell, or the cwd (#1632). Direct assignment
      # (not setdefault) beats server/env_bootstrap.py:27's setdefault, and pydantic
      # ranks process env above env_file (server/config.py:246-250).
      # Gates: main.py:523/527 (SAML/SCIM), mcp_remote/__init__.py:50, rmux/integration.py:45-56.
      # ponytail: fixed list; a fifth env-gated router must be added here (follow-up: two-env guard).
      os.environ.update(dict.fromkeys(
          ("SAML_ENABLED", "SCIM_ENABLED", "AIFACTORY_MCP_REMOTE_ENABLED",
           "AIFACTORY_RMUX_ENABLED", "APP_RMUX_ENABLED"), "false"))
  ```

- Line 29: `open(OUT, "w")` becomes `OUT.open("w", encoding="utf-8")`. The
  spec has non-ASCII text (the em dash in the API description, `main.py:423`),
  and `allow_unicode=True` writes it as-is. Without an explicit encoding the
  bytes depend on the locale, which breaks the intent's "byte-identical"
  outcome. (Taken from the robust design.)
- Docstring, under Usage (lines 8-9): one line saying the output is pinned to
  all feature flags off, so `.env`, exported flags and the working directory do
  not change it, and that `apps/web-server/static/` must not exist (see Risks).

The pin sits inside `main()`, not at module top, so importing the script has no
side effects. It runs before `server.main` is first imported, so it precedes
both `env_bootstrap` (imported first, `main.py:22`) and the module-level
`settings = Settings()` singleton (`config.py:394`) that `get_settings()`
returns.

Why this is enough:

- `env_bootstrap.py:27` uses `os.environ.setdefault`, so the pinned values win
  over `.env`.
- pydantic `Settings` (`config.py:246-250`, `env_file=".env"` against the cwd)
  ranks process env above the dotenv file, so `APP_RMUX_ENABLED=false` beats
  `APP_RMUX_ENABLED=true` from `.env.example` from any cwd.
- Exported shell flags are overwritten by the plain assignment.
- No other env-dependent branch in `main.py` changes paths: `settings.DEBUG`
  (426-427) only toggles `/docs` and `/redoc`, which are not in the schema;
  `REDIS_URL` and `SSL_ENABLED` (160, 279, 799) are runtime settings. The other
  `.env.example` keys (HOST, PORT, DEBUG, DEFAULT_SHELL, MAX_TERMINALS,
  MAX_CONCURRENT_TASKS) gate no router.

### 2. `.github/workflows/techdocs.yml` (comments and echo text only)

- Above the "Regenerate OpenAPI spec" step (line 160): a comment that the
  generator pins `SAML_ENABLED`, `SCIM_ENABLED`, `AIFACTORY_MCP_REMOTE_ENABLED`,
  `AIFACTORY_RMUX_ENABLED` and `APP_RMUX_ENABLED` off, so the spec is the
  default deployment.
- After the generator line in the fix hint (line 193): one `echo` saying the
  output does not depend on `.env` or exported flags, and that
  `apps/web-server/static/` must be absent.
- `continue-on-error` stays off (#906).

### 3. `apps/web-server/openapi.yaml`

Regenerate it. Commit it only if the bytes differ. Expected: 295 paths, no diff.

### Proposed answers to the intent's open questions

Proposed defaults for the user to confirm.

1. **Canonical flag set: all off.** All off is the default deployment and
   matches the committed spec (295 paths, no saml, scim or mcp-remote paths).
   It needs no extra CI dependencies. The values are assigned directly, not
   with `setdefault`, so they beat both loaders and exported shell flags.
   Predicates checked: `main.py:523`, `527`, `mcp_remote/__init__.py:50` and
   `rmux/integration.py:45-56`; mount sites `main.py:615` (mcp-remote) and
   `main.py:668` (rmux).
2. **Publish SAML, SCIM and remote-MCP, and depend on xmlsec?** This does not
   apply under all off. SAML is only imported when its flag is on
   (`main.py:523-526`), so CI's fresh venv (`techdocs.yml:162-165`) stays free
   of xmlsec and python3-saml. If anyone wants the full API published, that is
   a follow-up issue: "publish opt-in API surfaces to Backstage".
3. **Mark flag-gated routes?** No. Under all off no gated route is emitted, so
   there is nothing to mark. The always-mounted
   `/api/tasks/{task_id}/agent-console/sse` (`openapi.yaml:4779`) is not gated.
4. **Guard for a fifth env-gated router?** No new test in this change. The
   change is verified by hand (Verification below), and a follow-up issue
   covers an automated two-environment guard. The pin is five assignments with
   no branching, and the CI drift gate (`techdocs.yml:176-196`) already
   catches divergence between CI and the committed spec.
5. **#1631 and #1632 together?** Separately. They touch disjoint files (this
   change never edits `scripts/bump-version.js`). Whichever merges second
   regenerates `openapi.yaml` on top of the first. The same applies to #1671 if
   it adds routes.
6. **Will `openapi.yaml` change?** Probably not, because all off is what CI
   produces today. Regenerate it anyway and commit it only if the bytes differ.
7. **`techdocs.yml` changes?** Comment and hint text only, as described in
   section 2.
8. **New: lowercase flag variants (see Risks)?** Proposed: out of scope here,
   recorded as a known limit in Risks.

## Alternatives rejected

- **Remove the flag keys (`os.environ.pop`).** Both loaders run after the
  generator and would put them back from `.env`. Only an explicit `"false"`
  beats `setdefault` and dotenv.
- **Set the flags in the `techdocs.yml` step env.** This fixes CI only. Local
  runs would still drift.
- **`env_file=None` or a settings override in `config.py`, or skipping
  `env_bootstrap`.** These touch runtime code, which the intent forbids, and
  they leave exported shell flags unhandled.
- **All flags on.** It pulls xmlsec and python3-saml into CI and publishes
  routers that are off by default (Q1, Q2).
- **Load `server.env_bootstrap` first via `importlib`, then delete every
  case variant of the five keys before pinning (the robust design).** It closes
  the lowercase-key hole described in Risks, but it costs a helper, a constant,
  an `importlib` import and a second load-order dependency to cover a key
  spelling nobody uses (`.env.example` and all code use upper case). The
  smaller pin is kept. If the user wants the hole closed, this is the design
  to graft.
- **Run the generator in a child process with `env -i`.** It drops `PATH`, the
  venv and `APP_DISABLE_AUTH`, and adds a re-exec. More code for the same
  result.
- **Monkeypatch `get_settings` or `is_enabled`.** It couples the script to
  internal names, and the SAML and SCIM checks are inline `os.environ` reads.
- **`case_sensitive=True` in `Settings`.** It changes runtime behaviour.
- **`PYTHONUTF8=1` in CI instead of `encoding=`.** It fixes CI only.
- **A two-environment equality test now.** It needs a full venv and its own
  design. Deferred to a follow-up issue (Q4).
- **Spec markers or `x-` tags.** Nothing gated is emitted (Q3).
- **A shared helper.** No helper for this exists, and five names in one tuple
  is smaller than any abstraction.

## Risks

- **A built frontend changes the spec.** The flags do not cover this. The
  placeholder `GET /` (`main.py:711-717`, `openapi.yaml:10022`) is only
  registered when `apps/web-server/static/` is missing. After `npm run build`
  the directory exists, the developer gets 294 paths, and the drift gate fails.
  CI never has the directory (gitignored, `.gitignore:134`). Fixing this needs
  a `main.py` change (for example `include_in_schema=False`), which the intent
  rules out. Mitigation: the docstring and the fix hint say the directory must
  be absent, and a follow-up issue is filed.
- **Lowercase flag variants.** pydantic-settings is case-insensitive here
  (`config.py:246-250` sets no `case_sensitive`), and when two keys differ only
  in case, the last one in `os.environ` wins. An `app_rmux_enabled=true`
  exported in the shell, or written in `.env` (added after the pin by
  `env_bootstrap`'s `setdefault`), would still mount rmux. Nobody writes these
  keys in lower case today, so this is accepted as a known limit (proposed
  answer 8). The alternative above closes it if needed.
- **A fifth env-gated router added later** is not pinned. Mitigations: the
  `ponytail:` comment naming the gate lines, the CI drift gate, and the
  follow-up issue for an automated guard.
- **`os.environ` is changed in the generator process.** It is a one-shot
  script that starts no child processes, so nothing else sees the change.
- **Merge order with #1631 and #1671.** Whichever merges second regenerates
  `openapi.yaml`.
- No host or deployment is affected: the running server, Helm values and
  `.env.example` are not touched.

## Verification

Manual, from the worktree, with `apps/web-server/static/` absent and the
CI-style venv built as in `techdocs.yml:162-164` (`python3 -m venv /tmp/ws`,
then `pip install -r` the web-server and backend requirements):

1. Pre-fix baseline: with `cp apps/web-server/.env.example apps/web-server/.env`,
   run `APP_DISABLE_AUTH=true /tmp/ws/bin/python scripts/generate-openapi-spec.py`
   on the unfixed generator. Expect 297 paths. This proves the setup exercises
   the leak. Then `git checkout apps/web-server/openapi.yaml`.
2. With the fix and no `.env`: run the same command. Expect "295 paths", record
   `sha256sum apps/web-server/openapi.yaml`, and expect
   `git diff --exit-code apps/web-server/openapi.yaml` to be empty.
3. With the `.env` copy and all five flags exported as `true`: same hash.
4. From another cwd (`cd /tmp` and run the script by absolute path), and from
   `apps/web-server` with the `.env` present: same hash.
5. With a non-UTF-8 locale (`LC_ALL=en_US.ISO-8859-1 PYTHONUTF8=0`, if that
   locale is installed): same hash.
6. Clean up: remove the temporary `.env` and confirm `git status` is clean.

CI gates the change must pass:

- **Default ruff:** new lines are plain statements.
- **Strict ruff and mypy --strict ratchets** (`cq-ratchet.yml:107-113`, scope
  `scripts/*.py`): `main() -> int` is already annotated and the new code adds
  no untyped defs, no `Any` and no new imports that mypy cannot resolve.
  `OUT.open(..., encoding="utf-8")` removes a PTH123 finding (`PTH` is
  selected in `standards/ruff.toml:26`) rather than adding one. The per-file
  counts cannot rise. Run locally as CI does:
  `apps/backend/.venv/bin/python scripts/cq_ratchet.py --base origin/main
  --ruff apps/backend/.venv/bin/ruff --config standards/ruff.toml --paths
  'scripts/*.py'`, and the matching mypy step.
- **CodeQL:** only constant `"false"` values are written, and no env value is
  printed or logged.
- **Autonomy matrix `--check`** (`scripts/gen_autonomy_matrix.py`): no policy
  module is touched. Run `python scripts/gen_autonomy_matrix.py --check`
  anyway.
- **techdocs `refresh-and-validate`:** green, with no diff after regeneration.

Follow-up issues to file: (a) an automated two-environment spec-equality guard;
(b) "publish opt-in API surfaces to Backstage"; (c) the spec depends on
`apps/web-server/static/` being absent (fix with `include_in_schema=False` or a
constant `/` route).
