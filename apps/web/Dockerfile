# syntax=docker/dockerfile:1.7
# MowDeck web app: SvelteKit static build served by an unprivileged nginx (uid 101, port 8080).
# Build context: the repository root (pnpm workspace). Run read-only with a tmpfs on /tmp.
# Base images: public defaults, overridable with --build-arg (the pinned list is
# infra/compose/.env.example, which CI passes).

ARG NODE_IMAGE=public.ecr.aws/docker/library/node:22.23.3-alpine
ARG NGINX_IMAGE=public.ecr.aws/nginx/nginx-unprivileged:1.30.5-alpine

# The build stage runs on the build machine's platform: its output (static files) is the same for
# every target, so a multi-arch build (release workflow) compiles the app once, without QEMU.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS build
WORKDIR /repo
RUN npm install -g pnpm@10.34.6
COPY pnpm-workspace.yaml package.json pnpm-lock.yaml .npmrc ./
COPY apps/web/package.json apps/web/
COPY libs/mowdeck-ui/package.json libs/mowdeck-ui/
COPY tools/ts-codegen/package.json tools/ts-codegen/
COPY tests/e2e-web/package.json tests/e2e-web/
RUN --mount=type=cache,id=pnpm-store,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile --filter web...
COPY libs/mowdeck-ui libs/mowdeck-ui
COPY apps/web apps/web
# Compiles the messages (Paraglide), builds the SPA and writes build/csp.conf with the hashes of
# the inline bootstrap script.
RUN pnpm --filter web build

FROM ${NGINX_IMAGE}
COPY apps/web/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY apps/web/nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY --from=build /repo/apps/web/build/csp.conf /etc/nginx/snippets/csp.conf
# Renders /config.json and the final CSP from MOWDECK_WEB_MAP_* and MOWDECK_WEB_LEGAL_ENABLED
# into /tmp at container start.
COPY --chmod=0755 apps/web/nginx/40-mowdeck-runtime-config.sh /docker-entrypoint.d/40-mowdeck-runtime-config.sh
COPY --from=build /repo/apps/web/build /usr/share/nginx/html
USER 101
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD wget -q --spider http://127.0.0.1:8080/healthz || exit 1
