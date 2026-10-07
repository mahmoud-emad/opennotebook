# syntax=docker/dockerfile:1
# The server image: one image for the api, the worker and the migrate step;
# compose picks which by its command. Built from the repository root:
#   docker build -f deploy/server.Dockerfile .

FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

# ── build: resolve the locked dependencies into /app/.venv ───────────────────
FROM python:3.14.8-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# The dependencies first, from the lock alone, so a change to the code does
# not reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=server/uv.lock,target=uv.lock \
    --mount=type=bind,source=server/pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# Then the package itself. alembic.ini finds the migrations beside it.
COPY server/ /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ── run: the slim image, the code and its venv, a user that is not root ──────
FROM python:3.14.8-slim-trixie
# Videos (docs/video-overview-spec.md): ffmpeg encodes them, as a program of
# its own and never linked in (its x264 is GPL); skia draws a whiteboard's
# frames on the CPU but loads libEGL and libGL all the same.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libegl1 libgl1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 10001 --home-dir /app opennotebook \
    && mkdir -p /data/files \
    && chown opennotebook /data/files
COPY --from=build --chown=opennotebook /app /app
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    OPENNOTEBOOK_FILES_DIR=/data/files
USER opennotebook
EXPOSE 8000
CMD ["opennotebook", "serve", "--host", "0.0.0.0", "--port", "8000"]
