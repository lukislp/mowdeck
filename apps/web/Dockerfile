# syntax=docker/dockerfile:1.7
# MowDeck web app: SvelteKit static build served by an unprivileged nginx (uid 101, port 8080).
# Build context: the repository root (pnpm workspace). Run read-only with a tmpfs on /tmp.
# Base images: public defaults, overridable with --build-arg (the pinned list is
# infra/compose/.env.example, which CI passes).

ARG NODE_IMAGE=public.ecr.aws/docker/library/node:24.21.0-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1
ARG NGINX_IMAGE=public.ecr.aws/nginx/nginx-unprivileged:1.30.5-alpine@sha256:56b8272e43a0525f67aed68d1e0794a712d10e3e45a563e118e9a77c817e6f86

# The build stage runs on the build machine's platform: its output (static files) is the same for
# every target, so a multi-arch build (release workflow) compiles the app once, without QEMU.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS build
WORKDIR /repo
# pnpm through Corepack (bundled with Node 24; Node 25 and later no longer ship it): the first pnpm call downloads the version named by
# "packageManager" in the root package.json and checks it against the sha512 recorded there.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable pnpm
COPY pnpm-workspace.yaml package.json pnpm-lock.yaml .npmrc ./
COPY apps/web/package.json apps/web/
COPY libs/mowdeck-ui/package.json libs/mowdeck-ui/
COPY tools/ts-codegen/package.json tools/ts-codegen/
COPY tests/e2e-web/package.json tests/e2e-web/
RUN --mount=type=cache,id=pnpm-store,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile --filter web...
COPY libs/mowdeck-ui libs/mowdeck-ui
COPY apps/web apps/web
# The Agora Web SDK (proprietary, direct vendor camera path of ADR 0032) is left out by default:
# a published image never contains it, the camera relay (ADR 0045) serves every browser. Only an
# operator's own image may include it (--build-arg WEB_AGORA_SDK=include, apps/web/README.md);
# the build fails for any other value and for any Agora package in a build without it.
ARG WEB_AGORA_SDK=exclude
# Compiles the messages (Paraglide), builds the SPA and writes build/csp.conf with the hashes of
# the inline bootstrap script, plus THIRD-PARTY-LICENSES.txt and third-party-licences.json of the
# npm packages in the bundle (build-tools/release-licences.ts).
RUN MOWDECK_WEB_AGORA_SDK="$WEB_AGORA_SDK" pnpm --filter web build

FROM ${NGINX_IMAGE}
COPY apps/web/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY apps/web/nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY apps/web/nginx/security-headers-track.conf /etc/nginx/snippets/security-headers-track.conf
# The API explorer's frame (/_explorer/, ADR 0072) has its own CSP; it ships in build/_explorer
# and is served only with MOWDECK_WEB_API_EXPLORER=true.
COPY apps/web/nginx/security-headers-explorer.conf /etc/nginx/snippets/security-headers-explorer.conf
COPY --from=build /repo/apps/web/build/csp.conf /etc/nginx/snippets/csp.conf
# Renders /config.json and the final CSP from MOWDECK_WEB_MAP_* and MOWDECK_WEB_LEGAL_ENABLED
# into /tmp at container start.
COPY --chmod=0755 apps/web/nginx/40-mowdeck-runtime-config.sh /docker-entrypoint.d/40-mowdeck-runtime-config.sh
COPY --from=build /repo/apps/web/build /usr/share/nginx/html
# The MowDeck licence texts (LICENSING.md); the bundle's third-party notices are served as
# /THIRD-PARTY-LICENSES.txt. The labels replace the ones inherited from the nginx base image.
COPY LICENSE LICENSING.md /usr/share/doc/mowdeck/
COPY LICENSES/ /usr/share/doc/mowdeck/LICENSES/
LABEL org.opencontainers.image.title="mowdeck-web"       org.opencontainers.image.description="MowDeck web app"       org.opencontainers.image.source="https://github.com/lukislp/mowdeck"       org.opencontainers.image.url="https://github.com/lukislp/mowdeck"       org.opencontainers.image.revision=""       org.opencontainers.image.version=""       org.opencontainers.image.licenses="AGPL-3.0-only AND Apache-2.0"
# The build's version and the address of its source code (AGPL-3.0 section 13, LICENSING.md):
# set by the release build (MOWDECK_SOURCE_REF: the tag v<version>, or the commit of a test
# build), overridable at runtime. A fork that runs modified code sets its own repository.
ARG MOWDECK_VERSION=
ARG MOWDECK_SOURCE_URL=https://github.com/lukislp/mowdeck
ARG MOWDECK_SOURCE_REF=
ENV MOWDECK_VERSION=${MOWDECK_VERSION} \
    MOWDECK_SOURCE_URL=${MOWDECK_SOURCE_URL} \
    MOWDECK_SOURCE_REF=${MOWDECK_SOURCE_REF}
USER 101
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD wget -q --spider http://127.0.0.1:8080/healthz || exit 1
