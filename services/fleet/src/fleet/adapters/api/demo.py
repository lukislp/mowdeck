"""What Fleet refuses in the public demo (ADR 0068).

The visitor drives the prepared simulated mowers: commands, camera, manual drive, spot runs,
area names, mowing defaults, sites and groups all work. Refused: linking manufacturer accounts,
adding, removing or reconfiguring mowers (only simulated ones exist, and the seed job adds them),
the Bluetooth bridge link (it would make the server connect to an address the visitor names)
and RTK base stations of manufacturer accounts: adding, renaming, removing, their firmware and
which base station a mower uses.
"""

from mowdeck_service_kit.demo import DemoPolicy, rule

_P = "/api/v1/fleet"
_MOWER = rf"{_P}/mowers/[^/]+"

DEMO_POLICY = DemoPolicy(
    blocked=(
        rule(
            rf"{_P}/linked-accounts(/.*)?",
            "Manufacturer accounts cannot be linked in the demo; it only has simulated mowers.",
        ),
        rule(
            rf"{_P}/mowers",
            "The demo comes with its simulated mowers; adding mowers needs your own MowDeck.",
            "POST",
        ),
        rule(
            _MOWER,
            "The demo mowers stay as they are; rename and reconfigure mowers in your own MowDeck.",
            "PATCH",
            "DELETE",
        ),
        rule(
            rf"{_MOWER}/links/ble_bridge(/.*)?",
            "Bluetooth bridges cannot be linked in the demo.",
        ),
        rule(
            rf"{_P}/base-stations(/.*)?",
            "RTK base stations cannot be changed in the demo.",
        ),
        rule(
            rf"{_MOWER}/base-station",
            "RTK base stations cannot be changed in the demo.",
        ),
    )
)
