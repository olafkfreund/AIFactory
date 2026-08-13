"""The OAuth result page must ADDRESS its postMessage, not broadcast it (#1285).

`_oauth_result_html` renders the page the OAuth popup finishes on. It posts the
outcome -- which carries the connected email address and the provider -- back to
`window.opener`. With a `'*'` targetOrigin the browser delivers that payload to
whatever is loaded in the opener at that moment, and the popup's lifetime spans
a full third-party OAuth round trip, so "whatever is loaded" is not something
this page gets to assume.

The receiving half was fixed in #1283 (the listener checks `event.origin`
against an allowlist). That stops a forged INBOUND message. These tests pin the
outbound half: the half that stops the address leaving at all.

The origin is taken from `CORS_ORIGINS`, never from a request header -- a
targetOrigin an attacker can set is `'*'` with extra steps.
"""

from __future__ import annotations

from server.routes import email

PORTAL = "https://portal.example.com"


def _render(monkeypatch, origins: list[str]) -> str:
    """Render the result page with ``origins`` configured, returning its HTML."""
    settings = email.get_settings()
    monkeypatch.setattr(settings, "CORS_ORIGINS", origins)
    response = email._oauth_result_html(
        success=True,
        message="Connected",
        email="alice@example.com",
        provider="outlook",
    )
    return response.body.decode()


def test_the_configured_origin_is_the_target(monkeypatch) -> None:
    html = _render(monkeypatch, [PORTAL])
    assert f"window.opener.postMessage(payload, '{PORTAL}');" in html


def test_the_wildcard_target_origin_is_gone(monkeypatch) -> None:
    """The bug itself. ``'*'`` must not appear as a postMessage target."""
    html = _render(monkeypatch, [PORTAL])
    assert "postMessage(payload, '*')" not in html
    assert ", '*')" not in html


def test_every_configured_origin_is_addressed_individually(monkeypatch) -> None:
    """One targeted post per configured origin, rather than one guess.

    A deployment may legitimately serve the portal from more than one origin,
    and postMessage takes exactly one target. The browser delivers only the
    call whose origin matches the opener, so the rest are silent no-ops -- which
    is why the loop is correct where picking the first entry would be a guess.
    """
    second = "https://portal.internal.example"
    html = _render(monkeypatch, [PORTAL, second])
    assert f"window.opener.postMessage(payload, '{PORTAL}');" in html
    assert f"window.opener.postMessage(payload, '{second}');" in html


def test_a_wildcard_in_the_configured_origins_is_dropped_not_forwarded(
    monkeypatch,
) -> None:
    """``APP_CORS_ORIGINS='*'`` must not become a wildcard targetOrigin.

    The CORS layer tolerates a wildcard by dropping credentials; that tolerance
    must not leak into postMessage, where the wildcard IS the vulnerability.
    """
    html = _render(monkeypatch, ["*", PORTAL])
    assert ", '*')" not in html
    assert f"window.opener.postMessage(payload, '{PORTAL}');" in html


def test_no_configured_origin_means_no_post_at_all(monkeypatch) -> None:
    """Fail closed: render the outcome, post nothing. Never fall back to '*'."""
    html = _render(monkeypatch, [])
    assert "postMessage" not in html
    assert "Connected" in html


def test_the_address_is_not_in_a_page_that_posts_nowhere_useful(monkeypatch) -> None:
    """The payload still carries the address, so the target is what protects it.

    Stated as a test so nobody 'fixes' #1285 by deleting the email field and
    calls the leak closed: the field is what the settings page needs, and the
    targetOrigin is what keeps it addressed.
    """
    html = _render(monkeypatch, [PORTAL])
    assert "alice@example.com" in html
    assert f", '{PORTAL}');" in html
