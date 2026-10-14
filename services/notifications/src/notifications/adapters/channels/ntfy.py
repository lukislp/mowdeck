"""ntfy (https://ntfy.sh or a self-hosted server): one JSON publish per message.

The message is posted as JSON to the server root (``{"topic", "title", "message",
"priority", "tags", "click"}``), which carries any Unicode text without header encoding. An
access token goes into ``Authorization: Bearer``. The server must be on the allow list
(``MOWDECK_NOTIFICATIONS_NTFY_ALLOWED_SERVERS``) at send time as well, so removing a server from
the list stops deliveries to it at once.
"""

import httpx
from mowdeck_contracts.v1 import NotificationKind, NotificationSeverity

from notifications.adapters.channels import ChannelContext
from notifications.adapters.channels.webpush import classify
from notifications.domain.models import Delivery, DeliveryOutcome, DeliveryResult

PRIORITY = {
    NotificationSeverity.CRITICAL: 5,
    NotificationSeverity.WARNING: 4,
    NotificationSeverity.INFO: 3,
}
TAGS = {
    NotificationKind.MOWING_FINISHED: ["white_check_mark"],
    NotificationKind.JOB_STARTED: ["arrow_forward"],
    NotificationKind.MOWER_STUCK: ["warning"],
    NotificationKind.BATTERY_LOW: ["battery"],
    NotificationKind.MOWER_OFFLINE: ["electric_plug"],
    NotificationKind.MOWER_ONLINE: ["signal_strength"],
    NotificationKind.MOWER_LIFTED: ["rotating_light"],
    NotificationKind.MOWER_LEFT_DOCK_UNEXPECTEDLY: ["rotating_light"],
    NotificationKind.GEOFENCE_EXIT: ["rotating_light"],
    NotificationKind.BLADE_MAINTENANCE: ["wrench"],
    NotificationKind.TEST: ["bell"],
}


class NtfyChannel:
    """Publishes to the user's ntfy topic."""

    name = "ntfy"

    def __init__(
        self, http: httpx.AsyncClient, allowed_servers: frozenset[str], timeout: float = 10.0
    ) -> None:
        self._http = http
        self._allowed = allowed_servers
        self._timeout = timeout

    def body(self, delivery: Delivery) -> dict[str, object]:
        """The JSON publish request."""
        message = delivery.message
        target = delivery.target or {}
        body: dict[str, object] = {
            "topic": target.get("topic"),
            "title": message.title,
            "message": message.body,
            "priority": PRIORITY[message.severity],
            "tags": TAGS.get(message.kind, []),
        }
        if message.url and message.url.startswith(("https://", "http://")):
            body["click"] = message.url
        return body

    async def send(self, delivery: Delivery) -> DeliveryResult:
        """Publish one message."""
        target = delivery.target or {}
        server = str(target.get("server_url", "")).rstrip("/").lower()
        if server not in self._allowed:
            return DeliveryResult(DeliveryOutcome.FAILED, "ntfy server is not allowed")
        headers = {"Content-Type": "application/json"}
        token = target.get("token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self._http.post(
                f"{server}/", json=self.body(delivery), headers=headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            return DeliveryResult(DeliveryOutcome.RETRY, f"ntfy request failed: {exc!r}")
        result = classify(response, self.name)
        if result.outcome is DeliveryOutcome.GONE:
            # A 404 from an ntfy server means a wrong URL, not an expired target.
            return DeliveryResult(DeliveryOutcome.FAILED, result.error)
        return result


def create_ntfy_channel(context: ChannelContext) -> NtfyChannel | None:
    """Entry point factory: None when no server is allowed."""
    allowed = frozenset(s.rstrip("/").lower() for s in context.settings.ntfy_servers)
    if not allowed:
        return None
    return NtfyChannel(
        context.http, allowed, timeout=context.settings.notifications_http_timeout_seconds
    )
