#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

# updateContentCommand (devcontainer.json): everything that follows from the checked-out sources,
# the Python workspace, the Node workspace and the pre-commit hook. Runs when the container is
# built (also inside a Codespaces prebuild) and again when a Codespace opens from a prebuild that
# is older than the branch, so the installed dependencies always match the lock files.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PATH="$HOME/.local/bin:$PATH"

uv sync
pnpm install --frozen-lockfile
precommit=".venv/bin/pre-commit"
if [ -x "$precommit" ]; then "$precommit" install; fi
