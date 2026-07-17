# docker

> Source: curated best practices | 2026

---

# Docker - Production images: multi-stage, non-root, minimal

Docker images that ship to production should be small, reproducible, and run as an unprivileged user with no build tooling in the final layer. The core techniques are multi-stage builds (compile/install in a fat stage, copy only artifacts into a slim runtime), pinned base images, non-root `USER`, `.dockerignore`, healthchecks, and layer ordering that maximizes cache hits. This skill covers production Dockerfiles for compiled and interpreted languages, security hardening, and image slimming.

## When to Activate

Use when the task involves Docker:
- Writing or reviewing a Dockerfile
- Slimming an image or fixing a bloated/slow build
- Hardening a container (non-root, read-only, dropped caps)
- Multi-stage builds, caching, or `.dockerignore`
- Container healthchecks or entrypoints

## Patterns and Best Practices

### Multi-stage build (Python) — build fat, ship slim, run non-root

```dockerfile
# ---- build stage: has compilers, dev headers ----
FROM python:3.12-slim AS build
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# Copy deps first so this layer caches until requirements change
COPY requirements.txt .
RUN python -m venv /venv && /venv/bin/pip install -r requirements.txt

# ---- runtime stage: no build tools, minimal ----
FROM python:3.12-slim AS runtime
# Create an unprivileged user
RUN useradd --system --uid 10001 --no-create-home appuser
WORKDIR /app
COPY --from=build /venv /venv
COPY --chown=appuser:appuser . .
ENV PATH="/venv/bin:$PATH" PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"
ENTRYPOINT ["gunicorn", "-b", "0.0.0.0:8000", "app:app"]
```

### Multi-stage build (Go) — static binary in a distroless/scratch runtime

```dockerfile
FROM golang:1.23 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download                       # cached until go.mod/sum change
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /app ./cmd/server

FROM gcr.io/distroless/static:nonroot     # no shell, no package manager, runs as nonroot
COPY --from=build /app /app
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/app"]
```

`distroless`/`scratch` have no shell and no CVE surface from OS packages — the smallest, most secure runtime for static binaries.

### Layer caching and .dockerignore

Order instructions least-to-most volatile: base → system deps → dependency manifests → `pip/npm install` → application source. Copying source before installing deps busts the dependency cache on every code change.

```
# .dockerignore — keep the build context small and secrets out
.git
.venv
node_modules
**/__pycache__
*.env
.env*
Dockerfile
```

### Security hardening at build and run

```dockerfile
# In the Dockerfile: pin base by digest for reproducibility
FROM python:3.12-slim@sha256:<digest>
```

```bash
# At runtime: least privilege
docker run --read-only --tmpfs /tmp \
  --cap-drop=ALL --security-opt no-new-privileges \
  --user 10001 --memory=512m --pids-limit=200 myimage
```

- Always set a non-root `USER` (numeric UID so k8s `runAsNonRoot` can verify it).
- Pin the base image by digest for reproducible, tamper-evident builds.
- Never `COPY` secrets into a layer — use build secrets (`RUN --mount=type=secret`) or runtime env/mounts.
- Combine `apt-get update && install` in one `RUN` with `--no-install-recommends` and `rm -rf /var/lib/apt/lists/*` to avoid a stale/bloated cache layer.

### Build secrets (BuildKit) — no secret in image history

```dockerfile
RUN --mount=type=secret,id=pip_token \
    pip install --extra-index-url "https://$(cat /run/secrets/pip_token)@pkgs/simple" -r requirements.txt
```

## Anti-patterns

- Single-stage image shipping compilers, headers, and caches into production.
- Running as root (no `USER`) — container breakout runs as host root.
- `COPY . .` before installing dependencies — destroys layer cache on every code edit.
- `latest` tag or unpinned base — non-reproducible builds.
- Baking secrets (API keys, `.env`, private keys) into a layer — they persist in image history forever.
- `apt-get install` without `--no-install-recommends` or cleanup — bloated layers.
- No `.dockerignore` — huge build context, leaked `.git`/`.env`.
- No `HEALTHCHECK` and no resource limits at runtime.
