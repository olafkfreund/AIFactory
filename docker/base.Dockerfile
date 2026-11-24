# AIFactory's Wolfi package layers, published before the main Dockerfile consumes them.
FROM cgr.dev/chainguard/python:latest-dev@sha256:b73b9e61fa9e0c6ec59c68ee2db1a08a4a61b969dbe03a39c309033749ef7f78 AS py-build
LABEL org.opencontainers.image.source="https://github.com/olafkfreund/AIFactory"
USER root
ARG SECURITY_REFRESH=0
RUN echo "security refresh: ${SECURITY_REFRESH}" \
 && apk add --no-cache build-base cmake xmlsec-dev xmlsec-openssl \
        libxml2-dev libxslt-dev pkgconf openssl-dev libtool

FROM cgr.dev/chainguard/python:latest-dev@sha256:b73b9e61fa9e0c6ec59c68ee2db1a08a4a61b969dbe03a39c309033749ef7f78 AS runtime
LABEL org.opencontainers.image.source="https://github.com/olafkfreund/AIFactory"
USER root
ARG SECURITY_REFRESH=0
RUN echo "security refresh: ${SECURITY_REFRESH}" \
 && apk upgrade --no-cache
# System packages from Wolfi APK index. Build tools come bundled in :latest-dev.
#   git           — worktree operations
#   curl, wget    — downloads (HEALTHCHECK uses curl)
#   gh            — GitHub CLI (Wolfi apk package name)
#   (Node is NOT from apk: see the node-runtime COPY below, Factory#1710.)
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
# Two version floors below are CVE remediation, not preference. `apk upgrade`
# earlier in this stage does not clear either, because both packages come from
# the base image layer and must be named explicitly to be pulled forward:
#   libssl3 / libcrypto3 3.6.3-r3 — CVE-2026-14456 (HIGH), fixed in 3.6.3-r5.
#                        Named together because they ship from the same openssl
#                        origin and apk will not move one without the other.
#   busybox 1.37.0-r61 — CVE-2026-38753 (DoS via crafted AWK in awk_sub) and
#                        CVE-2026-38754 (heap overflow in ifsbreakup, shell/ash.c),
#                        both HIGH, both fixed in 1.38.0-r0.
#   wget    1.25.0-r14 — CVE-2026-58471 and CVE-2026-58472, both HIGH, both
#                        fixed in 1.25.0-r15.
#   curl / libcurl-openssl4 8.22.0-r4 — CVE-2026-92392 (HIGH), fixed in
#                        8.23.0-r0. Named together: same origin.
#   ca-certificates-bundle 20260909-r2 — CVE-2026-111943 (HIGH), fixed in
#                        20260909-r3.
#   uv 0.13.5-r1 — CVE-2026-111943 (HIGH, same Go x509 denylist), fixed in
#                        0.13.6-r1. Agents launch MCP servers with uvx.
#   libbrotlicommon1 / libbrotlidec1 / libbrotlienc1 1.2.0-r5 — CVE-2026-111497
#                        (HIGH), fixed in 1.2.0-r7. Named together: same origin.
#   py3-pip-wheel 26.2.2-r0 — CVE-2026-114613 (HIGH), msgpack map-deserialization
#                        leak in pip's vendored msgpack 1.2.1; fixed in 26.2.2-r1
#                        (vendors 1.2.3). python-3.15 depends on it, so it cannot
#                        be apk-deleted with the other pip packages below.
# Floors rather than == pins, so the build stays green as Wolfi revs further
# (1.38.0-r1 and later already exist); drop each once the base digest ships it.
# Verified present in the Wolfi x86_64 APKINDEX, then confirmed against the
# pinned base digest itself — a floor above the newest available version fails
# the build outright. git-lfs: floor checked the same way (3.7.1-r2 .. 3.8.0-r3);
# the explicit `git lfs push` before every push needs it (#1690, #1721).
RUN apk add --no-cache \
        bash \
        "busybox>=1.38.0-r0" \
        "libcrypto3>=3.6.3-r5" \
        "libssl3>=3.6.3-r5" \
        bubblewrap \
        ca-certificates \
        "ca-certificates-bundle>=20260909-r3" \
        "uv>=0.13.6-r1" \
        "libbrotlicommon1>=1.2.0-r7" \
        "libbrotlidec1>=1.2.0-r7" \
        "libbrotlienc1>=1.2.0-r7" \
        "py3-pip-wheel>=26.2.2-r1" \
        "curl>=8.23.0-r0" \
        "libcurl-openssl4>=8.23.0-r0" \
        git \
        "git-lfs>=3.7.1" \
        gh \
        gnupg \
        socat \
        "wget>=1.25.0-r15"

# Shared libs the venv's source-built lxml/xmlsec link against (see py-build).
RUN apk add --no-cache xmlsec xmlsec-openssl libxml2-16 libxslt

RUN apk del --no-cache py3.15-pip py3.15-pip-base \
 && ! apk info -e py3.15-pip && ! apk info -e py3.15-pip-base

# Keep git-lfs for explicit pushes without enabling system-wide LFS filters.
RUN git lfs uninstall --system

FROM runtime AS build-runtime
RUN apk add --no-cache chromium

FROM cgr.dev/chainguard/python:latest-dev@sha256:b73b9e61fa9e0c6ec59c68ee2db1a08a4a61b969dbe03a39c309033749ef7f78 AS gateway
LABEL org.opencontainers.image.source="https://github.com/olafkfreund/AIFactory"
USER root
ARG SECURITY_REFRESH=0
RUN echo "security refresh: ${SECURITY_REFRESH}" \
 && apk upgrade --no-cache
RUN apk add --no-cache "py3-pip-wheel>=26.2.2-r1" "uv>=0.13.6-r1" \
 && apk del --no-cache py3.15-pip py3.15-pip-base \
 && ! apk info -e py3.15-pip && ! apk info -e py3.15-pip-base
