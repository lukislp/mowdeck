# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The rules of deleting an account for good (ADR 0082), free of storage and bus.

An account moves ``active -> deleting -> purging -> erased`` (the row of an erased account is
gone; the :class:`AccountErasure` record stays as the proof):

* **deleting** (``Account.status``): requested by the owner; locked for everybody but the owner,
  who may restore it until ``delete_after`` (the grace period). Allowed only when the owner is the
  only member: other members are removed first, a deletion never removes people implicitly.
* **purging**: the grace period passed and the purge started: an :class:`AccountErasure` records
  which services have to confirm, which did, and when the next attempt is due. No way back.
* **erased**: every required service confirmed and Identity deleted its own rows.

A member who is not the owner deletes only themselves ("delete me", step 3): the user row, the
membership and the personal rows go at once, without a grace period, and the account stays.
:func:`self_deletion` decides which of the two operations applies to a member, and
:func:`audit_fields_after_user_scrub` what an audit row keeps once a user is gone.

The functions here are pure: the caller passes ``now``.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from identity.domain.models import Account, AccountStatus, Membership

RETRY_BASE_SECONDS = 15
"""First wait after a failed attempt; doubles with every failure."""

AUDIT_DETAILS_KEPT: frozenset[str] = frozenset(
    {
        "delete_after",
        "subuser",
        "kind",
        "reason",
        "writes",
        "deletes",
        "scope",
        "method",
        "session",
        "sessions",
        "sessions_ended",
        "jti",
        "key",
        "permissions",
        "level",
        "role",
        "grants",
        "revoked_at",
        "expires_at",
        "replaces",
        "prefix",
        "provider",
        "new_login_alerts",
        "by_admin",
        "owner",
        "former_owners",
        "aaguid",
        "counts",
        "services",
    }
)
"""The keys of an audit row's ``details`` that stay when the account is erased: ids, counts,
kinds and times. Every other key (username, display name, account or token name, e-mail, device
and client names, provider subjects, old and new names) is personal data and goes. Deny by
default: a key a future action adds is scrubbed until it is listed here. The migration
``0011`` (``audit_log_scrub``) holds the same list; a test keeps both equal."""


def other_members(memberships: Iterable[Membership], user_id: UUID) -> list[Membership]:
    """The memberships of the account besides this user's own (disabled ones count too: they are
    people who still have an account row)."""
    return [m for m in memberships if m.user_id != user_id]


def deletion_blocked_by(memberships: Iterable[Membership], user_id: UUID) -> int:
    """How many other members stop ``user_id`` from deleting the account (0: nothing does)."""
    return len(other_members(memberships, user_id))


class SelfDeletion(StrEnum):
    """What "delete me" means for a member (``DELETE /identity/me``)."""

    ACCOUNT = "account"
    """The owner: the whole account, scheduled with the grace period (the account deletion)."""
    USER = "user"
    """Every other member, sub-users included: the user alone, at once; the account stays."""


def self_deletion(membership: Membership) -> SelfDeletion:
    """Which operation deletes this member. The owner cannot leave an account behind without an
    owner (there is no hand-over), so the owner's own deletion is the account's; everybody else
    deletes only themselves."""
    return SelfDeletion.ACCOUNT if membership.is_owner else SelfDeletion.USER


def user_subject(user_id: UUID) -> str:
    """``actor_subject`` of a user's own audit rows."""
    return f"user:{user_id}"


def concerns_user(
    user_id: UUID, *, actor_subject: str | None, target_type: str | None, target_id: str | None
) -> bool:
    """Whether an audit row is about this user: they acted, or they are the target."""
    return actor_subject == user_subject(user_id) or (
        target_type == "user" and target_id == str(user_id)
    )


def audit_fields_after_user_scrub(
    user_id: UUID,
    *,
    actor_subject: str | None,
    actor_name: str | None,
    ip: str | None,
    details: Mapping[str, Any],
) -> tuple[str | None, str | None, dict[str, Any]]:
    """``(actor_name, ip, details)`` of an audit row that concerns a deleted user.

    ``details`` keeps only :data:`AUDIT_DETAILS_KEPT`. The name and the address go when they are
    the user's: the user acted, or nobody is named as the actor (a sign-in, where the address is
    the one the user came from). When somebody else acted on the user (an admin created or
    disabled them) the name and address are that person's and stay."""
    kept = {key: value for key, value in details.items() if key in AUDIT_DETAILS_KEPT}
    by_somebody_else = actor_subject is not None and actor_subject != user_subject(user_id)
    if by_somebody_else:
        return actor_name, ip, kept
    return None, None, kept


def grace_over(account: Account, now: datetime) -> bool:
    """Whether the grace period of an account scheduled for deletion has passed."""
    return (
        account.status is AccountStatus.DELETING
        and account.delete_after is not None
        and account.delete_after <= now
    )


def deletion_scheduled(account: Account) -> bool:
    """Whether the account is on its way out (``deleting`` or ``purging``). Such an account takes
    no new members and no new API tokens: a member added now would be erased without being told,
    a token could never be exchanged (token sync needs an active account)."""
    return account.status is not AccountStatus.ACTIVE


def retry_delay(attempts: int, maximum: timedelta) -> timedelta:
    """Wait before the next attempt after ``attempts`` failures: 30 s, 60 s, ... capped."""
    return min(maximum, timedelta(seconds=RETRY_BASE_SECONDS * 2 ** min(max(attempts, 1), 10)))


class ErasureState(StrEnum):
    """Where the purge of one account stands."""

    PURGING = "purging"
    """Services are being asked (retried until all confirmed)."""
    ERASED = "erased"
    """Every service confirmed and Identity's own rows are gone."""


@dataclass(slots=True)
class AccountErasure:
    """The purge of one account: the proof that it was asked, and what is still open.

    No foreign key and no personal data: the account row is deleted at the end, this record is
    what an operator and an auditor can still read ("account X was erased at T, services A, B
    confirmed, N rows").
    """

    account_id: UUID
    started_at: datetime
    required: list[str]
    state: ErasureState = ErasureState.PURGING
    confirmed: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    attempts: int = 0
    """Failed attempts since the last confirmation (resets when a service confirms)."""
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        self.state = ErasureState(self.state)

    @classmethod
    def begin(cls, account_id: UUID, required: Sequence[str], now: datetime) -> "AccountErasure":
        """The record of a purge that starts now (due at once)."""
        return cls(account_id, now, list(dict.fromkeys(required)), next_attempt_at=now)

    @property
    def open_services(self) -> list[str]:
        """The required services that did not confirm yet, in the order they are asked."""
        return [s for s in self.required if s not in self.confirmed]

    @property
    def all_confirmed(self) -> bool:
        """Every required service confirmed."""
        return not self.open_services

    def due(self, now: datetime) -> bool:
        """Whether the next attempt may run (a finished purge is never due)."""
        return self.state is ErasureState.PURGING and (
            self.next_attempt_at is None or self.next_attempt_at <= now
        )

    def confirm(self, service: str, deleted: int, now: datetime, *, wait: timedelta) -> None:
        """A service confirmed (a repeated confirmation changes nothing). The next attempt is due
        after ``wait`` (the settle time after the first service, else at once)."""
        if service not in self.required:
            msg = f"{service} is not a required service of this erasure"
            raise ValueError(msg)
        if service not in self.confirmed:
            self.confirmed.append(service)
            self.counts[service] = max(0, deleted)
        self.attempts, self.last_error = 0, None
        self.next_attempt_at = now + wait

    def fail(self, service: str, error: str, now: datetime, *, maximum: timedelta) -> None:
        """A service did not confirm: remember why and back off."""
        self.attempts += 1
        self.last_error = f"{service}: {error}"[:500]
        self.next_attempt_at = now + retry_delay(self.attempts, maximum)

    def lease(self, now: datetime, duration: timedelta) -> None:
        """Hold the next attempt back for ``duration``: whoever takes it runs it, another replica
        finds it not due."""
        self.next_attempt_at = now + duration

    def finish(self, now: datetime) -> None:
        """Identity's own rows are gone: the purge is complete."""
        self.state = ErasureState.ERASED
        self.completed_at, self.next_attempt_at, self.last_error = now, None, None
        self.attempts = 0

    def stalled(self, now: datetime, after: timedelta) -> bool:
        """A purge that runs longer than ``after`` without finishing: worth an alert."""
        return self.state is ErasureState.PURGING and now - self.started_at >= after
