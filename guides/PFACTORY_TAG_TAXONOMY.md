# PFactory Tag Taxonomy (v1)

> Status: v1 · Tracking epic [#327](https://github.com/olafkfreund/AIFactory/issues/327)

PFactory (the planning-and-governance layer) emits **governed** GitHub epics and
child issues once dual approval is reached (AI gates pass **and** a human
approves). AIFactory recognises, classifies, and picks these up via a shared
**tag taxonomy** — the "secret language" between the two systems.

The authoritative contract lives in the PFactory repo at `docs/tag-taxonomy.md`.
This note records the AIFactory side: which labels we create vs reuse, and where
the machine-readable metadata is parsed.

## Labels

Create the taxonomy labels in this repo with the idempotent script:

```bash
scripts/create-pfactory-labels.sh                 # current repo
scripts/create-pfactory-labels.sh --repo owner/name
scripts/create-pfactory-labels.sh --dry-run
```

### New — created by `scripts/create-pfactory-labels.sh`

| Label | Colour | Meaning |
|-------|--------|---------|
| `pfactory` | `#5319E7` | **Mandatory marker** — present on every PFactory-emitted issue |
| `handoff:aifactory` | `#1D76DB` | Route to AIFactory for execution |
| `handoff:tfactory` | `#0E8A16` | Route to TFactory for test generation |
| `type:software` `type:feature` `type:infra` `type:hosting` `type:testing` `type:cicd` `type:product` | `#C5DEF5` | Work category |
| `plan-type:software-service` `plan-type:data-pipeline` `plan-type:infra-change` `plan-type:generic-deliverable` | `#BFDADC` | PFactory plan-type descriptor |
| `priority:p0` `priority:p1` `priority:p2` `priority:p3` | `#D93F0B` → `#0E8A16` | Execution priority (p0 = critical path first) |

> `type:feature` already exists in this repo; the script re-creates it with
> `--force` (idempotent) under the shared `#C5DEF5` category colour.

### Reused — already in this repo (NOT created by the script)

| Label | Meaning |
|-------|---------|
| `epic` | Large body of work tracked via sub-issues — a PFactory epic is `epic` + `pfactory` |
| `sev:critical` `sev:high` `sev:medium` `sev:low` | Severity routing (reuse existing handling) |
| `backend` `frontend` `mcp` `security` | Component areas |

> The legacy `priority:high` / `priority:medium` / `priority:low` scheme is
> **distinct** from the PFactory `priority:p0..p3` scheme and is left untouched.

## `pfactory:meta` block

Every PFactory-emitted issue body ends with a machine-readable HTML comment, and
the same object is written to `.aifactory/specs/<plan_id>/requirements.json`
under `metadata`:

```
<!-- pfactory:meta
plan_id: ...
plan_type: ...
category: ...
priority: p1
risk: medium
cost_monthly_usd: 2492.58
effort_points: 39
effort_days: [15.6, 39.0]
access_verified: true
citations:
  - why: "..."
    uri: "..."
    source: "..."
taxonomy: v1
-->
```

`taxonomy: v1` lets AIFactory branch on version and degrade gracefully on
unknown/missing versions.

## Pickup behaviour (downstream issues)

| Issue | Behaviour |
|-------|-----------|
| [#329](https://github.com/olafkfreund/AIFactory/issues/329) | Ingest `pfactory` + `handoff:aifactory` issues as **governed** specs — skip AIFactory's own up-front planning/approval gate; epic children are the executable units. Non-`pfactory` issues are unaffected. |
| [#330](https://github.com/olafkfreund/AIFactory/issues/330) | Parse the `pfactory:meta` block + `requirements.json` metadata (cost, effort, access, citations) into the spec / planner context. |
| [#331](https://github.com/olafkfreund/AIFactory/issues/331) | Map `priority:*` → scheduling, `type:*` → track/agent selection, `sev:*` → severity routing, `type:testing` / `handoff:tfactory` → TFactory; surface `citations[]` in planner context. |
