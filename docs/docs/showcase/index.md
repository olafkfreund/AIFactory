---
title: Showcase
sidebar_position: 1
slug: /showcase/
---

# Showcase

Real outputs from the AIFactory pipeline.

## A finished spec

The planner-generated spec for "Add `/healthz` endpoint" looks like this:

```markdown
# Add /healthz endpoint

> Spec: 001-add-healthz-endpoint
> Created: 2026-05-26
> Status: Ready for Implementation

## Overview

Add a HTTP GET `/healthz` endpoint to the FastAPI app that returns
`{"status": "ok"}` with HTTP 200 when the service is up.

## User Stories

### Operators can probe service liveness

As an SRE, I want to point my load balancer's health check at /healthz,
so that unhealthy instances are removed from rotation automatically.

## Spec Scope

1. **Add /healthz handler** — Returns 200 + JSON status when service is up.
2. **Unit test** — pytest case asserting 200 + correct shape.

## Out of Scope

- Detailed dependency health (database, downstream APIs)
- Authentication on /healthz (probes should be unauthenticated)

## Expected Deliverable

1. `curl http://localhost:8000/healthz` returns 200 + `{"status": "ok"}`
2. `pytest -k healthz` passes
```

## A finished implementation plan

```json
{
  "subtasks": [
    {
      "id": "1.1",
      "title": "Write unit test for /healthz",
      "files": ["tests/test_healthz.py"],
      "status": "done"
    },
    {
      "id": "1.2",
      "title": "Add /healthz route to src/app/main.py",
      "files": ["src/app/main.py"],
      "status": "done"
    },
    {
      "id": "1.3",
      "title": "Run pytest -k healthz to verify",
      "files": [],
      "status": "done"
    }
  ]
}
```

## A finished QA report

```markdown
# QA Report — 001-add-healthz-endpoint

**Verdict:** ✅ Approved

## Acceptance criteria

- [x] `curl /healthz` returns 200 + `{"status": "ok"}` — verified
- [x] `pytest -k healthz` passes — verified

## Notes

- Route uses FastAPI's `@app.get` decorator with `response_model=HealthResponse`,
  consistent with existing routes in this codebase
- Test uses TestClient (FastAPI standard), matches the project's test style

Approved for merge.
```

## See it run

See the [demo walkthrough](/showcase/demo-walkthrough) for a step-by-step screencast of all of this happening in real time.
