---
status: approved
issue: 1875
spec: spec/2026-10-26-1875-docker-p0-layer-cache.md
---

# Plan: Cache the P0 docker acceptance image build

Decisions carried from the approved spec:

- CI builds `aifactory:p0-test` (target `runtime`) with
  `docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc # v7.4.0`,
  `load: true`, gha cache `scope=p0-acceptance`, `mode=max`, and
  `SECURITY_REFRESH=$(date -u +%Y-%m-%d)` as in `image-build.yml`.
- The pytest step sets `AIFACTORY_P0_PREBUILT_IMAGE=1`. Only then does the
  fixture reuse the image; it fails if the image is missing. Without the
  variable it builds cold as today. Never reuse an image just because it exists.
- Trivy/Syft keep scanning `built_image` (`aifactory:p0-test`).

## Steps

1. `tests/docker/helpers.py`: add `image_exists(tag) -> bool`
   (`docker image inspect`, returncode 0) and
   `build_or_reuse(dockerfile, tag, target) -> CompletedProcess | None`:
   returns `None` when `AIFACTORY_P0_PREBUILT_IMAGE == "1"` and the image
   exists; raises `RuntimeError` when the variable is set and the image is
   missing; otherwise returns `docker_build(...)`.
   `tests/docker/conftest.py:41`: call `build_or_reuse`; `None` means reuse;
   turn `RuntimeError` into `pytest.fail`.
   New `tests/docker/test_prebuilt_image.py` (no docker marker, monkeypatched,
   no Docker needed) covering: reuse, missing-image error, cold build when unset.
   → verify by `apps/backend/.venv/bin/pytest tests/docker/test_prebuilt_image.py -q`
   and `ruff check tests/docker`.
   Traps: new test file must not be marked `slow`/`docker`, so the default
   `-m "not slow"` suite runs it.
2. `.github/workflows/ci.yml` job `docker-acceptance` (~line 923): after
   "Set up Docker Buildx" add "Compute security-refresh token" and
   "Build P0 image (gha layer cache)" steps per the decisions; add
   `AIFACTORY_P0_PREBUILT_IMAGE: "1"` to the pytest step env; update the
   `timeout-minutes` comment.
   → verify by `actionlint .github/workflows/ci.yml` (if installed) and YAML parse.
   Traps: pin by SHA with version comment; no `continue-on-error` (rule 4.10).

## Deviations

- Step 2, after review: the build step gets `timeout-minutes: 40` (a cold build
  took 1754 s, so a hang fails before the 50-minute job limit), and `cache-to`
  gets `ignore-error=true`, so a cache-export error (for example HTTP 429) cannot
  turn the required job red. A failed export only costs the next run a cold build.

## Tests

- `pytest tests/docker/test_prebuilt_image.py -q`: passes; fails on dev
  (no `build_or_reuse`).
- CI `docker (P0 acceptance)` green; rerun it and record the warm build-step time.

## Rollback

Revert the PR. The fixture falls back to the cold build when the variable is unset.
