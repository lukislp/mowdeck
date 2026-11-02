"""Fills and empties demo sandboxes in the other services over HTTP (ADR 0076).

The :class:`~identity.application.ports.SandboxProvisioner` of a demo installation. It talks to
the services inside the Compose network only (``http://<service>:8000/api/v1/<service>``), as
the sandbox's user (a provisioning access token) and, where the demo policy refuses visitors
(registering mowers, the history, the purges), with the demo seed secret, which the gateway
strips from every outside request.

The garden template is the one of the shared demo's seed job (``infra/compose/demo/seed.py``);
a test keeps both equal.
"""

import math
from typing import Any
from uuid import UUID

import httpx
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.demo import DEMO_SEED_HEADER

_log = get_logger("identity.demo_sandbox")

ORIGIN = (48.137154, 11.576124)
"""Where the demo neighbourhood lies (the simulator's default origin)."""

MOWERS: list[dict[str, Any]] = [
    {
        "name": "Clover",
        "connection_options": {"garden": "family", "bridge": True},
        "offset_m": (0.0, 0.0),
        "schedule": {"days_of_week": ["mon", "wed", "fri"], "start_time": "09:00:00"},
    },
    {
        "name": "Juniper",
        "connection_options": {"garden": "orchard", "bridge": True, "battery_percent": 64.0},
        "offset_m": (70.0, 25.0),
        "schedule": {"days_of_week": ["tue", "sat"], "start_time": "08:30:00"},
    },
    {
        "name": "Pebble",
        "connection_options": {
            "garden": "default",
            "firmware_available": "sim-1.1.0",
            "firmware_release_notes": "Smoother turns at the lane ends and a quieter dock.",
        },
        "offset_m": (-55.0, 40.0),
        "schedule": {"days_of_week": ["thu", "sun"], "start_time": "10:00:00"},
    },
]
"""The sandbox's mowers: name, simulator options, where the garden lies relative to ``ORIGIN``."""

PREFERENCE_FIELDS = (
    "enabled_kinds",
    "all_mowers",
    "mower_ids",
    "quiet_hours",
    "timezone",
    "language",
    "channels",
)
"""The fields of ``PUT /notifications/preferences`` (``GET`` answers them plus read-only ones)."""

PURGE_ORDER = (
    "fleet",
    "scheduler",
    "telemetry",
    "notifications",
    "vision",
    "planner",
    "entitlements",
)
"""Fleet first: removing the mowers stops their simulation in the Device-Gateway."""

DEFAULT_URL_TEMPLATE = "http://{service}:8000/api/v1/{service}"


def garden_origin(offset_m: tuple[float, float]) -> tuple[float, float]:
    """Latitude and longitude ``offset_m`` (east, north) metres from :data:`ORIGIN`."""
    east, north = offset_m
    lat = ORIGIN[0] + north / 111_320.0
    lon = ORIGIN[1] + east / (111_320.0 * math.cos(math.radians(ORIGIN[0])))
    return round(lat, 7), round(lon, 7)


class ProvisioningFailed(Exception):  # noqa: N818 - reads like what happened
    """A service refused or did not answer while a sandbox was filled or emptied."""


class HttpSandboxProvisioner:
    """The sandbox provisioner on the services' HTTP APIs."""

    def __init__(
        self,
        *,
        seed_token: str,
        url_template: str = DEFAULT_URL_TEMPLATE,
        history_days: int = 7,
        timeout_seconds: float = 20.0,
        history_timeout_seconds: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not seed_token:
            msg = "demo sandboxes need the demo seed secret (MOWDECK_DEMO_SEED_TOKEN)"
            raise ValueError(msg)
        self._seed_token = seed_token
        self._template = url_template
        self._history_days = history_days
        self._timeout = timeout_seconds
        self._history_timeout = history_timeout_seconds
        self._transport = transport

    @property
    def services(self) -> tuple[str, ...]:
        """The services that hold data of a sandbox, in purge order."""
        return PURGE_ORDER

    async def ready(self) -> bool:
        """Every service a sandbox needs answers ``/readyz`` (the pool waits for it at start)."""
        async with httpx.AsyncClient(timeout=3.0, transport=self._transport) as client:
            for service in PURGE_ORDER:
                url = self._url(service, "").split("/api/")[0] + "/readyz"
                try:
                    response = await client.get(url)
                except httpx.HTTPError:
                    return False
                if response.status_code != 200:
                    return False
        return True

    def _url(self, service: str, path: str) -> str:
        return self._template.format(service=service) + path

    async def provision(self, account_id: UUID, access_token: str, *, history: bool) -> None:
        """Mowers, schedules, notification preferences and (``history``) the past days."""
        bearer = {"Authorization": f"Bearer {access_token}"}
        seeded = {**bearer, DEMO_SEED_HEADER: self._seed_token}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            created: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
            for spec in MOWERS:
                lat, lon = garden_origin(spec["offset_m"])
                options = {**spec["connection_options"], "origin_lat": lat, "origin_lon": lon}
                mower = await self._call(
                    client,
                    "POST",
                    self._url("fleet", "/mowers"),
                    seeded,
                    {
                        "name": spec["name"],
                        "connection_kind": "simulated",
                        "connection_options": options,
                    },
                )
                created.append((spec, str(mower["id"]), options))
            for spec, mower_id, _ in created:
                await self._call(
                    client,
                    "POST",
                    self._url("scheduler", "/schedules"),
                    bearer,
                    {
                        "name": f"{spec['name']}'s routine",
                        "target_type": "mower",
                        "target_id": mower_id,
                        "kind": "weekly",
                        "duration_minutes": 180,
                        "timezone": "Europe/Berlin",
                        "rotation_step_deg": 45,
                        **spec["schedule"],
                    },
                )
            # Notifications fills the inbox only for users who saved preferences (ADR 0068).
            url = self._url("notifications", "/preferences")
            current = await self._call(client, "GET", url, bearer)
            if not current.get("stored"):
                await self._call(
                    client, "PUT", url, bearer, {key: current[key] for key in PREFERENCE_FIELDS}
                )
            if history and self._history_days > 0:
                await self._call(
                    client,
                    "POST",
                    self._url("telemetry", "/demo/history"),
                    seeded,
                    {
                        "mowers": [
                            {
                                "mower_id": mower_id,
                                "account_id": str(account_id),
                                "connection_options": options,
                            }
                            for _, mower_id, options in created
                        ],
                        "days": self._history_days,
                    },
                    timeout=self._history_timeout,
                )

    async def purge(self, service: str, account_id: UUID) -> None:
        """``DELETE /api/v1/<service>/demo/accounts/<id>`` with the seed secret."""
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            answer = await self._call(
                client,
                "DELETE",
                self._url(service, f"/demo/accounts/{account_id}"),
                {DEMO_SEED_HEADER: self._seed_token},
            )
        _log.debug("sandbox purged in a service", service=service, deleted=answer.get("deleted"))

    @staticmethod
    async def _call(
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        body: Any = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - per call (the history takes long)
    ) -> dict[str, Any]:
        try:
            response = await client.request(
                method,
                url,
                headers={"Accept": "application/json", **headers},
                json=body,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as exc:
            msg = f"{method} {url}: {type(exc).__name__}"
            raise ProvisioningFailed(msg) from exc
        if response.status_code >= 400:
            msg = f"{method} {url}: {response.status_code} {response.text[:300]}"
            raise ProvisioningFailed(msg)
        if not response.content:
            return {}
        payload = response.json()
        return payload if isinstance(payload, dict) else {"items": payload}
