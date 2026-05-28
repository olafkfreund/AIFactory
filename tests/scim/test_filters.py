"""Tests for the minimum-viable SCIM filter parser (Epic #35 #41)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.scim.filters import ScimFilterInvalid, parse


class TestSupportedFilters:
    """The three filter shapes Okta + Azure AD actually send."""

    def test_username_eq(self):
        f = parse('userName eq "alice@corp.com"')
        assert f.attribute == "userName"
        assert f.value == "alice@corp.com"

    def test_external_id_eq(self):
        f = parse('externalId eq "okta-12345"')
        assert f.attribute == "externalId"
        assert f.value == "okta-12345"

    def test_active_true(self):
        f = parse("active eq true")
        assert f.attribute == "active"
        assert f.value is True

    def test_active_false(self):
        f = parse("active eq false")
        assert f.attribute == "active"
        assert f.value is False

    def test_active_quoted_form_accepted(self):
        """Some clients quote booleans. Don't 400 on them."""
        f = parse('active eq "true"')
        assert f.attribute == "active"
        assert f.value is True

    def test_operator_case_insensitive(self):
        """Spec says operators are case-insensitive."""
        f = parse('userName EQ "x"')
        assert f.attribute == "userName"


class TestRejectedFilters:
    """Anything outside our subset → ScimFilterInvalid → 400 invalidFilter."""

    def test_empty(self):
        with pytest.raises(ScimFilterInvalid, match="empty"):
            parse("")

    def test_whitespace_only(self):
        with pytest.raises(ScimFilterInvalid, match="empty"):
            parse("   ")

    def test_unsupported_operator_co(self):
        with pytest.raises(ScimFilterInvalid):
            parse('userName co "alice"')

    def test_unsupported_operator_sw(self):
        with pytest.raises(ScimFilterInvalid):
            parse('userName sw "alice"')

    def test_unsupported_attribute(self):
        """`displayName` is a real SCIM attribute but we don't make
        it filterable — keeps the surface small."""
        with pytest.raises(ScimFilterInvalid, match="filterable"):
            parse('displayName eq "Alice"')

    def test_composition_and_rejected(self):
        """No `and`/`or` composition in v1.1."""
        with pytest.raises(ScimFilterInvalid):
            parse('userName eq "alice" and active eq true')

    def test_parens_rejected(self):
        with pytest.raises(ScimFilterInvalid):
            parse('(userName eq "alice")')

    def test_malformed_no_operator(self):
        with pytest.raises(ScimFilterInvalid):
            parse('userName "alice"')

    def test_malformed_unquoted_value(self):
        with pytest.raises(ScimFilterInvalid):
            parse("userName eq alice")

    def test_active_with_non_boolean_rejected(self):
        with pytest.raises(ScimFilterInvalid, match="true/false"):
            parse('active eq "maybe"')

    def test_username_with_bare_bool_rejected(self):
        """Bareword bool only valid on `active`. `userName eq true`
        is almost certainly a mistake."""
        with pytest.raises(ScimFilterInvalid):
            parse("userName eq true")
