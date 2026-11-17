---
status: draft
issue: 2079
spec: spec/2026-11-17-2079-gitops-siem-wiring.md
coders:
  1: you
  2: you
  3: you
  4: you
reviewers:
  - codex
---

# Plan: wire AIFactory auth-failure SIEM push in gitops

All work is in factory-gitops (branch feat/2079-aifactory-siem-env, base main). Decisions: see the spec (dedicated ingest user and optional Secret, URL set, alert query unchanged, push inert until the Secret exists). Small steps, edited by the lead (below the handoff threshold; reason for `coders`).

## Steps

1. `apps/aifactory/manifests/manifests.yaml` after the `AIFACTORY_AGENT_COMMIT_TRAILERS` line (~339): add the three env entries described in the spec → verify by `python3 -c "import yaml;list(yaml.safe_load_all(open(...)))"` and grep.
   Traps: Argo syncs this live; the Secret is `optional: true`.
2. `apps/security-alerts/manifests/test_alerts.py`: add the sqlite fixture test → verify by running the file.
   Traps: SQL must stay unchanged in `alerts.py`.
3. `scripts/mint-audit-ingest-user.sh` (new, executable) → verify by `bash -n` and `DRY_RUN=1`. Order: Secret first, then user create-or-reset, then rollout restart and status.
   Traps: never echo passwords; read root login only via curl `-K -`; fail on non-2xx.
4. `docs/audit-siem-forwarding.md`: AIFactory section with owner commands (script, restart) and the proof request (invalid Bearer on a protected endpoint, audit_chain query, alert run) → verify by reading.

## Tests

`python3 apps/security-alerts/manifests/test_alerts.py` exits 0; repo CI checks green. Live proof is the owner/after-rollout step in the spec.

## Rollback

Revert the commit; the Secret, if created, is harmless.
