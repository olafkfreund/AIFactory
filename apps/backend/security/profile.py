"""
Security Profile Management
============================

Manages security profiles for projects, including caching and validation.
Uses project_analyzer to create dynamic security profiles based on detected stacks.
"""

from pathlib import Path

from project_analyzer import SecurityProfile, get_or_create_profile

# =============================================================================
# GLOBAL STATE
# =============================================================================

# #1944: .aifactory-security.json is agent-writable, so it is output only, never an
# input. The hook's profile lives in-process, built once by fresh detection.
_trusted_profiles: dict[tuple[Path, Path | None], SecurityProfile] = {}


def get_security_profile(
    project_dir: Path, spec_dir: Path | None = None
) -> SecurityProfile:
    """
    Get the security profile for a project.

    Built once per (project_dir, spec_dir) by fresh detection and then kept in
    memory. The profile file on disk is never read back, so edits to it by the
    agent have no effect; plan seeding mutates this same object.

    Args:
        project_dir: Project root directory
        spec_dir: Optional spec directory

    Returns:
        SecurityProfile for the project
    """
    project_dir = Path(project_dir).resolve()
    resolved_spec_dir = Path(spec_dir).resolve() if spec_dir else None
    key = (project_dir, resolved_spec_dir)
    if key not in _trusted_profiles:
        _trusted_profiles[key] = get_or_create_profile(
            project_dir, spec_dir, force_reanalyze=True
        )
    return _trusted_profiles[key]


def reset_profile_cache() -> None:
    """Reset the in-memory profiles (useful for testing or re-analysis)."""
    _trusted_profiles.clear()
