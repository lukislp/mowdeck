"""Passkeys of users (ADR 0050): adding, listing, renaming and removing WebAuthn credentials.

Logging in with a passkey is the authenticator plug-in ``PasskeyAuthenticator`` behind
``SessionService.login_passkey``; this module manages the credentials.

* Adding needs the current password (re-authentication right before the ceremony), then
  ``navigator.credentials.create()`` answers a single-use challenge bound to the user.
* A user manages their own passkeys. An account admin (``can_manage_users``) can list and remove
  the passkeys of the account's sub-users, the same reach as setting their password; persons
  manage their own.
* A passkey never replaces the password: every account keeps its password, so removing the last
  passkey is always allowed and there are no recovery codes.
* Audit: ``passkey.added``, ``passkey.renamed``, ``passkey.removed`` (with ``by_admin``),
  ``passkey.reauth_failed``; logins write ``passkey.used`` and ``session.login``.
"""

import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import Principal, PrincipalKind

from identity.application.authenticators import CHALLENGE_BYTES
from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.application.ports import CeremonyRejected, Ge, KnownCredential, PasskeyCeremonies
from identity.domain.credentials import validate_passkey_name
from identity.domain.errors import (
    AuthenticationFailed,
    ConflictError,
    ForbiddenError,
    InvalidInput,
    NotFoundError,
    RateLimited,
)
from identity.domain.models import (
    CeremonyPurpose,
    LoginAttempt,
    Passkey,
    RelyingParty,
    User,
    WebAuthnChallenge,
)

MAX_PASSKEYS_PER_USER = 20
DEFAULT_NAME = "Passkey"


@dataclass(frozen=True, slots=True)
class RegistrationStart:
    """Options for ``navigator.credentials.create()`` and the ceremony that answers them."""

    ceremony_id: UUID
    options: dict[str, Any]


class PasskeyService:
    """Use cases around the passkeys of users."""

    def __init__(self, deps: Deps, ceremonies: PasskeyCeremonies | None) -> None:
        self.deps = deps
        self.ceremonies = ceremonies

    @property
    def enabled(self) -> bool:
        """Passkeys are configured (a public URL gives the relying party)."""
        return self.deps.config.passkey_rp is not None and self.ceremonies is not None

    def _rp(self) -> tuple[RelyingParty, PasskeyCeremonies]:
        rp, ceremonies = self.deps.config.passkey_rp, self.ceremonies
        if rp is None or ceremonies is None:
            msg = "passkeys are not enabled on this installation"
            raise ForbiddenError(msg)
        return rp, ceremonies

    @staticmethod
    def _user(ctx: RequestContext) -> Principal:
        principal = ctx.principal
        if principal is None or principal.kind not in (PrincipalKind.USER, PrincipalKind.SUBUSER):
            msg = "only users have passkeys"
            raise ForbiddenError(msg)
        return principal

    # --- own passkeys ----------------------------------------------------------------------------

    async def list_own(self, ctx: RequestContext) -> list[Passkey]:
        """The caller's passkeys, newest first."""
        principal = self._user(ctx)
        async with self.deps.store.transaction() as tx:
            return await tx.passkeys.find(
                user_id=principal.subject, order_by="created_at", descending=True
            )

    async def start_registration(self, ctx: RequestContext, password: str) -> RegistrationStart:
        """Check the password again, then hand out creation options for the caller."""
        rp, ceremonies = self._rp()
        principal = self._user(ctx)
        async with self.deps.store.transaction() as tx:
            found = await tx.users.get(id=principal.subject)
            known = await tx.passkeys.find(user_id=principal.subject)
        if found is None or found.password_hash is None:
            raise AuthenticationFailed("no password set")
        await self._check_throttle(found)
        if not await self.deps.hasher.verify(found.password_hash, password):
            await self._reauth_failed(ctx, found, principal)
            raise AuthenticationFailed("wrong password for passkey registration")
        if len(known) >= MAX_PASSKEYS_PER_USER:
            msg = f"at most {MAX_PASSKEYS_PER_USER} passkeys per user"
            raise ConflictError(msg)
        now = self.deps.clock()
        timeout = self.deps.config.webauthn_timeout
        record = WebAuthnChallenge(
            id=uuid4(),
            purpose=CeremonyPurpose.REGISTER,
            challenge=secrets.token_bytes(CHALLENGE_BYTES),
            created_at=now,
            expires_at=now + timeout,
            user_id=found.id,
        )
        async with self.deps.store.transaction() as tx:
            await tx.webauthn_challenges.add(record)
        options = ceremonies.registration_options(
            rp,
            challenge=record.challenge,
            user_handle=found.id.bytes,
            user_name=found.username,
            display_name=found.display_name or found.username,
            exclude=[KnownCredential(k.credential_id, tuple(k.transports)) for k in known],
            timeout_ms=int(timeout.total_seconds() * 1000),
        )
        return RegistrationStart(record.id, options)

    async def finish_registration(
        self,
        ctx: RequestContext,
        ceremony_id: UUID,
        credential: dict[str, Any],
        name: str | None,
    ) -> Passkey:
        """Verify the new credential and store it as a passkey of the caller."""
        rp, ceremonies = self._rp()
        principal = self._user(ctx)
        label = validate_passkey_name(name) if name and name.strip() else DEFAULT_NAME
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            record = await tx.webauthn_challenges.get(
                id=ceremony_id, purpose=CeremonyPurpose.REGISTER.value, user_id=principal.subject
            )
            taken = await tx.webauthn_challenges.delete(id=ceremony_id) if record else 0
        if record is None or taken != 1 or now >= record.expires_at:
            msg = "the passkey ceremony expired; start again"
            raise InvalidInput(msg)
        try:
            created = ceremonies.verify_registration(
                rp, credential=credential, challenge=record.challenge
            )
        except CeremonyRejected as exc:
            msg = "the passkey could not be verified"
            raise InvalidInput(msg) from exc
        passkey = Passkey(
            id=uuid4(),
            user_id=principal.subject,
            credential_id=created.credential_id,
            public_key=created.public_key,
            sign_count=created.sign_count,
            name=label,
            created_at=now,
            transports=list(created.transports),
            aaguid=created.aaguid,
            backup_eligible=created.backup_eligible,
            backed_up=created.backed_up,
            created_user_agent=(ctx.user_agent or "")[:200] or None,
        )
        async with self.deps.uow() as work:
            if await work.tx.passkeys.count(user_id=principal.subject) >= MAX_PASSKEYS_PER_USER:
                msg = f"at most {MAX_PASSKEYS_PER_USER} passkeys per user"
                raise ConflictError(msg)
            try:
                await work.tx.passkeys.add(passkey)
            except ConflictError:
                msg = "this passkey is already registered"
                raise ConflictError(msg) from None
            await work.audit(
                ctx,
                "passkey.added",
                account_id=principal.account_id,
                target=("user", principal.subject),
                details={
                    "passkey": str(passkey.id),
                    "name": passkey.name,
                    "aaguid": passkey.aaguid,
                    "synced": passkey.backup_eligible,
                },
            )
        return passkey

    async def rename_own(self, ctx: RequestContext, passkey_id: UUID, name: str) -> Passkey:
        """Rename one of the caller's passkeys."""
        principal = self._user(ctx)
        label = validate_passkey_name(name)
        async with self.deps.uow() as work:
            passkey = await work.tx.passkeys.get(id=passkey_id, user_id=principal.subject)
            if passkey is None:
                msg = "passkey not found"
                raise NotFoundError(msg)
            old, passkey.name = passkey.name, label
            await work.tx.passkeys.update(passkey)
            await work.audit(
                ctx,
                "passkey.renamed",
                account_id=principal.account_id,
                target=("user", principal.subject),
                details={"passkey": str(passkey.id), "from": old, "to": label},
            )
        return passkey

    async def remove_own(self, ctx: RequestContext, passkey_id: UUID) -> None:
        """Remove one of the caller's passkeys (the password always stays)."""
        principal = self._user(ctx)
        assert principal.account_id is not None  # noqa: S101 - users always act for an account
        await self._remove(ctx, principal.account_id, principal.subject, passkey_id, admin=False)

    # --- admin: passkeys of sub-users ---------------------------------------------------------

    async def _subuser(self, account_id: UUID, user_id: UUID) -> User:
        async with self.deps.store.transaction() as tx:
            found = await tx.users.get(id=user_id)
            membership = await tx.memberships.get(account_id=account_id, user_id=user_id)
        if found is None or membership is None:
            msg = "user not found"
            raise NotFoundError(msg)
        if found.owner_account_id != account_id:
            msg = "only passkeys of sub-users of this account can be managed"
            raise ForbiddenError(msg)
        return found

    async def list_of_user(self, account_id: UUID, user_id: UUID) -> list[Passkey]:
        """Passkeys of a sub-user of the account (``can_manage_users``)."""
        await self._subuser(account_id, user_id)
        async with self.deps.store.transaction() as tx:
            return await tx.passkeys.find(user_id=user_id, order_by="created_at", descending=True)

    async def remove_of_user(
        self, ctx: RequestContext, account_id: UUID, user_id: UUID, passkey_id: UUID
    ) -> None:
        """Remove a passkey of a sub-user of the account (``can_manage_users``)."""
        await self._subuser(account_id, user_id)
        await self._remove(ctx, account_id, user_id, passkey_id, admin=True)

    async def _remove(
        self, ctx: RequestContext, account_id: UUID, user_id: UUID, passkey_id: UUID, *, admin: bool
    ) -> None:
        async with self.deps.uow() as work:
            passkey = await work.tx.passkeys.get(id=passkey_id, user_id=user_id)
            if passkey is None:
                msg = "passkey not found"
                raise NotFoundError(msg)
            await work.tx.passkeys.delete(id=passkey.id)
            await work.audit(
                ctx,
                "passkey.removed",
                account_id=account_id,
                target=("user", user_id),
                details={"passkey": str(passkey.id), "name": passkey.name, "by_admin": admin},
            )

    # --- helpers --------------------------------------------------------------------------------

    async def _check_throttle(self, found: User) -> None:
        """The username throttle of the password login applies to the re-authentication too."""
        config = self.deps.config
        since = self.deps.clock() - config.login_failure_window
        async with self.deps.store.transaction() as tx:
            failures = await tx.login_attempts.count(
                username_norm=found.username_norm, success=False, at=Ge(since)
            )
        if failures >= config.login_max_failures:
            msg = "too many failed attempts for this user; try again later"
            raise RateLimited(msg, int(config.login_failure_window.total_seconds()))

    async def _reauth_failed(self, ctx: RequestContext, found: User, principal: Principal) -> None:
        """A wrong password before a registration counts like a failed login of the user."""
        now = self.deps.clock()
        async with self.deps.uow() as work:
            await work.tx.login_attempts.add(
                LoginAttempt(uuid4(), now, found.username_norm, False, ctx.ip, "passkey reauth")
            )
            await work.audit(
                ctx,
                "passkey.reauth_failed",
                account_id=principal.account_id,
                target=("user", found.id),
                outcome="failure",
            )
