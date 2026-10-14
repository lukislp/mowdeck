"""Device discovery of a linked vendor cloud account (ADR 0034).

Fleet asks on ``gateway.cloud.discover`` when a user links a Mammotion account or re-reads it.
The gateway logs in through the adapter (or reuses the session its connected mowers of that
account share, so they are not logged out) and answers the device list. Credentials are never
logged; the log line names the MowDeck account and the linked account only.
"""

import asyncio
from collections.abc import Callable

from mowdeck_contracts.v1 import (
    CloudDiscoverError,
    CloudDiscoverErrorCode,
    CloudDiscoverReply,
    CloudDiscoverRequest,
)
from mowdeck_service_kit import get_logger

from device_gateway.application.ports import (
    CloudAccounts,
    CloudLoginError,
    CloudUnavailableError,
)

_log = get_logger(__name__)


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

    async def discover(self, request: CloudDiscoverRequest) -> CloudDiscoverReply:
        """The reply for ``request`` (never raises for a failed login or cloud)."""
        reply = await self._answer(request)
        outcome = "listed" if reply.error is None else reply.error.code.value
        if self._count is not None:
            self._count(outcome)
        _log.info(
            "cloud discovery answered",
            provider=request.provider.value,
            account_id=str(request.account_id),
            linked_account_id=str(request.linked_account_id) if request.linked_account_id else None,
            outcome=outcome,
            devices=len(reply.devices or []),
            reused_session=reply.reused_session,
        )
        return reply

    async def _answer(self, request: CloudDiscoverRequest) -> CloudDiscoverReply:
        if not self._accounts.supports(request.provider):
            return _error(
                CloudDiscoverErrorCode.UNSUPPORTED,
                f"no adapter for the provider {request.provider.value} is installed",
            )
        try:
            # The wait for a free slot counts against the timeout: at most ``concurrency``
            # vendor logins run at once on this instance.
            async with asyncio.timeout(self._timeout_seconds), self._slots:
                found = await self._accounts.discover(
                    request.provider, request.email, request.password
                )
        except CloudLoginError:
            return _error(
                CloudDiscoverErrorCode.LOGIN_FAILED, "the cloud refused the e-mail or password"
            )
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
