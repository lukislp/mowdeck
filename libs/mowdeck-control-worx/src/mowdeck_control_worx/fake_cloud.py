"""A fake Worx cloud for development and the browser tests (``ProviderContext.fake``).

The fake implements the adapter's session port (:class:`~mowdeck_control_worx.session.\
WorxSession`) on product items shaped like the cloud's (``GET /product-items`` with
``last_status.payload``), so the snapshot reader, the mapping, the pool and the connection run
unchanged against it; only pyworxcloud itself is left out.

Credentials: every e-mail address at :data:`FAKE_CLOUD_DOMAIN` (``garden@fake.mowdeck.test``)
exists at every brand and takes :data:`FAKE_CLOUD_PASSWORD`; anything else is refused like a
wrong password. An address starting with :data:`FAKE_RATE_LIMITED_PREFIX` is answered with HTTP
429 (to see the login guard). Every fake account has two mowers of its own:

* ``Wanda`` (:data:`FAKE_MOWER_ONLINE`), a wire-guided mower, online and docked, with a GPS
  module, the cutting height module, two zones, the one-time schedule (edge cut) and a rain
  delay of 60 minutes;
* ``Kurt`` (:data:`FAKE_MOWER_OFFLINE`), offline, without modules.

Commands change ``Wanda`` and notify the listeners like an MQTT push: a start mows at once
(beginning at the zone set before), a pause pauses, home drives home and arrives after
:data:`HOME_SECONDS`, the edge cut cuts the edge, the rain delay and the cutting height change
the configuration. ``Kurt`` refuses every command as offline.
"""

import asyncio
import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from mowdeck_control import CloudLoginRefused

from mowdeck_control_worx.session import (
    ActionKind,
    ChangeListener,
    CloudCommandError,
    CommandUnsupportedError,
    LoginRateLimited,
    MowerOfflineError,
    SessionOpener,
    WorxAction,
    WorxLogin,
    WorxSession,
)
from mowdeck_control_worx.snapshot import ZONE_SLOTS, WorxMower

FAKE_CLOUD_DOMAIN: Final = "fake.mowdeck.test"
FAKE_CLOUD_PASSWORD: Final = "fake-cloud-password"  # noqa: S105 - a public test value
FAKE_RATE_LIMITED_PREFIX: Final = "limited"
"""Accounts whose e-mail address starts with this are answered with HTTP 429."""

FAKE_MOWER_ONLINE: Final = "20231000000000000001"
FAKE_MOWER_OFFLINE: Final = "20231000000000000002"
FAKE_POSITION: Final = (52.52045, 13.40495)
"""Where the online fake mower's GPS module puts it (latitude, longitude)."""
HOME_SECONDS: Final = 2.0
"""How long the online fake mower takes to drive home."""


def product_item(
    serial: str,
    *,
    name: str,
    model: str,
    online: bool,
    full: bool,
    battery: int = 100,
) -> dict[str, Any]:
    """One product item as ``GET /product-items`` lists it, with its last MQTT report."""
    dat: dict[str, Any] = {
        "mac": "AABBCCDDEE01" if full else "AABBCCDDEE02",
        "fw": 3.31,
        "ls": 1,
        "le": 0,
        "lz": 0,
        "rsi": -58,
        "lk": 0,
        "bt": {"t": 21.5, "v": 20.1, "p": battery, "nr": 312, "c": 0, "m": 1},
        "dmp": [0.4, -1.2, 181.0],
        "st": {"b": 21840, "d": 523456, "wt": 33120},
        "rain": {"s": 0, "cnt": 0},
    }
    cfg: dict[str, Any] = {
        "id": 0,
        "lg": "en",
        "tm": "08:12:34",
        "dt": "29/10/2026",
        "sc": {"m": 1, "p": 0, "d": [["10:00", 240, 0]] * 7},
        "cmd": 0,
        "mz": [0, 0, 0, 0],
        "mzv": [0] * 10,
        "rd": 0,
    }
    if full:
        dat["modules"] = {
            "4G": {"gps": {"coo": list(FAKE_POSITION)}, "stat": "ok"},
            "EA": {"stat": "ok"},
        }
        cfg.update(
            mz=[12, 35, 0, 0],
            mzv=[0, 0, 0, 1, 1, 0, 0, 0, 1, 0],
            rd=60,
            modules={"EA": {"h": 45}},
        )
        cfg["sc"] = {**cfg["sc"], "ots": {"bc": 0, "wtm": 0}}
    return {
        "id": 1 if full else 2,
        "serial_number": serial,
        "name": name,
        "model": {"code": "WR155E", "friendly_name": model},
        "online": online,
        "protocol": 0,
        "mac_address": dat["mac"],
        "capabilities": ["border_cut"] if full else [],
        "mqtt_topics": {"command_in": f"PRM100/{serial}/commandIn", "command_out": "x"},
        "last_status": {"timestamp": "2026-10-29 08:12:34", "payload": {"cfg": cfg, "dat": dat}},
    }


def fake_items() -> dict[str, dict[str, Any]]:
    """The two product items of a fake account (fresh copies)."""
    return {
        FAKE_MOWER_ONLINE: product_item(
            FAKE_MOWER_ONLINE, name="Wanda", model="Landroid L 2000", online=True, full=True
        ),
        FAKE_MOWER_OFFLINE: product_item(
            FAKE_MOWER_OFFLINE,
            name="Kurt",
            model="Landroid M 500",
            online=False,
            full=False,
            battery=64,
        ),
    }


def is_fake_account(email: str) -> bool:
    """True for an e-mail address of the fake cloud."""
    local, _, domain = email.strip().casefold().rpartition("@")
    return bool(local) and domain == FAKE_CLOUD_DOMAIN


@dataclass(slots=True)
class FakeAccount:
    """The product items of one fake account and its open sessions."""

    items: dict[str, dict[str, Any]] = field(default_factory=fake_items)
    sessions: list["FakeSession"] = field(default_factory=list)


class FakeWorxCloud:
    """The fake vendor: accounts, product items, sessions (see the module docstring)."""

    def __init__(self, *, home_seconds: float = HOME_SECONDS) -> None:
        self.home_seconds = home_seconds
        self.accounts: dict[tuple[str, str], FakeAccount] = {}
        self.logins: list[tuple[str, str]] = []
        """Every login as ``(brand, e-mail)`` (no password)."""
        self.actions: list[tuple[str, WorxAction]] = []
        """Every action as ``(serial number, action)``."""

    def account(self, brand: str, email: str) -> FakeAccount:
        """The state of an account (created on first use)."""
        return self.accounts.setdefault((brand, email.strip().casefold()), FakeAccount())

    def opener(self) -> SessionOpener:
        """The session opener of a pool on this cloud."""

        async def open_fake(login: WorxLogin) -> WorxSession:
            return self.login(login)

        return open_fake

    def login(self, login: WorxLogin) -> "FakeSession":
        """Log in like the cloud: refuse wrong credentials, answer 429 for the limited ones."""
        self.logins.append((login.brand, login.email))
        if not is_fake_account(login.email) or login.password != FAKE_CLOUD_PASSWORD:
            raise CloudLoginRefused("the fake cloud refused the e-mail address or password")
        if login.email.strip().casefold().startswith(FAKE_RATE_LIMITED_PREFIX):
            raise LoginRateLimited("the fake cloud refused the login: too many logins")
        account = self.account(login.brand, login.email)
        session = FakeSession(self, account)
        account.sessions = [s for s in account.sessions if s.alive] + [session]
        return session

    def update(self, brand: str, email: str, serial: str, **dat: Any) -> None:
        """Tests and demos: change fields of a mower's ``dat`` (``ls``, ``le``, ...) and push."""
        account = self.account(brand, email)
        account.items[serial]["last_status"]["payload"]["dat"].update(dat)
        self._changed(account, serial)

    def _changed(self, account: FakeAccount, serial: str) -> None:
        for session in account.sessions:
            session.pushed(serial)

    def act(self, account: FakeAccount, serial: str, action: WorxAction) -> None:
        """Carry out ``action`` on the mower, as the cloud and the mower would."""
        self.actions.append((serial, action))
        item = account.items.get(serial)
        if item is None or not item["online"]:
            raise MowerOfflineError("the fake cloud reports the mower offline")
        payload = item["last_status"]["payload"]
        dat, cfg = payload["dat"], payload["cfg"]
        match action.kind:
            case ActionKind.START:
                dat.update(ls=7, le=0)
            case ActionKind.PAUSE:
                dat["ls"] = 34
            case ActionKind.HOME:
                if dat.get("ls") != 1:
                    dat["ls"] = 30
                    self._arrive_later(account, serial)
            case ActionKind.EDGE_CUT:
                if "ots" not in cfg.get("sc", {}):
                    raise CommandUnsupportedError("this mower has no edge cut on demand")
                dat["ls"] = 32
            case ActionKind.ZONE:
                zone = action.value
                if zone is None or not 0 <= zone < ZONE_SLOTS or not cfg["mz"][zone]:
                    raise CommandUnsupportedError("this zone is not set up on the mower")
                dat["lz"] = cfg["mzv"].index(zone)
            case ActionKind.RAIN_DELAY:
                cfg["rd"] = action.value
            case ActionKind.CUTTING_HEIGHT:
                if "EA" not in dat.get("modules", {}):
                    raise CommandUnsupportedError("this mower has no cutting height module")
                cfg.setdefault("modules", {})["EA"] = {"h": action.value}
        self._changed(account, serial)

    def _arrive_later(self, account: FakeAccount, serial: str) -> None:
        def arrive() -> None:
            dat = account.items[serial]["last_status"]["payload"]["dat"]
            if dat.get("ls") == 30:
                dat["ls"] = 1
                dat["bt"] = {**dat["bt"], "c": 1}
                self._changed(account, serial)

        asyncio.get_running_loop().call_later(self.home_seconds, arrive)


class FakeSession:
    """:class:`~mowdeck_control_worx.session.WorxSession` of one fake account."""

    def __init__(self, cloud: FakeWorxCloud, account: FakeAccount) -> None:
        self._cloud = cloud
        self._account = account
        self._alive = True
        self._listeners: list[ChangeListener] = []
        now = datetime.now(UTC)
        self._mowers = {s: WorxMower.from_item(i) for s, i in account.items.items()}
        self._changed = dict.fromkeys(self._mowers, now)

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

    def pushed(self, serial: str) -> None:
        """The cloud pushed a new report of ``serial`` (an MQTT message)."""
        if not self._alive:
            return
        item = copy.deepcopy(self._account.items[serial])
        self._mowers = {**self._mowers, serial: WorxMower.from_item(item)}
        self._changed[serial] = datetime.now(UTC)
        for listener in list(self._listeners):
            listener(self._mowers)

    async def send(self, serial_number: str, action: WorxAction) -> None:
        if not self._alive:
            raise CloudCommandError("the fake session is closed")
        self._cloud.act(self._account, serial_number, action)

    async def stop(self) -> None:
        self._alive = False
        self._listeners.clear()
