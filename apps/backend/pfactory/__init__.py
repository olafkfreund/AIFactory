"""PFactory integration — AIFactory's side of the PFactory pickup contract.

PFactory (the planning-and-governance layer) emits governed GitHub epics and
child issues tagged with a shared tag taxonomy (v1). This package recognises and
classifies those issues so AIFactory can ingest them as governed specs.

See guides/PFACTORY_TAG_TAXONOMY.md and epic #327.
"""

from .epics import extract_child_issue_numbers
from .metadata import (
    load_pfactory_metadata,
    parse_pfactory_meta,
    render_pfactory_context,
)
from .routing import CODER, TFACTORY, priority_rank, routing_target
from .taxonomy import (
    Classification,
    classify_labels,
    classify_requirements,
    is_governed_requirements,
)
from .tfactory_client import build_handoff_payload, send_handoff, tfactory_config

__all__ = [
    "CODER",
    "TFACTORY",
    "Classification",
    "build_handoff_payload",
    "classify_labels",
    "classify_requirements",
    "extract_child_issue_numbers",
    "is_governed_requirements",
    "load_pfactory_metadata",
    "parse_pfactory_meta",
    "priority_rank",
    "render_pfactory_context",
    "routing_target",
    "send_handoff",
    "tfactory_config",
]
