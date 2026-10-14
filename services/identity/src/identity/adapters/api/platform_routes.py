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
from pydantic import BaseModel

from identity.adapters.api.deps import ServicesDep
from identity.application.platform import PlatformAccount

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
