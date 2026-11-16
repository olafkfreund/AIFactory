---
status: draft
issue: 2059
spec: spec/2026-11-16-2059-transcript-followup.md
coders:
  1: you   # small tricky logic, one file plus a test; the lead writes it
  2: you
  3: you
reviewers:
  - codex   # final diff review per repo (agy is off by owner decision)
---

# Plan: transcript follow-up

Approved decisions: see the spec (caller in the shared trigger, best effort, inert without
a public https URL; hub `anthropic-api-key` pattern; RFC-0004 status). Out of scope and
left open on #2059: run id in transcript key, spec-creation transcripts, chunked upload,
multi-replica revert idempotency.

## Steps

1. TFactory new `apps/backend/agents/regression/notify.py` per spec design 1-3; call it
   from the end of `trigger.run_for_project` and from the CLI `run` command
   (`cli.py` ~117, keep its impact-selection args); tests in
   `tests/test_regression_notify.py` → `pytest tests/test_regression_notify.py tests/test_regression_trigger.py`.
   Traps: reuse `agents.handback.send._auth_headers`, `assert_safe_target_url`; no
   redirects; ruff; no new dependency (urllib).
2. Factory `shared/factory-common/factory_common/secrets.py`: pattern + test in
   `shared/factory-common/tests/test_secrets.py` → pytest that file.
   Traps: stdlib only; consumers keep their pinned copies.
3. Factory `docs/rfc/0004-governed-trajectory-capture.md`: status header → docs check
   used by the repo CI.

## Tests

Per step above, then each repo's CI commands.

## Rollback

Revert the PRs; the caller is inert without `TFACTORY_PUBLIC_URL`.
