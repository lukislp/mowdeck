"""Test helpers: the recorded-shape fixture, a scripted transport and a manual clock."""

import copy
import json
from collections.abc import Mapping
from datetime import UTC
from pathlib import Path
from typing import Any, Final

from aioautomower.model import MowerAttributes
from aioautomower.utils import mower_list_to_dictionary_dataclass
from mowdeck_control_husqvarna.auth import TOKEN_URL
from mowdeck_control_husqvarna.transport import HttpReply, TransportError, WebSocketLike

FIXTURES: Final = Path(__file__).parent / "fixtures"

HUGO: Final = "5e1c0d2a-7b64-4f0e-9c1d-3a2b1c0d9e8f"
"""Mowing in operation, GPS, work areas."""
LISA_CHARGING: Final = "8f2a6c3e-1d4b-4e5f-a6b7-c8d9e0f1a2b3"
"""Charging outside the schedule, no GPS."""
LISA_LIFTED: Final = "2b7d9e1f-3c5a-4d6e-8f90-a1b2c3d4e5f6"
"""Error 15 (Lifted), GPS."""

APP_KEY: Final = "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"
SECRET: Final = "s3cr3t-application-secret-value"  # noqa: S105 - a test value


def mowers_payload() -> dict[str, Any]:
    """``GET /mowers`` as the fixture records it (a fresh copy)."""
    data: dict[str, Any] = json.loads((FIXTURES / "mowers.json").read_text(encoding="utf-8"))
    return data


def parse(payload: Mapping[str, Any]) -> dict[str, MowerAttributes]:
    """aioautomower's parsing of a ``GET /mowers`` payload (the invalid id ``0-0`` dropped as
    the session does)."""
    data = {"data": [m for m in payload["data"] if m["id"] != "0-0"]}
    return mower_list_to_dictionary_dataclass(data, UTC)  # type: ignore[arg-type]


def attributes(mower_id: str = HUGO, **mower: Any) -> MowerAttributes:
    """One fixture mower, with fields of its ``mower`` block replaced."""
    payload = mowers_payload()
    for item in payload["data"]:
        if item["id"] == mower_id:
            item["attributes"]["mower"].update(mower)
    return parse(payload)[mower_id]


class ManualClock:
    """A monotonic clock and an ``asyncio.sleep`` replacement that only moves when told."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedTransport:
    """An :class:`~mowdeck_control_husqvarna.transport.HttpTransport` with fixed answers."""

    def __init__(
        self,
        *,
        token_status: int = 200,
        mowers: Mapping[str, Any] | None = None,
        status: Mapping[tuple[str, str], int] | None = None,
    ) -> None:
        self.token_status = token_status
        self.mowers = copy.deepcopy(dict(mowers)) if mowers is not None else mowers_payload()
        self.status = dict(status or {})
        self.forms: list[dict[str, str]] = []
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.fail_network = False
        self.closed = False
        self.tokens_issued = 0

    async def post_form(self, url: str, form: Mapping[str, str]) -> HttpReply:
        assert url == TOKEN_URL
        self.forms.append(dict(form))
        if self.fail_network:
            raise TransportError("token request failed: ClientConnectorError")
        if self.token_status != 200:
            return HttpReply(self.token_status, {"error": "invalid_client"})
        self.tokens_issued += 1
        return HttpReply(200, {"access_token": f"token-{self.tokens_issued}", "expires_in": 3600})

    async def request_json(
        self, method: str, url: str, *, headers: Mapping[str, str], json_body: Any = None
    ) -> HttpReply:
        path = url.split("/v1/", 1)[1].rstrip("/")
        self.calls.append((method, path, dict(headers), json_body))
        if self.fail_network:
            raise TransportError(f"{method.upper()} failed: ClientConnectorError")
        status = self.status.get((method, path))
        if status is not None:
            return HttpReply(status, {"errors": [{"status": str(status)}]})
        if method == "get" and path == "mowers":
            return HttpReply(200, copy.deepcopy(self.mowers))
        return HttpReply(202, {"data": {"type": "control", "id": "x"}})

    async def ws_connect(self, url: str, *, headers: Mapping[str, str]) -> WebSocketLike:
        raise TransportError("websocket failed: WSServerHandshakeError")

    async def close(self) -> None:
        self.closed = True
