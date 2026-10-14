"""Module descriptor of notifications, served at ``/.well-known/mowdeck-module``.

Identity reads it (``MOWDECK_IDENTITY_MODULE_URLS``) and shows the description in role
management. ``notifications.manage`` has no relation in the authorization model, so it is an
account key that follows ``mower.view`` (``implied_by``): whoever may view the account's mowers
may manage their own notifications. A user who views mowers only below the account (a group or
a single mower) is admitted as well, because the API checks the computed ``can_view`` on the
mowers themselves; notifications never cover a mower the user cannot view.
"""

from mowdeck_contracts.v1 import (
    NOTIFICATION_SENT,
    EventDef,
    FeatureDef,
    ModuleDescriptor,
    PermissionDef,
    UiAreaDef,
)

from notifications import __version__

MANAGE_PERMISSION = "notifications.manage"
"""Manage own notification settings and read own notifications."""

NOTIFICATIONS_FEATURE = "notifications"
"""Plan feature of notifications (grants ``notifications.push`` too: keys are hierarchical)."""
PUSH_FEATURE = "notifications.push"
"""Plan feature of Web Push: storing a browser subscription is refused without it."""

_SCHEMAS = "mowdeck-contracts:schemas/v1"

DESCRIPTOR = ModuleDescriptor(
    name="notifications",
    version=__version__,
    permissions=[
        PermissionDef(
            key=MANAGE_PERMISSION,
            description="Manage own notification settings and read own notifications",
            implied_by=["mower.view"],
        ),
    ],
    features=[
        FeatureDef(key=NOTIFICATIONS_FEATURE, description="Notifications: inbox, rules, channels"),
        FeatureDef(key=PUSH_FEATURE, description="Web Push notifications to the browser"),
    ],
    events=[EventDef(type=NOTIFICATION_SENT, schema_ref=f"{_SCHEMAS}/NotificationSent.json")],
    # The web app's inbox and settings; every viewer of a mower may use them (see above), so the
    # area follows ``mower.view`` rather than the account-level key.
    ui_areas=[
        UiAreaDef(
            route="/notifications",
            menu_label="Notifications",
            labels={"de": "Meldungen", "en": "Notifications"},
            required_permission="mower.view",
        )
    ],
)
