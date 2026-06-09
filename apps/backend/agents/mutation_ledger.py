"""Checkpoint-before-mutation + per-turn mutation ledger (#476, Hermes-inspired).

AIFactory isolates work in a git worktree (good), but within a run there is no
per-mutation snapshot and no turn-end check that a write/patch actually did what
the agent claimed. Borrowing Hermes' tool-executor pattern
(``ensure_checkpoint(work_dir, "before ")`` + ``_record_file_mutation_result``):

  - **Checkpoint before mutation** — a cheap git snapshot in the worktree before
    each destructive tool call, so a corrupt mutation can be rolled back.
  - **Mutation ledger** — every Write/Edit/Bash records an entry to
    ``<spec_dir>/.aifactory/mutations.jsonl`` (the PostToolUse hook stamps the
    outcome).
  - **Turn-end verification** — compare the *claimed* mutated files against what
    actually changed on disk and flag mismatches.
  - **Handover evidence** — the ledger rides into the TFactory handoff payload.

Stdlib + ``git`` only. Checkpoints use ``git stash create`` (a snapshot commit
that does not touch HEAD, the index, or the working tree); brand-new untracked
files aren't in that snapshot but ARE recorded per-entry in the ledger, so
rollback can still delete them. Best-effort: a git hiccup never breaks the run.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "MUTATING_TOOLS",
    "ledger_enabled",
    "MutationLedger",
    "git_checkpoint",
    "rollback_to",
    "verify_turn",
    "mutation_target",
]

# Tools that can change the tree. Bash is included (it can `rm`/`mv`/write).
MUTATING_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"})

_LEDGER_REL = ".aifactory/mutations.jsonl"


def ledger_enabled() -> bool:
    """Off by default; opt in with AIFACTORY_MUTATION_LEDGER=true."""
    return (os.environ.get("AIFACTORY_MUTATION_LEDGER") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo``; return stdout (stripped), '' on error."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def git_checkpoint(repo: Path) -> str | None:
    """Cheap pre-mutation snapshot: a ``git stash create`` commit sha (or None
    when the tree is clean / not a repo). Does not modify HEAD, index, or tree."""
    sha = _git(Path(repo), "stash", "create")
    return sha or None


def _git_lines(repo: Path, *args: str) -> list[str]:
    """Git stdout split into lines WITHOUT stripping (porcelain leading spaces
    are significant). '' / error → []."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return out.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def rollback_to(repo: Path, checkpoint_sha: str) -> bool:
    """Restore the working tree to a checkpoint snapshot. Returns success.

    Force-checks-out the snapshot commit's tree over the working tree (overwrites
    a corrupt mutation), rather than ``stash apply`` which 3-way-merges and would
    conflict on an already-modified file.
    """
    if not checkpoint_sha:
        return False
    out = subprocess.run(
        ["git", "-C", str(repo), "checkout", checkpoint_sha, "--", "."],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.returncode == 0


def mutation_target(tool: str, tool_input) -> str | None:
    """Best-effort 'what does this call mutate' for the ledger."""
    if not isinstance(tool_input, dict):
        return None
    for k in ("file_path", "path", "notebook_path", "filePath"):
        if isinstance(tool_input.get(k), str):
            return tool_input[k]
    if tool == "Bash" and isinstance(tool_input.get("command"), str):
        return tool_input["command"][:120]
    return None


@dataclass
class MutationLedger:
    """Append-only per-turn ledger of agent mutations, at <spec_dir>/.aifactory/."""

    spec_dir: Path
    entries: list[dict] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return Path(self.spec_dir) / _LEDGER_REL

    def record(
        self,
        *,
        tool: str,
        target: str | None,
        ok: bool | None,
        checkpoint: str | None = None,
        tool_use_id: str | None = None,
        detail: dict | None = None,
    ) -> dict:
        entry = {
            "ts": round(time.time(), 3),
            "tool": tool,
            "target": target,
            "ok": ok,
            "checkpoint": checkpoint,
            "tool_use_id": tool_use_id,
            **({"detail": detail} if detail else {}),
        }
        self.entries.append(entry)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass
        return entry

    def read(self) -> list[dict]:
        """Read the persisted ledger (for handoff / verification)."""
        try:
            return [
                json.loads(ln)
                for ln in self.path.read_text().splitlines()
                if ln.strip()
            ]
        except (OSError, ValueError):
            return list(self.entries)

    def claimed_targets(self) -> set[str]:
        return {
            e["target"]
            for e in self.read()
            if e.get("tool") in {"Write", "Edit", "MultiEdit", "NotebookEdit"}
            and e.get("target")
        }


def verify_turn(spec_dir: Path, repo: Path) -> dict:
    """Turn-end check: do the file-mutating ledger entries match what actually
    changed in the worktree? Returns ``{ok, claimed, actual, missing, unexpected}``.

    ``missing``  — files the agent claimed to write but git shows unchanged
                   (a mutation that didn't take — the Hermes failure mode).
    ``unexpected`` — files changed on disk with no ledger entry (untracked drift).
    """
    ledger = MutationLedger(Path(spec_dir))
    claimed = {Path(t).name for t in ledger.claimed_targets()}
    # Porcelain is `XY<space>PATH` (3-char prefix); read unstripped so the first
    # line's leading status space isn't lost.
    actual = {
        Path(ln[3:].strip()).name
        for ln in _git_lines(Path(repo), "status", "--porcelain")
        if len(ln) > 3
    }
    missing = sorted(claimed - actual)
    unexpected = sorted(actual - claimed)
    out = {
        "ok": not missing,
        "claimed": sorted(claimed),
        "actual": sorted(actual),
        "missing": missing,
        "unexpected": unexpected,
    }
    try:
        (Path(spec_dir) / ".aifactory" / "mutation_verify.json").write_text(
            json.dumps(out, indent=2)
        )
    except OSError:
        pass
    return out
