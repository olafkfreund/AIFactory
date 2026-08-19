"""The OAuth callback page must not reflect its query parameters as markup.

`/auth/{outlook,google}/callback` is an UNAUTHENTICATED redirect target (it is
in PUBLIC_PREFIXES in auth.py, because the IdP is the caller). Its `error` and
`error_description` query parameters are echoed into the result page, so anyone
who can get a victim to open a link controls that text. Two separate contexts
in one document have to hold, which is why there are two tests:

  - the `<p>` body, an HTML context; and
  - the `postMessage` payload, a JS string literal inside an inline `<script>`,
    where `</script>` ends the element before the JS parser is ever consulted
    and quote-escaping alone therefore proves nothing.
"""

from server.routes.email import _js_string, _oauth_result_html

XSS = "<script>alert(1)</script>"


def _body(**kw: str) -> str:
    body = _oauth_result_html(success=False, **kw).body
    return bytes(body).decode()


def test_message_is_escaped_in_the_html_context():
    html = _body(message=XSS)
    # The raw tag must not survive anywhere in the document.
    assert XSS not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_message_cannot_close_the_inline_script_element():
    # The payload fields go through _js_string. A `</script>` in any of them
    # would end the <script> element and turn the rest into live markup.
    html = _body(message=XSS, email=XSS, provider=XSS)
    # Exactly one script element: the one this function intends to emit.
    assert html.count("</script>") == 1


def test_js_string_escapes_the_tag_opener_not_just_quotes():
    out = _js_string("</script><img src=x onerror=alert(1)>")
    assert "</script>" not in out
    assert "\\x3c" in out
    # And the ordinary escaping still holds.
    assert _js_string("it's\n") == "'it\\'s\\n'"
