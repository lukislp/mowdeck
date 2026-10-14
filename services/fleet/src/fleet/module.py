"""Module descriptor of fleet, served at ``/.well-known/mowdeck-module``.

Identity reads it (``MOWDECK_IDENTITY_MODULE_URLS``) and shows the descriptions in role
management. Every key here has a relation in the authorization model, so it can be granted on
an account, a site, a group or a mower (``fleet.bulk_command``: not on a mower); Fleet checks
the computed relations, never the token's ``perms``.

The ui_area ``/fleet`` (fleet map, sites and groups) follows ``mower.view``: everybody sees the
mowers and places they may view there; editing and bulk commands are checked per object.
"""

from mowdeck_contracts.v1 import (
    CAMERA_FEATURE,
    FLEET_AREA_NAMES_CHANGED,
    MOWER_REGISTERED,
    MOWER_REMOVED,
    MOWER_UPDATED,
    EventDef,
    FeatureDef,
    ModuleDescriptor,
    PermissionDef,
    UiAreaDef,
)

from fleet import __version__

MANAGE_PERMISSION = "fleet.manage"
"""Register, change and remove mowers, sites and groups (``can_manage_mowers``)."""
BULK_PERMISSION = "fleet.bulk_command"
"""Commands to every mower of a site or group (``can_bulk_command``)."""

BULK_FEATURE = "fleet.bulk_commands"
"""Plan feature of bulk commands (ADR 0028). Fleet also enforces the core feature ``control``
(declared by Entitlements) and the limits ``max_mowers``, ``max_sites`` and ``max_groups``."""

_REGISTRATION_SCHEMA = "mowdeck-contracts:schemas/v1/MowerRegistration.json"

DESCRIPTOR = ModuleDescriptor(
    name="fleet",
    version=__version__,
    permissions=[
        PermissionDef(key="mower.view", description="See mowers, their status and commands"),
        PermissionDef(
            key="mower.control", description="Start, pause, resume and dock mowers, set blades"
        ),
        PermissionDef(key="mower.drive", description="Drive mowers manually"),
        PermissionDef(
            key=MANAGE_PERMISSION,
            description="Register, edit and remove mowers, sites and groups",
        ),
        PermissionDef(
            key=BULK_PERMISSION, description="Send a command to every mower of a site or group"
        ),
    ],
    events=[
        EventDef(type=MOWER_REGISTERED, schema_ref=_REGISTRATION_SCHEMA),
        EventDef(type=MOWER_UPDATED, schema_ref=_REGISTRATION_SCHEMA),
        EventDef(type=MOWER_REMOVED, schema_ref=_REGISTRATION_SCHEMA),
        EventDef(
            type=FLEET_AREA_NAMES_CHANGED,
            schema_ref="mowdeck-contracts:schemas/v1/AreaNamesChanged.json",
        ),
    ],
    features=[
        FeatureDef(
            key=CAMERA_FEATURE,
            description="Live camera of the mower (video through the vendor cloud)",
        ),
        FeatureDef(
            key=BULK_FEATURE,
            description="Commands to every mower of a site or group (the emergency stop is "
            "never gated)",
        ),
    ],
    ui_areas=[
        UiAreaDef(route="/mowers", menu_label="Mowers", required_permission="mower.view"),
        UiAreaDef(
            route="/fleet",
            menu_label="Fleet: map, sites and groups",
            required_permission="mower.view",
        ),
    ],
)
