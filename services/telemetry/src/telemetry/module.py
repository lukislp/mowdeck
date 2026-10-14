"""Module descriptor of telemetry, served at ``/.well-known/mowdeck-module``.

Identity reads it (``MOWDECK_IDENTITY_MODULE_URLS``) and shows the description in role
management. ``telemetry.view`` has no relation in the authorization model, so it is an account
key that follows ``mower.view`` (``implied_by``): whoever may view mowers may view their
telemetry. The routes check the computed ``can_view`` on the mower itself, never the token's
``perms``, so a viewer of one group sees the telemetry of exactly that group's mowers.

The ui_area ``/reports`` (mowing time, distance, jobs, availability and faults per site, built
on ``GET /reports/summary``) follows ``mower.view`` for the same reason.
"""

from mowdeck_contracts.v1 import ModuleDescriptor, PermissionDef, UiAreaDef

from telemetry import __version__

VIEW_PERMISSION = "telemetry.view"
"""View live state, history, position track and command timeline of mowers."""

DESCRIPTOR = ModuleDescriptor(
    name="telemetry",
    version=__version__,
    permissions=[
        PermissionDef(
            key=VIEW_PERMISSION,
            description="View live state, history and position track of mowers",
            implied_by=["mower.view"],
        ),
    ],
    ui_areas=[UiAreaDef(route="/reports", menu_label="Reports", required_permission="mower.view")],
)
