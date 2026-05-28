"""SCIM 2.0 Pydantic schemas per RFC 7643 (Epic #35 #41).

Scope: User + Group resources only. ServiceProviderConfig + Schema +
ResourceTypes are exposed as static JSON from the routes layer (no
Pydantic models needed — they're declared once and never vary).

Why Pydantic
------------

FastAPI's auto-validation gives us most of RFC 7644 §3.12's error
shapes for free. We just need to declare the structures.

Naming
------

Field names match the spec exactly (camelCase). Pydantic v2's
``alias`` is used so Python code uses snake_case while the JSON
wire format stays canonical.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_RESPONSE_SCHEMA = (
    "urn:ietf:params:scim:api:messages:2.0:ListResponse"
)
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_PATCH_OP_SCHEMA = (
    "urn:ietf:params:scim:api:messages:2.0:PatchOp"
)


class ScimMeta(BaseModel):
    """RFC 7643 §3.1 common attributes."""

    resource_type: str = Field(alias="resourceType")
    created: str | None = None
    last_modified: str | None = Field(default=None, alias="lastModified")
    location: str | None = None
    version: str | None = None

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class ScimEmail(BaseModel):
    """RFC 7643 §4.1.2 — multi-valued email attribute."""

    value: str
    type: str | None = None  # "work" / "home" / etc.
    primary: bool | None = None

    model_config = ConfigDict(populate_by_name=True)


class ScimName(BaseModel):
    """RFC 7643 §4.1.1 — complex 'name' attribute."""

    given_name: str | None = Field(default=None, alias="givenName")
    family_name: str | None = Field(default=None, alias="familyName")
    formatted: str | None = None
    middle_name: str | None = Field(default=None, alias="middleName")

    model_config = ConfigDict(populate_by_name=True)


class ScimUser(BaseModel):
    """RFC 7643 §4.1 — User resource.

    We don't model every spec attribute — only the ones AIFactory
    actually consumes + the ones Okta/Azure AD reliably send."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_USER_SCHEMA])
    id: str | None = None  # server-assigned on create
    external_id: str | None = Field(default=None, alias="externalId")
    user_name: str = Field(alias="userName")
    name: ScimName | None = None
    display_name: str | None = Field(default=None, alias="displayName")
    emails: list[ScimEmail] = Field(default_factory=list)
    active: bool = True
    meta: ScimMeta | None = None

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


class ScimGroupMember(BaseModel):
    """RFC 7643 §4.2 — multi-valued 'members' attribute on Group."""

    value: str  # User id
    display: str | None = None  # display name (informational)
    type: str | None = None  # "User" usually; "Group" for nested

    model_config = ConfigDict(populate_by_name=True)


class ScimGroup(BaseModel):
    """RFC 7643 §4.2 — Group resource."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_GROUP_SCHEMA])
    id: str | None = None
    external_id: str | None = Field(default=None, alias="externalId")
    display_name: str = Field(alias="displayName")
    members: list[ScimGroupMember] = Field(default_factory=list)
    meta: ScimMeta | None = None

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# List response
# ---------------------------------------------------------------------------


class ScimListResponse(BaseModel):
    """RFC 7644 §3.4.2 — paginated list result."""

    schemas: list[str] = Field(
        default_factory=lambda: [SCIM_LIST_RESPONSE_SCHEMA],
    )
    total_results: int = Field(alias="totalResults")
    start_index: int = Field(default=1, alias="startIndex")
    items_per_page: int = Field(alias="itemsPerPage")
    resources: list[dict] = Field(default_factory=list, alias="Resources")

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


class ScimPatchOperation(BaseModel):
    """RFC 7644 §3.5.2 — one op in a PATCH request.

    ``op`` is parsed case-insensitively in the routes layer (Azure AD
    sends "Add" capitalised).
    """

    op: Literal["add", "replace", "remove", "Add", "Replace", "Remove"]
    path: str | None = None  # required for remove, optional otherwise
    value: object = None  # heterogeneous: str | dict | list

    model_config = ConfigDict(populate_by_name=True)


class ScimPatchRequest(BaseModel):
    schemas: list[str] = Field(
        default_factory=lambda: [SCIM_PATCH_OP_SCHEMA],
    )
    operations: list[ScimPatchOperation] = Field(alias="Operations")

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class ScimError(BaseModel):
    """RFC 7644 §3.12 — typed error body returned by the routes layer
    for every non-2xx response so SCIM clients can dispatch on
    ``scimType``."""

    schemas: list[str] = Field(default_factory=lambda: [SCIM_ERROR_SCHEMA])
    status: str  # HTTP status, as STRING per spec
    detail: str
    scim_type: str | None = Field(default=None, alias="scimType")

    model_config = ConfigDict(populate_by_name=True)
