"""The cloud providers Fleet can link accounts of (ADR 0065).

Fleet knows no vendor: the Device-Gateway loads the provider plugins of the installed adapter
packages and answers ``gateway.cloud.providers`` with the enabled ones and their credential
fields. :class:`ProviderCatalog` asks for them, keeps the answer for a short time (the list
only changes when the gateway restarts with another configuration) and checks a link request
against the provider's descriptor before any vendor login.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta

from mowdeck_contracts.v1 import ProviderDescriptor
from mowdeck_service_kit import get_logger

from fleet.application.ports import Clock, CloudDiscovery
from fleet.domain.errors import FieldProblem, InvalidInput, Unavailable

_log = get_logger("fleet.providers")

PROVIDERS_UNAVAILABLE = "the Device-Gateway did not list the cloud providers"
UNKNOWN_PROVIDER = "this cloud provider is not installed or not enabled"


class ProviderCatalog:
    """The enabled providers, cached for ``ttl``."""

    def __init__(
        self, discovery: CloudDiscovery, clock: Clock, *, ttl: timedelta = timedelta(seconds=30)
    ) -> None:
        self.discovery = discovery
        self.clock = clock
        self.ttl = ttl
        self._cached: tuple[datetime, list[ProviderDescriptor]] | None = None

    async def _load(self) -> list[ProviderDescriptor]:
        try:
            reply = await self.discovery.providers()
        except Exception as exc:
            _log.warning("cloud provider list failed", error=type(exc).__name__)
            raise Unavailable(PROVIDERS_UNAVAILABLE) from exc
        self._cached = (self.clock(), list(reply.providers))
        return list(reply.providers)

    async def all(self) -> list[ProviderDescriptor]:
        """The enabled providers, sorted by name (503 when no gateway answers)."""
        if self._cached is not None and self.clock() - self._cached[0] < self.ttl:
            return list(self._cached[1])
        return await self._load()

    async def find(self, provider: str) -> ProviderDescriptor | None:
        """The descriptor of ``provider``; asks again once when it is not in the cached list
        (a provider enabled since)."""
        found = next((d for d in await self.all() if d.provider == provider), None)
        if found is None and self._cached is not None:
            found = next((d for d in await self._load() if d.provider == provider), None)
        return found

    async def get(self, provider: str) -> ProviderDescriptor:
        """The descriptor of ``provider``; 422 (field ``provider``) when it is not enabled."""
        found = await self.find(provider)
        if found is None:
            problem = FieldProblem("provider", UNKNOWN_PROVIDER, "unknown_provider")
            raise InvalidInput(UNKNOWN_PROVIDER, fields=[problem])
        return found


def checked_credentials(
    descriptor: ProviderDescriptor,
    credentials: Mapping[str, str],
    *,
    only_secrets: bool = False,
) -> dict[str, str]:
    """``credentials`` normalized; 422 with one problem per field (``credentials/<key>``) when
    they do not fit the descriptor. The messages never echo a value."""
    clean = descriptor.normalized(credentials)
    problems = descriptor.problems(clean, only_secrets=only_secrets)
    if problems:
        raise InvalidInput(
            "the credentials do not fit the provider's fields",
            fields=[FieldProblem(f"credentials/{p.key}", p.message, p.code) for p in problems],
        )
    return clean
