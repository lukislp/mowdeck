# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Refunds the operator pays back by hand (ADR 0082, section 5).

A refund of an account erasure that cannot be made at the provider is recorded in
``manual_refunds``: by the erasure itself when it skips a refund or cannot place all of a
credit balance, before it deletes the customer at the provider and again (a no-op by key) in
the transaction that deletes the account's rows
(:class:`billing.application.erasure.AccountPurge`), and by the webhook
path when a refund the provider had accepted fails after the erasure was over
(:meth:`ManualRefunds.record_lost`). One record per payment and invoice, whichever path came
first. A record names the payment, the invoice, the amount and why; never the account.

The operator lists the open records and marks one settled once the money went back
(``GET /api/v1/billing/operator/manual-refunds``, ``POST .../{id}/settle``, for operator
admins: ``can_manage_accounts`` on ``platform:main``). :meth:`ManualRefunds.sweep`,
run periodically by the service, deletes settled records after
:data:`~billing.domain.refunds.MANUAL_REFUND_RETENTION` and answers how many are open (the
gauge ``mowdeck_billing_manual_refunds_open``).
"""

from uuid import UUID

from mowdeck_service_kit import get_logger

from billing.application.ledger import PaymentLedger
from billing.application.ports import Clock, PaymentProvider, Store
from billing.domain.models import ManualRefund, ProviderRefund
from billing.domain.refunds import erasure_refund_lost, lost_refund_record, settled_before

_log = get_logger("billing.manual_refunds")

SWEEP_SECONDS = 300.0
"""How often the service sweeps settled records and refreshes the gauge of open ones."""


class ManualRefunds:
    """The record of refunds to pay by hand: written, listed, settled and swept."""

    def __init__(self, store: Store, clock: Clock, ledger: PaymentLedger | None = None) -> None:
        self._store = store
        self._clock = clock
        self._ledger = ledger

    async def listing(self, *, settled: bool = False) -> list[ManualRefund]:
        """The open records, oldest first; with ``settled`` the settled ones too."""
        async with self._store.transaction() as tx:
            return await tx.manual_refunds(settled=settled)

    async def settle(self, refund_id: UUID) -> ManualRefund | None:
        """Mark a record as paid back; None for an unknown id. Settling twice keeps the first
        time. The payments ledger counts the money as refunded (ADR 0083), in the same
        transaction and once."""
        async with self._store.transaction() as tx:
            settled = await tx.settle_manual_refund(refund_id, self._clock())
            if settled is not None and self._ledger is not None:
                await self._ledger.manual_refund(tx, settled)
        if settled is not None:
            _log.info(
                "manual refund settled",
                manual_refund=str(settled.id),
                payment=settled.payment_ref,
                amount=settled.amount,
                currency=settled.currency,
            )
        return settled

    async def sweep(self) -> int:
        """Delete the records settled longer ago than the retention; the number still open."""
        async with self._store.transaction() as tx:
            deleted = await tx.delete_settled_manual_refunds(settled_before(self._clock()))
            still_open = await tx.open_manual_refunds()
        if deleted:
            _log.info("settled manual refunds deleted", deleted=deleted)
        return still_open

    async def record_lost(
        self, refund: ProviderRefund, provider: PaymentProvider, erasure_running: bool
    ) -> ManualRefund | None:
        """A refund of an erasure that the provider reports as failed or canceled: recorded when
        the money is lost for good, None otherwise. Not while the erasure of its account is
        still running (``erasure_running``: Billing still has the account), because the next
        attempt refunds again; and not when another refund of the same erasure and invoice is on
        its way (a later attempt replaced it), which asks the provider for the payment's
        refunds."""
        if erasure_running or refund.payment_ref is None:
            return None
        others = await provider.refunds_of(refund.payment_ref)
        if not erasure_refund_lost(refund, others):
            return None
        record = lost_refund_record(refund, self._clock())
        async with self._store.transaction() as tx:
            inserted = await tx.add_manual_refund(record)
        _log.warning(
            "a refund of an account erasure failed: pay it out by hand",
            refund=refund.id,
            account_id=refund.metadata.get("account_id"),
            status=refund.status.value,
            failure_reason=refund.failure_reason,
            amount=refund.amount,
            currency=refund.currency,
            payment=record.payment_ref,
            invoice=record.invoice_ref,
            recorded=inserted,
        )
        return record if inserted else None
