"""Module descriptor of entitlements, served at ``/.well-known/mowdeck-module``.

Entitlements declares the core features every plan should list: ``view`` and
``emergency_stop`` (never taken away, ADR 0028) and ``control`` (every command except the
emergency stop, enforced by Fleet). The other features come from the modules that enforce them.
It defines no permission: every member reads the own account's plan, and the operator
endpoints are checked as ``platform:main#can_manage_plans`` in OpenFGA.
"""

from mowdeck_contracts.v1 import (
    FEATURE_CONTROL,
    FEATURE_EMERGENCY_STOP,
    FEATURE_VIEW,
    FeatureDef,
    ModuleDescriptor,
)

from entitlements import __version__

DESCRIPTOR = ModuleDescriptor(
    name="entitlements",
    version=__version__,
    features=[
        FeatureDef(key=FEATURE_VIEW, description="View mowers, maps and history (always kept)"),
        FeatureDef(key=FEATURE_CONTROL, description="Mower commands other than the emergency stop"),
        FeatureDef(
            key=FEATURE_EMERGENCY_STOP, description="Emergency stop of a mower (always kept)"
        ),
    ],
)
