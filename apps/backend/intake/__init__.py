"""Label-driven intake (RFC-0011).

Turns a labelled issue/work-item into a tier-driven execution profile and,
via the poller + ``/api/tasks/from-issue`` endpoint, into a governed build.
"""

from .execution_block import build_execution_block

__all__ = ["build_execution_block"]
