"""The pin rewriter must move only what it should (#1104).

`rewrite()` is deliberately pure — it takes the resolved digests as an argument
rather than reaching the network — so the rewriting logic can be tested for the
cases that actually matter: partial resolution, no-op runs, and not mangling
lines that were never pinned.

A rewriter that corrupts a Dockerfile on a transient registry error would be
worse than the manual bumping it replaces, which is the whole reason the
resolve step and the rewrite step are separated.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from base_image_pins import parse_pins, rewrite  # noqa: E402

_OLD = "sha256:" + "a" * 64
_NEW = "sha256:" + "b" * 64

_DOCKERFILE = f"""\
FROM docker.io/node:24-bookworm-slim@{_OLD} AS frontend
RUN echo build
# A COPY --from pin is NOT a FROM and must be left alone.
COPY --from=ghcr.io/olafkfreund/runner:latest@{_OLD} /x /x
FROM cgr.dev/chainguard/python:latest-dev@{_OLD} AS runtime
FROM scratch AS unpinned
"""


def test_parse_finds_only_pinned_from_lines() -> None:
    pins = parse_pins(_DOCKERFILE)
    assert [p["ref"] for p in pins] == [
        "docker.io/node:24-bookworm-slim",
        "cgr.dev/chainguard/python:latest-dev",
    ]
    # `COPY --from` and an unpinned `FROM scratch` are both out of scope.
    assert all("runner" not in p["ref"] for p in pins)


def test_rewrite_moves_only_the_refs_it_was_given() -> None:
    """A ref missing from `resolved` keeps its old digest, not a broken one."""
    text, changes = rewrite(_DOCKERFILE, {"cgr.dev/chainguard/python:latest-dev": _NEW})
    assert len(changes) == 1
    assert f"chainguard/python:latest-dev@{_NEW}" in text
    # The unresolved one is untouched — a registry hiccup must not corrupt it.
    assert f"node:24-bookworm-slim@{_OLD}" in text
    # And nothing else in the file moved.
    assert f"runner:latest@{_OLD}" in text
    assert "FROM scratch AS unpinned" in text


def test_rewrite_is_a_noop_when_nothing_moved() -> None:
    same = {p["ref"]: _OLD for p in parse_pins(_DOCKERFILE)}
    text, changes = rewrite(_DOCKERFILE, same)
    assert changes == []
    assert text == _DOCKERFILE


def test_rewrite_reports_each_change_readably() -> None:
    resolved = {p["ref"]: _NEW for p in parse_pins(_DOCKERFILE)}
    _, changes = rewrite(_DOCKERFILE, resolved)
    assert len(changes) == 2
    for line in changes:
        assert "->" in line
        assert _OLD[7:19] in line and _NEW[7:19] in line


def test_the_repos_own_dockerfile_is_parseable() -> None:
    """Guards against the regex drifting away from the file it exists to read."""
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    pins = parse_pins(dockerfile.read_text(encoding="utf-8"))
    assert len(pins) >= 2, (
        "expected the frontend and runtime stages to be digest-pinned"
    )
    assert all(p["digest"].startswith("sha256:") for p in pins)
