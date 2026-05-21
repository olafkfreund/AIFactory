"""Centralized path helpers for AIFactory data directory."""
import shutil
from pathlib import Path

AI_FACTORY_DIR = Path.home() / ".aifactory"


def migrate_legacy_data():
    """Safely migrate legacy AIFactory data folder to AIFactory."""
    legacy_dir = Path.home() / ".aifactory"
    if legacy_dir.exists() and not AI_FACTORY_DIR.exists():
        try:
            shutil.copytree(legacy_dir, AI_FACTORY_DIR, dirs_exist_ok=True)
            print(f"AIFactory - Successfully migrated legacy data from {legacy_dir} to {AI_FACTORY_DIR}")
        except Exception as e:
            print(f"AIFactory - Warning: failed to migrate legacy data: {e}")


# Run migration automatically on module load
migrate_legacy_data()


def get_data_dir() -> Path:
    """Return the AIFactory data directory, creating it if needed."""
    AI_FACTORY_DIR.mkdir(parents=True, exist_ok=True)
    return AI_FACTORY_DIR


def get_data_file(filename: str) -> Path:
    """Get a file path in the AIFactory data directory."""
    return get_data_dir() / filename
