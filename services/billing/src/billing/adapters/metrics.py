# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Prometheus metrics of Billing (served by the kit at ``/metrics``).

| Metric | Type | Meaning |
| --- | --- | --- |
| ``mowdeck_billing_manual_refunds_open`` | gauge | refunds of erased accounts the operator |
|  |  | has to pay back by hand and has not settled (ADR 0082); alert on ``> 0`` |

The value comes from the database (every replica reports the same number), refreshed by the
periodic sweep of :class:`~billing.application.manual_refunds.ManualRefunds`.
"""

from prometheus_client import CollectorRegistry, Gauge


class BillingMetrics:
    """Billing's gauges on a Prometheus registry (a private one when metrics are off)."""

    def __init__(self, registry: CollectorRegistry | None) -> None:
        target = registry if registry is not None else CollectorRegistry()
        self._manual_refunds = Gauge(
            "mowdeck_billing_manual_refunds_open",
            "Refunds of erased accounts to pay back by hand, not settled yet",
            registry=target,
        )

    def manual_refunds_open(self, count: int) -> None:
        """Open manual refunds."""
        self._manual_refunds.set(count)
