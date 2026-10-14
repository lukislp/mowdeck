# One-shot job that creates the OpenFGA store "mowdeck" and writes the
# authorization model when it changed. The official CLI image has no shell,
# so its static binary is copied next to busybox and jq (all pinned). The base
# images are public defaults, overridable with --build-arg (the pinned list is
# infra/compose/.env.example, which CI passes).
ARG FGA_CLI_IMAGE=openfga/cli:v0.8.1
ARG JQ_IMAGE=ghcr.io/jqlang/jq:1.8.1
ARG BUSYBOX_IMAGE=public.ecr.aws/docker/library/busybox:1.37.0

FROM ${FGA_CLI_IMAGE} AS fga
FROM ${JQ_IMAGE} AS jq

FROM ${BUSYBOX_IMAGE}
COPY --from=fga /fga /usr/local/bin/fga
COPY --from=jq /jq /usr/local/bin/jq
COPY model.fga /model/model.fga
COPY bootstrap/bootstrap.sh /usr/local/bin/authz-bootstrap
USER 65534:65534
ENTRYPOINT ["/bin/sh", "/usr/local/bin/authz-bootstrap"]
