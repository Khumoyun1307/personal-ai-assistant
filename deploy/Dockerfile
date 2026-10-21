# syntax=docker/dockerfile:1
# Build targets: `dev` (Compose dev stack) and `prod` (deploy).
# ARCHITECTURE §15.1 / §15.4.

ARG PYTHON_VERSION=3.14
ARG UV_VERSION=0.13.1

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim AS base
COPY --from=uv /uv /uvx /usr/local/bin/
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # The virtualenv lives outside the bind mount so Windows hosts never see it
    # and `uv sync` is not re-run from scratch on every container start.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:${PATH}"
WORKDIR /app

# ---------------------------------------------------------------- development
# Dependencies are installed by `just setup` into the /opt/venv volume, so the
# image stays valid while pyproject.toml changes. Runs as root deliberately:
# the source is a Docker Desktop bind mount from Windows and a non-root uid
# cannot reliably write uv.lock back to it.
FROM base AS dev
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*
CMD ["python", "-m", "assistant", "healthcheck"]

# ----------------------------------------------------------------- production
FROM base AS prod
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
RUN uv sync --frozen --no-dev \
    && groupadd --system assistant \
    && useradd --system --gid assistant --home-dir /app assistant \
    && chown -R assistant:assistant /app /opt/venv
USER assistant
CMD ["python", "-m", "assistant", "run"]
