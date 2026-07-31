#!/usr/bin/env python3
"""Keep the Dockerfile's digest-pinned base images alive and current (#1104, #1091).

Two subcommands, both operating on the digest-pinned ``FROM`` lines:

    check   every pinned digest still resolves. Exit 1 naming any that do not.
    bump    rewrite each pin to its tag's CURRENT digest. Exit 0 with no changes
            when nothing moved; prints one line per change otherwise.

Why this exists rather than leaving it to Dependabot: Dependabot's docker
ecosystem is configured in this repo and has never produced a single PR, while
the identical setup in PFactory and TFactory bumps within a minute (#1104). It
is not dormant here -- it raises npm PRs -- only the docker lane is silent, and
the per-job log that would explain it is not exposed by the REST API.

Meanwhile the pins genuinely rot. `chainguard/python:latest-dev` moved from
7a568bc to 534fb1a to 92b8a0af inside about a day, and a superseded Chainguard
digest is garbage-collected, at which point `docker (P0 acceptance)` cannot
start a build at all -- that is #1091, and it was red for reasons unconnected to
any PR under review.

A versioned tag would fix this properly, but the public Chainguard catalog
publishes only `latest` and `latest-dev` for python; versioned tags are a paid
tier. So the pin must move, and something has to move it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# A digest-pinned FROM: registry/name:tag@sha256:<64 hex>. The tag is captured
# because it is what we re-resolve against -- the digest is the thing that goes
# stale, the tag is the thing that stays meaningful.
_FROM_RE = re.compile(
    r"^(?P<prefix>FROM\s+)(?P<ref>(?P<name>[^\s:@]+):(?P<tag>[^\s@]+))@(?P<digest>sha256:[0-9a-f]{64})",
    re.MULTILINE,
)


def parse_pins(text: str) -> list[dict[str, str]]:
    """Return one entry per digest-pinned FROM line, in file order."""
    return [m.groupdict() for m in _FROM_RE.finditer(text)]


def rewrite(text: str, resolved: dict[str, str]) -> tuple[str, list[str]]:
    """Rewrite pins to ``resolved[ref]``. Pure: no network, so it is testable.

    ``resolved`` maps ``name:tag`` -> current digest. Refs absent from it are
    left untouched, so a lookup failure degrades to "no change" rather than to
    a corrupted Dockerfile.
    """
    changes: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        ref, old = m["ref"], m["digest"]
        new = resolved.get(ref)
        if not new or new == old:
            return m.group(0)
        changes.append(f"{ref}  {old[7:19]} -> {new[7:19]}")
        return f"{m['prefix']}{ref}@{new}"

    return _FROM_RE.sub(_sub, text), changes


def _current_digest(ref: str) -> str | None:
    """Resolve a tag to the digest it points at right now, or None."""
    out = subprocess.run(  # noqa: S603 - fixed argv, ref comes from our own Dockerfile
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            ref,
            "--format",
            "{{json .Manifest.Digest}}",
        ],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout.strip())
    except json.JSONDecodeError:
        return None


def _resolves(ref_with_digest: str) -> bool:
    return (
        subprocess.run(  # noqa: S603
            ["docker", "buildx", "imagetools", "inspect", ref_with_digest],  # noqa: S607
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def cmd_check(dockerfile: Path) -> int:
    pins = parse_pins(dockerfile.read_text(encoding="utf-8"))
    if not pins:
        print(
            "::error::no digest-pinned FROM lines found — this check is looking at the wrong file"
        )
        return 1
    dead = []
    for p in pins:
        full = f"{p['ref']}@{p['digest']}"
        if _resolves(full):
            print(f"ok    {full}")
        else:
            print(f"DEAD  {full}")
            print(f"::error::pinned digest no longer resolves: {full}")
            dead.append(full)
    if dead:
        print(f"{len(dead)} pinned base image(s) have been garbage-collected upstream.")
        return 1
    print(f"all {len(pins)} pinned base image(s) resolve")
    return 0


def cmd_bump(dockerfile: Path) -> int:
    text = dockerfile.read_text(encoding="utf-8")
    resolved: dict[str, str] = {}
    for p in parse_pins(text):
        digest = _current_digest(p["ref"])
        if digest is None:
            # Not fatal: a transient registry error must not rewrite anything.
            print(f"::warning::could not resolve {p['ref']}; leaving its pin alone")
            continue
        resolved[p["ref"]] = digest
    new_text, changes = rewrite(text, resolved)
    if not changes:
        print("all pins are already current")
        return 0
    dockerfile.write_text(new_text, encoding="utf-8")
    for c in changes:
        print(f"bumped {c}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["check", "bump"])
    ap.add_argument("--dockerfile", type=Path, default=Path("Dockerfile"))
    args = ap.parse_args(argv)
    return (
        cmd_check(args.dockerfile)
        if args.command == "check"
        else cmd_bump(args.dockerfile)
    )


if __name__ == "__main__":
    sys.exit(main())
