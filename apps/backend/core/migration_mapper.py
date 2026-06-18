"""Rewrite-mode support for language migrations (RFC-0010 Phase 7).

When the signed contract carries ``change_mode == "migration"``, AIFactory must
NOT edit the existing code in its native language. Instead it:

* generates in the **target** language (``resolve_generation_language``), never
  the repo's detected language;
* treats the legacy source as a **read-only reference oracle** (mounted under
  ``.aifactory/oracle/`` and excluded from the editable set);
* scaffolds the target in a new coexisting crate/dir (default placement) so the
  original keeps running as the parity oracle during transition;
* feeds the coder a per-module brief (source → target path) derived from the
  contract's ``tfactory.equivalence.module_map``.

Pure helpers + a thin workspace-prep orchestrator; the heavy lifting (cloning,
coding) lives in the existing build flow, which calls these.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ORACLE_DIRNAME = ".aifactory/oracle"


def load_contract(spec_dir: Path | str) -> dict[str, Any] | None:
    """Best-effort load the signed Task Contract stashed by the trusted-plan ingest.

    Returns None when absent/unreadable (the common non-trusted-plan path), so
    callers degrade to the normal build flow. Never raises.
    """
    try:
        path = Path(spec_dir) / "context" / "task_contract.json"
        if path.is_file():
            doc = json.loads(path.read_text(encoding="utf-8"))
            return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None
    return None


# Minimal scaffold per target language: (manifest filename, manifest text, src dir).
_CARGO_TOML = """[package]
name = "{crate}"
version = "0.1.0"
edition = "2021"

[dependencies]
"""


def is_migration(contract: dict[str, Any] | None) -> bool:
    """True when the contract is a language migration."""
    return bool(contract) and contract.get("change_mode") == "migration"


def resolve_generation_language(contract: dict[str, Any] | None) -> str | None:
    """The language the coder must GENERATE in for a migration, else None.

    None means "not a migration — fall back to repo detection" (unchanged
    behaviour). For a migration the target language wins over whatever the repo
    happens to be, fixing the "generates in the repo's native language" trap.
    """
    if not is_migration(contract):
        return None
    env = contract.get("environment") or {}
    return env.get("target_language") or env.get("language")


def module_map(contract: dict[str, Any] | None) -> dict[str, str]:
    """source module path → target module path, from the equivalence block."""
    if not contract:
        return {}
    eq = (contract.get("tfactory") or {}).get("equivalence") or {}
    mm = eq.get("module_map") or {}
    return {str(k): str(v) for k, v in mm.items()}


@dataclass
class ModuleBrief:
    """One unit of rewrite work handed to the coder."""

    source_module: str  # repo-relative path of the legacy module (oracle)
    target_module: str  # repo-relative path to generate
    source_excerpt: str = ""  # the legacy source (read-only reference)


def module_briefs(
    contract: dict[str, Any] | None,
    *,
    oracle_root: Path | None = None,
    max_chars: int = 8000,
) -> list[ModuleBrief]:
    """Build per-module rewrite briefs, attaching the legacy source as reference.

    ``oracle_root`` is where the legacy source is mounted; when given, each
    brief carries the source module's text (truncated) as the behavioral
    reference the coder must preserve.
    """
    briefs: list[ModuleBrief] = []
    for src, tgt in module_map(contract).items():
        excerpt = ""
        if oracle_root is not None:
            fp = oracle_root / src
            if fp.is_file():
                excerpt = fp.read_text(encoding="utf-8", errors="replace")[:max_chars]
        briefs.append(
            ModuleBrief(source_module=src, target_module=tgt, source_excerpt=excerpt)
        )
    return briefs


def mount_oracle(worktree: Path, project_dir: Path) -> Path:
    """Copy the legacy source into ``<worktree>/.aifactory/oracle/`` (reference).

    The coder's allowlist excludes this path, so the legacy source is read-only
    context (the parity oracle), never edited. Returns the oracle root.
    """
    oracle = Path(worktree) / ORACLE_DIRNAME
    oracle.mkdir(parents=True, exist_ok=True)

    def _ignore(_dir: str, names: list[str]) -> set[str]:
        return {
            n
            for n in names
            if n in {".git", ".aifactory", "node_modules", "target", "__pycache__"}
        }

    for entry in Path(project_dir).iterdir():
        if entry.name in {".git", ".aifactory"}:
            continue
        dest = oracle / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, dirs_exist_ok=True, ignore=_ignore)
        else:
            shutil.copy2(entry, dest)
    (oracle / "README.READONLY").write_text(
        "RFC-0010 migration oracle: the legacy source, READ-ONLY. Do NOT edit — "
        "it is the behavioral reference the rewrite must match.\n",
        encoding="utf-8",
    )
    return oracle


def scaffold_target(worktree: Path, contract: dict[str, Any]) -> list[Path]:
    """Create the target crate/dir skeleton + stub files for each target module.

    Default placement: a new coexisting crate (the original stays untouched). Stub
    files are created so the coder fills them in; returns the created paths.
    """
    created: list[Path] = []
    mm = module_map(contract)
    if not mm:
        return created
    # Infer the crate root from the first target path, e.g. rust/port/src/...
    first = next(iter(mm.values()))
    parts = first.split("/")
    lang = (resolve_generation_language(contract) or "rust").lower()
    if lang == "rust" and "src" in parts:
        crate_root = Path(worktree) / "/".join(parts[: parts.index("src")])
        crate = crate_root.name
        cargo = crate_root / "Cargo.toml"
        if not cargo.exists():
            crate_root.mkdir(parents=True, exist_ok=True)
            cargo.write_text(_CARGO_TOML.format(crate=crate), encoding="utf-8")
            created.append(cargo)
    for tgt in mm.values():
        fp = Path(worktree) / tgt
        if not fp.exists():
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(
                f"// RFC-0010 migration target (generate me).\n"
                f"// Behavioral reference: see {ORACLE_DIRNAME}/.\n",
                encoding="utf-8",
            )
            created.append(fp)
    return created


def prepare_migration_workspace(
    worktree: Path, project_dir: Path, contract: dict[str, Any]
) -> dict[str, Any]:
    """Mount the read-only oracle + scaffold the target. Returns a summary.

    No-op (empty summary) when the contract is not a migration.
    """
    if not is_migration(contract):
        return {}
    oracle = mount_oracle(worktree, project_dir)
    created = scaffold_target(worktree, contract)
    return {
        "oracle_root": str(oracle),
        "target_language": resolve_generation_language(contract),
        "scaffolded": [str(p) for p in created],
        "briefs": [vars(b) for b in module_briefs(contract, oracle_root=oracle)],
    }
