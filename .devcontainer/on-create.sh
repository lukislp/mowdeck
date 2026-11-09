#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

# onCreateCommand (devcontainer.json): the tools that do not depend on the checked-out sources,
# uv and pnpm. Runs when the container is built, also inside a Codespaces prebuild, so a Codespace
# opened from a prebuild already has them. The dependencies follow in update-content.sh, the
# Compose stack's .env in generate-env.sh (postCreateCommand), the stack itself in post-start.sh.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# uv is part of the image (.devcontainer/Dockerfile copies it from the pinned UV_IMAGE).
uv --version

# pnpm, at the version of package.json "packageManager" (Corepack reads it).
sudo env "PATH=$PATH" corepack enable
