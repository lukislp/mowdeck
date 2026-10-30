"""One session per account and the login guard that keeps logins rare (ban risk, ADR 0066)."""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from mowdeck_control import CloudLoginRefused, CloudUnavailable
from mowdeck_control_worx import pool as pool_module
from mowdeck_control_worx.fake_cloud import FAKE_CLOUD_PASSWORD, FakeWorxCloud
from mowdeck_control_worx.pool import (
    MIN_LOGIN_INTERVAL_S,
    RATE_LIMIT_HOLD_S,
    REFUSED_HOLD_S,
    UNAVAILABLE_HOLD_S,
    AccountPool,
    LoginGuard,
    PoolClosedError,
    account_of,
    same_password,
)
from mowdeck_control_worx.session import LoginRateLimited, WorxLogin, WorxSession

GOOD = WorxLogin("worx", "garden@fake.mowdeck.test", FAKE_CLOUD_PASSWORD)
WRONG = WorxLogin("worx", "garden@fake.mowdeck.test", "wrong")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _pool(cloud: FakeWorxCloud, clock: Clock, **kwargs: float) -> AccountPool:
    return AccountPool(cloud.opener(), guard=LoginGuard(clock=clock), **kwargs)


async def test_the_discovery_and_the_mowers_share_one_login() -> None:
    cloud, clock = FakeWorxCloud(), Clock()
    pool = _pool(cloud, clock, linger_seconds=60)
    first = await pool.acquire(GOOD)
    assert not first.reused
    await first.release()  # lingers
    second = await pool.acquire(
        WorxLogin("worx", " Garden@FAKE.mowdeck.test ", FAKE_CLOUD_PASSWORD)
    )
    third = await pool.acquire(GOOD)
    assert second.reused and third.reused and second.session is first.session
    assert pool.logins == 1 and cloud.logins == [("worx", "garden@fake.mowdeck.test")]
    assert pool.users(GOOD) == 2
    await second.release()
    await third.release()
    await pool.close()
    assert not first.session.alive
    with pytest.raises(PoolClosedError):
        await pool.acquire(GOOD)


async def test_concurrent_acquires_wait_for_one_login() -> None:
    cloud, clock = FakeWorxCloud(), Clock()
    pool = _pool(cloud, clock, linger_seconds=0)
    leases = await asyncio.gather(*(pool.acquire(GOOD) for _ in range(5)))
    assert pool.logins == 1 and len({id(lease.session) for lease in leases}) == 1
    for lease in leases:
        await lease.release()
    assert pool.open_sessions == 0


async def test_brands_are_separate_accounts() -> None:
    cloud, clock = FakeWorxCloud(), Clock()
    pool = _pool(cloud, clock)
    worx = await pool.acquire(GOOD)
    kress = await pool.acquire(WorxLogin("kress", GOOD.email, GOOD.password))
    assert worx.session is not kress.session and pool.logins == 2
    await pool.close()


async def test_a_refused_login_is_held_but_a_corrected_password_goes_out() -> None:
    cloud, clock = FakeWorxCloud(), Clock()
    pool = _pool(cloud, clock)
    with pytest.raises(CloudLoginRefused):
        await pool.acquire(WRONG)
    with pytest.raises(CloudLoginRefused, match="not tried again for 15 min"):
        await pool.acquire(WRONG)
    assert len(cloud.logins) == 1  # the second refusal never reached the cloud
    lease = await pool.acquire(GOOD)  # at once: refusals do not start the login interval
    assert len(cloud.logins) == 2
    await lease.release()
    clock.now += REFUSED_HOLD_S + 1
    with pytest.raises(CloudLoginRefused):
        await pool.acquire(WRONG)
    assert len(cloud.logins) == 3
    await pool.close()


async def test_a_rate_limited_account_waits_an_hour_for_every_password() -> None:
    cloud, clock = FakeWorxCloud(), Clock()
    pool = _pool(cloud, clock)
    limited = WorxLogin("worx", "limited@fake.mowdeck.test", FAKE_CLOUD_PASSWORD)
    with pytest.raises(LoginRateLimited):
        await pool.acquire(limited)
    with pytest.raises(CloudUnavailable, match="waits 60 min"):
        await pool.acquire(WorxLogin("worx", limited.email, "another"))
    assert len(cloud.logins) == 1
    clock.now += RATE_LIMIT_HOLD_S + 1
    with pytest.raises(LoginRateLimited):
        await pool.acquire(limited)
    assert len(cloud.logins) == 2


async def test_a_failing_cloud_is_held_briefly() -> None:
    calls: list[WorxLogin] = []

    async def broken(login: WorxLogin) -> WorxSession:
        calls.append(login)
        raise RuntimeError("boom")

    clock = Clock()
    pool = AccountPool(broken, guard=LoginGuard(clock=clock))
    with pytest.raises(CloudUnavailable, match="did not answer") as caught:
        await pool.acquire(GOOD)
    assert FAKE_CLOUD_PASSWORD not in str(caught.value)
    with pytest.raises(CloudUnavailable, match="waits 2 min"):
        await pool.acquire(GOOD)
    clock.now += UNAVAILABLE_HOLD_S + 1
    with pytest.raises(CloudUnavailable):
        await pool.acquire(GOOD)
    assert len(calls) == 2


async def test_a_dead_session_is_not_replaced_at_once() -> None:
    cloud, clock = FakeWorxCloud(), Clock()
    pool = _pool(cloud, clock)
    lease = await pool.acquire(GOOD)
    await lease.session.stop()  # e.g. the library gave up
    with pytest.raises(CloudUnavailable, match="logged in a moment ago"):
        await pool.acquire(GOOD)
    clock.now += MIN_LOGIN_INTERVAL_S
    fresh = await pool.acquire(GOOD)
    assert fresh.session is not lease.session and fresh.session.alive
    assert pool.logins == 2 and len(cloud.logins) == 2  # the held attempt never went out
    await pool.close()


async def test_a_new_password_stops_the_idle_session_of_the_old_one() -> None:
    cloud, clock = FakeWorxCloud(), Clock()
    pool = _pool(cloud, clock, linger_seconds=60)
    old = await pool.acquire(GOOD)
    await old.release()
    clock.now += MIN_LOGIN_INTERVAL_S
    changed = WorxLogin("worx", GOOD.email, FAKE_CLOUD_PASSWORD + "!")
    with pytest.raises(CloudLoginRefused):
        await pool.acquire(changed)
    assert not old.session.alive and pool.open_sessions == 0
    await pool.close()


def test_sessions_are_keyed_by_account_and_nothing_is_derived_from_the_password() -> None:
    assert account_of(GOOD) == account_of(WRONG) == "worx:garden@fake.mowdeck.test"
    assert account_of(WorxLogin("kress", GOOD.email, GOOD.password)) != account_of(GOOD)
    assert same_password(GOOD.password, FAKE_CLOUD_PASSWORD)
    assert not same_password(GOOD.password, WRONG.password)
    assert FAKE_CLOUD_PASSWORD not in repr(GOOD) and FAKE_CLOUD_PASSWORD not in account_of(GOOD)
    source = (Path(pool_module.__file__)).read_text("utf-8")
    assert "hashlib" not in source and "sha256" not in source.lower()
    guard = LoginGuard()
    guard.failed(WRONG, CloudLoginRefused("refused"))
    assert "wrong" not in repr(guard)


async def test_lingering_sessions_stop_after_the_linger_time() -> None:
    cloud, clock = FakeWorxCloud(), Clock()
    pool = _pool(cloud, clock, linger_seconds=0.01)
    lease = await pool.acquire(GOOD)
    await lease.release()
    stopped: Callable[[], bool] = lambda: not lease.session.alive  # noqa: E731
    for _ in range(100):
        if stopped():
            break
        await asyncio.sleep(0.01)
    assert stopped() and pool.open_sessions == 0
