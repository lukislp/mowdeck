# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Module descriptor of billing, served at ``/.well-known/mowdeck-module``.

Billing defines no permission of its own: the account endpoints are checked as
``can_manage_billing`` (permission key ``billing.manage``, declared by Identity; owner only by
default, admins excluded). It declares the event it produces.
"""

from mowdeck_contracts.v1 import BILLING_SUBSCRIPTION_CHANGED, EventDef, ModuleDescriptor

from billing import __version__

DESCRIPTOR = ModuleDescriptor(
    name="billing",
    version=__version__,
    events=[
        EventDef(
            type=BILLING_SUBSCRIPTION_CHANGED,
            schema_ref="mowdeck_contracts.v1.SubscriptionChanged",
        )
    ],
)
