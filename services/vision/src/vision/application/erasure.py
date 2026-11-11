# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): what Vision deletes when an account is deleted for good.

Identity asks on ``account.erase.vision`` once the grace period of a deleted account is over,
after Fleet removed the account's mowers.

The use case is the kit's :class:`~mowdeck_service_kit.erasure.AccountErasure` over the parts
Vision owns:

* the purge is :class:`~vision.application.demo.DemoPurge`, the one of the demo sandboxes
  (ADR 0076). It stops the analyses and executions running for the account, then deletes in one
  transaction the proposals with their images (the pictures the model saw, stored in the
  database) and drive steps, the provider requests, the users' own providers with their
  encrypted keys, the preferences with the opt-ins and the consent to a cloud model
  (ADR 0081), and the error watch of the account's mowers; in memory it drops the bus view of
  those mowers and the errors the automatic analysis remembered;
* what is left is counted over every table with an ``account_id`` column and, through the
  account's proposals, the images and drive steps
  (:meth:`vision.application.ports.Store.account_rows`). A row that is still there, or a purge
  that raised, is reported as a failure; Identity asks again.

Nothing is sent to an AI provider as part of an erasure. A request a model already received
stays with its provider under the provider's terms (ADR 0082, "Third parties").
"""

from mowdeck_service_kit.erasure import AccountErasure

from vision.application.demo import DemoPurge
from vision.application.ports import Store


def account_erasure(purge: DemoPurge, store: Store) -> AccountErasure:
    """The erasure of one account in Vision."""
    return AccountErasure(purge.purge_account, store.account_rows)
