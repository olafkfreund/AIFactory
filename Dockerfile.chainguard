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
# Digest is the OCI image-index (manifest-list) sha256 so multi-arch buildx
# (P0.6) can still resolve the right platform manifest. The `:latest-dev`
# tag is kept alongside the digest as a human hint and is ignored by docker
# when a digest is present. Updates land via Renovate PRs (renovate.json).
FROM cgr.dev/chainguard/node:latest-dev@sha256:ce3f18966af7a0ba76f96aa32d6240b437d00eeb775d92c1e7e75f457fe5a8b7 AS frontend-build

USER root
WORKDIR /build

# Workspace-aware install via root package + the frontend's package.json
COPY package.json package-lock.json ./
COPY apps/frontend-web/package.json apps/frontend-web/

RUN npm ci --workspace=apps/frontend-web

COPY apps/frontend-web/ apps/frontend-web/

# vite.config.ts: build.outDir = '../web-server/static'
RUN mkdir -p apps/web-server/static \
 && cd apps/frontend-web \
 && npm run build

# ---------------------------------------------------------------------------
# Stage 2: Runtime (Chainguard Python, dev variant for now — minimal split
# happens in P0.5 once we know what the runtime *actually* needs)
# ---------------------------------------------------------------------------
FROM cgr.dev/chainguard/python:latest-dev@sha256:c1d503ebc5088bd0143673af0d02f2db31e53acc506ba5a8f4756c337a989d3f AS runtime

USER root

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
RUN apk add --no-cache \
        bash \
        ca-certificates \
        curl \
        git \
        gh \
        gnupg \
        nodejs \
        npm \
        wget

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
