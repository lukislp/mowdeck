"""The documentation site must build in strict mode."""

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.mark.skipif(importlib.util.find_spec("mkdocs") is None, reason="mkdocs is not importable")
def test_mkdocs_build_strict(repo_root: Path) -> None:
    with tempfile.TemporaryDirectory() as out:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", out],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr
