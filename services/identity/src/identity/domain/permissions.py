"""Permission rules: the catalogue, default roles, role validation and anti-escalation.

Two kinds of permission keys exist:

* **model keys** have a ``grant_*`` relation in the OpenFGA model (``mower.control``). They can
  be granted on the levels the model allows and are checked per object.
* **account keys** are declared by a module but have no relation in the model
  (``device_gateway.view``). They can only be granted on the whole account and are enforced
  by the module from the token's ``perms`` claim. ``implied_by`` lets such a key follow model
  keys: holding all of them on the account implies it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from identity.domain.errors import InvalidInput
from identity.domain.models import (
    CatalogueEntry,
    LevelType,
    Membership,
    Role,
    RoleKind,
    SubjectType,
)

OWNER_ROLE_KEY = "owner"
ADMIN_ROLE_KEY = "admin"

VIEW_KEY = "mower.view"
"""Every ``mower.*`` and ``schedule.*`` key needs it in the same role (the model does not let
control imply view, so a role without view would grant blind control)."""

ADMIN_EXCLUDED_KEYS = frozenset({"billing.manage"})
"""Keys the fixed Admin relation does not include (the model: billing is owner only)."""


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    """A role seeded into every new account."""

    key: str
    name: str
    kind: RoleKind
    permissions: tuple[str, ...]
    description: str


DEFAULT_ROLES: tuple[RoleTemplate, ...] = (
    RoleTemplate(
        OWNER_ROLE_KEY,
        "Owner",
        RoleKind.SYSTEM_OWNER,
        (),
        "Everything, including billing and deleting the account. Fixed, not assignable.",
    ),
    RoleTemplate(
        ADMIN_ROLE_KEY,
        "Admin",
        RoleKind.SYSTEM_ADMIN,
        (),
        "Users, roles, tokens, sites and mowers. Fixed; only the owner assigns it.",
    ),
    RoleTemplate(
        "manager",
        "Manager",
        RoleKind.DEFAULT,
        ("mower.view", "mower.control", "schedule.edit", "fleet.bulk_command", "fleet.manage"),
        "Set up mowers, schedules and bulk commands.",
    ),
    RoleTemplate(
        "operator",
        "Operator",
        RoleKind.DEFAULT,
        ("mower.view", "mower.control", "mower.drive"),
        "Mow, drive and spot-mow.",
    ),
    RoleTemplate(
        "viewer",
        "Viewer",
        RoleKind.DEFAULT,
        ("mower.view",),
        "View only.",
    ),
)
"""Design section 3.1. "Integration" is not a role: API tokens carry their own scopes."""


class Catalogue:
    """The permission keys known to the installation."""

    def __init__(self, entries: Iterable[CatalogueEntry]) -> None:
        self._entries = {e.key: e for e in entries if not e.retired}

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def keys(self) -> list[str]:
        """All active keys, sorted."""
        return sorted(self._entries)

    @property
    def entries(self) -> list[CatalogueEntry]:
        """All active entries, sorted by key."""
        return [self._entries[k] for k in self.keys]

    def entry(self, key: str) -> CatalogueEntry:
        """The entry of ``key`` (``InvalidInput`` if unknown)."""
        try:
            return self._entries[key]
        except KeyError:
            msg = f"unknown permission {key!r}"
            raise InvalidInput(msg) from None

    def grantable(self, key: str, level: LevelType, subject: SubjectType) -> bool:
        """True if ``key`` can be held by ``subject`` on ``level``."""
        entry = self._entries.get(key)
        if entry is None or level.value not in entry.levels:
            return False
        return not (subject is SubjectType.API_TOKEN and entry.users_only)

    def effective(self, keys: Iterable[str], level: LevelType, subject: SubjectType) -> list[str]:
        """The subset of ``keys`` that takes effect on ``level`` for ``subject``, sorted."""
        return sorted({k for k in keys if self.grantable(k, level, subject)})

    def owner_keys(self) -> set[str]:
        """Everything (the owner holds every key)."""
        return set(self._entries)

    def admin_keys(self) -> set[str]:
        """Everything but the owner-only keys."""
        return set(self._entries) - ADMIN_EXCLUDED_KEYS

    def implied_account_keys(self, account_keys: set[str]) -> set[str]:
        """Account keys implied by ``account_keys`` (keys held on the whole account)."""
        return {
            e.key
            for e in self._entries.values()
            if not e.in_model and e.implied_by and set(e.implied_by) <= account_keys
        }


def validate_role_permissions(keys: Iterable[str], catalogue: Catalogue) -> list[str]:
    """Normalise and validate the keys of a role (``InvalidInput`` on any problem)."""
    wanted = sorted(set(keys))
    for key in wanted:
        catalogue.entry(key)
    needs_view = [k for k in wanted if k.startswith(("mower.", "schedule.")) and k != VIEW_KEY]
    if needs_view and VIEW_KEY not in wanted:
        msg = f"{', '.join(needs_view)} require {VIEW_KEY} in the same role"
        raise InvalidInput(msg)
    return wanted


def validate_token_grant(keys: Iterable[str], level: LevelType, catalogue: Catalogue) -> list[str]:
    """Validate the keys of one API token grant: every key must be grantable to a token there."""
    wanted = sorted(set(keys))
    if not wanted:
        msg = "a token grant needs at least one permission"
        raise InvalidInput(msg)
    for key in wanted:
        entry = catalogue.entry(key)
        if entry.users_only:
            msg = f"{key} cannot be granted to an API token"
            raise InvalidInput(msg)
        if level.value not in entry.levels:
            msg = f"{key} cannot be granted on {level.value}"
            raise InvalidInput(msg)
    return wanted


@dataclass(frozen=True, slots=True)
class HeldRole:
    """A role a subject holds at a level (for computing token claims)."""

    role: Role
    level_type: LevelType


def token_permissions(
    membership: Membership | None,
    held: Iterable[HeldRole],
    catalogue: Catalogue,
) -> tuple[list[str], list[str]]:
    """``(roles, perms)`` claims of a user in one account.

    ``perms`` is an upper bound: the union of keys held on any level, plus account keys held or
    implied on the whole account. It is a pre-filter for services, never a grant on its own.
    """
    roles: set[str] = set()
    perms: set[str] = set()
    if membership is not None and membership.is_owner:
        roles.add(OWNER_ROLE_KEY)
        perms |= catalogue.owner_keys()
    if membership is not None and membership.is_admin:
        roles.add(ADMIN_ROLE_KEY)
        perms |= catalogue.admin_keys()
    account_level: set[str] = set(perms)
    for item in held:
        if item.role.is_system:
            continue
        roles.add(item.role.key)
        effective = catalogue.effective(item.role.permissions, item.level_type, SubjectType.USER)
        perms.update(effective)
        if item.level_type is LevelType.ACCOUNT:
            account_level.update(effective)
    perms |= catalogue.implied_account_keys(account_level)
    return sorted(roles), sorted(perms)


def held_on_account(
    membership: Membership | None, held: Iterable[HeldRole], catalogue: Catalogue
) -> set[str]:
    """Keys a user holds on the whole account (owner, admin, account-level roles, implied)."""
    keys: set[str] = set()
    if membership is not None and membership.is_owner:
        keys |= catalogue.owner_keys()
    if membership is not None and membership.is_admin:
        keys |= catalogue.admin_keys()
    for item in held:
        if item.level_type is LevelType.ACCOUNT and not item.role.is_system:
            keys.update(
                catalogue.effective(item.role.permissions, item.level_type, SubjectType.USER)
            )
    return keys | catalogue.implied_account_keys(keys)


def roles_by_id(roles: Iterable[Role]) -> Mapping[object, Role]:
    """Index roles by id."""
    return {role.id: role for role in roles}
