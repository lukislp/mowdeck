"""Settings of the notifications service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). OpenFGA is
configured with the ``MOWDECK_OPENFGA_*`` variables of ``mowdeck-authz``, token verification
with the ``MOWDECK_AUTH_*`` variables of ``mowdeck-auth``.
"""

from datetime import timedelta
from typing import Any, Literal

from mowdeck_service_kit import ServiceSettings
from pydantic import Field, SecretStr, field_validator

from notifications.application.context import NotificationsConfig
from notifications.domain.models import RuleConfig


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


class Settings(ServiceSettings):
    """Settings of notifications: the kit settings plus service defaults."""

    service_name: str = Field(
        default="notifications", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    # --- rules -----------------------------------------------------------------------------

    notifications_mowing_min_seconds: float = Field(default=120, ge=0)
    """A mowing job shorter than this ends without ``mowing_finished``."""

    notifications_stuck_no_move_seconds: float = Field(default=300, ge=0)
    """``mower_stuck`` after this long without a position change while mowing (0: off)."""

    notifications_stuck_move_meters: float = Field(default=0.5, gt=0)
    """A position change below this counts as standing still."""

    notifications_battery_low_percent: int = Field(default=20, ge=1, le=100)
    """``battery_low`` below this battery level (not while charging)."""

    notifications_battery_rearm_percent: int = Field(default=30, ge=1, le=100)
    """``battery_low`` fires again only after the battery was back at this level."""

    notifications_offline_after_seconds: float = Field(default=120, ge=10)
    """``mower_offline`` when no state arrived for this long (the gateway sends one every 10 s)."""

    notifications_disconnect_grace_seconds: float = Field(default=30, ge=0)
    """``mower.disconnected`` becomes ``mower_offline`` after this long without a reconnect."""

    notifications_cooldown_seconds: float = Field(default=300, ge=0)
    """At most one notification of one kind per mower within this time."""

    notifications_offline_check_seconds: float = Field(default=10, ge=1)
    """How often the offline timer runs."""

    # --- theft alarm -----------------------------------------------------------------------

    notifications_alarm_cooldown_seconds: float = Field(default=600, ge=0)
    """At most one theft alarm (``mower_lifted``, ``mower_left_dock_unexpectedly``,
    ``geofence_exit``) of one kind per mower within this time."""

    notifications_lift_error_codes: str = "lifted,1004,1030,2502,2801,11131,11133"
    """Comma separated error codes that mean the mower was lifted or tilted (``mower_lifted``);
    a state with ``raw.lifted = true`` counts as lifted regardless."""

    notifications_dock_command_window_seconds: float = Field(default=600, ge=0)
    """A command seen this long before the mower leaves the dock explains the departure (no
    ``mower_left_dock_unexpectedly``); a manual drive frame within this time also suspends
    ``geofence_exit``."""

    notifications_dock_confirm_seconds: float = Field(default=20, ge=0)
    """An unexplained departure from the dock alarms only when a state at least this much later
    still shows the mower away without a command (0: at once)."""

    notifications_geofence_margin_meters: float = Field(default=5, ge=0, le=1000)
    """``geofence_exit`` when the position is further than this outside the mapped areas,
    transfer paths and dock."""

    notifications_ack_silence_seconds: float = Field(default=3600, ge=0)
    """Acknowledging an alarm silences repeats of it (same kind and mower) for the user this
    long."""

    # --- delivery --------------------------------------------------------------------------

    notifications_channels: str = "webpush,ntfy,apns"
    """Comma separated channels to enable (entry points of ``mowdeck.notification_channels``);
    ``log`` writes every message to the log (development). A channel without its configuration
    (VAPID keys, APNs key) is skipped."""

    notifications_vapid_private_key: SecretStr | None = None
    """VAPID private key (base64url raw key, base64url DER or PEM); Web Push is off without it
    unless ``NOTIFICATIONS_VAPID_KEY_PATH`` is set."""

    notifications_vapid_public_key: str | None = None
    """VAPID public key (base64url); optional, checked against the private key."""

    notifications_vapid_subject: str | None = None
    """VAPID ``sub`` claim, a ``mailto:`` or ``https:`` contact of the operator."""

    notifications_vapid_key_path: str | None = None
    """Development only: without a configured private key, load it from this file or create it
    there on first start (Compose mounts a volume); production sets the keys explicitly."""

    notifications_webpush_allowed_hosts: str = (
        "fcm.googleapis.com,push.services.mozilla.com,notify.windows.com,push.apple.com"
    )
    """Comma separated host suffixes a push endpoint may have (empty: any HTTPS host)."""

    notifications_ntfy_allowed_servers: str = "https://ntfy.sh"
    """Comma separated ntfy servers (origins) users may choose; empty disables ntfy."""

    apns_key: SecretStr | None = None
    """APNs signing key: the PEM contents of ``AuthKey_<KEY_ID>.p8`` from the Apple Developer
    account, usually given as the secret file ``MOWDECK_APNS_KEY_FILE``; the ``apns`` channel is
    off without it, the key id and the team id."""

    apns_key_id: str | None = Field(default=None, min_length=10, max_length=10)
    """Key ID of the APNs signing key (10 characters, the ``kid`` of the provider token)."""

    apns_team_id: str | None = Field(default=None, min_length=10, max_length=10)
    """Apple Developer Team ID (10 characters, the ``iss`` of the provider token)."""

    apns_bundle_id: str = Field(default="app.mowdeck.mobile", min_length=1, max_length=200)
    """Bundle ID of the iOS app, sent as ``apns-topic``."""

    apns_environment: Literal["development", "production"] = "development"
    """``development``: ``api.sandbox.push.apple.com`` (apps installed from Xcode with a
    development provisioning profile); ``production``: ``api.push.apple.com`` (TestFlight and
    App Store builds)."""

    notifications_app_url: str | None = None
    """Public base URL of the web app (links in notifications: ``<url>/mowers/<id>``)."""

    notifications_http_timeout_seconds: float = Field(default=10, gt=0, le=120)
    """Timeout of one request to a push service or ntfy server."""

    notifications_max_attempts: int = Field(default=8, ge=1, le=50)
    """Attempts of one delivery before it is marked failed."""

    notifications_backoff_base_seconds: float = Field(default=30, ge=0.1)
    """First retry delay; doubles per attempt."""

    notifications_backoff_max_seconds: float = Field(default=3600, ge=1)
    """Upper bound of the retry delay."""

    notifications_worker_interval_seconds: float = Field(default=1, ge=0.05)
    """Pause of the delivery worker when the outbox had nothing due."""

    notifications_worker_batch_size: int = Field(default=50, ge=1, le=1000)
    """Deliveries one worker claims per round."""

    notifications_lease_seconds: float = Field(default=60, ge=5)
    """A claimed delivery is invisible to other workers this long (crash recovery)."""

    notifications_delivery_retention_days: int = Field(default=7, ge=1, le=365)
    """Finished deliveries are deleted after this many days."""

    notifications_inbox_size: int = Field(default=200, ge=1, le=10_000)
    """Inbox items kept per user and account."""

    notifications_test_interval_seconds: float = Field(default=60, ge=0)
    """Minimum time between two test notifications of one user (429 otherwise)."""

    notifications_fleet_timeout_seconds: float = Field(default=5, gt=0)
    """Timeout of the ``fleet.mowers.list`` request that seeds the mower directory."""

    notifications_fleet_retry_max_seconds: float = Field(default=30, ge=1)
    """Upper bound of the backoff between seeding attempts."""

    @field_validator(
        "notifications_vapid_private_key",
        "notifications_vapid_public_key",
        "notifications_vapid_subject",
        "notifications_vapid_key_path",
        "notifications_app_url",
        "apns_key",
        "apns_key_id",
        "apns_team_id",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: Any) -> Any:
        """An empty variable (Compose passes ``${X:-}``) means "not set"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def channel_names(self) -> tuple[str, ...]:
        """``notifications_channels`` as a tuple."""
        return _split(self.notifications_channels)

    @property
    def ntfy_servers(self) -> tuple[str, ...]:
        """``notifications_ntfy_allowed_servers`` as a tuple."""
        return _split(self.notifications_ntfy_allowed_servers)

    @property
    def lift_error_codes(self) -> frozenset[str]:
        """``notifications_lift_error_codes`` as a set."""
        return frozenset(_split(self.notifications_lift_error_codes))

    @property
    def webpush_hosts(self) -> tuple[str, ...]:
        """``notifications_webpush_allowed_hosts`` as a tuple."""
        return _split(self.notifications_webpush_allowed_hosts)

    def to_config(self) -> NotificationsConfig:
        """The use case configuration."""
        return NotificationsConfig(
            rules=RuleConfig(
                mowing_min_seconds=self.notifications_mowing_min_seconds,
                stuck_no_move_seconds=self.notifications_stuck_no_move_seconds,
                stuck_move_meters=self.notifications_stuck_move_meters,
                battery_low_percent=self.notifications_battery_low_percent,
                battery_rearm_percent=max(
                    self.notifications_battery_rearm_percent,
                    self.notifications_battery_low_percent,
                ),
                offline_after_seconds=self.notifications_offline_after_seconds,
                disconnect_grace_seconds=self.notifications_disconnect_grace_seconds,
                cooldown_seconds=self.notifications_cooldown_seconds,
                alarm_cooldown_seconds=self.notifications_alarm_cooldown_seconds,
                lift_error_codes=self.lift_error_codes,
                dock_command_window_seconds=self.notifications_dock_command_window_seconds,
                dock_confirm_seconds=self.notifications_dock_confirm_seconds,
                geofence_margin_meters=self.notifications_geofence_margin_meters,
            ),
            inbox_keep=self.notifications_inbox_size,
            max_attempts=self.notifications_max_attempts,
            backoff_base=timedelta(seconds=self.notifications_backoff_base_seconds),
            backoff_max=timedelta(seconds=self.notifications_backoff_max_seconds),
            lease=timedelta(seconds=self.notifications_lease_seconds),
            batch_size=self.notifications_worker_batch_size,
            delivery_retention=timedelta(days=self.notifications_delivery_retention_days),
            test_every=timedelta(seconds=self.notifications_test_interval_seconds),
            app_url=self.notifications_app_url,
            ntfy_allowed_servers=self.ntfy_servers,
            webpush_allowed_hosts=self.webpush_hosts,
            ack_silence=timedelta(seconds=self.notifications_ack_silence_seconds),
        )
