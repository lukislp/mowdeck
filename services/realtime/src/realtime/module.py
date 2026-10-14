"""Module descriptor of realtime, served at ``/.well-known/mowdeck-module``.

Identity reads it (``MOWDECK_IDENTITY_MODULE_URLS``) and shows the description in role
management. ``realtime.view`` has no relation in the authorization model, so it is an account
key that follows ``mower.view`` (``implied_by``): whoever may view mowers may receive their live
updates. The socket checks the computed ``can_view`` on every subscribed mower, never the
token's ``perms``, so a viewer of one group receives exactly that group's mowers.
"""

from mowdeck_contracts.v1 import FeatureDef, ModuleDescriptor, PermissionDef

from realtime import __version__

VIEW_PERMISSION = "realtime.view"
"""Receive live state and command results of mowers over the WebSocket."""

DRIVE_FEATURE = "manual_drive"
"""Plan feature of manual driving (ADR 0028): ``drive_start`` is refused without it."""

DESCRIPTOR = ModuleDescriptor(
    name="realtime",
    version=__version__,
    permissions=[
        PermissionDef(
            key=VIEW_PERMISSION,
            description="Receive live state and command results of mowers",
            implied_by=["mower.view"],
        ),
    ],
    features=[FeatureDef(key=DRIVE_FEATURE, description="Drive mowers manually (joystick)")],
)
