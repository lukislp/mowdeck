# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The operator level (``platform:main``): the account list, the operator admins and the
account deletions per month of the payments dashboard (ADR 0083).

Operator admins manage accounts and plans but see no mower data (project plan, "Konten").
They are the ``operator_admin`` relation on ``platform:main`` in OpenFGA, written by the CLI
(``python -m identity.cli grant-platform-admin USERNAME``) and checked as
``can_manage_accounts`` (this service) and ``can_manage_plans`` (Entitlements).
"""

from dataclasses import dataclass
from uuid import UUID

from mowdeck_authz import PLATFORM_OBJECT, RelationshipTuple, user
from mowdeck_contracts.v1 import IDENTITY_GRANTS_CHANGED, GrantsChanged

from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.domain.credentials import normalize_username
from identity.domain.deletion_stats import (
    ACCOUNT_RESTORED,
    DELETION_REQUESTED,
    DeletionMonth,
    deletions_by_month,
)
from identity.domain.erasure import ErasureState
from identity.domain.errors import AuthzUnavailable, NotFoundError
from identity.domain.models import Account, AccountStatus, User


@dataclass(frozen=True, slots=True)
class PlatformAccount:
    """An account as operators see it: no mowers, no members beyond the owner's name."""

    account: Account
    owner_username: str | None


@dataclass(frozen=True, slots=True)
class PlatformAccountPage:
    """A page of accounts, oldest first."""

    items: list[PlatformAccount]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class DeletionStats:
    """Account deletions per month and where the open ones stand now (counts only)."""

    months: list[DeletionMonth]
    in_grace: int
    """Deletions requested whose grace period runs: the owner can still restore them."""
    purging: int
    """Erasures that started and wait for a service to confirm."""


def operator_admin_tuple(user_id: UUID) -> RelationshipTuple:
    """``platform:main#operator_admin@user:<id>``."""
    return RelationshipTuple(user(user_id), "operator_admin", PLATFORM_OBJECT)


class PlatformService:
    """Use cases of the operator level."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def accounts(self, offset: int, limit: int) -> PlatformAccountPage:
        """Every account of the installation, oldest first, with the owner's username."""
        async with self.deps.store.transaction() as tx:
            accounts = await tx.accounts.find(order_by="created_at")
            page = accounts[offset : offset + limit]
            items = []
            for acct in page:
                owners = await tx.memberships.find(
                    account_id=acct.id, is_owner=True, order_by="created_at"
                )
                owner: User | None = await tx.users.get(id=owners[0].user_id) if owners else None
                items.append(PlatformAccount(acct, owner.username if owner else None))
        return PlatformAccountPage(items, len(accounts), offset, limit)

    async def deletion_stats(self, months: int) -> DeletionStats:
        """Account deletions of the last ``months`` months for the payments dashboard (ADR
        0083): scheduled, restored and completed per month, from the audit rows (which stay,
        scrubbed, after an erasure) and the erasure records (ADR 0082). No account is named."""
        async with self.deps.store.transaction() as tx:
            requested = await tx.audit.find(action=DELETION_REQUESTED, outcome="success")
            restored = await tx.audit.find(action=ACCOUNT_RESTORED, outcome="success")
            erasures = await tx.erasures.find()
            in_grace = await tx.accounts.count(status=AccountStatus.DELETING.value)
        return DeletionStats(
            months=deletions_by_month(
                scheduled=[entry.at for entry in requested],
                restored=[entry.at for entry in restored],
                completed=[e.completed_at for e in erasures if e.completed_at is not None],
                now=self.deps.clock(),
                months=months,
            ),
            in_grace=in_grace,
            purging=sum(1 for e in erasures if e.state is ErasureState.PURGING),
        )

    async def set_admin(self, username: str, ctx: RequestContext, *, grant: bool = True) -> UUID:
        """Grant (or revoke) ``platform:main#operator_admin`` for a user; audited."""
        async with self.deps.store.transaction() as tx:
            found = await tx.users.get(username_norm=normalize_username(username))
        if found is None:
            msg = "user not found"
            raise NotFoundError(msg)
        item = operator_admin_tuple(found.id)
        try:
            if grant:
                await self.deps.sync.writer.write([item], [])
            else:
                await self.deps.sync.writer.write([], [item])
        except ValueError:
            raise
        except Exception as exc:
            msg = "the authorization service is unavailable; nothing was changed"
            raise AuthzUnavailable(msg) from exc
        async with self.deps.uow() as work:
            await work.audit(
                ctx,
                "platform.admin_granted" if grant else "platform.admin_revoked",
                target=("user", found.id),
                details={"username": found.username_norm},
            )
        # Other services drop their cached decisions of the user (without a bus, as in the CLI,
        # their decision cache expires within its TTL, 5 s by default).
        await self.deps.events.publish(
            IDENTITY_GRANTS_CHANGED, None, GrantsChanged(subjects=[f"user:{found.id}"])
        )
        return found.id
