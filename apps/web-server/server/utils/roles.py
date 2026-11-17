"""Org role hierarchy, kept import-free so auth modules can use it without a cycle."""

ROLE_LEVELS: dict[str, int] = {
    "viewer": 0,
    "member": 1,
    "admin": 2,
    "owner": 3,
}


def _role_level(role: str) -> int:
    """Return the numeric level for a role string, defaulting to -1."""
    return ROLE_LEVELS.get(role, -1)
