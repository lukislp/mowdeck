"""Module descriptor of the Planner, served at ``/.well-known/mowdeck-module``.

Identity reads it (``MOWDECK_IDENTITY_MODULE_URLS``) and shows the permission in role
management; Entitlements reads the feature (``MOWDECK_ENTITLEMENTS_MODULE_URLS``). Previews
follow the computed ``can_view`` of the mower and starting needs ``can_control``, so the
Planner declares no model relation of its own: ``planner.view`` is an account key that follows
``mower.view``. The plan feature ``spot_mowing`` gates every pattern (ADR 0030).
"""

from mowdeck_contracts.v1 import FeatureDef, ModuleDescriptor, PermissionDef

from planner import __version__

VIEW_PERMISSION = "planner.view"
"""See patterns, previews and coverage (follows ``mower.view``)."""

SPOT_MOWING_FEATURE = "spot_mowing"
"""Plan feature of spot mowing with patterns (previews, plans, coverage, templates)."""

DESCRIPTOR = ModuleDescriptor(
    name="planner",
    version=__version__,
    permissions=[
        PermissionDef(
            key=VIEW_PERMISSION,
            description="See mowing patterns, plan previews and coverage",
            implied_by=["mower.view"],
        ),
    ],
    features=[
        FeatureDef(
            key=SPOT_MOWING_FEATURE,
            description="Spot mowing of any shape with mowing patterns and re-mowing gaps",
        )
    ],
)
