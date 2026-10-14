"""Plan enforcement (ADR 0028): each enforcement point is one call through the Entitlements
client of ``mowdeck-service-kit`` (cached per account, fail-open only while billing is disabled).

* registering a mower: ``max_mowers``; creating a site: ``max_sites``; a group: ``max_groups``
* every command except the emergency stop: feature ``control`` (a ``grace`` or ``expired``
  account can still stop its mowers; no plan change ever stops a mower by itself)
* bulk commands other than the emergency stop: feature ``fleet.bulk_commands``
* a ``start_mow`` with a spot plan (a route of the Planner) and ``spot_mow``: feature
  ``spot_mowing``
* ``set_light`` (the mower's light, switched from the camera panel and by AI assist): feature
  ``camera`` as well

The feature names and the rule per command kind live in ``fleet.domain.rules``
(``command_features``); this module applies them through the Entitlements client.
"""

from uuid import UUID

from mowdeck_service_kit.entitlements import Entitlements, EntitlementsClient

from fleet.domain.errors import FeatureNotInPlanError, PlanLimitError
from fleet.domain.rules import FEATURE_CONTROL, FEATURE_LIGHT, FEATURE_SPOT

FEATURE_BULK = "fleet.bulk_commands"
"""Bulk commands to a site or group (the emergency stop is never gated)."""
EMERGENCY_STOP = "emergency_stop"
SET_LIGHT = "set_light"

__all__ = [
    "EMERGENCY_STOP",
    "FEATURE_BULK",
    "FEATURE_CONTROL",
    "FEATURE_LIGHT",
    "FEATURE_SPOT",
    "SET_LIGHT",
    "allow_all",
    "require_feature",
    "require_limit",
]


def allow_all() -> Entitlements:
    """A client without a bus and with billing disabled: allows everything (tests, no NATS)."""
    return EntitlementsClient(None, billing_enabled=False)


async def require_limit(
    entitlements: Entitlements, account_id: UUID, key: str, current: int
) -> None:
    """Raise :class:`PlanLimitError` unless the account may have one more of ``key``."""
    reply = await entitlements.check_limit(account_id, key, current)
    if not reply.allowed:
        raise PlanLimitError(reply, key)


async def require_feature(entitlements: Entitlements, account_id: UUID, feature: str) -> None:
    """Raise :class:`FeatureNotInPlanError` unless the account may use ``feature``."""
    reply = await entitlements.check_feature(account_id, feature)
    if not reply.allowed:
        raise FeatureNotInPlanError(reply, feature)
