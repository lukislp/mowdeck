"""APNs: the provider token, requests per severity, answer mapping, the factory and settings,
the device API, fan-out to devices and the outbox removing unregistered devices."""

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from mowdeck_contracts.v1 import NotificationKind, NotificationSeverity, PlanStatus
from mowdeck_service_kit.testing import FakeEntitlements
from notifications_support import (
    API,
    T0,
    Harness,
    ManualClock,
    Person,
    RecordingChannel,
    harness,
    make_settings,
    make_state,
)

from notifications.adapters.channels import ChannelContext, ChannelRegistry, discover_factories
from notifications.adapters.channels.apns import (
    MAX_PAYLOAD_BYTES,
    ApnsChannel,
    create_apns_channel,
    load_signing_key,
)
from notifications.adapters.channels.webpush import topic_for
from notifications.adapters.memory import MemoryStore
from notifications.application.context import NotificationsConfig
from notifications.application.outbox import Outbox
from notifications.domain.models import (
    ALL_CHANNELS_DEFAULT,
    ApnsDevice,
    Delivery,
    DeliveryOutcome,
    DeliveryResult,
    DeliveryStatus,
    InboxItem,
    Message,
    NewDelivery,
    TargetKind,
)
from notifications.settings import Settings

KEY_ID = "ABC123DEFG"
TEAM_ID = "TEAM456789"
BUNDLE = "app.mowdeck.mobile"
TOKEN = "a1" * 32  # 64 hex characters


def p8_key() -> tuple[str, ec.EllipticCurvePrivateKey]:
    """A fresh P-256 key as the PEM of an ``AuthKey_<KEY_ID>.p8`` file."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return pem, key


class FakeApns:
    """Records requests and answers with a scripted status and JSON body."""

    def __init__(
        self, status: int = 200, body: dict[str, Any] | None = None, headers: Any = None
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.body is None:
            return httpx.Response(self.status, headers=self.headers)
        return httpx.Response(self.status, json=self.body, headers=self.headers)


def channel_for(
    service: FakeApns, clock: ManualClock | None = None, environment: str = "development"
) -> tuple[ApnsChannel, ec.EllipticCurvePrivateKey]:
    pem, key = p8_key()
    channel = ApnsChannel(
        key=load_signing_key(pem),
        key_id=KEY_ID,
        team_id=TEAM_ID,
        bundle_id=BUNDLE,
        clock=clock or ManualClock(),
        environment="production" if environment == "production" else "development",
        http=httpx.AsyncClient(transport=httpx.MockTransport(service.handler)),
    )
    return channel, key


def delivery(
    token: str = TOKEN,
    *,
    severity: NotificationSeverity = NotificationSeverity.CRITICAL,
    body: str = "Vorgarten wurde angehoben",
    url: str | None = "https://app.example.test/mowers/1",
) -> Delivery:
    mower_id = uuid4()
    return Delivery(
        id=uuid4(),
        user_id=uuid4(),
        account_id=uuid4(),
        channel="apns",
        message=Message(
            kind=NotificationKind.MOWER_LIFTED,
            severity=severity,
            title="Alarm: Vorgarten",
            body=body,
            url=url,
            tag=f"mower_lifted:{mower_id}",
            mower_id=mower_id,
            notification_id=uuid4(),
        ),
        attempts=1,
        target_kind=TargetKind.APNS_DEVICE,
        target_id=uuid4(),
        target={"token": token},
    )


def claims(token: str, key: ec.EllipticCurvePrivateKey) -> dict[str, Any]:
    decoded: dict[str, Any] = jwt.decode(
        token, key.public_key(), algorithms=["ES256"], options={"verify_iat": False}
    )
    return decoded


# --- provider token -------------------------------------------------------------------------


def test_notifications_apns_provider_token_is_es256_and_cached() -> None:
    clock = ManualClock()
    channel, key = channel_for(FakeApns(), clock)
    first = channel.provider_token()
    header = jwt.get_unverified_header(first)
    assert header["alg"] == "ES256" and header["kid"] == KEY_ID
    assert claims(first, key) == {"iss": TEAM_ID, "iat": int(T0.timestamp())}
    clock.advance(49 * 60)
    assert channel.provider_token() == first  # reused (Apple throttles new tokens)
    clock.advance(60)
    second = channel.provider_token()
    assert second != first and claims(second, key)["iat"] == int(clock.now.timestamp())
    channel.forget_token()
    clock.advance(1)
    assert channel.provider_token() not in (first, second)


def test_notifications_apns_signing_key_must_be_p256_pem() -> None:
    with pytest.raises(ValueError, match="not a PEM private key"):
        load_signing_key("not a key")
    rsa_pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode("ascii")
    )
    with pytest.raises(ValueError, match="P-256"):
        load_signing_key(rsa_pem)
    p384 = (
        ec.generate_private_key(ec.SECP384R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode("ascii")
    )
    with pytest.raises(ValueError, match="P-256"):
        load_signing_key(p384)


# --- requests -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("severity", "priority", "level", "ttl"),
    [
        (NotificationSeverity.CRITICAL, "10", "time-sensitive", 24 * 3600),
        (NotificationSeverity.WARNING, "10", "active", 6 * 3600),
        (NotificationSeverity.INFO, "10", "active", 3600),
    ],
)
async def test_notifications_apns_request_per_severity(
    severity: NotificationSeverity, priority: str, level: str, ttl: int
) -> None:
    service = FakeApns()
    channel, key = channel_for(service)
    sent = delivery(TOKEN.upper(), severity=severity)
    result = await channel.send(sent)
    assert result.outcome is DeliveryOutcome.SENT
    (request,) = service.requests
    assert request.method == "POST"
    assert str(request.url) == f"https://api.sandbox.push.apple.com/3/device/{TOKEN}"
    headers = request.headers
    scheme, _, token = headers["authorization"].partition(" ")
    assert scheme == "bearer" and claims(token, key)["iss"] == TEAM_ID
    assert headers["apns-topic"] == BUNDLE
    assert headers["apns-push-type"] == "alert"
    assert headers["apns-priority"] == priority
    assert headers["apns-expiration"] == str(int(T0.timestamp()) + ttl)
    assert headers["apns-collapse-id"] == topic_for(sent.message.tag)
    assert len(headers["apns-collapse-id"].encode()) <= 64
    message = sent.message
    assert json.loads(request.content) == {
        "aps": {
            "alert": {"title": message.title, "body": message.body},
            "sound": "default",
            "thread-id": message.tag,
            "interruption-level": level,
        },
        "url": message.url,
        "kind": "mower_lifted",
        "mower_id": str(message.mower_id),
        "notification_id": str(message.notification_id),
    }


async def test_notifications_apns_payload_omits_none_and_shortens_a_long_body() -> None:
    service = FakeApns()
    channel, _ = channel_for(service)
    await channel.send(delivery(body="ä" * 3000, url=None))
    payload = json.loads(service.requests[0].content)
    assert len(service.requests[0].content) <= MAX_PAYLOAD_BYTES
    assert payload["aps"]["alert"]["body"] == "ä" * 200
    assert "url" not in payload


async def test_notifications_apns_production_host() -> None:
    service = FakeApns()
    channel, _ = channel_for(service, environment="production")
    await channel.send(delivery())
    assert service.requests[0].url.host == "api.push.apple.com"


# --- answers --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason", "outcome"),
    [
        (200, None, DeliveryOutcome.SENT),
        (410, "Unregistered", DeliveryOutcome.GONE),
        (400, "BadDeviceToken", DeliveryOutcome.GONE),
        (400, "DeviceTokenNotForTopic", DeliveryOutcome.GONE),
        (400, "BadCollapseId", DeliveryOutcome.FAILED),
        (403, "InvalidProviderToken", DeliveryOutcome.FAILED),
        (413, "PayloadTooLarge", DeliveryOutcome.FAILED),
        (429, "TooManyRequests", DeliveryOutcome.RETRY),
        (500, "InternalServerError", DeliveryOutcome.RETRY),
        (503, "ServiceUnavailable", DeliveryOutcome.RETRY),
        (502, None, DeliveryOutcome.RETRY),
    ],
)
async def test_notifications_apns_answer_mapping(
    status: int, reason: str | None, outcome: DeliveryOutcome
) -> None:
    channel, _ = channel_for(FakeApns(status, {"reason": reason} if reason else None))
    result = await channel.send(delivery())
    assert result.outcome is outcome
    if status != 200:
        assert result.error is not None and result.error.startswith(f"apns answered {status}")
        if reason:
            assert reason in result.error


async def test_notifications_apns_expired_provider_token_is_renewed() -> None:
    clock = ManualClock()
    service = FakeApns(403, {"reason": "ExpiredProviderToken"})
    channel, _ = channel_for(service, clock)
    first = await channel.send(delivery())
    assert first.outcome is DeliveryOutcome.RETRY and "ExpiredProviderToken" in (first.error or "")
    clock.advance(30)
    service.status, service.body = 200, None
    assert (await channel.send(delivery())).outcome is DeliveryOutcome.SENT
    tokens = [r.headers["authorization"] for r in service.requests]
    assert tokens[0] != tokens[1]  # a fresh token, not the cached one


async def test_notifications_apns_retry_after_and_non_json_bodies() -> None:
    channel, _ = channel_for(FakeApns(429, {"reason": "TooManyRequests"}, {"Retry-After": "120"}))
    result = await channel.send(delivery())
    assert result.retry_after is not None and result.retry_after.total_seconds() == 120

    def plain(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b"<html>bad</html>")

    channel, _ = channel_for(FakeApns())
    channel._http = httpx.AsyncClient(transport=httpx.MockTransport(plain))
    failed = await channel.send(delivery())
    assert failed.outcome is DeliveryOutcome.FAILED and failed.error == "apns answered 400"


async def test_notifications_apns_invalid_tokens_and_network_errors() -> None:
    service = FakeApns()
    channel, _ = channel_for(service)
    for token in ("zz" * 32, "ab" * 10, ""):
        result = await channel.send(delivery(token))
        assert result.outcome is DeliveryOutcome.FAILED
    missing = replace(delivery(), target=None)
    assert (await channel.send(missing)).outcome is DeliveryOutcome.FAILED
    assert service.requests == []

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    channel._http = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    result = await channel.send(delivery())
    assert result.outcome is DeliveryOutcome.RETRY and "apns request failed" in (result.error or "")


# --- factory and settings -------------------------------------------------------------------


async def test_notifications_apns_factory_needs_key_key_id_and_team() -> None:
    pem, _ = p8_key()
    complete = {"apns_key": pem, "apns_key_id": KEY_ID, "apns_team_id": TEAM_ID}
    async with httpx.AsyncClient() as http:
        for missing in ("apns_key", "apns_key_id", "apns_team_id"):
            settings = make_settings(**{k: v for k, v in complete.items() if k != missing})
            context = ChannelContext(settings, http, ManualClock(), None)
            assert create_apns_channel(context) is None
        sandbox = create_apns_channel(
            ChannelContext(make_settings(**complete), http, ManualClock(), None)
        )
        production = create_apns_channel(
            ChannelContext(
                make_settings(**complete, apns_environment="production"), http, ManualClock(), None
            )
        )
        broken = ChannelContext(
            make_settings(**{**complete, "apns_key": "garbage"}), http, ManualClock(), None
        )
        with pytest.raises(ValueError, match="AuthKey"):
            create_apns_channel(broken)
    assert "apns" in discover_factories()
    assert sandbox is not None and sandbox.host == "api.sandbox.push.apple.com"
    assert production is not None and production.host == "api.push.apple.com"
    assert sandbox._http is not http  # its own HTTP/2 client
    registry = ChannelRegistry({"apns": sandbox, "log": RecordingChannel()})
    await registry.aclose()
    assert sandbox._http.is_closed
    await production.aclose()


async def test_notifications_apns_unconfigured_channel_is_a_calm_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with httpx.AsyncClient() as http:
        context = ChannelContext(make_settings(), http, ManualClock(), None)
        with caplog.at_level(logging.INFO, logger="notifications.channels"):
            registry = ChannelRegistry.build(context, ["apns"])
    assert registry.names() == []
    (record,) = [r for r in caplog.records if "apns" in r.getMessage()]
    assert record.levelno == logging.INFO and "not configured" in record.getMessage()


def test_notifications_apns_settings_from_a_secret_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem, key = p8_key()
    path = tmp_path / f"AuthKey_{KEY_ID}.p8"
    path.write_text(pem + "\n", encoding="utf-8")
    monkeypatch.setenv("MOWDECK_APNS_KEY_FILE", str(path))
    monkeypatch.setenv("MOWDECK_APNS_KEY_ID", KEY_ID)
    monkeypatch.setenv("MOWDECK_APNS_TEAM_ID", TEAM_ID)
    monkeypatch.setenv("MOWDECK_APNS_ENVIRONMENT", "production")
    settings = Settings.from_env()
    assert settings.apns_key is not None
    loaded = load_signing_key(settings.apns_key.get_secret_value())
    assert loaded.private_numbers() == key.private_numbers()
    assert (settings.apns_key_id, settings.apns_team_id) == (KEY_ID, TEAM_ID)
    assert settings.apns_environment == "production"
    assert settings.apns_bundle_id == "app.mowdeck.mobile"
    assert "apns" in settings.channel_names


def test_notifications_apns_settings_empty_means_unset_and_ids_are_checked() -> None:
    settings = make_settings(apns_key="", apns_key_id=" ", apns_team_id="")
    assert (settings.apns_key, settings.apns_key_id, settings.apns_team_id) == (None, None, None)
    assert settings.apns_environment == "development"
    with pytest.raises(ValueError, match="apns_key_id"):
        make_settings(apns_key_id="short")
    with pytest.raises(ValueError, match="apns_environment"):
        make_settings(apns_environment="staging")


# --- device API -----------------------------------------------------------------------------


async def register(
    h: Harness, person: Person, token: str = TOKEN, name: str | None = "iPhone"
) -> httpx.Response:
    return await h.client.post(
        f"{API}/me/push/apns",
        json={"token": token, "device_name": name},
        headers=person.headers,
    )


async def test_notifications_apns_api_register_list_delete() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.add_mower(owner)
        other = await h.owner()
        await h.add_mower(other)
        created = await register(h, owner, TOKEN.upper())
        h.clock.advance(5)
        second = await register(h, owner, "b2" * 40, None)
        listed = (await h.client.get(f"{API}/me/push/apns", headers=owner.headers)).json()
        foreign = (await h.client.get(f"{API}/me/push/apns", headers=other.headers)).json()
        device_id = created.json()["id"]
        foreign_delete = await h.client.delete(
            f"{API}/me/push/apns/{device_id}", headers=other.headers
        )
        deleted = await h.client.delete(f"{API}/me/push/apns/{device_id}", headers=owner.headers)
        again = await h.client.delete(f"{API}/me/push/apns/{device_id}", headers=owner.headers)
        unknown = await h.client.delete(f"{API}/me/push/apns/{uuid4()}", headers=owner.headers)
        left = (await h.client.get(f"{API}/me/push/apns", headers=owner.headers)).json()
        anonymous = await h.client.get(f"{API}/me/push/apns")
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {"id", "device_name", "token_suffix", "created_at"}
    assert body["token_suffix"] == TOKEN[-8:] and body["device_name"] == "iPhone"
    assert second.status_code == 201 and second.json()["device_name"] is None
    assert [d["id"] for d in listed["items"]] == [device_id, second.json()["id"]]  # oldest first
    assert foreign == {"items": []}
    assert foreign_delete.status_code == 404
    assert (deleted.status_code, again.status_code, unknown.status_code) == (204, 404, 404)
    assert [d["id"] for d in left["items"]] == [second.json()["id"]]
    assert anonymous.status_code == 401
    stored = list(h.store.data.apns.values())
    assert {d.token for d in stored} == {"b2" * 40}  # lower case


async def test_notifications_apns_api_same_token_moves_to_the_new_owner() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.add_mower(owner)
        other = await h.owner()
        await h.add_mower(other)
        first = (await register(h, owner, TOKEN, "Old")).json()
        moved = (await register(h, other, TOKEN.upper(), "New")).json()
        owner_list = (await h.client.get(f"{API}/me/push/apns", headers=owner.headers)).json()
        other_list = (await h.client.get(f"{API}/me/push/apns", headers=other.headers)).json()
    assert moved["id"] == first["id"] and moved["device_name"] == "New"
    assert owner_list == {"items": []}
    assert [d["id"] for d in other_list["items"]] == [first["id"]]
    (device,) = h.store.data.apns.values()
    assert device.user_id == other.user_id and device.account_id == other.account_id


async def test_notifications_apns_api_validation() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.add_mower(owner)
        answers = [
            (await register(h, owner, token, name)).status_code
            for token, name in (
                ("a" * 63, None),  # too short
                ("a" * 201, None),  # too long
                ("g" * 64, None),  # not hex
                (" " + "a" * 64, None),
                (TOKEN, "x" * 101),  # name too long
            )
        ]
        missing = await h.client.post(f"{API}/me/push/apns", json={}, headers=owner.headers)
    assert answers == [422] * 5
    assert missing.status_code == 422
    assert h.store.data.apns == {}


async def test_notifications_apns_api_needs_the_push_feature() -> None:
    plans = FakeEntitlements(plan="crafted", features=["view"])
    async with harness(entitlements=plans) as h:
        owner = await h.owner()
        await h.add_mower(owner)
        refused = await register(h, owner)
        listed = await h.client.get(f"{API}/me/push/apns", headers=owner.headers)
        plans.features.append("notifications")
        allowed = await register(h, owner)
        plans.status = PlanStatus.EXPIRED
        again = await register(h, owner)
        delete = await h.client.delete(
            f"{API}/me/push/apns/{allowed.json()['id']}", headers=owner.headers
        )
    assert refused.status_code == 403
    assert refused.json()["type"] == "urn:mowdeck:problem:feature-not-in-plan"
    assert listed.status_code == 200  # reading and deleting stay possible
    assert allowed.status_code == 201, allowed.text
    assert again.status_code == 403
    assert delete.status_code == 204
    assert ("feature", "notifications.push") in plans.checks


async def test_notifications_apns_api_preferences_default_and_known_channel() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.add_mower(owner)
        defaults = (await h.client.get(f"{API}/preferences", headers=owner.headers)).json()
        saved = await h.client.put(
            f"{API}/preferences", json={"enabled_kinds": ["battery_low"]}, headers=owner.headers
        )
        explicit = await h.client.put(
            f"{API}/preferences",
            json={"enabled_kinds": ["battery_low"], "channels": ["apns"]},
            headers=owner.headers,
        )
    assert "apns" in defaults["channels"] and "apns" in ALL_CHANNELS_DEFAULT
    assert saved.status_code == 200 and "apns" in saved.json()["channels"]
    assert explicit.status_code == 200 and explicit.json()["channels"] == ["apns"]


# --- fan-out and outbox ---------------------------------------------------------------------


async def low_battery(h: Harness, owner: Person, mower_id: UUID) -> None:
    for battery in (50, 10):
        h.clock.advance(10)
        await h.publish_state(
            make_state(mower_id, owner.account_id, at=h.clock.now, battery=battery)
        )


async def test_notifications_apns_fanout_queues_one_delivery_per_device() -> None:
    apns = RecordingChannel("apns")
    async with harness(channels={"apns": apns}) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner, enabled_kinds=["battery_low"], channels=["apns"])
        ids = [(await register(h, owner, t)).json()["id"] for t in (TOKEN, "c3" * 32)]
        other = await h.owner()
        await h.add_mower(other)
        await register(h, other, "d4" * 32)  # another account, does not follow the mower
        await low_battery(h, owner, mower_id)
        await h.services.outbox.run_once()
    assert sorted(str(d.target_id) for d in apns.sent) == sorted(ids)
    assert {d.target["token"] for d in apns.sent if d.target} == {TOKEN, "c3" * 32}
    assert {d.target_kind for d in apns.sent} == {TargetKind.APNS_DEVICE}
    assert all(d.user_id == owner.user_id for d in apns.sent)


async def test_notifications_apns_fanout_respects_quiet_hours_and_the_channel_choice() -> None:
    apns, log = RecordingChannel("apns"), RecordingChannel()
    async with harness(channels={"apns": apns, "log": log}) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await register(h, owner)
        # T0 is 12:00 UTC = 14:00 in Berlin
        await h.subscribe_all(
            owner,
            enabled_kinds=["battery_low"],
            channels=["apns"],
            timezone="Europe/Berlin",
            quiet_hours={"start": "13:30", "end": "15:00"},
        )
        await low_battery(h, owner, mower_id)
        quiet = await h.services.outbox.run_once()
        h.clock.advance(3600)
        await h.subscribe_all(owner, enabled_kinds=["battery_low"], channels=["log"])
        await h.publish_state(
            make_state(mower_id, owner.account_id, at=h.clock.now, battery=90)
        )  # re-arm
        await low_battery(h, owner, mower_id)
        await h.services.outbox.run_once()
    assert quiet == 0
    assert apns.sent == []  # quiet hours, then the user deselected apns
    assert [d.message.kind for d in log.sent] == [NotificationKind.BATTERY_LOW]


def outbox_message() -> Message:
    return Message(
        kind=NotificationKind.TEST,
        severity=NotificationSeverity.INFO,
        title="t",
        body="b",
        url=None,
        tag="test",
        mower_id=None,
    )


async def enqueue(store: MemoryStore, user: UUID, account: UUID, device_id: UUID) -> UUID:
    item = InboxItem(
        id=uuid4(),
        user_id=user,
        account_id=account,
        mower_id=None,
        kind=NotificationKind.TEST,
        severity=NotificationSeverity.INFO,
        title="t",
        body="b",
        url=None,
        dedupe_key=str(uuid4()),
        occurred_at=T0,
        created_at=T0,
    )
    before = set(store.data.deliveries)
    new = NewDelivery(user, account, "apns", outbox_message(), TargetKind.APNS_DEVICE, device_id)
    assert await store.record(item, [new], 200)
    (created,) = set(store.data.deliveries) - before
    return created


async def test_notifications_apns_outbox_gone_removes_the_device() -> None:
    channel = RecordingChannel(
        "apns", results=[DeliveryResult(DeliveryOutcome.GONE, "apns answered 410: Unregistered")]
    )
    store, clock = MemoryStore(), ManualClock()
    outbox = Outbox(store, ChannelRegistry({"apns": channel}), NotificationsConfig(), clock)
    user, account, intruder = uuid4(), uuid4(), uuid4()
    device = await store.add_apns_device(ApnsDevice(uuid4(), user, account, TOKEN, "iPhone", T0))
    gone = await enqueue(store, user, account, device.id)
    foreign = await enqueue(store, intruder, account, device.id)  # not the intruder's device
    await outbox.run_once()
    assert store.data.deliveries[gone].status is DeliveryStatus.DROPPED
    assert store.data.deliveries[foreign].status is DeliveryStatus.DROPPED
    assert await store.apns_devices(user, account) == []
    assert [d.target for d in channel.sent] == [{"token": TOKEN}]
