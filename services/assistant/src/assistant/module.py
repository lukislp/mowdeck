"""Module descriptor of the assistant, served at ``/.well-known/mowdeck-module``.

The assistant defines no permission of its own: a proposal needs ``can_view`` on the mower,
and the actions it offers follow the caller's ``can_control`` and ``can_edit_schedule``. It
announces the UI area ``/assistant``, which the web app uses to show the chat box on the mower
page only where the add-on is deployed (ADR 0075).
"""

from mowdeck_contracts.v1 import ModuleDescriptor, UiAreaDef

from assistant import __version__

UI_AREA = "/assistant"

DESCRIPTOR = ModuleDescriptor(
    name="assistant",
    version=__version__,
    ui_areas=[
        UiAreaDef(
            route=UI_AREA,
            menu_label="Chat control",
            labels={"de": "Chat-Steuerung", "en": "Chat control"},
            required_permission="mower.control",
        )
    ],
)
