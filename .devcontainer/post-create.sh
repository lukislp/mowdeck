#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

# Runs once when the Codespace is created (devcontainer.json, postCreateCommand): the Python and
# Node toolchains and the Compose stack's .env. The stack itself starts in post-start.sh.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# uv, at the version of the stack's images (UV_IMAGE in infra/compose/.env.example).
uv_version="$(sed -n 's|^UV_IMAGE=.*/uv:\([0-9.]*\)@.*|\1|p' infra/compose/.env.example)"
pip install --user --quiet "uv==${uv_version}"
export PATH="$HOME/.local/bin:$PATH"

# pnpm, at the version of package.json "packageManager" (Corepack reads it).
sudo env "PATH=$PATH" corepack enable

uv sync
pnpm install --frozen-lockfile
precommit=".venv/bin/pre-commit"
if [ -x "$precommit" ]; then "$precommit" install; fi

.devcontainer/generate-env.sh
