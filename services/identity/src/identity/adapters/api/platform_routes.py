# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The operator level under ``/api/v1/identity/platform`` (Betreiber-Konsole, Phase 4).

Only operator admins: ``can_manage_accounts`` on ``platform:main`` in OpenFGA (account owners
get 403). The answers carry no mower data and no member list beyond the owner's username.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from mowdeck_authz import PLATFORM_OBJECT, ObjectRef, Relation
from mowdeck_authz.fastapi import require
from mowdeck_contracts.v1 import Principal
from pydantic import BaseModel, Field

from identity.adapters.api.deps import ServicesDep
from identity.application.platform import PlatformAccount
from identity.domain.deletion_stats import DEFAULT_ZONE

router = APIRouter(prefix="/identity/platform", tags=["identity: platform"])


def platform_object(request: Request) -> ObjectRef:
    """Resolver of the operator endpoints: the single ``platform:main`` object."""
    del request
    return PLATFORM_OBJECT


OperatorAdmin = Annotated[
    Principal, Depends(require(Relation.CAN_MANAGE_ACCOUNTS, platform_object))
]
"""``can_manage_accounts`` on ``platform:main`` (403 otherwise, 401 without a token)."""


class PlatformAccountOut(BaseModel):
    """An account for operators."""

    id: UUID
    name: str
    kind: str
    status: str
    created_at: datetime
    owner_username: str | None

    @classmethod
    def of(cls, item: PlatformAccount) -> "PlatformAccountOut":
        """From the use case view."""
        acct = item.account
        return cls(
            id=acct.id,
            name=acct.name,
            kind=acct.kind,
            status=acct.status.value,
            created_at=acct.created_at,
            owner_username=item.owner_username,
        )


class PlatformAccountPageOut(BaseModel):
    """A page of accounts (``offset``/``limit``), oldest first."""

    items: list[PlatformAccountOut]
    total: int
    offset: int
    limit: int


class DeletionMonthOut(BaseModel):
    """Account deletions of one month of the asked zone."""

    month: str = Field(description="YYYY-MM, a calendar month in ``time_zone``.")
    scheduled: int = Field(description="Deletions an owner requested.")
    restored: int = Field(description="Requests the owner took back within the grace period.")
    completed: int = Field(description="Erasures every service confirmed (ADR 0082).")


class DeletionStatsOut(BaseModel):
    """Account deletions per month for the payments dashboard (ADR 0083): counts only."""

    time_zone: str = Field(description="IANA zone of the month buckets (the ``tz`` asked for).")
    months: list[DeletionMonthOut]
    in_grace: int = Field(description="Requested now, still restorable by the owner.")
    purging: int = Field(description="Erasures waiting for a service to confirm.")


@router.get("/deletions")
async def platform_deletions(
    _: OperatorAdmin,
    services: ServicesDep,
    months: Annotated[int, Query(ge=1, le=36)] = 12,
    tz: Annotated[str, Query(min_length=1, max_length=64)] = DEFAULT_ZONE,
) -> DeletionStatsOut:
    """Account deletions of the last ``months`` months (operator admins): scheduled, restored
    and completed per local calendar month in the IANA zone ``tz`` (the web app passes the
    ``time_zone`` of Billing's payments dashboard, so both count the same months; 422 for an
    unknown zone), and how many are in the grace period or being erased now. No account id, no
    name."""
    stats = await services.platform.deletion_stats(months, tz)
    return DeletionStatsOut(
        months=[
            DeletionMonthOut(
                month=m.month, scheduled=m.scheduled, restored=m.restored, completed=m.completed
            )
            for m in stats.months
        ],
        time_zone=stats.time_zone,
        in_grace=stats.in_grace,
        purging=stats.purging,
    )


@router.get("/accounts")
async def platform_accounts(
    _: OperatorAdmin,
    services: ServicesDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PlatformAccountPageOut:
    """Every account of the installation (operator admins; no mower data)."""
    page = await services.platform.accounts(offset, limit)
    return PlatformAccountPageOut(
        items=[PlatformAccountOut.of(item) for item in page.items],
        total=page.total,
        offset=page.offset,
        limit=page.limit,
    )
