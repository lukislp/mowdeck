"""The API reference pages match the OpenAPI documents, and the renderer reads a small fixture."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

_DOCUMENT = {
    "openapi": "3.1.0",
    "info": {"title": "alpha", "version": "0.1.0"},
    "paths": {
        "/.well-known/mowdeck-module": {"get": {"summary": "Module Descriptor"}},
        "/api/v1/alpha/things/{thing_id}": {
            "get": {
                "tags": ["things"],
                "summary": "Get Thing",
                "description": "One thing (``alpha.view``).\n\n404 when not visible.",
                "parameters": [
                    {
                        "in": "path",
                        "name": "thing_id",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    },
                    {
                        "in": "query",
                        "name": "limit",
                        "required": False,
                        "schema": {"type": "integer", "maximum": 50, "default": 10},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "Successful Response",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}
                        },
                    }
                },
            },
            "delete": {
                "tags": ["things"],
                "summary": "Delete Thing",
                "responses": {"204": {"description": "Gone"}},
            },
        },
    },
    "components": {
        "schemas": {
            "Thing": {
                "description": "A thing.",
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "a|b"},
                    "parts": {"type": "array", "items": {"$ref": "#/components/schemas/Part"}},
                },
                "required": ["id"],
            },
            "Part": {"enum": ["a", "b"], "type": "string"},
            "Unused": {"type": "object"},
        }
    },
}


def _generator(repo_root: Path) -> ModuleType:
    path = repo_root / "tools/repo-checks/scripts/api_reference.py"
    spec = importlib.util.spec_from_file_location("api_reference", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(root: Path) -> Path:
    target = root / "apps/web/openapi/alpha.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(_DOCUMENT), encoding="utf-8")
    return root


def test_committed_pages_are_fresh(repo_root: Path) -> None:
    assert _generator(repo_root).main(["--root", str(repo_root), "--check"]) == 0, (
        "docs/reference/api is stale: run `uv run poe api-reference`"
    )


def test_every_service_document_has_a_page_in_the_nav(repo_root: Path) -> None:
    mkdocs = (repo_root / "mkdocs.yml").read_text(encoding="utf-8")
    for document in sorted((repo_root / "apps/web/openapi").glob("*.json")):
        assert f"reference/api/{document.stem}.md" in mkdocs, document.stem
    assert "reference/api/index.md" in mkdocs


def test_renders_operations_parameters_and_schemas(tmp_path: Path, repo_root: Path) -> None:
    pages = _generator(repo_root).build(_fixture(tmp_path))
    page = pages["alpha.md"]
    assert set(pages) == {"index.md", "alpha.md"}
    assert "mowdeck-module" not in page
    assert "#### GET /api/v1/alpha/things/{thing_id}" in page
    assert page.index("#### GET /api") < page.index("#### DELETE /api")
    assert "One thing (`alpha.view`).\n\n404 when not visible." in page
    assert "| `thing_id` | path | string (uuid) | yes |  |" in page
    assert "| `limit` | query | integer | no | max 50; default `10` |" in page
    assert "| 200 | Successful Response | `application/json` [Thing](#thing) |" in page
    assert "| 204 | Gone | no body |" in page
    assert "| `note` | string or null | no | a\\|b |" in page
    assert "| `parts` | array of [Part](#part) | no |  |" in page
    assert 'Type: `"a"` \\| `"b"`' in page
    assert "### Unused" not in page  # only schemas the operations use
    assert "| [alpha](alpha.md) | `/api/v1/alpha` | 2 |" in pages["index.md"]


def test_check_flags_stale_and_leftover_pages(tmp_path: Path, repo_root: Path) -> None:
    generator = _generator(repo_root)
    root = _fixture(tmp_path)
    assert generator.main(["--root", str(root), "--check"]) == 1
    assert generator.main(["--root", str(root)]) == 0
    assert generator.main(["--root", str(root), "--check"]) == 0
    (root / "docs/reference/api/gone.md").write_text("old\n", encoding="utf-8")
    assert generator.main(["--root", str(root), "--check"]) == 1
    assert generator.main(["--root", str(root)]) == 0
    assert not (root / "docs/reference/api/gone.md").exists()


def test_anchors_follow_the_toc_extension(repo_root: Path) -> None:
    anchors = _generator(repo_root).Anchors()
    assert anchors.add("GET /api/v1/fleet/mowers/{mower_id}") == "get-apiv1fleetmowersmower_id"
    assert anchors.add("things") == "things"
    assert anchors.add("Things") == "things_1"
