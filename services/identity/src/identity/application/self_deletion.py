# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

""" "Delete me" (ADR 0082): one request for whoever is signed in, the policy by role.

``DELETE /identity/me`` does not ask the client which operation applies. The rule is
:func:`identity.domain.erasure.self_deletion`:

* the **owner** deletes the account: the request is the account deletion of
  :meth:`AccountService.request_deletion`, with everything that holds there (409 while other
  members exist, the grace period, restore, the purge in every service afterwards);
* **every other member**, sub-users included, deletes only themselves, at once
  (:meth:`UserService.delete_self`): the account and what the user created in it stay.

Who may ask is the same for both (ADR 0057): a login session, confirmed with the password in the
request or a recent step-up. API tokens and service identities are refused before anything is
looked up. This use case holds the policy and the confirmation; the two operations stay where
they are, so there is one account deletion and one user deletion in the code base.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from mowdeck_auth.step_up import StepUpUnavailable
from mowdeck_contracts.v1 import Principal, PrincipalKind

from identity.application.accounts import AccountService
from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.application.reauth import Reauthenticator
from identity.application.users import UserService
from identity.domain.erasure import SelfDeletion, self_deletion
from identity.domain.errors import ForbiddenError

_INTERACTIVE = (PrincipalKind.USER, PrincipalKind.SUBUSER)


@dataclass(frozen=True, slots=True)
class SelfDeleted:
    """What "delete me" did."""

    operation: SelfDeletion
    account_id: UUID
    delete_after: datetime | None = None
    """``ACCOUNT``: when the grace period ends and the purge starts (the owner can restore the
    account until then). ``USER``: None, the user is gone now."""


class SelfDeletionService:
    """Deletes the caller: the account for its owner, the user for everybody else."""

    def __init__(
        self, deps: Deps, accounts: AccountService, users: UserService, reauth: Reauthenticator
    ) -> None:
        self.deps = deps
        self.accounts = accounts
        self.users = users
        self.reauth = reauth

    async def delete_me(self, password: str | None, ctx: RequestContext) -> SelfDeleted:
        """Apply the operation of the caller's role (see the module docstring).

        ``password`` confirms the request; without it the caller needs a recent step-up (401
        ``reauth-required`` otherwise). 403 ``step-up-unavailable`` for API tokens and services,
        403 for a token that is bound to no account or whose user is not a member of it.
        """
        principal, account_id = self._caller(ctx)
        operation = await self._operation(account_id, principal.subject)
        if operation is SelfDeletion.ACCOUNT:
            account = await self.accounts.request_deletion(account_id, password, ctx)
            return SelfDeleted(operation, account_id, account.delete_after)
        await self.reauth.check(ctx, principal, password, action="user")
        await self.users.delete_self(account_id, ctx)
        return SelfDeleted(operation, account_id)

    @staticmethod
    def _caller(ctx: RequestContext) -> tuple[Principal, UUID]:
        """The signed-in user and their account; everything else cannot delete itself."""
        principal = ctx.principal
        if principal is None:
            msg = "login required"
            raise ForbiddenError(msg)
        if principal.kind not in _INTERACTIVE:
            msg = "deleting yourself needs an interactive sign-in; API tokens and services cannot"
            raise StepUpUnavailable(msg)
        if principal.account_id is None:
            msg = "the token is not bound to an account"
            raise ForbiddenError(msg)
        return principal, principal.account_id

    async def _operation(self, account_id: UUID, user_id: UUID) -> SelfDeletion:
        async with self.deps.store.transaction() as tx:
            membership = await tx.memberships.get(account_id=account_id, user_id=user_id)
        if membership is None:
            msg = "you are not a member of this account"
            raise ForbiddenError(msg)
        return self_deletion(membership)
