"""Fetches ``/.well-known/mowdeck-module`` from the services in
``MOWDECK_ENTITLEMENTS_MODULE_URLS`` (the ``features`` become the catalogue)."""

import asyncio

import httpx
from mowdeck_contracts.v1 import MODULE_DESCRIPTOR_PATH, ModuleDescriptor
from mowdeck_service_kit import get_logger
from pydantic import ValidationError

from entitlements.application.ports import FetchedDescriptor

_log = get_logger("entitlements.modules")


class HttpModuleSource:
    """``ModuleSource`` port over HTTP, with a few quick retries per URL."""

    def __init__(
        self,
        urls: list[str],
        *,
        attempts: int = 3,
        delay_seconds: float = 1.0,
        timeout: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.urls = [u.rstrip("/") for u in urls if u.strip()]
        self.attempts = max(1, attempts)
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self._transport = transport

    async def _fetch_one(self, client: httpx.AsyncClient, url: str) -> FetchedDescriptor:
        for attempt in range(1, self.attempts + 1):
            try:
                response = await client.get(f"{url}{MODULE_DESCRIPTOR_PATH}")
                response.raise_for_status()
                return FetchedDescriptor(url, ModuleDescriptor.model_validate(response.json()))
            except (httpx.HTTPError, ValueError, ValidationError) as exc:
                _log.info(
                    "module descriptor not available",
                    url=url,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
                if attempt < self.attempts:
                    await asyncio.sleep(self.delay_seconds * attempt)
        return FetchedDescriptor(url, None)

    async def fetch(self) -> list[FetchedDescriptor]:
        """One entry per URL; unreachable modules give ``descriptor=None``."""
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
            return list(await asyncio.gather(*(self._fetch_one(client, url) for url in self.urls)))
