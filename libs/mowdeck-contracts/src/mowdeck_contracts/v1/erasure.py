# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""Account erasure (ADR 0082): Identity tells every service to delete what it holds of an account.

When the grace period of a deleted account has passed, Identity sends one core NATS request per
service on ``account.erase.<service>`` (:func:`mowdeck_contracts.v1.subjects.account_erase_subject`)
with an :class:`AccountEraseRequest`. The service deletes every row, file and authorization tuple
it keeps for the account and answers with an :class:`AccountEraseReply`.

Semantics every service must keep:

* **Idempotent.** Erasing an account twice, or an account the service never knew, is a success
  with ``deleted=0``. Identity repeats a request until it gets ``ok=True``.
* **Retry safe.** A service that fails (or cannot finish) answers ``ok=False`` with a short
  ``error`` or does not answer at all; nothing is half acknowledged. Identity retries with
  backoff and keeps the account in its purging state until every required service confirmed.
* **Completion is Identity's.** The reply is the confirmation. Identity records which services
  confirmed and deletes its own rows only after the last confirmation, so an account that is
  gone from Identity is gone everywhere.
* **No principal.** Core NATS is reachable by services only (ADR 0074); the request carries no
  token.
"""

from typing import Final
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime

ERASURE_SERVICES: Final[tuple[str, ...]] = (
    "fleet",
    "scheduler",
    "telemetry",
    "notifications",
    "vision",
    "planner",
    "billing",
    "entitlements",
)
"""The services that keep per-account data, in the order Identity asks them, whatever order an
installation lists them in. Two places of this order are rules:

* Fleet is first: removing the mowers stops their simulators and connections, so the other
  services see no further states of the account.
* Billing comes before Entitlements: Billing sets plans there (``entitlements.plan.set``, which
  creates the plan row of any account), so Entitlements erases only after Billing has ended the
  subscriptions and can set nothing for the account any more.

``billing`` is optional (the Compose profile ``billing``, the chart's
``services.billing.enabled``): Identity asks it only where the installation runs it."""


class AccountEraseRequest(ContractModel):
    """Request on ``account.erase.<service>`` (core NATS, internal): delete everything of the
    account."""

    account_id: UUID
    requested_at: UtcDateTime
    reason: str = Field(
        default="account deletion",
        max_length=64,
        description="Why: for the service's log line, never shown to anybody.",
    )


class AccountEraseReply(ContractModel):
    """Reply on ``account.erase.<service>``: ``ok`` once the service holds nothing of the
    account any more (``deleted`` rows removed, 0 when it was already empty), or ``ok=False``
    with an ``error`` (Identity asks again)."""

    ok: bool
    deleted: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=500)
