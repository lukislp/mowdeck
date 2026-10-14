"""The committed JSON Schemas must equal a fresh export."""

import json
from pathlib import Path

import pytest
from mowdeck_contracts import schemas

COMMITTED = Path(__file__).resolve().parents[1] / "schemas"
REGENERATE = "run: uv run python -m mowdeck_contracts.schemas libs/mowdeck-contracts/schemas"


def _load_tree(root: Path) -> dict[str, object]:
    return {
        path.relative_to(root).as_posix(): json.loads(path.read_text("utf-8"))
        for path in sorted(root.rglob("*.json"))
    }


def test_committed_schemas_are_current(tmp_path: Path) -> None:
    schemas.export_schemas(tmp_path)
    fresh = _load_tree(tmp_path)
    committed = _load_tree(COMMITTED)
    assert sorted(committed) == sorted(fresh), f"schema files differ; {REGENERATE}"
    for name, schema in fresh.items():
        assert committed[name] == schema, f"{name} is outdated; {REGENERATE}"


def test_export_removes_stale_files_and_is_deterministic(tmp_path: Path) -> None:
    stale = tmp_path / "v1" / "Removed.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")
    first = schemas.export_schemas(tmp_path)
    assert not stale.exists()
    contents = [p.read_bytes() for p in first]
    second = schemas.export_schemas(tmp_path)
    assert [p.read_bytes() for p in second] == contents
    command = json.loads((tmp_path / "v1" / "CommandEnvelope.json").read_text("utf-8"))
    assert command["$schema"] == schemas.JSON_SCHEMA_DIALECT
    assert command["title"] == "CommandEnvelope"


def test_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert schemas.main([str(tmp_path)]) == 0
    assert "wrote" in capsys.readouterr().out
    assert (tmp_path / "v1" / "MowerState.json").is_file()
    assert schemas.main(["--help"]) == 0
    assert schemas.main(["a", "b"]) == 2


def test_main_default_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert schemas.main([]) == 0
    assert (tmp_path / "schemas" / "v1" / "EventEnvelope.json").is_file()
