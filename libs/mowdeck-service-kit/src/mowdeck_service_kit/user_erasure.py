# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""A deleted user (ADR 0082, "delete me"): how a service reacts to ``identity.user.deleted``.

When a member deletes themselves, or an admin deletes a sub-user, Identity publishes
``identity.user.deleted`` (stream ``EVENTS``). The account goes on, so nothing of the account is
deleted; a service removes what is the *person's*:

* its rows keyed by the user (preferences, devices, tokens of that user), and
* the user's name wherever it was copied: a :class:`~mowdeck_contracts.v1.PrincipalRef` stored
  as JSON (who issued a command, who owns a schedule) carries ``display_name``. The reference
  keeps its opaque ``subject`` id, which names nobody once Identity's user row is gone; the name
  goes. What the user created stays with the account.

The part that is the same in every service lives here, once:

* :class:`UserErasure`, the use case: the service's ``forget`` (delete and scrub), what it must
  drop in memory, then a count of the personal references that are still there. Leftovers raise
  :class:`UserErasureIncomplete`, so the event is delivered again instead of acknowledged.
* :func:`user_deleted_handler`, the inbound bus adapter: decode the event, call the use case.
  It holds no rule. A failure raises (JetStream redelivers); a malformed event is dropped.
* Where the references are: by convention a ``PrincipalRef`` is stored under the name ``owner``
  or ``<verb>_by`` (:func:`is_ref_name`). ``mowdeck_service_kit.db`` reads the JSON columns of
  that name from the schema and scrubs and counts them on PostgreSQL
  (:func:`~mowdeck_service_kit.db.principal_ref_columns`); :func:`scrub_refs` and
  :func:`named_refs` do the same for a store that keeps dataclass rows in memory. A reference
  added later is covered without anybody listing it.

A service subscribes in its own bus adapter, so the subject stays visible to the repository's
subject scans (the NATS permissions, the architecture diagrams)::

    await bus.subscribe(
        event_subject_for_type(IDENTITY_USER_DELETED),
        user_deleted_handler("fleet", services.user_erasure.erase),
        durable="fleet-users",
        queue="fleet-users",
    )
"""

import dataclasses
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import IDENTITY_USER_DELETED, EventEnvelope, PrincipalRef, UserChanged
from pydantic import ValidationError

from mowdeck_service_kit.bus import BusMessage, DropMessage, MessageHandler
from mowdeck_service_kit.logging import get_logger

_log = get_logger(__name__)

REF_SUBJECT = "subject"
"""Key of a stored ``PrincipalRef`` that holds the opaque subject id."""
REF_NAME = "display_name"
"""Key of a stored ``PrincipalRef`` that holds the person's name: the personal part."""

Forget = Callable[[UUID, UUID | None], Awaitable[int]]
"""``forget(user_id, account_id)``: deletes the user's own rows and scrubs their name from what
stays; the number of rows changed (0 when nothing was left). Idempotent. ``account_id`` narrows
the work to the account the user was deleted in (None: wherever the user appears)."""

Leftovers = Callable[[UUID], Awaitable[Mapping[str, int]]]
"""The personal references to the user the service still holds, by ``table.column`` (or table);
empty when it holds none."""

ForgetInMemory = Callable[[UUID], None]
"""Drops what a use case of this process keeps of a user in memory."""

EraseUser = Callable[[UUID, UUID | None], Awaitable[int]]
"""What the handler calls: :meth:`UserErasure.erase`."""


def names_user(ref: Mapping[str, Any] | None, user_id: UUID) -> bool:
    """Whether a stored principal reference still carries the name of ``user_id``."""
    return (
        ref is not None and ref.get(REF_SUBJECT) == str(user_id) and ref.get(REF_NAME) is not None
    )


def anonymous_ref(ref: Mapping[str, Any]) -> dict[str, Any]:
    """The reference without the person's name: the subject id and the kind stay."""
    return {key: value for key, value in ref.items() if key != REF_NAME}


def is_ref_name(name: str) -> bool:
    """Whether a column or field of this name holds a ``PrincipalRef`` when it stores JSON: the
    convention of every schema is ``owner`` or ``<verb>_by`` (``issued_by``, ``started_by``)."""
    return name == "owner" or name.endswith("_by")


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    """A stored reference as its JSON shape: a mapping as it is, a ``PrincipalRef`` model (a
    domain row that keeps the contract type) dumped; anything else is no reference."""
    if isinstance(value, PrincipalRef):
        return value.model_dump(mode="json")
    return value if isinstance(value, Mapping) else None


def _without_name(value: Any) -> Any:
    """The reference without the person's name, in the type it was stored in."""
    if isinstance(value, PrincipalRef):
        return value.model_copy(update={REF_NAME: None})
    return anonymous_ref(value)


def _named(row: Any, user_id: UUID) -> Iterator[tuple[str, Any]]:
    """The fields of a dataclass row that hold a reference with the name of ``user_id`` (a JSON
    mapping or a ``PrincipalRef``)."""
    for field in dataclasses.fields(row):
        value = getattr(row, field.name)
        if is_ref_name(field.name) and names_user(_as_mapping(value), user_id):
            yield field.name, value


def scrub_refs(rows: Iterable[Any], user_id: UUID, account_id: UUID | None = None) -> int:
    """Remove the name of ``user_id`` from the references of dataclass ``rows``, in place (a
    memory store's rows); with ``account_id`` only in that account's rows. A reference stored as
    a ``PrincipalRef`` stays one, with ``display_name`` None. Returns how many references
    changed."""
    changed = 0
    for row in rows:
        if account_id is not None and getattr(row, "account_id", account_id) != account_id:
            continue
        for name, ref in list(_named(row, user_id)):
            setattr(row, name, _without_name(ref))
            changed += 1
    return changed


def named_refs(rows: Iterable[Any], user_id: UUID) -> dict[str, int]:
    """The references of dataclass ``rows`` that still carry the name of ``user_id``, by field
    name; fields without one are left out."""
    counts: dict[str, int] = {}
    for row in rows:
        for name, _ in _named(row, user_id):
            counts[name] = counts.get(name, 0) + 1
    return counts


class UserErasureIncomplete(Exception):  # noqa: N818 - a state, read like one in the use case
    """The service forgot the user and still holds personal references to them.

    The message names the places and their counts and nothing else."""

    def __init__(self, leftovers: Mapping[str, int]) -> None:
        self.leftovers = dict(leftovers)
        places = ", ".join(f"{name}: {count}" for name, count in sorted(self.leftovers.items()))
        super().__init__(f"personal references to the user remain ({places})")


class UserErasure:
    """Removes one user from one service: forget, drop from memory, then check nothing is left."""

    def __init__(
        self,
        forget: Forget,
        leftovers: Leftovers,
        in_memory: Sequence[ForgetInMemory] = (),
    ) -> None:
        self._forget = forget
        self._leftovers = leftovers
        self._in_memory = tuple(in_memory)

    async def erase(self, user_id: UUID, account_id: UUID | None = None) -> int:
        """Remove what is personal of ``user_id``; returns the number of rows changed (0 on a
        repeated call, or for a user the service never knew). Raises
        :class:`UserErasureIncomplete` when references are left afterwards, and whatever
        ``forget`` raises: the caller lets the event come again."""
        changed = await self._forget(user_id, account_id)
        for forget in self._in_memory:
            forget(user_id)
        remaining = {
            place: count for place, count in (await self._leftovers(user_id)).items() if count
        }
        if remaining:
            raise UserErasureIncomplete(remaining)
        return changed


def user_deleted_handler(service: str, erase: EraseUser) -> MessageHandler:
    """The handler of ``identity.user.deleted`` (a durable consumer of ``EVENTS``).

    Returns to acknowledge. An erasure that raised is raised on, so the event is delivered again
    (the use case is idempotent); an event that cannot be decoded is dropped with a warning,
    because no redelivery would repair it. Another identity event on the same consumer is
    acknowledged and ignored.
    """

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[UserChanged])
        except ValidationError as exc:
            raise DropMessage(
                f"invalid identity.user.deleted event: {exc.error_count()} errors"
            ) from exc
        if event.type != IDENTITY_USER_DELETED:
            return
        user_id, account_id = event.payload.user_id, event.payload.account_id
        try:
            changed = await erase(user_id, account_id)
        except UserErasureIncomplete as exc:
            _log.error(
                "user not forgotten: personal references remain",
                service=service,
                user_id=str(user_id),
                leftovers=exc.leftovers,
            )
            raise
        _log.info(
            "user forgotten",
            service=service,
            user_id=str(user_id),
            account_id=str(account_id) if account_id else None,
            changed=changed,
        )

    return handle
