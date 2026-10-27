"""The deploy maintenance window (ADR 0053): window arithmetic, the suppression of the offline /
online pair, the release when the mower stays offline, the CLI and the stores."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import MowerActivity
from notifications_support import T0, harness, make_state, seconds

from notifications import cli
from notifications.adapters.memory import MemoryStore
from notifications.application.maintenance import InvalidWindowError, close_window, open_window
from notifications.domain.models import MaintenanceWindow, MowerWatch, RuleConfig
from notifications.domain.rules import (
    check_offline,
    observe_connection,
    observe_state,
    release_held_offline,
)

CONFIG = RuleConfig(offline_after_seconds=120, disconnect_grace_seconds=30, cooldown_seconds=300)
MOWER, ACCOUNT = uuid4(), uuid4()


def window(
    open_s: float = 0, length_s: float = 900, *, closed_s: float | None = None
) -> MaintenanceWindow:
    """A window opened ``open_s`` after T0 that lasts ``length_s``."""
    return MaintenanceWindow(
        opened_at=T0 + seconds(open_s),
        expires_at=T0 + seconds(open_s + length_s),
        closed_at=None if closed_s is None else T0 + seconds(closed_s),
    )


# --- the window ----------------------------------------------------------------------------


def test_maintenance_window_ends_at_close_or_expiry() -> None:
    assert window().ends_at == T0 + seconds(900)
    assert window(closed_s=300).ends_at == T0 + seconds(300)
    assert window(closed_s=5000).ends_at == T0 + seconds(900)  # never beyond the expiry
    assert window().is_open(T0 + seconds(1))
    assert not window().is_open(T0 + seconds(900))
    assert not window(closed_s=300).is_open(T0 + seconds(400))


def test_maintenance_window_holds_offline_inside_and_shortly_after() -> None:
    w = window(closed_s=300)
    assert w.holds(T0 + seconds(100), T0 + seconds(130), CONFIG)
    assert w.holds(T0 + seconds(290), T0 + seconds(300 + 120), CONFIG)  # grace
    assert not w.holds(T0 + seconds(290), T0 + seconds(300 + 121), CONFIG)
    assert w.holds(T0 - seconds(30), T0 + seconds(5), CONFIG)  # last state just before the open
    assert not w.holds(T0 - seconds(3600), T0 + seconds(5), CONFIG)  # a real earlier outage
    assert not w.holds(T0 + seconds(301), T0 + seconds(310), CONFIG)  # began after the close


# --- the rules -----------------------------------------------------------------------------


def _seen(at: float = 0) -> MowerWatch:
    watch, _ = observe_state(
        None, make_state(MOWER, ACCOUNT, at=T0 + seconds(at)), T0 + seconds(at), CONFIG
    )
    return watch


def test_maintenance_disconnect_in_window_sends_nothing_and_no_online() -> None:
    w = window()
    watch = _seen()
    watch, events = observe_connection(
        watch,
        MOWER,
        ACCOUNT,
        connected=False,
        at=T0 + seconds(20),
        now=T0 + seconds(20),
        config=CONFIG,
        window=w,
    )
    watch, events = check_offline(watch, T0 + seconds(60), CONFIG, w)
    assert events == []
    assert watch.offline
    assert watch.offline_held
    assert not watch.offline_notified
    watch, events = observe_connection(
        watch,
        MOWER,
        ACCOUNT,
        connected=True,
        at=T0 + seconds(70),
        now=T0 + seconds(70),
        config=CONFIG,
        window=w,
    )
    assert events == []
    assert not watch.offline
    assert not watch.offline_held


def test_maintenance_without_window_the_offline_is_sent() -> None:
    watch = _seen()
    _, events = check_offline(watch, T0 + seconds(200), CONFIG, None)
    assert [e.kind.value for e in events] == ["mower_offline"]


def test_maintenance_still_offline_after_close_is_announced() -> None:
    w = window(closed_s=120)
    watch, _ = check_offline(_seen(), T0 + seconds(130), CONFIG, w)
    assert watch.offline_held
    # inside window + grace (120 + 120): nothing yet
    same, events = release_held_offline(watch, T0 + seconds(240), CONFIG, w)
    assert events == []
    assert same == watch
    released, events = release_held_offline(watch, T0 + seconds(241), CONFIG, w)
    assert [e.kind.value for e in events] == ["mower_offline"]
    assert released.offline_notified
    assert not released.offline_held
    # the mower comes back later: now an online follows, like any announced offline
    _, events = observe_connection(
        released,
        MOWER,
        ACCOUNT,
        connected=True,
        at=T0 + seconds(300),
        now=T0 + seconds(300),
        config=CONFIG,
    )
    assert [e.kind.value for e in events] == ["mower_online"]


def test_maintenance_offline_noticed_after_window_and_grace_is_sent() -> None:
    w = window(closed_s=100)
    _, events = check_offline(_seen(), T0 + seconds(400), CONFIG, w)
    assert [e.kind.value for e in events] == ["mower_offline"]


def test_maintenance_expired_window_holds_for_its_expiry_only() -> None:
    w = window(length_s=60)  # forgotten window, expired after a minute
    _, events = check_offline(_seen(), T0 + seconds(500), CONFIG, w)
    assert [e.kind.value for e in events] == ["mower_offline"]


def test_maintenance_reported_offline_state_is_held_too() -> None:
    w = window()
    watch, events = observe_state(
        _seen(),
        make_state(MOWER, ACCOUNT, at=T0 + seconds(10), activity=MowerActivity.OFFLINE),
        T0 + seconds(10),
        CONFIG,
        None,
        w,
    )
    assert events == []
    assert watch.offline_held


# --- the use cases -------------------------------------------------------------------------


async def test_maintenance_deploy_restart_notifies_nobody() -> None:
    async with harness(notifications_disconnect_grace_seconds=30) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner)
        await open_window(h.store, h.clock.now, minutes=15, max_minutes=60)
        await h.publish_state(make_state(mower_id, owner.account_id, at=h.clock.now))
        h.clock.advance(10)
        await h.link(mower_id, owner.account_id, connected=False)
        h.clock.advance(31)
        assert await h.services.watcher.sweep_offline() == []
        h.clock.advance(5)
        await h.link(mower_id, owner.account_id, connected=True)
        await h.publish_state(make_state(mower_id, owner.account_id, at=h.clock.now))
        await close_window(h.store, h.clock.now)
        h.clock.advance(100)
        assert await h.services.watcher.sweep_offline() == []
        assert await h.inbox(owner) == []


async def test_maintenance_still_offline_after_the_window_is_reported() -> None:
    async with harness(notifications_disconnect_grace_seconds=30) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner)
        await open_window(h.store, h.clock.now, minutes=15, max_minutes=60)
        await h.publish_state(make_state(mower_id, owner.account_id, at=h.clock.now))
        await h.link(mower_id, owner.account_id, connected=False)
        h.clock.advance(31)
        assert await h.services.watcher.sweep_offline() == []
        h.clock.advance(60)
        await close_window(h.store, h.clock.now)
        h.clock.advance(125)  # past window end + 120 s grace (and the 2 s window cache)
        swept = await h.services.watcher.sweep_offline()
        assert [e.kind.value for e in swept] == ["mower_offline"]
        items = await h.inbox(owner)
    assert [i["kind"] for i in items] == ["mower_offline"]


async def test_maintenance_theft_alarm_and_errors_are_never_held() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner)
        await open_window(h.store, h.clock.now, minutes=15, max_minutes=60)
        await h.publish_state(make_state(mower_id, owner.account_id, at=h.clock.now))
        h.clock.advance(10)
        lifted = make_state(
            mower_id,
            owner.account_id,
            at=h.clock.now,
            activity=MowerActivity.ERROR,
            error_code="lifted",
        )
        events = await h.services.watcher.on_state(lifted)
    assert "mower_lifted" in [e.kind.value for e in events]


async def test_maintenance_open_close_and_bounds() -> None:
    store = MemoryStore()
    assert await store.maintenance_window() is None
    assert await close_window(store, T0) is None
    opened = await open_window(store, T0, minutes=15, max_minutes=60, reason="deploy 0.9.0")
    assert opened.expires_at == T0 + timedelta(minutes=15)
    assert await store.maintenance_window() == opened
    closed = await close_window(store, T0 + seconds(90))
    assert closed is not None
    assert closed.closed_at == T0 + seconds(90)
    assert await close_window(store, T0 + seconds(100)) is None  # already closed
    for bad in (0, 61):
        with pytest.raises(InvalidWindowError):
            await open_window(store, T0, minutes=bad, max_minutes=60)


# --- the CLI -------------------------------------------------------------------------------


async def test_maintenance_cli_commands() -> None:
    store = MemoryStore()

    def clock() -> datetime:
        return T0

    async def run(action: str, minutes: int = 15) -> tuple[int, str]:
        return await cli.run_maintenance(
            store, action, minutes=minutes, reason="deploy", max_minutes=60, clock=clock
        )

    assert await run("status") == (0, "maintenance: never opened")
    assert await run("close") == (0, "maintenance: no window was open")
    code, line = await run("open")
    assert code == 0
    assert line.startswith("maintenance: open (opened 2026-10-09T12:00:00+00:00, until ")
    assert (await run("status"))[1].startswith("maintenance: open")
    code, line = await run("open", minutes=500)
    assert code == 2
    assert "between 1 and 60" in line
    code, line = await run("close")
    assert code == 0
    assert line.startswith("maintenance: closed")


def test_maintenance_cli_requires_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MOWDECK_DATABASE_URL", "MOWDECK_DATABASE_URL_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["maintenance", "status"]) == 2
