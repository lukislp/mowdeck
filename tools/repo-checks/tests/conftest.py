"""Shared fixtures for repository convention checks."""

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Return the repository root (the directory containing the workspace pyproject.toml)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "mkdocs.yml").exists() and (parent / "docs").is_dir():
            return parent
    msg = "repository root not found"
    raise RuntimeError(msg)
