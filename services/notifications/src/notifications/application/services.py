"""The use cases of notifications, wired once at startup."""

from dataclasses import dataclass
from uuid import UUID

from mowdeck_contracts.v1 import NewDeviceLogin, NotificationKind

from notifications.application.context import NotificationsConfig
from notifications.application.demo import DemoPurge
from notifications.application.fanout import Fanout, Notified
from notifications.application.live_activity import LiveActivities
from notifications.application.outbox import DeliveryObserver, Outbox
from notifications.application.ports import (
    Channels,
    Clock,
    ErrorExplainer,
    EventPublisher,
    PhotoSource,
    Store,
    ViewChecker,
)
from notifications.application.push_image import PushImages
from notifications.application.users import UserNotifications
from notifications.application.watcher import Watcher
from notifications.domain.messages import MessageCatalog
from notifications.domain.preferences import default_preferences
from notifications.domain.push_image import ImageLinks


class Accounts:
    """Reacts to Identity's user events."""

    def __init__(
        self,
        store: Store,
        fanout: Fanout | None = None,
        catalog: MessageCatalog | None = None,
        config: NotificationsConfig | None = None,
    ) -> None:
        self._store = store
        self._fanout = fanout
        self._catalog = catalog
        self._config = config or NotificationsConfig()

    async def user_removed(self, user_id: UUID, account_id: UUID | None) -> None:
        """Drop the user's preferences, targets, inbox and pending deliveries."""
        await self._store.drop_user(user_id, account_id)

    async def new_login(self, alert: NewDeviceLogin) -> Notified | None:
        """``security_new_login`` for the one user who signed in from a new device (ADR 0065).

        In the inbox and over the user's channels of that account (saved preferences or the
        defaults), also during quiet hours: "this wasn't me" cannot wait until morning. The
        notification opens the security page with the session to end; one per session. Never
        published to the account (``notifications.raised``): it concerns this user alone.
        Returns None when it was already stored (a redelivered event).
        """
        if self._fanout is None or self._catalog is None:
            return None
        preferences = await self._store.preferences(
            alert.user_id, alert.account_id
        ) or default_preferences(
            alert.user_id, alert.account_id, language=self._config.default_language
        )
        language = preferences.language
        params = {
            "device": self._device(language, alert),
            "where": self._where(language, alert),
        }
        link = f"/settings/security?revoke={alert.session_id}"
        return await self._fanout.notify(
            preferences,
            kind=NotificationKind.SECURITY_NEW_LOGIN,
            title_key="security_new_login.title",
            body_key="security_new_login.body",
            params=params,
            mower_id=None,
            occurred_at=alert.at,
            dedupe_key=f"security_new_login:{alert.session_id}",
            respect_quiet_hours=False,
            url=self._fanout.url_for(None, link),
        )

    def _device(self, language: str, alert: NewDeviceLogin) -> str:
        assert self._catalog is not None  # noqa: S101 - checked by new_login
        if alert.client == "app" and alert.os:
            return self._catalog.render(language, "security_new_login.device_app", {"os": alert.os})
        if alert.browser and alert.os:
            values = {"browser": alert.browser, "os": alert.os}
            return self._catalog.render(language, "security_new_login.device_browser", values)
        return alert.device

    def _where(self, language: str, alert: NewDeviceLogin) -> str:
        assert self._catalog is not None  # noqa: S101 - checked by new_login
        if alert.network == "local":
            local = self._catalog.render(language, "security_new_login.where_local", {})
            return f"{local}, {alert.ip_prefix}" if alert.ip_prefix else local
        if alert.ip_prefix:
            return alert.ip_prefix
        return self._catalog.render(language, "security_new_login.where_unknown", {})


@dataclass(frozen=True, slots=True)
class Services:
    """Every use case."""

    watcher: Watcher
    fanout: Fanout
    outbox: Outbox
    users: UserNotifications
    accounts: Accounts
    config: NotificationsConfig
    live: LiveActivities | None = None
    """Live Activities and widget reloads (ADR 0059)."""
    push_images: PushImages | None = None
    """Serves the push image URLs; None without a public URL and signing secret."""
    demo: DemoPurge | None = None
    """The demo sandbox purge (ADR 0076)."""


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
    photos: PhotoSource | None = None,
    image_links: ImageLinks | None = None,
    errors: ErrorExplainer | None = None,
) -> Services:
    """Create the use cases over ``store`` (``photos``: where error and theft events get their
    camera photo from, None for none; ``image_links``: the signed push image URLs; ``errors``:
    explains vendor error codes in the push texts, None keeps the vendor's message)."""
    fanout = Fanout(store, channels, views, publisher, catalog, config, clock, photos, errors)
    live = LiveActivities(store, channels, views, clock, errors)
    watcher = Watcher(store, fanout, config, clock, live)
    return Services(
        watcher=watcher,
        fanout=fanout,
        outbox=Outbox(store, channels, config, clock, observer),
        users=UserNotifications(store, fanout, channels, views, catalog, config, clock),
        accounts=Accounts(store, fanout, catalog, config),
        config=config,
        live=live,
        push_images=PushImages(store, image_links, clock) if image_links is not None else None,
        demo=DemoPurge(store, [fanout.forget_mowers, watcher.forget_mowers, live.forget_mowers]),
    )
