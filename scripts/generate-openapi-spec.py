#!/usr/bin/env python3
"""Regenerate apps/web-server/openapi.yaml from the FastAPI app.

This is the OpenAPI spec Backstage serves on the component's **APIs** tab
(referenced from catalog-info.yaml's `kind: API` entity via `$text`). Re-run
it whenever routes/schemas change so the published API docs stay accurate.

Usage:
    APP_DISABLE_AUTH=true apps/web-server/.venv/bin/python scripts/generate-openapi-spec.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEB_SERVER = REPO / "apps" / "web-server"
OUT = WEB_SERVER / "openapi.yaml"

sys.path.insert(0, str(WEB_SERVER))


def main() -> int:
    import yaml
    from server.main import app  # noqa: E402 — needs sys.path above

    spec = app.openapi()
    with open(OUT, "w") as f:
        yaml.safe_dump(spec, f, sort_keys=False, allow_unicode=True, width=100)
    print(
        f"Wrote {OUT.relative_to(REPO)} — "
        f"{spec['info']['title']} v{spec['info']['version']}, "
        f"{len(spec.get('paths', {}))} paths."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
