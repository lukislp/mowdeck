"""MowDeck's public API, called with the caller's own bearer token (ADR 0075).

The assistant reads Identity (``POST /me/permissions``), Fleet (the mower), Telemetry (map,
capabilities, state) and asks Vision for a structured answer, exactly as the web app would:
the same routes, the same token, the same authorization. It holds no credential of its own.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import httpx

from assistant.application.ports import MowerFacts
from assistant.domain.errors import UpstreamUnavailable

API = "/api/v1"


class HttpPlatform:
    """``Platform`` over HTTP with one shared connection pool."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        identity_url: str,
        fleet_url: str,
        telemetry_url: str,
    ) -> None:
        self.client = client
        self.identity = identity_url.rstrip("/")
        self.fleet = fleet_url.rstrip("/")
        self.telemetry = telemetry_url.rstrip("/")

    async def _get(self, token: str, url: str) -> httpx.Response:
        try:
            return await self.client.get(url, headers=_auth(token))
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"{_service(url)} did not answer") from exc

    async def relations(
        self, token: str, mower_id: UUID, relations: Sequence[str]
    ) -> dict[str, bool]:
        checks = [{"relation": r, "object": f"mower:{mower_id}"} for r in relations]
        url = f"{self.identity}{API}/identity/me/permissions"
        try:
            response = await self.client.post(url, json={"checks": checks}, headers=_auth(token))
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable("identity did not answer") from exc
        if response.status_code != 200:
            raise UpstreamUnavailable(f"identity answered {response.status_code}")
        answers = response.json()
        if not isinstance(answers, list) or len(answers) != len(relations):
            raise UpstreamUnavailable("identity answered an unexpected body")
        return {r: a is True for r, a in zip(relations, answers, strict=True)}

    async def mower(self, token: str, mower_id: UUID) -> MowerFacts | None:
        response = await self._get(token, f"{self.fleet}{API}/fleet/mowers/{mower_id}")
        if response.status_code in (403, 404):
            return None
        _ok(response)
        body: dict[str, Any] = response.json()
        names = body.get("area_names") or {}
        return MowerFacts(
            name=str(body.get("name") or ""),
            area_names={str(k): str(v) for k, v in names.items() if isinstance(v, str)},
        )

    async def map_areas(self, token: str, mower_id: UUID) -> list[tuple[str, str | None]]:
        response = await self._get(token, f"{self.telemetry}{API}/telemetry/mowers/{mower_id}/map")
        if response.status_code == 404:
            return []
        _ok(response)
        areas = response.json().get("map", {}).get("areas", [])
        return [
            (str(a["id"]), a.get("name") if isinstance(a.get("name"), str) else None)
            for a in areas
            if isinstance(a, dict) and isinstance(a.get("id"), str)
        ]

    async def connected(self, token: str, mower_id: UUID) -> bool:
        url = f"{self.telemetry}{API}/telemetry/mowers/{mower_id}/capabilities"
        response = await self._get(token, url)
        if response.status_code == 404:
            return False
        _ok(response)
        return True

    async def activity(self, token: str, mower_id: UUID) -> str | None:
        url = f"{self.telemetry}{API}/telemetry/mowers/{mower_id}/state"
        response = await self._get(token, url)
        if response.status_code == 404:
            return None
        _ok(response)
        body = response.json()
        if body.get("stale"):
            return "offline"
        activity = body.get("state", {}).get("activity")
        return activity if isinstance(activity, str) else None


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _service(url: str) -> str:
    return url.split("/api/v1/", 1)[-1].split("/", 1)[0] or "a service"


def _ok(response: httpx.Response) -> None:
    if response.status_code != 200:
        service = _service(str(response.request.url))
        raise UpstreamUnavailable(f"{service} answered {response.status_code}")
