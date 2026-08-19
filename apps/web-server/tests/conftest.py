"""Make ``server`` importable for the co-located web-server tests (#903).

These tests import the app as ``from server.routes import execution`` and patch
targets like ``server.routes.gitlab.run_glab_command``. That only resolves when
``apps/web-server`` is on ``sys.path``.

Several test files insert it themselves (``Path(__file__).resolve().parents[1]``),
and the ones that don't used to work by accident: pytest collects files in
alphabetical order, so an earlier file's insert leaked into later ones. Run such
a file on its own and it fails with ``ModuleNotFoundError: No module named
'server'``. Doing it once here makes the import deterministic and independent of
collection order.

Note the directory is ``web-server`` with a hyphen, so there is no importable
``apps.web-server`` package path — ``server.*`` (with this dir on sys.path) is
the only form that works. Patch targets written as ``apps.web-server.server.*``
never resolved; see #903.
"""

from __future__ import annotations

import sys
from pathlib import Path

WEB_SERVER = Path(__file__).resolve().parents[1]

if str(WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER))

# ``tests/`` itself, so ``verdict_helpers`` imports by name from any test file
# rather than each one re-deriving how to unwrap an ``honest_status`` refusal.
# pytest's default ``prepend`` import mode already inserts each test file's own
# directory, but doing it here too means a test module that is imported (rather
# than collected) still resolves it -- and it does not depend on which file
# pytest happened to collect first (#903 again).
TESTS_DIR = Path(__file__).resolve().parent

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
