"""Export the OpenAPI document of every service for the web app's generated TypeScript types.

Usage (from the repository root)::

    uv run poe openapi            # writes apps/web/openapi/<service>.json
    uv run poe openapi-check      # exit 1 when a committed document is stale

The services are built in memory (fake bus, store and authorizer, no background tasks, nothing
started), so no running stack is needed. Documents are rendered deterministically (sorted keys,
two space indent, trailing newline), so a diff shows only real API changes. The repository
checks compare the committed files with a fresh export, so CI fails when a route changes without
regenerating (like the contracts JSON Schemas).
"""

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI

OUT_DIR = Path("apps/web/openapi")
"""Default output directory, relative to the repository root."""

_TEST_KEK = "ZGV2LW9ubHkta2V5LWVuY3J5cHRpb24ta2V5LTAwMDA="  # the documented development key
_COMMON: dict[str, Any] = {"environment": "test", "log_format": "console", "nats_enabled": False}


def _identity() -> FastAPI:
    from mowdeck_authz.testing import InMemoryAuthorizer
    from mowdeck_service_kit.bus import InMemoryBus

    from identity.adapters.crypto import Argon2PasswordHasher
    from identity.adapters.memory import MemoryStore
    from identity.main import build_app
    from identity.settings import Settings

    authz = InMemoryAuthorizer()
    settings = Settings.model_validate({**_COMMON, "identity_key_encryption_key": _TEST_KEK})
    return build_app(
        settings,
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=authz,
        tuple_backend=authz,
        hasher=Argon2PasswordHasher("insecure_test"),
        background_tasks=False,
    )


def _fleet() -> FastAPI:
    from mowdeck_authz.testing import InMemoryAuthorizer
    from mowdeck_service_kit.bus import InMemoryBus

    from fleet.adapters.memory import MemoryStore
    from fleet.main import build_app
    from fleet.settings import Settings

    authz = InMemoryAuthorizer()
    return build_app(
        Settings.model_validate(_COMMON),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=authz,
        tuple_backend=authz,
        background_tasks=False,
    )


def _telemetry() -> FastAPI:
    from mowdeck_authz.testing import InMemoryAuthorizer
    from mowdeck_service_kit.bus import InMemoryBus

    from telemetry.adapters.memory import MemoryStore
    from telemetry.main import build_app
    from telemetry.settings import Settings

    return build_app(
        Settings.model_validate(_COMMON),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=InMemoryAuthorizer(),
        background_tasks=False,
    )


def _device_gateway() -> FastAPI:
    from mowdeck_service_kit.bus import InMemoryBus

    from device_gateway.main import build_app
    from device_gateway.settings import Settings

    return build_app(Settings.model_validate(_COMMON), bus=InMemoryBus())


def _realtime() -> FastAPI:
    from mowdeck_authz.testing import InMemoryAuthorizer
    from mowdeck_service_kit.bus import InMemoryBus

    from realtime.main import build_app
    from realtime.settings import Settings

    return build_app(
        Settings.model_validate(_COMMON),
        bus=InMemoryBus(),
        authorizer=InMemoryAuthorizer(),
        background_tasks=False,
    )


def _scheduler() -> FastAPI:
    from mowdeck_authz.testing import InMemoryAuthorizer
    from mowdeck_service_kit.bus import InMemoryBus

    from scheduler.adapters.memory import MemoryStore
    from scheduler.main import build_app
    from scheduler.settings import Settings

    return build_app(
        Settings.model_validate({**_COMMON, "scheduler_weather_provider": "static"}),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=InMemoryAuthorizer(),
        background_tasks=False,
    )


def _notifications() -> FastAPI:
    from mowdeck_authz.testing import InMemoryAuthorizer
    from mowdeck_service_kit.bus import InMemoryBus

    from notifications.adapters.memory import MemoryStore
    from notifications.main import build_app
    from notifications.settings import Settings

    return build_app(
        Settings.model_validate({**_COMMON, "notifications_channels": "log"}),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=InMemoryAuthorizer(),
        background_tasks=False,
    )


def _entitlements() -> FastAPI:
    from mowdeck_authz.testing import InMemoryAuthorizer
    from mowdeck_service_kit.bus import InMemoryBus

    from entitlements.adapters.memory import MemoryStore
    from entitlements.main import build_app
    from entitlements.settings import Settings

    return build_app(
        Settings.model_validate(_COMMON),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=InMemoryAuthorizer(),
        plan_specs=[],
        background_tasks=False,
    )


def _billing() -> FastAPI:
    from mowdeck_authz.testing import InMemoryAuthorizer
    from mowdeck_service_kit.bus import InMemoryBus

    from billing.adapters.memory import MemoryStore
    from billing.adapters.plans import StaticPlanCatalog
    from billing.adapters.providers.fake import FakeProvider
    from billing.domain.prices import PriceTable
    from billing.main import build_app
    from billing.settings import Settings

    return build_app(
        Settings.model_validate({**_COMMON, "billing_provider": "fake"}),
        bus=InMemoryBus(),
        store=MemoryStore(),
        provider=FakeProvider(),
        authorizer=InMemoryAuthorizer(),
        catalog=StaticPlanCatalog(),
        prices=PriceTable(),
    )


def _planner() -> FastAPI:
    from mowdeck_authz.testing import InMemoryAuthorizer
    from mowdeck_service_kit.bus import InMemoryBus

    from planner.adapters.memory import MemoryStore
    from planner.main import build_app
    from planner.settings import Settings

    return build_app(
        Settings.model_validate(_COMMON),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=InMemoryAuthorizer(),
    )


def _vision() -> FastAPI:
    from mowdeck_authz.testing import InMemoryAuthorizer
    from mowdeck_service_kit.bus import InMemoryBus
    from mowdeck_service_kit.testing import FakeEntitlements

    from vision.adapters.memory import MemoryStore
    from vision.main import build_app
    from vision.settings import Settings

    return build_app(
        Settings.model_validate(_COMMON),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=InMemoryAuthorizer(),
        entitlements=FakeEntitlements(),
        sweep=False,
        warm_litellm=False,
    )


def _assistant() -> FastAPI:
    from assistant.main import build_app
    from assistant.settings import Settings

    return build_app(Settings.model_validate(_COMMON))


SERVICES: Mapping[str, Callable[[], FastAPI]] = {
    "identity": _identity,
    "fleet": _fleet,
    "telemetry": _telemetry,
    "device-gateway": _device_gateway,
    "realtime": _realtime,
    "scheduler": _scheduler,
    "notifications": _notifications,
    "entitlements": _entitlements,
    "billing": _billing,
    "planner": _planner,
    "vision": _vision,
    "assistant": _assistant,
}
"""Service name (file name) to app factory."""


def render(document: Mapping[str, Any]) -> str:
    """Deterministic JSON: sorted keys, two space indent, trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def build_documents() -> dict[str, str]:
    """``{"<service>.json": rendered document}`` for every service."""
    return {f"{name}.json": render(factory().openapi()) for name, factory in SERVICES.items()}


def stale_files(out_dir: Path) -> list[str]:
    """Names of the documents in ``out_dir`` that are missing or differ from a fresh export."""
    stale: list[str] = []
    for name, text in build_documents().items():
        path = out_dir / name
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            stale.append(name)
    return stale


def main(argv: Sequence[str] | None = None) -> int:
    """``export_openapi.py [--check] [OUT_DIR]``."""
    args = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in args
    rest = [a for a in args if a != "--check"]
    out_dir = Path(rest[0]) if rest else OUT_DIR
    if check:
        stale = stale_files(out_dir)
        if stale:
            print(f"stale OpenAPI documents in {out_dir}: {', '.join(stale)}")
            print("regenerate with: uv run poe openapi")
            return 1
        print(f"OpenAPI documents in {out_dir} are up to date")
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in build_documents().items():
        (out_dir / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(SERVICES)} OpenAPI documents to {out_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
