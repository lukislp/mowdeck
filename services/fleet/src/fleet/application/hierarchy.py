"""The tuples Fleet owns: where an object sits in ``account > site > group > mower``.

Only ``account`` and ``parent`` relations, written through ``TupleWriter(owner="fleet")``
(ADR 0014). Grants on these objects are Identity's job.
"""

from collections.abc import Iterable
from uuid import UUID

from mowdeck_authz import RelationshipTuple, account, group, mower, site

from fleet.domain.models import Group, Mower, Site


def site_tuples(item: Site) -> list[RelationshipTuple]:
    """``site:<id>#account@account:<acc>``."""
    return [RelationshipTuple(account(item.account_id).as_subject(), "account", site(item.id))]


def group_parent(group_id: UUID, site_id: UUID) -> RelationshipTuple:
    """``group:<id>#parent@site:<site>``."""
    return RelationshipTuple(site(site_id).as_subject(), "parent", group(group_id))


def group_tuples(item: Group) -> list[RelationshipTuple]:
    """``group#account`` and, for a group inside a site, ``group#parent``."""
    found = [RelationshipTuple(account(item.account_id).as_subject(), "account", group(item.id))]
    if item.site_id is not None:
        found.append(group_parent(item.id, item.site_id))
    return found


def mower_site_parent(mower_id: UUID, site_id: UUID) -> RelationshipTuple:
    """``mower:<id>#parent@site:<site>``."""
    return RelationshipTuple(site(site_id).as_subject(), "parent", mower(mower_id))


def mower_group_parent(mower_id: UUID, group_id: UUID) -> RelationshipTuple:
    """``mower:<id>#parent@group:<group>``."""
    return RelationshipTuple(group(group_id).as_subject(), "parent", mower(mower_id))


def mower_tuples(item: Mower, group_ids: Iterable[UUID]) -> list[RelationshipTuple]:
    """``mower#account``, ``mower#parent@site`` and one ``mower#parent@group`` per group."""
    return [
        RelationshipTuple(account(item.account_id).as_subject(), "account", mower(item.id)),
        mower_site_parent(item.id, item.site_id),
        *(mower_group_parent(item.id, g) for g in sorted(set(group_ids), key=str)),
    ]
