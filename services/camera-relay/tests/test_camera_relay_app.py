"""The service skeleton: settings, health, and the NATS responders with an in-memory bus."""

import base64
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mowdeck_contracts.v1.relay import (
    RelayAttachReply,
    RelayDetachReply,
    RelayDetachRequest,
    RelayErrorCode,
    RelayRenewReply,
    RelayRenewRequest,
)
from mowdeck_contracts.v1.subjects import (
    ALL_CAMERA_RELAY_EVENTS,
    RELAY_CAMERA_ATTACH,
    RELAY_CAMERA_DETACH,
    RELAY_CAMERA_RENEW,
)
from mowdeck_service_kit.bus import InMemoryBus, RequestTimeoutError
from mowdeck_service_kit.testing import lifespan_client
from relay_support import FakeClock, FakeVendor, attach_request, key, principal

from camera_relay.adapters.sources import NO_DRIVER_DETAIL
from camera_relay.adapters.tickets import Ed25519TicketSigner
from camera_relay.application.service import RelayService
from camera_relay.main import build_app
from camera_relay.module import DESCRIPTOR
from camera_relay.settings import Settings


def _settings(**overrides: object) -> Settings:
    return Settings.model_validate({"environment": "test", "log_format": "console", **overrides})


def test_settings_defaults() -> None:
    settings = _settings()
    assert settings.service_name == "camera-relay"
    assert settings.http_port == 8000
    assert settings.relay_whep_base_url == "/media/whep"
    assert settings.relay_mediamtx_api_url is None
    assert not settings.media_enabled
    assert settings.relay_test_hooks is False
    assert settings.relay_ticket_signing_key is None
    assert settings.to_policy().max_leases_per_session == 32


def test_the_descriptor_lists_the_relay_events() -> None:
    assert [e.type for e in DESCRIPTOR.events] == [
        "camera.relay.started",
        "camera.relay.stopped",
        "camera.relay.lease_opened",
        "camera.relay.lease_closed",
    ]
    assert DESCRIPTOR.permissions == []


def test_a_configured_signing_key_is_used() -> None:
    key_bytes = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    app = build_app(
        _settings(relay_ticket_signing_key=base64.b64encode(key_bytes).decode()),
        bus=InMemoryBus(),
    )
    signer = app.state.relay.signer
    assert isinstance(signer, Ed25519TicketSigner)
    assert signer.public_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == Ed25519PrivateKey.from_private_bytes(key_bytes).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def test_an_invalid_signing_key_fails_at_startup() -> None:
    with pytest.raises(ValueError, match="key"):
        build_app(_settings(relay_ticket_signing_key="c2hvcnQ="), bus=InMemoryBus())


async def test_startup_and_shutdown_with_in_memory_bus() -> None:
    bus = InMemoryBus()
    app = build_app(_settings(), bus=bus, background_tasks=False)
    async with lifespan_client(app) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 200
        assert isinstance(app.state.relay, RelayService)
    assert app.state.draining is True
    assert not bus.is_connected


async def test_without_a_source_driver_attach_is_unavailable() -> None:
    bus = InMemoryBus()
    app = build_app(_settings(), bus=bus, background_tasks=False)
    async with lifespan_client(app):
        message = await bus.request(RELAY_CAMERA_ATTACH, attach_request(key()))
        reply = message.model(RelayAttachReply)
        assert reply.error is not None
        assert reply.error.code is RelayErrorCode.UNAVAILABLE
        assert reply.error.detail == NO_DRIVER_DETAIL
        kinds = [m.subject.rsplit(".", 1)[-1] for m in bus.messages(ALL_CAMERA_RELAY_EVENTS)]
        assert kinds == ["lease_opened", "lease_closed", "stopped"]


async def test_responders_attach_renew_and_detach_over_the_bus() -> None:
    bus = InMemoryBus()
    clock = FakeClock()
    app = build_app(_settings(), bus=bus, clock=clock, vendor=FakeVendor(), background_tasks=False)
    relay_key = key()
    async with lifespan_client(app):
        attached = (await bus.request(RELAY_CAMERA_ATTACH, attach_request(relay_key))).model(
            RelayAttachReply
        )
        assert attached.session is not None and attached.session.relay is not None
        lease_id = attached.session.relay.lease_id
        renew = RelayRenewRequest(
            account_id=relay_key.account_id,
            mower_id=relay_key.mower_id,
            lease_id=lease_id,
            requested_by=principal(),
        )
        renewed = (await bus.request(RELAY_CAMERA_RENEW, renew)).model(RelayRenewReply)
        assert renewed.session is not None
        foreign = renew.model_copy(update={"account_id": uuid4()})
        refused = (await bus.request(RELAY_CAMERA_RENEW, foreign)).model(RelayRenewReply)
        assert refused.error is not None and refused.error.code is RelayErrorCode.FORBIDDEN
        detach = RelayDetachRequest(**renew.model_dump())
        detached = (await bus.request(RELAY_CAMERA_DETACH, detach)).model(RelayDetachReply)
        assert detached.detached is True
        again = (await bus.request(RELAY_CAMERA_DETACH, detach)).model(RelayDetachReply)
        assert (again.detached, again.error) == (False, None)


async def test_an_invalid_request_gets_no_reply() -> None:
    bus = InMemoryBus()
    app = build_app(_settings(), bus=bus, background_tasks=False)
    async with lifespan_client(app):
        with pytest.raises(RequestTimeoutError):
            await bus.request(RELAY_CAMERA_ATTACH, b'{"account_id": "nope"}', timeout=0.2)
