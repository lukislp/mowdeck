# syntax=docker/dockerfile:1.7
#
# Image of the camera-relay service. The build context is the repository root:
#
#   docker build -f services/camera-relay/Dockerfile -t mowdeck/camera-relay:dev .
#
# Only this service and the workspace libraries it depends on are installed. The runtime image
# runs as the non-root user "app" (uid 10001), writes nothing at runtime (read-only root file
# system; mount a tmpfs at /tmp) and has no architecture specific steps (amd64 and arm64).
#
# The source pipeline (fake source, transcoder) runs Debian's ffmpeg with libx264 and the
# DejaVu font for the fake source's overlay. ffmpeg and libx264 are GPL: fine in this AGPL
# image (the relay only runs the binary). Kept lean: no recommended packages, and the GPU
# driver stack that Debian's ffmpeg pulls in only to load at runtime (LLVM, Mesa's Gallium
# drivers, Z3; about 200 MB) is removed. The build fails when ffmpeg would miss a linked
# library or cannot encode H.264 afterwards.

ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a

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
COPY services/camera-relay/ services/camera-relay/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package camera-relay

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    MOWDECK_HTTP_PORT=8000
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && unused=$(dpkg-query -W -f '${Package}\n' 'libllvm*' mesa-libgallium 'libz3-*' \
       libgl1-mesa-dri mesa-vulkan-drivers 2>/dev/null || true) \
    && if [ -n "$unused" ]; then dpkg --purge --force-depends $unused; fi \
    && if ldd /usr/bin/ffmpeg | grep -q 'not found'; then ldd /usr/bin/ffmpeg; exit 1; fi \
    && ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=size=64x48:rate=10 \
       -frames:v 3 -c:v libx264 -profile:v baseline -pix_fmt yuv420p -f null - \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/log/apt /var/log/dpkg.log
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv
WORKDIR /app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('MOWDECK_HTTP_PORT', '8000'), timeout=2)" || exit 1
CMD ["python", "-m", "camera_relay.main"]
