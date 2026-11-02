"""The committed OpenAPI documents of the web app (apps/web/openapi) match the services."""

import importlib.util
from pathlib import Path
from types import ModuleType


def _exporter(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "repo-checks" / "scripts" / "export_openapi.py"
    spec = importlib.util.spec_from_file_location("export_openapi", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openapi_documents_are_fresh(repo_root: Path) -> None:
    exporter = _exporter(repo_root)
    stale = exporter.stale_files(repo_root / "apps" / "web" / "openapi")
    assert stale == [], f"stale OpenAPI documents {stale}: run `uv run poe openapi`"


def test_openapi_documents_cover_the_web_app_routes(repo_root: Path) -> None:
    exporter = _exporter(repo_root)
    documents = exporter.build_documents()
    assert set(documents) == {
        "identity.json",
        "fleet.json",
        "telemetry.json",
        "device-gateway.json",
        "realtime.json",
        "scheduler.json",
        "notifications.json",
        "entitlements.json",
        "billing.json",
        "planner.json",
        "vision.json",
        "assistant.json",
    }
    assert '"/api/v1/assistant/mowers/{mower_id}/proposals"' in documents["assistant.json"]
    assert '"/api/v1/vision/me/provider/structured"' in documents["vision.json"]
    assert '"/api/v1/scheduler/schedules/{schedule_id}/run-now"' in documents["scheduler.json"]
    assert '"/api/v1/identity/ui-areas"' in documents["identity.json"]
    assert '"/api/v1/telemetry/mowers/{mower_id}/map"' in documents["telemetry.json"]
    assert '"/api/v1/telemetry/mowers/{mower_id}/capabilities"' in documents["telemetry.json"]
    assert '"/api/v1/fleet/connection-kinds"' in documents["fleet.json"]
    assert '"/api/v1/notifications/inbox"' in documents["notifications.json"]
    assert '"/api/v1/entitlements/me"' in documents["entitlements.json"]
    assert '"/api/v1/billing/status"' in documents["billing.json"]
    assert '"/api/v1/billing/webhooks/stripe"' in documents["billing.json"]
    assert '"/api/v1/planner/plans/preview"' in documents["planner.json"]
    assert '"/api/v1/vision/mowers/{mower_id}/recover"' in documents["vision.json"]
    assert '"/api/v1/vision/me/provider"' in documents["vision.json"]
    assert '"/api/v1/identity/platform/accounts"' in documents["identity.json"]
