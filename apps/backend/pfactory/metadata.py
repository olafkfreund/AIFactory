"""Parse PFactory's machine-readable metadata into AIFactory (epic #327, #330).

Every PFactory-emitted issue body ends with an HTML-comment block:

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

The same object is written to ``.aifactory/specs/<plan_id>/requirements.json``
under ``metadata`` — preferred when present (full fidelity, no parsing).

Everything here is tolerant: a missing block, malformed YAML, or an
unknown/old ``taxonomy`` version degrades to ``None`` / partial data and never
raises — pickup must survive whatever PFactory (or a hand-filer) emits.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

__all__ = [
    "load_pfactory_metadata",
    "parse_pfactory_meta",
    "render_pfactory_context",
]

_log = logging.getLogger(__name__)

# Match the `<!-- pfactory:meta … -->` comment block (non-greedy, DOTALL).
_META_RE = re.compile(r"<!--\s*pfactory:meta\s*(.*?)-->", re.DOTALL | re.IGNORECASE)

# Keys that mark a dict as PFactory metadata (vs AIFactory's own
# requirements.json["metadata"] which holds phase-model config).
_PFACTORY_KEYS = ("plan_id", "plan_type", "citations", "cost_monthly_usd", "taxonomy")


def parse_pfactory_meta(text: object) -> dict | None:
    """Extract + parse the ``pfactory:meta`` block from a body of text.

    Returns the parsed dict, or ``None`` if no block is present or the block
    is not valid YAML / not a mapping.
    """
    if not isinstance(text, str):
        return None
    match = _META_RE.search(text)
    if not match:
        return None
    body = match.group(1).strip()
    if not body:
        return None
    try:
        import yaml

        data = yaml.safe_load(body)
    except Exception:  # noqa: BLE001 — malformed metadata must never crash pickup
        _log.warning("Failed to parse pfactory:meta block", exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _looks_like_pfactory_meta(obj: object) -> bool:
    return isinstance(obj, dict) and any(k in obj for k in _PFACTORY_KEYS)


def load_pfactory_metadata(
    spec_dir: Path | str, requirements: dict | None = None
) -> dict | None:
    """Load PFactory metadata for a spec, preferring the highest-fidelity source.

    Resolution order:
      1. ``requirements.json["metadata"]`` (PFactory-written, full fidelity)
      2. the ``pfactory:meta`` block in the issue body (``requirements["description"]``)
      3. the ``pfactory:meta`` block in ``spec.md``

    ``requirements`` may be passed in to avoid a re-read; otherwise it is loaded
    from ``spec_dir``. Returns ``None`` when no metadata is found.
    """
    spec = Path(spec_dir)
    if requirements is None:
        req_file = spec / "requirements.json"
        if req_file.exists():
            try:
                requirements = json.loads(req_file.read_text())
            except (json.JSONDecodeError, OSError):
                requirements = None

    if isinstance(requirements, dict):
        meta = requirements.get("metadata")
        if _looks_like_pfactory_meta(meta):
            return meta
        from_body = parse_pfactory_meta(requirements.get("description"))
        if from_body is not None:
            return from_body

    spec_md = spec / "spec.md"
    if spec_md.exists():
        try:
            return parse_pfactory_meta(spec_md.read_text())
        except OSError:
            return None
    return None


def render_pfactory_context(meta: object) -> str:
    """Render PFactory metadata as a markdown block for the planner context.

    Only fields that are present are rendered, so partial / old-version metadata
    still produces useful context. Returns ``""`` when there is nothing to show.
    """
    if not isinstance(meta, dict) or not meta:
        return ""

    lines: list[str] = [
        "## PFactory Governance Context",
        "",
        "This spec was planned and approved by PFactory (its architecture, "
        "security, best-practice, and feasibility gates passed and a human "
        "approved it). Honour its findings and cite the same sources.",
        "",
    ]

    def _add(label: str, value: object) -> None:
        if value is not None and value != "":
            lines.append(f"- **{label}:** {value}")

    _add("Plan ID", meta.get("plan_id"))
    _add("Plan type", meta.get("plan_type"))
    _add("Category", meta.get("category"))
    _add("Priority", meta.get("priority"))
    _add("Risk", meta.get("risk"))
    _add("Estimated monthly cost (USD)", meta.get("cost_monthly_usd"))
    _add("Effort (points)", meta.get("effort_points"))
    _add("Effort (days)", meta.get("effort_days"))
    _add("Access verified", meta.get("access_verified"))
    if meta.get("taxonomy"):
        _add("Taxonomy", meta.get("taxonomy"))

    citations = meta.get("citations")
    if isinstance(citations, list) and citations:
        lines.extend(["", "### Citations", ""])
        for i, c in enumerate(citations, 1):
            if isinstance(c, dict):
                why = c.get("why", "")
                source = c.get("source", "")
                uri = c.get("uri", "")
                tail = " — ".join(p for p in (source, uri) if p)
                lines.append(f"{i}. {why}" + (f" ({tail})" if tail else ""))
            else:
                lines.append(f"{i}. {c}")

    return "\n".join(lines).rstrip() + "\n"
