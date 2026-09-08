"""The packed build Job must be able to write store paths it does not already carry.

The ``-nix`` image copies /nix/store in UNCHOWNED, so it is read-only to the
sandbox uid. Reading and executing work, which covers a WARM build; writing does
not, so a derivation the substrate lacks fails outright. The warm-up flake bakes
python + pytest and nothing else, so that meant exactly one usable language --
a Kotlin task found no JVM and reported ``gradle: exit 127`` (#1491, #1492).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from core.job_dispatch import (  # noqa: E402
    WRITABLE_STORE_ROOT,
    nix_develop_wrap,
)


def test_commands_run_against_a_writable_chroot_store():
    wrapped = nix_develop_wrap(["gradle test --no-daemon"])

    # Without this the Job can only ever use what the image already baked.
    assert f'--store "local?root={WRITABLE_STORE_ROOT}"' in wrapped
    # The store root has to exist before nix is asked to use it.
    assert wrapped.startswith(f"mkdir -p {WRITABLE_STORE_ROOT} && ")


def test_the_baked_store_stays_a_substituter():
    # A chroot store alone would re-fetch python + pytest over the network on
    # every Job -- the exact cost #768 removed by baking them. Keeping the
    # image's own store as a substituter makes a warm path a local copy.
    wrapped = nix_develop_wrap(["pytest -q"])

    assert '--extra-substituters "local?root=/"' in wrapped
    assert "--option require-sigs false" in wrapped


def test_the_store_is_not_inside_the_worktree():
    # /work is the repo worktree and is packed and shipped between Jobs; a store
    # under it would show up in `git status` and in the packed workspace.
    assert not WRITABLE_STORE_ROOT.startswith("/work")


def test_the_flake_ref_and_command_wrapper_are_unchanged():
    wrapped = nix_develop_wrap(["a", "b"])

    # `path:` is mandatory (RFC-0016 4.1) and the self-check at the bottom of
    # job_dispatch asserts this exact substring.
    assert "nix develop path:/work#default" in wrapped
    assert "--command bash -c" in wrapped
    assert "a && b" in wrapped
