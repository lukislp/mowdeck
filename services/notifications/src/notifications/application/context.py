"""Configuration of the use cases (built from the settings)."""

from dataclasses import dataclass, field
from datetime import timedelta

from notifications.domain.models import RuleConfig


@dataclass(frozen=True, slots=True)
class NotificationsConfig:
    """Everything the use cases read from the settings."""

    rules: RuleConfig = field(default_factory=RuleConfig)
    inbox_keep: int = 200
    """Inbox items kept per user and account."""
    max_attempts: int = 8
    """Attempts of one delivery before it is marked failed."""
    backoff_base: timedelta = timedelta(seconds=30)
    backoff_max: timedelta = timedelta(hours=1)
    lease: timedelta = timedelta(seconds=60)
    """How long a claimed delivery stays invisible to other workers."""
    batch_size: int = 50
    """Deliveries claimed per worker round."""
    send_concurrency: int = 10
    """Deliveries of one channel sent at the same time by one worker (a channel may ask for
    fewer with a ``max_concurrency`` attribute, APNs does)."""
    send_deadline: timedelta = timedelta(seconds=30)
    """Hard limit of one ``channel.send``; a send still running is cancelled and retried. Must
    stay below the lease, so a claim never expires while its send is still running."""
    stall_after: timedelta = timedelta(minutes=5)
    """The outbox counts as stalled (``/readyz`` fails, an error is logged) when due work
    waits and no delivery finished for this long."""
    delivery_retention: timedelta = timedelta(days=7)
    """Finished deliveries are deleted after this."""
    offline_batch_size: int = 200
    test_every: timedelta = timedelta(seconds=60)
    """Minimum time between two test notifications of one user."""
    app_url: str | None = None
    """Public base URL of the web app; notification links point to ``<app_url>/mowers/<id>``."""
    ntfy_allowed_servers: tuple[str, ...] = ("https://ntfy.sh",)
    """ntfy servers a user may choose (origins, compared exactly)."""
    webpush_allowed_hosts: tuple[str, ...] = ()
    """Host suffixes a push endpoint may have (empty: any HTTPS host)."""
    ack_silence: timedelta = timedelta(hours=1)
    """How long acknowledging an alarm silences repeats of it for the user."""
    default_language: str = "de"
    """Language of a user who never saved preferences (the app's default language)."""
    image_capture: bool = False
    """Take a camera photo for error and theft events (needs a photo source)."""
    image_retention: timedelta = timedelta(hours=24)
    """How long a photo is kept (inbox and push URL)."""
    image_keep: int = 200
    """Photos kept at most (the oldest go first)."""
    image_max_bytes: int = 512 * 1024
    """Largest photo taken or stored."""
    image_daily_limit: int = 6
    """Photos per mower and rolling 24 hours (a budget of its own, on top of the cooldowns)."""
    image_min_interval: timedelta = timedelta(seconds=60)
    """Minimum time between two photos of one mower."""
