"""Minimum-viable SCIM filter parser per RFC 7644 §3.4.2 (Epic #35 #41).

Supported grammar (intentionally small):
    <attribute> eq "<value>"

Where ``<attribute>`` is one of:
    userName | externalId | active

Examples that parse:
    userName eq "alice@corp.com"
    externalId eq "1234"
    active eq true
    active eq false

Examples that DO NOT parse (return 400 invalidFilter from the route layer):
    userName co "alice"           — operator not in our subset
    userName eq "alice" and ...   — composition not supported
    (a eq b) or (c eq d)          — grouping not supported

Why so small
------------

The Helm / SAML design doc decision #5 calls this out explicitly: Okta
+ Azure AD only ever send these 3 attributes with ``eq``. Implementing
the full RFC 7644 grammar is ~500 LOC of state-machine and bugs;
supporting the bare subset is 30 LOC. Operators who report a SCIM-sync
break on a more-exotic IdP get a 400 + invalidFilter and we expand
this parser in v1.2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ALLOWED_ATTRIBUTES = frozenset({"userName", "externalId", "active"})

# Regex matches: <attr> eq "<quoted-value>"  OR  <attr> eq true|false
# (whitespace flexible; case-insensitive for the operator only).
_FILTER_RE = re.compile(
    r'^\s*(?P<attr>\w+)\s+(?i:eq)\s+'
    r'(?:"(?P<str_value>[^"]*)"|(?P<bool_value>true|false))\s*$',
)


@dataclass(frozen=True)
class ScimFilter:
    """Parsed filter — caller dispatches on (attribute, value)."""

    attribute: str
    value: str | bool


class ScimFilterInvalid(ValueError):
    """Raised when the filter doesn't match our minimum-viable grammar.

    Routes layer catches + returns 400 + scimType=invalidFilter."""


def parse(filter_str: str) -> ScimFilter:
    """Parse ``filter`` query-string value into a ``ScimFilter``.

    Raises ``ScimFilterInvalid`` on:
      - empty input
      - operator other than ``eq``
      - attribute outside the allowed set
      - any composition (and / or / parens)
      - malformed quoting
    """
    if not filter_str or not filter_str.strip():
        raise ScimFilterInvalid("empty filter")

    match = _FILTER_RE.match(filter_str)
    if not match:
        raise ScimFilterInvalid(
            f"unsupported filter syntax — only `<attr> eq <value>` is "
            f"supported (got: {filter_str!r})"
        )

    attr = match.group("attr")
    if attr not in _ALLOWED_ATTRIBUTES:
        raise ScimFilterInvalid(
            f"attribute {attr!r} is not filterable; supported: "
            f"{sorted(_ALLOWED_ATTRIBUTES)}"
        )

    str_value = match.group("str_value")
    bool_value = match.group("bool_value")

    if str_value is not None:
        # Quoted string. `active` should not be quoted in real-world
        # use, but we accept either form rather than 400-ing on a
        # client that quotes booleans.
        if attr == "active":
            if str_value.lower() not in ("true", "false"):
                raise ScimFilterInvalid(
                    f"active must be true/false; got {str_value!r}"
                )
            return ScimFilter(attribute=attr, value=str_value.lower() == "true")
        return ScimFilter(attribute=attr, value=str_value)

    # Bare true/false
    if attr != "active":
        raise ScimFilterInvalid(
            f"{attr} value must be quoted string; got bareword {bool_value!r}"
        )
    return ScimFilter(attribute=attr, value=bool_value == "true")
