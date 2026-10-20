# syntax=docker/dockerfile:1.7
#
# Image of the vision service. The build context is the repository root:
#
#   docker build -f services/vision/Dockerfile -t mowdeck/vision:dev .
#
# Only this service and the workspace libraries it depends on are installed. The runtime image
# runs as the non-root user "app" (uid 10001), writes nothing at runtime (read-only root file
# system; mount a tmpfs at /tmp) and has no architecture specific steps (amd64 and arm64).
#
# The capture worker (ADR 0035, `python -m vision.capture`) needs a browser: build with
#
#   docker build -f services/vision/Dockerfile --build-arg WITH_CAPTURE=true \
#     -t mowdeck/vision-capture:dev .
#
# which adds the extra `capture` (Playwright), Playwright's Chromium headless shell with its
# system libraries and the pinned Agora Web SDK (checked against its npm sha512) at
# /opt/agora. Without the argument (the default) the image stays without a browser.

ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
ARG WITH_CAPTURE=false
ARG AGORA_SDK_VERSION=4.24.8
ARG AGORA_SDK_SHA512=TkHDu1HxYcjfbvpmOfOAucSgMJRlphODQlVN6fwAMCh7yIKt6J4O3wITNCvUkEVi9aaZTjeJOZ+onFUu/zlnuw==
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY libs/ libs/
COPY services/vision/ services/vision/
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$WITH_CAPTURE" = "true" ]; then extra="--extra capture"; else extra=""; fi \
    && uv sync --frozen --no-dev --no-editable --package vision $extra
# The Agora Web SDK for the capture worker: the UMD bundle of the npm package the web app pins.
RUN mkdir -p /opt/agora \
    && if [ "$WITH_CAPTURE" = "true" ]; then \
         python3 services/vision/docker/fetch_agora_sdk.py \
           "$AGORA_SDK_VERSION" "$AGORA_SDK_SHA512" /opt/agora; \
       fi

FROM ${PYTHON_IMAGE} AS runtime
ARG WITH_CAPTURE=false
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    MOWDECK_HTTP_PORT=8000 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv
# The MowDeck licence texts (LICENSING.md). Third-party notices: each package's dist-info and
# /usr/share/doc.
COPY LICENSE LICENSING.md /usr/share/doc/mowdeck/
COPY LICENSES/ /usr/share/doc/mowdeck/LICENSES/
LABEL org.opencontainers.image.licenses="AGPL-3.0-only AND Apache-2.0"
COPY --from=build /opt/agora /opt/agora
# Chromium headless shell and its system libraries (apt), only for the capture worker.
RUN if [ "$WITH_CAPTURE" = "true" ]; then \
      python -m playwright install --with-deps --only-shell chromium \
      && rm -rf /var/lib/apt/lists/* /root/.cache; \
    fi
WORKDIR /app
# The build's version and the address of its source code (AGPL-3.0 section 13, LICENSING.md):
# set by the release build (MOWDECK_SOURCE_REF: the tag v<version>, or the commit of a test
# build), overridable at runtime. A fork that runs modified code sets its own repository.
ARG MOWDECK_VERSION=
ARG MOWDECK_SOURCE_URL=https://github.com/lukislp/mowdeck
ARG MOWDECK_SOURCE_REF=
ENV MOWDECK_VERSION=${MOWDECK_VERSION} \
    MOWDECK_SOURCE_URL=${MOWDECK_SOURCE_URL} \
    MOWDECK_SOURCE_REF=${MOWDECK_SOURCE_REF}
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('MOWDECK_HTTP_PORT', '8000'), timeout=2)" || exit 1
CMD ["python", "-m", "vision.main"]
