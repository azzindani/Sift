# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────────
# Sift — production container.
#
# Two-stage build on python:3.12-slim. uv resolves and installs into a venv in
# stage 1; stage 2 copies the venv and adds only what the render path actually
# shells out to (ffmpeg). yt-dlp is a pinned Python dependency, not a PATH
# binary, so its version is locked by uv.lock rather than by the base image.
#
#   build    → uv sync --frozen (+ optional vision extra)
#   runtime  → /app/.venv + source; unprivileged `sift` user; tini as PID 1
#
# Build:            docker build -t sift:latest .
# With face-follow: docker build --build-arg VISION=1 -t sift:latest .
#                   (MediaPipe, ~300 MB. Without it, reframe="speaker" degrades
#                    to a centred crop and says so in the job progress.)
#
# Run (HTTP + token):
#   docker run --rm -p 8765:8765 -e SIFT_API_KEY=sk-... \
#              -v $PWD/sift-projects:/home/sift/projects sift:latest
#
# Auth (same contract as Folio):
#   Single token:  -e SIFT_API_KEY=sk-...
#   Multi inline:  -e SIFT_TOKENS='claude:sk-a,hermes:sk-b'
#   Multi file:    -v $PWD/tokens.json:/home/sift/tokens.json:ro \
#                  -e SIFT_TOKENS_FILE=/home/sift/tokens.json
#
# Persistence:
#   /home/sift/projects   the library — YAML records + rendered clips. THE DATA.
#   /home/sift/.sift      job DB + scratch. Rebuildable; safe to lose.
#
# For a public deployment use docker-compose.yml with --profile tls, which puts
# Caddy in front and terminates HTTPS for SIFT_DOMAIN.
# ─────────────────────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.12

# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

WORKDIR /app

# Lockfile-first: dependency resolution is cached independently of source changes,
# so editing a .py file does not re-resolve the whole tree.
COPY pyproject.toml uv.lock README.md ./

ARG VISION=0
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "${VISION}" = "1" ]; then \
        uv sync --frozen --no-dev --extra vision; \
    else \
        uv sync --frozen --no-dev; \
    fi

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

# ffmpeg carries libx264 (encode) and libass (caption burn-in); ffprobe ships with
# it. tini reaps the ffmpeg children the render worker spawns — without a real PID 1
# a killed encode becomes a zombie. curl is only here for the HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tini \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264 \
    && ffmpeg -hide_banner -filters 2>/dev/null | grep -q subtitles

# Unprivileged user with a stable home, so the paths in the docs are the real paths.
RUN groupadd --system sift \
    && useradd --system --gid sift --home-dir /home/sift --shell /bin/bash --create-home sift

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SIFT_HOST=0.0.0.0 \
    SIFT_PORT=8765 \
    SIFT_PROJECTS_DIR=/home/sift/projects \
    SIFT_DATA_DIR=/home/sift/.sift \
    MCP_CONSTRAINED_MODE=1 \
    SIFT_FORCE_IPV4=1

WORKDIR /app

COPY --from=builder --chown=sift:sift /app/.venv ./.venv
COPY --chown=sift:sift server.py engine.py _clip_*.py ./
COPY --chown=sift:sift shared ./shared
COPY --chown=sift:sift skills ./skills
COPY --chown=sift:sift assets ./assets

# Pre-create the mount points so an unmounted `docker run` still works.
RUN mkdir -p "${SIFT_PROJECTS_DIR}" "${SIFT_DATA_DIR}" && chown -R sift:sift /home/sift

USER sift
EXPOSE 8765
VOLUME ["/home/sift/projects"]

# /health is public by design, so the probe needs no token. It returns 503 when the
# toolchain is broken, which is exactly when the container should be considered sick.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${SIFT_PORT}/health" >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "server.py", "--transport", "http"]
