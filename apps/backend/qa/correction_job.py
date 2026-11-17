"""Entrypoint of the QA correction Job (#2116).

Runs in a Kubernetes Job on a packed workspace: unpack ``/work``, run the QA
fixer once, then upload the changed files for the control plane to validate
and apply (``core.workspace_fetch.fetch_correction``). Exits non-zero when the
fixer did not report success or the upload failed, so nothing is applied.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--spec-id", required=True)
    args = parser.parse_args(argv)

    from core.workspace_fetch import (  # noqa: PLC0415
        maybe_push_correction,
        maybe_unpack_workspace,
    )
    from qa.correction import _run_fixer_bg  # noqa: PLC0415

    project = Path(args.project)
    maybe_unpack_workspace(project)
    spec_dir = project / ".aifactory" / "specs" / args.spec_id
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not asyncio.run(_run_fixer_bg(spec_dir)):
        return 1
    return 0 if maybe_push_correction(project, base) else 1


if __name__ == "__main__":
    sys.exit(main())
