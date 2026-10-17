# syntax=docker/dockerfile:1.7
# SPDX-License-Identifier: Apache-2.0
#
# The agora-bridge image (ADR 0045). BUILD IT YOURSELF; IT IS NEVER PUBLISHED. The image contains
# Agora's proprietary native SDK, which MowDeck does not redistribute: building this image
# downloads it from Agora and means accepting Agora's SDK licence terms (see README.md). No CI
# job builds or pushes it. The build context is this directory:
#
#   docker build --platform linux/amd64 -t mowdeck-local/agora-bridge:local integrations/agora-bridge
#
# What happens (docker/install_sdk.py):
#   * agora-python-server-sdk 2.4.9 (MIT, sdist) and the native SDK 4.4.32.174 (x86_64 Linux)
#     are downloaded over verified TLS and checked against pinned sizes and SHA-256 digests;
#   * only the wrapper's agora/ package is copied: its setup.py (which downloads without a
#     hash check and disables TLS verification) never runs, and pip is not used;
#   * agora/version.txt names the pinned archive, so the wrapper's import never downloads;
#     the build proves it by running the bridge's --check without network access;
#   * the bridge restores TLS verification after the import and checks it (agora_bridge.guard).
#
# The runtime runs as the non-root user 10001 with a read-only root file system (the SDK's log
# goes to a private directory under /tmp, a tmpfs). It listens on 127.0.0.1:7700 inside the
# camera relay's network namespace (Compose: network_mode service:camera-relay; Helm: a second
# container in the relay's pod). x86_64 only: the pinned native SDK is the x86_64 build.

ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.13-slim

FROM ${PYTHON_IMAGE} AS sdk
COPY docker/install_sdk.py /build/install_sdk.py
RUN python -I /build/install_sdk.py /opt/agora-sdk \
    && missing=$(LD_LIBRARY_PATH=/opt/agora-sdk/agora/agora_sdk        ldd /opt/agora-sdk/agora/agora_sdk/libagora_rtc_sdk.so | grep 'not found' || true) \
    && if [ -n "$missing" ]; then echo "$missing"; exit 1; fi \
    && rm -r /opt/agora-sdk/agora/agora_sdk/include \
    && python -m compileall -q /opt/agora-sdk

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/agora-bridge:/opt/agora-sdk \
    TMPDIR=/tmp \
    AGORA_BRIDGE_LISTEN=127.0.0.1:7700
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin app
COPY --from=sdk /opt/agora-sdk /opt/agora-sdk
COPY src/agora_bridge /opt/agora-bridge/agora_bridge
RUN python -m compileall -q /opt/agora-bridge
USER 10001:10001
# Proof that the import needs no network: the SDK loads offline and TLS verification is intact.
RUN --network=none python -m agora_bridge --check
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD python -c "import os, socket; h, p = os.environ['AGORA_BRIDGE_LISTEN'].rsplit(':', 1); socket.create_connection((h, int(p)), 2).close()" || exit 1
CMD ["python", "-m", "agora_bridge.server"]
