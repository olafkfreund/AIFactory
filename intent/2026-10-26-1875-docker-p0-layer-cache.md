---
status: approved
issue: 1875
author: olafkfreund
---

# Intent: Cache the P0 docker acceptance image build

## Problem

The `docker (P0 acceptance)` job in `.github/workflows/ci.yml` builds the
runtime image cold inside pytest (`tests/docker/conftest.py` `built_image` →
`tests/docker/helpers.py` `docker_build`, 1800 s timeout). Since CPython 3.15
(#1842/#1873) the build compiles lxml, xmlsec, real-ladybug, tree-sitter and
pyyaml from source and takes about 1575 s in CI. That leaves about 225 s of
headroom, and a slow runner turns the job red with a timeout.

## Proposed outcome

The P0 job builds the image with a GitHub Actions layer cache, so a warm run
reuses the `py-build` stage. The pytest fixture uses that image instead of
building again. The Trivy and Syft P0 gates scan the same `aifactory:p0-test`
image. A local `pytest tests/docker -m docker` still builds cold.

## Affected users and systems

- `.github/workflows/ci.yml`, job `docker-acceptance`.
- `tests/docker/conftest.py` and `tests/docker/helpers.py`.
- Contributors running the P0 docker tests locally.

## Constraints

- Actions pinned by SHA, the way the repo already does.
- Its own gha cache scope; must not share `image-build`'s scope.
- The Trivy P0 gate must keep scanning the same image, and the cached
  `apk upgrade` layer must still refresh daily (`SECURITY_REFRESH`, CFactory#440).
- The cold path stays the default outside CI; a stale local image must never
  be reused silently.

## Open questions

None. The user chose the gha layer cache with a pre-built image (lead brief).
