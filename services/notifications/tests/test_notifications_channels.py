"""Channels: real pywebpush encryption and VAPID against a fake push service, ntfy, the
registry, VAPID key handling and the CLI."""

import base64
import json
import os
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any
from uuid import uuid4

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from mowdeck_contracts.v1 import NotificationKind, NotificationSeverity
from notifications_support import ManualClock, make_settings
from py_vapid import Vapid02

from notifications import cli
from notifications.adapters.channels import ChannelContext, ChannelRegistry, discover_factories
from notifications.adapters.channels.log import LogChannel, create_log_channel
from notifications.adapters.channels.ntfy import NtfyChannel, create_ntfy_channel
from notifications.adapters.channels.vapid import (
    b64url,
    generate_private_key,
    load_keys,
    load_or_create,
)
from notifications.adapters.channels.webpush import (
    WebPushChannel,
    create_webpush_channel,
    retry_after,
    topic_for,
)
from notifications.domain.models import Delivery, DeliveryOutcome, Message, TargetKind

SUBJECT = "mailto:ops@example.test"


def b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class Subscriber:
    """A browser: its key pair and auth secret."""

    def __init__(self, endpoint: str = "https://push.example.test/send/abc") -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.endpoint = endpoint

    def info(self) -> dict[str, Any]:
        public = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": b64url(public), "auth": b64url(self.auth)},
        }

    def decrypt(self, body: bytes) -> dict[str, Any]:
        plain = http_ece.decrypt(
            body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"
        )
        data: dict[str, Any] = json.loads(plain)
        return data


class FakePushService:
    """Validates a Web Push request like a push service would, then decrypts it."""

    def __init__(self, subscriber: Subscriber, vapid_public: str, status: int = 201) -> None:
        self.subscriber = subscriber
        self.vapid_public = vapid_public
        self.status = status
        self.headers: dict[str, str] = {}
        self.received: list[dict[str, Any]] = []
        self.extra_headers: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers["authorization"]
        scheme, _, params = auth.partition(" ")
        assert scheme == "vapid"
        parts = dict(p.split("=", 1) for p in params.split(","))
        assert parts["k"] == self.vapid_public
        assert Vapid02.verify(auth)
        header_b64, claims_b64, _ = parts["t"].split(".")
        claims = json.loads(b64d(claims_b64))
        assert json.loads(b64d(header_b64))["alg"] == "ES256"
        assert claims["aud"] == "https://push.example.test"
        assert claims["sub"] == SUBJECT
        assert request.headers["content-encoding"] == "aes128gcm"
        assert len(request.content) < 4096
        self.headers = dict(request.headers)
        self.received.append(self.subscriber.decrypt(request.content))
        return httpx.Response(self.status, headers=self.extra_headers)


def delivery(
    target: dict[str, Any] | None,
    *,
    severity: NotificationSeverity = NotificationSeverity.CRITICAL,
    body: str = "Fehler E1. Angehoben",
    url: str | None = "https://app.example.test/mowers/1",
) -> Delivery:
    mower_id = uuid4()
    return Delivery(
        id=uuid4(),
        user_id=uuid4(),
        account_id=uuid4(),
        channel="webpush",
        message=Message(
            kind=NotificationKind.MOWER_STUCK,
            severity=severity,
            title="Vorgarten braucht Hilfe",
            body=body,
            url=url,
            tag=f"mower_stuck:{mower_id}",
            mower_id=mower_id,
            notification_id=uuid4(),
        ),
        attempts=1,
        target_kind=TargetKind.PUSH_SUBSCRIPTION,
        target_id=uuid4(),
        target=target,
    )


def channel_for(service: FakePushService, private: str) -> WebPushChannel:
    transport = httpx.MockTransport(service.handler)
    return WebPushChannel(httpx.AsyncClient(transport=transport), load_keys(private), SUBJECT)


def push_setup(status: int = 201) -> tuple[Subscriber, FakePushService, WebPushChannel]:
    subscriber = Subscriber()
    private = generate_private_key()
    service = FakePushService(subscriber, load_keys(private).public_key, status=status)
    return subscriber, service, channel_for(service, private)


async def test_notifications_channels_webpush_encrypts_and_signs_for_the_push_service() -> None:
    subscriber, service, channel = push_setup()
    sent = delivery(subscriber.info())
    result = await channel.send(sent)
    assert result.outcome is DeliveryOutcome.SENT
    assert service.received[0] == {
        "title": "Vorgarten braucht Hilfe",
        "body": "Fehler E1. Angehoben",
        "url": "https://app.example.test/mowers/1",
        "tag": sent.message.tag,
        "kind": "mower_stuck",
        "mower_id": str(sent.message.mower_id),
        "notification_id": str(sent.message.notification_id),
    }
    assert service.headers["ttl"] == "86400" and service.headers["urgency"] == "high"
    assert service.headers["topic"] == topic_for(sent.message.tag)
    assert len(service.headers["topic"]) == 32


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (201, DeliveryOutcome.SENT),
        (404, DeliveryOutcome.GONE),
        (410, DeliveryOutcome.GONE),
        (429, DeliveryOutcome.RETRY),
        (503, DeliveryOutcome.RETRY),
        (400, DeliveryOutcome.FAILED),
        (413, DeliveryOutcome.FAILED),
    ],
)
async def test_notifications_channels_webpush_status_mapping(
    status: int, outcome: DeliveryOutcome
) -> None:
    subscriber, service, channel = push_setup(status)
    service.extra_headers = {"Retry-After": "120"} if status == 429 else {}
    result = await channel.send(delivery(subscriber.info(), severity=NotificationSeverity.INFO))
    assert result.outcome is outcome
    if status == 429:
        assert result.retry_after is not None and result.retry_after.total_seconds() == 120
    assert service.headers["urgency"] == "low" and service.headers["ttl"] == "3600"


async def test_notifications_channels_webpush_invalid_targets_and_network_errors() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    keys = load_keys(generate_private_key())
    channel = WebPushChannel(httpx.AsyncClient(transport=httpx.MockTransport(fail)), keys, SUBJECT)
    subscriber = Subscriber()
    network = await channel.send(delivery(subscriber.info(), severity=NotificationSeverity.WARNING))
    insecure = await channel.send(delivery({**subscriber.info(), "endpoint": "http://x.test/a"}))
    no_keys = await channel.send(delivery({"endpoint": "https://push.example.test/a", "keys": {}}))
    nothing = await channel.send(delivery(None))
    assert network.outcome is DeliveryOutcome.RETRY
    assert {insecure.outcome, no_keys.outcome, nothing.outcome} == {DeliveryOutcome.FAILED}


async def test_notifications_channels_webpush_shortens_a_long_body() -> None:
    subscriber, service, channel = push_setup()
    long = delivery(subscriber.info(), body="\U0001f331" * 1000)  # 4 bytes each in UTF-8
    assert len(channel.payload(long)) <= 3000
    assert (await channel.send(long)).outcome is DeliveryOutcome.SENT
    assert len(service.received[0]["body"]) == 200


def test_notifications_channels_retry_after_formats() -> None:
    def response(value: str | None) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": value} if value else {})

    in_seconds = retry_after(response("30"))
    past_date = retry_after(response("Wed, 21 Oct 2015 07:28:00 GMT"))
    assert retry_after(response(None)) is None
    assert in_seconds is not None and in_seconds.total_seconds() == 30
    assert past_date is not None and past_date.total_seconds() == 0
    assert retry_after(response("soon")) is None


async def test_notifications_channels_ntfy_publishes_json() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "x"})

    channel = NtfyChannel(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        frozenset({"https://ntfy.example.test"}),
    )
    target = {"server_url": "https://ntfy.example.test", "topic": "lawn", "token": "tk_abc"}
    result = await channel.send(delivery(target))
    relative = await channel.send(delivery({**target, "token": None}, url="/mowers/1"))
    assert result.outcome is DeliveryOutcome.SENT and relative.outcome is DeliveryOutcome.SENT
    first, second = seen
    assert str(first.url) == "https://ntfy.example.test/"
    assert first.headers["authorization"] == "Bearer tk_abc"
    assert json.loads(first.content) == {
        "topic": "lawn",
        "title": "Vorgarten braucht Hilfe",
        "message": "Fehler E1. Angehoben",
        "priority": 5,
        "tags": ["warning"],
        "click": "https://app.example.test/mowers/1",
    }
    assert "authorization" not in second.headers
    assert "click" not in json.loads(second.content)


async def test_notifications_channels_ntfy_errors() -> None:
    status = {"value": 404}

    def handler(request: httpx.Request) -> httpx.Response:
        if status["value"] == 0:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(status["value"])

    channel = NtfyChannel(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        frozenset({"https://ntfy.example.test"}),
    )
    target = {"server_url": "https://ntfy.example.test", "topic": "lawn"}
    assert (await channel.send(delivery(target))).outcome is DeliveryOutcome.FAILED  # 404
    status["value"] = 429
    assert (await channel.send(delivery(target))).outcome is DeliveryOutcome.RETRY
    status["value"] = 0
    assert (await channel.send(delivery(target))).outcome is DeliveryOutcome.RETRY
    other = {"server_url": "https://elsewhere.test", "topic": "lawn"}
    assert (await channel.send(delivery(other))).outcome is DeliveryOutcome.FAILED


async def test_notifications_channels_log_channel() -> None:
    channel = LogChannel()
    result = await channel.send(delivery(None))
    assert result.outcome is DeliveryOutcome.SENT and len(channel.sent) == 1


async def test_notifications_channels_registry_and_factories() -> None:
    async with httpx.AsyncClient() as http:
        private = generate_private_key()
        settings = make_settings(
            notifications_vapid_private_key=private, notifications_vapid_subject=SUBJECT
        )
        context = ChannelContext(settings, http, ManualClock(), load_keys(private))
        assert {"log", "webpush", "ntfy"} <= set(discover_factories())
        registry = ChannelRegistry.build(context, ["log", "webpush", "ntfy", "pigeon"])
        assert registry.names() == ["log", "ntfy", "webpush"]
        assert registry.get("pigeon") is None
        bare = ChannelContext(
            make_settings(notifications_ntfy_allowed_servers=""), http, ManualClock(), None
        )
        assert create_webpush_channel(bare) is None
        assert create_ntfy_channel(bare) is None
        assert isinstance(create_log_channel(bare), LogChannel)
        assert ChannelRegistry.build(bare, ["webpush", "ntfy"]).names() == []


def test_notifications_channels_broken_entry_points_are_skipped() -> None:
    entry_points = [
        EntryPoint("good", "notifications.adapters.channels.log:create_log_channel", "g"),
        EntryPoint("broken", "notifications.no_such_module:factory", "g"),
        EntryPoint("constant", "notifications.adapters.channels:ENTRY_POINT_GROUP", "g"),
    ]
    assert list(discover_factories(lambda: entry_points)) == ["good"]


def test_notifications_channels_vapid_keys(tmp_path: Path) -> None:
    private = generate_private_key()
    keys = load_keys(private)
    assert len(b64d(keys.public_key)) == 65
    assert load_keys(private, keys.public_key).public_key == keys.public_key
    with pytest.raises(ValueError, match="does not belong"):
        load_keys(private, load_keys(generate_private_key()).public_key)
    pem = keys.signer.private_pem().decode()
    assert load_keys(pem).public_key == keys.public_key
    path = tmp_path / "keys" / "vapid"
    created, was_created = load_or_create(path)
    loaded, again = load_or_create(path)
    assert was_created and not again and created.public_key == loaded.public_key


def test_notifications_channels_cli_generates_keys(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["generate-vapid-keys"]) == 0
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    private = lines["MOWDECK_NOTIFICATIONS_VAPID_PRIVATE_KEY"]
    public = lines["MOWDECK_NOTIFICATIONS_VAPID_PUBLIC_KEY"]
    assert load_keys(private, public).public_key == public
    with pytest.raises(SystemExit):
        cli.main([])
