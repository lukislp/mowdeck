"""What Billing refuses in the public demo (ADR 0068): everything that changes something.

The demo stack does not start Billing (profile ``billing``); should someone add it, no checkout,
customer portal or webhook reaches the payment provider.
"""

from mowdeck_service_kit.demo import DemoPolicy

DEMO_POLICY = DemoPolicy(
    allow_only=(),
    default_reason="There is nothing to buy in the demo.",
)
