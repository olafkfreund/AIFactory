"""
Magestic AI project initialization utilities.

Handles first-time setup of .aifactory directory and ensures proper gitignore configuration.
"""

from pathlib import Path


def ensure_gitignore_entry(project_dir: Path, entry: str = ".aifactory/") -> bool:
    """
    Ensure an entry exists in the project's .gitignore file.

    Creates .gitignore if it doesn't exist.

    Args:
        project_dir: The project root directory
        entry: The gitignore entry to add (default: ".aifactory/")

    Returns:
        True if entry was added, False if it already existed
    """
    gitignore_path = project_dir / ".gitignore"

    # Check if .gitignore exists and if entry is already present
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        lines = content.splitlines()

        # Check if entry already exists (exact match or with trailing newline variations)
        entry_normalized = entry.rstrip("/")
        for line in lines:
            line_stripped = line.strip()
            # Match both ".aifactory" and ".aifactory/"
            if (
                line_stripped == entry
                or line_stripped == entry_normalized
                or line_stripped == entry_normalized + "/"
            ):
                return False  # Already exists

        # Entry doesn't exist, append it
        # Ensure file ends with newline before adding our entry
        if content and not content.endswith("\n"):
            content += "\n"

        # If our comment section already exists, append right after it
        comment = "# Magestic AI data directory"
        if comment in content:
            content += entry + "\n"
        else:
            content += "\n" + comment + "\n" + entry + "\n"

        gitignore_path.write_text(content)
        return True
    else:
        # Create new .gitignore with the entry
        content = "# Magestic AI data directory\n"
        content += entry + "\n"

        gitignore_path.write_text(content)
        return True


def init_magestic_ai_dir(project_dir: Path) -> tuple[Path, bool]:
    """
    Initialize the .aifactory directory for a project.

    Creates the directory if needed and ensures it's in .gitignore.

    Args:
        project_dir: The project root directory

    Returns:
        Tuple of (magestic_ai_dir path, gitignore_was_updated)
    """
    project_dir = Path(project_dir)
    magestic_ai_dir = project_dir / ".aifactory"
    magestic_ai_dir.mkdir(parents=True, exist_ok=True)

    # Unconditional (#1185). This used to be gated on a `.gitignore_checked`
    # marker, so once the marker existed the entry was never re-checked: a
    # rebase, a merge that took theirs, or a tidy-up that dropped `.aifactory/`
    # from .gitignore silently retired the control, and the False return was
    # indistinguishable from "already correct". Same defect shape as #1172.
    # The marker bought nothing: ensure_gitignore_entry is idempotent and
    # exact-line-matched, and one read_text of a small file costs about what
    # the marker's stat did.
    gitignore_updated = ensure_gitignore_entry(project_dir, ".aifactory/")

    # Runtime root-level file written by agents during execution; must never be
    # committed. `.aifactory-status` was retired to `.aifactory/status.json` by
    # #1106, so it is no longer appended here — nothing writes it any more and
    # the factory should not inject a dead pattern into a repo it does not own.
    if ensure_gitignore_entry(project_dir, ".aifactory-security.json"):
        gitignore_updated = True

    return magestic_ai_dir, gitignore_updated


def get_magestic_ai_dir(project_dir: Path, ensure_exists: bool = True) -> Path:
    """
    Get the .aifactory directory path, optionally ensuring it exists.

    Args:
        project_dir: The project root directory
        ensure_exists: If True, create directory and update gitignore if needed

    Returns:
        Path to the .aifactory directory
    """
    if ensure_exists:
        magestic_ai_dir, _ = init_magestic_ai_dir(project_dir)
        return magestic_ai_dir

    return Path(project_dir) / ".aifactory"
