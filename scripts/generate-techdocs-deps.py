#!/usr/bin/env python3
"""Generate docs/dependencies.md for Backstage TechDocs from the project's
manifests (Python requirements + npm package.json).

Kept fresh by .github/workflows/techdocs.yml so the published Dependencies page
never drifts from what the project actually pins.

Usage:
    python3 scripts/generate-techdocs-deps.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "dependencies.md"

_REQ_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*([<>=!~].*)?$")


def _parse_requirements(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ_RE.match(line)
        if m:
            rows.append((m.group(1), (m.group(2) or "").strip() or "—"))
    return sorted(rows, key=lambda r: r[0].lower())


def _parse_package_json(path: Path, key: str) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return sorted(
        ((n, v) for n, v in data.get(key, {}).items()),
        key=lambda r: r[0].lower(),
    )


def _table(rows: list[tuple[str, str]]) -> str:
    if not rows:
        return "_None._\n"
    out = ["| Package | Version |", "|---------|---------|"]
    out += [f"| `{n}` | `{v}` |" for n, v in rows]
    return "\n".join(out) + "\n"


def main() -> int:
    backend = _parse_requirements(REPO / "apps" / "backend" / "requirements.txt")
    webserver = _parse_requirements(REPO / "apps" / "web-server" / "requirements.txt")
    fe = REPO / "apps" / "frontend-web" / "package.json"
    fe_deps = _parse_package_json(fe, "dependencies")
    fe_dev = _parse_package_json(fe, "devDependencies")

    md = [
        "# Dependencies",
        "",
        "> Auto-generated from the project manifests by "
        "`scripts/generate-techdocs-deps.py` (run in CI). Do not edit by hand.",
        "",
        f"AIFactory **declares** **{len(backend)}** backend, "
        f"**{len(webserver)}** web-server, and **{len(fe_deps)}** frontend "
        "runtime dependencies.",
        "",
        # This line said "pins" until #1284, which is what the tables below
        # disprove: nearly every Python entry is a `>=` floor. A generated doc
        # asserting a control the manifests do not implement is worse than no
        # doc, because it is the thing an auditor reads instead of the file.
        # The declared floors are the minimum supported version; the exact set
        # a build installs lives in the lock.
        "> **Python versions below are declared floors, not installed "
        "versions.** The runtime image installs the fully-resolved, "
        "hash-pinned closure from [`requirements.lock`](https://github.com/"
        "olafkfreund/AIFactory/blob/dev/requirements.lock) via "
        "`pip --require-hashes`; CI test jobs still resolve the floors "
        "freely (#1284).",
        "",
        "## Backend (Python) — `apps/backend/requirements.txt`",
        "",
        _table(backend),
        "## Web server (Python) — `apps/web-server/requirements.txt`",
        "",
        _table(webserver),
        "## Frontend (npm) — `apps/frontend-web/package.json`",
        "",
        "### Runtime",
        "",
        _table(fe_deps),
        "### Dev",
        "",
        _table(fe_dev),
    ]
    OUT.write_text("\n".join(md) + "\n")
    print(
        f"Wrote {OUT.relative_to(REPO)} — "
        f"{len(backend)} backend, {len(webserver)} web-server, "
        f"{len(fe_deps)}+{len(fe_dev)} frontend deps."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
