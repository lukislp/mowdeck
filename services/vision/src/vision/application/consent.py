# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Use cases of the consent to a cloud AI model (ADR 0081).

Two pieces, both built on the pure rules of :mod:`vision.domain.consent`:

* :class:`ConsentGate` is the **one enforcement point**. :meth:`ConsentGate.check` is called by
  :meth:`~vision.application.providers.ProviderService.resolve`, the single place that decides
  which model a request uses, so every path that sends user data to a model (manual assist and
  retry, automatic analysis, structured answers of the chat assistant) refuses without a
  covering consent *before* anything is captured or sent. The provider test sends a synthetic
  image through the provider's own path and never resolves, so it is exempt.
* :class:`ConsentService` is what the API and the opt-in of automatic analyses use: status,
  grant for the destination in effect now, withdraw (which also switches automatic analysis
  off).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit import get_logger

from vision.application.ports import Clock, Store
from vision.domain.consent import (
    Consent,
    Destination,
    consent_missing,
    consent_required,
    covers,
)
from vision.domain.errors import ConsentRequired, InvalidInput, ProviderMissing
from vision.domain.models import AutoSetting

_log = get_logger("vision.consent")


class DestinationSource(Protocol):
    """Where the caller's next request would go (the provider resolution implements it)."""

    async def destination(self, principal: Principal) -> Destination | None:
        """The destination of the caller's effective provider, None without one."""
        ...


@dataclass(frozen=True, slots=True)
class ConsentStatus:
    """What the consent dialog and the settings page need."""

    required: bool
    """The current destination is a cloud model, so consent is needed."""
    destination: Destination | None
    """Where a request would go now (None without a provider)."""
    granted_at: datetime | None
    granted_for: Destination | None
    """The destination of the recorded consent (may differ from ``destination``)."""
    covers: bool
    """The recorded consent covers the current destination."""


def _ids(principal: Principal) -> tuple[UUID, UUID]:
    if principal.account_id is None:
        raise InvalidInput("consent is given by a person in an account")
    return principal.subject, principal.account_id


class ConsentGate:
    """Records consents and refuses requests that lack one."""

    def __init__(self, *, store: Store, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    async def check(self, principal: Principal, destination: Destination) -> None:
        """Raise :class:`ConsentRequired` when ``destination`` is a cloud model without a
        covering consent of the caller."""
        if not consent_required(destination):
            return
        user, account = _ids(principal)
        if consent_missing(await self.store.get_consent(user, account), destination):
            msg = (
                f"this sends data to {destination.provider} ({destination.model}), "
                "a cloud AI provider: agree to it first"
            )
            raise ConsentRequired(msg, destination)

    async def status(self, principal: Principal, destination: Destination | None) -> ConsentStatus:
        """The caller's consent against ``destination``."""
        user, account = _ids(principal)
        consent = await self.store.get_consent(user, account)
        return ConsentStatus(
            required=destination is not None and consent_required(destination),
            destination=destination,
            granted_at=consent.granted_at if consent else None,
            granted_for=consent.destination if consent else None,
            covers=destination is not None and covers(consent, destination),
        )

    async def grant(self, principal: Principal, destination: Destination) -> None:
        """Record the caller's consent for ``destination`` (replaces an earlier one)."""
        user, account = _ids(principal)
        await self.store.put_consent(
            user, account, Consent(granted_at=self.clock(), destination=destination)
        )
        _log.info(
            "ai consent granted",
            user_id=str(user),
            family=destination.family,
            model=destination.model,
        )

    async def withdraw(self, principal: Principal) -> None:
        """Delete the caller's consent and switch automatic analysis off."""
        user, account = _ids(principal)
        existed = await self.store.delete_consent(user, account)
        setting = await self.store.get_auto_setting(user, account)
        if setting is not None and setting.enabled:
            await self.store.put_auto_setting(
                AutoSetting(
                    user_id=user,
                    account_id=account,
                    enabled=False,
                    updated_at=self.clock(),
                    kind=setting.kind,
                )
            )
        _log.info("ai consent withdrawn", user_id=str(user), existed=existed)


class ConsentService:
    """The consent use cases bound to the destination the caller's requests go to now."""

    def __init__(self, *, gate: ConsentGate, destinations: DestinationSource) -> None:
        self.gate = gate
        self.destinations = destinations

    async def status(self, principal: Principal) -> ConsentStatus:
        """Is consent required for the caller's current provider, and is it given?"""
        return await self.gate.status(principal, await self.destinations.destination(principal))

    async def grant(self, principal: Principal) -> ConsentStatus:
        """Agree to the current destination. 409 without a provider; a destination that needs
        no consent (local, simulated) records nothing."""
        destination = await self.destinations.destination(principal)
        if destination is None:
            raise ProviderMissing("there is no AI provider to agree to")
        if consent_required(destination):
            await self.gate.grant(principal, destination)
        return await self.gate.status(principal, destination)

    async def withdraw(self, principal: Principal) -> ConsentStatus:
        """Withdraw the consent; automatic analysis is switched off with it."""
        await self.gate.withdraw(principal)
        return await self.status(principal)

    async def require_current(self, principal: Principal) -> None:
        """Raise :class:`ConsentRequired` when the current destination is a cloud model the
        caller has not agreed to; passes without a provider (nothing would be sent)."""
        destination = await self.destinations.destination(principal)
        if destination is not None:
            await self.gate.check(principal, destination)


__all__ = ["ConsentGate", "ConsentService", "ConsentStatus", "DestinationSource"]
