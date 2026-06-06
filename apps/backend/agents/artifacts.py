"""
Build Artifacts + Async Review (Phase 6 of the self-healing pipeline;
tracking #397)
====================================================================

Antigravity-style **Artifacts**: tangible, reviewable deliverables the build
emits as it works — the plan, a per-wave diff summary, the QA report, the
security report. A human can read them at a glance and **comment without halting
the agent**; comments route into the existing inbox (``agents/inbox.py``) and the
coder folds them into the next turn (already wired via ``drain_inbox``). This is
the feedback channel for the Phase 4 ``ASYNC`` review tier.

Artifacts live in ``<spec_dir>/artifacts/`` with an ``index.json`` manifest, and
are mirrored to the source spec so the web UI (reading the main project) can
render them during a worktree build. Pure file I/O — never changes build flow.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .inbox import post_message

logger = logging.getLogger(__name__)

ARTIFACTS_DIRNAME = "artifacts"
INDEX_FILENAME = "index.json"

# Known artifact kinds (free-form is allowed; these are the pipeline's defaults).
KIND_PLAN = "plan"
KIND_WAVE_DIFF = "wave-diff"
KIND_QA = "qa"
KIND_SECURITY = "security"


@dataclass
class Artifact:
    kind: str
    name: str  # file stem, e.g. "plan" or "wave-1-diff"
    title: str
    path: str  # relative to spec dir, e.g. "artifacts/plan.md"


class ArtifactWriter:
    """Writes build artifacts + a manifest, mirrored to the source spec."""

    def __init__(self, spec_dir: Path, source_spec_dir: Path | None = None,
                 now: Any = None) -> None:
        self.spec_dir = Path(spec_dir)
        self.source_spec_dir = Path(source_spec_dir) if source_spec_dir else None
        self._now = now or (lambda: datetime.now().isoformat())

    def _dirs(self) -> list[Path]:
        dirs = [self.spec_dir]
        if self.source_spec_dir and self.source_spec_dir != self.spec_dir:
            dirs.append(self.source_spec_dir)
        return dirs

    def write(self, kind: str, title: str, content: str, *, name: str | None = None) -> Artifact:
        """Write/overwrite an artifact markdown file and update the manifest."""
        stem = name or kind
        rel = f"{ARTIFACTS_DIRNAME}/{stem}.md"
        artifact = Artifact(kind=kind, name=stem, title=title, path=rel)
        body = f"# {title}\n\n_Updated: {self._now()}_\n\n{content.rstrip()}\n"
        for base in self._dirs():
            try:
                (base / ARTIFACTS_DIRNAME).mkdir(parents=True, exist_ok=True)
                (base / rel).write_text(body, encoding="utf-8")
            except OSError as exc:  # artifacts must never break a build
                logger.debug("artifact write failed (%s): %s", base / rel, exc)
        self._update_index(artifact)
        return artifact

    def add_wave_diff(self, wave_num: int, summary: str) -> Artifact:
        return self.write(
            KIND_WAVE_DIFF,
            f"Wave {wave_num} — diff summary",
            summary,
            name=f"wave-{wave_num}-diff",
        )

    def _update_index(self, artifact: Artifact) -> None:
        for base in self._dirs():
            index_path = base / ARTIFACTS_DIRNAME / INDEX_FILENAME
            try:
                entries = self._read_index(index_path)
                entries = [e for e in entries if e.get("name") != artifact.name]
                entries.append(
                    {
                        "kind": artifact.kind,
                        "name": artifact.name,
                        "title": artifact.title,
                        "path": artifact.path,
                        "updated_at": self._now(),
                    }
                )
                index_path.parent.mkdir(parents=True, exist_ok=True)
                index_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            except OSError as exc:
                logger.debug("artifact index write failed (%s): %s", index_path, exc)

    @staticmethod
    def _read_index(index_path: Path) -> list[dict[str, Any]]:
        if not index_path.exists():
            return []
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def list_artifacts(self) -> list[dict[str, Any]]:
        return self._read_index(self.spec_dir / ARTIFACTS_DIRNAME / INDEX_FILENAME)


def list_artifacts(spec_dir: Path) -> list[dict[str, Any]]:
    """Read the artifact manifest for a spec (empty if none)."""
    return ArtifactWriter._read_index(
        Path(spec_dir) / ARTIFACTS_DIRNAME / INDEX_FILENAME
    )


def post_artifact_feedback(
    spec_dir: Path, artifact_name: str, comment: str, *, sender: str = "user"
) -> dict[str, Any]:
    """Route a human comment on an artifact into the agent's inbox (async review).

    The agent picks it up between turns (coder.py ``drain_inbox``) and folds it
    into the current task — feedback without halting, the Phase 4 ASYNC tier.
    """
    text = f"[review:{artifact_name}] {comment.strip()}"
    return post_message(spec_dir, text, sender=sender, source="artifact-review")
