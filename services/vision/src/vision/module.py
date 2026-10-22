"""Module descriptor of vision, served at ``/.well-known/mowdeck-module``.

Vision defines no permission of its own: requesting, approving and rejecting a proposal need
``can_drive`` on the mower (permission key ``mower.drive``), the provider settings belong to the
caller. It declares its plan features, its event and the settings page of the web app.
"""

from mowdeck_contracts.v1 import (
    FEATURE_AI_RECOVER,
    FEATURE_AI_RECOVER_HOSTED,
    VISION_PROPOSAL_CREATED,
    VISION_PROPOSAL_UPDATED,
    EventDef,
    FeatureDef,
    ModuleDescriptor,
    UiAreaDef,
)

from vision import __version__

DESCRIPTOR = ModuleDescriptor(
    name="vision",
    version=__version__,
    features=[
        FeatureDef(
            key=FEATURE_AI_RECOVER,
            description="AI assist (recover a stuck mower, find the dock) with an own provider",
        ),
        FeatureDef(
            key=FEATURE_AI_RECOVER_HOSTED,
            description="AI assist with the vision model the installation provides",
        ),
    ],
    events=[
        EventDef(
            type=VISION_PROPOSAL_CREATED,
            schema_ref="mowdeck-contracts:schemas/v1/VisionProposalCreated.json",
        ),
        EventDef(
            type=VISION_PROPOSAL_UPDATED,
            schema_ref="mowdeck-contracts:schemas/v1/VisionProposalUpdated.json",
        ),
    ],
    ui_areas=[
        UiAreaDef(
            route="/settings/ai-provider",
            menu_label="AI provider",
            labels={"de": "KI-Anbieter", "en": "AI provider"},
            required_permission="mower.drive",
        )
    ],
)
