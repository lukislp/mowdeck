"""Module descriptor of identity, served at ``/.well-known/mowdeck-module``.

Identity reads its own descriptor too: the keys declared here join the permission catalogue.
"""

from mowdeck_contracts.v1 import (
    IDENTITY_ACCOUNT_CREATED,
    IDENTITY_API_TOKEN_REVOKED,
    IDENTITY_GRANTS_CHANGED,
    IDENTITY_USER_CREATED,
    IDENTITY_USER_DELETED,
    IDENTITY_USER_DISABLED,
    EventDef,
    ModuleDescriptor,
    PermissionDef,
    UiAreaDef,
)

from identity import __version__

USERS_MANAGE = "users.manage"
AUDIT_VIEW = "audit.view"
BILLING_MANAGE = "billing.manage"

_SCHEMAS = "mowdeck-contracts:schemas/v1"

DESCRIPTOR = ModuleDescriptor(
    name="identity",
    version=__version__,
    permissions=[
        PermissionDef(
            key=USERS_MANAGE, description="Manage users, roles and API tokens of the account"
        ),
        PermissionDef(key=AUDIT_VIEW, description="View the audit log of the account"),
        PermissionDef(
            key=BILLING_MANAGE, description="Manage the subscription and payment of the account"
        ),
    ],
    events=[
        EventDef(type=IDENTITY_ACCOUNT_CREATED, schema_ref=f"{_SCHEMAS}/AccountCreated.json"),
        EventDef(type=IDENTITY_USER_CREATED, schema_ref=f"{_SCHEMAS}/UserChanged.json"),
        EventDef(type=IDENTITY_USER_DISABLED, schema_ref=f"{_SCHEMAS}/UserChanged.json"),
        EventDef(type=IDENTITY_USER_DELETED, schema_ref=f"{_SCHEMAS}/UserChanged.json"),
        EventDef(type=IDENTITY_GRANTS_CHANGED, schema_ref=f"{_SCHEMAS}/GrantsChanged.json"),
        EventDef(type=IDENTITY_API_TOKEN_REVOKED, schema_ref=f"{_SCHEMAS}/ApiTokenRevoked.json"),
    ],
    ui_areas=[
        UiAreaDef(
            route="/settings/users", menu_label="Users and roles", required_permission=USERS_MANAGE
        ),
        UiAreaDef(route="/settings/audit", menu_label="Audit log", required_permission=AUDIT_VIEW),
    ],
)
