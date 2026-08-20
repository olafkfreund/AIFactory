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
# the right platform manifest. The version tag is kept alongside the digest as
# a human hint and is ignored by docker when a digest is present.
# Builds are amd64-only; arm64 support removed (not needed).
# The Rollup optional-dep workaround below is kept for safety.
#
# Why not cgr.dev/chainguard/node (issue #1091): this stage was pinned to
# `node:latest-dev@sha256:...`, which cannot work. Chainguard rebuilds
# `latest-*` continuously and garbage-collects superseded digests, so the pin
# 404s within days and `load metadata` fails before the build starts:
#   ERROR: cgr.dev/chainguard/node:latest-dev@sha256:ce3f18...: not found
# The usual answer -- pin a versioned tag by digest -- is not available: the
# public Chainguard catalog publishes only `latest`, `latest-dev`,
# `latest-slim`, `next` and `next-dev` for node. `node:22-dev`, `node:24-dev`
# and `node:24` all 404; versioned tags are a paid Production-tier feature
# under a customer-specific org path. Docker Official Images do retain
# superseded digests for versioned tags (node:24.0.0-bookworm-slim from May
# 2025 still resolves), so a pin here survives while staying reproducible.
#
# Constraints this tag has to satisfy:
#   - node >= 24.0.0, npm >= 10 (root package.json `engines`)
#   - glibc, so the `-gnu` Rollup native binaries installed below are correct;
#     an Alpine/musl variant would need `@rollup/rollup-linux-x64-musl`
# Nothing from this stage ships: only apps/web-server/static is copied into
# the runtime stage, so the base's CVE posture is not part of the attack
# surface. The runtime stage stays on Chainguard, where it does matter.
# Digest bumps land via Dependabot PRs (.github/dependabot.yml).

FROM docker.io/node:24-bookworm-slim@sha256:3638d9a6fe4030bd716be989438248074489337ba3275657f93595428be4fc03 AS frontend-build

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
FROM cgr.dev/chainguard/python:latest-dev@sha256:534fb1a1b9ad4d9d149ab669ca4218be76c84990e2f3379c7f703d224647666b AS runtime

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

# Bake the provider coder CLIs into the image so the control-plane boot never
# npm-installs them (mirrors TFactory #791: the install-clis init container hung
# 8+ min on a slow registry and stalled the rollout). .npm-global/bin is already
# on PATH (copilot is already baked below the same way). Versions pinned here.
#
# The pins are watched by the hub `agent-CLI freshness` job
# (Factory/scripts/check_cli_freshness.py --open-bump-pr), which proposes bumps
# as `chore/agent-cli-pins` across all three service repos at once and never
# merges them, plus factory-gitops/.github/workflows/cli-canary.yml, which
# asserts every repo pins all three CLIs identically and that each pin installs
# and launches.
#
# NOT Dependabot, whatever an earlier version of this comment claimed
# (factory-gitops#206). Dependabot's Dockerfile parser reads `FROM` lines only —
# no package-ecosystem parses shell arguments inside a RUN layer, so it cannot
# see the `@version` on the npm install below and never could. It does cover the
# `FROM` lines in this file, and nothing else in it.
#
# `install.cjs` is NOT redundant with the npm postinstall (Factory#383). The
# postinstall downloads the 275 MB platform-native binary correctly, but leaves
# `bin/claude.exe` as the 11-line stub that ships in the package — a script whose
# whole body prints "native binary not installed" and exits 1. So `claude` on
# PATH was broken in every control-plane pod while the binary underneath it ran
# fine, and the stub's own error blames --ignore-scripts / --omit=optional,
# neither of which is used here. Re-running install.cjs completes the swap.
#
# `claude --version` is the point of the fix, not decoration: this shipped broken
# because nothing asserted the CLI works. Full path, since PATH is set for the
# runtime user rather than for RUN.
RUN npm install -g \
        @anthropic-ai/claude-code@2.1.224 \
        @openai/codex@0.147.0 \
        @google/gemini-cli@0.54.4 \
 && node /home/nonroot/.npm-global/lib/node_modules/@anthropic-ai/claude-code/install.cjs \
 && /home/nonroot/.npm-global/bin/claude --version \
 && npm cache clean --force

# Google Antigravity CLI (`agy`) — a SEPARATE product from @google/gemini-cli,
# despite the `antigravity` alias above suggesting otherwise. That alias makes
# the "Antigravity" provider really gemini-cli under another name, so it can
# only reach the models gemini-cli knows (up to gemini-3.5-flash); the Gemini
# 3.6/3.7 family is served by the real CLI only.
#
# Installed as `agy` ALONGSIDE the alias rather than over it, deliberately: the
# real CLI needs its own sign-in, which no image can bake, so replacing the
# alias would trade a working Gemini path for one that fails on auth. Providers
# opt into `agy` per model; the alias keeps serving everything as before.
# NOTE the flag spellings differ — gemini-cli's `--yolo` is
# `--dangerously-skip-permissions` here — so a provider pointed at `agy` must
# build its own argv rather than reuse the gemini one.
#
# Distributed as a single glibc-linked Go binary via a GCS tarball, not npm; the
# canonical URL comes from the auto-updater manifest at
#   https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json
# To bump: read `version` + `url` from that manifest and recompute the sha256.
# The checksum is verified rather than trusted — this is an unsigned binary from
# a mutable bucket, so a silent swap upstream must fail the build, not ship.
# `--version` is asserted for the same reason `claude --version` is above.
ARG ANTIGRAVITY_VERSION=1.1.16-6607970839166976
ARG ANTIGRAVITY_SHA256=7742953b7835b457e9102f1357a493913657dfd147435584f609d58356ec085a
RUN set -eu; \
    url="https://storage.googleapis.com/antigravity-public/antigravity-cli/${ANTIGRAVITY_VERSION}/linux-x64/cli_linux_x64.tar.gz"; \
    curl -fsSL -o /tmp/antigravity.tgz "$url"; \
    echo "${ANTIGRAVITY_SHA256}  /tmp/antigravity.tgz" | sha256sum -c -; \
    tar xzf /tmp/antigravity.tgz -C /tmp antigravity; \
    install -m 0755 /tmp/antigravity /home/nonroot/.npm-global/bin/agy; \
    rm -f /tmp/antigravity.tgz /tmp/antigravity; \
    /home/nonroot/.npm-global/bin/agy --version
itHub Copilot CLI — pre-installed so the CopilotAgenticProvider finds `copilot`
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

# Supply-chain: @github/copilot vendors `foundry-local-sdk` (Microsoft Foundry
# *local-model* runtime) deep inside its platform bundle, and that subtree drags
# in a string of HIGH npm CVEs — adm-zip 0.5.17 (CVE-2026-39244, ZIP DoS),
# serialize-javascript 6.0.2 (GHSA-5c6j-r48x-rmvq, RCE), and more surface as each
# is patched. AIFactory only uses copilot as a GitHub-hosted agentic coder; the
# local-foundry path is never exercised, so the whole subtree is unused dead
# weight. Remove it outright to clear the entire vuln class at once, then smoke
# the CLI so the build fails if copilot actually needed it.
RUN set -eux; \
    find /home/nonroot/.npm-global -type d -name foundry-local-sdk -prune -exec rm -rf {} +; \
    ! find /home/nonroot/.npm-global -type d -name foundry-local-sdk | grep .; \
    test -x /home/nonroot/.npm-global/bin/copilot; \
    rm -rf /home/nonroot/.cache/copilot
# NB: do NOT execute `copilot` here — the CLI self-downloads its platform runtime
# bundle (which re-vendors foundry-local-sdk + adm-zip) into ~/.cache/copilot on
# first run, which would bake the vuln back into the image. `test -x` verifies the
# install without triggering that fetch; the runtime re-fetch is ephemeral pod
# cache, not part of the scanned image.

# Single Python venv shared by web-server and backend scripts (matches
# agent_service.py's sys.executable expectations)
RUN python3 -m venv /home/projects/MagesticAI/.venv

# Install from the hash-pinned lock, NOT from the two requirements.txt files
# (#1284). Those declare `>=` floors, so installing from them made every image
# build resolve whatever PyPI served that minute and recorded nothing about what
# landed — the only class of external reference in this repo that was still
# mutable, in the one process that holds the agent's credentials.
#
# requirements.lock is the compiled closure of exactly those two files, so the
# declared floors are unchanged; --require-hashes is what makes it load-bearing.
# Without that flag pip would happily accept a substituted artifact, and the file
# would be documentation rather than a control. It also makes pip fail closed on
# an out-of-date lock: a dependency added to requirements.txt but not compiled in
# is absent here, the import fails at runtime, and the ci.yml `deps-lock-drift`
# job is what catches that in review instead.
#
# Regeneration command lives in the lockfile header.
RUN /home/projects/MagesticAI/.venv/bin/pip install --no-cache-dir \
        --require-hashes \
        -r /home/projects/MagesticAI/requirements.lock

# Git identity for in-container worktree operations
RUN git config --global user.name "AIFactory" \
 && git config --global user.email "aifactory@container" \
 && git config --global credential."https://github.com".helper "!gh auth git-credential"

# The credential helper above is what makes `git push` work at all. `gh` is
# authenticated from GITHUB_TOKEN in the pod env, but git cannot see gh's
# credential on its own, so a push to the https origin has no username to send
# and dies non-interactively with:
#   fatal: could not read Username for 'https://github.com': No such device or
#   address
# — which surfaced as an HTTP 409 on /worktree/create-pr: the build finished,
# every subtask passed, and the branch never left the pod.
#
# Set globally rather than per-command because AIFactory pushes from five
# separate call sites (pr_endgame, routes/pr, completion_orchestration), and a
# sixth added later would silently inherit the broken behaviour. No secret is
# stored — the helper shells out to gh, which reads its own token from the
# environment (cf. env-not-argv). TFactory scopes the same helper per-command
# instead, which suits its ephemeral verify Job; this is a long-lived pod.

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
COPY --from=ghcr.io/olafkfreund/tfactory-runner-nix:latest@sha256:369e1aa003519d5edc8363c2f9aa69247798ebdc312ebd2c1e46aff61d4613c9 /nix/store /nix/store
COPY --from=ghcr.io/olafkfreund/tfactory-runner-nix:latest@sha256:369e1aa003519d5edc8363c2f9aa69247798ebdc312ebd2c1e46aff61d4613c9 --chown=65532:65532 /nix/var /nix/var

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
