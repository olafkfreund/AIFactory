#!/usr/bin/env python3
"""Frontend supply-chain guards.

The portal used to fetch Monaco from ``cdn.jsdelivr.net`` at runtime: an
authenticated session executing third-party script that neither the image scan
(node_modules never reaches the runtime layer) nor the lockfile gate governs.
Monaco is now self-hosted and a Content-Security-Policy pins script execution
to our own origin. These tests fail if either protection is undone.
"""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_FRONTEND = _ROOT / "apps" / "frontend-web"


def _frontend_pkg() -> dict:
    return json.loads((_FRONTEND / "package.json").read_text())


def test_monaco_is_a_pinned_dependency_not_a_cdn_fetch() -> None:
    """Monaco must be a real, exactly-pinned dependency we can scan and patch.

    As a bare peer dependency its version came from the loader's hard-coded CDN
    URL, so Dependabot findings against it were unactionable — no lockfile
    change could move what the browser actually ran.
    """
    version = _frontend_pkg()["dependencies"].get("monaco-editor")
    assert version, "monaco-editor must be a direct dependency, not an implicit peer"
    assert version[0].isdigit(), (
        f"monaco-editor must be pinned exactly (got {version!r}) — a range lets the "
        "served editor drift from the version we scanned"
    )


def test_build_copies_monaco_into_the_served_assets() -> None:
    """The build must place Monaco under the served static root.

    Without this the loader points at a path that 404s in production while
    working locally against a stale directory.
    """
    scripts = _frontend_pkg()["scripts"]
    assert "copy:monaco" in scripts, "build must vendor Monaco into the static root"
    assert "copy:monaco" in scripts["build"], "copy:monaco must run as part of build"
    assert "web-server/static/monaco/vs" in scripts["copy:monaco"], (
        "Monaco must land in the directory the server actually serves"
    )


def test_editor_page_points_the_loader_at_our_own_origin() -> None:
    """`loader.config` must override the default CDN path."""
    src = (_FRONTEND / "src" / "pages" / "EditorPage.tsx").read_text()
    assert "loader.config" in src, "EditorPage must configure the Monaco loader"
    assert "'/monaco/vs'" in src or '"/monaco/vs"' in src, (
        "the loader must resolve Monaco from our own origin"
    )


def test_no_cdn_script_sources_in_frontend_source() -> None:
    """No source file may pull executable script from a third-party CDN."""
    cdn_hosts = ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com")
    offenders: list[str] = []
    for path in (*_FRONTEND.glob("src/**/*.ts"), *_FRONTEND.glob("src/**/*.tsx")):
        text = path.read_text(errors="replace")
        if any(host in text for host in cdn_hosts):
            offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, f"third-party CDN referenced in: {offenders}"


def test_csp_pins_script_execution_to_our_origin() -> None:
    """The default CSP must not permit off-origin script."""
    import sys

    sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
    from server.config import Settings

    csp = Settings().CONTENT_SECURITY_POLICY
    assert "script-src 'self'" in csp, "script-src must be self-scoped"
    assert "https://" not in csp.split("script-src")[1].split(";")[0], (
        "no remote origin may be allowed to serve script"
    )
    assert "frame-ancestors 'none'" in csp, "the portal must not be framable"
