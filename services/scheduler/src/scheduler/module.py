"""Module descriptor of scheduler, served at ``/.well-known/mowdeck-module``.

Identity reads it (``MOWDECK_IDENTITY_MODULE_URLS``) and shows the descriptions in role
management. ``schedule.edit`` has a relation in the authorization model
(``can_edit_schedule``), so it can be granted on an account, a site, a group or a mower.
``scheduler.view`` has none: it is an account key that follows ``mower.view`` (``implied_by``);
the API itself checks the computed ``can_view`` on each schedule's target, so a schedule is
visible exactly to those who see its mower or group. The web app's schedules module (the
``ui_area`` ``/schedules``) therefore follows ``mower.view``; it asks for the edit rights per
target and the API decides again on every call.
"""

from mowdeck_contracts.v1 import (
    SCHEDULER_RUN_RECORDED,
    SCHEDULER_SCHEDULE_DISABLED,
    EventDef,
    FeatureDef,
    ModuleDescriptor,
    PermissionDef,
    UiAreaDef,
)

from scheduler import __version__

VIEW_PERMISSION = "scheduler.view"
"""See schedules and their runs (follows ``mower.view``)."""
EDIT_PERMISSION = "schedule.edit"
"""Create, change and delete schedules (``can_edit_schedule``)."""

SCHEDULES_FEATURE = "schedules"
"""Plan feature of schedules (ADR 0028); the limit ``max_schedules`` caps their number."""

_SCHEMAS = "mowdeck-contracts:schemas/v1"

DESCRIPTOR = ModuleDescriptor(
    name="scheduler",
    version=__version__,
    permissions=[
        PermissionDef(
            key=VIEW_PERMISSION,
            description="See schedules and their runs",
            implied_by=["mower.view"],
        ),
        PermissionDef(key=EDIT_PERMISSION, description="Create, change and delete schedules"),
    ],
    features=[FeatureDef(key=SCHEDULES_FEATURE, description="Mowing schedules with rain pause")],
    events=[
        EventDef(type=SCHEDULER_RUN_RECORDED, schema_ref=f"{_SCHEMAS}/ScheduleRunRecorded.json"),
        EventDef(type=SCHEDULER_SCHEDULE_DISABLED, schema_ref=f"{_SCHEMAS}/ScheduleDisabled.json"),
    ],
    ui_areas=[
        UiAreaDef(
            route="/schedules",
            menu_label="Schedules",
            labels={"de": "Zeitpläne", "en": "Schedules"},
            required_permission="mower.view",
        )
    ],
)
