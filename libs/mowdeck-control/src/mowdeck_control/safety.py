"""Blade rules for manual driving, kept as pure functions so they are easy to test and reuse.

Rules (plan: "Risiken", "Sicherheit beim Fahren"):

* Blades are off unless a ``ManualDrive`` frame explicitly asks for them (``blade_on``).
* Blades are forced off when the mower does not allow blades while driving manually
  (``MowerCapabilities.manual_drive_blades``).
* Blades are forced off during transfer moves. Leaving the dock is a transfer, so a frame that
  arrives while the mower is docked or charging never starts the blades.
* Blades never run while a safety sensor stop is active.
"""

from dataclasses import dataclass

from mowdeck_contracts.v1 import ManualDrive, MowerActivity, MowerCapabilities

_TRANSFER_ACTIVITIES = frozenset({MowerActivity.DOCKED, MowerActivity.CHARGING})


@dataclass(frozen=True, slots=True)
class BladeDecision:
    """Whether the blades may run for one drive frame, and why not if forced off."""

    blade_on: bool
    forced_off_reason: str | None = None


def decide_manual_blade(
    frame: ManualDrive, capabilities: MowerCapabilities, activity: MowerActivity
) -> BladeDecision:
    """Apply the blade rules to one ``ManualDrive`` frame."""
    if not frame.blade_on:
        return BladeDecision(blade_on=False)
    if activity is MowerActivity.ERROR:
        return BladeDecision(False, "blades forced off: a sensor stop is active")
    if not capabilities.manual_drive_blades:
        return BladeDecision(
            False, "blades forced off: this mower does not allow blades while driving manually"
        )
    if activity in _TRANSFER_ACTIVITIES:
        return BladeDecision(False, "blades forced off: leaving the dock is a transfer move")
    return BladeDecision(blade_on=True)
