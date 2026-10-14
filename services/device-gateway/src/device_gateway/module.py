"""Module descriptor of device-gateway, served at ``/.well-known/mowdeck-module``.

Identity, Entitlements and the web app read it at startup: permissions declared here appear in
role management without changes elsewhere.
"""

from mowdeck_contracts.v1 import (
    MOWER_CONNECTED,
    MOWER_DISCONNECTED,
    EventDef,
    ModuleDescriptor,
    PermissionDef,
)

from device_gateway import __version__

VIEW_PERMISSION = "device_gateway.view"
"""Permission that protects the mower listing."""

_CONNECTION_EVENT_SCHEMA = "mowdeck-contracts:schemas/v1/MowerConnectionEvent.json"

DESCRIPTOR = ModuleDescriptor(
    name="device-gateway",
    version=__version__,
    permissions=[
        PermissionDef(
            key=VIEW_PERMISSION,
            description="View the mowers held by the Device-Gateway and their connection status",
            # Account-wide key (no relation in the authorization model): whoever may view the
            # mowers of the whole account may also see their connection status.
            implied_by=["mower.view"],
        ),
    ],
    events=[
        EventDef(type=MOWER_CONNECTED, schema_ref=_CONNECTION_EVENT_SCHEMA),
        EventDef(type=MOWER_DISCONNECTED, schema_ref=_CONNECTION_EVENT_SCHEMA),
    ],
)
