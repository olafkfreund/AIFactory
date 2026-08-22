"""No tracked symlink may point outside the repository.

AIFactory#1392. `.antigravitycli/8d339854-….json` was committed as a symlink to
`/home/olafkfreund/.gemini/config/projects/…` — an absolute path in a
developer's home directory.

Two consequences, and the first is the one that cost a benchmark run:

* **Every containerised build died on unpack.** `run.py` calls
  `maybe_unpack_workspace`, which calls `_safe_extract` -> `_vet_member`, which
  raises `ValueError: unsafe tar link target: '/home/olafkfreund/…'`. The guard
  is correct — a tar member linking outside the destination is a tarslip — so
  the failure was the *tarball* being wrong, not the check. Three benchmark
  cells were dispatched as k8s Jobs, all three died here, and the backend
  reaped them as "stranded" with a one-line log. Zero files changed, ~$3.42 of
  planning spent, and the task status still read `in_progress`.
* **It leaks a host path into a public repository.**

Asserted over the git index rather than the working tree: a working-tree scan
would miss a symlink that is committed but not checked out, and would flag local
debris that is not committed. The index is what ships.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SYMLINK_MODE = "120000"


def _tracked_symlinks() -> list[tuple[str, str]]:
    """(path, target) for every symlink in the git index."""
    listing = subprocess.run(
        ["git", "ls-files", "-s"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    out: list[tuple[str, str]] = []
    for line in listing.splitlines():
        meta, _, path = line.partition("\t")
        if not meta.startswith(_SYMLINK_MODE):
            continue
        # Read the blob by its INDEX hash, not `HEAD:<path>`. A newly staged
        # symlink is not in HEAD yet, so `git show HEAD:…` returns empty and the
        # absolute-target check silently never fires -- the check would pass on
        # exactly the change it exists to catch. Found by mutation-testing this
        # test: staging an absolute symlink left it green.
        blob = meta.split()[1]
        target = subprocess.run(
            ["git", "cat-file", "blob", blob],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        out.append((path, target))
    return out


def test_no_tracked_symlink_escapes_the_repository() -> None:
    offenders = []
    for path, target in _tracked_symlinks():
        if target.startswith("/"):
            offenders.append(f"{path} -> {target} (absolute)")
            continue
        resolved = (Path(path).parent / target).as_posix()
        if resolved.startswith("../") or "/../" in f"/{resolved}":
            # A relative link may still climb out of the tree.
            depth = 0
            for part in resolved.split("/"):
                depth = depth - 1 if part == ".." else depth + (part not in ("", "."))
                if depth < 0:
                    offenders.append(f"{path} -> {target} (escapes the tree)")
                    break

    assert not offenders, (
        f"tracked symlink(s) point outside the repository: {offenders}. "
        "A tar of this tree is refused by `_vet_member` in artifact_store.py, so "
        "every containerised build dies during unpack (AIFactory#1392)."
    )


def test_the_scan_examined_the_index_not_an_empty_list() -> None:
    """Guard the measurement itself.

    If `git ls-files -s` ever stops returning parseable output, the test above
    passes having inspected nothing — the pass-shaped empty measurement this
    fleet keeps filing (Factory#832). Assert the listing is non-trivial.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-s"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert len(listing) > 100, (
        f"git ls-files returned only {len(listing)} entries -- the symlink scan "
        "is not examining this repository"
    )


def test_pack_skips_symlink_that_unpack_would_reject(tmp_path: Path) -> None:
    """A workspace holding an escaping symlink must survive a pack/unpack round trip.

    This is the AIFactory#1392 failure as a test. The link is rejected at
    UNPACK, inside the build Job, so the symptom is a build that dies before
    reading any source -- not a pack error anyone would notice. Asserting only
    that ``_tar_workspace`` omits the member would pass against a packer that
    dropped the wrong thing, so the assertion is the round trip: unpack must
    reach the real file.

    Both link shapes are planted. The absolute one is the observed defect; the
    relative ``../`` escape is the same class and is what a naive "skip absolute
    symlinks" fix would let through.
    """
    from apps.backend.core import artifact_store as store

    outside = tmp_path / "outside.json"
    outside.write_text("{}")

    src = tmp_path / "ws"
    (src / "sub").mkdir(parents=True)
    (src / "real.py").write_text("x = 1\n")
    (src / ".antigravitycli").mkdir()
    (src / ".antigravitycli" / "proj.json").symlink_to(outside)  # absolute
    (src / "sub" / "rel_escape").symlink_to(Path("..") / ".." / "outside.json")
    (src / "sub" / "inside_link").symlink_to(Path("..") / "real.py")  # must be KEPT

    blob = store._tar_workspace(src)

    dest = tmp_path / "dest"
    dest.mkdir()
    store._safe_extract(blob, dest)  # the mutation point: raises before this fix

    assert (dest / "real.py").read_text() == "x = 1\n"
    assert not (dest / ".antigravitycli" / "proj.json").exists()
    assert not (dest / "sub" / "rel_escape").exists()
    assert (dest / "sub" / "inside_link").is_symlink(), "an inside link must survive"
