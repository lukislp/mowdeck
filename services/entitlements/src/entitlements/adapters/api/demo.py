"""What Entitlements refuses in the public demo (ADR 0068): every change of plans and account
plans (operator actions; the demo account keeps its plan)."""

from mowdeck_service_kit.demo import DemoPolicy

DEMO_POLICY = DemoPolicy(
    allow_only=(),
    default_reason="Plans cannot be changed in the demo.",
)
