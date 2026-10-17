"""Module descriptor of camera-relay, served at ``/.well-known/mowdeck-module``.

The relay has no public routes and no permissions of its own: Fleet checks ``can_view`` and the
plan feature ``camera`` before it asks for a lease. The descriptor lists the events it
publishes on the stream ``CAMERA``.
"""

from mowdeck_contracts.v1 import EventDef, ModuleDescriptor
from mowdeck_contracts.v1.relay import (
    CAMERA_RELAY_LEASE_CLOSED,
    CAMERA_RELAY_LEASE_OPENED,
    CAMERA_RELAY_STARTED,
    CAMERA_RELAY_STOPPED,
)

from camera_relay import __version__

DESCRIPTOR = ModuleDescriptor(
    name="camera-relay",
    version=__version__,
    events=[
        EventDef(type=CAMERA_RELAY_STARTED, schema_ref="mowdeck_contracts.v1.CameraRelayStarted"),
        EventDef(type=CAMERA_RELAY_STOPPED, schema_ref="mowdeck_contracts.v1.CameraRelayStopped"),
        EventDef(
            type=CAMERA_RELAY_LEASE_OPENED,
            schema_ref="mowdeck_contracts.v1.CameraRelayLeaseOpened",
        ),
        EventDef(
            type=CAMERA_RELAY_LEASE_CLOSED,
            schema_ref="mowdeck_contracts.v1.CameraRelayLeaseClosed",
        ),
    ],
)
