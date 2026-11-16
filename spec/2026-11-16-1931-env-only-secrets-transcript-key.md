---
status: draft
issue: 1931
intent: intent/2026-11-16-1931-env-only-secrets-transcript-key.md
---

# Spec: env-only secrets switch and project-qualified transcript key

## Design

1. `config.py`: new setting `REQUIRE_ENV_SECRETS: bool = False` (env `APP_REQUIRE_ENV_SECRETS`). In `Settings.__init__`, before the generate branches: if set and `API_TOKEN` or `JWT_SECRET` is empty, raise `ValueError` naming the missing env var (never a value).
2. `core/workspace_fetch.py`: `maybe_push_transcripts(spec_id)` keys by `os.environ.get("JOB_ID") or spec_id`. `transcript_uris(spec_id, job_id=None)` lists by `job_id or spec_id`. `completion.py` passes `task_id`. `routes/tasks_regression.py` `_transcript_uris` regex (line ~27) must accept `:` in the key, or project-qualified URIs are dropped and revert PRs say "none recorded" (Codex review, confirmed). `revert_pr.py` already words the caveat as earlier-run only: no change.

The guard checks resolved settings (env, `.env` or constructor), not process-env provenance: it stops file generation, nothing more.

## Alternatives rejected

- Default-on switch keyed on `KUBERNETES_SERVICE_HOST`: could crash non-prod in-cluster installs.
- New `AIFACTORY_RUN_ID` env from the control plane: a new contract; JOB_ID already carries project and spec.
- Run id in the key (fixes "previous run's URI"): needs per-dispatch id agreement; stays in the follow-up issue.

## Risks

Transcript links already recorded in `task_metadata.json` under the old key are not cleared (`_record_transcript_uris` ignores an empty list); the isolation guarantee covers newly recorded links only. Stale links age out with the 90-day evidence TTL.

Subprocess-backend transcripts with S3 set are keyed by spec id while the control plane lists by task id, so they are not linked (local dev and any in-cluster subprocess setup; build Jobs are covered). Old transcripts under the spec-id prefix stop being listed (evidence TTL is 90 days).

## Verification

Unit tests: config raises with the switch on and env empty, passes with env set, unchanged by default; transcript key contains `JOB_ID`, listing by task id ignores another project's same spec id. Run the touched test modules with pytest and ruff.
