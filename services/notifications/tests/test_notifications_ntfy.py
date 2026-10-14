"""ntfy: a real server (``binwiederhier/ntfy``) receives the JSON publish.

Runs when ``MOWDECK_TEST_NTFY_URL`` is set (CI starts the server like NATS). Locally::

    docker run -d --name notifications-test-ntfy -p 8091:80 binwiederhier/ntfy:v2.28.0 serve
    MOWDECK_TEST_NTFY_URL=http://127.0.0.1:8091 uv run pytest services/notifications -k ntfy
    docker rm -f notifications-test-ntfy
"""

import json
import os
from uuid import uuid4

import httpx
import pytest
from mowdeck_contracts.v1 import NotificationKind, NotificationSeverity

from notifications.adapters.channels.ntfy import NtfyChannel
from notifications.domain.models import Delivery, DeliveryOutcome, Message, TargetKind

NTFY_URL = os.environ.get("MOWDECK_TEST_NTFY_URL")

pytestmark = pytest.mark.skipif(not NTFY_URL, reason="needs MOWDECK_TEST_NTFY_URL")


async def test_notifications_ntfy_real_server_receives_the_message() -> None:
    assert NTFY_URL
    server = NTFY_URL.rstrip("/").lower()
    topic = f"mowdeck-test-{uuid4().hex[:12]}"
    delivery = Delivery(
        id=uuid4(),
        user_id=uuid4(),
        account_id=uuid4(),
        channel="ntfy",
        message=Message(
            kind=NotificationKind.MOWING_FINISHED,
            severity=NotificationSeverity.INFO,
            title="Vorgarten ist fertig mit Mähen",
            body="42 Min. gemäht. Akku 55 %.",
            url="https://app.example.test/mowers/1",
            tag="mowing_finished:1",
            mower_id=None,
        ),
        attempts=1,
        target_kind=TargetKind.NTFY,
        target_id=None,
        target={"server_url": server, "topic": topic, "token": None},
    )
    async with httpx.AsyncClient() as http:
        channel = NtfyChannel(http, frozenset({server}))
        result = await channel.send(delivery)
        polled = await http.get(f"{server}/{topic}/json", params={"poll": "1"})
    assert result.outcome is DeliveryOutcome.SENT
    messages = [json.loads(line) for line in polled.text.splitlines() if line.strip()]
    message = next(m for m in messages if m.get("event") == "message")
    assert message["title"] == "Vorgarten ist fertig mit Mähen"
    assert message["message"] == "42 Min. gemäht. Akku 55 %."
    assert message["priority"] == 3
    assert message["tags"] == ["white_check_mark"]
    assert message["click"] == "https://app.example.test/mowers/1"
