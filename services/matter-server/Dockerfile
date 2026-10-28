# syntax=docker/dockerfile:1.7
# MowDeck matter-server (ADR 0063): every mower as its own Matter node. Node 24 runs the
# TypeScript sources directly (type stripping), so there is no compile step.
# Build context: the repository root (pnpm workspace). Runs read-only as uid 1000 (node) with
# /data as its volume (Matter fabrics and setup credentials) and needs the host network (mDNS).
# Base image: public default, overridable with --build-arg (the pinned list is
# infra/compose/.env.example, which CI passes).

ARG NODE_IMAGE=public.ecr.aws/docker/library/node:24.21.0-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1

# The build stage runs on the build machine's platform: the dependencies are plain JavaScript (no
# native addons), so a multi-arch build needs no QEMU for them.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS build
WORKDIR /repo
# pnpm through Corepack: the version named by "packageManager" in the root package.json,
# checked against the sha512 recorded there.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable pnpm
COPY pnpm-workspace.yaml package.json pnpm-lock.yaml .npmrc ./
COPY apps/web/package.json apps/web/
COPY apps/mobile/package.json apps/mobile/
COPY libs/mowdeck-ui/package.json libs/mowdeck-ui/
COPY tools/ts-codegen/package.json tools/ts-codegen/
COPY tests/e2e-web/package.json tests/e2e-web/
COPY services/matter-server/package.json services/matter-server/
RUN --mount=type=cache,id=pnpm-store,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile --filter matter-server --prod --ignore-scripts
COPY services/matter-server/src services/matter-server/src
# A self-contained copy with the production dependencies only.
RUN --mount=type=cache,id=pnpm-store,target=/root/.local/share/pnpm/store \
    pnpm --filter matter-server deploy --prod --legacy --ignore-scripts /out

FROM ${NODE_IMAGE}
WORKDIR /app
COPY --from=build /out/package.json ./package.json
COPY --from=build /out/node_modules ./node_modules
COPY --from=build /out/src ./src
# The MowDeck licence texts (LICENSING.md).
COPY LICENSE LICENSING.md /usr/share/doc/mowdeck/
COPY LICENSES/ /usr/share/doc/mowdeck/LICENSES/
RUN mkdir -p /data && chown 1000:1000 /data
LABEL org.opencontainers.image.title="mowdeck-matter-server" \
      org.opencontainers.image.description="MowDeck Matter server: mowers in Apple Home, Google Home and Home Assistant" \
      org.opencontainers.image.source="https://github.com/lukislp/mowdeck" \
      org.opencontainers.image.url="https://github.com/lukislp/mowdeck" \
      org.opencontainers.image.licenses="AGPL-3.0-only AND Apache-2.0 AND MIT"
# The build's version and the address of its source code (AGPL-3.0 section 13, LICENSING.md).
ARG MOWDECK_VERSION=
ARG MOWDECK_SOURCE_URL=https://github.com/lukislp/mowdeck
ARG MOWDECK_SOURCE_REF=
ENV MOWDECK_VERSION=${MOWDECK_VERSION} \
    MOWDECK_SOURCE_URL=${MOWDECK_SOURCE_URL} \
    MOWDECK_SOURCE_REF=${MOWDECK_SOURCE_REF} \
    MOWDECK_MATTER_DATA_DIR=/data \
    NODE_ENV=production
USER 1000:1000
VOLUME ["/data"]
EXPOSE 8790
HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=10s \
  CMD wget -q --spider "http://127.0.0.1:${MOWDECK_MATTER_HTTP_PORT:-8790}/healthz" || exit 1
CMD ["node", "src/main.ts"]
