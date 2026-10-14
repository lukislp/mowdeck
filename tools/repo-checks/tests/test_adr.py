"""Guards for Architecture Decision Records."""

import re
from pathlib import Path

import pytest

REQUIRED = ("Status", "Context", "Decision", "Consequences")
ADR_NAME = re.compile(r"^\d{4}-[a-z0-9-]+\.md$")


def _adr_files(repo_root: Path) -> list[Path]:
    adr_dir = repo_root / "docs" / "adr"
    return sorted(p for p in adr_dir.glob("*.md") if p.name not in {"README.md", "template.md"})


def test_adrs_exist(repo_root: Path) -> None:
    assert _adr_files(repo_root), "docs/adr must contain at least one ADR"


def test_adr_names_and_headings(repo_root: Path) -> None:
    for path in _adr_files(repo_root):
        assert ADR_NAME.match(path.name), f"{path.name}: expected NNNN-kebab-case.md"
        text = path.read_text("utf-8")
        headings = {m.group(1).strip() for m in re.finditer(r"^#{2,3}\s+(.+)$", text, re.M)}
        for required in REQUIRED:
            assert required in headings, f"{path.name}: missing heading '{required}'"


def test_adr_numbers_unique(repo_root: Path) -> None:
    numbers = [p.name[:4] for p in _adr_files(repo_root)]
    assert len(numbers) == len(set(numbers)), "duplicate ADR numbers"


@pytest.mark.parametrize("name", ["README.md", "template.md"])
def test_adr_support_files(repo_root: Path, name: str) -> None:
    assert (repo_root / "docs" / "adr" / name).exists()
