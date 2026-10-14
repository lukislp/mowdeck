"""Value objects of a realtime connection (pure, no I/O)."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from mowdeck_contracts.v1 import Principal, PrincipalKind


@dataclass(frozen=True, slots=True)
class Authenticated:
    """A verified access token: who and until when."""

    principal: Principal
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CloseRequest:
    """Why and how a connection ends.

    ``code`` None means the peer is already gone: nothing is sent. ``flush`` sends the queued
    messages (an ``error`` explaining the close) before the close frame.
    """

    code: int | None
    reason: str = ""
    flush: bool = True


def acceptable_principal(principal: Principal) -> bool:
    """Only principals of an account that can hold object rights may open a socket.

    Service principals hold no rights in the authorization model and operator principals have
    no account, so neither can subscribe to anything.
    """
    return principal.kind is not PrincipalKind.SERVICE and principal.account_id is not None


def same_principal(current: Principal, fresh: Principal) -> bool:
    """A refreshed token must belong to the same subject and account."""
    return current.subject == fresh.subject and current.account_id == fresh.account_id


def unique_ids(ids: list[UUID]) -> list[UUID]:
    """``ids`` without duplicates, in their first order."""
    return list(dict.fromkeys(ids))


MAX_CLOSE_REASON_BYTES = 123
"""RFC 6455: the close reason must fit into a control frame."""


def close_reason(text: str) -> str:
    """``text`` cut to fit into a close frame."""
    encoded = text.encode("utf-8")[:MAX_CLOSE_REASON_BYTES]
    return encoded.decode("utf-8", errors="ignore")
