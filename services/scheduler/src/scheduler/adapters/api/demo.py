"""What the Scheduler refuses in the public demo (ADR 0068): only the development weather
switch. Schedules, skip, run now and the mowing angles work; the weather is the static provider
(no request to a weather service)."""

from mowdeck_service_kit.demo import DemoPolicy, rule

DEMO_POLICY = DemoPolicy(
    blocked=(
        rule(
            r"/api/v1/scheduler/dev/.*",
            "The weather of the demo is fixed.",
        ),
    )
)
