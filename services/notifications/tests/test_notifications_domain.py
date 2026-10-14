"""Messages, quiet hours, preferences filter and model helpers."""

import json
from datetime import UTC, datetime, time, timedelta
from importlib import resources
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import NotificationKind, NotificationSeverity

from notifications.domain.messages import MessageCatalog
from notifications.domain.models import (
    DEFAULT_KINDS,
    RULE_KINDS,
    Message,
    MowerWatch,
    NotificationEvent,
    Preferences,
    QuietHours,
    backoff_delay,
)
from notifications.domain.preferences import in_quiet_hours, valid_timezone, wants
from notifications.messages import LANGUAGES, load_catalog


def test_notifications_domain_templates_cover_every_kind_in_every_language() -> None:
    catalog = load_catalog()
    assert catalog.languages == sorted(LANGUAGES)
    package = resources.files("notifications.messages")
    keys = {
        lang: set(json.loads(package.joinpath(f"{lang}.json").read_text("utf-8")))
        for lang in LANGUAGES
    }
    assert keys["de"] == keys["en"]
    for kind in NotificationKind:
        assert f"{kind.value}.title" in keys["en"]
        assert any(k.startswith(f"{kind.value}.body") for k in keys["en"])


def test_notifications_domain_render_with_fallbacks() -> None:
    catalog = load_catalog()
    params = {"mower": "Front lawn", "minutes": 42, "battery": 55}
    assert catalog.render("en", "mowing_finished.title", params) == "Front lawn finished mowing"
    assert catalog.render("de", "mowing_finished.body", params) == "42 Min. gemäht. Akku 55 %."
    assert catalog.render("fr", "mowing_finished.title", params) == "Front lawn finished mowing"
    assert catalog.render("de", "no.such.key", {}) == "no.such.key"
    # a missing parameter renders empty, the whitespace is collapsed
    assert catalog.render("en", "mower_stuck.body_error", {"error_code": "E1"}) == "Error E1."
    broken = MessageCatalog({"en": {"x": "{a.b}", "y": "{0}"}})
    assert broken.render("en", "x", {}) == "{a.b}"
    assert broken.render("en", "y", {}) == "{0}"
    assert broken.keys("en") == {"x", "y"}
    with pytest.raises(ValueError, match="default language"):
        MessageCatalog({"de": {}})


@pytest.mark.parametrize(
    ("start", "end", "local", "quiet"),
    [
        ("22:00", "07:00", "23:30", True),
        ("22:00", "07:00", "06:59", True),
        ("22:00", "07:00", "07:00", False),
        ("22:00", "07:00", "12:00", False),
        ("12:00", "14:00", "13:00", True),
        ("12:00", "14:00", "14:00", False),
        ("08:00", "08:00", "08:00", False),
    ],
)
def test_notifications_domain_quiet_hours_windows(
    start: str, end: str, local: str, quiet: bool
) -> None:
    window = QuietHours(time.fromisoformat(start), time.fromisoformat(end))
    hour, minute = map(int, local.split(":"))
    at = datetime(2026, 10, 9, hour, minute, tzinfo=UTC)
    assert in_quiet_hours(window, "UTC", at) is quiet


def test_notifications_domain_quiet_hours_follow_the_time_zone() -> None:
    window = QuietHours(time(22, 0), time(7, 0))
    # 21:30 UTC is 23:30 in Berlin (summer time) and 17:30 in New York
    at = datetime(2026, 7, 1, 21, 30, tzinfo=UTC)
    assert in_quiet_hours(window, "Europe/Berlin", at)
    assert not in_quiet_hours(window, "America/New_York", at)
    assert not in_quiet_hours(window, "Not/AZone", at)  # falls back to UTC: 21:30
    assert not in_quiet_hours(None, "UTC", at)
    # winter: 21:30 UTC is 22:30 in Berlin
    assert in_quiet_hours(window, "Europe/Berlin", datetime(2026, 12, 1, 21, 30, tzinfo=UTC))
    assert valid_timezone("Europe/Berlin") and not valid_timezone("Mars/Base")
    assert not valid_timezone("../etc")


def test_notifications_domain_wants_kind_and_mower() -> None:
    mower, other = uuid4(), uuid4()
    prefs = Preferences(user_id=uuid4(), account_id=uuid4(), mower_ids=frozenset({mower}))
    event = NotificationEvent.of(
        NotificationKind.BATTERY_LOW, mower, prefs.account_id, datetime.now(UTC), "x"
    )
    assert wants(prefs, event)
    assert not wants(prefs.evolve(enabled_kinds=frozenset()), event)
    assert not wants(prefs.evolve(mower_ids=frozenset({other})), event)
    assert wants(prefs.evolve(mower_ids=frozenset(), all_mowers=True), event)
    started = NotificationEvent.of(
        NotificationKind.JOB_STARTED, mower, prefs.account_id, datetime.now(UTC), "y"
    )
    assert not wants(prefs, started)  # off by default
    assert NotificationKind.JOB_STARTED in RULE_KINDS - DEFAULT_KINDS
    assert NotificationKind.TEST not in RULE_KINDS


def test_notifications_domain_message_json_and_backoff() -> None:
    message = Message(
        kind=NotificationKind.TEST,
        severity=NotificationSeverity.INFO,
        title="t",
        body="b",
        url=None,
        tag="test",
        mower_id=None,
    )
    assert Message.from_json(message.to_json()) == message
    full = Message(
        kind=NotificationKind.BATTERY_LOW,
        severity=NotificationSeverity.WARNING,
        title="t",
        body="b",
        url="/mowers/x",
        tag="battery_low:x",
        mower_id=uuid4(),
        notification_id=uuid4(),
    )
    assert Message.from_json(full.to_json()) == full
    base, maximum = timedelta(seconds=30), timedelta(hours=1)
    assert [backoff_delay(n, base, maximum).total_seconds() for n in (1, 2, 3, 4)] == [
        30,
        60,
        120,
        240,
    ]
    assert backoff_delay(500, base, maximum) == maximum
    assert backoff_delay(0, base, maximum) == base


def test_notifications_domain_watch_needs_last_seen() -> None:
    with pytest.raises(ValueError, match="last_seen_at"):
        MowerWatch.from_json(uuid4(), uuid4(), {})
