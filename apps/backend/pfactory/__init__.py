"""PFactory integration — AIFactory's side of the PFactory pickup contract.

PFactory (the planning-and-governance layer) emits governed GitHub epics and
child issues tagged with a shared tag taxonomy (v1). This package recognises and
classifies those issues so AIFactory can ingest them as governed specs.

See guides/PFACTORY_TAG_TAXONOMY.md and epic #327.
"""

from .metadata import (
    load_pfactory_metadata,
    parse_pfactory_meta,
    render_pfactory_context,
)
from .taxonomy import (
    Classification,
    classify_labels,
    classify_requirements,
    is_governed_requirements,
)

__all__ = [
    "Classification",
    "classify_labels",
    "classify_requirements",
    "is_governed_requirements",
    "load_pfactory_metadata",
    "parse_pfactory_meta",
    "render_pfactory_context",
]
