"""Device discovery of a linked vendor cloud account (ADR 0034, ADR 0065).

Fleet asks on ``gateway.cloud.discover`` when a user links a vendor account or re-reads it, and
on ``gateway.cloud.providers`` which providers are enabled. The gateway logs in through the
provider's plugin (which may reuse the session its connected mowers of that account share, so
they are not logged out) and answers the device list. Credentials are never logged; the log line
names the provider, the MowDeck account and the linked account only.
"""

import asyncio
from collections.abc import Callable

from mowdeck_contracts.v1 import (
    CloudDiscoverError,
    CloudDiscoverErrorCode,
    CloudDiscoverReply,
    CloudDiscoverRequest,
    CloudProvidersReply,
    ProviderDescriptor,
)
from mowdeck_service_kit import get_logger

from device_gateway.application.ports import (
    CloudAccounts,
    CloudLoginError,
    CloudUnavailableError,
)

_log = get_logger(__name__)


def request_credentials(
    request: CloudDiscoverRequest, descriptor: ProviderDescriptor
) -> dict[str, str]:
    """The credentials of ``request``; a sender of ADR 0034 (``email`` and ``password``, no
    ``credentials``) fills the provider's identity field and its only secret field."""
    if request.credentials:
        return dict(request.credentials)
    secrets = sorted(descriptor.secret_keys)
    if request.email is None or request.password is None or len(secrets) != 1:
        return {}
    return {descriptor.identity_field.key: request.email, secrets[0]: request.password}


class CloudDiscoveryService:
    """Answers :class:`CloudDiscoverRequest` with the devices of the account or an error."""

    def __init__(
        self,
        accounts: CloudAccounts,
        *,
        timeout_seconds: float,
        count: Callable[[str], None] | None = None,
        concurrency: int = 2,
    ) -> None:
        self._accounts = accounts
        self._timeout_seconds = timeout_seconds
        self._count = count
        self._slots = asyncio.Semaphore(max(1, concurrency))

    def providers(self) -> CloudProvidersReply:
        """The enabled providers with their credential fields."""
        return CloudProvidersReply(providers=self._accounts.descriptors())

    async def discover(self, request: CloudDiscoverRequest) -> CloudDiscoverReply:
        """The reply for ``request`` (never raises for a failed login or cloud)."""
        reply = await self._answer(request)
        outcome = "listed" if reply.error is None else reply.error.code.value
        if self._count is not None:
            self._count(outcome)
        _log.info(
            "cloud discovery answered",
            provider=request.provider,
            account_id=str(request.account_id),
            linked_account_id=str(request.linked_account_id) if request.linked_account_id else None,
            outcome=outcome,
            devices=len(reply.devices or []),
            reused_session=reply.reused_session,
        )
        return reply

    async def _answer(self, request: CloudDiscoverRequest) -> CloudDiscoverReply:
        descriptor = self._accounts.descriptor(request.provider)
        if descriptor is None:
            return _error(
                CloudDiscoverErrorCode.UNSUPPORTED,
                f"the provider {request.provider} is not installed or not enabled",
            )
        credentials = descriptor.normalized(request_credentials(request, descriptor))
        if descriptor.problems(credentials):
            # Fleet checks the fields first; a mismatch means another version of the
            # descriptor. Nothing of the values is echoed.
            return _error(
                CloudDiscoverErrorCode.LOGIN_FAILED,
                "the credentials do not fit the provider's fields",
            )
        try:
            # The wait for a free slot counts against the timeout: at most ``concurrency``
            # vendor logins run at once on this instance.
            async with asyncio.timeout(self._timeout_seconds), self._slots:
                found = await self._accounts.discover(request.provider, credentials)
        except CloudLoginError:
            return _error(CloudDiscoverErrorCode.LOGIN_FAILED, "the cloud refused the credentials")
        except CloudUnavailableError as exc:
            return _error(CloudDiscoverErrorCode.UNAVAILABLE, str(exc)[:500])
        except TimeoutError:
            return _error(
                CloudDiscoverErrorCode.UNAVAILABLE,
                f"the cloud did not answer within {self._timeout_seconds:.0f} s",
            )
        return CloudDiscoverReply(devices=found.devices, reused_session=found.reused_session)


def _error(code: CloudDiscoverErrorCode, detail: str) -> CloudDiscoverReply:
    return CloudDiscoverReply(error=CloudDiscoverError(code=code, detail=detail))
