# One-shot job that creates the OpenFGA store "mowdeck" and writes the
# authorization model when it changed. The official CLI image has no shell,
# so its static binary is copied next to busybox and jq (all pinned). The base
# images are public defaults, overridable with --build-arg (the pinned list is
# infra/compose/.env.example, which CI passes).
ARG FGA_CLI_IMAGE=openfga/cli:v0.8.1@sha256:1b8035ea4aeb8ad739cbea972488cdda37886644ed004737880b63403bae0fe8
ARG JQ_IMAGE=ghcr.io/jqlang/jq:1.8.1@sha256:4f34c6d23f4b1372ac789752cc955dc67c2ae177eb1b5860b75cdc5091ce6f91
ARG BUSYBOX_IMAGE=public.ecr.aws/docker/library/busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e

FROM ${FGA_CLI_IMAGE} AS fga
FROM ${JQ_IMAGE} AS jq

FROM ${BUSYBOX_IMAGE}
COPY --from=fga /fga /usr/local/bin/fga
COPY --from=jq /jq /usr/local/bin/jq
COPY model.fga /model/model.fga
COPY bootstrap/bootstrap.sh /usr/local/bin/authz-bootstrap
USER 65534:65534
ENTRYPOINT ["/bin/sh", "/usr/local/bin/authz-bootstrap"]
