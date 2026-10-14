"""Delivery channels and their registry (extension point ``mowdeck.notification_channels``).

A channel is any object with a ``name`` and ``async send(delivery) -> DeliveryResult`` (the
:class:`~notifications.application.ports.NotificationChannel` protocol). Channels are found
through the Python entry point group ``mowdeck.notification_channels``: the entry point name is
the channel name and the object is a factory ``(ChannelContext) -> NotificationChannel | None``
(None: not configured, for example Web Push without VAPID keys). This package registers its own
channels the same way::

    [project.entry-points."mowdeck.notification_channels"]
    log = "notifications.adapters.channels.log:create_log_channel"
    webpush = "notifications.adapters.channels.webpush:create_webpush_channel"
    ntfy = "notifications.adapters.channels.ntfy:create_ntfy_channel"
    apns = "notifications.adapters.channels.apns:create_apns_channel"

Only the channels named in ``MOWDECK_NOTIFICATIONS_CHANNELS`` are created. A channel may have an
``async aclose()`` (for example APNs with its own HTTP/2 client); the app calls it on shutdown.
"""

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points

import httpx

from notifications.adapters.channels.vapid import VapidKeys
from notifications.application.ports import Clock, NotificationChannel
from notifications.settings import Settings

ENTRY_POINT_GROUP = "mowdeck.notification_channels"

_log = logging.getLogger("notifications.channels")


@dataclass(frozen=True, slots=True)
class ChannelContext:
    """What a channel factory gets."""

    settings: Settings
    http: httpx.AsyncClient
    """Shared HTTP client (no redirects); tests pass one with a mock transport."""
    clock: Clock
    vapid: VapidKeys | None
    """The VAPID key pair, None when Web Push is not configured."""


ChannelFactory = Callable[[ChannelContext], NotificationChannel | None]
EntryPointSource = Callable[[], Iterable[EntryPoint]]


def _installed_entry_points() -> Iterable[EntryPoint]:
    return entry_points(group=ENTRY_POINT_GROUP)


def discover_factories(
    source: EntryPointSource = _installed_entry_points,
) -> dict[str, ChannelFactory]:
    """Every channel factory registered as an entry point (broken ones are logged, skipped)."""
    factories: dict[str, ChannelFactory] = {}
    for entry_point in source():
        try:
            factory = entry_point.load()
        except Exception:
            _log.exception("cannot load notification channel %s", entry_point)
            continue
        if not callable(factory):
            _log.warning("ignoring notification channel %s: not callable", entry_point)
            continue
        factories.setdefault(entry_point.name, factory)
    return factories


class ChannelRegistry:
    """The configured channels (implements the ``Channels`` port)."""

    def __init__(self, channels: Mapping[str, NotificationChannel]) -> None:
        self._channels = dict(channels)

    @classmethod
    def build(
        cls,
        context: ChannelContext,
        enabled: Iterable[str],
        factories: Mapping[str, ChannelFactory] | None = None,
    ) -> "ChannelRegistry":
        """Create the ``enabled`` channels with their factories."""
        available = dict(factories) if factories is not None else discover_factories()
        channels: dict[str, NotificationChannel] = {}
        for name in enabled:
            factory = available.get(name)
            if factory is None:
                _log.warning("notification channel %r is not installed", name)
                continue
            channel = factory(context)
            if channel is None:
                # Enabled by default but not set up (no VAPID keys, no APNs key): not an error.
                _log.info("notification channel %r is not configured, skipped", name)
                continue
            channels[name] = channel
        return cls(channels)

    async def aclose(self) -> None:
        """Close every channel that has an ``aclose`` (own HTTP clients)."""
        for channel in self._channels.values():
            close = getattr(channel, "aclose", None)
            if callable(close):
                await close()

    def names(self) -> list[str]:
        """Names of the configured channels, sorted."""
        return sorted(self._channels)

    def get(self, name: str) -> NotificationChannel | None:
        """The channel called ``name``."""
        return self._channels.get(name)
