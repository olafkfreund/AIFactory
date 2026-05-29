# AIFactory operator guides

This directory holds operator-facing runbooks, compliance evidence maps, security artifacts, and dashboards. It is intentionally separate from the user-facing concept documentation at [`../docs/docs/`](../docs/docs/) (rendered at <https://olafkfreund.github.io/AIFactory/>).

## Audience

- **Platform / SRE engineers** running AIFactory on a Kubernetes cluster.
- **Compliance + audit teams** mapping AIFactory's technical controls onto ISO 27001 / SOC 2 / GDPR frameworks.
- **Security architects** reviewing AIFactory's threat model.

## Deployment

- [`deployment/runbook.md`](deployment/runbook.md) — fresh install for EKS, AKS, GKE, and vanilla Kubernetes + Vault. Includes pre-flight checklist and verification gates.
- [`deployment/upgrade.md`](deployment/upgrade.md) — upgrade procedure between releases (v0.x → v1.0, v1.0 → v1.1, v1.1 → v1.2 preview). Covers forward-only migrations, `pg_dump` backups, and rollback constraints.

## Operations

- [`operations/image-mirroring.md`](operations/image-mirroring.md) — mirror AIFactory container images to private / air-gapped registries while preserving cosign signatures + SBOM + SLSA-3 provenance.

## Compliance

- [`compliance/iso27001-evidence.md`](compliance/iso27001-evidence.md) — ISO 27001 Annex A control-by-control evidence map (~31 controls directly evidenced as of v1.1).
- [`compliance/soc2-evidence.md`](compliance/soc2-evidence.md) — SOC 2 Trust Service Criteria evidence map (CC1-CC9 + A1 + C1).
- [`compliance/dpia-data-flow.md`](compliance/dpia-data-flow.md) — Data Protection Impact Assessment template with PII inventory, data-flow diagram, lawful-basis matrix, Art. 17 / Art. 30 mapping.

## Security

- [`security/threat-model.md`](security/threat-model.md) — STRIDE-per-component threat model covering Web pod, Agent pod, LiteLLM gateway, audit subsystem, and tenant namespace.

## Observability

- [`observability/grafana-aifactory.json`](observability/grafana-aifactory.json) — Grafana dashboard JSON with panels for HTTP request rate + latency, error rate, audit-row write rate, OIDC / SAML auth, per-org LLM budget, agent pod counts per tenant, OTel trace volume + p95.

## Operator workflows (legacy)

- [`HANDOVER_WORKFLOW.md`](HANDOVER_WORKFLOW.md) — handover patterns between operators + AIFactory's autonomous pipeline.
- [`CLAUDE_CODE_MCP_TOOLS.md`](CLAUDE_CODE_MCP_TOOLS.md) — MCP tool catalog reference.
- [`REMOTE_MCP_SERVER.md`](REMOTE_MCP_SERVER.md) — remote MCP server setup.
- [`session-logs/`](session-logs/) — session retrospectives + design narratives.

## Cross-references

- Concept docs (user-facing): [`../docs/docs/concepts/`](../docs/docs/concepts/)
- Design plans (per-feature internal rationale): [`../docs/plans/`](../docs/plans/)
- Drill scripts: [`../scripts/drills/`](../scripts/drills/) — `backup-restore.sh`, `upgrade-in-place.sh`, `image-mirroring.sh`. All three run `--dry-run` on every CI pass.
- LiteLLM dashboard (companion to the operations dashboard above): [`../charts/aifactory/dashboards/litellm.json`](../charts/aifactory/dashboards/litellm.json)

## Archive

The pre-Docusaurus `guides/` content was archived on 2026-05-26:

- Archive snapshot: [`../docs-archive/2026-05-26/guides/`](../docs-archive/2026-05-26/guides/)
- Use `git log --follow docs-archive/2026-05-26/guides/<file>.md` to trace the original history.

## Maintenance

Update this README whenever a new operator-facing document lands. Each entry above should be one sentence describing the audience + the answer it provides; the document itself carries the detail.
