"""A channel that writes each message to the service log (development and tests)."""

from mowdeck_service_kit import get_logger

from notifications.adapters.channels import ChannelContext
from notifications.domain.models import Delivery, DeliveryResult

_log = get_logger("notifications.channel.log")


class LogChannel:
    """Logs ``notification delivered`` with the rendered text; never fails."""

    name = "log"

    def __init__(self) -> None:
        self.sent: list[Delivery] = []
        """Every delivery, for assertions in tests."""

    async def send(self, delivery: Delivery) -> DeliveryResult:
        """Write the message to the log."""
        self.sent.append(delivery)
        message = delivery.message
        _log.info(
            "notification delivered",
            channel=self.name,
            user_id=str(delivery.user_id),
            account_id=str(delivery.account_id),
            kind=message.kind.value,
            severity=message.severity.value,
            mower_id=str(message.mower_id) if message.mower_id else None,
            title=message.title,
            body=message.body,
        )
        return DeliveryResult.sent()


def create_log_channel(context: ChannelContext) -> LogChannel:
    """Entry point factory."""
    del context
    return LogChannel()
