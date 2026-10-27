"""APNs requests for Live Activities and widget reloads (ADR 0059): topic, push type, priority,
size limit and the answer mapping of the shared client."""

import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from notifications_support import ManualClock

from notifications.adapters.channels.apns import ApnsChannel, load_signing_key
from notifications.domain.models import DeliveryOutcome

TOKEN = "a1" * 32


def make(handler: httpx.MockTransport) -> ApnsChannel:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return ApnsChannel(
        key=load_signing_key(pem),
        key_id="ABC123DEFG",
        team_id="TEAM456789",
        bundle_id="app.mowdeck.mobile",
        clock=ManualClock(),
        environment="production",
        http=httpx.AsyncClient(transport=handler),
    )


async def test_live_activity_push_uses_the_liveactivity_topic_and_type() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    channel = make(httpx.MockTransport(handle))
    payload = {"aps": {"event": "update", "timestamp": 1, "content-state": {"state": "mowing"}}}
    result = await channel.push(TOKEN.upper(), payload, push_type="liveactivity", priority=5)
    request = seen[0]
    assert result.outcome is DeliveryOutcome.SENT
    assert request.url.host == "api.push.apple.com"
    assert request.url.path == f"/3/device/{TOKEN}"
    assert request.headers["apns-topic"] == "app.mowdeck.mobile.push-type.liveactivity"
    assert request.headers["apns-push-type"] == "liveactivity"
    assert request.headers["apns-priority"] == "5"
    assert request.headers["authorization"].startswith("bearer ")
    assert json.loads(request.content) == payload


async def test_background_push_uses_the_bundle_topic() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    channel = make(httpx.MockTransport(handle))
    await channel.push(TOKEN, {"aps": {"content-available": 1}}, push_type="background", priority=5)
    assert seen[0].headers["apns-topic"] == "app.mowdeck.mobile"
    assert seen[0].headers["apns-push-type"] == "background"


@pytest.mark.parametrize(
    ("status", "reason", "outcome"),
    [
        (410, "Unregistered", DeliveryOutcome.GONE),
        (400, "BadDeviceToken", DeliveryOutcome.GONE),
        (429, "TooManyRequests", DeliveryOutcome.RETRY),
        (400, "BadPayload", DeliveryOutcome.FAILED),
    ],
)
async def test_answers_map_like_alerts(status: int, reason: str, outcome: DeliveryOutcome) -> None:
    channel = make(
        httpx.MockTransport(lambda request: httpx.Response(status, json={"reason": reason}))
    )
    result = await channel.push(TOKEN, {"aps": {}}, push_type="liveactivity", priority=10)
    assert result.outcome is outcome


async def test_bad_tokens_and_oversized_payloads_are_refused_before_the_network() -> None:
    called: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        called.append(1)
        return httpx.Response(200)

    channel = make(httpx.MockTransport(handle))
    bad = await channel.push("not-hex", {"aps": {}}, push_type="liveactivity", priority=5)
    big = await channel.push(
        TOKEN, {"aps": {"x": "y" * 5000}}, push_type="liveactivity", priority=5
    )
    assert bad.outcome is DeliveryOutcome.FAILED and big.outcome is DeliveryOutcome.FAILED
    assert called == []
