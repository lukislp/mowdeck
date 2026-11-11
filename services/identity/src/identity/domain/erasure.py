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

ASK_ORDER: tuple[str, ...] = (
    "fleet",
    "scheduler",
    "telemetry",
    "notifications",
    "vision",
    "planner",
    "billing",
    "entitlements",
)
"""The order in which the services are asked to erase an account (``ERASURE_SERVICES`` of the
contracts; a test keeps both equal). It is a rule, not the order somebody happened to write a
setting in: :func:`ask_order` applies it to every purge.

* ``fleet`` first: removing the mowers stops their connections and simulators, so the other
  services see no further state of the account.
* ``billing`` before ``entitlements``: Billing sets plans in Entitlements, and setting a plan
  creates the plan row of any account. Entitlements is asked only when Billing has confirmed
  that it has ended the subscriptions and applies nothing for the account any more; otherwise
  a late plan change would write the row of an erased account again."""


def ask_order(services: Iterable[str]) -> list[str]:
    """``services`` once each, in the order they are asked (:data:`ASK_ORDER`). A name the order
    does not know comes last, in the order given."""
    rank = {name: position for position, name in enumerate(ASK_ORDER)}
    return sorted(dict.fromkeys(services), key=lambda name: rank.get(name, len(rank)))


def required_services(listed: Iterable[str], deployed_optional: Iterable[str]) -> tuple[str, ...]:
    """The services that must confirm an account erasure in this installation: the listed ones
    and, of the optional parts the deployment says it runs, those that keep data of an account
    (Billing). Other optional parts (the assistant, the camera relay) keep none and are not
    asked. In the order they are asked."""
    optional = [name for name in deployed_optional if name in ASK_ORDER]
    return tuple(ask_order([*listed, *optional]))


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


OWNS_ANOTHER_ACCOUNT = (
    "you own another account: delete that account first; "
    "deleting you here would leave it without an owner"
)
MEMBER_OF_ANOTHER_ACCOUNT = (
    "you are a member of another account as well: leave that account first; "
    "deleting you here would remove you there too"
)


def self_deletion_blocker(memberships: Iterable[Membership], account_id: UUID) -> str | None:
    """Why this person cannot delete themselves from ``account_id``, or None.

    Deleting a user removes the person everywhere: the user row and with it every membership.
    Today a user has exactly one membership (a membership is only ever created together with its
    user: a registration or a new sub-user), so this never blocks. The rule still looks at all
    memberships, so that a later way into a second account (invitations) cannot silently reach
    the deletion: owning another account would leave that account without an owner, and a
    membership elsewhere would be removed without that account being told."""
    elsewhere = [m for m in memberships if m.account_id != account_id]
    if any(m.is_owner for m in elsewhere):
        return OWNS_ANOTHER_ACCOUNT
    if elsewhere:
        return MEMBER_OF_ANOTHER_ACCOUNT
    return None


def self_deletion(memberships: Iterable[Membership], account_id: UUID) -> SelfDeletion | None:
    """Which operation deletes the person signed in to ``account_id``, decided over all of their
    ``memberships``; None when none applies (:func:`self_deletion_blocker` says why, or the
    person is not a member of the account).

    The owner cannot leave an account behind without an owner (there is no hand-over), so the
    owner's own deletion is the account's; everybody else deletes only themselves."""
    all_of_them = list(memberships)
    own = next((m for m in all_of_them if m.account_id == account_id), None)
    if own is None or self_deletion_blocker(all_of_them, account_id) is not None:
        return None
    return SelfDeletion.ACCOUNT if own.is_owner else SelfDeletion.USER


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


USER_DELETED_EVENT = "identity.user.deleted"
"""The event that tells the services a user is gone. It carries the opaque user id and no name
(:data:`NO_USERNAME` at most), and the services still have to read it, so it is the one event
about a deleted user that stays in the stream."""

NO_USERNAME = "-"
"""What Identity sends in place of the user name (``USERNAME_PLACEHOLDER`` of the contracts; a
test keeps both equal)."""

EVENT_FILTERS: tuple[str, ...] = (
    "event.billing.>",
    "event.fleet.>",
    "event.identity.account.created",
    "event.identity.session.new_device",
    "event.identity.user.created",
    "event.identity.user.deleted",
    "event.identity.user.disabled",
    "event.mower.>",
    "event.notifications.>",
    "event.scheduler.>",
    "event.telemetry.>",
    "event.tracking.>",
    "event.vision.>",
)
"""The subjects of the ``EVENTS`` stream whose events can name an account or a user with more
than an id (names, texts, positions, a device): what is read when an account or a user is erased.
Together with :data:`OPAQUE_EVENTS` it covers every event type of the contracts; a repository
check fails for a new event that is in neither."""

OPAQUE_EVENTS: Mapping[str, str] = {
    "event.identity.grants.changed": "the account id and subject ids (user:<uuid>)",
    "event.identity.api_token.revoked": "the token id, the account id and a fixed reason",
    "event.identity.access.revoked.*": "the revoked id and two times (the deny list)",
    "event.entitlements.plan.changed": "the account id and a plan key",
}
"""Events that are not read: they carry random ids and nothing that names anybody once the rows
are gone, and they are the bulk of the stream. They leave with the stream's retention."""


class EventScope(StrEnum):
    """Whose stored events are removed."""

    ACCOUNT = "account"
    USER = "user"


def _same_id(value: Any, wanted: UUID) -> bool:
    return isinstance(value, str) and value.lower() == str(wanted)


def event_names_account(event: Mapping[str, Any], account_id: UUID) -> bool:
    """Whether a stored domain event (the envelope as JSON) belongs to the account: the
    envelope's ``account_id`` says so, or the payload's (every payload of an account's event
    carries it). Platform level events (no account) belong to nobody."""
    payload = event.get("payload")
    return _same_id(event.get("account_id"), account_id) or (
        isinstance(payload, Mapping) and _same_id(payload.get("account_id"), account_id)
    )


def event_names_user(event: Mapping[str, Any], user_id: UUID) -> bool:
    """Whether a stored domain event names the user and must go when the user is deleted.

    An event names a user by ``user_id`` in its payload (the user's notifications, logins from a
    new device, ``identity.user.created`` with the user name of version 1) or by a principal
    reference in it (``requested_by``, ``owner``: the subject id next to the display name). The
    account's other events stay: the account goes on. :data:`USER_DELETED_EVENT` itself stays
    as long as it carries no name, the services still consume it."""
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return False
    if event.get("type") == USER_DELETED_EVENT and payload.get("username") in (None, NO_USERNAME):
        return False
    if _same_id(payload.get("user_id"), user_id):
        return True
    return any(
        isinstance(value, Mapping) and _same_id(value.get("subject"), user_id)
        for value in payload.values()
    )


TOKEN_SKEW = timedelta(seconds=60)
"""How much longer than its lifetime an access token may be accepted somewhere: the leeway a
verifier allows on ``exp`` (``MOWDECK_AUTH_LEEWAY_SECONDS``, at most
``mowdeck_auth.settings.MAX_LEEWAY_SECONDS`` = 30 s) plus as much again for clocks that differ
between hosts. The two are tied: every verifier refuses to start with a larger leeway, and a
test keeps this constant at twice that maximum."""


def tokens_expired_at(
    ended_at: datetime, access_token_ttl: timedelta, skew: timedelta = TOKEN_SKEW
) -> datetime:
    """From when on no access token issued up to ``ended_at`` can be accepted anywhere: its
    lifetime (the one setting the issuer signs tokens with) plus the skew.

    Access tokens are verified offline, so ending the sessions does not end the tokens. The purge
    of an account asks no service to delete before this moment (a still valid token could write
    rows again after the service confirmed), and a deny list entry
    (``identity.access.revoked``) lives exactly until then."""
    return ended_at + access_token_ttl + skew


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


@dataclass(slots=True)
class EventErasure:
    """The progress of removing one account's or one user's stored events from the stream.

    The stream is read filter by filter (:data:`EVENT_FILTERS`), in slices. The record remembers
    where the stream ended when the work began, so the work ends although events keep arriving,
    and how far it read, so a restart continues instead of starting over. It holds ids and
    counters and is deleted when the work is done.
    """

    subject_id: UUID
    """The account or the user."""
    scope: str
    requested_at: datetime
    last_sequence: int
    """The newest sequence of the stream when the work began; nothing above it is read."""
    filter_index: int = 0
    scanned_sequence: int = 0
    """The last sequence read with the current filter."""
    erased: int = 0

    @classmethod
    def begin(
        cls, scope: "EventScope", subject_id: UUID, now: datetime, last_sequence: int
    ) -> "EventErasure":
        """The record of work that starts now and reads up to ``last_sequence``."""
        return cls(subject_id, EventScope(scope).value, now, max(0, last_sequence))

    @property
    def finished(self) -> bool:
        """Every filter was read to the bound."""
        return self.filter_index >= len(EVENT_FILTERS)

    @property
    def filter(self) -> str:
        """The subject filter being read."""
        return EVENT_FILTERS[self.filter_index]

    def names(self, event: Mapping[str, Any]) -> bool:
        """Whether a stored event belongs to the account, or names the user."""
        if self.scope == EventScope.ACCOUNT.value:
            return event_names_account(event, self.subject_id)
        return event_names_user(event, self.subject_id)

    def scanned(self, sequence: int) -> None:
        """A message of the current filter was handled."""
        self.scanned_sequence = max(self.scanned_sequence, sequence)

    def next_filter(self) -> None:
        """The current filter was read to the bound."""
        self.filter_index += 1
        self.scanned_sequence = 0


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
    def begin(
        cls,
        account_id: UUID,
        required: Sequence[str],
        now: datetime,
        *,
        ask_from: datetime | None = None,
    ) -> "AccountErasure":
        """The record of a purge that starts now. The services are asked from ``ask_from`` on
        (:func:`tokens_expired_at`: when the last access token of the account has run out);
        without it the first attempt is due at once."""
        return cls(account_id, now, ask_order(required), next_attempt_at=ask_from or now)

    @property
    def open_services(self) -> list[str]:
        """The required services that did not confirm yet, in the order they are asked
        (:func:`ask_order`, also for a record that was stored in another order)."""
        return [s for s in ask_order(self.required) if s not in self.confirmed]

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
