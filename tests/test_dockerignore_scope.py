"""Doc-only trees must not ship in the production image (#1105).

`Dockerfile:174` is `COPY . /home/projects/MagesticAI/`, so anything not in
`.dockerignore` lands in the runtime image. `docs/` and `tests/` were excluded;
`docs-archive/` (44 files) was missed and shipped.

This is small in bytes and real in principle: the image is the artefact the
supply-chain gates scan and sign, so every file in it is attack surface and
SBOM noise that nothing at runtime reads.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _ignored() -> set[str]:
    text = (_REPO / ".dockerignore").read_text(encoding="utf-8")
    return {
        line.strip().rstrip("/")
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_doc_only_trees_are_excluded_from_the_image() -> None:
    ignored = _ignored()
    missing = [d for d in ("docs", "docs-archive", "tests") if d not in ignored]
    assert not missing, (
        f"these doc/test trees are copied into the production image by "
        f"Dockerfile's `COPY . ...` because .dockerignore omits them: {missing}"
    )


def test_scripts_is_deliberately_not_excluded() -> None:
    """Guards the reasoning, so a later tidy-up does not break the MCP path.

    `scripts/` looks like the same class as docs-archive and is NOT excluded on
    purpose: apps/ references `scripts/start-aifactory-mcp.sh` at runtime, so a
    directory-level exclusion would remove a file the image actually needs.
    """
    assert "scripts" not in _ignored(), (
        "scripts/ was excluded wholesale — but scripts/start-aifactory-mcp.sh is "
        "referenced at runtime. Exclude individual files instead."
    )
