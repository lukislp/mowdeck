# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

# The dev container's base image (devcontainer.json "build"): the tag 1-3.13-bookworm of
# mcr.microsoft.com/devcontainers/python, pinned by digest like the stack's base images. Digest
# only, because the Dev Container CLI does not parse "tag@digest"; bump by looking the tag up
# again (`docker buildx imagetools inspect mcr.microsoft.com/devcontainers/python:1-3.13-bookworm`).
FROM mcr.microsoft.com/devcontainers/python@sha256:0aa711e570b306c02946cdda67587ce8c65978dbb65691341cbcfd4854dfcfff

# The image ships the Yarn apt repository, whose signing key is no longer accepted
# (NO_PUBKEY 62D54FD4003F6525), so every `apt-get update` fails and with it every Dev Container
# feature that installs packages (Docker-in-Docker was the first to break). Nothing here needs
# Yarn: pnpm comes through Corepack (on-create.sh). Remove the source before the features run.
RUN rm -f /etc/apt/sources.list.d/yarn.list /etc/apt/sources.list.d/yarn.sources \
    && rm -f /usr/share/keyrings/yarn-archive-keyring.gpg /etc/apt/trusted.gpg.d/yarn.gpg
