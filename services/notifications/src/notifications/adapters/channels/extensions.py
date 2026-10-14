"""Documented extension points for further channels (interfaces only, no implementation).

A new channel is a package with a class that satisfies
:class:`~notifications.application.ports.NotificationChannel` and an entry point::

    [project.entry-points."mowdeck.notification_channels"]
    email = "mowdeck_notify_email:create_email_channel"

The factory receives a :class:`~notifications.adapters.channels.ChannelContext` (settings,
shared HTTP client, clock) and returns the channel or None when it is not configured. The
fan-out queues one delivery without a target for every channel that is neither ``webpush`` nor
``ntfy``; such a channel finds its own address for ``delivery.user_id`` (for example through
the protocols below), so its users' addresses stay in its own storage.
"""

from typing import Protocol
from uuid import UUID


class EmailAddressBook(Protocol):
    """Where an e-mail channel finds a user's verified address."""

    async def address(self, user_id: UUID, account_id: UUID) -> str | None:
        """The verified address, None when the user has none."""
        ...


class EmailSender(Protocol):
    """Sends one e-mail (SMTP, an API provider, ...)."""

    async def send(self, to: str, subject: str, text: str) -> None:
        """Send, raising on temporary failures (the outbox retries)."""
        ...


class TelegramChats(Protocol):
    """Where a Telegram channel finds a user's chat (after the user started the bot)."""

    async def chat_id(self, user_id: UUID, account_id: UUID) -> int | None:
        """The chat id, None when the user has not linked Telegram."""
        ...


class TelegramBot(Protocol):
    """Sends one Telegram message (Bot API ``sendMessage``)."""

    async def send_message(self, chat_id: int, text: str) -> None:
        """Send, raising on temporary failures (the outbox retries)."""
        ...
