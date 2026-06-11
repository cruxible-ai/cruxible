# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.8.15-python3.11-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY packages/cruxible-client ./packages/cruxible-client

RUN uv sync --frozen --no-dev --extra server --no-editable

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    CRUXIBLE_HOST=0.0.0.0 \
    CRUXIBLE_PORT=8100 \
    CRUXIBLE_SERVER_AUTH=true \
    CRUXIBLE_SERVER_STATE_DIR=/var/lib/cruxible/server

RUN groupadd --system cruxible \
    && useradd \
        --system \
        --gid cruxible \
        --home-dir /var/lib/cruxible \
        --create-home \
        --shell /usr/sbin/nologin \
        cruxible \
    && mkdir -p /app /var/lib/cruxible/server \
    && chown -R cruxible:cruxible /app /var/lib/cruxible

WORKDIR /app

COPY --from=builder --chown=cruxible:cruxible /app/.venv /app/.venv
COPY --chown=root:root deploy/runtime/entrypoint.sh /usr/local/bin/cruxible-runtime-entrypoint

RUN chmod 0755 /usr/local/bin/cruxible-runtime-entrypoint

USER cruxible

EXPOSE 8100

ENTRYPOINT ["cruxible-runtime-entrypoint"]
CMD ["cruxible-server"]
