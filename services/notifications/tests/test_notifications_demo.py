"""Public demo (ADR 0068): nothing leaves the server; the inbox keeps working."""

from typing import Any

from mowdeck_service_kit.demo import DEMO_HEADER
from notifications_support import harness, make_settings

from notifications.adapters.api.demo import DEMO_POLICY

API = "/api/v1/notifications"


def test_demo_mode_turns_every_channel_off() -> None:
    settings = make_settings(
        demo_mode=True,
        notifications_channels="webpush,ntfy,apns",
        notifications_ntfy_allowed_servers="https://ntfy.sh",
        notifications_image_enabled=True,
    )
    assert settings.channel_names == ()
    assert settings.ntfy_servers == ()
    assert settings.notifications_image_enabled is False
    assert settings.apns_key is None
    normal = make_settings(notifications_channels="webpush,ntfy")
    assert normal.channel_names == ("webpush", "ntfy")


def test_the_policy_refuses_every_outbound_channel_but_not_the_inbox() -> None:
    refused: list[tuple[str, str]] = [
        ("POST", f"{API}/push-subscriptions"),
        ("DELETE", f"{API}/push-subscriptions/abc"),
        ("POST", f"{API}/me/push/apns"),
        ("PUT", f"{API}/me/live-activities/device"),
        ("PUT", f"{API}/me/live-activities/mowers/abc"),
        ("PUT", f"{API}/ntfy"),
        ("POST", f"{API}/test"),
    ]
    allowed: list[tuple[str, str]] = [
        ("PUT", f"{API}/preferences"),
        ("POST", f"{API}/inbox/abc/read"),
        ("POST", f"{API}/inbox/abc/ack"),
        ("GET", f"{API}/inbox"),
    ]
    for method, path in refused:
        assert DEMO_POLICY.refusal(method, path), (method, path)
    for method, path in allowed:
        assert DEMO_POLICY.refusal(method, path) is None, (method, path)


async def test_the_service_refuses_a_push_subscription_in_demo_mode() -> None:
    kwargs: dict[str, Any] = {"demo_mode": True}
    async with harness(**kwargs) as h:
        response = await h.client.post(
            f"{API}/push-subscriptions",
            json={"endpoint": "https://fcm.googleapis.com/x", "keys": {"p256dh": "a", "auth": "b"}},
        )
    assert response.status_code == 403
    assert response.headers[DEMO_HEADER] == "1"
