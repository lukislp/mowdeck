"""A neutral cloud provider for Fleet's tests (ADR 0066): Fleet knows no vendor, so its tests
link accounts of ``acme`` (e-mail and password) and ``zeta`` (application key and secret plus a
region), as a fake Device-Gateway lists them on ``gateway.cloud.providers``."""

from typing import Any

from mowdeck_contracts.v1 import CloudDiscoverRequest, CloudProvidersReply, ProviderDescriptor
from mowdeck_contracts.v1.subjects import GATEWAY_CLOUD_PROVIDERS
from mowdeck_service_kit.bus import BusMessage, InMemoryBus

PROVIDER = "acme"

ACME = ProviderDescriptor.model_validate(
    {
        "provider": PROVIDER,
        "name": "Acme Cloud",
        "fields": [
            {"key": "email", "kind": "email", "label": "E-mail", "identity": True},
            {"key": "password", "kind": "secret", "label": "Password"},
        ],
        "tested_on_hardware": True,
    }
)
ZETA = ProviderDescriptor.model_validate(
    {
        "provider": "zeta",
        "name": "Zeta Connect",
        "fields": [
            {"key": "app_key", "kind": "text", "label": "Application key", "identity": True},
            {"key": "app_secret", "kind": "secret", "label": "Application secret"},
            {
                "key": "region",
                "kind": "choice",
                "label": "Region",
                "choices": [{"value": "eu", "label": "Europe"}, {"value": "us", "label": "US"}],
            },
        ],
        "needs_developer_account": True,
        "links": [{"label": "Developer portal", "url": "https://developer.zeta.test"}],
    }
)


def link_body(email: str, password: str, provider: str = PROVIDER) -> dict[str, Any]:
    """The body of ``POST /linked-accounts`` for an e-mail and password provider."""
    return {"provider": provider, "credentials": {"email": email, "password": password}}


def login_of(request: CloudDiscoverRequest) -> tuple[str, str]:
    """The e-mail and password of an ``acme`` discovery request."""
    return request.credentials.get("email", ""), request.credentials.get("password", "")


class FakeProviders:
    """Answers ``gateway.cloud.providers`` like the Device-Gateway."""

    def __init__(self, *descriptors: ProviderDescriptor) -> None:
        self.descriptors = list(descriptors or (ACME,))
        self.requests = 0

    async def handle(self, message: BusMessage) -> None:
        self.requests += 1
        await message.respond(CloudProvidersReply(providers=self.descriptors))


async def serve_providers(bus: InMemoryBus, *descriptors: ProviderDescriptor) -> FakeProviders:
    """Subscribe a :class:`FakeProviders` (default: ``acme`` only)."""
    providers = FakeProviders(*descriptors)
    await bus.subscribe(GATEWAY_CLOUD_PROVIDERS, providers.handle, queue="device-gateway")
    return providers
