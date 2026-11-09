# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The dev container (docs/adr/0080-codespaces-dev-container.md) installs only pinned tools.

uv comes from the stack's uv image, pinned by digest, instead of an unpinned ``pip install``
(OpenSSF Scorecard's Pinned-Dependencies check flags those): the image must stay the
``UV_IMAGE`` of ``infra/compose/.env.example``, so the dev container runs the same uv as the
stack's images.
"""

import re
from pathlib import Path

COPY_FROM_RE = re.compile(r"^COPY\s+--from=(\S+)", re.MULTILINE)
ENV_LINE_RE = re.compile(r"^(\w+_IMAGE)=(\S*)\s*$", re.MULTILINE)


def test_dev_container_copies_the_pinned_uv(repo_root: Path) -> None:
    dockerfile = (repo_root / ".devcontainer" / "Dockerfile").read_text("utf-8")
    env = (repo_root / "infra" / "compose" / ".env.example").read_text("utf-8")
    images = dict(ENV_LINE_RE.findall(env))
    assert COPY_FROM_RE.findall(dockerfile) == [images["UV_IMAGE"]]


def test_lifecycle_scripts_install_nothing_with_pip(repo_root: Path) -> None:
    for script in sorted((repo_root / ".devcontainer").glob("*.sh")):
        text = script.read_text("utf-8")
        assert not re.search(r"\bpip3? install\b", text), script.name
