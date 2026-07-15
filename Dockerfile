# =============================================================================
# AIFactory — Chainguard distroless build
# =============================================================================
# Epic #26 (issue #27): port from the legacy Ubuntu Dockerfile to a
# Chainguard base. Each P0 chunk turns one or more tests in tests/docker/
# from skipped → passing.
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Build the React frontend
# ---------------------------------------------------------------------------
# Digest is the OCI image-index (manifest-list) sha256 so buildx can resolve
# the right platform manifest. The `:latest-dev` tag is kept alongside the
# digest as a human hint and is ignored by docker when a digest is present.
# Updates land via Renovate PRs (renovate.json).
# Builds are amd64-only; arm64 support removed (not needed).
# The Rollup optional-dep workaround below is kept for safety.

FROM cgr.dev/chainguard/node:latest-dev@sha256:ce3f18966af7a0ba76f96aa32d6240b437d00eeb775d92c1e7e75f457fe5a8b7 AS frontend-build

USER root
WORKDIR /build

# Workspace-aware install via root package + the frontend's package.json
COPY package.json package-lock.json ./
COPY apps/frontend-web/package.json apps/frontend-web/

# npm ci leaves out Rollup's per-platform optional-dep native binaries when the
# build target's arch differs from the lockfile-generation host (npm/cli #4828),
# breaking multi-arch Docker builds with:
#   Error: Cannot find module @rollup/rollup-linux-arm64-gnu
# Workaround: install with `npm ci`, then explicitly install both Linux
# rollup native binaries with --no-save so the lockfile stays canonical.
RUN npm ci --workspace=apps/frontend-web \
 && cd apps/frontend-web \
 && npm install --no-save --force \
      @rollup/rollup-linux-x64-gnu \
      @rollup/rollup-linux-arm64-gnu

COPY apps/frontend-web/ apps/frontend-web/

# vite.config.ts: build.outDir = '../web-server/static'
RUN mkdir -p apps/web-server/static \
 && cd apps/frontend-web \
 && npm run build

# ---------------------------------------------------------------------------
# Stage 2: Runtime (Chainguard Python, dev variant for now — minimal split
# happens in P0.5 once we know what the runtime *actually* needs)
# ---------------------------------------------------------------------------
FROM cgr.dev/chainguard/python:latest-dev@sha256:55cd38584d1bba1913a1d58da07184cbe512724bc03e822e269404c73cd4c9cd AS runtime

USER root

# Pull all available Wolfi security patches at build time. The base is pinned by
# digest for reproducibility, but a pinned digest lags behind freshly-disclosed
# CVEs. `apk upgrade` clears fixable HIGH/CRITICAL findings between digest bumps;
# when the snapshot itself lags, bump the digest above to a Chainguard rebuild
# that ships the fix (what cleared CVE-2026-45447, libcrypto3/libssl3 → 3.6.3-r1).
RUN apk upgrade --no-cache

# System packages from Wolfi APK index. Build tools come bundled in :latest-dev.
#   git           — worktree operations
#   curl, wget    — downloads (HEALTHCHECK uses curl)
#   gh            — GitHub CLI (Wolfi apk package name)
#   nodejs, npm   — runtime Node for `npm install -g @anthropic-ai/claude-code`
#                   spawned by the agent. Installed via apk instead of
#                   binary-copying from the frontend stage so dynamic linker
#                   deps (libuv etc.) resolve correctly.
#   ca-certificates — TLS roots
#   bash          — entrypoint script (will be removed in P0.3)
#   bubblewrap    — OS-level bash sandbox for agent commands. Without it the
#                   Claude Agent SDK logs "Sandbox disabled: ... bubblewrap
#                   (bwrap) not installed" and runs commands with NO filesystem
#                   /network enforcement — unacceptable for enterprise use
#                   (#363). The cluster node allows unprivileged user
#                   namespaces (verified), so bwrap can create the sandbox.
#   socat         — required alongside bwrap by the SDK sandbox for the
#                   network-proxy path; its absence triggers the same warning.
RUN apk add --no-cache \
        bash \
        bubblewrap \
        ca-certificates \
        curl \
        git \
        gh \
        gnupg \
        nodejs \
        npm \
        socat \
        wget

# RFC-0016 #674: the per-language build toolchains (go/rust/maven/openjdk/cmake/
# build-base) that USED to be baked here have been REMOVED. AIFactory builds and
# gates now run on the Nix Job-per-task substrate (AIFACTORY_SANDBOX_BACKEND=
# nixjob): each task's flake — materialized from the contract `environment` —
# supplies the exact toolchain inside an ephemeral k8s Job on the thin nix-base
# image, backed by a warm /nix store. This image is now a THIN control-plane
# image (agent + git + node + sandbox), not a multi-language build host. The
# `binutils>=2.46-r2` Trivy patch went with build-base (binutils was only present
# as its dependency on the fat base), so it is removed too. Live-proven 2026-06-20:
# a real Go gate ran green via the nixjob backend (go1.26.3 from Nix, not this
# image). DO NOT re-add toolchains here — add packages to the task flake instead.

# Epic #44 R3 — optionally bundle the rmux binary.
#
# Build args:
#   WITH_RMUX=false   (default — bank-pilot image; no rmux binary at all)
#   WITH_RMUX=true    (dev/demo image; pins rmux v0.3.1 by SHA-256)
#
# CI matrix builds both: ``aifactory:vX`` (default) and ``aifactory:vX-rmux``.
# Bank-pilot image's Trivy report + Syft SBOM contain no rmux components.
#
# Arch support: only ``x86_64-unknown-linux-gnu`` is available upstream as
# of v0.3.1 (no aarch64 Linux build yet — tracked in Helvesec/rmux roadmap).
# ARM64 builds fail-fast with a clear message rather than silently install
# the wrong binary.
ARG WITH_RMUX=false
ARG RMUX_VERSION=0.3.1
# SHA-256 of rmux-v0.3.1-x86_64-unknown-linux-gnu.tar.gz from upstream
# SHA256SUMS file.  Bump together with RMUX_VERSION on upgrades.
ARG RMUX_SHA256_AMD64=511d3caceea4fcbc1458877a192efffcde5ceb1455f040f1a79c63ab00804cf8
RUN if [ "$WITH_RMUX" = "true" ]; then \
      arch="$(uname -m)"; \
      case "$arch" in \
        x86_64) \
          target="x86_64-unknown-linux-gnu"; \
          sha="${RMUX_SHA256_AMD64}" \
          ;; \
        *) \
          echo "WITH_RMUX=true: unsupported arch '$arch' (rmux v${RMUX_VERSION} ships x86_64 Linux only)" >&2; \
          exit 1 \
          ;; \
      esac; \
      curl -fsSL "https://github.com/Helvesec/rmux/releases/download/v${RMUX_VERSION}/rmux-v${RMUX_VERSION}-${target}.tar.gz" \
           -o /tmp/rmux.tar.gz; \
      echo "${sha}  /tmp/rmux.tar.gz" | sha256sum -c -; \
      mkdir -p /tmp/rmux-extract; \
      tar -xzf /tmp/rmux.tar.gz -C /tmp/rmux-extract; \
      find /tmp/rmux-extract -name rmux -type f -executable -exec install -m 0755 {} /usr/local/bin/rmux \; ; \
      rm -rf /tmp/rmux.tar.gz /tmp/rmux-extract; \
      /usr/local/bin/rmux -V; \
    else \
      echo "rmux integration not bundled (WITH_RMUX=false — bank-pilot image)"; \
    fi

# Project layout (keeping the legacy path under /home/projects for minimum
# diff with the existing Dockerfile; P0.4 may relocate to /app under nonroot)
RUN mkdir -p /home/projects/MagesticAI \
 && chown -R nonroot:nonroot /home/projects

# Copy project sources (respects .dockerignore)
COPY --chown=nonroot:nonroot . /home/projects/MagesticAI/

# Copy built frontend assets from Stage 1
COPY --from=frontend-build --chown=nonroot:nonroot \
    /build/apps/web-server/static/ \
    /home/projects/MagesticAI/apps/web-server/static/

# Drop to nonroot for venv + npm config (writeable paths only)
USER nonroot

# Configure npm global install dir under the nonroot home
RUN mkdir -p /home/nonroot/.npm-global \
 && npm config set prefix /home/nonroot/.npm-global

# GitHub Copilot CLI — pre-installed so the CopilotAgenticProvider finds `copilot`
# on PATH (unlike claude-code, which the Claude runtime npm-installs on demand).
# The provider requires the CLI present; runtime selection is still gated by
# AIFACTORY_RUNTIMES + a Copilot subscription sign-in on the pod (AIFactory #790).
#
# Pre-bake the coder's MCP stdio servers too (#816). The Claude session spawns
# them via `npx` at connect (core/client.py); on a cold or network-restricted
# runner that npx fetch stalls and — historically — burned the whole build to the
# deadline. Installing them globally means npx resolves them locally and never
# hits the registry at connect. The connect watchdog (#816 fix #1) is the safety
# net; this removes the stall itself for batch/offline runs.
RUN npm install -g \
    @github/copilot \
    @upstash/context7-mcp \
    @playwright/mcp

# Single Python venv shared by web-server and backend scripts (matches
# agent_service.py's sys.executable expectations)
RUN python3 -m venv /home/projects/MagesticAI/.venv

RUN /home/projects/MagesticAI/.venv/bin/pip install --no-cache-dir \
        -r /home/projects/MagesticAI/apps/web-server/requirements.txt \
        -r /home/projects/MagesticAI/apps/backend/requirements.txt

# Git identity for in-container worktree operations
RUN git config --global user.name "AIFactory" \
 && git config --global user.email "aifactory@container"

# Persistent data directory
RUN mkdir -p /home/nonroot/.aifactory

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
ENV APP_HOST=0.0.0.0 \
    APP_PORT=3101 \
    APP_BACKEND_PATH=/home/projects/MagesticAI/apps/backend \
    APP_PROJECTS_DATA_DIR=/home/nonroot/.aifactory \
    APP_DEFAULT_SHELL=/bin/bash \
    PYTHONUNBUFFERED=1 \
    PATH="/home/nonroot/.npm-global/bin:/home/projects/MagesticAI/.venv/bin:$PATH"

EXPOSE 3101

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:3101/api/health || exit 1

WORKDIR /home/projects/MagesticAI/apps/web-server

# Direct CMD — no shell wrapper. Egress control belongs in K8s NetworkPolicy
# (P4 of Epic #26), not in an entrypoint script. Runs as `nonroot` (uid 65532)
# from this point onwards.
#
# Explicitly clear the entrypoint inherited from cgr.dev/chainguard/python
# (which is `/usr/bin/python`) so `docker run image <cmd>` works portably.
# Absolute path to the venv python so we never depend on PATH ordering.
ENTRYPOINT []
CMD ["/home/projects/MagesticAI/.venv/bin/python", "-m", "server.main"]

# ---------------------------------------------------------------------------
# Stage 3: build-runtime (the ``-nix`` variant) — runtime image + baked Nix
# ---------------------------------------------------------------------------
# RFC-0017 #190: packed (multi-node) build Jobs must NOT mount the warm-store
# ``aifactory-nix-store`` PVC — it is RWO ``local-path`` and its PV is
# nodeAffinity-pinned to one node, so mounting it re-pins every build Job there
# (defeating the /work depin). This variant bakes the Nix store INTO the image
# so a packed build Job sources nix from the image and carries zero node
# affinity. Pair it with ``AIFACTORY_PACKED_NIX_IN_IMAGE=true`` (which drops the
# PVC mount on the packed path) by pointing ``AIFACTORY_BUILD_IMAGE`` at a
# ``:vX-nix`` tag in gitops.
#
# Only the ``:vX-nix`` tag is built from this stage (CI passes target=runtime
# for the default + rmux images, target=build-runtime here). The default
# bank-pilot / rmux images are byte-for-byte unchanged — no nix, no size bump.
FROM runtime AS build-runtime

USER root

# Pull the Nix store directly from the substrate image via ``COPY --from=<ref>``
# (a LITERAL external image — a stage-local ARG here fails on the classic builder
# with "invalid reference format", so we hardcode the tag). This avoids adding a
# ``FROM <substrate>`` line, which would (a) be amd64-only and so fail the P0
# multi-arch invariant, and (b) be a floating-tag base the P0 digest-pin gate
# rejects. The ref mirrors ``DEFAULT_NIX_IMAGE`` in core/job_dispatch.py.
#
# Split the copy for build speed + cache reuse (smaller/faster than a single
# chowned tree):
#   * /nix/store — the multi-GB content-addressed blobs — copied UNCHOWNED.
#     Chowning it would rewrite every inode and bust BuildKit's layer reuse
#     (slow build, fat cache). Store paths are world-readable (mode 0444/0555),
#     so nonroot can read + exec them — which is all a WARM build (toolchains
#     already in the substrate) needs.
#   * /nix/var — the sqlite db, profiles and gcroots (small) — chowned to the
#     sandbox uid so nonroot nix can open its db + take locks.
# Limitation: the store is read-only to nonroot, so a COLD flake (a derivation
# not already in the substrate) can't write new paths. Warm builds — the packed
# multi-node case we're unblocking — work; cold-write support is a follow-up
# (writable overlay at Job runtime) tracked on the slice-3 issue.
COPY --from=ghcr.io/olafkfreund/tfactory-runner-nix:latest /nix/store /nix/store
COPY --from=ghcr.io/olafkfreund/tfactory-runner-nix:latest --chown=65532:65532 /nix/var /nix/var

USER nonroot

# nix resolves from the baked default profile; flakes on; reuse the apk cert
# bundle already present in the runtime stage (ca-certificates).
ENV PATH="/nix/var/nix/profiles/default/bin:${PATH}" \
    NIX_CONFIG="experimental-features = nix-command flakes" \
    NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Build-time smoke: nix is on PATH AND nonroot can open the db / read the store
# (validates the /nix/var chown + world-readable store via a pure eval).
RUN nix --version && [ "$(nix eval --expr '1 + 1')" = "2" ]

# ENTRYPOINT/CMD/HEALTHCHECK/WORKDIR are inherited from the runtime stage.
