"""P7 — Evidence / compliance / drill acceptance tests.

Ten tests verify that all the closing-deliverables of Epic #26 v1.0
exist + contain the mandatory sections. Document quality is reviewed
in the PR; tests gate the structural contract.

Mapping to issue #34 acceptance:
  Docs (4 required):
    1. SOC2 evidence            (→ P7.1)
    2. DPIA / data-flow diagram (→ P7.2)
    3. STRIDE threat model      (→ P7.3)
    4. Deployment runbook       (→ P7.4)
    5. Upgrade guide            (→ P7.5)
  Drills (3 required):
    6. backup-restore            (→ P7.6)
    7. upgrade-in-place          (→ P7.6)
    8. image-mirroring           (→ P7.6 + existing P0 doc)
  Cross-references (gate the docs are actually wired):
    9. guides/README.md indexes the 5 new docs
   10. Each drill script is executable + responds to --help
"""

from __future__ import annotations

import pytest


@pytest.mark.evidence
@pytest.mark.skip(reason="P7.1 pending: SOC2 evidence")
def test_soc2_evidence_doc_exists(repo_root) -> None:
    """soc2-evidence.md exists + covers CC1..CC9."""
    pytest.fail("P7.1 not landed")


@pytest.mark.evidence
@pytest.mark.skip(reason="P7.2 pending: DPIA")
def test_dpia_doc_exists(repo_root) -> None:
    """dpia-data-flow.md exists with PII inventory + lawful-basis matrix."""
    pytest.fail("P7.2 not landed")


@pytest.mark.evidence
@pytest.mark.skip(reason="P7.3 pending: threat model")
def test_threat_model_doc_exists(repo_root) -> None:
    """threat-model.md exists with STRIDE pass + named limitations."""
    pytest.fail("P7.3 not landed")


@pytest.mark.evidence
@pytest.mark.skip(reason="P7.4 pending: deployment runbook")
def test_deployment_runbook_exists(repo_root) -> None:
    """runbook.md exists with EKS / AKS / GKE / vanilla install paths."""
    pytest.fail("P7.4 not landed")


@pytest.mark.evidence
@pytest.mark.skip(reason="P7.5 pending: upgrade guide")
def test_upgrade_guide_exists(repo_root) -> None:
    """upgrade.md exists with v0.x→v1.0 procedure + rollback."""
    pytest.fail("P7.5 not landed")


@pytest.mark.evidence
@pytest.mark.skip(reason="P7.6 pending: backup-restore drill")
def test_backup_restore_drill_script(repo_root) -> None:
    """backup-restore.sh exists, is executable, --help works."""
    pytest.fail("P7.6 not landed")


@pytest.mark.evidence
@pytest.mark.skip(reason="P7.6 pending: upgrade-in-place drill")
def test_upgrade_in_place_drill_script(repo_root) -> None:
    """upgrade-in-place.sh exists, is executable, --help works."""
    pytest.fail("P7.6 not landed")


@pytest.mark.evidence
@pytest.mark.skip(reason="P7.6 pending: image-mirroring drill")
def test_image_mirroring_drill_script(repo_root) -> None:
    """image-mirroring.sh exists, is executable, --help works."""
    pytest.fail("P7.6 not landed")


@pytest.mark.evidence
@pytest.mark.skip(reason="P7.1-P7.5 pending: index cross-references")
def test_guides_readme_indexes_all_new_docs(repo_root) -> None:
    """guides/README.md links to the 5 new P7 docs."""
    pytest.fail("not landed")


@pytest.mark.evidence
@pytest.mark.skip(reason="P7.6 pending: dry-run drill execution")
def test_backup_restore_drill_dry_run(repo_root, tmp_path) -> None:
    """backup-restore.sh --dry-run exits 0 + prints planned actions."""
    pytest.fail("P7.6 not landed")
