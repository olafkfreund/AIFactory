---
status: draft
issue: 1960
intent: intent/2026-10-30-1960-repo-scoped-correlation-key.md
---

# Spec: never emit a bare issue number as the correlation key

## Design

- `correlation_key(spec_id, issue_number, repo)`: return `<repo>#<n>` when
  both are known, else `af-<spec_id>`. Every caller already passes the spec id,
  so intake, task rows and completion events stay consistent.
- `_event_correlation_key(req, spec_id)` in `tfactory_client.py` mirrors it:
  issue + repo gives `<repo>#<n>`; issue without repo gives `af-<spec_id>`;
  no issue stays None (synthetic key stays service-local, as today).
  `_thread_issue` passes the spec id it already has at its call site.

## Alternatives rejected

- Derive the repo from the project's `gitRepo`/origin when the spec lacks it:
  a payload-supplied issue need not be from the project's repo, and a project
  can name more than one repo (gitRepo vs fork origin). A wrong repo
  misattributes a work item, which is worse than a key that does not join.
  Intake already stores a declared, validated repo (#1900), so new specs carry
  one when the caller declares it.
- Fail loudly when the repo is missing: would break legacy specs mid-build.

## Risks

A legacy spec with an issue but no repo now gets a new key mid-run: its earlier
events (bare key) and later ones (`af-`) split into two CFactory rows. Bare keys
are already unmatched after CFactory #506, so nothing joins today either.

## Verification

Unit tests: missing repo never yields a bare number; repo present unchanged;
handoff key equals event key for every requirements shape.
