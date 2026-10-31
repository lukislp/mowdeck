"""Wiring of realtime: health, readiness, descriptor, settings, main(), adapters."""

from typing import Any
from uuid import uuid4

import pytest
from mowdeck_authz import RelationshipTuple, account, mower, user
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import MODULE_DESCRIPTOR_PATH, Principal, PrincipalKind
from mowdeck_contracts.v1.subjects import command_result_subject, telemetry_subject
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client
from realtime_support import make_settings, make_state

import realtime.main as realtime_main
from realtime.adapters.auth import JwtTokenVerifier, RejectAllTokens, verifier_from_env
from realtime.adapters.authz import AuthzAccessChecker
from realtime.adapters.metrics import PrometheusMetrics
from realtime.adapters.server import RealtimeServer
from realtime.domain.errors import InvalidTokenError
from realtime.main import build_app, uvicorn_options
from realtime.module import DESCRIPTOR, VIEW_PERMISSION
from realtime.settings import Settings


def test_realtime_settings_defaults_and_config() -> None:
    settings = Settings.model_validate({"environment": "test"})
    assert settings.service_name == "realtime"
    assert settings.realtime_auth_timeout_seconds == 5
    assert settings.realtime_recheck_seconds == 60
    assert settings.realtime_max_connections_per_account == 50
    assert settings.realtime_max_connections_per_user == 10
    assert settings.realtime_max_subscriptions == 200
    assert settings.realtime_queue_size == 256
    assert settings.realtime_ping_seconds == 20
    assert settings.allowed_origins == frozenset()
    assert settings.realtime_max_connections_per_address == 0
    config = settings.to_config()
    assert (config.queue_size, config.max_subscriptions) == (256, 200)
    assert config.limit_per_login_session is False
    demo = make_settings(demo_mode=True, realtime_max_connections_per_address=20).to_config()
    assert (demo.limit_per_login_session, demo.max_connections_per_address) == (True, 20)
    origins = make_settings(realtime_allowed_origins=" https://a , https://b ,").allowed_origins
    assert origins == {"https://a", "https://b"}


def test_realtime_uvicorn_options_use_protocol_pings() -> None:
    assert uvicorn_options(make_settings()) == {
        "ws_ping_interval": 20,
        "ws_ping_timeout": 20,
        "ws_max_size": 16384,
    }
    assert uvicorn_options(make_settings(realtime_ping_seconds=0))["ws_ping_interval"] is None


def test_realtime_main_serves_with_the_realtime_server(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(app: Any, settings: Settings, **kwargs: Any) -> None:
        captured.update(kwargs, app=app, settings=settings)

    monkeypatch.setattr(realtime_main, "run", fake_run)
    monkeypatch.setenv("MOWDECK_NATS_ENABLED", "false")
    monkeypatch.setenv("MOWDECK_OPENFGA_API_URL", "http://openfga.test:8080")
    monkeypatch.setenv("MOWDECK_REALTIME_PING_SECONDS", "15")
    monkeypatch.delenv("MOWDECK_AUTH_JWKS_URL", raising=False)
    realtime_main.main()
    assert captured["server_class"] is RealtimeServer
    assert captured["ws_ping_interval"] == 15
    assert isinstance(captured["app"].state.hub.verifier, RejectAllTokens)


async def test_realtime_health_readiness_and_descriptor() -> None:
    app = build_app(make_settings(), bus=InMemoryBus(), authorizer=InMemoryAuthorizer())
    async with lifespan_client(app) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        ready = await client.get("/readyz")
        assert ready.status_code == 200, ready.text
        descriptor = (await client.get(MODULE_DESCRIPTOR_PATH)).json()
        metrics = (await client.get("/metrics")).text
    assert descriptor == DESCRIPTOR.model_dump(mode="json")
    assert VIEW_PERMISSION in {p["key"] for p in descriptor["permissions"]}
    assert descriptor["permissions"][0]["implied_by"] == ["mower.view"]
    assert "mowdeck_realtime_connections" in metrics


async def test_realtime_not_ready_without_a_bus() -> None:
    app = build_app(make_settings(), authorizer=InMemoryAuthorizer())  # nats disabled
    async with lifespan_client(app) as client:
        assert (await client.get("/readyz")).status_code == 503


async def test_realtime_one_subscription_per_family_without_queue_group() -> None:
    bus = InMemoryBus()
    app = build_app(make_settings(), bus=bus, authorizer=InMemoryAuthorizer())
    async with lifespan_client(app):
        subscriptions = app.state.realtime_subscriptions
        assert subscriptions is not None
        assert len(subscriptions.all()) == 6
        # Malformed or mismatching messages are dropped, never redelivered.
        mower_id = uuid4()
        await bus.publish(telemetry_subject(mower_id), b"{}")
        await bus.publish(telemetry_subject(mower_id), make_state(uuid4(), uuid4()))
        await bus.publish(command_result_subject(mower_id), b"[]")
        await bus.publish("event.identity.grants.changed", b"{}")
        await bus.publish(f"device.mower.{mower_id}.capabilities", b"{}")
        await bus.publish("event.notifications.sent", b"{}")
        await bus.publish("event.vision.proposal.created", b"[]")
        assert len(bus.dropped) == 7
    assert app.state.hub.accepting is False


async def test_realtime_jwt_verifier_adapter() -> None:
    from mowdeck_auth.testing import TestKeys

    keys = TestKeys()
    principal = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)
    verifier = JwtTokenVerifier(keys.verifier())
    verified = await verifier.verify(keys.mint(principal))
    assert verified.principal.subject == principal.subject
    with pytest.raises(InvalidTokenError):
        await verifier.verify("garbage")
    with pytest.raises(InvalidTokenError):
        await RejectAllTokens().verify("x")
    assert verifier_from_env({}) is None
    assert verifier_from_env({"MOWDECK_AUTH_JWKS_URL": "http://identity:8000/jwks"}) is not None


async def test_realtime_access_checker_adapter() -> None:
    owner = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)
    assert owner.account_id is not None
    own, foreign = uuid4(), uuid4()
    authz = InMemoryAuthorizer(
        [
            RelationshipTuple(user(owner.subject), "owner", account(owner.account_id)),
            RelationshipTuple(account(owner.account_id).as_subject(), "account", mower(own)),
            RelationshipTuple(account(uuid4()).as_subject(), "account", mower(foreign)),
        ]
    )
    checker = AuthzAccessChecker(authz)
    assert await checker.can_view(owner, [own, foreign]) == [True, False]
    assert await checker.can_view(owner, []) == []
    service = Principal(subject=uuid4(), kind=PrincipalKind.SERVICE)
    assert await checker.can_view(service, [own]) == [False]
    assert await checker.can_drive(owner, own) is True
    assert await checker.can_drive(owner, foreign) is False
    assert await checker.can_drive(service, own) is False
    assert checker.subject_key(owner) == f"user:{owner.subject}"
    assert checker.subject_key(service) == ""
    assert await checker.available() is True
    assert await AuthzAccessChecker(object()).available() is True  # type: ignore[arg-type]


def test_realtime_prometheus_metrics() -> None:
    from prometheus_client import CollectorRegistry, generate_latest

    registry = CollectorRegistry()
    metrics = PrometheusMetrics(registry)
    metrics.connections(2)
    metrics.subscriptions(3)
    metrics.sent("state")
    metrics.dropped("coalesced")
    metrics.closed(4430)
    metrics.closed(None)
    metrics.drive_sessions(1)
    metrics.drive_frame("rate_limited")
    text = generate_latest(registry).decode()
    assert "mowdeck_realtime_drive_sessions 1.0" in text
    assert 'mowdeck_realtime_drive_frames_total{outcome="rate_limited"} 1.0' in text
    assert 'mowdeck_realtime_messages_sent_total{type="state"} 1.0' in text
    assert 'mowdeck_realtime_messages_dropped_total{reason="coalesced"} 1.0' in text
    assert 'mowdeck_realtime_closed_total{code="none"} 1.0' in text
    assert "mowdeck_realtime_subscriptions 3.0" in text
    PrometheusMetrics(None).sent("pong")  # without a registry nothing is exported
