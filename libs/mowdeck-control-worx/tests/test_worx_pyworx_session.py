"""The real session on pyworxcloud, against a stand-in ``WorxCloud`` (no network)."""

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any, ClassVar

import pytest
from mowdeck_control import CloudLoginRefused, CloudUnavailable
from mowdeck_control_worx.pyworx_session import PyworxSession, snapshot_of
from mowdeck_control_worx.session import (
    ActionKind,
    CloudCommandError,
    CommandUnsupportedError,
    LoginRateLimited,
    MowerOfflineError,
    NoAnswerError,
    WorxAction,
    WorxLogin,
)
from pyworxcloud import WorxCloud
from pyworxcloud.events import LandroidEvent
from pyworxcloud.exceptions import (
    AuthorizationError,
    NoConnectionError,
    NoCuttingHeightError,
    OfflineError,
    RequestError,
    ServiceUnavailableError,
    TimeoutException,
    TooManyRequestsError,
    ZoneNotDefined,
)
from worx_support import PLAIN, WIRE, item

LOGIN = WorxLogin("kress", "garden@example.com", "s3cret-password")


class Device:
    def __init__(self, data: dict[str, Any]) -> None:
        self.mower = data


class StandIn:
    """The parts of ``WorxCloud`` the session uses, with chosen failures."""

    def __init__(
        self,
        username: str,
        password: str,
        cloud: str,
        *,
        command_timeout: float,
        deduplicate_inflight_commands: bool,
    ) -> None:
        self.args = (username, password, cloud, command_timeout, deduplicate_inflight_commands)
        self.devices: dict[str, Device] = {}
        self.callbacks: dict[LandroidEvent, Callable[..., Any]] = {}
        self.auth_error: Exception | None = None
        self.connect_error: Exception | None = None
        self.command_error: Exception | None = None
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.disconnected = 0

    async def authenticate(self) -> bool:
        if self.auth_error is not None:
            raise self.auth_error
        return True

    async def connect(self) -> bool:
        if self.connect_error is not None:
            raise self.connect_error
        for serial in (WIRE, PLAIN):
            data = item(serial)
            self.devices[data["name"]] = Device(data)
        return True

    async def disconnect(self) -> None:
        self.disconnected += 1

    def set_callback(self, event: LandroidEvent, func: Callable[..., Any]) -> None:
        self.callbacks[event] = func

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name not in {"start", "pause", "home", "edgecut", "setzone", "raindelay",
                        "set_cutting_height"}:  # fmt: skip
            raise AttributeError(name)

        async def command(*args: Any) -> None:
            self.calls.append((name, args))
            if self.command_error is not None:
                raise self.command_error

        return command


def factory(
    *, auth: Exception | None = None, connect: Exception | None = None
) -> tuple[list[StandIn], Callable[..., StandIn]]:
    made: list[StandIn] = []

    def make(*args: Any, **kwargs: Any) -> StandIn:
        cloud = StandIn(*args, **kwargs)
        cloud.auth_error, cloud.connect_error = auth, connect
        made.append(cloud)
        return cloud

    return made, make


def test_the_default_factory_is_pyworxclouds_class() -> None:
    import inspect

    default = inspect.signature(PyworxSession.open).parameters["factory"].default
    assert default is WorxCloud
    params = inspect.signature(WorxCloud.__init__).parameters
    assert {"command_timeout", "deduplicate_inflight_commands"} <= set(params)
    assert list(params)[1:4] == ["username", "password", "cloud"]


async def test_open_logs_in_lists_the_mowers_and_subscribes() -> None:
    made, make = factory()
    session = await PyworxSession.open(LOGIN, factory=make)
    [cloud] = made
    assert cloud.args == ("garden@example.com", "s3cret-password", "kress", 120.0, True)
    assert set(session.mowers()) == {WIRE, PLAIN} and session.alive
    assert session.changed_at(WIRE) is not None
    assert set(cloud.callbacks) == {
        LandroidEvent.DATA_RECEIVED,
        LandroidEvent.API,
        LandroidEvent.MQTT_RATELIMIT,
    }
    await session.stop()
    await session.stop()
    assert not session.alive and cloud.disconnected == 1


@pytest.mark.parametrize(
    ("error", "expected", "text"),
    [
        (AuthorizationError(), CloudLoginRefused, "Kress refused the e-mail address or password"),
        (RequestError(), CloudLoginRefused, "refused"),
        (TooManyRequestsError(), LoginRateLimited, "too many logins"),
        (NoConnectionError(), CloudUnavailable, "did not answer"),
        (ServiceUnavailableError(), CloudUnavailable, "did not answer"),
    ],
)
async def test_login_errors(
    error: Exception, expected: type[Exception], text: str, caplog: pytest.LogCaptureFixture
) -> None:
    made, make = factory(auth=error)
    with caplog.at_level(logging.DEBUG), pytest.raises(expected, match=text) as caught:
        await PyworxSession.open(LOGIN, factory=make)
    assert made[0].disconnected == 1  # nothing left open
    for secret in (LOGIN.password, LOGIN.email):
        assert secret not in str(caught.value) and secret not in caplog.text


async def test_a_failing_connect_cleans_up() -> None:
    made, make = factory(connect=RuntimeError("payload with garden@example.com"))
    with pytest.raises(CloudUnavailable) as caught:
        await PyworxSession.open(LOGIN, factory=make)
    assert "garden@example.com" not in str(caught.value) and made[0].disconnected == 1
    made, make = factory(connect=TooManyRequestsError())
    with pytest.raises(LoginRateLimited):
        await PyworxSession.open(LOGIN, factory=make)


async def test_mqtt_events_from_another_thread_reach_the_listeners() -> None:
    made, make = factory()
    session = await PyworxSession.open(LOGIN, factory=make)
    cloud = made[0]
    seen: list[int | None] = []
    unlisten = session.listen(lambda mowers: seen.append(mowers[WIRE].status_code))
    cloud.devices["Rasenwerk"].mower["last_status"]["payload"]["dat"]["ls"] = 34

    def push() -> None:  # pyworxcloud calls handlers on the paho thread
        cloud.callbacks[LandroidEvent.DATA_RECEIVED](name="Rasenwerk", device=None)

    thread = threading.Thread(target=push)
    thread.start()
    thread.join()
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen == [34] and session.mowers()[WIRE].status_code == 34
    cloud.callbacks[LandroidEvent.API](api_data={})  # unchanged data: no listener call
    await asyncio.sleep(0.02)
    assert seen == [34]
    cloud.callbacks[LandroidEvent.MQTT_RATELIMIT](message="limit for garden@example.com")
    unlisten()
    await session.stop()


async def test_commands_call_pyworxcloud() -> None:
    made, make = factory()
    session = await PyworxSession.open(LOGIN, factory=make)
    for action in (
        WorxAction(ActionKind.ZONE, 1),
        WorxAction(ActionKind.START),
        WorxAction(ActionKind.PAUSE),
        WorxAction(ActionKind.HOME),
        WorxAction(ActionKind.EDGE_CUT),
        WorxAction(ActionKind.RAIN_DELAY, 90),
        WorxAction(ActionKind.CUTTING_HEIGHT, 50),
    ):
        await session.send(WIRE, action)
    assert made[0].calls == [
        ("setzone", (WIRE, 1)),
        ("start", (WIRE,)),
        ("pause", (WIRE,)),
        ("home", (WIRE,)),
        ("edgecut", (WIRE,)),
        ("raindelay", (WIRE, "90")),
        ("set_cutting_height", (WIRE, 50)),
    ]
    with pytest.raises(CommandUnsupportedError):
        await session.send(WIRE, WorxAction(ActionKind.ZONE))  # no value
    await session.stop()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OfflineError("offline"), MowerOfflineError),
        (ZoneNotDefined("zone"), CommandUnsupportedError),
        (NoCuttingHeightError("no"), CommandUnsupportedError),
        (TimeoutException("late"), NoAnswerError),
        (NoConnectionError(), CloudCommandError),
        (TooManyRequestsError(), CloudCommandError),
        (ServiceUnavailableError(), CloudCommandError),
    ],
)
async def test_command_errors(error: Exception, expected: type[Exception]) -> None:
    made, make = factory()
    session = await PyworxSession.open(LOGIN, factory=make)
    made[0].command_error = error
    with pytest.raises(expected):
        await session.send(WIRE, WorxAction(ActionKind.START))
    await session.stop()


def test_snapshot_skips_unreadable_devices() -> None:
    class Cloud:
        devices: ClassVar[dict[str, object]] = {
            "a": Device(item(WIRE)),
            "b": object(),
            "c": Device({"name": "no serial"}),
        }

    assert list(snapshot_of(Cloud())) == [WIRE]
    assert snapshot_of(object()) == {}
