# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A deleted user (ADR 0082, "delete me"): what Vision forgets of a person.

Identity publishes ``identity.user.deleted`` when a member deletes themselves or an admin deletes
a sub-user. The account goes on, and so do its proposals: who asked for an analysis, who approved
it and who decided it is the account's record of what the mower did, like Fleet's commands.
What goes:

* the person's own rows, every table with a ``user_id`` column, read from the schema: their own
  model with its encrypted key and headers (``user_providers``), their preferences with the
  opt-in to automatic analyses and the consent to a cloud model (``user_preferences``), and the
  cost accounting of the model calls they made (``provider_requests``). Those rows also count
  towards the daily caps; the account's cap for hosted analyses counts that person's calls of
  the day no more;
* the person's name wherever Vision copied it as part of a ``PrincipalRef``: who requested,
  approved and decided a proposal (``proposals.requested_by``, ``approved_by``,
  ``decided_by``). The reference keeps its opaque subject id, as ``requested_by_subject`` and
  ``approved_by_subject`` do; once Identity's user row is gone they name nobody.

Vision keeps nothing of a user in memory (analyses and executions are per proposal and mower).

The use case is the kit's :class:`~mowdeck_service_kit.user_erasure.UserErasure` over the store:
the delete and scrub (:meth:`vision.application.ports.Store.forget_user`), then a count over the
same tables and columns (:meth:`~vision.application.ports.Store.user_leftovers`). Something left
fails the event, which comes again.

A disabled user is a different event: ``identity.user.disabled`` stops their own model and
opt-ins (:meth:`~vision.application.ports.Store.delete_user`) and keeps their cost rows and
their name, because the user may be enabled again.
"""

from mowdeck_service_kit.user_erasure import UserErasure

from vision.application.ports import Store


def user_erasure(store: Store) -> UserErasure:
    """The erasure of one user in Vision."""
    return UserErasure(store.forget_user, store.user_leftovers)
