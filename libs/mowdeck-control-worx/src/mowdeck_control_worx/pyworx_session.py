"""The real cloud: a :class:`~mowdeck_control_worx.session.WorxSession` on pyworxcloud 6.4.2.

One :class:`PyworxSession` is one ``WorxCloud`` of pyworxcloud: :meth:`PyworxSession.open`
logs in (``authenticate``: the brand app's OAuth client, password grant), lists the account's
mowers (``connect``: ``GET /product-items``) and subscribes to their MQTT topics over AWS IoT.
From then on the mowers' reports arrive as MQTT pushes; pyworxcloud also refreshes the product
items over REST every 5 to 10 minutes and renews its token, and reconnects MQTT on its own (at
most every 15 minutes). MowDeck adds no polling of its own.

pyworxcloud calls its event handlers on the paho MQTT thread; :meth:`PyworxSession._on_event`
hands every event to the event loop (``call_soon_threadsafe``), where the snapshot is rebuilt
and the listeners run.

Errors: a refused login (HTTP 400, 401, 403) is ``CloudLoginRefused``; HTTP 429 is
:class:`~mowdeck_control_worx.session.LoginRateLimited` (the pool then waits an hour); anything
else ``CloudUnavailable``. No message carries a credential, a token or a vendor payload; the
library's own log redacts the e-mail address.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

from mowdeck_control import CloudLoginRefused, CloudUnavailable
from pyworxcloud import WorxCloud
from pyworxcloud.events import LandroidEvent
from pyworxcloud.exceptions import (
    APIError,
    AuthorizationError,
    ForbiddenError,
    InternalServerError,
    MowerNotFoundError,
    NoConnectionError,
    NoCuttingHeightError,
    NoOneTimeScheduleError,
    OfflineError,
    RequestError,
    ServiceUnavailableError,
    TimeoutException,
    TooManyRequestsError,
    ZoneNoProbability,
    ZoneNotDefined,
)

from mowdeck_control_worx.descriptor import BRANDS, MAX_COMMAND_TIMEOUT_S
from mowdeck_control_worx.session import (
    ActionKind,
    ChangeListener,
    CloudCommandError,
    CommandUnsupportedError,
    LoginRateLimited,
    MowerOfflineError,
    NoAnswerError,
    WorxAction,
    WorxLogin,
)
from mowdeck_control_worx.snapshot import WorxMower

_log = logging.getLogger("mowdeck_control_worx.pyworx_session")

CloudFactory = Callable[..., Any]
"""``WorxCloud`` itself, or a stand-in with its interface (tests)."""

_REFUSED: Final = (AuthorizationError, RequestError, ForbiddenError)
_UNAVAILABLE: Final = (
    NoConnectionError,
    InternalServerError,
    ServiceUnavailableError,
    APIError,
    TimeoutException,
    TimeoutError,
    OSError,
)


def _brand(login: WorxLogin) -> str:
    return BRANDS.get(login.brand, login.brand)


def snapshot_of(cloud: Any) -> dict[str, WorxMower]:
    """The mowers of a ``WorxCloud`` by serial number (``devices`` by name, each with the
    product item it was built from in ``mower``). Items that cannot be read are skipped."""
    mowers: dict[str, WorxMower] = {}
    devices = getattr(cloud, "devices", None) or {}
    for device in list(devices.values()):
        item = getattr(device, "mower", None)
        if not isinstance(item, Mapping):
            continue
        try:
            mower = WorxMower.from_item(item)
        except ValueError:
            continue
        mowers[mower.serial_number] = mower
    return mowers


class PyworxSession:
    """One logged-in account on pyworxcloud (see the module docstring)."""

    def __init__(self, cloud: Any, loop: asyncio.AbstractEventLoop, brand: str) -> None:
        self._cloud = cloud
        self._loop = loop
        self._brand = brand
        self._alive = True
        self._listeners: list[ChangeListener] = []
        self._mowers: dict[str, WorxMower] = {}
        self._changed: dict[str, datetime] = {}
        self._refresh()

    @classmethod
    async def open(cls, login: WorxLogin, *, factory: CloudFactory = WorxCloud) -> "PyworxSession":
        """Log in and connect; raises ``CloudLoginRefused``, ``LoginRateLimited`` or
        ``CloudUnavailable`` (and leaves nothing open)."""
        brand = _brand(login)
        try:
            cloud = factory(
                login.email,
                login.password,
                login.brand,
                command_timeout=MAX_COMMAND_TIMEOUT_S,
                deduplicate_inflight_commands=True,
            )
        except (TypeError, ValueError) as exc:
            raise CloudUnavailable(f"the {brand} cloud cannot be set up") from exc
        try:
            await cloud.authenticate()
        except TooManyRequestsError:
            await _quietly_disconnect(cloud)
            msg = f"the {brand} cloud refused the login: too many logins of this account"
            raise LoginRateLimited(msg) from None
        except _REFUSED:
            await _quietly_disconnect(cloud)
            raise CloudLoginRefused(f"{brand} refused the e-mail address or password") from None
        except _UNAVAILABLE as exc:
            await _quietly_disconnect(cloud)
            _log.warning("login at the %s cloud failed: %s", brand, type(exc).__name__)
            raise CloudUnavailable(f"the {brand} cloud did not answer") from None
        loop = asyncio.get_running_loop()
        session = cls(cloud, loop, brand)
        cloud.set_callback(LandroidEvent.DATA_RECEIVED, session._on_event)
        cloud.set_callback(LandroidEvent.API, session._on_event)
        cloud.set_callback(LandroidEvent.MQTT_RATELIMIT, session._on_rate_limit)
        try:
            await cloud.connect()
        except TooManyRequestsError:
            await session.stop()
            raise LoginRateLimited(f"the {brand} cloud limits this account right now") from None
        except Exception as exc:  # pyworxcloud cleans up and re-raises whatever failed
            await session.stop()
            _log.warning("connecting to the %s cloud failed: %s", brand, type(exc).__name__)
            raise CloudUnavailable(f"the {brand} cloud did not answer") from None
        session._refresh()
        return session

    # --- events (any thread) -----------------------------------------------------------------

    def _on_event(self, **_: Any) -> None:
        if not self._alive:
            return
        try:
            self._loop.call_soon_threadsafe(self._refresh)
        except RuntimeError:  # the loop closed while the MQTT thread still delivered
            self._alive = False

    def _on_rate_limit(self, message: str = "", **_: Any) -> None:
        del message  # the library's text may name the account
        _log.warning("the %s cloud rate limits the MQTT link of an account", self._brand)

    # --- loop side ---------------------------------------------------------------------------

    def _refresh(self) -> None:
        if not self._alive:
            return
        mowers = snapshot_of(self._cloud)
        now = datetime.now(UTC)
        for serial, mower in mowers.items():
            if self._mowers.get(serial) != mower:
                self._changed[serial] = now
        if mowers == self._mowers:
            return
        self._mowers = mowers
        for listener in list(self._listeners):
            try:
                listener(mowers)
            except Exception:
                _log.exception("a Worx listener failed")

    @property
    def alive(self) -> bool:
        return self._alive

    def mowers(self) -> Mapping[str, WorxMower]:
        return self._mowers

    def changed_at(self, serial_number: str) -> datetime | None:
        return self._changed.get(serial_number)

    def listen(self, listener: ChangeListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unlisten() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unlisten

    async def send(self, serial_number: str, action: WorxAction) -> None:
        cloud, value = self._cloud, action.value
        try:
            match action.kind:
                case ActionKind.START:
                    await cloud.start(serial_number)
                case ActionKind.PAUSE:
                    await cloud.pause(serial_number)
                case ActionKind.HOME:
                    await cloud.home(serial_number)
                case ActionKind.EDGE_CUT:
                    await cloud.edgecut(serial_number)
                case ActionKind.ZONE:
                    await cloud.setzone(serial_number, _value(value))
                case ActionKind.RAIN_DELAY:
                    await cloud.raindelay(serial_number, str(_value(value)))
                case ActionKind.CUTTING_HEIGHT:
                    await cloud.set_cutting_height(serial_number, _value(value))
        except OfflineError:
            raise MowerOfflineError(f"the {self._brand} cloud reports the mower offline") from None
        except (ZoneNotDefined, ZoneNoProbability):
            raise CommandUnsupportedError("this zone is not set up on the mower") from None
        except NoCuttingHeightError:
            raise CommandUnsupportedError("this mower has no cutting height module") from None
        except NoOneTimeScheduleError:
            raise CommandUnsupportedError("this mower has no edge cut on demand") from None
        except ValueError:
            raise CommandUnsupportedError("the mower does not take this value") from None
        except (TimeoutException, TimeoutError):
            raise NoAnswerError(f"the mower did not answer {action.kind.value}") from None
        except TooManyRequestsError:
            msg = f"the {self._brand} cloud limits this account right now; try again later"
            raise CloudCommandError(msg) from None
        except NoConnectionError:
            msg = f"the MQTT link to the {self._brand} cloud is down; it reconnects by itself"
            raise CloudCommandError(msg) from None
        except (MowerNotFoundError, *_UNAVAILABLE) as exc:
            _log.warning("a %s command failed: %s", self._brand, type(exc).__name__)
            raise CloudCommandError(f"the {self._brand} cloud did not answer") from None

    async def stop(self) -> None:
        if not self._alive:
            return
        self._alive = False
        self._listeners.clear()
        await _quietly_disconnect(self._cloud)


def _value(value: int | None) -> int:
    if value is None:
        raise ValueError("the action needs a value")
    return value


async def _quietly_disconnect(cloud: Any) -> None:
    try:
        await cloud.disconnect()
    except Exception as exc:
        _log.warning("closing a Worx cloud session failed: %s", type(exc).__name__)
