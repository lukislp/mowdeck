"""What Notifications refuses in the public demo (ADR 0068).

The demo keeps every notification in the in-app inbox and sends nothing out: no Web Push
subscription (its endpoint is a third-party push service), no APNs device or Live Activity, no
ntfy topic and no test message. Preferences, the inbox, read and acknowledge work.
"""

from mowdeck_service_kit.demo import DemoPolicy, rule

_P = "/api/v1/notifications"
_OUTBOUND = (
    "The demo sends nothing outside the app: notifications stay in the inbox. "
    "Push, ntfy and the iOS app work in your own MowDeck."
)

DEMO_POLICY = DemoPolicy(
    blocked=(
        rule(rf"{_P}/push-subscriptions(/[^/]+)?", _OUTBOUND),
        rule(rf"{_P}/me/push/apns(/[^/]+)?", _OUTBOUND),
        rule(rf"{_P}/me/live-activities(/.*)?", _OUTBOUND),
        rule(rf"{_P}/ntfy", _OUTBOUND),
        rule(rf"{_P}/test", _OUTBOUND),
    )
)
