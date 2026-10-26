"""Attribution values persisted when a task enters the system."""


def principal_attribution(principal: object) -> dict[str, str | None]:
    """Derive task attribution from a trusted auth principal."""
    if not isinstance(principal, dict):
        return {"originator": None, "agent_identity": None}

    principal_id = principal.get("id")
    originator = None
    if not principal.get("is_service") and principal_id != "default":
        originator = principal_id or principal.get("email")

    api_key_id = principal.get("api_key_id")
    return {
        "originator": originator,
        "agent_identity": f"agent:aifactory/{api_key_id}" if api_key_id else None,
    }


def issue_originator(provider: str | None, login: str | None) -> str | None:
    """Identify an issue author in the provider's namespace; null if either is unknown."""
    return f"{provider}:{login}" if provider and login else None
