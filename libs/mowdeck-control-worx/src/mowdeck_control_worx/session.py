"""The port between the adapter and one logged-in Worx account (no vendor SDK).

A :class:`WorxSession` is one login of one app account at one brand's cloud: it holds the
account's mowers (:class:`~mowdeck_control_worx.snapshot.WorxMower`, by serial number), tells
listeners when an MQTT push or the library's REST refresh changed them, and sends commands. Two
implementations exist: :class:`~mowdeck_control_worx.pyworx_session.PyworxSession` on
pyworxcloud (the real cloud) and the fake cloud's session
(:mod:`~mowdeck_control_worx.fake_cloud`, development and tests). Everything above this port
(pool, connection, provider) is the same for both.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from mowdeck_control import CloudUnavailable

from mowdeck_control_worx.snapshot import WorxMower


class ActionKind(StrEnum):
    """What a command asks the mower to do (pyworxcloud's method in brackets)."""

    START = "start"
    """Start (or resume) mowing (``start``)."""
    PAUSE = "pause"
    """Stop blades and wheels where the mower is (``pause``)."""
    HOME = "home"
    """End the job and drive to the charging station (``home``)."""
    EDGE_CUT = "edge_cut"
    """Cut along the boundary once (``edgecut``)."""
    ZONE = "zone"
    """Begin the next start at zone ``value`` (0 to 3, ``setzone``); sent before a start."""
    RAIN_DELAY = "rain_delay"
    """Set the rain delay to ``value`` minutes (``raindelay``)."""
    CUTTING_HEIGHT = "cutting_height"
    """Set the cutting height to ``value`` mm (``set_cutting_height``)."""


@dataclass(frozen=True, slots=True)
class WorxAction:
    """One command for one mower."""

    kind: ActionKind
    value: int | None = None


@dataclass(frozen=True, slots=True)
class WorxLogin:
    """The credentials of one app account; the password stays out of ``repr``."""

    brand: str
    email: str
    password: str = field(repr=False)


class LoginRateLimited(CloudUnavailable):
    """The cloud answered a login with HTTP 429: too many logins of this account."""


class WorxCommandError(Exception):
    """A command did not reach the mower or the mower refused it. The message is for users
    and holds no credential and no vendor payload."""


class MowerOfflineError(WorxCommandError):
    """The cloud reports the mower offline; nothing was sent."""


class CommandUnsupportedError(WorxCommandError):
    """The mower lacks the feature (no zone there, no one-time schedule, ...)."""


class NoAnswerError(WorxCommandError):
    """The command went out but the mower did not answer in time."""


class CloudCommandError(WorxCommandError):
    """The cloud or its MQTT link failed (not connected, rate limited, server error)."""


ChangeListener = Callable[[Mapping[str, WorxMower]], None]


@runtime_checkable
class WorxSession(Protocol):
    """One logged-in account (see the module docstring). Listeners and every method run on
    the event loop; an implementation that receives data on another thread hands it over."""

    @property
    def alive(self) -> bool:
        """False once stopped; a pool opens a new session then."""
        ...

    def mowers(self) -> Mapping[str, WorxMower]:
        """The account's mowers by serial number, as last reported."""
        ...

    def changed_at(self, serial_number: str) -> datetime | None:
        """When the data of this mower last changed (UTC), None before the first report."""
        ...

    def listen(self, listener: ChangeListener) -> Callable[[], None]:
        """Call ``listener`` with :meth:`mowers` after every change; returns the unsubscribe."""
        ...

    async def send(self, serial_number: str, action: WorxAction) -> None:
        """Send ``action``; raises a :class:`WorxCommandError`."""
        ...

    async def stop(self) -> None:
        """Log out: close the MQTT link and the HTTP session (idempotent)."""
        ...


SessionOpener = Callable[[WorxLogin], Awaitable[WorxSession]]
"""``open(login)``: a started session; raises ``CloudLoginRefused``, :class:`LoginRateLimited`
or ``CloudUnavailable``."""
