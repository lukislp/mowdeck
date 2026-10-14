# syntax=docker/dockerfile:1.7
#
# Image of the entitlements service. The build context is the repository root:
#
#   docker build -f services/entitlements/Dockerfile -t mowdeck/entitlements:dev .
#
# Only this service and the workspace libraries it depends on are installed. The runtime image
# runs as the non-root user "app" (uid 10001), writes nothing at runtime (read-only root file
# system; mount a tmpfs at /tmp) and has no architecture specific steps (amd64 and arm64).

ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.13-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.24

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY libs/ libs/
COPY services/entitlements/ services/entitlements/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package entitlements

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    MOWDECK_HTTP_PORT=8000
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv
# The default plan file (MOWDECK_ENTITLEMENTS_PLANS_FILE); Compose mounts the repository copy.
COPY infra/entitlements/plans.yaml /etc/mowdeck/entitlements/plans.yaml
WORKDIR /app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('MOWDECK_HTTP_PORT', '8000'), timeout=2)" || exit 1
CMD ["python", "-m", "entitlements.main"]
