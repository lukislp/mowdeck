"""A fake Husqvarna cloud for development and the browser tests (``ProviderContext.fake``).

The fake answers the same requests as Husqvarna, through the adapter's transport port: the
token request, ``GET /mowers``, ``POST /mowers/{id}/actions`` and the event websocket. So the
real adapter code (token cache, throttle, aioautomower's session and models, the mapping) runs
unchanged against it.

Credentials: every application key that starts with :data:`FAKE_APP_KEY_PREFIX` (for example
:data:`FAKE_APP_KEY`) exists and takes :data:`FAKE_APP_SECRET`; anything else is refused like a
wrong key at Husqvarna. Every fake application sees two mowers of its own:

* ``Hugo`` (:data:`FAKE_MOWER_ONLINE`), an Automower 430X NERA, online, parked in its charging
  station outside its schedule, with GPS, headlights and two work areas;
* ``Greta`` (:data:`FAKE_MOWER_OFFLINE`), an Automower 305 without GPS, not connected.

Actions change the online mower and push ``mower-event-v2`` and ``planner-event-v2`` events: a
start mows at once, a pause pauses, a resume returns to the schedule, a park drives home and
arrives after :data:`PARK_SECONDS`. The offline mower accepts actions and does nothing, like a
mower that is switched off.
"""

import asyncio
import copy
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from aioautomower.const import API_BASE_URL, WS_URL
from aiohttp import WSMessage, WSMsgType

from mowdeck_control_husqvarna.account import HusqvarnaAccount, open_account
from mowdeck_control_husqvarna.auth import TOKEN_URL
from mowdeck_control_husqvarna.pool import AccountOpener
from mowdeck_control_husqvarna.transport import HttpReply, TransportError, WebSocketLike

FAKE_APP_KEY_PREFIX: Final = "fake-husqvarna-"
"""Every application key with this prefix exists at the fake cloud."""
FAKE_APP_KEY: Final = "fake-husqvarna-app-key"
FAKE_APP_SECRET: Final = "fake-cloud-password"  # noqa: S105 - a public test value
"""The secret every fake application takes."""

FAKE_MOWER_ONLINE: Final = "c0ffee00-4a7d-4c2e-9b1a-000000000001"
FAKE_MOWER_OFFLINE: Final = "c0ffee00-4a7d-4c2e-9b1a-000000000002"
FAKE_POSITION: Final = (52.52045, 13.40495)
"""Where the online fake mower parks (latitude, longitude)."""
FAKE_WORK_AREAS: Final = ((123456, "Front lawn"), (654321, "Back lawn"))
PARK_SECONDS: Final = 2.0
"""How long the fake online mower takes to drive home."""
TOKEN_LIFETIME_S: Final = 86399


def _now_ms() -> int:
    return int(time.time() * 1000)


def mower_item(
    mower_id: str,
    *,
    name: str,
    model: str,
    serial: int,
    connected: bool,
    gps: bool,
    battery: int = 100,
    work_areas: tuple[tuple[int, str], ...] = (),
) -> dict[str, Any]:
    """One item of ``GET /mowers`` (JSON:API, as the Automower Connect API answers)."""
    attributes: dict[str, Any] = {
        "system": {"name": name, "model": model, "serialNumber": serial},
        "battery": {"batteryPercent": battery, "remainingChargingTime": 0},
        "capabilities": {
            "canConfirmError": gps,
            "headlights": gps,
            "position": gps,
            "stayOutZones": False,
            "workAreas": bool(work_areas),
        },
        "mower": {
            "mode": "MAIN_AREA",
            "activity": "PARKED_IN_CS",
            "inactiveReason": "NONE",
            "state": "RESTRICTED",
            "errorCode": 0,
            "errorCodeTimestamp": 0,
            "isErrorConfirmable": False,
        },
        "calendar": {
            "tasks": [
                {
                    "start": 540,
                    "duration": 480,
                    "monday": True,
                    "tuesday": False,
                    "wednesday": True,
                    "thursday": False,
                    "friday": True,
                    "saturday": False,
                    "sunday": False,
                }
            ]
        },
        "planner": {
            "nextStartTimestamp": 0,
            "override": {"action": "NOT_ACTIVE"},
            "restrictedReason": "WEEK_SCHEDULE",
        },
        "metadata": {"connected": connected, "statusTimestamp": _now_ms()},
        "positions": (
            [{"latitude": FAKE_POSITION[0], "longitude": FAKE_POSITION[1]}] if gps else []
        ),
        "settings": {
            "cuttingHeight": 4,
            "headlight": {"mode": "EVENING_ONLY" if gps else None},
        },
        "statistics": {
            "cuttingBladeUsageTime": 123_456,
            "numberOfChargingCycles": 321,
            "numberOfCollisions": 4_321,
            "totalChargingTime": 456_789,
            "totalCuttingTime": 1_234_567,
            "totalDriveDistance": 98_765,
            "totalRunningTime": 1_345_678,
            "totalSearchingTime": 23_456,
        },
    }
    if work_areas:
        attributes["mower"]["workAreaId"] = work_areas[0][0]
        attributes["workAreas"] = [
            {
                "workAreaId": area_id,
                "name": area_name,
                "type": "SYSTEMATIC",
                "cuttingHeight": 50,
                "enabled": True,
                "useGlobalCuttingHeight": True,
            }
            for area_id, area_name in work_areas
        ]
    return {"type": "mower", "id": mower_id, "attributes": attributes}


def fake_mowers() -> dict[str, dict[str, Any]]:
    """The two mowers of a fake application (fresh copies)."""
    return {
        FAKE_MOWER_ONLINE: mower_item(
            FAKE_MOWER_ONLINE,
            name="Hugo",
            model="Automower 430X NERA",
            serial=192_000_001,
            connected=True,
            gps=True,
            work_areas=FAKE_WORK_AREAS,
        ),
        FAKE_MOWER_OFFLINE: mower_item(
            FAKE_MOWER_OFFLINE,
            name="Greta",
            model="Automower 305",
            serial=192_000_002,
            connected=False,
            gps=False,
            battery=64,
        ),
    }


def is_fake_key(app_key: str) -> bool:
    """True for an application key of the fake cloud."""
    return app_key.startswith(FAKE_APP_KEY_PREFIX) and len(app_key) > len(FAKE_APP_KEY_PREFIX)


class FakeWebSocket:
    """The event websocket of the fake cloud (the subset aioautomower uses)."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[WSMessage] = asyncio.Queue()
        self._closed = False
        self.sent: list[str] = []

    @property
    def closed(self) -> bool:
        return self._closed

    def push(self, payload: Mapping[str, Any]) -> None:
        """Deliver one JSON message."""
        if not self._closed:
            self._queue.put_nowait(WSMessage(WSMsgType.TEXT, json.dumps(payload), None))

    async def receive(self, timeout: float | None = None) -> WSMessage:  # noqa: ASYNC109 - aiohttp's signature
        if self._closed and self._queue.empty():
            return WSMessage(WSMsgType.CLOSED, None, None)
        return await asyncio.wait_for(self._queue.get(), timeout)

    async def send_str(self, data: str, compress: int | None = None) -> None:
        del compress
        if self._closed:
            raise TransportError("the fake websocket is closed")
        self.sent.append(data)
        if not data:  # the keepalive: Husqvarna answers with an empty frame
            self._queue.put_nowait(WSMessage(WSMsgType.TEXT, "", None))

    async def close(self, *, code: int = 1000, message: bytes = b"") -> bool:
        del code, message
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(WSMessage(WSMsgType.CLOSE, None, None))
        return True


@dataclass(slots=True)
class FakeApplication:
    """The state of one fake application key: its mowers and open websockets."""

    mowers: dict[str, dict[str, Any]] = field(default_factory=fake_mowers)
    sockets: list[FakeWebSocket] = field(default_factory=list)


class FakeHusqvarnaCloud:
    """The fake vendor: applications, tokens, mowers, websockets (see the module docstring)."""

    def __init__(self, *, park_seconds: float = PARK_SECONDS) -> None:
        self.park_seconds = park_seconds
        self.applications: dict[str, FakeApplication] = {}
        self._tokens: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        """Every request as ``(method, path)``, the token requests as ``("post", "token")``."""
        self.actions: list[tuple[str, Any]] = []
        """Every action body's ``data`` as ``(mower id, data)``."""
        self.refuse_websocket = False
        """Tests: refuse websocket handshakes (the account falls back to REST)."""

    def application(self, app_key: str) -> FakeApplication:
        """The state of ``app_key`` (created on first use)."""
        return self.applications.setdefault(app_key, FakeApplication())

    def transport(self) -> "FakeTransport":
        """A transport into this cloud (one per account session)."""
        return FakeTransport(self)

    def opener(self, **account_options: Any) -> AccountOpener:
        """The account opener of a pool on this cloud."""

        async def open_fake(app_key: str, app_secret: str) -> HusqvarnaAccount:
            account = HusqvarnaAccount(self.transport(), app_key, app_secret, **account_options)
            return await open_account(account)

        return open_fake

    # --- Vendor side -------------------------------------------------------------------------

    def issue_token(self, form: Mapping[str, str]) -> HttpReply:
        self.requests.append(("post", "token"))
        key = form.get("client_id", "")
        good = (
            form.get("grant_type") == "client_credentials"
            and is_fake_key(key)
            and form.get("client_secret") == FAKE_APP_SECRET
        )
        if not good:
            return HttpReply(400, {"error": "invalid_client"})
        token = f"fake-token-{len(self._tokens) + 1}"
        self._tokens[token] = key
        body = {
            "access_token": token,
            "expires_in": TOKEN_LIFETIME_S,
            "token_type": "Bearer",
            "provider": "husqvarna",
            "scope": "iam:read amc:api",
        }
        return HttpReply(200, body)

    def key_of(self, headers: Mapping[str, str]) -> str | None:
        """The application key a request is authorised for, None when it is not."""
        bearer = headers.get("Authorization", "")
        key = self._tokens.get(bearer.removeprefix("Bearer "))
        if key is None or headers.get("X-Api-Key") != key:
            return None
        return key

    def answer(self, method: str, url: str, headers: Mapping[str, str], body: Any) -> HttpReply:
        path = url.removeprefix(API_BASE_URL + "/").rstrip("/")
        self.requests.append((method, path))
        key = self.key_of(headers)
        if key is None:
            return HttpReply(401, {"errors": [{"title": "Unauthorized"}]})
        application = self.application(key)
        parts = path.split("/")
        if method == "get" and parts == ["mowers"]:
            return HttpReply(200, {"data": [copy.deepcopy(m) for m in application.mowers.values()]})
        if method == "post" and len(parts) == 3 and parts[0] == "mowers" and parts[2] == "actions":
            mower = application.mowers.get(parts[1])
            if mower is None:
                return HttpReply(404, {"errors": [{"title": "Not found"}]})
            action = (body or {}).get("data", {})
            self.actions.append((parts[1], copy.deepcopy(action)))
            if not isinstance(action, dict) or not self._act(application, mower, action):
                return HttpReply(400, {"errors": [{"title": "Bad request"}]})
            return HttpReply(202, {"data": {"type": "control", "id": f"fake-{len(self.requests)}"}})
        return HttpReply(404, {"errors": [{"title": "Not found"}]})

    def connect_websocket(self, headers: Mapping[str, str]) -> FakeWebSocket:
        bearer = headers.get("Authorization", "").removeprefix("Bearer ")
        key = self._tokens.get(bearer)
        if key is None or self.refuse_websocket:
            raise TransportError("websocket handshake refused")
        socket = FakeWebSocket()
        application = self.application(key)
        application.sockets = [s for s in application.sockets if not s.closed] + [socket]
        socket.push({"ready": True, "connectionId": f"fake-{len(application.sockets)}"})
        return socket

    def push(self, app_key: str, mower_id: str, event: str, attributes: Mapping[str, Any]) -> None:
        """Send an event of ``mower_id`` to every open websocket of ``app_key``."""
        for socket in self.application(app_key).sockets:
            socket.push({"id": mower_id, "type": event, "attributes": dict(attributes)})

    def update(self, app_key: str, mower_id: str, **mower: Any) -> None:
        """Tests and demos: change fields of a mower's ``mower`` block (``state``,
        ``activity``, ``errorCode``, ...) and push the ``mower-event-v2``."""
        item = self.application(app_key).mowers[mower_id]
        item["attributes"]["mower"].update(mower)
        self._changed(app_key, mower_id, item)

    def _changed(self, app_key: str, mower_id: str, item: dict[str, Any]) -> None:
        attributes = item["attributes"]
        attributes["metadata"]["statusTimestamp"] = _now_ms()
        self.push(app_key, mower_id, "mower-event-v2", {"mower": dict(attributes["mower"])})
        self.push(app_key, mower_id, "planner-event-v2", {"planner": dict(attributes["planner"])})

    def _act(
        self, application: FakeApplication, item: dict[str, Any], action: Mapping[str, Any]
    ) -> bool:
        kind = action.get("type")
        options = action.get("attributes") or {}
        attributes = item["attributes"]
        if not attributes["metadata"]["connected"]:
            return kind in _ACTIONS  # a switched-off mower does nothing
        mower, planner = attributes["mower"], attributes["planner"]
        if kind in ("Start", "StartInWorkArea"):
            if not isinstance(options.get("duration"), int) or options["duration"] < 1:
                return False
            if kind == "StartInWorkArea":
                areas = {a["workAreaId"] for a in attributes.get("workAreas", [])}
                if options.get("workAreaId") not in areas:
                    return False
                mower["workAreaId"] = options["workAreaId"]
            mower.update(activity="MOWING", state="IN_OPERATION")
            planner.update(override={"action": "FORCE_MOW"}, restrictedReason="NONE")
        elif kind == "Pause":
            mower["state"] = "PAUSED"
        elif kind == "ResumeSchedule":
            planner["override"] = {"action": "NOT_ACTIVE"}
            if mower["activity"] == "PARKED_IN_CS":
                mower["state"] = "RESTRICTED"
                planner["restrictedReason"] = "WEEK_SCHEDULE"
            else:
                mower.update(activity="MOWING", state="IN_OPERATION")
                planner["restrictedReason"] = "NONE"
        elif kind in ("ParkUntilNextSchedule", "ParkUntilFurtherNotice"):
            reason = "PARK_OVERRIDE" if kind == "ParkUntilFurtherNotice" else "WEEK_SCHEDULE"
            planner["override"] = {"action": "FORCE_PARK"}
            if mower["activity"] != "PARKED_IN_CS":
                mower.update(activity="GOING_HOME", state="IN_OPERATION")
                self._arrive_later(item, reason)
            else:
                mower["state"] = "RESTRICTED"
                planner["restrictedReason"] = reason
        else:
            return False
        key = next(k for k, a in self.applications.items() if a is application)
        self._changed(key, item["id"], item)
        return True

    def _arrive_later(self, item: dict[str, Any], reason: str) -> None:
        def arrive() -> None:
            attributes = item["attributes"]
            if attributes["mower"]["activity"] != "GOING_HOME":
                return
            attributes["mower"].update(activity="PARKED_IN_CS", state="RESTRICTED")
            attributes["planner"]["restrictedReason"] = reason
            for key, application in self.applications.items():
                if application.mowers.get(item["id"]) is item:
                    self._changed(key, item["id"], item)

        asyncio.get_running_loop().call_later(self.park_seconds, arrive)


_ACTIONS: Final = frozenset(
    {
        "Start",
        "StartInWorkArea",
        "Pause",
        "ResumeSchedule",
        "ParkUntilNextSchedule",
        "ParkUntilFurtherNotice",
    }
)


class FakeTransport:
    """:class:`~mowdeck_control_husqvarna.transport.HttpTransport` into a
    :class:`FakeHusqvarnaCloud`."""

    def __init__(self, cloud: FakeHusqvarnaCloud) -> None:
        self.cloud = cloud
        self.closed = False

    async def post_form(self, url: str, form: Mapping[str, str]) -> HttpReply:
        if url != TOKEN_URL:
            return HttpReply(404)
        return self.cloud.issue_token(form)

    async def request_json(
        self, method: str, url: str, *, headers: Mapping[str, str], json_body: Any = None
    ) -> HttpReply:
        return self.cloud.answer(method.lower(), url, headers, json_body)

    async def ws_connect(self, url: str, *, headers: Mapping[str, str]) -> WebSocketLike:
        if url != WS_URL:
            raise TransportError("unknown websocket")
        return self.cloud.connect_websocket(headers)

    async def close(self) -> None:
        self.closed = True
