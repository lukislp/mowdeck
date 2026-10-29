"""One Automower Connect session per application key: token, websocket and a rare REST refresh.

Husqvarna limits an application key to one request per second and 10,000 requests a month, so
the account lives on the websocket: :meth:`HusqvarnaAccount.start` reads every mower once over
REST (``GET /mowers``) and opens the event websocket; from then on aioautomower's
``AutomowerSession`` applies the pushed events (``mower-event-v2``, ``battery-event-v2``,
``position-event-v2``, ...) to its data and the account hands the new snapshot to its listeners.
REST is used again only

* every ``refresh_s`` seconds (default :data:`DEFAULT_REFRESH_S`, 30 minutes, never below
  aioautomower's own cycle of :data:`MIN_REFRESH_S`, 300 s), to catch anything an event missed,
* after the websocket was opened again (it closes after two hours or on network errors), and
* for commands (``POST /mowers/{id}/actions``).

That is at most about 60 refreshes a day (1,800 a month) per application key plus the commands
and a token a day, well below the quota, whatever the number of mowers on the account.
:meth:`HusqvarnaAccount.maintain_once` runs every :data:`KEEPALIVE_S`: it sends the websocket
keepalive (an empty frame, as the Home Assistant integration does), reopens a closed websocket
with a growing back-off and runs the due refresh.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from aioautomower.exceptions import (
    ApiForbiddenError,
    HusqvarnaAutomowerError,
    HusqvarnaWSClientError,
    HusqvarnaWSServerHandshakeError,
)
from aioautomower.model import MowerAttributes
from aioautomower.session import AutomowerSession
from mowdeck_control import CloudLoginRefused, CloudUnavailable

from mowdeck_control_husqvarna.auth import (
    MIN_REQUEST_INTERVAL_S,
    ClientCredentialsAuth,
    Clock,
    RequestThrottle,
    Sleep,
    TokenSource,
)
from mowdeck_control_husqvarna.transport import HttpTransport

_log = logging.getLogger("mowdeck_control_husqvarna.account")

MIN_REFRESH_S: Final = 300.0
"""aioautomower's ``REST_POLL_CYCLE``: the account never polls faster."""
DEFAULT_REFRESH_S: Final = 1800.0
"""REST refresh while the websocket is up (rate limits: see the module docstring)."""
KEEPALIVE_S: Final = 60.0
"""Websocket keepalive and maintenance tick."""
MAX_RECONNECT_BACKOFF_S: Final = 960.0
"""Longest wait between two attempts to reopen the websocket."""

Listener = Callable[[Mapping[str, MowerAttributes]], None]
UtcClock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class ActionKind(StrEnum):
    """The mower actions MowDeck sends (``POST /mowers/{id}/actions``)."""

    RESUME_SCHEDULE = "resume_schedule"
    PAUSE = "pause"
    PARK_UNTIL_NEXT_SCHEDULE = "park_until_next_schedule"
    PARK_UNTIL_FURTHER_NOTICE = "park_until_further_notice"
    START_FOR = "start_for"
    START_IN_WORK_AREA = "start_in_work_area"


@dataclass(frozen=True, slots=True)
class MowerAction:
    """One action; ``minutes`` for the starts, ``work_area_id`` for a work area start."""

    kind: ActionKind
    minutes: int | None = None
    work_area_id: int | None = None


async def _apply(session: AutomowerSession, mower_id: str, action: MowerAction) -> None:
    commands = session.commands
    match action.kind:
        case ActionKind.RESUME_SCHEDULE:
            await commands.resume_schedule(mower_id)
        case ActionKind.PAUSE:
            await commands.pause_mowing(mower_id)
        case ActionKind.PARK_UNTIL_NEXT_SCHEDULE:
            await commands.park_until_next_schedule(mower_id)
        case ActionKind.PARK_UNTIL_FURTHER_NOTICE:
            await commands.park_until_further_notice(mower_id)
        case ActionKind.START_FOR:
            await commands.start_for(mower_id, timedelta(minutes=action.minutes or 0))
        case ActionKind.START_IN_WORK_AREA:
            assert action.work_area_id is not None  # noqa: S101 - set by the connection
            await commands.start_in_workarea(
                mower_id, action.work_area_id, timedelta(minutes=action.minutes or 0)
            )


class HusqvarnaAccount:
    """The shared session of one application key (see the module docstring)."""

    def __init__(
        self,
        transport: HttpTransport,
        app_key: str,
        app_secret: str,
        *,
        refresh_s: float = DEFAULT_REFRESH_S,
        request_interval_s: float = MIN_REQUEST_INTERVAL_S,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        utcnow: UtcClock = _utcnow,
        keepalive_s: float = KEEPALIVE_S,
    ) -> None:
        self._transport = transport
        self._clock = clock  # the throttle's sleep is ``sleep``; the maintenance loop's is real
        self._utcnow = utcnow
        self.refresh_s = max(MIN_REFRESH_S, refresh_s)
        self.keepalive_s = keepalive_s
        self.tokens = TokenSource(transport, app_key, app_secret, clock=clock)
        self.auth = ClientCredentialsAuth(
            transport,
            self.tokens,
            throttle=RequestThrottle(request_interval_s, clock=clock, sleep=sleep),
        )
        self._session: AutomowerSession | None = None
        self._listeners: list[Listener] = []
        self._snapshot: dict[str, MowerAttributes] = {}
        self._changed_at: dict[str, datetime] = {}
        self._refreshed_at = 0.0
        self._reconnect_at = 0.0
        self._backoff_s = keepalive_s
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._ws_ready_seen = False
        self._refresh_requested = False
        self.refreshes = 0
        """REST refreshes (``GET /mowers``) so far, the first one included."""
        self.heartbeat_at: datetime | None = None
        """When Husqvarna last answered or pushed anything."""

    def __repr__(self) -> str:
        return f"HusqvarnaAccount(mowers={len(self._snapshot)}, stopped={self._stopped})"

    # --- State -------------------------------------------------------------------------------

    @property
    def mowers(self) -> Mapping[str, MowerAttributes]:
        """The latest data of every mower of the account by Husqvarna mower id."""
        return self._snapshot

    def changed_at(self, mower_id: str) -> datetime | None:
        """When the data of ``mower_id`` last changed (REST or websocket)."""
        return self._changed_at.get(mower_id)

    @property
    def alive(self) -> bool:
        """False after :meth:`stop`."""
        return not self._stopped

    @property
    def websocket_open(self) -> bool:
        """True while the event websocket is connected and listened to."""
        session = self._session
        ws = self.auth.ws
        return (
            session is not None
            and ws is not None
            and not ws.closed
            and session.ws_task is not None
            and not session.ws_task.done()
        )

    def listen(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` with every new snapshot; returns the unsubscribe function."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _on_data(self, data: Mapping[str, MowerAttributes]) -> None:
        now = self._utcnow()
        self.heartbeat_at = now
        fresh = dict(data)
        for mower_id, attributes in fresh.items():
            if self._snapshot.get(mower_id) != attributes:
                self._changed_at[mower_id] = now
        self._snapshot = fresh
        for listener in list(self._listeners):
            try:
                listener(fresh)
            except Exception:
                _log.exception("a Husqvarna state listener failed")

    def _on_pong(self, at: datetime) -> None:
        self.heartbeat_at = at

    def _on_ws_ready(self) -> None:
        """Husqvarna confirmed a websocket: after a reopen (by the session itself or by
        :meth:`maintain_once`) events may have been missed, so the next tick refreshes."""
        if self._ws_ready_seen:
            self._refresh_requested = True
        self._ws_ready_seen = True

    # --- Lifecycle -----------------------------------------------------------------------------

    async def start(self) -> None:
        """Log in, read every mower once and open the websocket. Raises
        :class:`~mowdeck_control.CloudLoginRefused` or
        :class:`~mowdeck_control.CloudUnavailable`."""
        session = AutomowerSession(self.auth, poll=False)
        self._session = session
        session.register_data_callback(self._on_data)
        session.register_pong_callback(self._on_pong)
        session.register_ws_ready_callback(self._on_ws_ready)
        try:
            await self.refresh()
        except ApiForbiddenError as exc:
            msg = (
                "the Husqvarna application has no access to the Automower Connect API "
                "(connect it in the Husqvarna Developer Portal)"
            )
            raise CloudLoginRefused(msg) from exc
        except HusqvarnaAutomowerError as exc:
            msg = "the Husqvarna cloud did not answer"
            raise CloudUnavailable(msg) from exc
        if not await self._open_websocket():
            # Not open now: the first "ready" later already follows a gap in the events.
            self._ws_ready_seen = True
        self._task = asyncio.get_running_loop().create_task(self._maintain())

    async def refresh(self) -> None:
        """Read every mower over REST (one request) and notify the listeners."""
        session = self._session
        if session is None:
            msg = "the account is not started"
            raise RuntimeError(msg)
        self._refreshed_at = self._clock()
        self.refreshes += 1
        data = await session.get_status()
        self._on_data(data)

    async def _open_websocket(self) -> bool:
        session = self._session
        if session is None:
            return False
        try:
            await self.auth.websocket_connect()
            await session.start_listening()
        except (HusqvarnaWSClientError, HusqvarnaWSServerHandshakeError) as exc:
            self._reconnect_at = self._clock() + self._backoff_s
            _log.warning(
                "Husqvarna websocket unavailable (%s); next try in %.0f s",
                type(exc).__name__,
                self._backoff_s,
            )
            self._backoff_s = min(self._backoff_s * 2, MAX_RECONNECT_BACKOFF_S)
            return False
        self._backoff_s = self.keepalive_s
        return True

    async def maintain_once(self) -> None:
        """One maintenance tick: keepalive, reopen a closed websocket, refresh when due."""
        session = self._session
        if session is None or self._stopped:
            return
        if self.websocket_open:
            await session.send_empty_message()
        elif self._clock() >= self._reconnect_at:
            await self._open_websocket()
        if self._refresh_requested or self._clock() - self._refreshed_at >= self.refresh_s:
            self._refresh_requested = False
            try:
                await self.refresh()
            except (HusqvarnaAutomowerError, CloudLoginRefused, CloudUnavailable) as exc:
                _log.warning("Husqvarna refresh failed: %s", type(exc).__name__)

    async def _maintain(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self.keepalive_s)
            try:
                await self.maintain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("Husqvarna account maintenance failed")

    async def act(self, mower_id: str, action: MowerAction) -> None:
        """Send ``action`` to ``mower_id``; raises aioautomower's errors (and
        :class:`~mowdeck_control.CloudLoginRefused` / :class:`~mowdeck_control.CloudUnavailable`
        when no token can be had)."""
        session = self._session
        if session is None or self._stopped:
            msg = "the Husqvarna session is closed"
            raise CloudUnavailable(msg)
        await _apply(session, mower_id, action)

    async def stop(self) -> None:
        """Close the websocket and the transport (idempotent)."""
        if self._stopped:
            return
        self._stopped = True
        self._listeners.clear()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        session, self._session = self._session, None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        with contextlib.suppress(Exception):
            await self.auth.websocket_close()
        await self._transport.close()


async def open_account(account: HusqvarnaAccount) -> HusqvarnaAccount:
    """``account`` started; stopped again when the start fails."""
    try:
        await account.start()
    except BaseException:
        await account.stop()
        raise
    return account
