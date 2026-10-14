"""Permission keys and the relations of the authorization model they map to.

Identity stores roles as sets of permission keys and writes them as ``grant_*`` tuples on the
level of the assignment. Services ask only about the computed ``can_*`` relations, which add
inheritance from the parents and the tenant guard. Keep this table in sync with
``infra/openfga/model.fga``; ``test_authz_permissions.py`` checks it against the packaged model.
"""

from dataclasses import dataclass
from enum import StrEnum

from mowdeck_authz.refs import ACCOUNT, API_TOKEN, GROUP, MOWER, SITE, USER


class Relation(StrEnum):
    """Computed relations services check (never a raw ``grant_*`` relation)."""

    CAN_VIEW = "can_view"
    CAN_CONTROL = "can_control"
    CAN_DRIVE = "can_drive"
    CAN_EDIT_SCHEDULE = "can_edit_schedule"
    CAN_BULK_COMMAND = "can_bulk_command"
    CAN_MANAGE_MOWERS = "can_manage_mowers"
    CAN_MANAGE_USERS = "can_manage_users"
    CAN_MANAGE_BILLING = "can_manage_billing"
    CAN_VIEW_AUDIT = "can_view_audit"
    CAN_MANAGE_ACCOUNT = "can_manage_account"
    CAN_EMERGENCY_STOP = "can_emergency_stop"
    CAN_OPERATOR_MANAGE = "can_operator_manage"
    CAN_MANAGE_ACCOUNTS = "can_manage_accounts"
    """On ``platform:main``: operator admins list and manage accounts (no mower data)."""
    CAN_MANAGE_PLANS = "can_manage_plans"
    """On ``platform:main``: operator admins edit plans and assign them to accounts."""
    MEMBER = "member"


@dataclass(frozen=True, slots=True)
class PermissionRelations:
    """How one permission key appears in the model."""

    key: str
    grant: str
    """Direct, assignable relation written by Identity, e.g. ``grant_mower_control``."""
    computed: Relation
    """Relation services check, e.g. ``can_control``."""
    levels: frozenset[str]
    """Object types on which the grant may be written."""
    subject_types: frozenset[str]
    """Subject types that may hold the grant (API tokens never hold administrative keys)."""


_ALL_LEVELS = frozenset({ACCOUNT, SITE, GROUP, MOWER})
_ANY_SUBJECT = frozenset({USER, API_TOKEN})
_USERS_ONLY = frozenset({USER})


def _entry(
    key: str,
    grant: str,
    computed: Relation,
    levels: frozenset[str] = _ALL_LEVELS,
    subjects: frozenset[str] = _ANY_SUBJECT,
) -> PermissionRelations:
    return PermissionRelations(key, grant, computed, levels, subjects)


PERMISSION_RELATIONS: dict[str, PermissionRelations] = {
    p.key: p
    for p in (
        _entry("mower.view", "grant_mower_view", Relation.CAN_VIEW),
        _entry("mower.control", "grant_mower_control", Relation.CAN_CONTROL),
        _entry("mower.drive", "grant_mower_drive", Relation.CAN_DRIVE),
        _entry("schedule.edit", "grant_schedule_edit", Relation.CAN_EDIT_SCHEDULE),
        _entry(
            "fleet.bulk_command",
            "grant_fleet_bulk_command",
            Relation.CAN_BULK_COMMAND,
            frozenset({ACCOUNT, SITE, GROUP}),
        ),
        _entry("fleet.manage", "grant_fleet_manage", Relation.CAN_MANAGE_MOWERS),
        _entry(
            "users.manage",
            "grant_users_manage",
            Relation.CAN_MANAGE_USERS,
            frozenset({ACCOUNT}),
            _USERS_ONLY,
        ),
        _entry(
            "billing.manage",
            "grant_billing_manage",
            Relation.CAN_MANAGE_BILLING,
            frozenset({ACCOUNT}),
            _USERS_ONLY,
        ),
        _entry(
            "audit.view",
            "grant_audit_view",
            Relation.CAN_VIEW_AUDIT,
            frozenset({ACCOUNT}),
            _USERS_ONLY,
        ),
    )
}
"""Permission key to model relations (design document, section 1.1)."""

GRANT_RELATIONS: frozenset[str] = frozenset(p.grant for p in PERMISSION_RELATIONS.values())
"""All ``grant_*`` relation names."""


def permission(key: str) -> PermissionRelations:
    """Look up a permission key; ``KeyError`` with a clear message if it is unknown."""
    try:
        return PERMISSION_RELATIONS[key]
    except KeyError:
        msg = f"unknown permission key {key!r}"
        raise KeyError(msg) from None


def grant_relation(key: str, object_type: str, subject_type: str = USER) -> str:
    """Return the ``grant_*`` relation of ``key`` on ``object_type`` for ``subject_type``.

    Raises ``ValueError`` when the key cannot be granted on that level or to that subject type
    (for example ``users.manage`` on a mower, or to an API token).
    """
    entry = permission(key)
    if object_type not in entry.levels:
        msg = f"{key} cannot be granted on {object_type}"
        raise ValueError(msg)
    if subject_type not in entry.subject_types:
        msg = f"{key} cannot be granted to {subject_type}"
        raise ValueError(msg)
    return entry.grant
