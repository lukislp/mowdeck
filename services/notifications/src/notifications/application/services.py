"""The use cases of notifications, wired once at startup."""

from dataclasses import dataclass
from uuid import UUID

from notifications.application.context import NotificationsConfig
from notifications.application.fanout import Fanout
from notifications.application.outbox import DeliveryObserver, Outbox
from notifications.application.ports import Channels, Clock, EventPublisher, Store, ViewChecker
from notifications.application.users import UserNotifications
from notifications.application.watcher import Watcher
from notifications.domain.messages import MessageCatalog


class Accounts:
    """Reacts to Identity's user events."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def user_removed(self, user_id: UUID, account_id: UUID | None) -> None:
        """Drop the user's preferences, targets, inbox and pending deliveries."""
        await self._store.drop_user(user_id, account_id)


@dataclass(frozen=True, slots=True)
class Services:
    """Every use case."""

    watcher: Watcher
    fanout: Fanout
    outbox: Outbox
    users: UserNotifications
    accounts: Accounts
    config: NotificationsConfig


def build_services(
    store: Store,
    *,
    channels: Channels,
    views: ViewChecker,
    publisher: EventPublisher | None,
    catalog: MessageCatalog,
    config: NotificationsConfig,
    clock: Clock,
    observer: DeliveryObserver | None = None,
) -> Services:
    """Create the use cases over ``store``."""
    fanout = Fanout(store, channels, views, publisher, catalog, config, clock)
    return Services(
        watcher=Watcher(store, fanout, config, clock),
        fanout=fanout,
        outbox=Outbox(store, channels, config, clock, observer),
        users=UserNotifications(store, fanout, channels, views, catalog, config, clock),
        accounts=Accounts(store),
        config=config,
    )
