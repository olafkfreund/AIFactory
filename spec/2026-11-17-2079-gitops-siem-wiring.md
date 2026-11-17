---
status: approved
issue: 2079
intent: intent/2026-11-17-2079-gitops-siem-wiring.md
---

# Spec: wire AIFactory auth-failure SIEM push in gitops

## Design

1. `apps/aifactory/manifests/manifests.yaml`, `aifactory` container env: `AIFACTORY_AUDIT_SIEM_URL=http://observe.factory.svc.cluster.local:5080` (not secret); `AIFACTORY_AUDIT_SIEM_USER` and `AIFACTORY_AUDIT_SIEM_PASSWORD` from Secret `observe-audit-ingest` keys `user` and `password`, `optional: true`. `audit_siem.siem_config()` returns None unless URL, user and password are all non-empty, so a missing Secret means no push. Org and stream use the code defaults (`default`, `audit_chain`), which are what the alert reads.
2. `scripts/mint-audit-ingest-user.sh`: owner-run. Reads the root login from `observe-root` through a curl config on stdin, generates a random password, deletes and recreates the OpenObserve user, then applies Secret `observe-audit-ingest` (HTTP non-2xx fails the script; secrets travel via a private temp dir, not argv). A rerun converges on matching credentials after a partial failure (until it does, a running pod only logs a failed push). It ends with `rollout restart deploy/aifactory` and `rollout status`, because env from a newly created Secret is read only at container start. Prints no secret; `DRY_RUN=1` prints the plan only.
3. `apps/security-alerts/manifests/test_alerts.py`: run the SQL that `evaluate` builds against an in-memory sqlite table holding AIFactory-shaped and CFactory-shaped rows; assert only `auth.failure` rows count.
4. `docs/audit-siem-forwarding.md`: AIFactory source section with the owner steps and the proof request.

## Alternatives rejected

- Reuse `observe-root` in the pod: no extra owner step, but root credentials in an app pod.
- A second forwarder CronJob: rejected earlier (cursor is CFactory's `max(id)`, AIFactory ids are UUIDs).
- Change the alert query: unneeded, the pushed row already has `action`.

## Risks

Open-source OpenObserve may offer only coarse roles, so the user may not be strictly ingest-only; the script uses the least role that can write to a stream and the doc says so. A wrong password only logs a warning in AIFactory; auth is unaffected (best-effort push).

## Verification

`python3 apps/security-alerts/manifests/test_alerts.py`; `kubectl kustomize apps/aifactory/manifests` (or yaml parse) shows the env; `bash -n` and `DRY_RUN=1` for the script; after the owner step and rollout, one request with a non-empty invalid `Authorization: Bearer x` header to a protected endpoint (not a health route, not a missing header), then an `audit_chain` query for a fresh `source = 'aifactory' AND action = 'auth.failure'` row, then one `security-alerts` run (`kubectl create job --from=cronjob/security-alerts`) whose `auth_failure_spike` record is no longer `no_field`. sqlite proves only the SQL, not ingestion.
