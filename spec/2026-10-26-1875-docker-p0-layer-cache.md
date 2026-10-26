---
status: approved
issue: 1875
intent: intent/2026-10-26-1875-docker-p0-layer-cache.md
---

# Spec: Cache the P0 docker acceptance image build

## Design

User's decision: build the image in a workflow step with a gha layer cache,
then let the fixture reuse it.

1. `.github/workflows/ci.yml`, job `docker-acceptance`, after "Set up Docker
   Buildx": a "Compute security-refresh token" step (same as
   `image-build.yml`) and a `docker/build-push-action` step pinned to the
   SHA `image-build.yml` uses (`c3c9e263…` v7.4.0) with `context: .`,
   `file: Dockerfile`, `target: runtime`, `push: false`, `load: true`,
   `tags: aifactory:p0-test`, `build-args: SECURITY_REFRESH=<date>`,
   `cache-from: type=gha,scope=p0-acceptance`,
   `cache-to: type=gha,mode=max,scope=p0-acceptance`.
   `mode=max` exports the `py-build` stage layers, which are the slow ones.
2. The pytest step sets `AIFACTORY_P0_PREBUILT_IMAGE=1`.
3. `tests/docker/conftest.py` `built_image`: when that variable is `1`,
   check `aifactory:p0-test` exists (`docker image inspect`) and return the
   tag without building; if it is missing, fail (the workflow promised an
   image). Otherwise build cold exactly as today. The decision lives in a
   small helper so a unit test can cover it without Docker.

Trivy (`test_trivy_no_high_critical`) and Syft scan `built_image`, which is
still `aifactory:p0-test`, so the gates scan the same image.

## Alternatives rejected

- Reuse an image whenever `aifactory:p0-test` exists locally: a stale image
  from an older checkout would pass the tests silently.
- Share `scope=image-build`: the two workflows would evict each other's
  entries and couple their build args.
- Build once per workflow and pass the image as an artifact between jobs:
  only one job uses it, so this adds an upload/download for nothing.
- Raise the 1800 s timeout: hides the problem and keeps the 26-minute job.

## Risks

- Cold cache (first run on a new scope, or after eviction): the build step
  takes the old ~1575 s but no longer has pytest's 1800 s subprocess timeout;
  the job's 50-minute timeout still bounds it.
- gha cache is 10 GB per repo; `mode=max` adds a second image's layers.
  Eviction only costs a cold build.
- `SECURITY_REFRESH` changes daily, so the runtime stage after the
  `apk upgrade` layer rebuilds once a day; `py-build` stays cached.

## Verification

- Unit test: with the variable set and the image present, the fixture helper
  does not call `docker_build`; without the variable it builds.
- CI: the P0 job is green on the PR, and a second (warm) run's build step
  time is recorded in the PR.
