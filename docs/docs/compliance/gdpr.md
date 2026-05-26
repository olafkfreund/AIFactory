---
title: GDPR
sidebar_position: 2
---

# GDPR & Privacy

AIFactory is designed to be GDPR-compliant out of the box for self-hosted deployments. The DPIA (Data Protection Impact Assessment) is checked into `docs-archive/2026-05-26/guides/compliance/dpia-data-flow.md`.

## What data AIFactory stores

| Category | Examples | Where |
|---|---|---|
| **User identity** | Email, name (from OIDC profile) | Postgres `users` table |
| **Code artifacts** | Spec files, plans, QA reports | `.aifactory/specs/` on disk |
| **Audit log** | API calls, who-did-what-when | Postgres `audit_logs` table |
| **API secrets** | Encrypted (KMS) at rest | Postgres `llm_endpoints` table |
| **LLM transcripts** | Agent ↔ LLM messages | `apps/backend/.aifactory/logs/` (configurable retention) |

## What AIFactory does NOT store

- The user's **code** — that lives in the user's repo, not in AIFactory's database
- **Browser fingerprints**, location data, tracking pixels
- **Third-party analytics** — no Google Analytics, no Segment, no PostHog calls

## Data subject rights

| Right | How |
|---|---|
| **Access** | `GET /api/users/{id}/data-export` returns all rows for that user in JSON |
| **Erasure** | `DELETE /api/users/{id}` cascades to all owned tasks; audit log entries are redacted (body discarded, chain preserved) |
| **Portability** | The export from "Access" is JSON, transportable to another instance |
| **Rectification** | Edit via the UI; changes are logged |

## Data transfer

If you deploy AIFactory self-hosted, no data leaves your network except:

- **LLM provider API calls** — to whatever endpoint you configured. Pin to EU regions for GDPR-friendly providers (Anthropic EU, Azure OpenAI EU, OpenAI EU data residency).
- **GitHub API calls** — for issue/PR sync. Limited to repos you explicitly link.

## DPO contact

If you self-host: you are the controller. The DPIA template is in `docs-archive/`. Fill in your DPO contact and your processor list.

If you use a managed AIFactory deployment (when available): see your service-specific privacy notice.
