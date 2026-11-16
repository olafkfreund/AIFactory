---
status: approved
issue: 2059
intent: intent/2026-11-16-2059-transcript-followup.md
---

# Spec: transcript follow-up (caller, redaction, RFC status)

## Design

1. TFactory new `agents/regression/notify.py` with `notify_aifactory(config_spec_dir,
   project_id, commit, run_id, repo_root, diff)`, called from `trigger.run_for_project`
   and from the CLI `run` command (which calls `run_regression` directly). It POSTs only
   when all hold: `diff.has_regressions`; `commit` set; `<spec_dir>/context/source.json`
   has `aifactory.task_id` and `aifactory.api_url`; `TFACTORY_PUBLIC_URL` is https;
   and `git -C repo_root rev-parse HEAD` starts with `commit` (the tested checkout is the
   reported sha; no git or a mismatch means skip, logged).
2. Request: `{merge_sha, run_url}` to `{api_url}/api/tasks/{task_id}/post-merge-regression`,
   `run_url = {TFACTORY_PUBLIC_URL}/api/projects/{project}/regression?run={run_id}`
   (run-specific link; the portal may ignore the query and show the latest run).
   Bearer via `send._auth_headers`, `assert_safe_target_url` first, and redirects are
   refused (an opener whose redirect handler raises) so the token cannot follow a 3xx to
   another origin. Best effort: any failure is logged, never fails the run.
3. HTTP route and MCP tool build a config with no `spec_dir`/`commit`, so they stay inert;
   the CLI and nightly job (which can pass `--commit` and `--spec-dir`) are the surfaces
   that report. Accepted: wiring task and commit into the portal route is a separate change.
4. Factory hub: add `anthropic-api-key` pattern `sk-ant-[A-Za-z0-9_-]{10,}` plus a test.
5. RFC-0004 header: phase 1 and the revert path implemented; governed capture remains.

## Alternatives rejected

- A new TFactory route or event feed: more surface, and the route in AIFactory exists.
- Failing the run when the call fails: the revert is advice to the owner, not a gate.
- Re-vendoring `factory_common` in the consumers now: pins are by SHA, so they stay
  green and move when each repo next bumps.

## Risks

A non-merge commit is refused by AIFactory (pipeline merge record, 409), logged by the
caller. A stale checkout is caught by the HEAD check in step 1.

## Verification

TFactory unit tests with an injected sender (called once on regression with run-specific URL; HEAD mismatch skipped; redirect refused; not called without
commit, without regressions, without https URL, or without an aifactory envelope; sender
error does not raise). Hub test for the redaction. `ruff` and pytest per repo CI.
